"""
assettrack/sector_analysis.py — 類股板塊分析：板塊「廣度」共同漲跌偵測（離線運算）

sector_analysis 新功能。目標（使用者 item#4）：抓出市場針對特定類股族群的「共同」
買進上漲、共同賣出下跌 —— 不是單一名股在動，而是整個族群普遍同向。

演算法：廣度擴散指數 (breadth diffusion) + 持續性過濾 (persistence)。

  1. 廣度 (breadth) = (#上漲成分股 − #下跌成分股) / #有報價成分股 ，範圍 −1…+1。
     這是「普遍」的核心訊號：10 檔裡 8 漲 ≈ +0.6。
  2. 市值加權報酬 (cap-weighted return) = Σ wᵢ·rᵢ（wᵢ 為成分股市值權重）。
     這同時就是使用者 item#1 要的板塊「市值漲跌%」，並用來確認漲跌有份量、非微幅雜訊。
  3. 當日判為「普遍上漲」：廣度 ≥ +0.5 且 市值加權報酬 > 門檻（兩訊號需一致，
     沿用本專案 ETF 分析「兩個真實訊號需同向」的紀律）。
  4. 每日累計持續性 (item#4「每日累計追蹤」)：只有當某板塊在最近 N 天中有 ≥ K 天
     呈同向普遍走勢，才在 summary dashboard 標記出來，用以區分「持續性共同買賣」與
     「單日雜訊」。

這個模組不打任何網路請求 —— 純讀取 storage 逐日真實累積的板塊快照
(load_sector_daily_snapshots)。沒有真實快照就沒有廣度趨勢，不回填、不臆測；
資料不足時誠實回報 ready=False 供畫面顯示「資料收集中」。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional


# Validated forecast horizon: 10 NYSE sessions (~two calendar weeks), matching
# scripts/validate_sector_two_week_market_samples.py.
SECTOR_FORWARD_SESSIONS = 10
SECTOR_FORWARD_HORIZON_LABEL = "未來 10 個交易日（約兩週）"


# ── 幣別正規化 (bug#00085) ───────────────────────────────────────────────────
# 重大既有缺陷：市值加權先前直接把不同幣別的 marketcap 相加。yfinance 回傳的市值
# 以「該股本地幣別」計價，例如 005930.KS（三星）市值 1,444,643,883,320,000 KRW，
# 而 MU（美光）926,700,995,278 USD —— 韓元數值大約 1560 倍，導致兩檔韓股在
# 「存儲記憶體」板塊佔了 99.94% 權重，美光只剩 0.04%。該板塊的「市值漲跌%」因此
# 幾乎等於韓股報酬，畫面的「佔比」欄也是錯的。
#
# 修法：依代碼後綴推斷計價幣別，換算為 USD 後再加權。取不到匯率時「絕不硬加」，
# 退回等權（equal weight）並明確回報 weighting 模式，讓畫面誠實標示。
_CURRENCY_BY_SUFFIX: dict[str, str] = {
    ".KS": "KRW", ".KQ": "KRW", ".T": "JPY", ".HK": "HKD",
    ".TW": "TWD", ".TWO": "TWD", ".L": "GBP", ".SS": "CNY", ".SZ": "CNY",
    ".DE": "EUR", ".PA": "EUR", ".AS": "EUR", ".MI": "EUR",
    ".MC": "EUR", ".BR": "EUR", ".VI": "EUR", ".HE": "EUR", ".LS": "EUR",
    ".SW": "CHF", ".ST": "SEK", ".OL": "NOK", ".CO": "DKK",
    ".TO": "CAD", ".V": "CAD", ".AX": "AUD", ".NS": "INR", ".BO": "INR",
}


def infer_currency(symbol: str) -> str:
    """由 ticker 後綴推斷市值計價幣別（yfinance 的 marketcap 以本地幣別計價）。
    無後綴（美股）視為 USD。純字串推斷、不打網路、不臆測數值。"""
    s = str(symbol or "").upper()
    for suf, cur in _CURRENCY_BY_SUFFIX.items():
        if s.endswith(suf):
            return cur
    return "USD"


def _mc_usd(m: dict, fx: Optional[dict] = None) -> Optional[float]:
    """成分股市值換算為 USD。fx 為 {currency: 該幣別兌 USD 的匯率}（1 單位外幣 =
    fx[cur] USD）。USD 恆為 1.0；缺匯率回 None（不猜、不當成 1:1）。"""
    mc = m.get("marketcap")
    if not mc:
        return None
    cur = m.get("currency") or infer_currency(m.get("symbol", ""))
    if cur == "USD":
        return float(mc)
    rate = (fx or {}).get(cur)
    return float(mc) * float(rate) if rate else None


def cap_weights(members: list[dict], fx: Optional[dict] = None) -> tuple[dict[str, float], str]:
    """回傳 ({symbol: 權重0~1}, weighting模式)。

    模式 'marketcap'：全部有市值的成分股都能換算為 USD → 用真實市值權重。
    模式 'equal'    ：成分股跨幣別但缺匯率（無法公平比較）→ 退回等權，避免讓
                     高面額幣別（如 KRW）憑數值大小灌爆權重。"""
    usd = {}
    missing = False
    for m in members:
        if m.get("marketcap"):
            v = _mc_usd(m, fx)
            if v is None:
                missing = True
            else:
                usd[m.get("symbol")] = v
    total = sum(usd.values())
    if not missing and total > 0:
        return {k: v / total for k, v in usd.items()}, "marketcap"
    # 等權後備：只計有報價的成分股
    syms = [m.get("symbol") for m in members if m.get("marketcap") or m.get("day_pct") is not None]
    if not syms:
        return {}, "equal"
    w = 1.0 / len(syms)
    return {s: w for s in syms}, "equal"


def _cap_weighted(members: list[dict], field: str, fx: Optional[dict] = None) -> Optional[float]:
    """Cap-weighted average of `field`, with currencies normalised to USD.
    Falls back to equal weighting when the group spans currencies we have no FX
    for — never silently sums mixed-currency market caps. None if nothing qualifies."""
    weights, _mode = cap_weights(members, fx)
    num = 0.0
    den = 0.0
    for m in members:
        v = m.get(field)
        w = weights.get(m.get("symbol"))
        if w and v is not None:
            num += w * v
            den += w
    return round(num / den, 2) if den else None


def _breadth(members: list[dict]) -> tuple[int, int, int, Optional[float]]:
    """Return (n_up, n_down, n_rated, breadth) from members' real day_pct.
    breadth = (n_up - n_down) / n_rated in −1…+1; None if no member has a real
    day_pct."""
    n_up = sum(1 for m in members if m.get("day_pct") is not None and m["day_pct"] > 0)
    n_down = sum(1 for m in members if m.get("day_pct") is not None and m["day_pct"] < 0)
    n_rated = sum(1 for m in members if m.get("day_pct") is not None)
    breadth = round((n_up - n_down) / n_rated, 3) if n_rated else None
    return n_up, n_down, n_rated, breadth


def summarize_group(members_data: dict[str, dict], symbols: list[str],
                    fx: Optional[dict] = None) -> dict:
    """Build one group's current-day view (item#1 / item#8) from freshly-fetched
    member data (quotes.fetch_sector_members_data output).

    Returns total_marketcap, cap-weighted day/week/month % (the group's 「市值漲跌
    %」), today's up/down breadth, and the per-member rows (sorted by day_pct desc
    for display — biggest gainer on top, per item#1)."""
    # bug#00091：投資建議一律以美股為主——排除台股成分股（.TW/.TWO 結尾），
    # 讓廣度/市值加權/共識與回測皆不含台股；台股持倉追蹤不受影響。
    symbols = [s for s in symbols if not str(s).upper().endswith((".TW", ".TWO"))]
    members: list[dict] = []
    for sym in symbols:
        d = dict(members_data.get(sym, {}) or {})
        d["symbol"] = sym
        d.setdefault("currency", infer_currency(sym))
        members.append(d)

    # bug#00085：權重改走 cap_weights()，市值一律換算 USD 後才比較；跨幣別又缺匯率
    # 時退回等權並回報 weighting='equal'（畫面須標示），不再讓 KRW 之類的高面額
    # 幣別憑數值大小佔走 99% 權重。
    weights, weighting = cap_weights(members, fx)
    total_mc_usd = 0.0
    for m in members:
        v = _mc_usd(m, fx)
        if v:
            total_mc_usd += v
    # item#1「佔比」：即實際採用的權重（%），與 capw 口徑完全一致。
    for m in members:
        w = weights.get(m.get("symbol"))
        m["weight"] = round(w * 100, 2) if w else None

    n_up, n_down, n_rated, breadth = _breadth(members)
    members_sorted = sorted(
        members,
        key=lambda m: (m.get("day_pct") is not None, m.get("day_pct") or 0.0),
        reverse=True,
    )
    return {
        "total_marketcap": total_mc_usd if total_mc_usd else None,
        "weighting": weighting,
        "capw_day": _cap_weighted(members, "day_pct", fx),
        "capw_week": _cap_weighted(members, "week_pct", fx),
        "capw_month": _cap_weighted(members, "month_pct", fx),
        "n_up": n_up,
        "n_down": n_down,
        "n_rated": n_rated,
        "breadth": breadth,
        "members": members_sorted,
    }


