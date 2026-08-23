"""
類股個股 1–3 日條件機率模型。

本模組把兩種使用者關心的訊號放在同一個、可 walk-forward 驗證的條件中：

* 板塊：近 5 個交易日是否已有至少 3 日「廣度＋等權報酬」同向普漲／普跌。
* 個股：收盤價、30MA、60MA 的排列；當日上／下影線；連漲／連跌日數。

歷史模型只使用 T 日（含）以前的特徵，結果則看 T 之後第 1、2、3 個交易日的
收盤方向。歷史市值不可得，因此板塊報酬刻意採等權，避免把今天市值套回過去所造成
的前視偏誤。畫面只採用相對無條件基準確有差異、樣本足夠且前後子區間方向一致的
條件；沒有差異便回空，不佔結論區版面。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
import math
import statistics
from typing import Any, Mapping, Optional, Sequence


MODEL_VERSION = 1
DEFAULT_HORIZONS = (1, 2, 3)
DEFAULT_MIN_SAMPLES = 30
DEFAULT_MIN_EDGE = 0.03
DEFAULT_MIN_CONFIDENCE = 60.0
CONFIRMATION_MIN_COVERAGE = 0.70
CONFIRMATION_BREADTH_THRESHOLD = 0.60
SECTOR_CONFIRMATION_POLICY_VERSION = 3

# Which of the six gates a Policy Version parameter can actually move.  Recorded
# alongside every rejection so that "the model abstained" can be told apart from
# "a threshold we chose abstained" without re-running anything.
TUNABLE_REJECTION_GATES = frozenset(
    {"min_samples", "min_edge", "min_confidence"}
)


@dataclass(frozen=True)
class PredictionSignal:
    """One accepted, point-in-time probability claim for one future horizon.

    ``probability_up`` always means P(up), even for a down recommendation;
    ``direction_probability`` is the probability of the displayed direction.
    Keeping both prevents the experiment ledger from accidentally interpreting a
    70% down forecast as P(up)=70%.
    """

    group: str
    symbol: str
    horizon_sessions: int
    direction: str
    probability_up: float
    direction_probability: float
    baseline_probability_up: float
    baseline_direction_probability: float
    edge: float
    confidence: float
    sample_size: int
    significance: Mapping[str, Any]
    ma_state: str
    candle_pattern: str
    streak: int
    sector_state: str


@dataclass(frozen=True)
class PredictionRejection:
    """Why one (group, symbol, horizon) cell produced no directional claim.

    ``gate`` is the single filter that actually stopped it, in evaluation order,
    and ``tunable`` says whether any whitelisted Policy Version parameter could
    have changed that outcome.  ``observed`` carries the values compared at that
    gate (n, edge, confidence, …) so the margin is visible without recomputation.
    """

    group: str
    symbol: str
    horizon_sessions: int
    gate: str
    tunable: bool
    observed: Mapping[str, Any] = field(default_factory=dict)


def signed_streak(closes: list[float]) -> int:
    """回傳截至最後一根的連漲（正）／連跌（負）日數；平盤或不足兩根為 0。"""
    if len(closes) < 2:
        return 0
    last_delta = closes[-1] - closes[-2]
    if abs(last_delta) <= 1e-12:
        return 0
    sign = 1 if last_delta > 0 else -1
    count = 1
    for i in range(len(closes) - 2, 0, -1):
        delta = closes[i] - closes[i - 1]
        if (delta > 0) != (sign > 0) or abs(delta) <= 1e-12:
            break
        count += 1
    return sign * count


def candle_pattern(open_: Optional[float], high: Optional[float],
                   low: Optional[float], close: Optional[float]) -> str:
    """以整根 K 棒比例辨識明顯上／下影線；其餘回 neutral。"""
    if None in (open_, high, low, close):
        return "neutral"
    span = float(high) - float(low)
    if span <= 1e-12:
        return "neutral"
    body_top = max(float(open_), float(close))
    body_bottom = min(float(open_), float(close))
    upper = max(0.0, float(high) - body_top)
    lower = max(0.0, body_bottom - float(low))
    # 影線至少佔全日振幅 35%，且明顯長於另一側，避免把一般小尾巴當訊號。
    if upper / span >= 0.35 and upper >= max(lower * 1.5, span * 0.35):
        return "upper_wick"
    if lower / span >= 0.35 and lower >= max(upper * 1.5, span * 0.35):
        return "lower_wick"
    return "neutral"


def ma_pattern(close: Optional[float], ma30: Optional[float],
               ma60: Optional[float]) -> str:
    """收盤／30MA／60MA 的四種排列。"""
    if close is None or ma30 is None or ma60 is None:
        return "unknown"
    if close >= ma30 >= ma60:
        return "bullish"
    if close <= ma30 <= ma60:
        return "bearish"
    return "ma30_above" if ma30 >= ma60 else "ma30_below"


def streak_bucket(streak: int) -> str:
    if streak >= 3:
        return "up3plus"
    if streak > 0:
        return "up1to2"
    if streak <= -3:
        return "down3plus"
    if streak < 0:
        return "down1to2"
    return "flat"


def latest_member_features(bars: list[dict]) -> dict:
    """由一檔個股 ascending 日線產生可直接存進 summary 的最新特徵。"""
    clean = [
        b for b in bars
        if b.get("close") is not None
    ]
    if not clean:
        return {
            "open": None, "high": None, "low": None, "ma30": None, "ma60": None,
            "streak": 0, "candle_pattern": "neutral",
        }
    last = clean[-1]
    closes = [float(b["close"]) for b in clean]
    ma30 = sum(closes[-30:]) / 30.0 if len(closes) >= 30 else None
    ma60 = sum(closes[-60:]) / 60.0 if len(closes) >= 60 else None
    return {
        "open": last.get("open"),
        "high": last.get("high"),
        "low": last.get("low"),
        "ma30": round(ma30, 6) if ma30 is not None else None,
        "ma60": round(ma60, 6) if ma60 is not None else None,
        "streak": signed_streak(closes),
        "candle_pattern": candle_pattern(
            last.get("open"), last.get("high"), last.get("low"), last.get("close")
        ),
    }


def _feature_state(close: float, ma30: float, ma60: float, streak: int,
                   candle: str, sector: str) -> tuple[str, str, str, str]:
    return (
        ma_pattern(close, ma30, ma60),
        candle,
        streak_bucket(streak),
        sector if sector in ("up", "down") else "none",
    )


def _pattern_key(state: tuple[str, str, str, str]) -> str:
    return "|".join(state)


def _sector_states(groups: dict[str, list[str]],
                   bars_by_symbol: dict[str, list[dict]],
                   breadth_threshold: float = 0.5,
                   capw_threshold: float = 0.1,
                   lookback: int = 5,
                   min_days: int = 3) -> dict[str, dict[str, str]]:
    """由歷史日線重建每個板塊每天的持續性廣度狀態；只看當日及以前。"""
    closes_by = {
        sym: {str(b["date"]): float(b["close"]) for b in bars if b.get("date") and b.get("close") is not None}
        for sym, bars in bars_by_symbol.items()
    }
    returns_by: dict[str, dict[str, float]] = {}
    for sym, closes in closes_by.items():
        dates = sorted(closes)
        returns_by[sym] = {
            dates[i]: (closes[dates[i]] / closes[dates[i - 1]] - 1.0) * 100.0
            for i in range(1, len(dates))
            if closes[dates[i - 1]]
        }

    out: dict[str, dict[str, str]] = {}
    for group, symbols in groups.items():
        dates = sorted({
            d for sym in symbols for d in returns_by.get(sym, {})
        })
        daily: list[tuple[str, str]] = []
        for date in dates:
            vals = [
                returns_by[sym][date] for sym in symbols
                if date in returns_by.get(sym, {})
            ]
            if len(vals) < 3:
                continue
            n_up = sum(v > 0 for v in vals)
            n_down = sum(v < 0 for v in vals)
            breadth = (n_up - n_down) / len(vals)
            equal_ret = sum(vals) / len(vals)
            direction = "none"
            if breadth >= breadth_threshold and equal_ret > capw_threshold:
                direction = "up"
            elif breadth <= -breadth_threshold and equal_ret < -capw_threshold:
                direction = "down"
            daily.append((date, direction))

        states: dict[str, str] = {}
        for i, (date, _) in enumerate(daily):
            window = [d for _, d in daily[max(0, i - lookback + 1):i + 1]]
            up_n, down_n = window.count("up"), window.count("down")
            state = "none"
            if up_n >= min_days and up_n >= down_n:
                state = "up"
            elif down_n >= min_days:
                state = "down"
            states[date] = state
        out[group] = states
    return out


def detect_current_sector_state(
    snapshots: list[dict],
    breadth_threshold: float = 0.5,
    return_threshold: float = 0.1,
    lookback: int = 5,
    min_days: int = 3,
) -> dict:
    """用與多年模型相同的等權口徑，判斷目前板塊持續性狀態。"""
    daily: list[str] = []
    for snapshot in sorted(snapshots or [], key=lambda x: str(x.get("date") or "")):
        vals = [
            float(m["day_pct"]) for m in (snapshot.get("members") or [])
            if m.get("day_pct") is not None
        ]
        if len(vals) < 3:
            continue
        n_up = sum(v > 0 for v in vals)
        n_down = sum(v < 0 for v in vals)
        breadth = (n_up - n_down) / len(vals)
        equal_ret = sum(vals) / len(vals)
        direction = "none"
        if breadth >= breadth_threshold and equal_ret > return_threshold:
            direction = "up"
        elif breadth <= -breadth_threshold and equal_ret < -return_threshold:
            direction = "down"
        daily.append(direction)

    window = daily[-lookback:]
    up_n, down_n = window.count("up"), window.count("down")
    direction = "none"
    ready = len(window) >= min_days
    if ready and up_n >= min_days and up_n >= down_n:
        direction = "up"
    elif ready and down_n >= min_days:
        direction = "down"
    return {
        "ready": ready,
        "direction": direction,
        "up_days": up_n,
        "down_days": down_n,
        "days_evaluated": len(window),
        "weighting": "equal",
    }


def build_relative_momentum_breadth_confirmation(
    groups: dict[str, list[str]],
    bars_by_symbol: dict[str, list[dict]],
    benchmark_symbol: str = "QQQ",
) -> dict:
    """Build the current cross-sector momentum + 50MA breadth confirmation.

    The method mirrors the externally validated challenger: six- and twelve-month
    equal-weight member momentum ending 21 sessions ago are cross-sectionally
    standardised and averaged.  The top/bottom two sectors become candidates;
    they emit up/down only when at least 60%/at most 40% of members are above
    their 50-session moving average.  Missing history means abstention.
    """
    import pandas as pd

    filtered = {
        name: [
            str(symbol).upper() for symbol in symbols
            if not str(symbol).upper().endswith((".TW", ".TWO"))
        ]
        for name, symbols in groups.items()
    }
    benchmark_rows = bars_by_symbol.get(benchmark_symbol) or []
    benchmark_dates = [
        str(row["date"]) for row in benchmark_rows
        if row.get("date") and row.get("close") is not None
    ]
    if len(benchmark_dates) < 274:
        return {
            "policy_version": SECTOR_CONFIRMATION_POLICY_VERSION,
            "as_of": None,
            "groups": {},
        }
    sessions = pd.Index(sorted(dict.fromkeys(benchmark_dates)))
    prices = pd.DataFrame(index=sessions)
    for symbol in sorted({s for members in filtered.values() for s in members}):
        series = pd.Series(
            {
                str(row["date"]): float(row["close"])
                for row in bars_by_symbol.get(symbol, [])
                if row.get("date") and row.get("close") is not None
            },
            dtype=float,
        )
        prices[symbol] = series.reindex(sessions).ffill(limit=3)

    i = len(sessions) - 1
    components: dict[str, tuple[float, float, float, int, int]] = {}
    states = {
        name: {
            "ready": False,
            "direction": "none",
            "rank_direction": "none",
            "pct_above_50ma": None,
            "members_evaluated": 0,
            "members_required": max(3, math.ceil(len(members) * CONFIRMATION_MIN_COVERAGE)),
            "trend_ready": False,
            "trend_direction": "none",
            "fast_trend_ready": False,
            "fast_trend_direction": "none",
            "sma5": None,
            "sma20": None,
            "sma150": None,
            "as_of": str(sessions[-1]),
        }
        for name, members in filtered.items()
    }
    daily_returns = prices.pct_change(fill_method=None)
    for name, members in filtered.items():
        required = states[name]["members_required"]
        member_returns = daily_returns[members]
        sector_return = member_returns.mean(axis=1).where(
            member_returns.notna().sum(axis=1) >= required
        )
        sector_index = (1.0 + sector_return).cumprod()
        recent20 = sector_index.iloc[-20:]
        if len(recent20) >= 20 and recent20.notna().sum() >= 20:
            sma5 = float(recent20.iloc[-5:].mean())
            sma20 = float(recent20.mean())
            states[name].update({
                "fast_trend_ready": True,
                "fast_trend_direction": (
                    "up" if sma5 > sma20 else "down" if sma5 < sma20 else "none"
                ),
                "sma5": round(sma5, 8),
                "sma20": round(sma20, 8),
            })
        recent = sector_index.iloc[-150:]
        if len(recent) < 150 or recent.notna().sum() < 150:
            continue
        sma5 = float(recent.iloc[-5:].mean())
        sma150 = float(recent.mean())
        states[name].update({
            "trend_ready": True,
            "trend_direction": (
                "up" if sma5 > sma150 else "down" if sma5 < sma150 else "none"
            ),
            "sma5": round(sma5, 8),
            "sma150": round(sma150, 8),
        })
    for name, members in filtered.items():
        required = states[name]["members_required"]
        end = prices.iloc[i - 21][members]
        start6 = prices.iloc[i - 21 - 126][members]
        start12 = prices.iloc[i - 21 - 252][members]
        r6 = (end / start6 - 1.0).replace([float("inf"), float("-inf")], pd.NA).dropna()
        r12 = (end / start12 - 1.0).replace([float("inf"), float("-inf")], pd.NA).dropna()
        current = prices.iloc[i][members]
        ma50 = prices.iloc[i - 49:i + 1][members].mean(axis=0, skipna=False)
        above = (current / ma50 - 1.0).replace(
            [float("inf"), float("-inf")], pd.NA
        ).dropna()
        common = r6.index.intersection(r12.index).intersection(above.index)
        if len(common) < required:
            continue
        r6, r12, above = r6[common], r12[common], above[common]
        pct_above = float((above > 0).mean())
        components[name] = (
            float(r6.mean()), float(r12.mean()), pct_above, len(above), required
        )

    if len(components) < 4:
        return {
            "policy_version": SECTOR_CONFIRMATION_POLICY_VERSION,
            "as_of": str(sessions[-1]),
            "groups": states,
        }

    def zscores(position: int) -> dict[str, float]:
        values = {name: row[position] for name, row in components.items()}
        mean = statistics.fmean(values.values())
        std = statistics.pstdev(values.values())
        return {
            name: ((value - mean) / std if std > 1e-12 else 0.0)
            for name, value in values.items()
        }

    z6, z12 = zscores(0), zscores(1)
    ranked = sorted(
        components,
        key=lambda name: (0.5 * z6[name] + 0.5 * z12[name], name),
    )
    rank_directions = {
        **{name: "down" for name in ranked[:2]},
        **{name: "up" for name in ranked[-2:]},
    }
    for name, (_, _, pct_above, evaluated, required) in components.items():
        rank_direction = rank_directions.get(name, "none")
        direction = "none"
        if rank_direction == "up" and pct_above >= CONFIRMATION_BREADTH_THRESHOLD:
            direction = "up"
        elif rank_direction == "down" and pct_above <= 1.0 - CONFIRMATION_BREADTH_THRESHOLD:
            direction = "down"
        states[name] = {
            **states[name],
            "ready": True,
            "direction": direction,
            "rank_direction": rank_direction,
            "pct_above_50ma": round(pct_above, 6),
            "members_evaluated": evaluated,
            "members_required": required,
            "as_of": str(sessions[-1]),
        }
    return {
        "policy_version": SECTOR_CONFIRMATION_POLICY_VERSION,
        "as_of": str(sessions[-1]),
        "groups": states,
    }


def build_prediction_model(
    groups: dict[str, list[str]],
    bars_by_symbol: dict[str, list[dict]],
    horizons: tuple[int, ...] = DEFAULT_HORIZONS,
) -> dict:
    """以多檔多年日線建立條件機率表，不做任何網路或檔案 I/O。"""
    filtered_groups = {
        name: [
            s for s in symbols
            if not str(s).upper().endswith((".TW", ".TWO")) and s in bars_by_symbol
        ]
        for name, symbols in groups.items()
    }
    sector_by_date = _sector_states(filtered_groups, bars_by_symbol)
    samples: dict[tuple[str, int], list[tuple[str, bool]]] = defaultdict(list)
    baseline: dict[int, list[tuple[str, bool]]] = {h: [] for h in horizons}
    all_dates: set[str] = set()

    for group, symbols in filtered_groups.items():
        group_states = sector_by_date.get(group, {})
        for symbol in symbols:
            bars = sorted(
                [b for b in bars_by_symbol.get(symbol, []) if b.get("date") and b.get("close") is not None],
                key=lambda b: str(b["date"]),
            )
            closes: list[float] = []
            for i, bar in enumerate(bars):
                close = float(bar["close"])
                closes.append(close)
                if len(closes) < 60:
                    continue
                ma30 = sum(closes[-30:]) / 30.0
                ma60 = sum(closes[-60:]) / 60.0
                streak = signed_streak(closes)
                candle = candle_pattern(
                    bar.get("open"), bar.get("high"), bar.get("low"), close
                )
                date = str(bar["date"])
                state = _feature_state(
                    close, ma30, ma60, streak, candle,
                    group_states.get(date, "none"),
                )
                key = _pattern_key(state)
                for h in horizons:
                    if i + h >= len(bars):
                        continue
                    future = float(bars[i + h]["close"])
                    if abs(future - close) <= 1e-12:
                        continue
                    up = future > close
                    samples[(key, h)].append((date, up))
                    baseline[h].append((date, up))
                    all_dates.add(date)

    patterns: dict[str, dict[str, dict]] = {}
    for (key, h), rows in samples.items():
        ordered = sorted(rows, key=lambda x: x[0])
        # 以「日期」切時間前後半，不能用列數硬切；同一天多檔個股不得被拆到兩側，
        # 否則所謂穩定性會把同一市場事件同時算成訓練前段與驗證後段。
        unique_dates = sorted({date for date, _ in ordered})
        split_date = unique_dates[len(unique_dates) // 2] if unique_dates else ""
        early = [row for row in ordered if row[0] < split_date]
        late = [row for row in ordered if row[0] >= split_date]

        def _rate(part: list[tuple[str, bool]]) -> Optional[float]:
            return sum(up for _, up in part) / len(part) if part else None

        patterns.setdefault(key, {})[str(h)] = {
            "n": len(rows),
            "up_rate": round(_rate(rows) or 0.0, 6),
            "distinct_dates": len({d for d, _ in rows}),
            "early_up_rate": round(_rate(early), 6) if early else None,
            "late_up_rate": round(_rate(late), 6) if late else None,
        }

    baseline_out = {
        str(h): {
            "n": len(rows),
            "up_rate": round(sum(up for _, up in rows) / len(rows), 6) if rows else None,
            "distinct_dates": len({d for d, _ in rows}),
        }
        for h, rows in baseline.items()
    }
    dates = sorted(all_dates)
    return {
        "version": MODEL_VERSION,
        "horizons": list(horizons),
        "first_date": dates[0] if dates else None,
        "last_date": dates[-1] if dates else None,
        "symbols_evaluated": len({
            s for symbols in filtered_groups.values() for s in symbols
        }),
        "patterns": patterns,
        "baseline": baseline_out,
        "num_tests": max(1, len(patterns) * len(horizons) * 2),
        "sector_confirmation": build_relative_momentum_breadth_confirmation(
            groups, bars_by_symbol
        ),
    }


_MA_LABELS = {
    "bullish": "收盤 > 30MA > 60MA（多頭排列）",
    "bearish": "收盤 < 30MA < 60MA（空頭排列）",
    "ma30_above": "30MA > 60MA、但收盤跌破 30MA",
    "ma30_below": "30MA < 60MA、但收盤站上 30MA",
}
_CANDLE_LABELS = {
    "upper_wick": "明顯上引線",
    "lower_wick": "明顯下引線",
    "neutral": "無顯著影線",
}


def _streak_label(streak: int) -> str:
    if streak > 0:
        return f"連漲 {streak} 日"
    if streak < 0:
        return f"連跌 {abs(streak)} 日"
    return "當日平盤"


def _sector_label(direction: str, flow: Optional[dict] = None) -> str:
    label = {
        "up": "板塊已達持續普漲共識",
        "down": "板塊已達持續普跌共識",
    }.get(direction, "板塊目前無持續性共識")
    if direction in ("up", "down") and flow:
        days = int(flow.get("days_evaluated") or 0)
        same = int(flow.get("up_days" if direction == "up" else "down_days") or 0)
        if days and same:
            label += f"（近 {days} 日有 {same} 日同向）"
    return label


def compute_prediction_signals(
    groups: dict[str, list[str]],
    summaries: dict[str, dict],
    flows: dict[str, dict],
    model: Optional[dict],
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_edge: float = DEFAULT_MIN_EDGE,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> tuple[PredictionSignal, ...]:
    """Return the accepted claims consumed by both the UI and Experiment Engine.

    This is intentionally presentation-free.  Thresholds, stability checks and
    multiple-test-adjusted significance live here once, so a displayed forecast
    cannot silently diverge from the forecast that is later settled.
    """
    return evaluate_prediction_cells(
        groups,
        summaries,
        flows,
        model,
        min_samples=min_samples,
        min_edge=min_edge,
        min_confidence=min_confidence,
    )[0]


def compute_prediction_rejections(
    groups: dict[str, list[str]],
    summaries: dict[str, dict],
    flows: dict[str, dict],
    model: Optional[dict],
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_edge: float = DEFAULT_MIN_EDGE,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> tuple[PredictionRejection, ...]:
    """Return one record per (group, symbol, horizon) that emitted no signal.

    Without this the ledger records a single ``prediction_thresholds_not_met``
    for six structurally different reasons, and "is the threshold too high?"
    becomes unanswerable from stored evidence — it has to be reconstructed by
    re-running the model offline.  Two of the six gates (``stability`` and
    ``significance``) have no parameter at all, so a bare "thresholds not met"
    actively misleads: it implies a knob exists where none does.
    """
    return evaluate_prediction_cells(
        groups,
        summaries,
        flows,
        model,
        min_samples=min_samples,
        min_edge=min_edge,
        min_confidence=min_confidence,
    )[1]


def evaluate_prediction_cells(
    groups: dict[str, list[str]],
    summaries: dict[str, dict],
    flows: dict[str, dict],
    model: Optional[dict],
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_edge: float = DEFAULT_MIN_EDGE,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> tuple[tuple[PredictionSignal, ...], tuple[PredictionRejection, ...]]:
    """Run every cell once and report both the accepts and the rejects.

    Both come from the same traversal on purpose: a rejection reason derived
    from a second, separate pass could disagree with the decision actually
    taken, which is the precise class of divergence this engine exists to stop.
    """
    if not model or model.get("version") != MODEL_VERSION:
        return (), ()
    from .backtest_stats import direction_significance

    patterns = model.get("patterns") or {}
    baselines = model.get("baseline") or {}
    num_tests = int(model.get("num_tests") or 1)
    model_horizons = tuple(
        int(value) for value in (model.get("horizons") or DEFAULT_HORIZONS)
    )
    signals: list[PredictionSignal] = []
    rejections: list[PredictionRejection] = []

    def reject(
        group: str,
        symbol: str,
        horizons: Sequence[int],
        gate: str,
        **observed: Any,
    ) -> None:
        for horizon in horizons:
            rejections.append(
                PredictionRejection(
                    group=group,
                    symbol=symbol,
                    horizon_sessions=int(horizon),
                    gate=gate,
                    tunable=gate in TUNABLE_REJECTION_GATES,
                    observed=dict(observed),
                )
            )

    for group, configured_symbols in groups.items():
        summary = summaries.get(group) or {}
        current_by_symbol = {
            str(member.get("symbol")): member
            for member in (summary.get("members") or [])
            if member.get("symbol")
        }
        flow = flows.get(group) or {}
        sector_state = flow.get("direction") if flow.get("ready") else "none"
        if sector_state not in ("up", "down"):
            sector_state = "none"

        for symbol in configured_symbols:
            member = current_by_symbol.get(symbol)
            if not member:
                reject(group, symbol, model_horizons, "member_absent")
                continue
            close = member.get("price")
            ma30 = member.get("ma30")
            ma60 = member.get("ma60")
            if close is None or ma30 is None or ma60 is None:
                reject(
                    group,
                    symbol,
                    model_horizons,
                    "member_incomplete",
                    has_price=close is not None,
                    has_ma30=ma30 is not None,
                    has_ma60=ma60 is not None,
                )
                continue
            streak = int(member.get("streak") or 0)
            candle = member.get("candle_pattern") or candle_pattern(
                member.get("open"),
                member.get("high"),
                member.get("low"),
                close,
            )
            state = _feature_state(
                float(close),
                float(ma30),
                float(ma60),
                streak,
                candle,
                sector_state,
            )
            rows = patterns.get(_pattern_key(state)) or {}

            for raw_horizon in model.get("horizons") or DEFAULT_HORIZONS:
                horizon = int(raw_horizon)
                stat = rows.get(str(horizon)) or {}
                baseline_up = (baselines.get(str(horizon)) or {}).get("up_rate")
                sample_size = int(stat.get("n") or 0)
                probability_up = stat.get("up_rate")
                if probability_up is None or baseline_up is None:
                    reject(
                        group,
                        symbol,
                        (horizon,),
                        "pattern_cell_empty",
                        pattern=_pattern_key(state),
                        sample_size=sample_size,
                    )
                    continue
                if sample_size < min_samples:
                    reject(
                        group,
                        symbol,
                        (horizon,),
                        "min_samples",
                        pattern=_pattern_key(state),
                        sample_size=sample_size,
                        threshold=min_samples,
                    )
                    continue
                probability_up = float(probability_up)
                baseline_up = float(baseline_up)
                direction = "up" if probability_up >= baseline_up else "down"
                direction_probability = (
                    probability_up if direction == "up" else 1.0 - probability_up
                )
                baseline_direction_probability = (
                    baseline_up if direction == "up" else 1.0 - baseline_up
                )
                edge = direction_probability - baseline_direction_probability
                if edge < min_edge:
                    reject(
                        group,
                        symbol,
                        (horizon,),
                        "min_edge",
                        pattern=_pattern_key(state),
                        direction=direction,
                        edge=round(edge, 6),
                        threshold=min_edge,
                        sample_size=sample_size,
                    )
                    continue

                early = stat.get("early_up_rate")
                late = stat.get("late_up_rate")
                stable = (
                    early is not None
                    and late is not None
                    and (
                        (early > baseline_up and late > baseline_up)
                        if direction == "up"
                        else (early < baseline_up and late < baseline_up)
                    )
                )
                if not stable:
                    # No parameter governs this gate.  It is the single largest
                    # filter in the model (roughly half of all cells once the
                    # numeric thresholds are relaxed), so recording it as a
                    # "threshold" would misattribute abstention to a knob.
                    reject(
                        group,
                        symbol,
                        (horizon,),
                        "stability",
                        pattern=_pattern_key(state),
                        direction=direction,
                        early_up_rate=early,
                        late_up_rate=late,
                        baseline_up_rate=baseline_up,
                    )
                    continue

                significance = direction_significance(
                    sample_size,
                    direction_probability,
                    baseline_direction_probability,
                    horizon=horizon,
                    num_tests=num_tests,
                    distinct_dates=(
                        int(stat.get("distinct_dates") or 0) or None
                    ),
                )
                if not significance:
                    reject(
                        group,
                        symbol,
                        (horizon,),
                        "significance",
                        pattern=_pattern_key(state),
                        direction=direction,
                        edge=round(edge, 6),
                        sample_size=sample_size,
                        num_tests=num_tests,
                    )
                    continue
                confidence = max(
                    0.0,
                    min(99.0, (1.0 - significance["p_value"]) * 100.0),
                )
                if confidence <= min_confidence:
                    reject(
                        group,
                        symbol,
                        (horizon,),
                        "min_confidence",
                        pattern=_pattern_key(state),
                        direction=direction,
                        confidence=round(confidence, 4),
                        threshold=min_confidence,
                        edge=round(edge, 6),
                        sample_size=sample_size,
                    )
                    continue
                signals.append(
                    PredictionSignal(
                        group=group,
                        symbol=symbol,
                        horizon_sessions=horizon,
                        direction=direction,
                        probability_up=probability_up,
                        direction_probability=direction_probability,
                        baseline_probability_up=baseline_up,
                        baseline_direction_probability=(
                            baseline_direction_probability
                        ),
                        edge=edge,
                        confidence=confidence,
                        sample_size=sample_size,
                        significance=significance,
                        ma_state=state[0],
                        candle_pattern=candle,
                        streak=streak,
                        sector_state=sector_state,
                    )
                )

    return tuple(signals), tuple(rejections)


def generate_prediction_recommendations(
    groups: dict[str, list[str]],
    summaries: dict[str, dict],
    flows: dict[str, dict],
    model: Optional[dict],
    min_samples: int = DEFAULT_MIN_SAMPLES,
    min_edge: float = DEFAULT_MIN_EDGE,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> list:
    """將目前條件投影成 Recommendation；無顯著變化的個股／前瞻期直接略過。"""
    from .shared import Recommendation, _section

    signals = compute_prediction_signals(
        groups,
        summaries,
        flows,
        model,
        min_samples=min_samples,
        min_edge=min_edge,
        min_confidence=min_confidence,
    )
    by_target: dict[tuple[str, str], list[PredictionSignal]] = defaultdict(list)
    for signal in signals:
        by_target[(signal.group, signal.symbol)].append(signal)
    candidates: list[tuple[float, float, Recommendation]] = []

    for (group, symbol), accepted in by_target.items():
        flow = flows.get(group) or {}
        accepted.sort(key=lambda signal: signal.horizon_sessions)
        first = accepted[0]
        horizon_text = "｜".join(
            f"+{signal.horizon_sessions}日"
            f"{'上漲' if signal.direction == 'up' else '下跌'} "
            f"{signal.direction_probability * 100:.0f}%"
            f"（信心 {signal.confidence:.0f}%）"
            for signal in accepted
        )
        directions = {signal.direction for signal in accepted}
        rec_direction = (
            "多" if directions == {"up"}
            else "空" if directions == {"down"}
            else "觀望"
        )
        condition_text = (
            f"{_sector_label(first.sector_state, flow)}；"
            f"{_streak_label(first.streak)}；"
            f"{_MA_LABELS.get(first.ma_state, first.ma_state)}；"
            f"{_CANDLE_LABELS.get(first.candle_pattern, first.candle_pattern)}"
        )
        detail_lines = []
        for signal in accepted:
            significance = signal.significance
            detail_lines.append(
                f"+{signal.horizon_sessions}日："
                f"{'上漲' if signal.direction == 'up' else '下跌'}"
                f"{signal.direction_probability * 100:.1f}%"
                f"（無條件基準 "
                f"{signal.baseline_direction_probability * 100:.1f}%，"
                f"差 {signal.edge * 100:+.1f}pp，n={signal.sample_size}，"
                f"ESS={significance['ess']}，"
                f"95%CI {significance['ci_lo'] * 100:.0f}–"
                f"{significance['ci_hi'] * 100:.0f}%，"
                f"p={significance['p_value']:.3f}）"
            )
        rec = Recommendation(
            rec_id=f"sector-predictive:{group}:{symbol}",
            category="sector_predictive",
            direction=rec_direction,
            verdict=f"🔮 【1–3日條件機率】{group}／{symbol}：{horizon_text}",
            basis=f"目前條件：{condition_text}。只列相對基準有變化且前後期一致者。",
            detail_sections=[
                _section(
                    "目前條件（板塊＋個股）",
                    formula=(
                        "條件 = 板塊近5日持續性廣度狀態 × 個股收盤/30MA/60MA排列 "
                        "× 上下影線型態 × 連漲跌級距"
                    ),
                    substitution=condition_text,
                    explanation=(
                        "板塊某日普漲／普跌須成分股廣度與等權報酬同向，近5日達3日才成立；"
                        "歷史板塊報酬採等權，避免把今日市值回填到歷史造成前視偏誤。"
                    ),
                ),
                _section(
                    "1–3 日條件機率與信心水準",
                    formula=(
                        "條件機率 = 歷史相同條件後同方向次數 ÷ 樣本數；"
                        "信心水準 = 1 − 單尾二項檢定 p 值（相對無條件基準）；"
                        "ESS = min(n/h, 不同訊號日期數/h)"
                    ),
                    substitution="\n".join(detail_lines),
                    explanation=(
                        "機率回答下一次方向的歷史頻率；信心水準回答該差異不像隨機波動的程度，"
                        "兩者不可互換。僅採 n≥30、差異≥3pp、信心>60%，且前後半段方向一致的項目。"
                    ),
                ),
                _section(
                    "限制",
                    explanation=(
                        "這是條件式歷史統計，不保證下一次必然同向；未計交易成本、財報突發事件"
                        "與盤中尚未收完的 K 棒。未通過門檻的條件完全不顯示。"
                    ),
                ),
            ],
        )
        candidates.append((
            max(signal.confidence for signal in accepted),
            max(signal.edge for signal in accepted),
            rec,
        ))

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [rec for _, _, rec in candidates]
