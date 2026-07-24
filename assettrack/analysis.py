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
from .storage import taiwan_now


def _filter_window(snapshots: list[dict], cutoff_date: str) -> list[dict]:
    return [s for s in snapshots if s.get("date", "") >= cutoff_date]


def _median(xs: list) -> Optional[float]:
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if n == 0:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def _endpoint_view(snaps_subset: list[dict]) -> "tuple[dict, dict, Optional[float]]":
    """bug#00110（使用者審查 #4）：把視窗一端的數筆快照聚合成穩健代表值——每檔持股
    的權重／價格取該端各快照的中位數、AUM 取中位數，避免單一異常端點快照翻轉整個
    方向訊號。缺真實值者不臆造（僅對有值者取中位數）。"""
    weights: dict[str, list] = {}
    prices: dict[str, list] = {}
    aums: list = []
    for s in snaps_subset:
        a = s.get("aum")
        if a is not None:
            aums.append(a)
        for h in s.get("holdings", []) or []:
            sym = h.get("symbol")
            if sym is None:
                continue
            w = h.get("weight", 0.0)
            if w is not None:
                weights.setdefault(sym, []).append(w)
            p = h.get("price")
            if p is not None:
                prices.setdefault(sym, []).append(p)
    return ({k: _median(v) for k, v in weights.items()},
            {k: _median(v) for k, v in prices.items()},
            _median(aums))


