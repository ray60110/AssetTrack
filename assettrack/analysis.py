"""
assettrack/analysis.py — 進階分析：主動式 ETF 跨基金持股趨勢共識（離線運算）

bug#00060: 使用者要求系統能「離線」自動整理主動式 ETF 的買賣趨勢，並回報有多少
比例的 ETF 呈現相似趨勢、以及該趨勢對應的總股數。

bug#00061: 延伸為「多數性」（數個 ETF 同時間區間同向買賣）與「規模性」（單一或多數
ETF 購入大量市值部位）雙維度結論，供 Dashboard 首頁卡片與 ETF 頁面共用。

這個模組不打任何網路請求 —— 純粹讀取 `storage.py` 已經在背景刷新時逐日真實累積
下來的每檔 ETF 持股快照（`load_etf_daily_snapshots`），在本機離線運算完成。

**沒有真實快照就沒有趨勢**：不會、也不能對缺資料的天數做任何估計或回填。一檔 ETF
必須在指定視窗（預設 60 天）內至少有兩筆「真實」記錄的快照，才會被納入計算；
不足的 ETF 會被列在 `etf_coverage` 裡標示 `ready=False`，供畫面顯示「資料收集中」。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from .quotes import estimate_shares


def _filter_window(snapshots: list[dict], cutoff_date: str) -> list[dict]:
    return [s for s in snapshots if s.get("date", "") >= cutoff_date]


def compute_symbol_trends(
    snapshots_by_etf: dict[str, list[dict]],
    window_days: int = 60,
    flat_threshold_pp: float = 0.5,
    consensus_threshold: float = 0.5,
    as_of: Optional[str] = None,
) -> dict:
    """Compute cross-ETF holding-weight trend consensus from real daily snapshots.

    snapshots_by_etf: {etf_symbol: [{"date": "YYYY-MM-DD", "aum": float|None,
                       "holdings": [{"symbol": str, "weight": float}, ...]}, ...]}
                       (as returned by storage.load_etf_daily_snapshots; order doesn't
                       matter, this function re-sorts and re-filters defensively.)

    For each ETF with >= 2 real snapshots inside the trailing `window_days`, compares
    its earliest vs. latest snapshot in that window. For every holding symbol seen in
    either snapshot (a symbol dropping out of the top list between snapshots is
    treated as its weight going to 0 — a real observed signal, not a guess), the
    direction is classified using **two independent real signals that must agree**
    (bug#00061 follow-up — a user-requested fix for a representativeness gap):

      - share_dir:  sign of the real share-count delta (shares1 - shares0), each
                    computed from real AUM x weight / real holding price at that
                    snapshot's date (quotes.estimate_shares). None when either
                    snapshot lacks a real price for that holding (e.g. snapshots
                    recorded before this feature started capturing prices).
      - weight_dir: sign of the raw holding-weight delta (w1 - w0), thresholded by
                    flat_threshold_pp (percentage points) — this is the OLD, sole
                    signal used before this fix.

    A symbol is only "up" if share_dir == "up" AND weight_dir == "up" (real shares
    increased AND its proportion of the fund increased); only "down" if both agree
    downward. Every other case — including when only one signal moved, they
    disagree, or the real-price data needed for share_dir isn't available yet —
    is classified "flat" and excluded from consensus/scale ranking.

    Why: weight_dir alone can't tell a real purchase from a stock simply rallying
    while the fund does nothing (rising price mechanically raises that holding's
    weight with zero trading). Requiring share_dir to agree filters that out,
    since share_dir is computed from real per-date prices and stays flat when the
    real share count didn't change, even if price and weight both moved.

    Then, across all ready ETFs, each held symbol gets a cross-ETF consensus (「多數
    性」): the fraction of ETFs (that actually hold/held it) moving the same
    direction. A symbol only gets a "up"/"down" consensus label when that fraction
    is >= consensus_threshold (default 50%); otherwise it's "mixed".

    Two dollar/share estimates are computed per (etf, symbol) contribution:
      - value_delta: aum_latest*(w1/100) - aum_earliest*(w0/100) — a direct real
        dollar estimate, the primary basis for 「規模性」 (scale) ranking. Still
        includes price-return effects (it's a raw dollar-exposure delta), but
        `direction` filtering above already keeps price-only drift out of what
        gets reported as a "move" in the first place.
      - share_delta: real share-count delta as described above (None when a real
        price wasn't available for either snapshot — never fabricated/guessed).

    Returns a dict with `symbols` (multiplicity-oriented, see rank_symbol_trends)
    and `raw_contributions` (every individual etf/symbol up|down event with its
    value_delta, unfiltered by consensus — see rank_scale_events for the
    single-or-multi-fund "large position" view).
    """
    as_of_date = as_of or datetime.utcnow().strftime("%Y-%m-%d")
    cutoff_date = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=window_days)).strftime("%Y-%m-%d")

    etf_coverage: dict[str, dict] = {}
    all_contributions: list[dict] = []

    for etf_sym, raw_snaps in snapshots_by_etf.items():
        snaps = sorted(_filter_window(raw_snaps or [], cutoff_date), key=lambda s: s.get("date", ""))
        days_in_window = len(snaps)
        ready = days_in_window >= 2
        etf_coverage[etf_sym] = {
            "days_in_window": days_in_window,
            "first_date": snaps[0]["date"] if snaps else None,
            "last_date": snaps[-1]["date"] if snaps else None,
            "ready": ready,
        }
        if not ready:
            continue

        earliest, latest = snaps[0], snaps[-1]
        early_w = {h["symbol"]: h.get("weight", 0.0) for h in earliest.get("holdings", [])}
        late_w = {h["symbol"]: h.get("weight", 0.0) for h in latest.get("holdings", [])}
        early_price = {h["symbol"]: h.get("price") for h in earliest.get("holdings", [])}
        late_price = {h["symbol"]: h.get("price") for h in latest.get("holdings", [])}
        early_aum = earliest.get("aum")
        late_aum = latest.get("aum")

        for sym in set(early_w) | set(late_w):
            w0 = early_w.get(sym, 0.0)
            w1 = late_w.get(sym, 0.0)
            weight_delta = w1 - w0

            if weight_delta > flat_threshold_pp:
                weight_dir = "up"
            elif weight_delta < -flat_threshold_pp:
                weight_dir = "down"
            else:
                weight_dir = "flat"

            shares0 = estimate_shares(sym, w0, early_aum, early_price.get(sym)) if w0 else None
            shares1 = estimate_shares(sym, w1, late_aum, late_price.get(sym)) if w1 else None
            share_delta = (shares1 - shares0) if (shares0 is not None and shares1 is not None) else None

            if share_delta is None:
                share_dir = None
            elif share_delta > 0:
                share_dir = "up"
            elif share_delta < 0:
                share_dir = "down"
            else:
                share_dir = "flat"

            # bug#00061 follow-up (user decision): only count a move as real
            # accumulation/reduction when the real share-count signal AND the
            # weight/AUM-proportion signal agree — a symbol rallying in price
            # with zero trading would otherwise show up as "up" on weight_dir
            # alone. Any disagreement, or missing share_dir (no real price yet),
            # is "flat" — excluded from consensus/scale ranking rather than guessed.
            if share_dir == "up" and weight_dir == "up":
                direction = "up"
            elif share_dir == "down" and weight_dir == "down":
                direction = "down"
            else:
                direction = "flat"

            value_delta = None
            if early_aum is not None and late_aum is not None:
                value_delta = late_aum * (w1 / 100.0) - early_aum * (w0 / 100.0)

            all_contributions.append({
                "etf": etf_sym,
                "symbol": sym,
                "direction": direction,
                "weight_delta": round(weight_delta, 4),
                "share_delta": share_delta,
                "value_delta": value_delta,
                "aum_latest": late_aum,
                "first_date": earliest["date"],
                "last_date": latest["date"],
            })

    etfs_ready = [e for e, c in etf_coverage.items() if c["ready"]]

    # ── 多數性 (multiplicity): aggregate per held symbol across ETFs ───────────
    by_symbol: dict[str, list[dict]] = {}
    for c in all_contributions:
        by_symbol.setdefault(c["symbol"], []).append(c)

    symbols_report: dict[str, dict] = {}
    for sym, contribs in by_symbol.items():
        etfs_up = [c["etf"] for c in contribs if c["direction"] == "up"]
        etfs_down = [c["etf"] for c in contribs if c["direction"] == "down"]
        etfs_flat = [c["etf"] for c in contribs if c["direction"] == "flat"]
        evaluated = len(contribs)
        if evaluated == 0:
            continue

        pct_up = len(etfs_up) / evaluated
        pct_down = len(etfs_down) / evaluated

        if pct_up >= consensus_threshold and pct_up >= pct_down:
            consensus, consensus_pct, consensus_etfs = "up", pct_up, etfs_up
        elif pct_down >= consensus_threshold:
            consensus, consensus_pct, consensus_etfs = "down", pct_down, etfs_down
        else:
            consensus, consensus_pct, consensus_etfs = "mixed", max(pct_up, pct_down), []

        est_total_share_delta = None
        est_total_value_delta = None
        if consensus in ("up", "down"):
            sd = [c["share_delta"] for c in contribs if c["etf"] in consensus_etfs and c["share_delta"] is not None]
            vd = [c["value_delta"] for c in contribs if c["etf"] in consensus_etfs and c["value_delta"] is not None]
            if sd:
                est_total_share_delta = int(sum(abs(d) for d in sd))
            if vd:
                est_total_value_delta = sum(abs(d) for d in vd)

        symbols_report[sym] = {
            "etfs_up": etfs_up,
            "etfs_down": etfs_down,
            "etfs_flat": etfs_flat,
            "etfs_evaluated": evaluated,
            "pct_up": round(pct_up * 100, 1),
            "pct_down": round(pct_down * 100, 1),
            "consensus": consensus,
            "consensus_pct": round(consensus_pct * 100, 1),
            "est_total_share_delta": est_total_share_delta,
            "est_total_value_delta": est_total_value_delta,
        }

    return {
        "window_days": window_days,
        "as_of": as_of_date,
        "etf_coverage": etf_coverage,
        "etfs_ready_count": len(etfs_ready),
        "etfs_total_count": len(etf_coverage),
        "etfs_ready_pct": round(len(etfs_ready) / len(etf_coverage) * 100, 1) if etf_coverage else 0.0,
        "symbols": symbols_report,
        "raw_contributions": all_contributions,
    }


def rank_symbol_trends(
    report: dict,
    min_etfs_evaluated: int = 4,
    top_n: int = 20,
) -> list[tuple[str, dict]]:
    """Rank held symbols by cross-ETF consensus strength (「多數性」) for display.

    Only includes symbols evaluated across >= min_etfs_evaluated ETFs (min raised
    to 4 per review: with the old default of 2, a single ETF nudging its weight
    up while a second ETF stayed flat already cleared the 50% consensus_threshold
    and got reported as a "multiplicity" signal despite only one fund actually
    having moved — not a real cross-fund consensus). Sorted
    by consensus_pct desc, then by how many ETFs contributed (more evidence first).
    Symbols with consensus == "mixed" are sorted to the bottom.
    """
    items = [
        (sym, info) for sym, info in report.get("symbols", {}).items()
        if info["etfs_evaluated"] >= min_etfs_evaluated
    ]
    items.sort(
        key=lambda kv: (
            kv[1]["consensus"] != "mixed",
            kv[1]["consensus_pct"],
            kv[1]["etfs_evaluated"],
        ),
        reverse=True,
    )
    return items[:top_n]


def rank_scale_events(
    report: dict,
    top_n: int = 15,
    min_abs_value: float = 5_000_000.0,
    min_relative_to_aum: float = 0.005,
) -> list[dict]:
    """Rank individual etf/symbol moves by real dollar scale (「規模性」) —
    deliberately NOT filtered by cross-ETF consensus, so a single fund making one
    outsized move (e.g. one ETF putting $150M into a name) surfaces here even
    though it's just one data point, not a multi-ETF pattern.

    A move only counts as "significant scale" when its value_delta clears BOTH:
      - an absolute floor (default $5M — filters out noise in small funds), and
      - a floor relative to that ETF's own AUM (default 0.5% — so a $5M move in a
        $200M fund counts, but the same $5M in a $50B fund doesn't get flagged as
        "large" for that fund).
    Both are real, disclosed figures (AUM x weight) — nothing here is estimated
    beyond the same AUM x weight approximation already used across this app.
    """
    events = []
    for c in report.get("raw_contributions", []):
        if c["direction"] == "flat" or c["value_delta"] is None:
            continue
        aum = c.get("aum_latest")
        rel_floor = (aum * min_relative_to_aum) if aum else min_abs_value
        threshold = max(min_abs_value, rel_floor)
        if abs(c["value_delta"]) >= threshold:
            events.append(c)

    events.sort(key=lambda c: abs(c["value_delta"]), reverse=True)
    return events[:top_n]


def _fmt_usd(v: float) -> str:
    av = abs(v)
    if av >= 1e9:
        return f"${av / 1e9:.2f}B"
    if av >= 1e6:
        return f"${av / 1e6:.1f}M"
    if av >= 1e3:
        return f"${av / 1e3:.0f}K"
    return f"${av:,.0f}"


def generate_etf_conclusions(
    report: dict,
    min_etfs_evaluated: int = 4,
    top_n: int = 5,
    positions=None,
) -> list[str]:
    """Turn the multiplicity (rank_symbol_trends) and scale (rank_scale_events)
    rankings into short, readable Chinese conclusion bullets — the actual text
    shown on the Dashboard card and the ETF page's 「結論」 section.

    Returns an empty list when there's genuinely nothing to report yet (not
    enough real accumulated days) — callers should show an honest "資料收集中"
    state rather than treat an empty list as an error.

    When `positions` is given, each consensus symbol is cross-referenced with the
    user's own holdings (see shared.position_stance_by_symbol) to append a
    constructive note — 你已持有且趨勢一致 / 你已持有但主動式ETF在減碼 / 你尚未
    持有可留意進場 — turning a bare description into actionable advice.
    """
    from .shared import position_stance_by_symbol
    stance = position_stance_by_symbol(positions) if positions else {}

    bullets: list[str] = []

    multi = [
        (sym, info) for sym, info in rank_symbol_trends(report, min_etfs_evaluated=min_etfs_evaluated, top_n=top_n)
        if info["consensus"] in ("up", "down")
    ]
    for sym, info in multi:
        up = info["consensus"] == "up"
        verb = "增碼" if up else "減碼"
        n = len(info["etfs_up"] if up else info["etfs_down"])
        value_s = f"，估計合計{'加碼' if up else '減碼'}約 {_fmt_usd(info['est_total_value_delta'])}" \
            if info.get("est_total_value_delta") else ""
        note = ""
        if stance:
            held = stance.get(sym.upper())  # '多' / '空' / '混合' / None
            signal_dir = "多" if up else "空"  # 增碼=偏多訊號，減碼=偏空訊號
            if held == "混合":
                note = "　▶ 你在此標的多空部位並存"
            elif held is not None:
                note = (
                    f"　▶ 與你目前偏{held}的部位方向一致" if held == signal_dir
                    else f"　▶ ⚠️ 與你目前偏{held}的部位方向相反，留意是否調節"
                )
            elif up:
                note = "　▶ 你尚未持有，可留意是否符合進場條件"
        bullets.append(
            f"📊 【多數性】{sym}：{n}/{info['etfs_evaluated']} 檔追蹤中的主動式ETF同步{verb}"
            f"（{info['consensus_pct']:.0f}% 一致）{value_s}{note}"
        )

    for c in rank_scale_events(report, top_n=top_n):
        verb = "大幅加碼" if c["direction"] == "up" else "大幅減碼"
        bullets.append(
            f"💰 【規模性】{c['etf']} 於 {c['first_date']}～{c['last_date']} 期間"
            f"{verb} {c['symbol']}，估計市值變化約 {_fmt_usd(c['value_delta'])}"
        )

    return bullets
