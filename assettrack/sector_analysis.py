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


def _cap_weighted(members: list[dict], field: str) -> Optional[float]:
    """Market-cap-weighted average of `field` across members that have both a real
    marketcap and a real value for that field. None if nothing qualifies."""
    num = 0.0
    den = 0.0
    for m in members:
        mc = m.get("marketcap")
        v = m.get(field)
        if mc and v is not None:
            num += mc * v
            den += mc
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


def summarize_group(members_data: dict[str, dict], symbols: list[str]) -> dict:
    """Build one group's current-day view (item#1 / item#8) from freshly-fetched
    member data (quotes.fetch_sector_members_data output).

    Returns total_marketcap, cap-weighted day/week/month % (the group's 「市值漲跌
    %」), today's up/down breadth, and the per-member rows (sorted by day_pct desc
    for display — biggest gainer on top, per item#1)."""
    # bug#00091：投資建議一律以美股為主——排除台股成分股（.TW/.TWO 結尾），
    # 讓廣度/市值加權/共識與回測皆不含台股；台股持倉追蹤不受影響。
    symbols = [s for s in symbols if not str(s).upper().endswith((".TW", ".TWO"))]
    members: list[dict] = []
    total_mc = 0.0
    for sym in symbols:
        d = dict(members_data.get(sym, {}) or {})
        d["symbol"] = sym
        members.append(d)
        mc = d.get("marketcap")
        if mc:
            total_mc += mc

    # item#1「佔比」：各成分股市值佔板塊總市值的比重（%）。與整個模組的市值加權
    # 口徑一致；缺真實市值者為 None（不捏造）。總市值為 0 時全部為 None。
    for m in members:
        mc = m.get("marketcap")
        m["weight"] = round(mc / total_mc * 100, 2) if (mc and total_mc) else None

    n_up, n_down, n_rated, breadth = _breadth(members)
    members_sorted = sorted(
        members,
        key=lambda m: (m.get("day_pct") is not None, m.get("day_pct") or 0.0),
        reverse=True,
    )
    return {
        "total_marketcap": total_mc if total_mc else None,
        "capw_day": _cap_weighted(members, "day_pct"),
        "capw_week": _cap_weighted(members, "week_pct"),
        "capw_month": _cap_weighted(members, "month_pct"),
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


def generate_sector_recommendations(flows: "dict[str, dict]",
                                    backtest: "Optional[dict]" = None) -> "list":
    """把類股廣度共識組成三層結構化建議（bug#00117）。第一層＝板塊普遍上漲/下跌；
    第二層＝廣度＋市值加權雙訊號一致＋持續性；第三層＝廣度公式＋帶入本板塊數字＋回測。
    generate_sector_conclusions 為其薄 wrapper。"""
    from .shared import Recommendation, _section
    recs: list = []
    ranked = sorted(
        (
            (name, f) for name, f in flows.items()
            if f.get("ready") and f.get("direction") in ("up", "down")
        ),
        key=lambda kv: (kv[1]["up_days"] if kv[1]["direction"] == "up" else kv[1]["down_days"]),
        reverse=True,
    )
    for name, f in ranked:
        up = f["direction"] == "up"
        emoji = "📈" if up else "📉"
        verb = "普遍上漲（共同買進）" if up else "普遍下跌（共同賣出）"
        hit = f["up_days"] if up else f["down_days"]
        capw = f.get("latest_capw")
        capw_s = f"，最新市值加權 {capw:+.2f}%" if capw is not None else ""
        recs.append(Recommendation(
            rec_id=f"sector:{name}", category="sector",
            direction="多" if up else "空",
            verdict=f"{emoji} 【類股共識】{name}：{verb}{capw_s}",
            basis=(f"近 {f['days_evaluated']} 個交易日中有 {hit} 天『廣度』與『市值加權報酬』兩訊號同向"
                   f"普遍走勢，達持續性門檻即成立。"),
            detail_sections=[_section(
                "廣度擴散指數共識公式（雙訊號一致）",
                formula=("廣度 = (上漲成分股數 − 下跌成分股數) ÷ 有報價成分股數（範圍 −1…+1）；"
                         "某日判『普遍上漲』需 廣度 ≥ breadth_threshold(0.5) 且 市值加權報酬 > capw_threshold(0.1%)"),
                substitution=(f"{name}：近 {f['days_evaluated']} 個交易日中 {hit} 天同向"
                              f"{('，最新市值加權報酬 ' + format(capw, '+.2f') + '%') if capw is not None else ''}"),
                explanation="市值加權報酬 = Σ(權重×報酬)，同時就是該板塊的市值漲跌%。再加持續性過濾：最近 5 個交易日中 ≥ 3 天同向才標記，區分『持續共同買賣』與『單日雜訊』。成分股於運算前排除台股。"),
                _sector_backtest_section("up" if up else "down", backtest)],
        ))
    return recs


def generate_sector_conclusions(flows: dict[str, dict],
                                backtest: "Optional[dict]" = None) -> list[str]:
    """薄 wrapper（bug#00117）：以 generate_sector_recommendations 為單一真理來源，投影為
    主頁用的「一句話」字串清單。畫面完整三層改由 recs 直接渲染。"""
    from .shared import dashboard_line
    return [dashboard_line(r) for r in generate_sector_recommendations(flows, backtest=backtest)]


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