def compute_symbol_trends(
    snapshots_by_etf: dict[str, list[dict]],
    window_days: int = 14,
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
    as_of_date = as_of or taiwan_now().strftime("%Y-%m-%d")
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
        # bug#00110（使用者審查 #4）：不再用單一頭尾快照，改取視窗兩端各 k 筆的中位數
        # 代表值（k = min(3, len//2)，兩端不重疊；只有 2 筆時退化為原本的兩點比較），
        # 降低單一異常端點快照翻轉整個方向訊號的脆弱度。日期標籤仍取真實頭尾（span）。
        k = max(1, min(3, len(snaps) // 2))
        early_w, early_price, early_aum = _endpoint_view(snaps[:k])
        late_w, late_price, late_aum = _endpoint_view(snaps[-k:])

        for sym in set(early_w) | set(late_w):
            w0 = early_w.get(sym) or 0.0
            w1 = late_w.get(sym) or 0.0
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

        # bug#00107（使用者審查 #1）：需嚴格多於反向才算共識。舊版 `pct_up >= pct_down`
        # 讓 2 上 2 下的平手一律判為「up」，注入系統性多頭偏誤並經跨模型放大；平手
        # （pct_up == pct_down）現一律歸 mixed，多空對稱處理。
        if pct_up >= consensus_threshold and pct_up > pct_down:
            consensus, consensus_pct, consensus_etfs = "up", pct_up, etfs_up
        elif pct_down >= consensus_threshold and pct_down > pct_up:
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

    ac_trends = compute_asset_class_trends(
        snapshots_by_etf, window_days=window_days,
        flat_threshold_pp=flat_threshold_pp,
        consensus_threshold=consensus_threshold, as_of=as_of_date,
    )

    return {
        "window_days": window_days,
        "as_of": as_of_date,
        "etf_coverage": etf_coverage,
        "etfs_ready_count": len(etfs_ready),
        "etfs_total_count": len(etf_coverage),
        "etfs_ready_pct": round(len(etfs_ready) / len(etf_coverage) * 100, 1) if etf_coverage else 0.0,
        "symbols": symbols_report,
        "raw_contributions": all_contributions,
        "asset_classes": ac_trends,
    }


_ASSET_CLASS_NAMES = {
    "stock": "股票 (Stock)",
    "bond": "債券 (Bond)",
    "cash": "現金 (Cash)",
    "preferred": "特別股 (Preferred)",
    "convertible": "可轉債 (Convertible)",
    "other": "黃金/大宗商品/其他 (Gold/Commodities/Other)",
}


def compute_asset_class_trends(
    snapshots_by_etf: dict[str, list[dict]],
    window_days: int = 14,
    flat_threshold_pp: float = 0.5,
    consensus_threshold: float = 0.5,
    as_of: Optional[str] = None,
) -> dict:
    """Compute cross-ETF asset class allocation trends (Stock, Bond, Cash, Gold/Commodities/Other)
    over trailing window_days from real daily snapshots (bug#00103).

    Compares earliest vs latest snapshot's asset_classes breakdown for each ready ETF.
    Identifies cross-ETF majority consensus for broad asset allocation shifts.
    """
    as_of_date = as_of or taiwan_now().strftime("%Y-%m-%d")
    cutoff_date = (datetime.strptime(as_of_date, "%Y-%m-%d") - timedelta(days=window_days)).strftime("%Y-%m-%d")

    by_class: dict[str, list[dict]] = {}
    etfs_ready = 0

    for etf_sym, raw_snaps in snapshots_by_etf.items():
        snaps = sorted(_filter_window(raw_snaps or [], cutoff_date), key=lambda s: s.get("date", ""))
        if len(snaps) < 2:
            continue
        earliest, latest = snaps[0], snaps[-1]
        early_ac = earliest.get("asset_classes") or {}
        late_ac = latest.get("asset_classes") or {}
        if not early_ac and not late_ac:
            continue

        etfs_ready += 1
        all_keys = set(early_ac) | set(late_ac)
        for cls_key in all_keys:
            w0 = float(early_ac.get(cls_key, 0.0) or 0.0)
            w1 = float(late_ac.get(cls_key, 0.0) or 0.0)
            delta = w1 - w0
            if delta > flat_threshold_pp:
                direction = "up"
            elif delta < -flat_threshold_pp:
                direction = "down"
            else:
                direction = "flat"

            by_class.setdefault(cls_key, []).append({
                "etf": etf_sym,
                "direction": direction,
                "delta_pp": delta,
            })

    classes_report: dict[str, dict] = {}
    for cls_key, contribs in by_class.items():
        evaluated = len(contribs)
        if evaluated == 0:
            continue
        up_contribs = [c for c in contribs if c["direction"] == "up"]
        down_contribs = [c for c in contribs if c["direction"] == "down"]
        pct_up = len(up_contribs) / evaluated
        pct_down = len(down_contribs) / evaluated

        # bug#00107（使用者審查 #1）：同 compute_symbol_trends——平手須歸 mixed，不偏多。
        if pct_up >= consensus_threshold and pct_up > pct_down:
            consensus, consensus_pct = "up", pct_up
        elif pct_down >= consensus_threshold and pct_down > pct_up:
            consensus, consensus_pct = "down", pct_down
        else:
            consensus, consensus_pct = "mixed", max(pct_up, pct_down)

        avg_delta = sum(c["delta_pp"] for c in contribs) / evaluated
        classes_report[cls_key] = {
            "name": _ASSET_CLASS_NAMES.get(cls_key, cls_key),
            "etfs_up": [c["etf"] for c in up_contribs],
            "etfs_down": [c["etf"] for c in down_contribs],
            "evaluated": evaluated,
            "pct_up": round(pct_up * 100, 1),
            "pct_down": round(pct_down * 100, 1),
            "consensus": consensus,
            "consensus_pct": round(consensus_pct * 100, 1),
            "avg_delta_pp": round(avg_delta, 2),
        }

    return {"etfs_ready": etfs_ready, "classes": classes_report}


def rank_symbol_trends(
    report: dict,
    min_etfs_evaluated: int = 4,
    top_n: int = 20,
) -> list[tuple[str, dict]]:
    """Rank held symbols by cross-ETF consensus strength (「多數性」) for display."""
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
    """Rank individual etf/symbol moves by real dollar scale (「規模性」)."""
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


def _etf_backtest_section(direction: str, backtest: "Optional[dict]"):
    """把 etf_backtest_note() 的回測命中率結論收成第三層 breakdown section（bug#00117）。
    以同一份 note 為 substitution，維持單一真理來源、不另重算統計。"""
    from .shared import _section
    note = etf_backtest_note(direction, backtest).replace("　▶ 回測：", "").strip()
    return _section(
        "回測驗證（walk-forward 命中率）",
        formula="命中率 = 訊號後前瞻 h 日方向正確次數 ÷ 可評估訊號數；超額 edge = 命中率 − 同宇集基準上漲率",
        substitution=note,
        explanation=("回測呼叫與畫面同一判斷函式（compute_symbol_trends），每個歷史日只餵 ≤T 的真實快照、"
                     "結構上無前視偏誤；顯著性經 Wilson CI＋對基準單尾二項檢定（以 ESS 消重疊視窗自相關、"
                     "Bonferroni 多重比較調整）。可評估訊號 < 20 時誠實標『資料累積中』。"))


def _etf_stance_section(sym: str, up: bool, stance: dict):
    """與使用者部位方向一致性的第三層 section（bug#00117）。無部位資料則回 None。"""
    from .shared import _section
    if not stance:
        return None
    held = stance.get(sym.upper())
    signal_dir = "多" if up else "空"
    if held == "混合":
        note = "你在此標的多空部位並存。"
    elif held is not None:
        note = (f"與你目前偏{held}的部位方向一致。" if held == signal_dir
                else f"⚠️ 與你目前偏{held}的部位方向相反，留意是否調節。")
    elif up:
        note = "你尚未持有此標的，可留意是否符合進場條件。"
    else:
        return None
    return _section("與你的部位比對", substitution=note,
                    explanation="以你目前持倉的淨多空立場（position_stance_by_symbol）與此訊號方向交叉比對，作為建設性提示，非加減碼指令。")


def generate_etf_recommendations(
    report: dict,
    min_etfs_evaluated: int = 4,
    top_n: int = 5,
    positions=None,
    backtest: "Optional[dict]" = None,
) -> "list":
    """把多數性／大類輪動／規模性三段結論組成三層結構化建議（bug#00117）。
    第一層＝方向結論；第二層＝如何判斷（多數性/雙真實訊號同向）；第三層＝共識公式＋
    帶入本標的數字＋回測＋部位一致性。generate_etf_conclusions 為其薄 wrapper。"""
    from .shared import Recommendation, _section, position_stance_by_symbol
    stance = position_stance_by_symbol(positions) if positions else {}
    window = report.get("window_days", 14)
    recs: list = []

    # 1. 大類資產輪動共識 (Asset-Class Allocation Consensus)
    ac_data = (report.get("asset_classes") or {}).get("classes", {})
    for cls_key, info in ac_data.items():
        if info.get("consensus") in ("up", "down"):
            up = info["consensus"] == "up"
            verb = "增碼" if up else "減碼"
            n = len(info["etfs_up"] if up else info["etfs_down"])
            action_desc = "機構資金轉向風險配置/加碼" if (up and cls_key == "stock") else "機構資金防守/轉向避險資產" if (up and cls_key in ("cash", "bond")) else "機構適度減碼防守性資產" if (not up and cls_key in ("cash", "bond")) else "機構籌碼調整"
            recs.append(Recommendation(
                rec_id=f"etf_ac:{cls_key}", category="etf",
                direction="多" if up else "空",
                verdict=(f"🌐 【大類資產輪動】{info['consensus_pct']:.0f}% 主動式 ETF 同步{verb}"
                         f"「{info['name']}」▶ {action_desc}"),
                basis=(f"近 {window} 天內 {n}/{info['evaluated']} 檔主動式 ETF 同向調整此大類資產配置，"
                       f"達 ≥50% 多數性共識即成立。"),
                detail_sections=[_section(
                    "大類資產輪動共識公式",
                    formula="共識比例 = 同向調整此大類的 ETF 數 ÷ 有評估此大類的 ETF 數；≥ consensus_threshold(0.5) 且方向多於反向才成立",
                    substitution=(f"= {n}/{info['evaluated']} = {info['consensus_pct']:.0f}%　"
                                  f"平均資產配置權重變動 {info['avg_delta_pp']:+.1f}pp（{verb}）"),
                    explanation="讀取各主動式 ETF 每日真實 asset_classes 快照，於 14 天緊湊視窗比較最早 vs 最新配置；平手（多空檔數相等）歸 mixed 不計入，避免系統性多頭偏誤。")],
            ))

    # 2. 個股同時買入/賣出共識 (Symbol Level Buy/Sell Consensus)
    multi = [
        (sym, info) for sym, info in rank_symbol_trends(report, min_etfs_evaluated=min_etfs_evaluated, top_n=top_n)
        if info["consensus"] in ("up", "down")
    ]
    for sym, info in multi:
        up = info["consensus"] == "up"
        emoji = "🟢 【同時買入】" if up else "🔴 【同時賣出】"
        verb = "同步增碼 (買入)" if up else "同步減碼 (賣出)"
        n = len(info["etfs_up"] if up else info["etfs_down"])
        value_s = (f"，估計合計{'加碼' if up else '減碼'}約 {_fmt_usd(info['est_total_value_delta'])}"
                   if info.get("est_total_value_delta") else "")
        secs = [_section(
            "個股多數性共識公式（雙真實訊號同向）",
            formula="共識比例 = 同向 ETF 數 ÷ 評估 ETF 數；需 pct_up > pct_down（平手歸 mixed）；每檔須『真實股數變化 Δ』與『權重變化 Δ（門檻 0.5pp）』同向才計為增/減碼",
            substitution=f"= {n}/{info['etfs_evaluated']} = {info['consensus_pct']:.0f}% 同向{value_s}",
            explanation="真實股數由各快照當日 AUM×權重÷真實持股價反推；兩個獨立真實訊號任一缺席或方向不一致一律歸『持平』、不計入，確保訊號為真實交易而非雜訊。")]
        stance_sec = _etf_stance_section(sym, up, stance)
        if stance_sec:
            secs.append(stance_sec)
        secs.append(_etf_backtest_section("up" if up else "down", backtest))
        recs.append(Recommendation(
            rec_id=f"etf_sym:{sym}", category="etf",
            direction="多" if up else "空",
            verdict=f"{emoji}{sym}：{n}/{info['etfs_evaluated']} 檔追蹤中的主動式 ETF {verb}",
            basis=(f"跨基金多數性共識 {info['consensus_pct']:.0f}% 一致，且每檔皆為真實股數變化與權重變化"
                   f"同向才計入。"),
            detail_sections=secs,
        ))

    # 3. 規模性大額變動
    for c in rank_scale_events(report, top_n=top_n):
        up = c["direction"] == "up"
        verb = "大幅加碼" if up else "大幅減碼"
        recs.append(Recommendation(
            rec_id=f"etf_scale:{c['etf']}:{c['symbol']}", category="etf",
            direction="多" if up else "空",
            verdict=f"💰 【規模性大額變動】{c['etf']} {verb} {c['symbol']}（{c['first_date']}～{c['last_date']}）",
            basis="單一基金的大額真實部位變動——刻意不套用跨基金共識門檻，讓單一大額動作也能被看見。",
            detail_sections=[_section(
                "規模性大額變動門檻",
                formula="計入條件：|市值變化| ≥ $5M 且 |市值變化| ÷ 該基金 AUM ≥ 0.5%（雙重門檻避免小基金雜訊）",
                substitution=f"{c['etf']} 於 {c['first_date']}～{c['last_date']} {verb} {c['symbol']}，估計市值變化約 {_fmt_usd(c['value_delta'])}",
                explanation="以連續真實快照的持股市值差分衍生；規模性與多數性互補——前者看單一大額動作，後者看跨基金一致性。")],
        ))

    return recs


def generate_etf_conclusions(
    report: dict,
    min_etfs_evaluated: int = 4,
    top_n: int = 5,
    positions=None,
    backtest: "Optional[dict]" = None,
) -> list[str]:
    """薄 wrapper（bug#00117）：以 generate_etf_recommendations 為單一真理來源，投影為
    主頁用的「一句話」字串清單（結論＋判斷依據）。畫面完整三層改由 recs 直接渲染。"""
    from .shared import dashboard_line
    return [dashboard_line(r) for r in generate_etf_recommendations(
        report, min_etfs_evaluated=min_etfs_evaluated, top_n=top_n,
        positions=positions, backtest=backtest)]


# ─────────────────────────────────────────────────────────────────────────────
# Per-ETF active stock-selection tilt + daily cross-fund breadth stance
# ─────────────────────────────────────────────────────────────────────────────
# 使用者需求（2026-07）：「透過各 ETF 主動選股，觀察出趨勢並且 by daily check 顯示
# 多空建議」。現行 compute_symbol_trends 只有「個股跨基金共識」與「跨模型整體」兩端，
# 缺少中間層——每一檔 ETF 自己這段視窗在主動加/減什麼、淨傾向偏多還偏空。此層補上
# 該視圖，並把各檔傾向聚合成一個「每日主動選股多空廣度」讀數。
#
# 紀律不變：不重算、不打網路——完全從 compute_symbol_trends() 已算好的同一份 report
# 的 raw_contributions（雙真實訊號同向的個股加/減碼事件）衍生，單一真理來源。


def compute_etf_selection_tilt(
    report: dict,
    tilt_min_net: float = 0.1,
    stance_breadth_min: float = 0.2,
) -> dict:
    """Per-ETF active stock-selection tilt + daily cross-fund breadth stance,
    derived from the SAME report that compute_symbol_trends() returns (single
    source of truth; no recompute, no network).

    「主動選股」= the dual-signal (real share-count delta AND weight delta must
    agree) individual-holding accumulate/reduce events already present in
    report['raw_contributions']. For each ETF we net its up vs down holdings:

      net_score = (up_n - down_n) / evaluated        (evaluated incl. flats,
                                                       so noise is diluted, not
                                                       amplified — conservative)

    tilt: 'long' if net_score > tilt_min_net, 'short' if < -tilt_min_net, else
    'neutral'. Aggregate breadth = (etfs_long - etfs_short) / etfs_evaluated,
    mapped to a daily stance ('long'/'short'/'neutral'); 'insufficient' when no
    ETF is ready (honest "資料累積中", never a fabricated direction).
    """
    contribs = report.get("raw_contributions", [])
    by_etf: dict[str, list[dict]] = {}
    for c in contribs:
        by_etf.setdefault(c["etf"], []).append(c)

    etfs: dict[str, dict] = {}
    for etf, cs in by_etf.items():
        ups = [c for c in cs if c["direction"] == "up"]
        downs = [c for c in cs if c["direction"] == "down"]
        flats = [c for c in cs if c["direction"] == "flat"]
        evaluated = len(cs)
        moved = len(ups) + len(downs)
        net_score = ((len(ups) - len(downs)) / evaluated) if evaluated else 0.0
        value_net = sum(
            (c["value_delta"] or 0.0)
            * (1 if c["direction"] == "up" else -1 if c["direction"] == "down" else 0)
            for c in cs
        )
        if moved == 0:
            tilt = "neutral"
        elif net_score > tilt_min_net:
            tilt = "long"
        elif net_score < -tilt_min_net:
            tilt = "short"
        else:
            tilt = "neutral"
        top_buys = sorted(
            [c for c in ups if c["value_delta"] is not None],
            key=lambda c: abs(c["value_delta"]), reverse=True,
        )[:3]
        top_sells = sorted(
            [c for c in downs if c["value_delta"] is not None],
            key=lambda c: abs(c["value_delta"]), reverse=True,
        )[:3]
        etfs[etf] = {
            "up_n": len(ups),
            "down_n": len(downs),
            "flat_n": len(flats),
            "evaluated": evaluated,
            "net_score": round(net_score, 3),
            "value_net": value_net,
            "tilt": tilt,
            "top_buys": [c["symbol"] for c in top_buys],
            "top_sells": [c["symbol"] for c in top_sells],
        }

    evaluated_etfs = [e for e, v in etfs.items() if v["evaluated"] > 0]
    longs = [e for e in evaluated_etfs if etfs[e]["tilt"] == "long"]
    shorts = [e for e in evaluated_etfs if etfs[e]["tilt"] == "short"]
    neutrals = [e for e in evaluated_etfs if etfs[e]["tilt"] == "neutral"]
    n = len(evaluated_etfs)
    breadth = ((len(longs) - len(shorts)) / n) if n else 0.0
    if n == 0:
        stance = "insufficient"
    elif breadth >= stance_breadth_min:
        stance = "long"
    elif breadth <= -stance_breadth_min:
        stance = "short"
    else:
        stance = "neutral"

    return {
        "as_of": report.get("as_of"),
        "window_days": report.get("window_days"),
        "etfs": etfs,
        "aggregate": {
            "etfs_long": len(longs),
            "etfs_short": len(shorts),
            "etfs_neutral": len(neutrals),
            "etfs_evaluated": n,
            "breadth": round(breadth, 3),
            "stance": stance,
        },
    }


def etf_stance_recommendation(tilt: dict, backtest: "Optional[dict]" = None) -> "list":
    """每日主動選股多空廣度 stance 的三層結構化建議（bug#00117）。回傳 list（0 或 1 則），
    與 etf_stance_phrase 共用同一 tilt 輸出，維持首頁卡片與分析框讀數一致。"""
    from .shared import Recommendation, _section
    agg = (tilt or {}).get("aggregate", {})
    n = agg.get("etfs_evaluated", 0)
    st = agg.get("stance")
    if not n or st == "insufficient":
        return [Recommendation(
            rec_id="etf_stance", category="etf_stance", direction=None,
            verdict="📊 每日主動選股多空：資料累積中（就緒 ETF 不足，無法判斷方向）",
            basis="", detail_sections=[_section(
                "每日主動選股多空廣度公式",
                formula="廣度 breadth = (偏多 ETF 數 − 偏空 ETF 數) ÷ 就緒 ETF 數；就緒 ETF = 0 時誠實回『資料累積中』",
                explanation="每檔 ETF 先算主動選股淨傾向 net_score，再跨就緒 ETF 聚合成廣度；資料不足絕不臆造方向。")],
        )]
    label = {"long": "🟢 偏多", "short": "🔴 偏空", "neutral": "⚪ 中性觀望"}.get(st, "⚪ 中性觀望")
    direction = "多" if st == "long" else "空" if st == "short" else "觀望"
    secs = [_section(
        "每日主動選股多空廣度公式",
        formula=("每檔 ETF：net_score = (加碼數 − 減碼數) ÷ 評估數（分母含持平以稀釋雜訊、偏保守）；"
                 "偏多/偏空門檻 |net_score| > 0.1。整體廣度 breadth = (偏多 ETF − 偏空 ETF) ÷ 就緒 ETF"),
        substitution=(f"= ({agg['etfs_long']} − {agg['etfs_short']}) ÷ {agg['etfs_evaluated']} "
                      f"= {agg['breadth']:+.2f} → {label}"),
        explanation="完全從 compute_symbol_trends 的 raw_contributions（雙真實訊號同向的加/減碼事件）衍生，不重算、不打網路——補上『個股共識』與『跨模型整體』之間缺少的每檔選股淨傾向中間層。")]
    if st in ("long", "short"):
        secs.append(_etf_backtest_section("up" if st == "long" else "down", backtest))
    return [Recommendation(
        rec_id="etf_stance", category="etf_stance", direction=direction,
        verdict=f"📊 每日主動選股多空：{label}",
        basis=(f"就緒 ETF 中 {agg['etfs_long']} 檔偏多／{agg['etfs_short']} 檔偏空／{agg['etfs_neutral']} 檔中性，"
               f"廣度 {agg['breadth']:+.2f} 映射整體多空。"),
        detail_sections=secs,
    )]


def etf_stance_phrase(tilt: dict) -> str:
    """薄 wrapper（bug#00117）：以 etf_stance_recommendation 為單一真理來源，投影為一句話
    （首頁卡片截取＋ActiveETFsScreen 分析框共用，兩處讀數一致）。"""
    from .shared import dashboard_line
    recs = etf_stance_recommendation(tilt)
    return dashboard_line(recs[0]) if recs else "📊 每日主動選股多空：資料累積中"


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward backtest for the cross-ETF accumulation consensus (bug#00092)
# ─────────────────────────────────────────────────────────────────────────────
# Same discipline as calibration.backtest_verdicts (期權): 100% offline, zero
# network, no backfill, no fabrication — built purely on the real daily ETF
# snapshots storage has been accumulating (etf_cache/history/*.jsonl, each holding
# carrying its real per-date price). The signal being validated is *exactly* the
# one the ETF card shows: compute_symbol_trends()'s cross-ETF「多數性」consensus
# (share-count AND weight must agree). For each historical day T taken "as now",
# we recompute that consensus using ONLY snapshots ≤ T (no look-ahead), then check
# whether the consensus symbol's own real forward price (median real holding price
# across the ETFs that hold it) moved the predicted way over ≥ horizon calendar
# days. Hit rates per look-ahead are compared to the baseline up-rate of the same
# display universe, giving the signal's edge. Sample < min_signals → honestly
# flagged "資料累積中", never a falsely confident number (0 on day one — by design).

_etf_bt_cache: dict = {}
_ETF_BT_CACHE_MAX = 8


def _etf_data_signature(snapshots_by_etf: dict) -> tuple:
    return tuple(sorted(
        (e, len(s or []), (s[-1].get("date") if s else None))
        for e, s in snapshots_by_etf.items()
    ))


def _build_symbol_price_series(snapshots_by_etf: dict[str, list[dict]]) -> dict[str, list]:
    """{symbol: [(date, price), ...] ascending} — for each date, the median of the
    real per-date holding prices reported across every ETF that held the symbol
    that day. Only real prices are used (None dropped, never fabricated)."""
    by_sym_date: dict[str, dict[str, list[float]]] = {}
    for snaps in snapshots_by_etf.values():
        for snap in (snaps or []):
            d = snap.get("date")
            if not d:
                continue
            for h in snap.get("holdings", []) or []:
                sym = h.get("symbol")
                price = h.get("price")
                if sym is None or price is None or price <= 0:
                    continue
                by_sym_date.setdefault(sym, {}).setdefault(d, []).append(price)

    def _median(xs: list[float]) -> float:
        xs = sorted(xs)
        n = len(xs)
        return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0

    series: dict[str, list] = {}
    for sym, dmap in by_sym_date.items():
        series[sym] = [(d, _median(v)) for d, v in sorted(dmap.items())]
    return series


def _price_on_or_before(series: list, target: str):
    """Most recent (date, price) with date <= target; None if none."""
    out = None
    for d, px in series:
        if d <= target:
            out = px
        else:
            break
    return out


def _price_on_or_after(series: list, parsed_series, target_date):
    """First price whose date >= target_date; None if none. parsed_series is the
    pre-parsed date list aligned with series."""
    for (d, px), pd_ in zip(series, parsed_series):
        if pd_ >= target_date:
            return px
    return None


def backtest_etf_consensus(
    snapshots_by_etf: dict[str, list[dict]],
    horizons: tuple = (1, 5, 10, 14, 30, 60),  # 含 30/60 天長線前瞻期（bug#00106）
    window_days: int = 14,
    min_etfs_evaluated: int = 4,
    consensus_threshold: float = 0.5,
    flat_threshold_pp: float = 0.5,
    min_signals: int = 20,
) -> dict:
    """Walk-forward calibration of the cross-ETF「多數性」consensus (1/5/10-day
    look-aheads). Returns a report shaped identically to
    calibration.backtest_verdicts so calibration.calibration_status_label() works
    on it unchanged.

    by_horizon: {h: {baseline_up_rate, baseline_n, up_n, up_hit_rate, up_mean_fwd,
                     down_n, down_hit_rate, down_mean_fwd, evaluated_signals, ready}}
    """
    cache_key = (_etf_data_signature(snapshots_by_etf), tuple(horizons),
                 window_days, min_etfs_evaluated, round(consensus_threshold, 3),
                 round(flat_threshold_pp, 3), min_signals)
    if cache_key in _etf_bt_cache:
        return _etf_bt_cache[cache_key]

    price_series = _build_symbol_price_series(snapshots_by_etf)
    parsed_series = {
        sym: [datetime.strptime(d, "%Y-%m-%d").date() for d, _ in ser]
        for sym, ser in price_series.items()
    }

    signal_dates = sorted({
        snap.get("date")
        for snaps in snapshots_by_etf.values()
        for snap in (snaps or [])
        if snap.get("date")
    })

    up = {h: [] for h in horizons}
    down = {h: [] for h in horizons}
    baseline = {h: [] for h in horizons}
    records: list = []  # bug#00094: 逐訊號紀錄，供子區間穩定性檢定
    symbols_seen: set = set()

    for T in signal_dates:
        T_date = datetime.strptime(T, "%Y-%m-%d").date()
        upto = {
            e: [s for s in (snaps or []) if s.get("date") and s["date"] <= T]
            for e, snaps in snapshots_by_etf.items()
        }
        upto = {e: s for e, s in upto.items() if s}
        report = compute_symbol_trends(
            upto, window_days=window_days,
            flat_threshold_pp=flat_threshold_pp,
            consensus_threshold=consensus_threshold, as_of=T,
        )
        for sym, info in report.get("symbols", {}).items():
            if info.get("etfs_evaluated", 0) < min_etfs_evaluated:
                continue  # display universe only (same gate as the card)
            ser = price_series.get(sym)
            if not ser:
                continue
            entry = _price_on_or_before(ser, T)
            if not entry or entry <= 0:
                continue
            psd = parsed_series[sym]
            consensus = info.get("consensus")
            contributed = False
            for h in horizons:
                from datetime import timedelta as _td
                exit_px = _price_on_or_after(ser, psd, T_date + _td(days=h))
                if not exit_px or exit_px <= 0:
                    continue
                fwd = exit_px / entry - 1.0
                baseline[h].append(fwd)
                if consensus == "up":
                    up[h].append(fwd)
                    records.append({"date": T, "h": h, "dir": "up", "hit": fwd > 0})
                elif consensus == "down":
                    down[h].append(fwd)
                    records.append({"date": T, "h": h, "dir": "down", "hit": fwd < 0})
                contributed = True
            if contributed:
                symbols_seen.add(sym)

    def _hit_rate(xs, expect_up):
        if not xs:
            return None
        hits = sum(1 for x in xs if (x > 0) == expect_up)
        return hits / len(xs)

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

    result = {
        "horizons": list(horizons),
        "window_days": window_days,
        "min_etfs_evaluated": min_etfs_evaluated,
        "min_signals": min_signals,
        "symbols_with_price": len(symbols_seen),
        "total_signal_days": len(signal_dates),
        "first_date": signal_dates[0] if signal_dates else None,
        "last_date": signal_dates[-1] if signal_dates else None,
        "by_horizon": by_horizon,
    }

    from .backtest_stats import attach_significance
    attach_significance(result, records)

    if len(_etf_bt_cache) >= _ETF_BT_CACHE_MAX:
        _etf_bt_cache.clear()
    _etf_bt_cache[cache_key] = result
    return result


def etf_backtest_note(direction: str, backtest: "Optional[dict]", min_signals: int = 20) -> str:
    """One-line 回測 hit-rate suffix for an ETF「多數性」bullet, matching the
    style of the options verdict cards. `direction` is "up"/"down". Picks the
    look-ahead with the most samples for that direction (prefer 5 → 10 → 1)."""
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
    n = st[key_n]
    hit = st[key_hit]
    base = st.get("baseline_up_rate")
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


# ─────────────────────────────────────────────────────────────────────────────
# Walk-forward backtest for the daily active-selection breadth stance (C-b)
# ─────────────────────────────────────────────────────────────────────────────
# 設計決策 C-b：把每日聚合的「主動選股多空廣度 stance」對照一個「持有宇集市場代理」
# 的前瞻報酬來驗證。某歷史日 T「當作當下」，只用 ≤T 的真實快照重推 stance；市場代理
# 的前瞻 h 日報酬 = 該日所有有真實價格個股的 (price_{T+h}/price_T − 1) 的橫斷面中位數
# （_build_symbol_price_series，100% 真實快照、零網路），代表這些主動經理人操作的大盤。
# 與 backtest_etf_consensus 完全相同的 walk-forward 紀律、by_horizon 形狀與顯著性接法，
# 故 calibration_status_label / significance_phrase 可直接沿用。
# 內生性註記：stance 由「真實股數變動」（實際交易）衍生，代理報酬為價格報酬，兩者大致
# 獨立；殘餘動能自相關已由 baseline+edge 與 ESS（backtest_stats）部分抵銷。

_etf_tilt_bt_cache: dict = {}
_ETF_TILT_BT_CACHE_MAX = 8


def backtest_etf_selection_tilt(
    snapshots_by_etf: dict[str, list[dict]],
    horizons: tuple = (1, 5, 10, 14, 30, 60),
    window_days: int = 14,
    tilt_min_net: float = 0.1,
    stance_breadth_min: float = 0.2,
    consensus_threshold: float = 0.5,
    flat_threshold_pp: float = 0.5,
    min_signals: int = 20,
) -> dict:
    """Walk-forward validation of the daily active-selection breadth stance
    against the held-universe market-proxy forward return (design C-b).

    Returns a report shaped identically to backtest_etf_consensus (by_horizon
    with baseline_up_rate / up_* / down_* / ready), so the same calibration
    status label and significance helpers work unchanged. One signal per date
    (the aggregate stance), not per symbol.
    """
    cache_key = (_etf_data_signature(snapshots_by_etf), tuple(horizons),
                 window_days, round(tilt_min_net, 3), round(stance_breadth_min, 3),
                 round(consensus_threshold, 3), round(flat_threshold_pp, 3), min_signals)
    if cache_key in _etf_tilt_bt_cache:
        return _etf_tilt_bt_cache[cache_key]

    price_series = _build_symbol_price_series(snapshots_by_etf)
    parsed_series = {
        sym: [datetime.strptime(d, "%Y-%m-%d").date() for d, _ in ser]
        for sym, ser in price_series.items()
    }

    signal_dates = sorted({
        snap.get("date")
        for snaps in snapshots_by_etf.values()
        for snap in (snaps or [])
        if snap.get("date")
    })

    up = {h: [] for h in horizons}
    down = {h: [] for h in horizons}
    baseline = {h: [] for h in horizons}
    records: list = []

    from datetime import timedelta as _td

    for T in signal_dates:
        T_date = datetime.strptime(T, "%Y-%m-%d").date()
        upto = {
            e: [s for s in (snaps or []) if s.get("date") and s["date"] <= T]
            for e, snaps in snapshots_by_etf.items()
        }
        upto = {e: s for e, s in upto.items() if s}
        report = compute_symbol_trends(
            upto, window_days=window_days,
            flat_threshold_pp=flat_threshold_pp,
            consensus_threshold=consensus_threshold, as_of=T,
        )
        tilt = compute_etf_selection_tilt(
            report, tilt_min_net=tilt_min_net, stance_breadth_min=stance_breadth_min,
        )
        stance = tilt["aggregate"]["stance"]
        if stance not in ("long", "short"):
            continue  # no directional call that day → not a signal

        for h in horizons:
            fwds = []
            for sym, ser in price_series.items():
                entry = _price_on_or_before(ser, T)
                if not entry or entry <= 0:
                    continue
                exit_px = _price_on_or_after(ser, parsed_series[sym], T_date + _td(days=h))
                if not exit_px or exit_px <= 0:
                    continue
                fwds.append(exit_px / entry - 1.0)
            proxy_fwd = _median(fwds)
            if proxy_fwd is None:
                continue
            baseline[h].append(proxy_fwd)
            if stance == "long":
                up[h].append(proxy_fwd)
                records.append({"date": T, "h": h, "dir": "up", "hit": proxy_fwd > 0})
            else:
                down[h].append(proxy_fwd)
                records.append({"date": T, "h": h, "dir": "down", "hit": proxy_fwd < 0})

    def _hit_rate(xs, expect_up):
        if not xs:
            return None
        hits = sum(1 for x in xs if (x > 0) == expect_up)
        return hits / len(xs)

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

    result = {
        "horizons": list(horizons),
        "window_days": window_days,
        "min_signals": min_signals,
        "total_signal_days": len(signal_dates),
        "first_date": signal_dates[0] if signal_dates else None,
        "last_date": signal_dates[-1] if signal_dates else None,
        "by_horizon": by_horizon,
    }

    from .backtest_stats import attach_significance
    attach_significance(result, records)

    if len(_etf_tilt_bt_cache) >= _ETF_TILT_BT_CACHE_MAX:
        _etf_tilt_bt_cache.clear()
    _etf_tilt_bt_cache[cache_key] = result
    return result
