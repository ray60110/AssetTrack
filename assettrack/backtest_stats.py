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
) -> Optional[dict]:
    """單一方向（多/空）在單一前瞻期的顯著性摘要。無樣本/缺基準時回傳 None。
    bug#00106: 導入 Effective Sample Size (ESS = floor(n / horizon)) 計算顯著性 p 值與 Wilson CI，
    消除連續每日重複訊號在長前瞻期（如 14/30/60 天）下重疊視窗報酬自相關造成的 p 值過度膨脹與顯著性高估。
    """
    if not n or hit_rate is None or baseline_rate is None:
        return None
    ess = max(1, math.floor(n / max(1, horizon)))
    hits_ess = int(round(hit_rate * ess))
    raw_hits = int(round(hit_rate * n))
    lo, hi = wilson_interval(hits_ess, ess, z=z)
    p = binom_sf(hits_ess, ess, baseline_rate)
    alpha_adj = 0.05 / max(1, num_tests)
    return {
        "n": n,
        "ess": ess,
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

    for h, st in by_h.items():
        base = st.get("baseline_up_rate")
        up_n, up_hr = _hz_dir_stats(st, "up")
        down_n, down_hr = _hz_dir_stats(st, "down")
        up_sig = direction_significance(up_n, up_hr, base, horizon=int(h), num_tests=num_tests)
        down_base = (1.0 - base) if base is not None else None
        down_sig = direction_significance(down_n, down_hr, down_base, horizon=int(h), num_tests=num_tests)
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
    """bug#00110: 計算並導出 % 格式的信心水準 (Confidence Level %) 及 95% 信賴區間。
    信心水準 % 定義為：基於統計二項檢定 p 值 (1 - p_value)，並結合樣本數門檻調整。
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

    raw_conf = (1.0 - p_val) * 100.0 if p_val is not None else (hit_rate * 100.0 if hit_rate is not None else 50.0)
    if n < 5:
        conf_pct = min(raw_conf, 60.0)
    elif n < 20:
        conf_pct = min(raw_conf, 85.0)
    else:
        conf_pct = raw_conf

    conf_pct = round(max(50.0, min(99.0, conf_pct)), 0)

    return {
        "confidence_pct": conf_pct,
        "confidence_str": f"{conf_pct:.0f}%",
        "ci_str": f"95%CI {lo:.0f}–{hi:.0f}%",
        "p_value": p_val,
        "n": n,
        "hit_rate": hit_rate,
    }


def find_best_horizon_confidence(report: dict, direction: str, horizons: tuple = (7, 10, 14, 21, 30, 35)) -> dict:
    """bug#00110: 跨多個前瞻期 (+7~+35天波段區間) 動態搜尋信心水準 (%) 最高的前瞻期 h_best。
    回傳 {best_horizon, confidence_pct, confidence_str, ci_str, p_value, n, hit_rate, meets_threshold(>=60%)}
    """
    best = None  # (conf_pct, h, conf_info)
    for h in horizons:
        info = confidence_percentage_info(report, h, direction)
        c_pct = info.get("confidence_pct")
        if c_pct is not None:
            if best is None or c_pct > best[0]:
                best = (c_pct, h, info)

    if best is None:
        return {
            "best_horizon": 14,
            "confidence_pct": None,
            "confidence_str": "樣本累積中",
            "ci_str": "",
            "p_value": None,
            "n": 0,
            "hit_rate": None,
            "meets_threshold": False,
        }

    c_pct, h_best, info = best
    info["best_horizon"] = h_best
    info["meets_threshold"] = c_pct >= 60.0
    return info



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