def compute_breadth_history(snapshots: list[dict]) -> list[dict]:
    """Per-day breadth diffusion + cap-weighted return from real daily snapshots
    (storage.load_sector_daily_snapshots output). Returns one row per snapshot:
    {date, breadth, capw, n_up, n_down, n_rated}, ascending by date."""
    rows = []
    for snap in sorted(snapshots, key=lambda s: s.get("date", "")):
        members = snap.get("members", []) or []
        n_up, n_down, n_rated, breadth = _breadth(members)
        rows.append({
            "date": snap.get("date"),
            "breadth": breadth,
            "capw": _cap_weighted(members, "day_pct"),
            "n_up": n_up,
            "n_down": n_down,
            "n_rated": n_rated,
        })
    return rows


def detect_broad_flow(
    snapshots: list[dict],
    lookback: int = 5,
    min_days: int = 3,
    breadth_threshold: float = 0.5,
    capw_threshold: float = 0.1,
) -> dict:
    """Persistence filter over the breadth history (item#4). A day counts as
    「普遍上漲」when breadth ≥ +breadth_threshold AND cap-weighted return >
    +capw_threshold (both real signals must agree); 「普遍下跌」is the mirror.

    Over the last `lookback` days with real data, if ≥ `min_days` are broadly up
    (and up dominates), the group's direction is "up"; mirror for "down";
    otherwise "none". ready=False when there aren't `min_days` real snapshots yet
    (honest 「資料收集中」, never a fabricated conclusion).

    Returns {ready, direction, up_days, down_days, days_evaluated, lookback,
    min_days, latest_breadth, latest_capw, first_date, last_date}."""
    history = compute_breadth_history(snapshots)
    window = history[-lookback:]
    evaluated = len(window)

    up_days = sum(
        1 for r in window
        if r["breadth"] is not None and r["capw"] is not None
        and r["breadth"] >= breadth_threshold and r["capw"] > capw_threshold
    )
    down_days = sum(
        1 for r in window
        if r["breadth"] is not None and r["capw"] is not None
        and r["breadth"] <= -breadth_threshold and r["capw"] < -capw_threshold
    )

    ready = evaluated >= min_days
    direction = "none"
    if ready:
        if up_days >= min_days and up_days >= down_days:
            direction = "up"
        elif down_days >= min_days:
            direction = "down"

    latest = history[-1] if history else {}
    return {
        "ready": ready,
        "direction": direction,
        "up_days": up_days,
        "down_days": down_days,
        "days_evaluated": evaluated,
        "lookback": lookback,
        "min_days": min_days,
        "latest_breadth": latest.get("breadth"),
        "latest_capw": latest.get("capw"),
        "first_date": window[0]["date"] if window else None,
        "last_date": window[-1]["date"] if window else None,
    }


def _sector_backtest_section(direction: str, backtest: "Optional[dict]"):
    """把 sector_backtest_note() 的回測結論收成第三層 breakdown section（bug#00117）。"""
    from .shared import _section
    note = sector_backtest_note(direction, backtest).replace("　▶ 回測：", "").strip()
    return _section(
        "回測驗證（walk-forward 命中率）",
        formula="命中率 = 訊號後前瞻 h 日『市值加權報酬複利成的類指數』方向正確次數 ÷ 可評估訊號數；超額 edge = 命中率 − 基準上漲率",
        substitution=note,
        explanation=("回測呼叫與畫面同一判斷函式（detect_broad_flow），每個歷史日只餵 ≤T 的真實快照、無前視偏誤；"
                     "顯著性經 Wilson CI＋對基準二項檢定（ESS 消自相關、Bonferroni 多重比較）。樣本 < 20 標『資料累積中』。"))


