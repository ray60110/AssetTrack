"""
assettrack/cross_model.py — 跨模型總結建議（用各訊號的回測可信度加權，主頁一張總結）

bug#00096（使用者需求：第二步「跨模型的分析建議」——橫跨四大功能統整出一個最佳投資
建議，放在主頁）。

原則：四大功能各自從自己的面向給方向，本模組把「有回測背書」的三項（主動式ETF／期權／
類股）各自的**淨方向分數**（多−空，正規化到 −1…+1）以**該項回測可信度**加權，合成一個
整體傾向；「近期重大事件」不投方向票（依使用者決定維持資訊性），改作**謹慎度修正**——
若近日有 FED/CPI/NFP 等重大總經事件，提示降低把握、等待塵埃落定。

可信度直接沿用 bug#00094 的統計驗證：
  • 樣本不足（n<20）→ 權重 0（該項這次棄權，不硬湊方向）。
  • 樣本足但未顯著優於基準 → 低權重（0.2）。
  • 顯著（未過多重檢定）→ 中權重（0.5）。
  • 顯著且過多重比較調整 → full 權重（1.0）。
所有可信訊號權重皆為 0 時，誠實回報「資料累積中，尚無足夠可信訊號」，不給假結論。

純離線、零網路。輸入是各功能既有的 report/flows 與回測（皆含 significance），輸出一個
結構供主頁卡片直接渲染。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

_READY_MIN_N = 20
_READY_MIN_ESS = 3     # bug#00111（使用者審查 #5）：有效獨立樣本數下限
_NEUTRAL_BAND = 0.15   # |score| 小於此視為中性
_STRONG_BAND = 0.5     # |score| 大於此視為強烈


TARGET_HORIZON = 14  # bug#00097 pt3：跨模型總結統一以 14 天為預測區間，各模型權重取此前瞻期


def _best_direction_sig_at(backtest: Optional[dict], horizon: int) -> Optional[dict]:
    """取指定前瞻期 horizon 下、樣本較多之方向的顯著性摘要（對齊同一時間維度）。"""
    st = (backtest or {}).get("by_horizon", {}).get(horizon, {})
    best = None
    for d in ("up", "down"):
        sig = (st.get("significance") or {}).get(d)
        if sig and (best is None or sig["n"] > best["n"]):
            best = sig
    return best


def _reliability(backtest: Optional[dict], horizon: int = TARGET_HORIZON) -> tuple:
    """回測可信度 → (權重 0..1, 標籤)，一律以「目標前瞻期 horizon（14 天）」評估，讓不同
    原生時間尺度的模型（ETF 60 天、期權 14 天、類股 5 天）在同一時間維度上比較可信度。
    該前瞻期無回測/樣本不足 → 0（棄權，不硬湊）。"""
    sig = _best_direction_sig_at(backtest, horizon)
    # bug#00111（使用者審查 #5）：就緒門檻與顯著性口徑一致——除原始 n≥20 外，另要求
    # 有效獨立樣本數 ESS≥3。避免長前瞻期下 ESS=floor(n/h) 被自相關砍到 1 時，仍以
    # 「1 個有效樣本」給出權重（此時 Wilson CI 近乎 (0,1)、顯著性無意義）。
    if sig is None or sig["n"] < _READY_MIN_N or sig.get("ess", 0) < _READY_MIN_ESS:
        return 0.0, "資料累積中"
    if sig["significant_adj"]:
        return 1.0, "顯著"
    if sig["significant_95"]:
        return 0.5, "偏顯著(未過多重檢定)"
    return 0.2, "未顯著"


def _etf_direction_score(etf_report: Optional[dict], min_etfs_evaluated: int = 4) -> tuple:
    up = dn = 0
    for info in (etf_report or {}).get("symbols", {}).values():
        if info.get("etfs_evaluated", 0) < min_etfs_evaluated:
            continue
        if info.get("consensus") == "up":
            up += 1
        elif info.get("consensus") == "down":
            dn += 1
    tot = up + dn
    return ((up - dn) / tot if tot else 0.0), up, dn


def _options_direction_score(verdict_report: Optional[dict]) -> tuple:
    up = dn = 0
    for v in (verdict_report or {}).get("verdicts", {}).values():
        if not v.get("ready"):
            continue
        if v.get("direction") == "多":
            up += 1
        elif v.get("direction") == "空":
            dn += 1
    tot = up + dn
    return ((up - dn) / tot if tot else 0.0), up, dn


def _sector_direction_score(flows: Optional[dict]) -> tuple:
    up = dn = 0
    for f in (flows or {}).values():
        if not f.get("ready"):
            continue
        if f.get("direction") == "up":
            up += 1
        elif f.get("direction") == "down":
            dn += 1
    tot = up + dn
    return ((up - dn) / tot if tot else 0.0), up, dn


def _direction_word(score: float) -> str:
    if abs(score) < _NEUTRAL_BAND:
        return "中性觀望"
    strong = abs(score) >= _STRONG_BAND
    if score > 0:
        return "強烈偏多" if strong else "偏多"
    return "強烈偏空" if strong else "偏空"


def _parse_event_date(d) -> Optional[date]:
    if isinstance(d, date):
        return d
    try:
        return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _event_caution(upcoming_macro, today: date, within_days: int = 3) -> Optional[str]:
    """最近一個 within_days 天內的重大總經事件 → 謹慎提示；無則 None。
    upcoming_macro: [(date, label, time_str), ...]（shared.get_upcoming_macro_events 格式）。"""
    if not upcoming_macro:
        return None
    soon = []
    for item in upcoming_macro:
        try:
            d = _parse_event_date(item[0])
            label = item[1]
        except Exception:
            continue
        if d is None:
            continue
        delta = (d - today).days
        if 0 <= delta <= within_days:
            soon.append((delta, label))
    if not soon:
        return None
    soon.sort()
    delta, label = soon[0]
    name = {"▼FED": "FED 利率決議", "★NFP": "NFP 非農", "◆CPI": "CPI 通膨"}.get(label, label)
    when = "今日" if delta == 0 else f"{delta} 日內"
    return f"⚠️ {when}有重大總經事件（{name}），建議降低單邊把握、等待數據塵埃落定。"


def synthesize_cross_model(
    etf_report: Optional[dict] = None,
    etf_backtest: Optional[dict] = None,
    options_verdict_report: Optional[dict] = None,
    options_backtest: Optional[dict] = None,
    sector_flows: Optional[dict] = None,
    sector_backtest: Optional[dict] = None,
    upcoming_macro=None,
    today: Optional[date] = None,
    etf_min_etfs_evaluated: int = 4,
    target_horizon: int = TARGET_HORIZON,
) -> dict:
    """把三項有回測背書的方向訊號以回測可信度加權，合成整體傾向；事件作謹慎度修正。"""
    etf_s, etf_up, etf_dn = _etf_direction_score(etf_report, etf_min_etfs_evaluated)
    opt_s, opt_up, opt_dn = _options_direction_score(options_verdict_report)
    sec_s, sec_up, sec_dn = _sector_direction_score(sector_flows)

    feats = [
        {"key": "etf", "name": "主動式ETF共識", "score": etf_s, "up": etf_up, "down": etf_dn,
         "backtest": etf_backtest},
        {"key": "options", "name": "期權方向結論", "score": opt_s, "up": opt_up, "down": opt_dn,
         "backtest": options_backtest},
        {"key": "sector", "name": "類股板塊共識", "score": sec_s, "up": sec_up, "down": sec_dn,
         "backtest": sector_backtest},
    ]

    contributions = []
    total_w = 0.0
    weighted_sum = 0.0
    n_significant = 0
    for f in feats:
        w, rel_label = _reliability(f["backtest"], target_horizon)
        active = (f["up"] + f["down"]) > 0
        eff_w = w if active else 0.0
        if eff_w > 0:
            total_w += eff_w
            weighted_sum += eff_w * f["score"]
            if w >= 1.0:
                n_significant += 1
        contributions.append({
            "key": f["key"], "name": f["name"],
            "direction_score": round(f["score"], 3),
            "up": f["up"], "down": f["down"],
            "weight": round(eff_w, 3), "reliability": rel_label,
        })

    caution = _event_caution(upcoming_macro, today) if today else None

    from .shared import Recommendation, _section

    def _contrib_lines() -> str:
        bits = []
        for c in contributions:
            if c["weight"] <= 0:
                bits.append(f"{c['name']}：{c['reliability']}（棄權，權重 0）")
            else:
                lean = "偏多" if c["direction_score"] > 0.05 else "偏空" if c["direction_score"] < -0.05 else "中性"
                bits.append(f"{c['name']}：{lean} {c['up']}多/{c['down']}空，方向分數 {c['direction_score']:+.2f}，"
                            f"權重 {c['weight']:.2f}（{c['reliability']}）")
        return "；".join(bits)

    _rel_expl = ("各項在 14 天前瞻期的回測可信度→權重：樣本不足(n<20)→0（棄權，不硬湊方向）、"
                 "未顯著→0.2、顯著→0.5、顯著且過多重比較調整→1.0。淨方向分數＝(多−空)正規化到 −1…+1。"
                 "「近期重大事件」不投方向票，改作謹慎度修正。")

    if total_w <= 0:
        head_line = (f"🧭 跨模型總結（預測區間 {target_horizon} 天）：目前尚無足夠「經"
                     f"回測背書」的可信訊號，持續使用系統累積真實快照（尤其 {target_horizon} "
                     f"天前瞻樣本累積最慢）後才會給出加權總結（不硬湊方向）。")
        rec = Recommendation(
            rec_id="cross_model", category="cross_model", direction=None,
            verdict=f"🧭 跨模型總結建議（預測區間 {target_horizon} 天）：資料累積中",
            basis="三項方向訊號目前皆未達回測可信門檻，總結棄權不給方向。",
            detail_sections=[_section(
                "加權合成公式與可信度",
                formula="整體分數 = Σ(各項權重 × 各項淨方向分數) ÷ Σ權重",
                substitution=f"目前各項貢獻：{_contrib_lines()}",
                explanation=_rel_expl)],
        )
        return {
            "overall_direction": "資料累積中",
            "score": None, "confidence": "—",
            "contributions": contributions,
            "event_caution": caution,
            "summary_lines": [head_line],
            "recommendation": rec,
        }

    score = weighted_sum / total_w
    direction = _direction_word(score)

    if n_significant >= 2 and abs(score) >= 0.3:
        confidence = "高"
    elif n_significant >= 1:
        confidence = "中"
    else:
        confidence = "低"

    # 卡片文字
    head = f"🧭 跨模型總結建議（預測區間 {target_horizon} 天）：【{direction}】（加權分數 {score:+.2f}，把握度：{confidence}）"
    detail_bits = []
    for c in contributions:
        if c["weight"] <= 0:
            detail_bits.append(f"{c['name']}：{c['reliability']}（棄權）")
        else:
            lean = "偏多" if c["direction_score"] > 0.05 else "偏空" if c["direction_score"] < -0.05 else "中性"
            detail_bits.append(
                f"{c['name']}：{lean} {c['up']}多/{c['down']}空，權重 {c['weight']:.2f}（{c['reliability']}）")
    summary_lines = [head, "　依據：" + "；".join(detail_bits)]
    if caution:
        summary_lines.append("　" + caution)
    if confidence == "低":
        summary_lines.append("　（把握度低：貢獻訊號多未達統計顯著，總結僅供參考。）")

    # bug#00117：三層結構化建議（單一真理來源；主頁卡片 detail_headline 可點選進公式頁）。
    dir_word = "多" if score > 0.05 else "空" if score < -0.05 else "觀望"
    rec_sections = [_section(
        "加權合成公式",
        formula="整體分數 = Σ(各項權重 × 各項淨方向分數) ÷ Σ權重；映射為 強烈偏多/偏多/中性觀望/偏空/強烈偏空",
        substitution=(f"整體分數 = {score:+.2f} → 【{direction}】（把握度 {confidence}）\n"
                      f"各項貢獻：{_contrib_lines()}"),
        explanation=_rel_expl)]
    if caution:
        rec_sections.append(_section("近期重大事件謹慎度修正", substitution=caution,
                                     explanation="近 3 日內若有 FED／CPI／NFP 等重大總經事件，提示降低單邊把握、等待數據塵埃落定；事件不改變方向分數，僅作把握度提示。"))
    rec = Recommendation(
        rec_id="cross_model", category="cross_model", direction=dir_word,
        verdict=head,
        basis="三項有回測背書的方向訊號（ETF／期權／類股）各自的淨方向分數，以其 14 天前瞻回測可信度加權合成。",
        detail_sections=rec_sections,
    )

    return {
        "overall_direction": direction,
        "score": round(score, 3),
        "confidence": confidence,
        "contributions": contributions,
        "event_caution": caution,
        "summary_lines": summary_lines,
        "recommendation": rec,
    }
