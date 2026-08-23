"""
assettrack/calibration.py — 訊號回測校準（walk-forward，純離線，只用真實累積快照）

bug#00070: 使用者要求「訊號回測校準先寫好邏輯，讓使用者可以隨時知道校準狀態」。
bug#00089 擴充: 回測對象由單一 skew 訊號升級為「分析結論卡」的綜合方向結論
（options_analysis.compute_directional_verdicts：未平倉 skew ＋ 排除股價變動的
殘差偏向），並同時評估 1 / 5 / 10 天三組前瞻期——結論卡顯示的預測邏輯與被回測
的邏輯是**同一個函式**，無兩套標準；三組前瞻期並列可看出訊號偏短線還是波段有效。

原則同全系統：100% 離線、零網路、不回填、不捏造。校準完全建立在 storage 每日真實
累積下來的期權快照（options_cache/history/*.jsonl，每筆含當日 spot_price）之上。

做法（walk-forward，避免前視偏誤）：對每個標的、每一個「當作當下」的歷史日 T，
只用 ≤ T 的快照重新推導當日的綜合方向結論（與畫面結論卡完全同一套
compute_directional_verdicts），再看該標的在 T 之後 ≥ horizon 天的第一筆真實快照
的 spot 變化是否與結論方向一致（命中）。彙總各前瞻期命中率並與「基準上漲日比例」
比較，得出訊號是否有超額（edge）。

**誠實狀態**：可評估訊號數 < 門檻時，明白標示「樣本不足/資料累積中」而非給出看似
可信的數字。剛上線時必然為 0——這正是要讓使用者「隨時知道」的狀態。

已知限制（會誠實顯示於畫面）：
  - 同標的重疊 outcome 區間會直接 purge；跨標的同日相關性另以不同 session 數限制 ESS。
  - horizon 使用內建 NYSE 完整休市日日曆並先按真實 lastTradeDate 去除重複快照。
  - 觀望（無方向）日不計入命中率，只計入基準。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .market_sessions import NYSESessionCalendar
from .options_analysis import compute_directional_verdicts


_NYSE_CALENDAR = NYSESessionCalendar()

DEFAULT_HORIZONS = (1, 5, 7, 10, 14, 21, 30, 35, 60)  # 含 +7~+35 天波段前瞻期（bug#00110）

# 結果快取：Dashboard 首頁卡片每 60 秒重繪一次，walk-forward 對逐日快照全量重算
# 並不便宜；但輸入資料（累積快照）一天只會多一筆，故以「資料簽章」為 key 快取，
# 同一份資料只算一次，畫面重繪直接取用。
_bt_cache: dict = {}
_BT_CACHE_MAX = 8


def _parse(d: str):
    return datetime.strptime(d, "%Y-%m-%d").date()


def trading_sessions_between(d0: date, d1: date) -> int:
    """Count completed NYSE market sessions in ``(d0, d1]``."""
    if d1 <= d0:
        return 0
    sessions = 0
    cur = d0
    while cur < d1:
        cur += timedelta(days=1)
        if _NYSE_CALENDAR.is_session(cur):
            sessions += 1
    return sessions


def _snapshot_session_date(snapshot: dict) -> Optional[str]:
    """Infer the US market session represented by an options-chain snapshot."""
    explicit = snapshot.get("session_date")
    if explicit:
        try:
            return _parse(str(explicit)[:10]).isoformat()
        except (TypeError, ValueError):
            pass

    latest_trade = None
    for contract in snapshot.get("contracts", []):
        raw = contract.get("lastTradeDate")
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if latest_trade is None or parsed > latest_trade:
            latest_trade = parsed
    if latest_trade is None:
        return snapshot.get("date")

    inferred = latest_trade.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    captured = snapshot.get("date")
    # A malformed/future lastTradeDate must not move the snapshot beyond capture.
    if captured and inferred > captured:
        return captured
    return inferred


def normalise_option_snapshots(snapshots: list[dict]) -> list[dict]:
    """Map capture dates to US sessions and keep only the latest capture per session."""
    by_session: dict[str, dict] = {}
    for snapshot in sorted(
        (s for s in (snapshots or []) if s.get("date")),
        key=lambda s: s["date"],
    ):
        session = _snapshot_session_date(snapshot)
        if not session:
            continue
        normalised = dict(snapshot)
        normalised["captured_date"] = snapshot.get("date")
        normalised["session_date"] = session
        normalised["date"] = session
        by_session[session] = normalised
    return [by_session[key] for key in sorted(by_session)]


def _data_signature(snapshots_by_underlying: dict) -> tuple:
    """Cache signature that changes when same-session quote content changes."""
    def _snapshot_sig(snapshot: dict) -> tuple:
        contracts = snapshot.get("contracts", [])
        return (
            snapshot.get("date"),
            round(float(snapshot.get("spot_price") or 0.0), 6),
            len(contracts),
            round(sum(float(c.get("openInterest") or 0.0) for c in contracts), 3),
            round(sum(float(c.get("bid") or 0.0) for c in contracts), 3),
            round(sum(float(c.get("ask") or 0.0) for c in contracts), 3),
            max((str(c.get("lastTradeDate") or "") for c in contracts), default=""),
        )

    return tuple(sorted(
        (u, tuple(_snapshot_sig(snapshot) for snapshot in (snaps or [])))
        for u, snaps in snapshots_by_underlying.items()
    ))


def assess_model_health(
    records: list[dict],
    preferred_horizon: int = 5,
    recent_sessions: int = 8,
    min_sessions: int = 3,
) -> dict:
    """Prequential health check on the most recent matured forecast sessions.

    This is a safety monitor, not a training claim. It can mark the champion
    degraded and start a conservative threshold proposal; it cannot promote an
    unvalidated replacement policy.
    """
    horizons = sorted({int(r["h"]) for r in records if r.get("h") is not None})
    if not horizons:
        return {
            "status": "warming_up",
            "reason": "尚無已結算的方向預測",
            "horizon": None,
            "recent_n": 0,
            "recent_hit_rate": None,
            "miss_streak": 0,
        }
    horizon = preferred_horizon if preferred_horizon in horizons else min(
        horizons, key=lambda h: (abs(h - preferred_horizon), h)
    )
    def _evaluate_direction(direction: str) -> dict:
        by_date: dict[str, list[bool]] = {}
        for record in records:
            if int(record.get("h", -1)) == horizon and record.get("dir") == direction:
                by_date.setdefault(record["date"], []).append(bool(record["hit"]))
        daily = [
            (session, sum(hits) / len(hits))
            for session, hits in sorted(by_date.items())
            if hits
        ][-recent_sessions:]
        hit_rate = sum(rate for _, rate in daily) / len(daily) if daily else None
        miss_streak = 0
        for _, rate in reversed(daily):
            if rate < 0.5:
                miss_streak += 1
            else:
                break
        if len(daily) < min_sessions:
            status = "warming_up"
            reason = f"已結算 {len(daily)}/{min_sessions} 個獨立 session"
        elif hit_rate is not None and hit_rate <= 0.40 and miss_streak >= 3:
            status = "degraded"
            reason = (
                f"最近 {len(daily)} 個 session 命中率 {hit_rate:.0%}，"
                f"且連續失配 {miss_streak} 次"
            )
        elif (hit_rate is not None and hit_rate < 0.50) or miss_streak >= 2:
            status = "warning"
            reason = f"最近 {len(daily)} 個 session 命中率 {hit_rate:.0%}，需持續監控"
        else:
            status = "healthy"
            reason = f"最近 {len(daily)} 個 session 命中率 {hit_rate:.0%}"
        return {
            "status": status,
            "reason": reason,
            "recent_n": len(daily),
            "recent_hit_rate": hit_rate,
            "miss_streak": miss_streak,
        }

    by_direction = {
        direction: _evaluate_direction(direction)
        for direction in ("up", "down")
    }
    labels = {"up": "偏多", "down": "偏空"}
    failed = next(
        (direction for direction in ("up", "down")
         if by_direction[direction]["status"] == "degraded"),
        None,
    )
    warned = next(
        (direction for direction in ("up", "down")
         if by_direction[direction]["status"] == "warning"),
        None,
    )
    if failed:
        chosen = by_direction[failed]
        status = "degraded"
        reason = f"{labels[failed]}分支失效：{chosen['reason']}"
    elif warned:
        chosen = by_direction[warned]
        status = "warning"
        reason = f"{labels[warned]}分支警告：{chosen['reason']}"
    elif all(item["status"] == "healthy" for item in by_direction.values()):
        chosen = max(by_direction.values(), key=lambda item: item["recent_n"])
        status = "healthy"
        reason = "偏多／偏空分支近期均未偵測到失配"
    else:
        chosen = max(by_direction.values(), key=lambda item: item["recent_n"])
        status = "warming_up"
        reason = "方向分支仍在累積獨立 session"
    return {
        "status": status,
        "reason": reason,
        "horizon": horizon,
        "recent_n": chosen["recent_n"],
        "recent_hit_rate": chosen["recent_hit_rate"],
        "miss_streak": chosen["miss_streak"],
        "by_direction": by_direction,
    }


def backtest_verdicts(
    snapshots_by_underlying: dict[str, list[dict]],
    horizons: tuple = DEFAULT_HORIZONS,
    window_days: int = 14,
    r: float = 0.04,
    min_signals: int = 20,
    verdict_params: Optional[dict] = None,
) -> dict:
    """對累積快照做 walk-forward 綜合方向結論校準（市場 session 前瞻期）。

    回傳（供畫面與結論卡直接顯示）：
      horizons, window_days, min_signals
      underlyings_with_data, total_snapshot_days, first_date, last_date
      by_horizon: {h: {baseline_up_rate, baseline_n,
                       bullish_n, bullish_hit_rate, bullish_mean_fwd,
                       bearish_n, bearish_hit_rate, bearish_mean_fwd,
                       evaluated_signals, ready}}
    """
    allowed_params = {
        "skew_call_hi",
        "skew_call_lo",
        "bias_min_abs",
        "bias_min_pct",
        "bias_min_n",
    }
    params = {
        key: value for key, value in (verdict_params or {}).items()
        if key in allowed_params
    }
    normalised_by_underlying = {
        underlying: normalise_option_snapshots(snaps)
        for underlying, snaps in snapshots_by_underlying.items()
    }
    cache_key = (
        _data_signature(normalised_by_underlying),
        tuple(horizons),
        window_days,
        round(r, 3),
        min_signals,
        tuple(sorted(params.items())),
    )
    if cache_key in _bt_cache:
        return _bt_cache[cache_key]

    bull = {h: [] for h in horizons}
    bear = {h: [] for h in horizons}
    baseline = {h: [] for h in horizons}
    records: list = []  # 逐訊號紀錄；後續會 purge 重疊 outcome 區間
    baseline_records: list = []

    total_days = 0
    underlyings_with_data = 0
    first_date: Optional[str] = None
    last_date: Optional[str] = None

    for u, snaps in normalised_by_underlying.items():
        if not snaps:
            continue
        underlyings_with_data += 1
        total_days += len(snaps)
        fd, ld = snaps[0]["date"], snaps[-1]["date"]
        first_date = fd if (first_date is None or fd < first_date) else first_date
        last_date = ld if (last_date is None or ld > last_date) else last_date

        dates = [s["date"] for s in snaps]
        parsed = [_parse(d) for d in dates]
        for i, T in enumerate(dates):
            spot_t = snaps[i].get("spot_price")
            if not spot_t or spot_t <= 0:
                continue
            # 各前瞻期只接受「剛好 T+h」的真實快照。若中間缺資料，不可把
            # +8 session 的報酬冒充 +5 session outcome。
            fwd_ret_by_h: dict[int, tuple[float, int]] = {}
            for h in horizons:
                fwd_idx = None
                for j in range(i + 1, len(dates)):
                    elapsed = trading_sessions_between(parsed[i], parsed[j])
                    if elapsed == h:
                        fwd_idx = j
                        break
                    if elapsed > h:
                        break
                if fwd_idx is None:
                    continue
                spot_f = snaps[fwd_idx].get("spot_price")
                if not spot_f or spot_f <= 0:
                    continue
                fwd_ret_by_h[h] = (spot_f / spot_t - 1.0, fwd_idx)
            if not fwd_ret_by_h:
                continue

            # 以「當下」T 重新推導綜合方向結論——與結論卡同一個函式、只用 ≤T 的快照
            rep = compute_directional_verdicts(
                {u: snaps[: i + 1]},
                r=r,
                window_days=window_days,
                as_of=T,
                **params,
            )
            direction = rep["verdicts"].get(u, {}).get("direction")

            for h, (fwd_ret, fwd_idx) in fwd_ret_by_h.items():
                baseline[h].append(fwd_ret)
                baseline_records.append({
                    "underlying": u,
                    "date": T,
                    "outcome_date": dates[fwd_idx],
                    "entry_index": i,
                    "outcome_index": fwd_idx,
                    "h": h,
                    "outcome_up": fwd_ret > 0,
                    "forward_return": fwd_ret,
                })
                if direction == "多":
                    bull[h].append(fwd_ret)
                    records.append({
                        "underlying": u,
                        "date": T,
                        "outcome_date": dates[fwd_idx],
                        "entry_index": i,
                        "outcome_index": fwd_idx,
                        "h": h,
                        "dir": "up",
                        "hit": fwd_ret > 0,
                        "forward_return": fwd_ret,
                    })
                elif direction == "空":
                    bear[h].append(fwd_ret)
                    records.append({
                        "underlying": u,
                        "date": T,
                        "outcome_date": dates[fwd_idx],
                        "entry_index": i,
                        "outcome_index": fwd_idx,
                        "h": h,
                        "dir": "down",
                        "hit": fwd_ret < 0,
                        "forward_return": fwd_ret,
                    })

    def _hit_rate(xs: list, expect_up: bool) -> Optional[float]:
        if not xs:
            return None
        hits = sum(1 for x in xs if (x > 0) == expect_up)
        return hits / len(xs)

    def _mean(xs: list) -> Optional[float]:
        return (sum(xs) / len(xs)) if xs else None

    # Long-horizon daily labels overlap almost completely.  Treating them as
    # independent is the main reason the legacy backtest looked precise while
    # failing live.  The public backtest now uses only non-overlapping intervals;
    # raw counts remain visible for auditability.
    from .options_forecasting import build_probability_backtest, purge_overlapping_records

    raw_bull = {h: list(bull[h]) for h in horizons}
    raw_bear = {h: list(bear[h]) for h in horizons}
    raw_baseline = {h: list(baseline[h]) for h in horizons}
    purged_records = purge_overlapping_records(records)
    purged_baseline_records = purge_overlapping_records(baseline_records)
    bull = {
        h: [r["forward_return"] for r in purged_records if r["h"] == h and r["dir"] == "up"]
        for h in horizons
    }
    bear = {
        h: [r["forward_return"] for r in purged_records if r["h"] == h and r["dir"] == "down"]
        for h in horizons
    }
    baseline = {
        h: [r["forward_return"] for r in purged_baseline_records if r["h"] == h]
        for h in horizons
    }

    by_horizon: dict[int, dict] = {}
    for h in horizons:
        evaluated = len(bull[h]) + len(bear[h])
        by_horizon[h] = {
            "baseline_up_rate": _hit_rate(baseline[h], True),
            "baseline_n": len(baseline[h]),
            "bullish_n": len(bull[h]),
            "bullish_hit_rate": _hit_rate(bull[h], True),
            "bullish_mean_fwd": _mean(bull[h]),
            "bearish_n": len(bear[h]),
            "bearish_hit_rate": _hit_rate(bear[h], False),
            "bearish_mean_fwd": _mean(bear[h]),
            "evaluated_signals": evaluated,
            "ready": evaluated >= min_signals,
            "raw_baseline_n": len(raw_baseline[h]),
            "raw_bullish_n": len(raw_bull[h]),
            "raw_bearish_n": len(raw_bear[h]),
            "purged": True,
        }

    result = {
        "horizons": list(horizons),
        "window_days": window_days,
        "min_signals": min_signals,
        "underlyings_with_data": underlyings_with_data,
        "total_snapshot_days": total_days,
        "first_date": first_date,
        "last_date": last_date,
        "by_horizon": by_horizon,
        "model_params": params,
        "overlap_purged": True,
    }

    from .backtest_stats import attach_significance
    attach_significance(result, purged_records)
    result["probability_backtest"] = build_probability_backtest(
        records,
        baseline_records,
        min_samples=min_signals,
    )
    result["model_health"] = assess_model_health(purged_records)

    if len(_bt_cache) >= _BT_CACHE_MAX:
        _bt_cache.clear()
    _bt_cache[cache_key] = result
    return result


def calibration_status_label(report: dict) -> str:
    """把回測結果轉成一句「校準狀態」標籤（給使用者隨時一眼判斷可信度）。
    以三組前瞻期中樣本最多的一組為準。"""
    by_h = report.get("by_horizon", {})
    ev = max((st["evaluated_signals"] for st in by_h.values()), default=0)
    need = report["min_signals"]
    if ev == 0:
        return "尚無可評估訊號（資料累積中）"
    # bug#00094: 有統計驗證資訊時，改用「顯著性 + 前後穩定性」的可信度總結
    if any(st.get("significance") for st in by_h.values()):
        from .backtest_stats import validation_label
        prefix = "初步樣本" if ev < need else "可參考"
        return f"{prefix}（{validation_label(report)}）"
    if ev < need:
        return f"初步樣本（n={ev} < 門檻 {need}，僅供參考）"
    return f"可參考（n={ev}）"