def assess_sector_composite(
    flows: "dict[str, dict]",
    confirmations: "Optional[dict[str, dict]]" = None,
) -> "dict[str, dict]":
    """Count the three independently observable sector-direction votes.

    The production policy is deliberately asymmetric.  Two bullish votes and no
    bearish conflict create an experimental bullish candidate.  A risk alert
    requires contemporaneous breadth-down plus SMA5 < SMA20; lagging 6/12-month
    momentum and SMA5/150 can block a long but cannot raise a warning by
    themselves.  Missing and neutral inputs are abstentions rather than
    implicit votes.
    """
    confirmations = confirmations or {}
    assessments: dict[str, dict] = {}
    for name in sorted(set(flows) | set(confirmations)):
        flow = flows.get(name) or {}
        confirmation = confirmations.get(name) or {}
        breadth = (
            flow.get("direction")
            if flow.get("ready") and flow.get("direction") in ("up", "down")
            else "none"
        )
        momentum_breadth = (
            confirmation.get("direction")
            if confirmation.get("ready")
            and confirmation.get("direction") in ("up", "down")
            else "none"
        )
        trend = (
            confirmation.get("trend_direction")
            if confirmation.get("trend_ready")
            and confirmation.get("trend_direction") in ("up", "down")
            else "none"
        )
        votes = {
            "breadth_3_of_5": breadth,
            "relative_momentum_50ma": momentum_breadth,
            "sma_5_150": trend,
        }
        up_votes = sum(direction == "up" for direction in votes.values())
        down_votes = sum(direction == "down" for direction in votes.values())
        fast_trend = (
            confirmation.get("fast_trend_direction")
            if confirmation.get("fast_trend_ready")
            and confirmation.get("fast_trend_direction") in ("up", "down")
            else "none"
        )
        status = "abstain"
        if up_votes >= 2 and down_votes == 0:
            status = "bullish_candidate"
        elif breadth == "down" and fast_trend == "down":
            status = "risk_alert"
        assessments[name] = {
            "status": status,
            "up_votes": up_votes,
            "down_votes": down_votes,
            "votes": votes,
        }
    return assessments


def generate_sector_risk_warnings(
    flows: "dict[str, dict]",
    confirmations: "Optional[dict[str, dict]]" = None,
) -> "list[str]":
    """Headline lines for two-week down forecasts; not a short-sale instruction."""
    return [
        rec.verdict
        for rec in generate_sector_recommendations(flows, confirmations)
        if rec.direction == "空"
    ]


def generate_sector_recommendations(
    flows: "dict[str, dict]",
    confirmations: "Optional[dict[str, dict]]" = None,
    backtest: "Optional[dict]" = None,
) -> "list":
    """Emit two-week directional forecasts from the live composite policy.

    Longs require 2-of-3 bullish votes and zero bearish votes.  Downs require
    contemporaneous breadth-down plus SMA5 < SMA20.  The horizon is the
    validated 10 NYSE-session window.  Down forecasts are not short-sale
    advice.  The unused ``backtest`` argument stays for call compatibility.
    """
    from .shared import Recommendation, _section
    recs: list = []
    confirmations = confirmations or {}
    assessments = assess_sector_composite(flows, confirmations)
    ranked = sorted(
        (
            (name, row) for name, row in assessments.items()
            if row["status"] == "bullish_candidate"
        ),
        key=lambda kv: (
            kv[1]["up_votes"],
            (flows.get(kv[0]) or {}).get("up_days") or 0,
        ),
        reverse=True,
    )
    for name, assessment in ranked:
        f = flows.get(name) or {}
        confirmation = confirmations.get(name) or {}
        votes = assessment["votes"]
        pct_above = confirmation.get("pct_above_50ma")
        pct_above_text = (
            f"{pct_above * 100:.0f}%" if pct_above is not None else "—"
        )
        vote_text = "、".join(
            label for key, label in (
                ("breadth_3_of_5", "breadth 3-of-5"),
                ("relative_momentum_50ma", "相對動能＋50MA breadth"),
                ("sma_5_150", "SMA5/150 趨勢"),
            )
            if votes[key] == "up"
        )
        capw = f.get("latest_capw")
        recs.append(Recommendation(
            rec_id=f"sector:{name}", category="sector",
            direction="多",
            verdict=f"📈 【預測】{name}：{SECTOR_FORWARD_HORIZON_LABEL}上漲",
            basis=(
                f"2-of-3 規則由 {vote_text} 投下多方票，且沒有任何偏空票；"
                f"預測標的是訊號日後 {SECTOR_FORWARD_SESSIONS} 個交易日的板塊等權方向。"
            ),
            detail_sections=[_section(
                "Vote A：breadth 3-of-5",
                formula=("廣度 = (上漲成分股數 − 下跌成分股數) ÷ 有報價成分股數（範圍 −1…+1）；"
                         "某日判『普遍上漲』需 廣度 ≥ breadth_threshold(0.5) 且 市值加權報酬 > capw_threshold(0.1%)"),
                substitution=(f"{name}：近 {f.get('days_evaluated') or 0} 個交易日中 "
                              f"{f.get('up_days') or 0} 天上漲；本票＝{votes['breadth_3_of_5']}"
                              f"{('，最新市值加權報酬 ' + format(capw, '+.2f') + '%') if capw is not None else ''}"),
                explanation="最近 5 個合格交易日中至少 3 日同向才通過第一關。"),
                _section(
                    "Vote B：相對動能＋50MA breadth",
                    formula=(
                        "score = 0.5×z(6月動能，排除最近1月) + 0.5×z(12月動能，排除最近1月)；"
                        "六板塊 top/bottom 2 再要求 50MA breadth ≥60%/≤40%"
                    ),
                    substitution=(
                        f"{name}：相對動能排名方向＝{confirmation.get('rank_direction')}；"
                        f"成分股站上 50MA＝{pct_above_text}；本票＝{votes['relative_momentum_50ma']}；"
                        f"資料日＝{confirmation.get('as_of') or '—'}"
                    ),
                    explanation="排名與成分股 50MA breadth 同時符合時，才形成第二票。",
                ),
                _section(
                    "Vote C：等權板塊指數 SMA5/150",
                    formula="以成分股日報酬等權建立板塊指數；SMA5 > SMA150 為多方票。",
                    substitution=(
                        f"{name}：SMA5＝{confirmation.get('sma5') or '—'}；"
                        f"SMA150＝{confirmation.get('sma150') or '—'}；"
                        f"本票＝{votes['sma_5_150']}"
                    ),
                    explanation=(
                        f"預測窗與驗證相同：訊號日後 {SECTOR_FORWARD_SESSIONS} 個交易日的"
                        "板塊等權報酬方向。"
                    ),
                )],
        ))
    down_ranked = sorted(
        (
            (name, row) for name, row in assessments.items()
            if row["status"] == "risk_alert"
        ),
        key=lambda kv: (
            (flows.get(kv[0]) or {}).get("down_days") or 0,
            kv[0],
        ),
        reverse=True,
    )
    for name, assessment in down_ranked:
        f = flows.get(name) or {}
        confirmation = confirmations.get(name) or {}
        recs.append(Recommendation(
            rec_id=f"sector:{name}", category="sector",
            direction="空",
            verdict=f"📉 【預測】{name}：{SECTOR_FORWARD_HORIZON_LABEL}下跌",
            basis=(
                f"breadth 3-of-5 偏空且 SMA5 < SMA20；"
                f"預測標的是訊號日後 {SECTOR_FORWARD_SESSIONS} 個交易日的板塊等權方向。"
                "這不是放空建議。"
            ),
            detail_sections=[_section(
                "即時空方確認",
                formula="breadth 3-of-5 為空，且等權板塊指數 SMA5 < SMA20。",
                substitution=(
                    f"{name}：breadth＝{assessment['votes']['breadth_3_of_5']}；"
                    f"SMA5＝{confirmation.get('sma5') or '—'}；"
                    f"SMA20＝{confirmation.get('sma20') or '—'}；"
                    f"近 {f.get('days_evaluated') or 0} 日中 "
                    f"{f.get('down_days') or 0} 天偏空"
                ),
                explanation=(
                    f"與驗證相同，預測 {SECTOR_FORWARD_HORIZON_LABEL}的板塊等權方向；"
                    "慢速動能／SMA150 只擋多方，不單獨構成下跌預測。"
                ),
            )],
        ))
    return recs


