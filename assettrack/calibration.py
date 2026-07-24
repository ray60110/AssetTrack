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
  - 連續多日對同一標的的訊號高度自相關，命中率會略為樂觀；樣本需夠大才穩健。
  - horizon 以「日曆天」計（找 ≥ T+horizon 的第一筆快照），非交易日；系統沒開的
    日子沒有快照，屬預期。
  - 觀望（無方向）日不計入命中率，只計入基準。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from .options_analysis import compute_directional_verdicts

DEFAULT_HORIZONS = (1, 5, 7, 10, 14, 21, 30, 35, 60)  # 含 +7~+35 天波段前瞻期（bug#00110）

# 結果快取：Dashboard 首頁卡片每 60 秒重繪一次，walk-forward 對逐日快照全量重算
# 並不便宜；但輸入資料（累積快照）一天只會多一筆，故以「資料簽章」為 key 快取，
# 同一份資料只算一次，畫面重繪直接取用。
_bt_cache: dict = {}
_BT_CACHE_MAX = 8


def _parse(d: str):
    return datetime.strptime(d, "%Y-%m-%d").date()


def _data_signature(snapshots_by_underlying: dict) -> tuple:
    """快取 key 用的輕量資料簽章：每檔標的的 (代碼, 快照數, 最末日)。"""
    return tuple(sorted(
        (u, len(s or []), s[-1].get("date") if s else None)
        for u, s in snapshots_by_underlying.items()
    ))


def backtest_verdicts(
    snapshots_by_underlying: dict[str, list[dict]],
    horizons: tuple = DEFAULT_HORIZONS,
    window_days: int = 14,
    r: float = 0.04,
    min_signals: int = 20,
) -> dict:
    """對累積快照做 walk-forward 綜合方向結論校準（1/5/10 天三組前瞻期）。

    回傳（供畫面與結論卡直接顯示）：
      horizons, window_days, min_signals
      underlyings_with_data, total_snapshot_days, first_date, last_date
      by_horizon: {h: {baseline_up_rate, baseline_n,
                       bullish_n, bullish_hit_rate, bullish_mean_fwd,
                       bearish_n, bearish_hit_rate, bearish_mean_fwd,
                       evaluated_signals, ready}}
    """
    cache_key = (_data_signature(snapshots_by_underlying), tuple(horizons),
                 window_days, round(r, 3), min_signals)
    if cache_key in _bt_cache:
        return _bt_cache[cache_key]

    bull = {h: [] for h in horizons}
    bear = {h: [] for h in horizons}
    baseline = {h: [] for h in horizons}
    records: list = []  # bug#00094: 逐訊號紀錄，供子區間穩定性檢定

    total_days = 0
    underlyings_with_data = 0
    first_date: Optional[str] = None
    last_date: Optional[str] = None

    for u, raw in snapshots_by_underlying.items():
        snaps = sorted([s for s in (raw or []) if s.get("date")], key=lambda s: s["date"])
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
            # 各前瞻期的 forward 快照：T 之後 ≥ h 個日曆天的第一筆（不足時誠實跳過）
            fwd_ret_by_h: dict[int, float] = {}
            for h in horizons:
                fwd_idx = None
                for j in range(i + 1, len(dates)):
                    if (parsed[j] - parsed[i]).days >= h:
                        fwd_idx = j
                        break
                if fwd_idx is None:
                    continue
                spot_f = snaps[fwd_idx].get("spot_price")
                if not spot_f or spot_f <= 0:
                    continue
                fwd_ret_by_h[h] = spot_f / spot_t - 1.0
            if not fwd_ret_by_h:
                continue

            # 以「當下」T 重新推導綜合方向結論——與結論卡同一個函式、只用 ≤T 的快照
            rep = compute_directional_verdicts(
                {u: snaps[: i + 1]}, r=r, window_days=window_days, as_of=T
            )
            direction = rep["verdicts"].get(u, {}).get("direction")

            for h, fwd_ret in fwd_ret_by_h.items():
                baseline[h].append(fwd_ret)
                if direction == "多":
                    bull[h].append(fwd_ret)
                    records.append({"date": T, "h": h, "dir": "up", "hit": fwd_ret > 0})
                elif direction == "空":
                    bear[h].append(fwd_ret)
                    records.append({"date": T, "h": h, "dir": "down", "hit": fwd_ret < 0})

    def _hit_rate(xs: list, expect_up: bool) -> Optional[float]:
        if not xs:
            return None
        hits = sum(1 for x in xs if (x > 0) == expect_up)
        return hits / len(xs)

    def _mean(xs: list) -> Optional[float]:
        return (sum(xs) / len(xs)) if xs else None

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
    }

    from .backtest_stats import attach_significance
    attach_significance(result, records)

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
