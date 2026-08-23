"""
assettrack/backtest_stats.py — 回測結果的統計顯著性驗證（純離線、零相依）

bug#00094: 回測本身也有準確性問題——一個命中率是「真本事」還是「運氣」？三套
walk-forward 回測（期權/ETF/類股）皆回傳同一種 `by_horizon` 結構，本模組把它們的
原始命中數轉成可判讀的統計：

  1. Wilson score 信賴區間——小樣本下比常態近似更穩健的命中率區間。
  2. 對基準的單尾二項檢定——H0：命中率 = 基準上漲率（無技能）；H1：命中率 > 基準。
     並對「多個前瞻期 × 多/空」的多次檢定做 Bonferroni 調整，避免因為挑最好看的一組
     而高估顯著性。
  3. 前後子區間穩定性——把訊號依日期切前後兩半，看命中是否兩段都成立；只在一段有效
     的訊號標為不穩定（近似樣本外檢查，防非平穩/單一盤勢過擬合）。

已知限制（誠實揭露，不假裝精確）：連續日對同一標的的訊號高度自相關、長前瞻期相鄰
訊號報酬重疊，故「有效獨立樣本」小於原始 n，二項檢定會略微高估顯著性；多重比較調整
與子區間穩定性是對此的部分防禦，無法完全消除。判定「顯著」時一律偏保守。

這裡只做純統計，不打網路、不讀檔；輸入是回測 report 的既有 by_horizon 聚合值
（up_n / up_hit_rate / down_n / down_hit_rate / baseline_up_rate）加上可選的逐訊號
records（{date, h, dir, hit}），所以三套回測共用同一份驗證邏輯，無兩套標準。
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Optional

TREND_CONFIDENCE_THRESHOLD = 60.0


def wilson_interval(hits: int, n: int, z: float = 1.96) -> tuple:
    """Wilson score 信賴區間（預設 95%）。n=0 時回傳 (0,1) 表示完全未知。"""
    if n <= 0:
        return (0.0, 1.0)
    phat = hits / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (phat + z2 / (2 * n)) / denom
    half = (z * math.sqrt(phat * (1 - phat) / n + z2 / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def binom_sf(k: int, n: int, p: float) -> float:
    """單尾上尾機率 P(X >= k)，X ~ Binomial(n, p)。n<=2000 用 log-space 精確計算，
    更大時退回帶連續性校正的常態近似（避免超長迴圈）。"""
    if n <= 0 or k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p <= 0.0:
        return 0.0  # 需要 >=1 次命中但 p=0 → 不可能
    if p >= 1.0:
        return 1.0
    if n > 2000:
        mu = n * p
        sd = math.sqrt(n * p * (1 - p))
        if sd == 0:
            return 1.0 if k <= mu else 0.0
        zc = (k - 0.5 - mu) / sd
        return 0.5 * math.erfc(zc / math.sqrt(2))
    logp = math.log(p)
    log1mp = math.log1p(-p)
    terms = []
    for i in range(k, n + 1):
        logc = math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1)
        terms.append(logc + i * logp + (n - i) * log1mp)
    m = max(terms)
    return min(1.0, math.exp(m) * sum(math.exp(t - m) for t in terms))


def direction_significance(
    n: int,
    hit_rate: Optional[float],
    baseline_rate: Optional[float],
    horizon: int = 1,
    num_tests: int = 1,
    z: float = 1.96,
    distinct_dates: Optional[int] = None,
    overlap_purged: bool = False,
) -> Optional[dict]:
    """單一方向（多/空）在單一前瞻期的顯著性摘要。無樣本/缺基準時回傳 None。

    一般回測以 Effective Sample Size (ESS = floor(n / horizon)) 保守處理重疊視窗。
    若呼叫端已在樣本建構時 purge 重疊 label interval，必須傳 ``overlap_purged=True``，
    此時不再除以 horizon 第二次，只保留跨標的同日聚類上限。

    bug#00125（跨截面修正）：舊 ESS 只對「時間」去相關（÷horizon），沒有對「跨標的」去
    相關。彙總回測把多檔標的池在一起時，同一天的 6 檔半導體其實是**同一個市場日**的一次
    觀測，卻被算成 6 筆獨立樣本。實測本機資料：h=1 的池化結果 n=24、ESS=24、命中率 96%、
    p=0.033「顯著」，但那 24 筆只落在 5 個不同日期、且 6 檔標的高度同向——真正的獨立觀
    測約 5 次，不是 24 次。故 ESS 另以「不同訊號日期數 ÷ horizon」設上限：

        ESS = max(1, min(floor(n / horizon), floor(distinct_dates / horizon)))

    `distinct_dates=None`（呼叫端沒提供日期）時退回舊行為，只做時間去相關。此修正只會
    **收緊**顯著性，不會放寬。
    """
    if not n or hit_rate is None or baseline_rate is None:
        return None
    h = max(1, horizon)
    # A caller that already removed overlapping label intervals must not be
    # penalised a second time by dividing n by the horizon again.
    ess = n if overlap_purged else math.floor(n / h)
    if distinct_dates is not None:
        date_ess = distinct_dates if overlap_purged else math.floor(distinct_dates / h)
        ess = min(ess, date_ess)
    ess = max(1, ess)
    hits_ess = int(round(hit_rate * ess))
    raw_hits = int(round(hit_rate * n))
    lo, hi = wilson_interval(hits_ess, ess, z=z)
    p = binom_sf(hits_ess, ess, baseline_rate)
    alpha_adj = 0.05 / max(1, num_tests)
    return {
        "n": n,
        "ess": ess,
        "distinct_dates": distinct_dates,
        "hits": raw_hits,
        "hits_ess": hits_ess,
        "hit_rate": hit_rate,
        "baseline_rate": baseline_rate,
        "ci_lo": lo,
        "ci_hi": hi,
        "p_value": p,
        "significant_95": p < 0.05,          # 單一檢定顯著
        "significant_adj": p < alpha_adj,     # 過多重比較調整後仍顯著（保守）
        "alpha_adj": alpha_adj,
        "num_tests": num_tests,
    }


def _stability(records: list, by_h: Optional[dict] = None, min_half: int = 8) -> Optional[dict]:
    """把逐訊號 records 依日期切前後兩半，用樣本最多的前瞻期評估命中是否兩段都成立。
    records: [{date, h, dir, hit(bool)}, ...]（只含有方向的訊號）。

    bug#00112（使用者審查 #7）：「一致」判準由固定 0.5 改為「兩段都贏無技能基準」。
    舊版以 hit>0.5 判定，會把基準本就 60%、命中僅 52/53% 的劣訊號標為「前後一致」而
    誤導。改以各半段依方向組成加權的無技能期望命中率為門檻（up→baseline_up_rate、
    down→1−baseline_up_rate）；缺基準時退回 0.5（與舊版相容）。"""
    if not records:
        return None
    counts = Counter(r["h"] for r in records)
    if not counts:
        return None
    h = counts.most_common(1)[0][0]
    recs = sorted((r for r in records if r["h"] == h), key=lambda r: r["date"])
    if len(recs) < 2 * min_half:
        return {"horizon": h, "consistent": None,
                "reason": "樣本不足以評估前後子區間穩定性", "n": len(recs)}
    base = None
    if by_h is not None:
        st = by_h.get(h) if h in by_h else by_h.get(str(h), {})
        base = (st or {}).get("baseline_up_rate")

    def _expected(rs: list) -> float:
        if base is None:
            return 0.5
        vals = [base if r["dir"] == "up" else (1.0 - base) for r in rs]
        return (sum(vals) / len(vals)) if vals else 0.5

    mid = len(recs) // 2
    early, late = recs[:mid], recs[mid:]
    er = sum(1 for r in early if r["hit"]) / len(early)
    lr = sum(1 for r in late if r["hit"]) / len(late)
    early_exp, late_exp = _expected(early), _expected(late)
    return {
        "horizon": h,
        "consistent": bool(er > early_exp and lr > late_exp),
        "early_rate": er,
        "late_rate": lr,
        "early_expected": round(early_exp, 3),
        "late_expected": round(late_exp, 3),
        "early_n": len(early),
        "late_n": len(late),
        "early_span": (early[0]["date"], early[-1]["date"]),
        "late_span": (late[0]["date"], late[-1]["date"]),
    }


def _hz_dir_stats(st: dict, direction: str) -> tuple:
    """讀單一 by_horizon 條目的 (n, hit_rate)，同時支援 ETF/類股的 up_/down_ 命名與
    期權（calibration）的 bullish_/bearish_ 命名，讓一份驗證層通吃三套回測。"""
    if direction == "up":
        return st.get("up_n", st.get("bullish_n", 0)), \
            st.get("up_hit_rate", st.get("bullish_hit_rate"))
    return st.get("down_n", st.get("bearish_n", 0)), \
        st.get("down_hit_rate", st.get("bearish_hit_rate"))


def attach_significance(report: dict, records: Optional[list] = None) -> dict:
    """把 Wilson CI + 對基準的二項檢定（含多重比較調整）寫進 report 每個 by_horizon，
    並用 records 算前後子區間穩定性寫進 report['stability']。就地修改並回傳 report。"""
    by_h = report.get("by_horizon", {})

    num_tests = 0
    for st in by_h.values():
        if _hz_dir_stats(st, "up")[0]:
            num_tests += 1
        if _hz_dir_stats(st, "down")[0]:
            num_tests += 1
    num_tests = max(1, num_tests)

    # bug#00125: 由 records 統計每個 (horizon, direction) 的**不同訊號日期數**，供
    # direction_significance 做跨截面去相關（同一天多檔標的 ≠ 多次獨立觀測）。
    dates_by: dict = {}
    for r in (records or []):
        if r.get("date") is None:
            continue
        dates_by.setdefault((r.get("h"), r.get("dir")), set()).add(r["date"])

    def _dd(h, direction):
        s = dates_by.get((h, direction))
        return len(s) if s else None

    for h, st in by_h.items():
        base = st.get("baseline_up_rate")
        up_n, up_hr = _hz_dir_stats(st, "up")
        down_n, down_hr = _hz_dir_stats(st, "down")
        overlap_purged = bool(report.get("overlap_purged"))
        up_sig = direction_significance(
            up_n, up_hr, base, horizon=int(h), num_tests=num_tests,
            distinct_dates=_dd(h, "up"), overlap_purged=overlap_purged,
        )
        down_base = (1.0 - base) if base is not None else None
        down_sig = direction_significance(
            down_n, down_hr, down_base, horizon=int(h), num_tests=num_tests,
            distinct_dates=_dd(h, "down"), overlap_purged=overlap_purged,
        )
        st["significance"] = {"up": up_sig, "down": down_sig, "num_tests": num_tests}

    report["stability"] = _stability(records, by_h) if records else None
    return report


def _verdict_word(sig: dict) -> str:
    if sig["significant_adj"]:
        return "顯著優於基準"
    if sig["significant_95"]:
        return "優於基準(未過多重檢定，偏參考)"
    return "與基準無顯著差異"


def significance_phrase(report: dict, horizon: int, direction: str) -> str:
    """給結論卡就地顯示的一句統計後綴（接在命中率之後）。無資料回空字串。"""
    st = report.get("by_horizon", {}).get(horizon, {})
    sig = (st.get("significance") or {}).get(direction)
    if not sig:
        return ""
    lo, hi = sig["ci_lo"] * 100, sig["ci_hi"] * 100
    base = sig["baseline_rate"] * 100
    return (f"，95%CI {lo:.0f}–{hi:.0f}%，基準 {base:.0f}%，"
            f"{_verdict_word(sig)}(p={sig['p_value']:.3f})")


def confidence_percentage_info(report: dict, horizon: int, direction: str) -> dict:
    """導出回測 edge 的證據分數與 95% 信賴區間。

    `1-p` 只能描述「反對無技能基準的證據」，不是下一次漲跌的預測機率。為了相容既有
    顯示仍保留 `confidence_pct` 欄位，也回傳語意較精確的 `evidence_pct`。Dashboard
    依產品規格以嚴格 `>60%` 作趨勢門檻；樣本量、ESS、調整後顯著性與穩定性另以
    `validation_passed / validation_reason` 保留供診斷與校準。
    回傳 {"confidence_pct": float|None, "confidence_str": str, "ci_str": str, "p_value": float|None}
    """
    st = report.get("by_horizon", {}).get(horizon, {})
    sig = (st.get("significance") or {}).get(direction)
    if not sig or not sig.get("n"):
        return {"confidence_pct": None, "confidence_str": "樣本累積中", "ci_str": "", "p_value": None, "n": 0}

    n = sig["n"]
    p_val = sig["p_value"]
    hit_rate = sig["hit_rate"]
    lo, hi = sig["ci_lo"] * 100, sig["ci_hi"] * 100

    # bug#00125: 舊版在 p_value 缺漏時退回寫死的 50.0。該分支目前不可達
    # （direction_significance 一定會填 p_value），但這正是本次修掉的那種「算不出來就
    # 捏一個看起來合理的數字」模式，一次重構就會復活。改為明確回報「無法計算」。
    if p_val is None and hit_rate is None:
        return {"confidence_pct": None, "confidence_str": "無法計算信心水準",
                "ci_str": f"95%CI {lo:.0f}–{hi:.0f}%", "p_value": None,
                "n": n, "hit_rate": None, "below_baseline": None}

    raw_conf = (1.0 - p_val) * 100.0 if p_val is not None else hit_rate * 100.0
    if n < 5:
        conf_pct = min(raw_conf, 60.0)
    elif n < 20:
        conf_pct = min(raw_conf, 85.0)
    else:
        conf_pct = raw_conf

    # bug#00125: 舊版下限鎖死 50.0。命中率**低於**基準時 p→1、raw_conf→0，會被改寫成
    # 「信心水準 50%」，讀起來像「擲硬幣」，但回測其實是說這個訊號比什麼都不做更差。
    # 方向判定不受影響（門檻仍擋掉），但顯示的「程度」會誤導，故改為據實顯示並附旗標。
    below = (hit_rate is not None and sig.get("baseline_rate") is not None
             and hit_rate < sig["baseline_rate"])
    conf_pct = round(max(0.0, min(99.0, conf_pct)), 0)

    return {
        "confidence_pct": conf_pct,
        "evidence_pct": conf_pct,
        "confidence_str": (f"{conf_pct:.0f}%（低於無技能基準）" if below else f"{conf_pct:.0f}%"),
        "ci_str": f"95%CI {lo:.0f}–{hi:.0f}%",
        "p_value": p_val,
        "n": n,
        "hit_rate": hit_rate,
        "below_baseline": below,
    }


def evaluable_horizons(report: dict, direction: str, horizons: tuple) -> tuple:
    """bug#00125: 只回傳「該方向真的有前瞻樣本」的前瞻期。

    背景：`calibration.backtest_verdicts` 只有在 T 之後存在 ≥ T+h 的真實快照時才會累積
    一筆前瞻樣本。快照累積天數不足時（例如剛上線只有 6 天、跨 5 日曆天），所有 h≥7 的
    樣本數都是 0，整組候選都算不出信心水準。此函式讓呼叫端能誠實區分「算過但沒 edge」
    與「根本還沒有樣本可算」，而不是靜默退回一個寫死的天數。
    """
    out = []
    for h in horizons:
        by_h = report.get("by_horizon", {}) or {}
        st = by_h.get(h)
        if st is None:
            st = by_h.get(str(h), {})
        n, _ = _hz_dir_stats(st or {}, direction)
        if n:
            out.append(h)
    return tuple(out)


def max_evaluable_horizon(report: dict, horizons: Optional[tuple] = None) -> Optional[int]:
    """整份 report（不分方向、含 baseline）中最大「有前瞻樣本」的 h，供畫面誠實揭露
    「目前資料只夠評估到 +N 天」。完全沒有任何前瞻樣本時回 None。"""
    by_h = report.get("by_horizon", {}) or {}
    cands = []
    for h, st in by_h.items():
        try:
            h_int = int(h)
        except (TypeError, ValueError):
            continue
        if horizons is not None and h_int not in horizons:
            continue
        if (st or {}).get("baseline_n") or _hz_dir_stats(st or {}, "up")[0] \
                or _hz_dir_stats(st or {}, "down")[0]:
            cands.append(h_int)
    return max(cands) if cands else None


def find_best_horizon_confidence(
    report: dict,
    direction: str,
    horizons: tuple = (1, 5, 7, 10, 14, 21, 30, 35),
    fallback_horizon: Optional[int] = None,
    preferred_horizon: int = 5,
) -> dict:
    """Resolve one pre-declared forecast horizon and validate its backtest edge.

    Horizon selection uses availability and distance to the pre-declared 5-session
    target, never the largest `(1-p)` on the same data. This removes horizon
    shopping. The legacy function/field names remain for API compatibility.

    Dashboard trend activation follows the product rule `confidence_pct > 60%`
    (strictly greater, not equal). The fuller n／ESS／edge／Bonferroni／stability
    result is retained separately as `validation_passed` for diagnostics and
    calibration, while model-health `degraded` remains a downstream limitation
    disclosure and proposal trigger under governance decision D-02.
    """
    usable = evaluable_horizons(report, direction, horizons)
    if not usable:
        return {
            "best_horizon": fallback_horizon,
            "confidence_pct": None,
            "evidence_pct": None,
            "confidence_str": "樣本累積中",
            "ci_str": "",
            "p_value": None,
            "n": 0,
            "hit_rate": None,
            "meets_threshold": False,
            "gate_reason": "尚無前瞻樣本",
            "evaluable_horizons": usable,
        }

    h_best = min(usable, key=lambda h: (abs(h - preferred_horizon), -h))
    info = confidence_percentage_info(report, h_best, direction)
    c_pct = info.get("confidence_pct")
    if c_pct is None:
        info.update({
            "best_horizon": h_best,
            "meets_threshold": False,
            "gate_reason": "回測證據無法計算",
            "evaluable_horizons": usable,
        })
        return info

    info["best_horizon"] = h_best
    by_h = report.get("by_horizon", {}) or {}
    st = by_h.get(h_best) if h_best in by_h else by_h.get(str(h_best), {})
    sig = ((st or {}).get("significance") or {}).get(direction) or {}
    need_n = int(report.get("min_signals", 20))
    reasons = []
    if info.get("n", 0) < need_n:
        reasons.append(f"n={info.get('n', 0)}<{need_n}")
    if sig.get("ess", 0) < 3:
        reasons.append(f"ESS={sig.get('ess', 0)}<3")
    if info.get("hit_rate") is None or sig.get("baseline_rate") is None \
            or info["hit_rate"] <= sig["baseline_rate"]:
        reasons.append("未優於無技能基準")
    if not sig.get("significant_adj"):
        reasons.append("未通過多重檢定")
    stability = report.get("stability") or {}
    if stability.get("consistent") is False:
        reasons.append("前後區間不穩定")
    info["validation_passed"] = not reasons
    info["validation_reason"] = "；".join(reasons) if reasons else "通過完整統計驗證"
    info["meets_threshold"] = c_pct > TREND_CONFIDENCE_THRESHOLD
    info["gate_reason"] = (
        f"信心水準 {c_pct:.0f}% 已超過 {TREND_CONFIDENCE_THRESHOLD:.0f}%"
        if info["meets_threshold"]
        else f"信心水準 {c_pct:.0f}% 未超過 {TREND_CONFIDENCE_THRESHOLD:.0f}%"
    )
    info["evaluable_horizons"] = usable
    return info


def has_backtest_evidence(conf_best: dict) -> bool:
    """bug#00125: 是否真有可用的回測統計證據（有信心水準且有前瞻期）。
    結論卡的 60% 門檻守門一律經由此函式，避免「無證據」被當成「通過」。"""
    return conf_best.get("confidence_pct") is not None and conf_best.get("best_horizon") is not None


def horizon_coverage_note(report: dict, horizons: tuple = (1, 5, 7, 10, 14, 21, 30, 35)) -> str:
    """bug#00125: 一句誠實揭露「資料涵蓋到哪」，接在結論標頭後面，讓使用者一眼知道
    看不到長前瞻期結論是因為快照還不夠久，而不是系統只會算 14 天。"""
    days = report.get("total_snapshot_days") or 0
    first, last = report.get("first_date"), report.get("last_date")
    span = ""
    if first and last:
        try:
            from datetime import datetime as _dt
            d = (_dt.strptime(last, "%Y-%m-%d") - _dt.strptime(first, "%Y-%m-%d")).days
            span = f"／跨 {d} 日曆天"
        except (TypeError, ValueError):
            span = ""
    h_max = max_evaluable_horizon(report, horizons)
    limit = f"目前僅能評估至 +{h_max} 天" if h_max else "尚無任何前瞻樣本"
    return f"快照 {days} 天{span}，{limit}"



def validation_label(report: dict) -> str:
    """一句「回測可信度」總結，供校準狀態列顯示：綜合顯著性與前後穩定性。
    取樣本最多的前瞻期/方向做代表。"""
    by_h = report.get("by_horizon", {})
    best = None  # (n, horizon, direction, sig)
    for h, st in by_h.items():
        for d in ("up", "down"):
            sig = (st.get("significance") or {}).get(d)
            if sig and (best is None or sig["n"] > best[0]):
                best = (sig["n"], h, d, sig)
    if best is None:
        return "尚無可評估訊號（資料累積中）"

    _, h, d, sig = best
    parts = [f"前瞻{h}日 n={sig['n']}", f"95%CI {sig['ci_lo']*100:.0f}–{sig['ci_hi']*100:.0f}%",
             _verdict_word(sig)]

    stab = report.get("stability")
    if stab is not None:
        if stab.get("consistent") is True:
            parts.append("前後子區間一致")
        elif stab.get("consistent") is False:
            parts.append("⚠️前後子區間不一致(慎用)")
        # consistent is None → 樣本不足，略過不誤導
    return "；".join(parts)