def generate_sector_conclusions(
    flows: dict[str, dict],
    confirmations: "Optional[dict[str, dict]]" = None,
    backtest: "Optional[dict]" = None,
) -> list[str]:
    """薄 wrapper（bug#00117）：以 generate_sector_recommendations 為單一真理來源，投影為
    主頁用的「一句話」字串清單。畫面完整三層改由 recs 直接渲染。"""
    from .shared import dashboard_line
    return [
        dashboard_line(r)
        for r in generate_sector_recommendations(
            flows, confirmations=confirmations, backtest=backtest
        )
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward backtest for the sector breadth/flow signal (bug#00093)
# ─────────────────────────────────────────────────────────────────────────────
# Same discipline as the options (calibration.py) and ETF (analysis.py) backtests:
# 100% offline, zero network, no backfill, no fabrication — built purely on the
# real daily sector snapshots storage accumulates (sector_cache/history/*.jsonl).
# The signal validated is *exactly* what the sector card shows: detect_broad_flow()'s
# persistent 「普遍上漲/下跌」direction. For each historical day T taken "as now" we
# recompute the direction using ONLY snapshots ≤ T (no look-ahead), then check
# whether the group's own real forward cap-weighted return (a compounded index of
# the same capw the breadth history uses) over ≥ horizon days moved the predicted
# way. Hit rates per look-ahead vs the baseline up-rate give the edge; sample <
# min_signals is honestly flagged (0 on day one, by design). Report shape matches
# calibration.backtest_verdicts so calibration.calibration_status_label() works.

_sector_bt_cache: dict = {}
_SECTOR_BT_CACHE_MAX = 8


def _sector_data_signature(snapshots_by_group: dict) -> tuple:
    return tuple(sorted(
        (g, len(s or []), (sorted(s, key=lambda r: r.get("date", ""))[-1].get("date") if s else None))
        for g, s in snapshots_by_group.items()
    ))


def _capw_index(hist: list[dict]) -> dict:
    """Compound the per-day cap-weighted return (capw, in %) into a group price-like
    index level per date (starts at 1.0). Days with no real capw carry the level
    forward unchanged (no fabricated move)."""
    level = 1.0
    idx: dict = {}
    for row in hist:
        capw = row.get("capw")
        if capw is not None:
            level *= (1.0 + capw / 100.0)
        idx[row.get("date")] = level
    return idx


def backtest_sector_flow(
    snapshots_by_group: dict[str, list[dict]],
    horizons: tuple = (1, 5, 10, 14, 30, 60),  # 含 30/60 天長線前瞻期（bug#00106）
    lookback: int = 5,
    min_days: int = 3,
    breadth_threshold: float = 0.5,
    capw_threshold: float = 0.1,
    min_signals: int = 20,
) -> dict:
    """Walk-forward calibration of the sector 「普遍上漲/下跌」flow (1/5/10-day
    look-aheads). Returns a report shaped identically to
    calibration.backtest_verdicts (so calibration_status_label() works)."""
    cache_key = (_sector_data_signature(snapshots_by_group), tuple(horizons),
                 lookback, min_days, round(breadth_threshold, 3),
                 round(capw_threshold, 3), min_signals)
    if cache_key in _sector_bt_cache:
        return _sector_bt_cache[cache_key]

    up = {h: [] for h in horizons}
    down = {h: [] for h in horizons}
    baseline = {h: [] for h in horizons}
    records: list = []  # bug#00094: 逐訊號紀錄，供子區間穩定性檢定

    groups_evaluated = 0
    all_dates: set = set()

    for group, raw in snapshots_by_group.items():
        snaps = sorted([s for s in (raw or []) if s.get("date")], key=lambda s: s["date"])
        if len(snaps) < 2:
            continue
        groups_evaluated += 1
        hist = compute_breadth_history(snaps)
        idx = _capw_index(hist)
        dates = [row["date"] for row in hist]
        all_dates.update(dates)
        parsed = [datetime.strptime(d, "%Y-%m-%d").date() for d in dates]

        for i, T in enumerate(dates):
            entry = idx.get(T)
            if not entry or entry <= 0:
                continue
            direction = detect_broad_flow(
                snaps[: i + 1], lookback=lookback, min_days=min_days,
                breadth_threshold=breadth_threshold, capw_threshold=capw_threshold,
            )["direction"]
            for h in horizons:
                target = parsed[i] + timedelta(days=h)
                fwd = None
                for j in range(i + 1, len(dates)):
                    if parsed[j] >= target:
                        exit_lvl = idx.get(dates[j])
                        if exit_lvl and exit_lvl > 0:
                            fwd = exit_lvl / entry - 1.0
                        break
                if fwd is None:
                    continue
                baseline[h].append(fwd)
                if direction == "up":
                    up[h].append(fwd)
                    records.append({"date": T, "h": h, "dir": "up", "hit": fwd > 0})
                elif direction == "down":
                    down[h].append(fwd)
                    records.append({"date": T, "h": h, "dir": "down", "hit": fwd < 0})

    def _hit_rate(xs, expect_up):
        if not xs:
            return None
        return sum(1 for x in xs if (x > 0) == expect_up) / len(xs)

    def _mean(xs):
        return (sum(xs) / len(xs)) if xs else None

    by_horizon = {}
    for h in horizons:
        evaluated = len(up[h]) + len(down[h])
        by_horizon[h] = {
            "baseline_up_rate": _hit_rate(baseline[h], True),
            "baseline_n": len(baseline[h]),
            "up_n": len(up[h]),
            "up_hit_rate": _hit_rate(up[h], True),
            "up_mean_fwd": _mean(up[h]),
            "down_n": len(down[h]),
            "down_hit_rate": _hit_rate(down[h], False),
            "down_mean_fwd": _mean(down[h]),
            "evaluated_signals": evaluated,
            "ready": evaluated >= min_signals,
        }

    dates_sorted = sorted(all_dates)
    result = {
        "horizons": list(horizons),
        "lookback": lookback,
        "min_days": min_days,
        "min_signals": min_signals,
        "groups_evaluated": groups_evaluated,
        "total_signal_days": len(dates_sorted),
        "first_date": dates_sorted[0] if dates_sorted else None,
        "last_date": dates_sorted[-1] if dates_sorted else None,
        "by_horizon": by_horizon,
    }

    from .backtest_stats import attach_significance
    attach_significance(result, records)

    if len(_sector_bt_cache) >= _SECTOR_BT_CACHE_MAX:
        _sector_bt_cache.clear()
    _sector_bt_cache[cache_key] = result
    return result


def sector_backtest_note(direction: str, backtest: "Optional[dict]", min_signals: int = 20) -> str:
    """One-line 回測 hit-rate suffix for a sector 「類股共識」bullet, style-matched to
    the options/ETF cards. `direction` is "up"/"down"."""
    if not backtest or not backtest.get("by_horizon"):
        return "　▶ 回測：訊號樣本累積中，命中率尚無法估計"
    key_n = "up_n" if direction == "up" else "down_n"
    key_hit = "up_hit_rate" if direction == "up" else "down_hit_rate"
    by_h = backtest["by_horizon"]
    order = [h for h in (5, 10, 1) if h in by_h] + [h for h in by_h if h not in (5, 10, 1)]
    best_h = max(order, key=lambda h: (by_h[h].get(key_n) or 0)) if order else None
    if best_h is None or not by_h[best_h].get(key_n):
        return "　▶ 回測：訊號樣本累積中，命中率尚無法估計"
    st = by_h[best_h]
    n, hit, base = st[key_n], st[key_hit], st.get("baseline_up_rate")
    edge_s = ""
    if hit is not None and base is not None:
        edge = (hit - base) if direction == "up" else (hit - (1 - base))
        edge_s = f"，超額 {edge * 100:+.0f}pp"
    note = f"　▶ 回測：前瞻{best_h}日同向共識命中率 {hit * 100:.0f}%（n={n}{edge_s}）"
    from .backtest_stats import significance_phrase
    note += significance_phrase(backtest, best_h, direction)
    if n < max(5, backtest.get("min_signals", min_signals) // 2):
        note += "（樣本偏少，僅供參考）"
    return note


# ═════════════════════════════════════════════════════════════════════════════
# 市場級「普遍性大跌」偵測 (bug#00085)
# ═════════════════════════════════════════════════════════════════════════════
# 復盤發現 detect_broad_flow() 對「單日崩盤」完全失聲：2026-07-23 科技七巨頭七檔
# 全跌、市值加權 -3.91%（廣度 -1.0 為理論極值），卻因持續性過濾要求「5 天中 3 天」
# 而只計為 1 天 → 判定 none，警訊遲至 7/25 才出現（晚 2 個交易日）。
#
# 更深層的問題是「門檻對不同板塊不等價」：
#   • 成分股越少，廣度極值越廉價。光通訊僅 5 檔，breadth=-1.0 在半數交易日都會發生；
#     科技七巨頭 7 檔，14 天僅 1 次。同一個 ±0.5 門檻對兩者意義天差地遠。
#   • 固定 % 門檻同樣不等價。-2% 對七巨頭是 14 天僅見的重挫，對光通訊只是日常波動
#     （|capw|>3% 的天數：七巨頭 1/14，光通訊 10/14）。
#
# 因此本層一律改用「相對化」判準，兩個訊號都必須同向且顯著（沿用本專案「兩個真實
# 訊號需同向」的紀律）：
#   1. 廣度顯著性：二項式尾機率 P(X≥k | n, p=0.5)，自動校正成分股數差異。
#      5 檔全跌 p=1/32≈0.031；7 檔全跌 p=1/128≈0.0078 —— 後者顯著 4 倍。
#   2. 報酬極端性：z-score = (當日 capw − 該板塊自身歷史均值) / 自身標準差。
#      以各板塊「自己的」波動度為尺，讓高波動與低波動板塊站上同一基準。
#
# 三個新訊號：
#   A. 單日嚴重度 (severity)  —— 繞過持續性過濾，當日極端即示警（解決「晚兩天」）。
#   B. 市場廣度 (market breadth) —— 跨板塊聚合，回答「普遍性」（單一板塊無法回答）。
#   D. 基準對照 (benchmark)  —— 區分「大盤普跌」與「類股獨有賣壓」（解決偽普遍性）。
#
# 全部純離線運算，只讀已累積的真實快照；資料不足時誠實回報 ready=False。

# 相對化判準的預設參數
SEVERITY_P_THRESHOLD: float = 0.05     # 廣度二項式尾機率門檻
SEVERITY_Z_THRESHOLD: float = 2.0      # capw z-score 門檻（σ）
BASELINE_WINDOW: int = 20              # z-score 基準取樣窗（交易日）
BASELINE_MIN: int = 8                  # 算 z-score 所需最少樣本
MARKET_STRESS_RATIO: float = 0.5       # 幾成板塊同時顯著下跌才算市場級訊號


def _binom_tail_p(k: int, n: int) -> float:
    """P(X ≥ k) where X ~ Binomial(n, 0.5) — 在「漲跌各半」的虛無假設下，n 檔中至少
    k 檔同向的機率。用來校正成分股數差異：5 檔全跌 p≈0.031、7 檔全跌 p≈0.0078。
    n≤0 或 k≤0 時回 1.0（不顯著）。"""
    if n <= 0 or k <= 0:
        return 1.0
    if k > n:
        return 0.0
    total = 0
    c = 1  # C(n, 0)
    for i in range(0, n + 1):
        if i >= k:
            total += c
        c = c * (n - i) // (i + 1)
    return total / (2 ** n)


def breadth_pvalue(n_up: int, n_down: int, n_rated: int) -> tuple[float, float]:
    """回傳 (p_down, p_up)：分別為「至少這麼多檔下跌 / 上漲」的二項式尾機率。
    值越小代表越不可能是隨機造成，即『共同買賣』的證據越強。"""
    return _binom_tail_p(n_down, n_rated), _binom_tail_p(n_up, n_rated)


def _mean_std(vals: list[float]) -> tuple[Optional[float], Optional[float]]:
    """樣本平均與樣本標準差（n-1）。樣本 < 2 或無變異時標準差回 None。"""
    xs = [v for v in vals if v is not None]
    n = len(xs)
    if n < 2:
        return (xs[0] if xs else None), None
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1)
    sd = var ** 0.5
    return mean, (sd if sd > 1e-12 else None)


def classify_days(
    snapshots: list[dict],
    baseline_window: int = BASELINE_WINDOW,
    baseline_min: int = BASELINE_MIN,
    p_threshold: float = SEVERITY_P_THRESHOLD,
    z_threshold: float = SEVERITY_Z_THRESHOLD,
    require_complete: bool = True,
) -> list[dict]:
    """逐日標記「顯著普遍上漲 / 下跌」，改用相對化判準取代固定門檻。

    每個交易日的 z-score 只用『該日之前』的資料當基準（嚴格無前視偏誤），且預設只
    採計 session_complete 的完整交易日（避免用盤中未完成的走勢污染統計基準）。舊格式
    快照沒有 session_complete 欄位，視為完整（向後相容）。

    回傳每日一列：{date, breadth, capw, n_up, n_down, n_rated, p_down, p_up,
    z, sig_down, sig_up, complete, baseline_n}。"""
    rows = compute_breadth_history(snapshots)
    by_date = {s.get("date"): s for s in snapshots}
    out: list[dict] = []
    capw_hist: list[float] = []          # 只累積『已完成交易日』的 capw 當基準

    for r in rows:
        snap = by_date.get(r["date"], {})
        complete = snap.get("session_complete", True)
        p_down, p_up = breadth_pvalue(r["n_up"], r["n_down"], r["n_rated"])

        base = capw_hist[-baseline_window:]
        mean, sd = _mean_std(base)
        z = None
        if r["capw"] is not None and mean is not None and sd and len(base) >= baseline_min:
            z = (r["capw"] - mean) / sd

        usable = (not require_complete) or complete
        # 「嚴重度」判準（訊號A）：廣度顯著 + 自身波動 z-score 極端，兩者都要。
        # 這是單一板塊獨自構成警訊所需的高門檻。
        sig_down = bool(
            usable and z is not None and p_down <= p_threshold and z <= -z_threshold
        )
        sig_up = bool(
            usable and z is not None and p_up <= p_threshold and z >= z_threshold
        )
        # 「參與」判準（訊號B用）：只要廣度顯著且方向向下即可，不要求幅度極端。
        # 理由：市場級的顯著性應由「同時參與的板塊數」本身提供，而非要求每個板塊
        # 各自都達極端值 —— 後者對高波動板塊門檻過嚴，會讓普遍性大跌完全測不出來。
        part_down = bool(usable and p_down <= p_threshold and (r["capw"] or 0) < 0)
        part_up = bool(usable and p_up <= p_threshold and (r["capw"] or 0) > 0)

        out.append({
            **r,
            "p_down": p_down, "p_up": p_up, "z": (round(z, 2) if z is not None else None),
            "sig_down": sig_down, "sig_up": sig_up,
            "part_down": part_down, "part_up": part_up,
            "complete": complete, "baseline_n": len(base),
        })
        # 基準只收完整交易日，且在『之後』才加入 → 當日不參與自己的基準
        if complete and r["capw"] is not None:
            capw_hist.append(r["capw"])
    return out


def detect_severity_event(snapshots: list[dict], **kw) -> dict:
    """訊號A：單日嚴重度。回傳最新一個交易日是否構成『單日極端普遍漲跌』，
    完全繞過持續性過濾 —— 這正是 2026-07-23 那類單日崩盤被漏掉的原因。

    回傳 {ready, date, direction('up'/'down'/'none'), breadth, capw, z, p, complete}。"""
    rows = classify_days(snapshots, **kw)
    if not rows:
        return {"ready": False, "direction": "none", "date": None}
    last = rows[-1]
    direction = "down" if last["sig_down"] else ("up" if last["sig_up"] else "none")
    return {
        "ready": last["z"] is not None,
        "date": last["date"],
        "direction": direction,
        "breadth": last["breadth"],
        "capw": last["capw"],
        "z": last["z"],
        "p": last["p_down"] if direction == "down" else last["p_up"],
        "n_rated": last["n_rated"],
        "complete": last["complete"],
        "baseline_n": last["baseline_n"],
    }


def compute_composite_index(snapshots_by_group: dict[str, list[dict]]) -> dict[str, dict]:
    """離線市場代理：把所有板塊全部成分股合併（依 symbol 去重，避免同一檔股票出現在
    多個板塊被重複計數），算出每日「全體市值加權報酬」與「全體廣度」。

    用途是在沒有 SPY/QQQ 真實基準時（例如離線回測歷史快照）仍能提供市場級對照。
    它不是真正的大盤指數 —— 只涵蓋使用者自訂的板塊universe，故回傳 source='composite'
    以便畫面誠實標示。"""
    by_date: dict[str, dict[str, dict]] = {}
    for _g, snaps in snapshots_by_group.items():
        for s in snaps:
            d = s.get("date")
            if not d:
                continue
            slot = by_date.setdefault(d, {})
            for m in (s.get("members") or []):
                sym = m.get("symbol")
                if sym and sym not in slot:      # 跨板塊去重
                    slot[sym] = m

    out: dict[str, dict] = {}
    for d, members_map in by_date.items():
        members = list(members_map.values())
        n_up, n_down, n_rated, breadth = _breadth(members)
        out[d] = {
            "date": d,
            "capw": _cap_weighted(members, "day_pct"),
            "breadth": breadth,
            "n_up": n_up, "n_down": n_down, "n_rated": n_rated,
            "source": "composite",
        }
    return out


def compute_market_breadth(
    snapshots_by_group: dict[str, list[dict]],
    **kw,
) -> list[dict]:
    """訊號B：跨板塊市場廣度。逐日統計「有幾個板塊同時顯著普遍下跌／上漲」。

    這是單一板塊層級永遠回答不了的問題 —— 使用者要的「普遍性大跌」指的正是多個
    類股族群同時遭到共同賣出。各板塊是否算顯著，一律用 classify_days() 的相對化
    判準（二項式顯著性＋自身波動 z-score），所以小板塊不會因成分股少而灌水。

    回傳每日一列：{date, n_sectors, n_down, n_up, ratio_down, ratio_up,
    diffusion, down_sectors, up_sectors}，ascending by date。"""
    per_group = {g: classify_days(s, **kw) for g, s in snapshots_by_group.items()}
    dates = sorted({r["date"] for rows in per_group.values() for r in rows if r.get("date")})

    out: list[dict] = []
    for d in dates:
        downs, ups, n = [], [], 0
        for g, rows in per_group.items():
            r = next((x for x in rows if x["date"] == d), None)
            if not r:
                continue
            n += 1
            if r["part_down"]:
                downs.append(g)
            elif r["part_up"]:
                ups.append(g)
        if not n:
            continue
        out.append({
            "date": d,
            "n_sectors": n,
            "n_down": len(downs), "n_up": len(ups),
            "ratio_down": round(len(downs) / n, 3),
            "ratio_up": round(len(ups) / n, 3),
            "diffusion": round((len(ups) - len(downs)) / n, 3),
            "down_sectors": downs, "up_sectors": ups,
        })
    return out


def detect_market_stress(
    snapshots_by_group: dict[str, list[dict]],
    benchmark_by_date: Optional[dict[str, dict]] = None,
    stress_ratio: float = MARKET_STRESS_RATIO,
    **kw,
) -> dict:
    """訊號B+D 合併：判定最新交易日是否出現「市場普遍性賣壓／買盤」，並以基準對照
    區分成因。

    分類（direction='down' 時）：
      • 'market_wide'    大盤基準本身也顯著下跌 → 整體市場在跌，非類股獨有。
      • 'sector_specific' 基準持平／上漲但多數板塊重挫 → 資金針對這些類股族群賣出。
      • 'unknown'        無可用基準（誠實標示，不臆測）。

    benchmark_by_date 可為 compute_composite_index() 的輸出（source='composite'，
    離線代理）或真實 SPY/QQQ（source='benchmark'）。"""
    series = compute_market_breadth(snapshots_by_group, **kw)
    if not series:
        return {"ready": False, "direction": "none", "date": None}
    last = series[-1]

    # 幅度確認：除了「多少板塊同時參與」，還要求整體複合指數當日的變動相對它自己的
    # 歷史夠顯著（z ≤ -1σ），避免「多數板塊都小跌一點」被誤判為普遍性大跌。
    # 兩個訊號需同向（廣度參與度 + 整體幅度），沿用本專案一貫紀律。
    comp_all = compute_composite_index(snapshots_by_group)
    comp_series = [comp_all[d]["capw"] for d in sorted(comp_all) if comp_all[d]["capw"] is not None]
    comp_z = None
    if len(comp_series) >= BASELINE_MIN + 1:
        prior = comp_series[:-1][-BASELINE_WINDOW:]
        mean, sd = _mean_std(prior)
        if mean is not None and sd:
            comp_z = round((comp_series[-1] - mean) / sd, 2)

    direction = "none"
    breadth_ok_down = last["ratio_down"] >= stress_ratio and last["n_down"] > last["n_up"]
    breadth_ok_up = last["ratio_up"] >= stress_ratio and last["n_up"] > last["n_down"]
    # 幅度門檻：有 z 就要求 ≤-1σ / ≥+1σ；資料不足以算 z 時退回「不判定」而非硬猜。
    if breadth_ok_down and comp_z is not None and comp_z <= -1.0:
        direction = "down"
    elif breadth_ok_up and comp_z is not None and comp_z >= 1.0:
        direction = "up"

    bench = (benchmark_by_date or {}).get(last["date"]) if benchmark_by_date else None
    attribution, bench_capw, bench_source = "unknown", None, None
    if bench:
        bench_capw = bench.get("capw")
        bench_source = bench.get("source")
        if bench_capw is not None and direction != "none":
            same_way = (bench_capw < 0) if direction == "down" else (bench_capw > 0)
            # 基準同向且幅度不算輕微 → 視為大盤層級現象
            attribution = "market_wide" if (same_way and abs(bench_capw) >= 0.5) else "sector_specific"

    return {
        "ready": last["n_sectors"] > 0,
        "date": last["date"],
        "direction": direction,
        "n_sectors": last["n_sectors"],
        "n_down": last["n_down"], "n_up": last["n_up"],
        "ratio_down": last["ratio_down"], "ratio_up": last["ratio_up"],
        "down_sectors": last["down_sectors"], "up_sectors": last["up_sectors"],
        "attribution": attribution,
        "benchmark_capw": bench_capw,
        "benchmark_source": bench_source,
        "composite_z": comp_z,
        "stress_ratio": stress_ratio,
    }


def generate_market_conclusions(
    stress: dict,
    severity_by_group: Optional[dict[str, dict]] = None,
) -> list[str]:
    """把市場級訊號轉成中文條列建議。只在真的成立時輸出，資料不足或無訊號回空 list
    （由畫面顯示「資料收集中」），絕不生成假結論。"""
    out: list[str] = []
    if stress.get("ready") and stress.get("direction") in ("up", "down"):
        down = stress["direction"] == "down"
        emoji = "🚨" if down else "🚀"
        verb = "普遍性賣壓（共同賣出）" if down else "普遍性買盤（共同買進）"
        n = stress["n_down"] if down else stress["n_up"]
        sectors = stress["down_sectors"] if down else stress["up_sectors"]
        line = (
            f"{emoji} 【市場廣度】{stress['date']}：{n}/{stress['n_sectors']} 個板塊"
            f"同時{verb} —— {'、'.join(sectors)}"
        )
        attr = stress.get("attribution")
        bc = stress.get("benchmark_capw")
        src = "大盤基準" if stress.get("benchmark_source") == "benchmark" else "全體成分股複合指數"
        if attr == "market_wide":
            line += f"；{src} {bc:+.2f}%，屬大盤層級普跌，非單一類股現象" if bc is not None else ""
        elif attr == "sector_specific":
            line += f"；{src} {bc:+.2f}%，大盤未同步 → 資金針對這些類股族群操作" if bc is not None else ""
        else:
            line += "；無可用基準，無法判定是否為大盤層級（資料收集中）"
        out.append(line)

    for name, ev in (severity_by_group or {}).items():
        if ev.get("direction") in ("up", "down") and ev.get("ready"):
            down = ev["direction"] == "down"
            emoji = "⚠️" if down else "✨"
            verb = "單日極端普遍下跌" if down else "單日極端普遍上漲"
            out.append(
                f"{emoji} 【單日嚴重度】{name}：{ev['date']} {verb} —— "
                f"{ev['n_rated']} 檔中廣度 {ev['breadth']:+.2f}（隨機機率 {ev['p']*100:.1f}%）、"
                f"市值加權 {ev['capw']:+.2f}%（{ev['z']:+.1f}σ）"
            )
    return out


def summarize_consensus(
    flows: dict[str, dict],
    stress: Optional[dict] = None,
    backtest: Optional[dict] = None,
) -> Optional[dict]:
    """把「各板塊各自的共識」綜合成一句**目前狀態的初步結論**（bug#00087）。

    先前系統只逐板塊條列（「CPU 普遍下跌」「光通訊 普遍下跌」…），從未把它們合起來
    說出「目前類股層面整體是什麼狀態」，使用者因此看不到「持續下跌」這個結論。

    判定純屬**描述性**——陳述「現在有幾個板塊已達共識」這個事實，不外推未來走勢。
    是否具備預測力另由回測校準狀態標示（目前多為「與基準無顯著差異」，會一併附上，
    避免把描述誤讀為預測）。

    regime：
      broad_decline  下跌共識板塊 ≥ 半數，且明顯多於上漲 → 類股層面持續性賣壓
      broad_advance  鏡像
      mixed          兩方向都有、未過半 → 分歧輪動
      quiet          無任何板塊達共識

    回傳 {regime, headline, detail, n_down, n_up, n_none, total,
          down_sectors, up_sectors, calibration}，無 ready 板塊時回 None。"""
    ready = {g: f for g, f in flows.items() if f.get("ready")}
    if not ready:
        return None

    down = [g for g, f in ready.items() if f.get("direction") == "down"]
    up = [g for g, f in ready.items() if f.get("direction") == "up"]
    total = len(ready)
    n_d, n_u = len(down), len(up)

    if n_d and n_d >= total / 2 and n_d > n_u:
        regime = "broad_decline"
    elif n_u and n_u >= total / 2 and n_u > n_d:
        regime = "broad_advance"
    elif n_d or n_u:
        regime = "mixed"
    else:
        regime = "quiet"

    verb = {
        "broad_decline": "類股層面呈現持續性賣壓（共同賣出）",
        "broad_advance": "類股層面呈現持續性買盤（共同買進）",
        "mixed": "類股分歧、資金輪動中",
        "quiet": "各板塊均未形成普遍性共識",
    }[regime]
    emoji = {"broad_decline": "📉", "broad_advance": "📈",
             "mixed": "🔀", "quiet": "😐"}[regime]

    headline = f"{emoji} 【初步結論】{verb} —— {total} 個板塊中 {n_d} 個普遍下跌、{n_u} 個普遍上漲"

    parts = []
    if down:
        parts.append(f"下跌共識：{'、'.join(down)}")
    if up:
        parts.append(f"上漲共識：{'、'.join(up)}")
    if stress and stress.get("direction") in ("up", "down"):
        attr = {"market_wide": "屬大盤層級", "sector_specific": "為類股獨有",
                "unknown": "基準不明"}.get(stress.get("attribution"), "")
        parts.append(
            f"最新交易日（{stress.get('date')}）跨板塊廣度 "
            f"{stress.get('n_down')}/{stress.get('n_sectors')} 同向{attr}"
        )
    detail = "；".join(parts) if parts else "尚無板塊達持續性共識門檻"

    calib = None
    if backtest:
        try:
            from .calibration import calibration_status_label
            calib = calibration_status_label(backtest)
        except Exception:
            calib = None

    return {
        "regime": regime,
        "headline": headline,
        "detail": detail,
        "n_down": n_d, "n_up": n_u, "n_none": total - n_d - n_u, "total": total,
        "down_sectors": down, "up_sectors": up,
        "calibration": calib,
    }


def consensus_lines(
    flows: dict[str, dict],
    stress: Optional[dict] = None,
    backtest: Optional[dict] = None,
) -> list[str]:
    """把 summarize_consensus() 轉成可直接顯示的文字行（含誠實的校準註記）。"""
    s = summarize_consensus(flows, stress, backtest)
    if not s or s["regime"] == "quiet":
        return []
    out = [s["headline"], f"   [dim]{s['detail']}[/dim]"]
    if s.get("calibration"):
        out.append(
            f"   [dim]※ 上述為「目前已發生」的描述性狀態；此訊號的前瞻預測力回測："
            f"{s['calibration']}[/dim]"
        )
    return out
