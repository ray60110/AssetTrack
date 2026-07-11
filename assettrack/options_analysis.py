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
from typing import Optional


def _filter_window(snapshots: list[dict], cutoff_date: str) -> list[dict]:
    return [s for s in snapshots if s.get("date", "") >= cutoff_date]


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
    as_of_date = as_of or datetime.utcnow().strftime("%Y-%m-%d")
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

        call_oi_buildup = 0.0
        put_oi_buildup = 0.0

        for csym in matched_symbols:
            c0, c1 = early_by_sym[csym], late_by_sym[csym]
            oi0, oi1 = c0.get("openInterest"), c1.get("openInterest")
            p0, p1 = c0.get("lastPrice"), c1.get("lastPrice")

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
                if c1.get("type") == "call":
                    call_oi_buildup += oi_delta
                elif c1.get("type") == "put":
                    put_oi_buildup += oi_delta

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

        if call_oi_buildup or put_oi_buildup:
            total = call_oi_buildup + put_oi_buildup
            underlying_skew[underlying] = {
                "call_oi_buildup": int(call_oi_buildup),
                "put_oi_buildup": int(put_oi_buildup),
                "call_pct": round(call_oi_buildup / total * 100, 1) if total else 0.0,
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


def generate_options_conclusions(report: dict, top_n: int = 5, positions=None) -> list[str]:
    """Turn compute_options_flow()'s output into short Chinese conclusion bullets
    for the Dashboard card and the options watchlist screen's 結論 section.
    Empty list means genuinely nothing to report yet (not enough real accumulated
    days) — callers should show an honest "資料收集中" state, not an error.

    When `positions` is given, each per-underlying skew signal is cross-referenced
    with the user's own net stance on that underlying (see
    shared.position_stance_by_symbol) to append a constructive note —
    aligned / opposite (risk) / not yet held — instead of a bare description.
    """
    from .shared import position_stance_by_symbol
    stance = position_stance_by_symbol(positions) if positions else {}

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

    # Bullish/bearish skew per underlying (only meaningfully lopsided ones)
    skew_items = sorted(
        report.get("underlying_skew", {}).items(),
        key=lambda kv: abs(kv[1]["call_oi_buildup"] - kv[1]["put_oi_buildup"]),
        reverse=True,
    )
    for underlying, s in skew_items[:top_n]:
        signal_dir = "多" if s["call_pct"] >= 70 else "空" if s["call_pct"] <= 30 else None
        if signal_dir is None:
            continue
        if signal_dir == "多":
            base = (
                f"📈 {underlying}：近期新增未平倉集中在買權（{s['call_oi_buildup']:,} 口 vs "
                f"賣權 {s['put_oi_buildup']:,} 口），資金偏多關注"
            )
        else:
            base = (
                f"📉 {underlying}：近期新增未平倉集中在賣權（{s['put_oi_buildup']:,} 口 vs "
                f"買權 {s['call_oi_buildup']:,} 口），資金偏空/避險關注"
            )
        # 未平倉變化無法區分開倉/平倉、買方/賣方，方向僅供參考
        base += "（註：未平倉增減無法區分開/平倉，方向僅供參考）"
        bullets.append(base + _stance_note(underlying, signal_dir, stance))

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
