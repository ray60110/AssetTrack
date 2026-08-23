"""
assettrack/options_analysis.py — 期權觀察清單：建倉與價格波動偵測（離線運算）

bug#00061: 根據使用者的部位標的建立期權觀察清單，每日追蹤是否出現大量買權/賣權
建倉，或期權價格大幅漲跌，生成結論告知使用者。

跟 analysis.py（主動式ETF趨勢）同一套原則：100% 離線、零網路請求，只讀取
storage.py 已經在背景刷新時逐日真實累積下來的 options_cache/history/*.jsonl
快照（storage.load_options_daily_snapshots）。**沒有真實快照就沒有結論** ——
不會、也不能對缺資料的天數做任何估計或回填。

視窗刻意比 ETF 趨勢（60天）短很多，預設只有 14 天：追蹤範圍限定在
28-60 天到期、價平 ±15% 履約價的合約（quotes.fetch_options_snapshot），
這些合約本身會隨時間自然「滾出」追蹤範圍（例如今天 45 天到期的合約，30 天後
只剩 15 天到期，已經不在 28-60 天視窗內，不會再被抓取）。視窗拉太長，同一張
合約多半早就到期或滾出範圍，早期快照與最新快照根本比不起來；14 天大致能保留
足夠的合約重疊率，讓「同一張合約」的未平倉量/價格比較有意義。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from statistics import median
from typing import Optional

from .storage import taiwan_now

# bug#00125: 方向結論的候選前瞻期。與 calibration.DEFAULT_HORIZONS 對齊（含 1／5），
# 舊版寫成 (7,10,14,21,30,35) 而把最早會累積出真實樣本的短前瞻期排除在外，反而讓
# find_best_horizon_confidence 一路退回寫死的 14 天。注意：這是**前瞻期**，與
# OPTIONS_FLOW_WINDOW_DAYS（回看**觀察視窗** 14 天）是不同概念，不可互為預設值。
VERDICT_HORIZONS = (1, 5, 7, 10, 14, 21, 30, 35)


def _em_dte(exp_move: Optional[dict]) -> int:
    """bug#00125: Expected Move 實際使用的 DTE。

    `compute_expected_move(target_dte=30)` 只是「挑最接近 30 天的到期日」，實際 dte 可能是
    7 天或 60 天，而 σ 是用**那個實際 dte** 以 √(dte/365) 縮放的。舊版標題卻寫死「未來 30
    天 ±1σ」，於是只有 7DTE 鏈的標的會把一個 7 天的波動帶宣稱成 30 天（約窄一倍）。
    """
    return int((exp_move or {}).get("dte") or 0)


def _em_formula(exp_move: Optional[dict]) -> str:
    """bug#00125: σ 的實際計算方式。無 ATM IV 時 `compute_expected_move` 會退回
    `straddle × 0.85` 的跨式近似並設 low_confidence，舊版卻一律標示 BS IV 公式，
    等於把一個沒算過 IV 的數字掛上 IV 公式的名義。"""
    if (exp_move or {}).get("atm_iv"):
        return "±1σ = Spot × ATM_IV × √(DTE/365)"
    return "±1σ ≈ ATM 跨式權利金 × 0.85（無可用 ATM IV，退回跨式近似，非 BS 公式）"


def _em_iv_note(exp_move: Optional[dict]) -> str:
    """Expected Move 的 IV／可信度註記；低可信度必須顯示，不得靜默。"""
    em = exp_move or {}
    bits = []
    if em.get("atm_iv"):
        bits.append(f"ATM IV {em['atm_iv'] * 100:.0f}%")
    else:
        bits.append("無 ATM IV，跨式近似")
    if em.get("low_confidence"):
        bits.append("⚠️ 報價品質低")
    return f"（{'，'.join(bits)}）"


def _filter_window(snapshots: list[dict], cutoff_date: str) -> list[dict]:
    return [s for s in snapshots if s.get("date", "") >= cutoff_date]


def _no_live_data(c: dict) -> bool:
    """bug#00080: 該合約列是否為「無有效市場資料」——未平倉量與雙邊報價全為 0/None。
    yfinance 在新交易日 OI 尚未結算時，常整列回傳 OI=0、bid=0、ask=0（成交時間仍停在
    前一交易日）。這種列不是真的「持倉歸零」，拿它比對會產生 -100% 假減倉（或隔日
    0→N 的假建倉），因此比對前直接跳過。舊格式快照無 bid/ask（皆 None），此時退化為
    「OI 為 0/None 即視為無資料」。"""
    return not c.get("openInterest") and not c.get("bid") and not c.get("ask")


def compute_options_flow(
    snapshots_by_underlying: dict[str, list[dict]],
    window_days: int = 14,
    oi_buildup_min_contracts: int = 500,
    oi_buildup_min_pct: float = 50.0,
    price_swing_min_pct: float = 20.0,
    price_swing_min_abs: float = 0.15,
    as_of: Optional[str] = None,
) -> dict:
    """Compute real day-over-day options-flow signals from accumulated snapshots.

    snapshots_by_underlying: {underlying: [{"date": "YYYY-MM-DD",
        "spot_price": float|None, "contracts": [{"contractSymbol": str,
        "type": "call"/"put", "strike": float, "expiry": "YYYY-MM-DD",
        "lastPrice": float|None, "volume": float|None,
        "openInterest": float|None, "impliedVolatility": float|None}, ...]}, ...]}
        (as returned by storage.load_options_daily_snapshots)

    For each underlying with >= 2 real snapshots in the trailing `window_days`,
    matches contracts between the earliest and latest snapshot **by
    contractSymbol** (strike/expiry/type are baked into it, so this is an exact
    match — no guessing). Only contracts present in both snapshots are compared;
    contracts that expired, rolled out of the 28-60 DTE band, or are newly
    in-band are simply not compared (real absence, not treated as zero-change).

    Per matched contract:
      - "buildup"     if |OI delta| >= oi_buildup_min_contracts (raised from 200 —
                         200 contracts of OI drift over 14 days is common noise
                         for liquid underlyings), OR (earliest OI > 0 and
                         |OI delta %| >= oi_buildup_min_pct)
      - "price_swing" if earliest price > 0 and |price delta %| >= price_swing_min_pct
                         AND |price delta $| >= price_swing_min_abs (added dollar
                         floor — a cheap contract moving $0.10→$0.12 is a "20%"
                         swing with no real significance)
    (a contract can be flagged for both at once)

    Returns a dict with `coverage` (per-underlying data readiness, mirrors
    analysis.py's etf_coverage), `events` (every flagged contract with its real
    deltas), and `underlying_skew` (per-underlying aggregate call vs put OI
    buildup, for a bullish/bearish leaning signal).
    """
    as_of_date = as_of or taiwan_now().strftime("%Y-%m-%d")
    cutoff_date = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=window_days)).strftime("%Y-%m-%d")

    coverage: dict[str, dict] = {}
    events: list[dict] = []
    underlying_skew: dict[str, dict] = {}

    for underlying, raw_snaps in snapshots_by_underlying.items():
        snaps = sorted(_filter_window(raw_snaps or [], cutoff_date), key=lambda s: s.get("date", ""))
        ready = len(snaps) >= 2
        coverage[underlying] = {
            "days_in_window": len(snaps),
            "first_date": snaps[0]["date"] if snaps else None,
            "last_date": snaps[-1]["date"] if snaps else None,
            "ready": ready,
        }
        if not ready:
            continue

        earliest, latest = snaps[0], snaps[-1]
        early_by_sym = {c["contractSymbol"]: c for c in earliest.get("contracts", []) if c.get("contractSymbol")}
        late_by_sym = {c["contractSymbol"]: c for c in latest.get("contracts", []) if c.get("contractSymbol")}
        matched_symbols = set(early_by_sym) & set(late_by_sym)

        from .greeks import bs_greeks

        spot = latest.get("spot_price") or earliest.get("spot_price")
        call_oi_buildup = 0.0
        put_oi_buildup = 0.0
        call_dollar_oi_buildup = 0.0
        put_dollar_oi_buildup = 0.0

        for csym in matched_symbols:
            c0, c1 = early_by_sym[csym], late_by_sym[csym]
            if _no_live_data(c0) or _no_live_data(c1):
                continue  # bug#00080: 任一端為未結算空資料 → 不比對，避免假訊號
            oi0, oi1 = c0.get("openInterest"), c1.get("openInterest")
            p0, p0_low = _quote_mid(c0, earliest.get("date"))
            p1, p1_low = _quote_mid(c1, latest.get("date"))
            # 方向模型只接受可交易的窄價差中間價。寬價差或僅有 lastPrice 的報價仍可
            # 用於 OI 建倉統計，但不得產生權利金漲跌／重定價方向。
            if p0_low or p1_low:
                p0 = p1 = None

            oi_delta = (oi1 - oi0) if (oi0 is not None and oi1 is not None) else None
            oi_pct = (oi_delta / oi0 * 100) if (oi_delta is not None and oi0) else None
            price_delta_pct = ((p1 - p0) / p0 * 100) if (p0 and p1 is not None) else None

            is_buildup = oi_delta is not None and (
                abs(oi_delta) >= oi_buildup_min_contracts
                or (oi_pct is not None and abs(oi_pct) >= oi_buildup_min_pct)
            )
            is_swing = (
                price_delta_pct is not None
                and abs(price_delta_pct) >= price_swing_min_pct
                and p0 is not None and p1 is not None
                and abs(p1 - p0) >= price_swing_min_abs
            )

            if is_buildup and oi_delta > 0:
                opt_type = c1.get("type")
                strike = c1.get("strike")
                expiry = c1.get("expiry")
                iv = c1.get("impliedVolatility")
                dte = _dte_between(latest["date"], expiry) if expiry else None

                # Dollar Delta OI Exposure (bug#00105): OI_delta * Spot * |Delta| * 100
                g = bs_greeks(spot, strike, dte, iv, opt_type, premium=p1, r=0.04)
                delta_val = abs(g["delta"]) if (g and g.get("delta") is not None) else 0.50
                dollar_exposure = oi_delta * (spot or strike or 100.0) * delta_val * 100.0

                if opt_type == "call":
                    call_oi_buildup += oi_delta
                    call_dollar_oi_buildup += dollar_exposure
                elif opt_type == "put":
                    put_oi_buildup += oi_delta
                    put_dollar_oi_buildup += dollar_exposure

            if is_buildup or is_swing:
                events.append({
                    "underlying": underlying,
                    "contractSymbol": csym,
                    "type": c1.get("type"),
                    "strike": c1.get("strike"),
                    "expiry": c1.get("expiry"),
                    "oi_delta": oi_delta,
                    "oi_pct": round(oi_pct, 1) if oi_pct is not None else None,
                    "price_delta_pct": round(price_delta_pct, 1) if price_delta_pct is not None else None,
                    "is_buildup": is_buildup,
                    "is_swing": is_swing,
                    "first_date": earliest["date"],
                    "last_date": latest["date"],
                })

        if call_oi_buildup or put_oi_buildup or call_dollar_oi_buildup or put_dollar_oi_buildup:
            total_dollar = call_dollar_oi_buildup + put_dollar_oi_buildup
            total_contracts = call_oi_buildup + put_oi_buildup
            call_pct = (
                round(call_dollar_oi_buildup / total_dollar * 100, 1)
                if total_dollar > 0
                else (round(call_oi_buildup / total_contracts * 100, 1) if total_contracts > 0 else 0.0)
            )
            underlying_skew[underlying] = {
                "call_oi_buildup": int(call_oi_buildup),
                "put_oi_buildup": int(put_oi_buildup),
                "call_dollar_oi": round(call_dollar_oi_buildup, 2),
                "put_dollar_oi": round(put_dollar_oi_buildup, 2),
                "call_pct": call_pct,  # Dollar Delta OI Skew
            }

    events.sort(key=lambda e: abs(e["oi_delta"] or 0) + abs((e["price_delta_pct"] or 0) * 10), reverse=True)

    ready_count = sum(1 for c in coverage.values() if c["ready"])
    return {
        "window_days": window_days,
        "as_of": as_of_date,
        "coverage": coverage,
        "ready_count": ready_count,
        "total_count": len(coverage),
        "ready_pct": round(ready_count / len(coverage) * 100, 1) if coverage else 0.0,
        "events": events,
        "underlying_skew": underlying_skew,
    }


def _fmt_contract(e: dict) -> str:
    cp = "買權" if e["type"] == "call" else "賣權"
    return f"{e['underlying']} {e['expiry']}到期 ${e['strike']:g} {cp}"


def generate_options_conclusions(report: dict, top_n: int = 5) -> list[str]:
    """Turn compute_options_flow()'s output into 🎯 contract-level event bullets
    （未平倉建倉／權利金大幅波動）。Empty list means genuinely nothing to report
    yet (not enough real accumulated days) — callers should show an honest
    "資料收集中" state, not an error.

    bug#00089 版面去重：原本尾隨的 📈📉 每檔標的 skew 方向 bullets（含部位方向
    提示）與分析結論卡的方向依據是同一份 underlying_skew、同一組 70/30 門檻、
    同一句 _stance_note，屬重複輸出，已移除——方向判讀統一由
    generate_verdict_cards 的結論卡呈現，本函式只保留合約層級事件證據。
    """
    bullets: list[str] = []

    for e in report.get("events", [])[:top_n]:
        parts = []
        if e["is_buildup"] and e["oi_delta"]:
            direction = "增加" if e["oi_delta"] > 0 else "減少"
            pct_s = f"（{'+' if e['oi_pct'] and e['oi_pct'] > 0 else ''}{e['oi_pct']:.0f}%）" if e["oi_pct"] is not None else ""
            parts.append(f"未平倉量{direction} {abs(int(e['oi_delta'])):,} 口{pct_s}")
        if e["is_swing"] and e["price_delta_pct"] is not None:
            direction = "上漲" if e["price_delta_pct"] > 0 else "下跌"
            parts.append(f"價格{direction} {abs(e['price_delta_pct']):.0f}%")
        detail = "、".join(parts)
        bullets.append(f"🎯 {_fmt_contract(e)}：{e['first_date']}～{e['last_date']} 期間{detail}")

    return bullets


def _stance_note(underlying: str, signal_dir: str, stance: dict) -> str:
    """依使用者對該標的的多空立場，回傳一句建設性提示（無持倉或混合則從略）。"""
    s_dir = stance.get(underlying)
    if s_dir is None:
        return "　▶ 你尚未持有此標的，可留意是否符合進場條件" if stance else ""
    if s_dir == "混合":
        return ""
    if s_dir == signal_dir:
        return f"　▶ 與你目前偏{s_dir}的部位方向一致"
    return f"　▶ ⚠️ 與你目前偏{s_dir}的部位方向相反，留意反向風險"


# ─────────────────────────────────────────────────────────────────────────────
# bug#00066: 「排除股價變動因素」的震盪/背離訊號（期權預測）
# ─────────────────────────────────────────────────────────────────────────────

def _dte_between(as_of_date: str, expiry: str) -> Optional[int]:
    try:
        d0 = datetime.strptime(as_of_date, "%Y-%m-%d").date()
        d1 = datetime.strptime(expiry, "%Y-%m-%d").date()
        return (d1 - d0).days
    except (ValueError, TypeError):
        return None


def _date_add(date_str: str, days: int) -> Optional[str]:
    try:
        return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def compute_iv_percentile(
    snapshots: list[dict],
    atm_band_pct: float = 10.0,
    min_days: int = 8,
) -> dict:
    """依累積的每日快照計算該標的的「IV 位階」(bug#00068，需求 #1)。

    每一天取「近價平(±atm_band_pct%)」合約 IV 的平均當作當日代表 IV，串成時間序列，
    再算最新一天的代表 IV 在整段歷史中的百分位(≤ 最新值的天數 / 總天數)。這回答了
    「現在的隱含波動率相對自己的歷史是高還是低」——判斷選擇權貴/便宜最關鍵的一條資訊。

    誠實門檻：樣本不足 `min_days` 天就回 ready=False、percentile=None(百分位在樣本
    太少時無意義，不強行顯示)。回傳 {ready, days, current_iv, percentile}。
    """
    series: list[tuple[str, float]] = []
    for s in sorted(snapshots or [], key=lambda x: x.get("date", "")):
        spot = s.get("spot_price")
        if not spot or spot <= 0:
            continue
        lo, hi = spot * (1 - atm_band_pct / 100.0), spot * (1 + atm_band_pct / 100.0)
        ivs = [
            c["impliedVolatility"] for c in s.get("contracts", [])
            if c.get("impliedVolatility") and c.get("strike") is not None
            and c["impliedVolatility"] > 0 and lo <= c["strike"] <= hi
        ]
        if ivs:
            series.append((s.get("date"), sum(ivs) / len(ivs)))

    days = len(series)
    current = series[-1][1] if series else None
    if days < min_days:
        return {"ready": False, "days": days, "current_iv": current, "percentile": None}
    hist = [v for _, v in series]
    below = sum(1 for v in hist if v <= current)
    return {"ready": True, "days": days, "current_iv": current, "percentile": round(below / len(hist) * 100)}


def compute_observed_regime(
    snapshots_by_underlying: dict[str, list[dict]],
    lookback_sessions: int = 6,
    move_threshold_pct: float = 2.0,
    breadth_threshold: float = 0.60,
) -> dict:
    """描述「市場目前發生什麼」，刻意與期權的 forward forecast 分離。

    每檔取最近 `lookback_sessions` 筆有效市場快照的起終現價，報酬 ≤ -2% 視為下跌、
    ≥ +2% 視為上漲；達 60% 廣度且中位報酬同向時才標示市場階段。另比較近價平 IV
    的中位變化，提供「跌價＋升波／跌價＋降波」樣態，但不把 Greeks 或 IV 當成必然的
    未來方向。回傳值只描述已觀察到的 regime，不參與預測命中率。
    """
    symbols: dict[str, dict] = {}
    returns: list[float] = []
    iv_changes: list[float] = []
    up_count = down_count = flat_count = 0

    def _atm_iv(snapshot: dict) -> Optional[float]:
        spot = snapshot.get("spot_price")
        if not spot or spot <= 0:
            return None
        vals = [
            float(c["impliedVolatility"])
            for c in snapshot.get("contracts", [])
            if c.get("impliedVolatility")
            and c.get("strike") is not None
            and abs(float(c["strike"]) / spot - 1.0) <= 0.10
        ]
        return median(vals) if vals else None

    for underlying, raw in snapshots_by_underlying.items():
        snaps = [
            s for s in sorted(raw or [], key=lambda x: x.get("date", ""))
            if s.get("date") and s.get("spot_price") and s["spot_price"] > 0
        ][-max(2, lookback_sessions):]
        if len(snaps) < 2:
            continue
        first, latest = snaps[0], snaps[-1]
        ret = latest["spot_price"] / first["spot_price"] - 1.0
        threshold = move_threshold_pct / 100.0
        state = "down" if ret <= -threshold else "up" if ret >= threshold else "flat"
        if state == "down":
            down_count += 1
        elif state == "up":
            up_count += 1
        else:
            flat_count += 1
        iv0, iv1 = _atm_iv(first), _atm_iv(latest)
        iv_change = (iv1 - iv0) if iv0 is not None and iv1 is not None else None
        if iv_change is not None:
            iv_changes.append(iv_change)
        returns.append(ret)
        symbols[underlying] = {
            "state": state,
            "return": ret,
            "first_date": first["date"],
            "last_date": latest["date"],
            "iv_change": iv_change,
        }

    ready = len(returns)
    median_return = median(returns) if returns else None
    down_share = down_count / ready if ready else 0.0
    up_share = up_count / ready if ready else 0.0
    if ready and down_share >= breadth_threshold and median_return is not None and median_return < 0:
        state = "down"
    elif ready and up_share >= breadth_threshold and median_return is not None and median_return > 0:
        state = "up"
    else:
        state = "mixed"

    median_iv_change = median(iv_changes) if iv_changes else None
    iv_state = (
        "rising" if median_iv_change is not None and median_iv_change >= 0.03
        else "falling" if median_iv_change is not None and median_iv_change <= -0.03
        else "stable" if median_iv_change is not None
        else "unknown"
    )
    return {
        "state": state,
        "ready_count": ready,
        "up_count": up_count,
        "down_count": down_count,
        "flat_count": flat_count,
        "median_return": median_return,
        "down_share": down_share,
        "up_share": up_share,
        "median_iv_change": median_iv_change,
        "iv_state": iv_state,
        "symbols": symbols,
    }


def _repricing_decomp(c0: dict, c1: dict, date0: str, date1: str,
                      spot0: float, spot1: float, r: float) -> "Optional[dict]":
    """bug#00097（使用者要求 1+2）：單一合約在兩筆快照間，扣除 delta/gamma(物理凸性)/
    theta(時間流逝)/DTE 逐日縮短後的「重定價殘差」。

    作法不再用一階泰勒（delta×ΔS），而是直接以 Black-Scholes 重新定價：以 t0 的 IV
    （缺則由 t0 權利金反解）為基準，在「新現價 spot1、**縮短後的到期天數 dte1**、
    IV 維持不變」下算理論價 expected_p1；殘差 = 實際 p1 − expected_p1。
    如此 delta、gamma(凸性)、theta(時間衰減) 與每日不同的 DTE 全部被精確扣除（非近似），
    殘差僅剩「IV/需求重定價」（vega×ΔIV 及高階）。正殘差＝該合約被額外買高。
    回傳 {residual, expected_p1, expected_move(=expected_p1−p0), iv0, dte0, dte1}；
    資料不足（缺價/缺 IV/DTE≤0/無法反解）回 None（誠實缺資料，不臆測）。"""
    from .greeks import bs_price, implied_vol
    p0, p0_low = _quote_mid(c0, date0)
    p1, p1_low = _quote_mid(c1, date1)
    strike, otype = c0.get("strike"), c0.get("type")
    # lastPrice 是「最後一次成交」，不是當下可交易價格。方向特徵只接受雙邊報價中間價；
    # 只要任一端缺雙邊報價或價差過寬就棄權，避免陳舊成交製造虛假 IV residual。
    if p0_low or p1_low or p0 is None or p1 is None or not p0 or not strike:
        return None
    dte0 = _dte_between(date0, c0.get("expiry"))
    dte1 = _dte_between(date1, c1.get("expiry"))
    if dte0 is None or dte1 is None or dte1 <= 0:
        return None
    iv0 = c0.get("impliedVolatility")
    if not iv0 or iv0 <= 0:
        iv0 = implied_vol(spot0, strike, dte0, p0, otype, r=r)
    if not iv0 or iv0 <= 0:
        return None
    expected_p1 = bs_price(spot1, strike, dte1, iv0, otype, r=r)
    if expected_p1 is None:
        return None
    return {
        "residual": p1 - expected_p1,
        "expected_p1": expected_p1,
        "expected_move": expected_p1 - p0,
        "iv0": iv0,
        "dte0": dte0,
        "dte1": dte1,
        "p0": p0,
        "p1": p1,
        "price_source": "mid",
    }


def compute_iv_divergence(
    snapshots_by_underlying: dict[str, list[dict]],
    r: float = 0.04,
    window_days: int = 14,
    price_delta_min_abs: float = 0.10,
    residual_min_abs: float = 0.15,
    residual_min_pct: float = 25.0,
    as_of: Optional[str] = None,
) -> dict:
    """偵測「排除當日股價變動因素後」的期權異常震盪與背離（bug#00066 需求 3）。

    對每個標的，取視窗內最早與最新兩筆真實快照，以 contractSymbol 精確配對同一張
    合約，逐一計算：
      ΔS   = spot_latest - spot_earliest（標的價格變動）
      ΔP   = price_latest - price_earliest（權利金變動）
      預期 = 以 t0 的 IV 在「新現價 spot1、縮短後的 DTE」重新定價後的理論價變動
             （bug#00097：含 delta+gamma(凸性)+theta(時間流逝)+DTE，非僅 delta×ΔS）
      殘差 = ΔP - 預期（扣除上述後、純由 IV/需求/事件驅動的部分）
      ΔIV  = iv_latest - iv_earliest（隱含波動率變化，百分點）

    旗標：
      - is_vol_shock（超額震盪）：|殘差| ≥ residual_min_abs 且 ≥ residual_min_pct% ×
        起始權利金——代表權利金變動大幅超出股價變動所能解釋，屬波動率/事件驅動。
      - is_divergence（背離）：ΔP 與「預期」方向相反，且兩者量值皆具意義——代表權利金
        走勢與標的隱含方向不一致（例如股價上漲買權卻下跌、IV 壓縮）。

    delta0 需要最早日的 IV 才能算；缺 IV 的合約無法估「預期」，直接略過（不臆測）。
    回傳結構比照 compute_options_flow：coverage / events / ready_count 等。
    """
    as_of_date = as_of or taiwan_now().strftime("%Y-%m-%d")
    cutoff_date = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=window_days)).strftime("%Y-%m-%d")

    coverage: dict[str, dict] = {}
    events: list[dict] = []

    for underlying, raw_snaps in snapshots_by_underlying.items():
        snaps = sorted(_filter_window(raw_snaps or [], cutoff_date), key=lambda s: s.get("date", ""))
        ready = len(snaps) >= 2
        coverage[underlying] = {
            "days_in_window": len(snaps),
            "first_date": snaps[0]["date"] if snaps else None,
            "last_date": snaps[-1]["date"] if snaps else None,
            "ready": ready,
        }
        if not ready:
            continue

        earliest, latest = snaps[0], snaps[-1]
        spot0, spot1 = earliest.get("spot_price"), latest.get("spot_price")
        if not spot0 or not spot1:
            continue
        spot_delta = spot1 - spot0
        spot_delta_pct = (spot_delta / spot0 * 100.0) if spot0 else None

        # bug#00068 需求 #2：財報感知。每日快照抓取時已把「當時已知的下次財報日」
        # 記進 snapshot(earnings_date)，故即使財報已過，也仍留有紀錄可判定。若任一
        # 已記錄的財報日落在比較區間 [first-2, last+3] 內，該區間的權利金/IV 劇變多屬
        # 財報預期反應(前跑升波、後崩波)，非獨立訊號 —— 標記後於結論中降權並註明。
        earnings_dates = {s.get("earnings_date") for s in snaps if s.get("earnings_date")}
        win_lo = _date_add(earliest["date"], -2)
        win_hi = _date_add(latest["date"], 3)
        near_earn_date = next(
            (ed for ed in sorted(earnings_dates) if win_lo and win_hi and win_lo <= ed <= win_hi),
            None,
        )
        near_earnings = near_earn_date is not None

        early_by_sym = {c["contractSymbol"]: c for c in earliest.get("contracts", []) if c.get("contractSymbol")}
        late_by_sym = {c["contractSymbol"]: c for c in latest.get("contracts", []) if c.get("contractSymbol")}

        for csym in set(early_by_sym) & set(late_by_sym):
            c0, c1 = early_by_sym[csym], late_by_sym[csym]
            iv0, iv1 = c0.get("impliedVolatility"), c1.get("impliedVolatility")

            # bug#00097：預期變動改用「扣除 delta/gamma(凸性)/theta(時間)/DTE」的重定價分解，
            # 不再只扣 delta×ΔS；殘差僅剩 IV/需求驅動。
            dec = _repricing_decomp(c0, c1, earliest["date"], latest["date"], spot0, spot1, r)
            if dec is None:
                continue  # 缺 IV/DTE 無法估重定價，跳過（不臆測）

            p0, p1 = dec["p0"], dec["p1"]
            price_delta = p1 - p0
            expected = dec["expected_move"]
            residual = dec["residual"]
            iv_delta_pts = ((iv1 - iv0) * 100.0) if (iv0 is not None and iv1 is not None) else None

            is_vol_shock = (
                abs(residual) >= residual_min_abs
                and abs(residual) >= (residual_min_pct / 100.0) * p0
            )
            # 背離：實際權利金方向與「股價隱含方向」相反，且雙方量值皆有意義
            is_divergence = (
                abs(price_delta) >= price_delta_min_abs
                and abs(expected) >= price_delta_min_abs
                and (price_delta > 0) != (expected > 0)
            )
            if not (is_vol_shock or is_divergence):
                continue

            events.append({
                "underlying": underlying,
                "contractSymbol": csym,
                "type": c1.get("type"),
                "strike": c1.get("strike"),
                "expiry": c1.get("expiry"),
                "spot_delta": round(spot_delta, 2),
                "spot_delta_pct": round(spot_delta_pct, 1) if spot_delta_pct is not None else None,
                "price_delta": round(price_delta, 2),
                "expected_move": round(expected, 2),
                "residual": round(residual, 2),
                "residual_pct": round(residual / p0 * 100.0, 0) if p0 else None,
                "iv_delta_pts": round(iv_delta_pts, 1) if iv_delta_pts is not None else None,
                "is_vol_shock": is_vol_shock,
                "is_divergence": is_divergence,
                "near_earnings": near_earnings,
                "earnings_date": near_earn_date,
                "first_date": earliest["date"],
                "last_date": latest["date"],
            })

    # 非財報驅動的訊號優先(near_earnings=False 排前)，其次依殘差大小
    events.sort(key=lambda e: (e.get("near_earnings", False), -abs(e["residual"] or 0)))
    ready_count = sum(1 for c in coverage.values() if c["ready"])
    return {
        "window_days": window_days,
        "as_of": as_of_date,
        "coverage": coverage,
        "ready_count": ready_count,
        "total_count": len(coverage),
        "ready_pct": round(ready_count / len(coverage) * 100, 1) if coverage else 0.0,
        "events": events,
    }


def _iv_percentile_note(underlying: str, iv_pct_by_underlying: Optional[dict]) -> str:
    """把 IV 位階(compute_iv_percentile)接成一句提示；資料不足或缺就從略(bug#00068)。"""
    if not iv_pct_by_underlying:
        return ""
    info = iv_pct_by_underlying.get(underlying)
    if not info or not info.get("ready") or info.get("percentile") is None:
        return ""
    p = info["percentile"]
    if p >= 70:
        tone = "偏高，選擇權相對貴、賣方/收租較有利"
    elif p <= 30:
        tone = "偏低，選擇權相對便宜、買方成本較低"
    else:
        tone = "中性"
    return f"　▶ 標的 IV 位階第 {p} 百分位（近{info['days']}日，{tone}）"


def generate_divergence_conclusions(
    report: dict, top_n: int = 6, positions=None, iv_pct_by_underlying: Optional[dict] = None
) -> list[str]:
    """把 compute_iv_divergence() 的事件轉成中文「選擇權投資建議」bullet。

    每則點出：股價變動能解釋多少、實際變動多少、扣除後的超額殘差與 IV 變化，並在
    有持倉時附上與部位方向一致/相反的建設性提示。若事件區間含財報(near_earnings)
    則註明多屬財報預期反應、非獨立訊號(bug#00068 #2)；若有 IV 位階則附上(bug#00068 #1)。
    空 list 代表暫無足夠真實資料。
    """
    from .shared import position_stance_by_symbol
    stance = position_stance_by_symbol(positions) if positions else {}

    bullets: list[str] = []
    for e in report.get("events", [])[:top_n]:
        cp = "買權" if e["type"] == "call" else "賣權"
        contract = f"{e['underlying']} ${e['strike']:g}{cp} {e['expiry']}到期"
        iv_s = f"，IV {e['iv_delta_pts']:+.1f}pt" if e["iv_delta_pts"] is not None else ""
        sd_pct = f"{e['spot_delta_pct']:+.1f}%" if e["spot_delta_pct"] is not None else "—"

        if e["is_divergence"]:
            stock_dir = "上漲" if (e["spot_delta"] or 0) > 0 else "下跌"
            opt_dir = "上漲" if (e["price_delta"] or 0) > 0 else "下跌"
            head = (
                f"↔️ 背離：{contract}　股價{stock_dir} {sd_pct}，但權利金反向{opt_dir} "
                f"${e['price_delta']:+.2f}{iv_s}，方向不一致，留意"
            )
        else:  # vol shock
            head = (
                f"🌀 異常震盪：{contract}　股價變動 {sd_pct} 僅能解釋約 ${e['expected_move']:+.2f}，"
                f"實際權利金變動 ${e['price_delta']:+.2f}，扣除股價後殘差 ${e['residual']:+.2f}"
                f"（{e['residual_pct']:+.0f}%){iv_s}，研判為波動率/事件驅動而非跟隨股價"
            )

        # 財報感知：區間含財報時明確降級提示（避免把 IV crush/run-up 誤當獨立訊號）
        if e.get("near_earnings"):
            head += f"　▶ ⚠️ 區間含財報（{e.get('earnings_date')}），IV/權利金劇變多屬財報預期反應，非獨立訊號"

        # 部位方向提示：買權偏多、賣權偏空，與使用者立場比對
        signal_dir = "多" if e["type"] == "call" else "空"
        head += _stance_note(e["underlying"], signal_dir, stance)
        head += _iv_percentile_note(e["underlying"], iv_pct_by_underlying)
        bullets.append(head)

    return bullets


def generate_analysis_card(
    verdict_report: dict,
    flow_report: dict,
    div_report: dict,
    backtest: Optional[dict] = None,
    positions=None,
    iv_pct_by_underlying: Optional[dict] = None,
    top_verdicts: Optional[int] = None,
    include_neutral: bool = False,
    top_divergence: int = 6,
    top_flow: int = 4,
) -> list[str]:
    """整合後的「分析結論卡」輸出（bug#00089 版面去重，取代原
    generate_combined_options_advice）——原「分析結論卡」與「選擇權投資建議」
    兩區目的重複（skew 方向 bullets 與結論卡的方向依據是同一份 underlying_skew、
    同一組門檻、同一句部位提示），整合為單一輸出，仍由期權觀察清單頁面與
    Dashboard 首頁卡片共用「同一份」文字（沿用 bug#00067 兩處對齊原則）。

    結構：先列每檔標的的綜合方向結論（generate_verdict_cards：方向＋回測命中率＋
    IV 位階＋部位提示），再列支持判讀的「重點異常事件」——🌀 異常震盪 / ↔️ 背離 /
    🎯 建倉與價格波動，皆為合約層級證據，與結論卡不重複。事件列不再重附 IV 位階
    （結論卡已有，避免同一標的重複兩次）。空 list 代表資料仍在累積。
    """
    bullets = generate_verdict_cards(
        verdict_report, backtest=backtest, positions=positions,
        iv_pct_by_underlying=iv_pct_by_underlying,
        top_n=top_verdicts, include_neutral=include_neutral,
    )
    events = generate_divergence_conclusions(div_report, top_n=top_divergence, positions=positions)
    events += generate_options_conclusions(flow_report, top_n=top_flow)
    if events:
        if bullets:
            bullets.append("[dim]── 重點異常事件（支持上方判讀的合約層級訊號）──[/dim]")
        bullets += events
    return bullets


def _clean_note(s: str) -> str:
    """把 _verdict_backtest_note / _stance_note / _iv_percentile_note 的
    「　▶ 」前綴去掉，供分組版面改用自己的縮排項目符號。"""
    return s.replace("　▶ ", "").replace("▶ ", "").strip()


def generate_options_recommendations(
    verdict_report: dict,
    flow_report: dict,
    div_report: dict,
    snapshots_by_underlying: dict,
    r: float = 0.04,
    window_days: int = 14,
    positions=None,
    iv_pct_by_underlying: Optional[dict] = None,
    include_neutral: bool = False,
    top_events_per_underlying: int = 4,
    top_n: Optional[int] = None,
    verdict_params: Optional[dict] = None,
) -> "list":
    """建立每檔標的的結構化期權預測建議。

    原始 skew／殘差方向、固定 +5-session 的校準機率、purged walk-forward、Brier skill、
    是否可採用及失效後如何修改，都經 options_forecasting 的同一個 interface 判定。
    未通過完整驗證仍顯示原始預測，但 Recommendation.direction 固定為「觀望」。
    """
    from .shared import Recommendation, _section, position_stance_by_symbol
    from .calibration import backtest_verdicts
    from .options_forecasting import assess_option_forecast
    stance = position_stance_by_symbol(positions) if positions else {}
    active_bias_min_pct = float((verdict_params or {}).get("bias_min_pct", 0.03))
    verdicts = verdict_report.get("verdicts", {})
    items = [
        (u, v) for u, v in verdicts.items()
        if v["ready"] and (v["direction"] is not None or include_neutral)
    ]

    def _strength(v: dict) -> float:
        s = abs(v["bias"] or 0.0)
        if v["call_pct"] is not None:
            s += abs(v["call_pct"] - 50.0) / 100.0
        return s
    items.sort(key=lambda kv: (kv[1]["direction"] is None, -_strength(kv[1])))
    if top_n is not None:
        items = items[:top_n]

    all_div_events = div_report.get("events", [])
    all_flow_events = flow_report.get("events", [])

    recs: list = []
    for u, v in items:
        bt_u = backtest_verdicts(
            {u: snapshots_by_underlying.get(u, [])},
            window_days=window_days,
            r=r,
            verdict_params=verdict_params,
        )
        assessment = assess_option_forecast(
            v.get("direction"),
            bt_u,
            verdict_params=verdict_params,
        )
        h_best = assessment.horizon
        display_direction = assessment.actionable_direction
        mark = ("🟢 看多" if display_direction == "多" else "🔴 看空" if display_direction == "空" else "⚪ 觀望")
        if assessment.status == "degraded":
            mark = f"⚠️ {mark}"

        # 判斷依據（第二層）
        basis_bits = []
        if v["skew_score"] != 0 and v["call_pct"] is not None:
            side = "買權" if v["skew_score"] > 0 else "賣權"
            pct = v["call_pct"] if v["skew_score"] > 0 else 100.0 - v["call_pct"]
            basis_bits.append(f"新增未平倉 {pct:.0f}% 集中{side}")
        if v.get("bias_score", 0) != 0 and v["bias"] is not None:
            basis_bits.append(f"排除股價變動後 OI 加權殘差 ${v['bias']:+.2f}/股（{v['bias_n']} 張）")

        if display_direction is None:
            if v["direction"] is not None:
                basis = (
                    f"原始籌碼訊號指向{v['direction']}，但不作為正式方向。"
                    f"回測診斷：{assessment.diagnosis} 修改建議：{assessment.modification_guidance}"
                )
            elif v.get("skew_unconfirmed"):
                basis = "建倉 skew 未獲同側重定價確認（疑為賣方發起），暫不給方向。"
            elif v["conflict"]:
                basis = "skew 與殘差方向相反，兩訊號矛盾，暫不給方向。"
            else:
                basis = "無足夠方向訊號。"
        else:
            basis = (("；".join(basis_bits) + "。") if basis_bits else "") + \
                    "方向已通過 purged walk-forward 與機率回測，才可作為正式預測。"

        # 第三層 sections
        secs = []
        secs.append(_section(
            "① Dollar Delta OI Skew（建倉方向）",
            formula="Dollar Delta OI = ΔOI × Spot × |Delta| × 100；買權占比 call_pct ≥ 70% 計看多、≤ 30% 計看空",
            substitution=(f"買權占比 call_pct = {v['call_pct']:.0f}%" if v.get("call_pct") is not None else "資料累積中"),
            explanation="以 Delta 權重名義金額曝光取代舊有純合約張數，避免數百張低價末日 Call 以張數扭曲真實機構資金方向。"))
        secs.append(_section(
            "② BS 重定價殘差偏向（IV/需求）",
            formula=("以合約 t0 IV 為基準，用 Black-Scholes 在『新現價＋當日縮短後 DTE＋IV 不變』下重定價，"
                     "殘差 = 實際權利金 − 理論價（精確扣除 delta/gamma/theta/DTE）；bias = Σ買權殘差 − Σ賣權殘差（OI 加權）；"
                     f"方向門檻 = max(0.15, {active_bias_min_pct:.2f}% × 現價)"),
            substitution=(f"OI 加權淨殘差 bias = ${v['bias']:+.2f}/股（{v['bias_n']} 張合約）" if v.get("bias") is not None else "資料累積中"),
            explanation="殘差僅剩由 IV／需求驅動的重定價：買權殘差為正＝偏多力量、賣權殘差為正＝偏空力量。skew 需同側殘差交叉確認買/賣方向，否則標 skew_unconfirmed 不計入。"))
        latest_snap = snapshots_by_underlying.get(u, [])[-1] if snapshots_by_underlying.get(u) else None
        exp_move = compute_expected_move(latest_snap) if latest_snap else None
        if v.get("direction") is not None:
            probability = (
                f"{assessment.probability * 100:.0f}%"
                if assessment.probability is not None else "尚無法估計"
            )
            baseline_probability = (
                f"{assessment.baseline_probability * 100:.0f}%"
                if assessment.baseline_probability is not None else "—"
            )
            brier_skill = (
                f"{assessment.brier_skill:+.3f}"
                if assessment.brier_skill is not None else "—"
            )
            secs.append(_section(
                "③ 預測機率、purged 回測與失效診斷",
                formula=("前瞻期事前固定為 +5 個市場 session；同標的重疊 outcome 區間只保留一筆。"
                         "P(同向) 只用預測當時已成熟 outcome 做 expanding empirical-Bayes 校準；"
                         "Brier skill = 1 − Brier(model) / Brier(base rate)"),
                substitution=(
                    f"h=+{h_best or 5} sessions，校準機率 {probability}（基準 {baseline_probability}），"
                    f"purged n={assessment.sample_n}／raw n={assessment.raw_sample_n}，"
                    f"Brier skill {brier_skill}，狀態 {assessment.status}"
                ),
                explanation=(f"診斷：{assessment.diagnosis} 修改方式："
                             f"{assessment.modification_guidance}")))
        if exp_move and exp_move.get("spot") and exp_move.get("sigma_abs"):
            spot_val = exp_move["spot"]; sig_val = exp_move["sigma_abs"]
            lo_val = max(0.0, spot_val - sig_val); hi_val = spot_val + sig_val
            secs.append(_section(
                f"Expected Move（未來 {_em_dte(exp_move)} 天 ±1σ）",
                formula=_em_formula(exp_move),
                substitution=f"預估波動範圍 ${lo_val:.2f} ～ ${hi_val:.2f}{_em_iv_note(exp_move)}",
                explanation="無方向的波動預期，供評估進出場區間與損益兩平。"))
        if v.get("near_earnings"):
            secs.append(_section("財報降權", substitution=f"⚠️ 區間含財報（{v['earnings_date']}）",
                                 explanation="區間含財報者多屬財報預期反應，方向訊號降權看待。"))
        st = _clean_note(_stance_note(u, v["direction"], stance))
        if st:
            secs.append(_section("與你的部位比對", substitution=st,
                                 explanation="以你目前持倉的淨多空立場與此訊號方向交叉比對，作為建設性提示。"))
        iv = _clean_note(_iv_percentile_note(u, iv_pct_by_underlying))
        if iv:
            secs.append(_section("IV 位階", substitution=iv,
                                 explanation="標的相對自身歷史的 IV 百分位，輔助判斷期權買賣方成本高低。"))
        # 合約層級重點事件（只取這檔）
        u_div = {"events": [e for e in all_div_events if e.get("underlying") == u]}
        u_flow = {"events": [e for e in all_flow_events if e.get("underlying") == u]}
        ev = generate_divergence_conclusions(u_div, top_n=top_events_per_underlying)
        ev += generate_options_conclusions(u_flow, top_n=top_events_per_underlying)
        if ev:
            secs.append(_section("重點異常事件（已排除當日股價變動）",
                                 substitution="\n".join(f"· {e}" for e in ev),
                                 explanation="異常震盪/背離與大量建倉事件，皆已排除當日股價變動因素，僅呈現超出價格變動的期權活動。"))

        recs.append(Recommendation(
            rec_id=f"opt:{u}", category="options",
            direction=(display_direction or "觀望"),
            verdict=f"{mark} [bold]{u}[/bold] ｜ {assessment.summary}",
            basis=basis,
            detail_sections=secs,
        ))
    return recs


def generate_grouped_analysis_card(
    verdict_report: dict,
    flow_report: dict,
    div_report: dict,
    snapshots_by_underlying: dict,
    r: float = 0.04,
    window_days: int = 14,
    positions=None,
    iv_pct_by_underlying: Optional[dict] = None,
    include_neutral: bool = False,
    top_events_per_underlying: int = 3,
    summary_only: bool = False,
    verdict_params: Optional[dict] = None,
) -> list[str]:
    """bug#00099：把「分析結論卡」改為**逐標的分組、縮排**的版面，且每檔標的用
    **自己的獨立 purged walk-forward 回測**（只餵該標的的快照）給出校準機率與 proper score——
    不再讓所有標的共用同一份彙總回測、也不再把各標的的事件混在同一串清單下。

    summary_only=True（bug#00100，供 Dashboard 卡片使用）：每檔只輸出**一行總結**，
    清楚區分原始預測與通過完整回測後才可採用的正式方向；細節留到期權頁呈現。

    每組結構（回傳為「已完成排版」的多行字串清單，呼叫端直接 "\\n".join，不要再加
    項目符號前綴）：

        ⚪ 觀望 NVDA ｜ 模型原始預測 +5 sessions 上漲，但回測未通過
            · 回測診斷：不重疊成熟樣本不足
            · 如何修改：先累積 outcome，不為了過門檻調參

    include_neutral=True 時，已就緒但無方向的標的也會列出「⚪ 觀望」組（供完整頁面
    使用）；卡片取 False 只列有方向者。空 list 代表資料仍在累積。
    """
    from .calibration import backtest_verdicts
    from .shared import position_stance_by_symbol
    stance = position_stance_by_symbol(positions) if positions else {}

    verdicts = verdict_report.get("verdicts", {})
    items = [
        (u, v) for u, v in verdicts.items()
        if v["ready"] and (v["direction"] is not None or include_neutral)
    ]

    def _strength(v: dict) -> float:
        s = abs(v["bias"] or 0.0)
        if v["call_pct"] is not None:
            s += abs(v["call_pct"] - 50.0) / 100.0
        return s
    items.sort(key=lambda kv: (kv[1]["direction"] is None, -_strength(kv[1])))

    all_div_events = div_report.get("events", [])
    all_flow_events = flow_report.get("events", [])

    lines: list[str] = []
    for u, v in items:
        # 此標的獨立回測（只餵這檔快照）
        bt_u = backtest_verdicts(
            {u: snapshots_by_underlying.get(u, [])},
            window_days=window_days,
            r=r,
            verdict_params=verdict_params,
        )
        from .options_forecasting import assess_option_forecast
        assessment = assess_option_forecast(
            v.get("direction"),
            bt_u,
            verdict_params=verdict_params,
        )
        h_best = assessment.horizon
        display_direction = assessment.actionable_direction
        mark = (
            "🟢 看多" if display_direction == "多"
            else "🔴 看空" if display_direction == "空"
            else "⚪ 觀望"
        )
        if assessment.status == "degraded":
            mark = f"⚠️ {mark}"

        # bug#00100: Dashboard 卡片只要一行總結（方向＋該檔獨立回測命中率）
        if summary_only:
            trend_mark = "🟢" if display_direction == "多" else "🔴" if display_direction == "空" else "⚪"
            lines.append(f"{trend_mark} [bold]{u}[/bold]　{assessment.summary}")
            continue

        if lines:
            lines.append("")  # 組間空行

        latest_snap = snapshots_by_underlying.get(u, [])[-1] if snapshots_by_underlying.get(u) else None
        exp_move = compute_expected_move(latest_snap) if latest_snap else None

        lines.append(f"{mark} [bold]{u}[/bold] ｜ {assessment.summary}")
        lines.append(f"    · 籌碼數據源：基於 {v['first_date']} ～ {v['last_date']} 觀察視窗（回看 {window_days} 天）")

        basis = []
        if v["skew_score"] != 0 and v["call_pct"] is not None:
            side = "買權" if v["skew_score"] > 0 else "賣權"
            pct = v["call_pct"] if v["skew_score"] > 0 else 100.0 - v["call_pct"]
            basis.append(f"新增未平倉 {pct:.0f}% 集中{side}")
        if v.get("bias_score", 0) != 0 and v["bias"] is not None:
            basis.append(f"排除股價變動後 OI 加權殘差 ${v['bias']:+.2f}/股（{v['bias_n']} 張）")

        if display_direction is None:
            if v["direction"] is not None:
                reason = f"回測診斷：{assessment.diagnosis}"
            elif v.get("skew_unconfirmed"):
                reason = "建倉 skew 未獲同側重定價確認（疑為賣方發起），暫不給方向"
            elif v["conflict"]:
                reason = "skew 與殘差方向相反，暫不給方向"
            else:
                reason = "無足夠方向訊號"
            lines.append(f"    [dim]· {reason}[/dim]")
            if v["direction"] is not None:
                lines.append(f"    [bold yellow]· 如何修改：{assessment.modification_guidance}[/bold yellow]")
        else:
            if basis:
                lines.append(f"    · 依據：{'；'.join(basis)}")

            if exp_move and exp_move.get("spot") and exp_move.get("sigma_abs"):
                spot_val = exp_move["spot"]
                sig_val = exp_move["sigma_abs"]
                lo_val = max(0.0, spot_val - sig_val)
                hi_val = spot_val + sig_val
                lines.append(f"    · 未來 {_em_dte(exp_move)} 天預估波動範圍 (±1σ)："
                             f"${lo_val:.2f} ～ ${hi_val:.2f}{_em_iv_note(exp_move)}")

            probability = assessment.probability * 100 if assessment.probability is not None else None
            base_probability = (
                assessment.baseline_probability * 100
                if assessment.baseline_probability is not None else None
            )
            skill = assessment.brier_skill
            lines.append(
                "    · 機率回測："
                f"[bold green]校準機率 {probability:.0f}%[/bold green]，"
                f"基準 {base_probability:.0f}%，purged n={assessment.sample_n}，"
                f"Brier skill {skill:+.3f}"
            )

            if v.get("skew_unconfirmed"):
                lines.append("    · [dim]ⓘ 建倉 skew 未獲同側重定價確認，已不計入方向[/dim]")
            if v["near_earnings"]:
                lines.append(f"    · [yellow]⚠️ 區間含財報（{v['earnings_date']}），多屬財報預期反應，降權看待[/yellow]")
            st = _clean_note(_stance_note(u, v["direction"], stance))
            if st:
                lines.append(f"    · {st}")
            iv = _clean_note(_iv_percentile_note(u, iv_pct_by_underlying))
            if iv:
                lines.append(f"    · {iv}")

        # 該標的合約層級事件（只取這檔）。
        u_div = {"events": [e for e in all_div_events if e.get("underlying") == u]}
        u_flow = {"events": [e for e in all_flow_events if e.get("underlying") == u]}
        ev = generate_divergence_conclusions(u_div, top_n=top_events_per_underlying)
        ev += generate_options_conclusions(u_flow, top_n=top_events_per_underlying)
        if ev:
            lines.append("    [dim]重點事件：[/dim]")
            for e in ev:
                lines.append(f"        {e}")

    return lines


# ─────────────────────────────────────────────────────────────────────────────
# bug#00069 / bug#00071: 淨 Greeks —— **逐標的**各自計算(INTC 的期權只算 INTC，
# 不與 MU/AMD 混在一起)，每檔給自己的 delta$/theta/vega 與自身漲跌情境；另附一個
# 清楚標示的「投組合計」供參考(非混算)。
# ─────────────────────────────────────────────────────────────────────────────

def _greeks_for_group(positions, spot, r, today) -> dict:
    """計算「單一標的」(同一 underlying 的股票+選擇權)自身的淨 Greeks。"""
    from .greeks import bs_greeks, implied_vol

    d = t = v = gc = 0.0
    priced = 0
    unpriced: list[str] = []
    has_options = False

    for p in positions:
        if p.instrument_type in ("stock", "etf"):
            price = p.market_price if p.market_price is not None else spot
            if price is None:
                unpriced.append(p.symbol)
                continue
            d += p.quantity * price
            priced += 1
        elif p.instrument_type == "option":
            has_options = True
            premium = p.market_price
            if (spot is None or premium is None or premium <= 0
                    or not p.strike or not p.expiry or not p.option_type):
                unpriced.append(p.symbol)
                continue
            dte = _dte_between(today, p.expiry)
            if dte is None or dte <= 0:
                unpriced.append(p.symbol)
                continue
            iv = implied_vol(spot, p.strike, dte, premium, p.option_type, r)
            g = bs_greeks(spot, p.strike, dte, iv, p.option_type, r=r) if iv else None
            if not g or g["delta"] is None:
                unpriced.append(p.symbol)
                continue
            scale = p.quantity * (p.multiplier or 100.0)
            d += g["delta"] * scale * spot
            t += g["theta"] * scale
            v += g["vega"] * scale
            gc += g["gamma"] * scale * spot * spot
            priced += 1

    scen = {pct: d * (pct / 100.0) + 0.5 * gc * (pct / 100.0) ** 2 for pct in (-10, -5, 5, 10)}
    return {
        "delta_dollars": d, "theta_day": t, "vega_1pt": v, "gamma_cash": gc,
        "scenarios": scen, "priced": priced, "unpriced": unpriced, "has_options": has_options,
    }


def _business_days_between(d0, d1) -> int:
    """d0→d1 之間的交易日數(不含週末，忽略假日)。d1<=d0 回 0。"""
    if d1 <= d0:
        return 0
    days = 0
    cur = d0
    while cur < d1:
        cur += timedelta(days=1)
        if cur.weekday() < 5:  # 一~五
            days += 1
    return days


def _last_trade_stale(last_trade_date: Optional[str], as_of: Optional[str],
                      max_stale_days: int = 1) -> bool:
    """lastPrice 的成交時間是否已過期(超過 max_stale_days 個交易日)。
    無法解析成交時間或快照日期時回 False(無從判斷，保守地不因此丟棄，但仍會被標低可信度)。"""
    if not last_trade_date or not as_of:
        return False
    try:
        td = datetime.strptime(last_trade_date[:10], "%Y-%m-%d").date()
        ad = datetime.strptime(as_of[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False
    return _business_days_between(td, ad) > max_stale_days


def _quote_mid(c: dict, as_of: Optional[str] = None,
               max_spread_pct: float = 30.0) -> tuple[Optional[float], bool]:
    """回傳 (價格, low_confidence)。優先用 (bid+ask)/2 中間價；沒有可靠雙邊報價、
    或買賣價差過寬(>max_spread_pct)時，退回 lastPrice 並標記低可信度。
    若 lastPrice 的成交時間已過期(超過 1 個交易日)，則不再拿它當 fallback——回
    (None, True) 讓上層視為資料不足，比顯示一個過時價格更誠實。"""
    bid, ask = c.get("bid"), c.get("ask")
    if bid and ask and bid > 0 and ask > 0 and ask >= bid:
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid * 100.0
        return mid, spread_pct > max_spread_pct
    last = c.get("lastPrice")
    if last and last > 0:
        if _last_trade_stale(c.get("lastTradeDate"), as_of):
            return None, True  # 過期成交價 → 視為資料不足，不顯示
        return last, True  # 無雙邊報價 → 一律視為低可信度
    return None, True


def compute_expected_move(snapshot: Optional[dict], factor: float = 0.85,
                          target_dte: int = 30) -> Optional[dict]:
    """由最新快照估算「到期前的市場隱含波動」(bug#00073 / bug#00077)。

    到期日選擇：在快照內選 DTE 最接近 target_dte(預設 30) 且同時有價平買賣權的
    到期日——不再永遠取最近到期，避免只剩 1~7 天的短天期被 Gamma/財報/週選放大。
    價平＝履約價最接近現價者。

    兩個語意不同、分開回傳的數字：
      * sigma_abs：主數值 ±1σ = spot × ATM_IV × √(DTE/365)（年化、無方向的波動預期）。
        缺 IV 時退回「跨式價 × factor(0.85)」近似並標記低可信度。
      * straddle / breakeven_abs：價平跨式價，代表「到期損益兩平區間」寬度——與 σ 不同,
        不應混為一談。

    價格一律優先用 (bid+ask)/2 中間價，無雙邊報價或價差過寬時退回 lastPrice 並在
    low_confidence 標記(見 _quote_mid)。100% 用真實報價,無估計/回填。

    回傳 {expiry, dte, atm_strike, straddle, breakeven_abs, sigma_abs, sigma_pct,
    atm_iv, spot, low_confidence} 或 None(資料不足時)。
    """
    if not snapshot:
        return None
    spot = snapshot.get("spot_price")
    as_of = snapshot.get("date")
    contracts = snapshot.get("contracts", [])
    if not spot or spot <= 0 or not contracts:
        return None

    by_exp: dict[str, list] = {}
    for c in contracts:
        e = c.get("expiry")
        if e:
            by_exp.setdefault(e, []).append(c)

    # 選 DTE 最接近 target_dte 且同時有價平買/賣權的到期日
    best = None  # (|dte-target|, expiry, dte, atm, call, put)
    for e, cs in by_exp.items():
        calls = {c["strike"]: c for c in cs if c.get("type") == "call" and c.get("strike") is not None}
        puts = {c["strike"]: c for c in cs if c.get("type") == "put" and c.get("strike") is not None}
        common = set(calls) & set(puts)
        if not common:
            continue
        dte = _dte_between(as_of, e)
        if dte is None or dte <= 0:
            continue
        atm = min(common, key=lambda k: abs(k - spot))
        rank = abs(dte - target_dte)
        if best is None or rank < best[0]:
            best = (rank, e, dte, atm, calls[atm], puts[atm])
    if best is None:
        return None
    _, e, dte, atm, call, put = best

    call_px, call_lc = _quote_mid(call, as_of)
    put_px, put_lc = _quote_mid(put, as_of)
    if call_px is None or put_px is None:
        return None
    straddle = call_px + put_px
    low_conf = call_lc or put_lc

    ivs = [x for x in (call.get("impliedVolatility"), put.get("impliedVolatility")) if x and x > 0]
    atm_iv = (sum(ivs) / len(ivs)) if ivs else None

    if atm_iv:
        sigma_abs = spot * atm_iv * (dte / 365.0) ** 0.5
    else:
        sigma_abs = straddle * factor  # 無 IV 時退回跨式近似
        low_conf = True

    return {
        "expiry": e,
        "dte": dte,
        "atm_strike": atm,
        "straddle": straddle,
        "breakeven_abs": straddle,
        "sigma_abs": sigma_abs,
        "sigma_pct": sigma_abs / spot * 100.0,
        "atm_iv": atm_iv,
        "spot": spot,
        "low_confidence": low_conf,
    }


def compute_portfolio_greeks(positions, spot_by_underlying: dict, r: float = 0.04,
                             options_only: bool = False) -> dict:
    """**逐標的**計算淨 Greeks(bug#00071)：先把部位依 underlying 分桶(股票用自身代碼、
    選擇權用其 underlying)，每檔標的只用自己的部位與自己的現價計算，彼此不混算。

    `options_only=True`(bug#00072)：此為「期權分析」情境(期權觀察清單頁面),完全略過
    現股/ETF 部位——只分析選擇權,不因使用者持有現股就回頭把現股算進來。

    每檔標的(以美元計)：
        delta$    = Σ delta × 數量 × 乘數 × 標的現價 (股票 delta=1)
        theta/日  = Σ theta × 數量 × 乘數
        vega/1pt  = Σ vega  × 數量 × 乘數
        情境      = 該標的自身 ±m：delta$·m + ½·gamma_cash·m²
    另回傳 `total`(清楚標示的投組合計,僅供參考)。缺現價/無法反解 IV 者列入 unpriced。

    回傳 {"by_underlying": {sym: {...}}, "total": {...}}。
    """
    today = taiwan_now().strftime("%Y-%m-%d")

    from .shared import is_taiwan_position

    buckets: dict[str, list] = {}
    for p in positions or []:
        # 期權分析（投資建議）一律排除台股部位（bug#00091）。
        if is_taiwan_position(p):
            continue
        it = p.instrument_type
        if it in ("stock", "etf"):
            if options_only:
                continue  # 期權分析頁面：現股不納入
            key = p.symbol.upper().replace(".TWO", "").replace(".TW", "")
        elif it == "option":
            key = (p.underlying or "").upper()
        else:
            continue
        if not key:
            continue
        buckets.setdefault(key, []).append(p)

    by_underlying: dict[str, dict] = {}
    for key, ps in buckets.items():
        # 股票用自身 market_price；選擇權需要標的現價 → spot_by_underlying[key]
        spot = spot_by_underlying.get(key)
        by_underlying[key] = _greeks_for_group(ps, spot, r, today)

    total = {"delta_dollars": 0.0, "theta_day": 0.0, "vega_1pt": 0.0,
             "gamma_cash": 0.0, "priced": 0, "unpriced": []}
    for g in by_underlying.values():
        total["delta_dollars"] += g["delta_dollars"]
        total["theta_day"] += g["theta_day"]
        total["vega_1pt"] += g["vega_1pt"]
        total["gamma_cash"] += g["gamma_cash"]
        total["priced"] += g["priced"]
        total["unpriced"] += g["unpriced"]
    total["scenarios"] = {
        pct: total["delta_dollars"] * (pct / 100.0) + 0.5 * total["gamma_cash"] * (pct / 100.0) ** 2
        for pct in (-10, -5, 5, 10)
    }

    return {"by_underlying": by_underlying, "total": total}


# ─────────────────────────────────────────────────────────────────────────────
# bug#00089: 分析結論卡 —— 每檔標的的「綜合方向結論」（未平倉 skew ＋ 排除股價
# 變動的殘差偏向），與回測共用同一套邏輯：calibration.backtest_verdicts 逐日
# walk-forward 重算的就是 compute_directional_verdicts 本尊，畫面顯示的預測
# 邏輯與被驗證的邏輯保證是同一個函式，無兩套標準。
# ─────────────────────────────────────────────────────────────────────────────

def _residual_bias(earliest: dict, latest: dict, r: float) -> "tuple[Optional[float], int, Optional[float], Optional[float]]":
    """兩筆快照間以 contractSymbol 精確配對的合約，扣除 delta/gamma(凸性)/theta(時間
    流逝)/DTE 變化後的淨「重定價」殘差偏向（bug#00097）。

    殘差定義與 compute_iv_divergence 共用同一個 _repricing_decomp（以 t0 的 IV 在
    新現價、縮短後的 DTE 重新定價，殘差 = 實際 p1 − 理論價），但這裡不套事件門檻——
    要的是全體可比對合約的**淨方向**：買權殘差為正代表偏多力量、賣權殘差為正代表偏空。

    bug#00108（使用者審查 #2）：改為 **OI（未平倉量）加權平均**而非等權「加總」——
    舊版逐張等權相加，使單一高價或極低 OI 合約即可主導整檔標的方向；現以各合約 OI
    為權重取加權平均，殘差以「每股美元」計並回傳，門檻端再相對現價正規化（見
    compute_directional_verdicts）。同時分別回傳買權側／賣權側的 OI 加權平均殘差，供
    bug#00109（#3）以「同側重定價」交叉確認建倉 skew 的買/賣方向。
    回傳 (bias 每股OI加權平均, n, call_resid, put_resid)；缺資料時 (None,0,None,None)。"""
    spot0, spot1 = earliest.get("spot_price"), latest.get("spot_price")
    if not spot0 or not spot1:
        return None, 0, None, None
    d0, d1 = earliest.get("date"), latest.get("date")

    early = {c["contractSymbol"]: c for c in earliest.get("contracts", []) if c.get("contractSymbol")}
    late = {c["contractSymbol"]: c for c in latest.get("contracts", []) if c.get("contractSymbol")}
    num = den = 0.0                      # 全體：Σ sign·殘差·w ／ Σ w
    num_call = den_call = 0.0            # 買權側（供 #3 同側確認）
    num_put = den_put = 0.0             # 賣權側
    n = 0
    for csym in set(early) & set(late):
        c0, c1 = early[csym], late[csym]
        if _no_live_data(c0) or _no_live_data(c1):
            continue  # 同 bug#00080：未結算空資料列不比對
        # bug#00097：殘差改用扣除 delta/gamma(凸性)/theta(時間流逝)/DTE 的重定價分解，
        # 不再只扣 delta×ΔS——排除單純由物理凸性與時間流逝造成的偏差。
        dec = _repricing_decomp(c0, c1, d0, d1, spot0, spot1, r)
        if dec is None:
            continue
        resid = dec["residual"]
        oi = c1.get("openInterest") or c0.get("openInterest")
        w = float(oi) if (oi and oi > 0) else 1.0   # 缺 OI 退回等權 1（不臆造大權重）
        if c0.get("type") == "call":
            num += resid * w
            num_call += resid * w
            den_call += w
        else:
            num -= resid * w
            num_put += resid * w
            den_put += w
        den += w
        n += 1
    if n == 0 or den <= 0:
        return None, 0, None, None
    bias = num / den                                    # 每股、OI 加權、已帶方向（買+賣−）
    call_resid = (num_call / den_call) if den_call else None
    put_resid = (num_put / den_put) if den_put else None
    return bias, n, call_resid, put_resid


def compute_directional_verdicts(
    snapshots_by_underlying: dict[str, list[dict]],
    r: float = 0.04,
    window_days: int = 14,
    skew_call_hi: float = 70.0,
    skew_call_lo: float = 30.0,
    bias_min_abs: float = 0.15,
    bias_min_pct: float = 0.03,
    bias_min_n: int = 2,
    as_of: Optional[str] = None,
) -> dict:
    """每檔標的的「綜合方向結論」——分析結論卡與回測共用的唯一判斷邏輯。

    兩條方向性子訊號，各給 +1 / 0 / −1 分後相加：
      1. skew：視窗內新增未平倉集中在買權（call_pct ≥ skew_call_hi）→ +1，
         集中在賣權（≤ skew_call_lo）→ −1。門檻沿用 generate_options_conclusions
         既有的 70/30，資料來源為 compute_options_flow 的 underlying_skew。
      2. 殘差偏向：扣除 delta/gamma(凸性)/theta(時間流逝)/DTE 後的 **OI 加權平均**重定價
         殘差 bias（_residual_bias，bug#00097/00108，每股美元），參與合約數 ≥ bias_min_n
         且 |bias| ≥ 門檻 → ±1。門檻為 max(bias_min_abs, bias_min_pct%×現價)，相對現價
         正規化（bug#00108 #2），使 $600 與 $15 的標的不再共用同一絕對門檻。

    bug#00109（#3）：建倉 skew 由「同側重定價殘差」交叉確認——買權集中但買權淨被壓價
    （多為賣方賣出買權）不確認偏多、賣權集中但賣權淨被壓價（賣出賣權＝偏多）不確認偏空，
    此時 skew 不計入方向並標 skew_unconfirmed=True。

    合計 >0 → 「多」、<0 → 「空」、=0 → None（觀望）；兩訊號同時非零且方向相反時
    另標 conflict=True（矛盾 → 不給方向，比硬給一個方向誠實）。

    walk-forward 安全：以 as_of 限定「當下」可見資料（與 compute_options_flow /
    compute_iv_divergence 同一套視窗機制），回測逐日重算時無前視偏誤。
    回傳 {as_of, window_days, verdicts: {underlying: {...}}, ready_count, total_count}。
    """
    as_of_date = as_of or taiwan_now().strftime("%Y-%m-%d")
    cutoff_date = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=window_days)).strftime("%Y-%m-%d")

    flow = compute_options_flow(snapshots_by_underlying, window_days=window_days, as_of=as_of_date)
    skew_by_u = flow.get("underlying_skew", {})

    verdicts: dict[str, dict] = {}
    for underlying, raw_snaps in snapshots_by_underlying.items():
        snaps = sorted(_filter_window(raw_snaps or [], cutoff_date), key=lambda s: s.get("date", ""))
        ready = len(snaps) >= 2
        v: dict = {
            "ready": ready,
            "days_in_window": len(snaps),
            "first_date": snaps[0]["date"] if snaps else None,
            "last_date": snaps[-1]["date"] if snaps else None,
            "direction": None,
            "conflict": False,
            "skew_score": 0,
            "skew_unconfirmed": False,
            "call_pct": None,
            "bias": None,
            "bias_n": 0,
            "near_earnings": False,
            "earnings_date": None,
        }
        verdicts[underlying] = v
        if not ready:
            continue

        # 子訊號 1：未平倉建倉 skew
        skew = skew_by_u.get(underlying)
        if skew and skew.get("call_pct") is not None:
            v["call_pct"] = skew["call_pct"]
            if skew["call_pct"] >= skew_call_hi:
                v["skew_score"] = 1
            elif skew["call_pct"] <= skew_call_lo:
                v["skew_score"] = -1

        # 子訊號 2：排除股價變動後的淨殘差偏向（bug#00108 #2：OI 加權平均、門檻相對現價）
        earliest, latest = snaps[0], snaps[-1]
        spot1 = latest.get("spot_price")
        bias, bias_n, call_resid, put_resid = _residual_bias(earliest, latest, r)
        v["bias"], v["bias_n"] = (round(bias, 2) if bias is not None else None), bias_n
        bias_score = 0
        if bias is not None and bias_n >= bias_min_n and spot1:
            thr = max(bias_min_abs, bias_min_pct / 100.0 * spot1)
            if bias >= thr:
                bias_score = 1
            elif bias <= -thr:
                bias_score = -1
        v["bias_score"] = bias_score

        # bug#00109（#3）：OI 建倉 skew 無法分辨買方/賣方發起——以「同側重定價殘差」交叉
        # 確認。eps 取現價 0.02% 為雜訊帶；同側殘差明顯為負（該側被壓價）即不確認 skew。
        if spot1:
            eps = 0.0002 * spot1
            if v["skew_score"] > 0 and call_resid is not None and call_resid < -eps:
                v["skew_score"], v["skew_unconfirmed"] = 0, True
            elif v["skew_score"] < 0 and put_resid is not None and put_resid < -eps:
                v["skew_score"], v["skew_unconfirmed"] = 0, True

        total = v["skew_score"] + bias_score
        v["direction"] = "多" if total > 0 else "空" if total < 0 else None
        v["conflict"] = v["skew_score"] * bias_score < 0

        # 財報感知（同 compute_iv_divergence）：區間含財報 → 訊號多屬財報預期反應
        earnings_dates = {s.get("earnings_date") for s in snaps if s.get("earnings_date")}
        win_lo = _date_add(earliest["date"], -2)
        win_hi = _date_add(latest["date"], 3)
        v["earnings_date"] = next(
            (ed for ed in sorted(earnings_dates) if win_lo and win_hi and win_lo <= ed <= win_hi),
            None,
        )
        v["near_earnings"] = v["earnings_date"] is not None

    ready_count = sum(1 for v in verdicts.values() if v["ready"])
    return {
        "as_of": as_of_date,
        "window_days": window_days,
        "verdicts": verdicts,
        "ready_count": ready_count,
        "total_count": len(verdicts),
    }


def _verdict_backtest_note(direction: str, backtest: Optional[dict]) -> str:
    """把 calibration.backtest_verdicts 的結果接成結論卡上的一句回測提示。
    取該方向樣本數最大的前瞻期（並列時偏好 5 → 10 → 1 日）；樣本為 0 或無回測
    報告時誠實顯示「樣本累積中」，不顯示無意義的命中率。"""
    if not backtest or not backtest.get("by_horizon"):
        return "　▶ 回測：訊號樣本累積中，命中率尚無法估計"
    key_n = "bullish_n" if direction == "多" else "bearish_n"
    key_hit = "bullish_hit_rate" if direction == "多" else "bearish_hit_rate"
    by_h = backtest["by_horizon"]
    prefer = sorted(by_h, key=lambda h: (-(by_h[h][key_n] or 0), {5: 0, 10: 1}.get(h, 2)))
    h = prefer[0]
    st = by_h[h]
    n = st[key_n]
    if not n:
        return "　▶ 回測：訊號樣本累積中，命中率尚無法估計"
    hit = st[key_hit]
    base = st["baseline_up_rate"]
    if base is not None and direction == "空":
        base = 1.0 - base
    edge_s = f"，edge {(hit - base) * 100:+.0f}pp" if (hit is not None and base is not None) else ""
    note = f"　▶ 回測：前瞻{h}日同向訊號命中率 {hit * 100:.0f}%（n={n}{edge_s}）"
    # bug#00094: 附上 Wilson 信賴區間 + 對基準的二項檢定顯著性（分辨真 edge 與運氣）
    from .backtest_stats import significance_phrase
    note += significance_phrase(backtest, h, "up" if direction == "多" else "down")
    if n < max(5, backtest.get("min_signals", 20) // 2):
        note += "　⚠️ 樣本不足僅供參考"
    return note


def generate_verdict_cards(
    verdict_report: dict,
    backtest: Optional[dict] = None,
    positions=None,
    iv_pct_by_underlying: Optional[dict] = None,
    top_n: Optional[int] = None,
    include_neutral: bool = False,
) -> list[str]:
    """把 compute_directional_verdicts 的結果轉成「分析結論卡」bullet（每檔標的一則）。

    每則含：方向結論、判斷依據（skew 占比／淨殘差）、該方向訊號的歷史回測命中率
    （與 calibration.backtest_verdicts 同一套邏輯的 walk-forward 結果）、財報降權
    註記、與使用者部位方向的一致性提示、IV 位階建議。include_neutral=True 時，
    已就緒但無方向的標的也會列出「觀望」卡（含矛盾原因），供完整頁面使用；
    Dashboard 卡片取 False 只列有方向者。空 list 代表資料仍在累積。
    """
    from .shared import position_stance_by_symbol
    stance = position_stance_by_symbol(positions) if positions else {}
    from .backtest_stats import find_best_horizon_confidence, has_backtest_evidence

    items = [
        (u, v) for u, v in verdict_report.get("verdicts", {}).items()
        if v["ready"] and (v["direction"] is not None or include_neutral)
    ]
    # 有方向者排前，依證據強度（|淨殘差| + skew 傾斜度）遞減
    def _strength(v: dict) -> float:
        s = abs(v["bias"] or 0.0)
        if v["call_pct"] is not None:
            s += abs(v["call_pct"] - 50.0) / 100.0
        return s
    items.sort(key=lambda kv: (kv[1]["direction"] is None, -_strength(kv[1])))

    bullets: list[str] = []
    for u, v in items[: top_n if top_n is not None else len(items)]:
        basis = []
        if v["skew_score"] != 0 and v["call_pct"] is not None:
            side = "買權" if v["skew_score"] > 0 else "賣權"
            pct = v["call_pct"] if v["skew_score"] > 0 else 100.0 - v["call_pct"]
            basis.append(f"新增未平倉 Dollar Delta OI {pct:.0f}% 集中{side}")
        if v.get("bias_score", 0) != 0 and v["bias"] is not None:
            basis.append(f"排除股價變動後 OI 加權殘差 ${v['bias']:+.2f}/股（{v['bias_n']} 張）")

        # bug#00125: 這條路徑舊版**完全沒有信心門檻**，等於保留了修正前的行為。目前它
        # 只被 generate_analysis_card 使用、TUI 未接線，但兩份並存的 verdict 呈現若有一
        # 份沒守門，日後接回去就會把修好的 bug 原封不動帶回來。改為共用同一個守門。
        _dk = "up" if v["direction"] == "多" else "down"
        _cb = find_best_horizon_confidence(backtest, _dk, horizons=VERDICT_HORIZONS) if (
            backtest and v["direction"] is not None) else {}
        _gated = v["direction"] is not None and (
            has_backtest_evidence(_cb) and _cb.get("meets_threshold"))

        if _gated:
            mark = "🟢 看多" if v["direction"] == "多" else "🔴 看空"
            line = f"{mark} {u}：{'；'.join(basis)}（{v['first_date']}～{v['last_date']}）"
            line += _verdict_backtest_note(v["direction"], backtest)
            if v.get("skew_unconfirmed"):
                line += "　▶ ⓘ 建倉 skew 未獲同側重定價確認，已不計入方向"
            if v["near_earnings"]:
                line += f"　▶ ⚠️ 區間含財報（{v['earnings_date']}），多屬財報預期反應，降權看待"
            line += _stance_note(u, v["direction"], stance)
            line += _iv_percentile_note(u, iv_pct_by_underlying)
        else:
            if v["direction"] is not None and not has_backtest_evidence(_cb):
                reason = f"訊號指向{v['direction']}，但尚無前瞻樣本可回測驗證，不給方向"
            elif v["direction"] is not None:
                reason = (f"最高信心水準僅 {_cb['confidence_str']}（未達 60% 門檻），不給方向")
            elif v.get("skew_unconfirmed"):
                reason = "建倉 skew 未獲同側重定價確認（疑為賣方發起），不給方向"
            elif v["conflict"]:
                reason = f"skew 與殘差方向相反（{'；'.join(basis) if basis else '互相抵銷'}），不給方向"
            else:
                reason = "無足夠方向訊號"
            line = f"⚪ 觀望 {u}：{reason}"
        bullets.append(line)

    return bullets
