from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from assettrack.sector_predictive import (
    build_relative_momentum_breadth_confirmation,
    build_prediction_model,
    candle_pattern,
    compute_prediction_signals,
    detect_current_sector_state,
    generate_prediction_recommendations,
    latest_member_features,
    ma_pattern,
    signed_streak,
)


def test_relative_momentum_breadth_confirmation_ranks_and_gates_six_groups():
    dates = pd.date_range("2024-01-02", periods=300, freq="B")
    groups = {f"G{i}": [f"S{i}{j}" for j in range(4)] for i in range(6)}
    bars_by = {
        "QQQ": [
            {"date": date.strftime("%Y-%m-%d"), "close": 100.0}
            for date in dates
        ]
    }
    for group_i, members in enumerate(groups.values()):
        daily = 0.9990 + group_i * 0.0004
        for symbol in members:
            bars_by[symbol] = [
                {
                    "date": date.strftime("%Y-%m-%d"),
                    "close": 100.0 * (daily ** offset),
                }
                for offset, date in enumerate(dates)
            ]

    result = build_relative_momentum_breadth_confirmation(groups, bars_by)

    assert result["groups"]["G5"]["direction"] == "up"
    assert result["groups"]["G4"]["direction"] == "up"
    assert result["groups"]["G0"]["direction"] == "down"
    assert result["groups"]["G1"]["direction"] == "down"
    assert result["groups"]["G2"]["direction"] == "none"
    assert result["groups"]["G3"]["direction"] == "none"
    assert result["groups"]["G5"]["trend_direction"] == "up"
    assert result["groups"]["G3"]["trend_direction"] == "up"
    assert result["groups"]["G0"]["trend_direction"] == "down"
    assert result["groups"]["G2"]["trend_direction"] == "down"
    assert result["groups"]["G5"]["trend_ready"] is True
    assert result["groups"]["G5"]["sma5"] > result["groups"]["G5"]["sma150"]
    assert result["groups"]["G0"]["fast_trend_ready"] is True
    assert result["groups"]["G0"]["fast_trend_direction"] == "down"
    assert result["groups"]["G0"]["sma5"] < result["groups"]["G0"]["sma20"]
    assert result["groups"]["G5"]["fast_trend_direction"] == "up"
    assert result["groups"]["G5"]["sma5"] > result["groups"]["G5"]["sma20"]


def _current_inputs():
    groups = {"CPU": ["AMD"]}
    summaries = {
        "CPU": {
            "members": [{
                "symbol": "AMD",
                "price": 120.0,
                "ma30": 110.0,
                "ma60": 100.0,
                "streak": 4,
                "open": 119.0,
                "high": 130.0,
                "low": 118.0,
                "candle_pattern": "upper_wick",
            }]
        }
    }
    flows = {
        "CPU": {
            "ready": True, "direction": "up",
            "up_days": 4, "down_days": 0, "days_evaluated": 5,
        }
    }
    return groups, summaries, flows


def _model(up_rate: float = 0.70, early: float = 0.68, late: float = 0.72):
    rows = {
        str(h): {
            "n": 240,
            "up_rate": up_rate,
            "distinct_dates": 220,
            "early_up_rate": early,
            "late_up_rate": late,
        }
        for h in (1, 2, 3)
    }
    return {
        "version": 1,
        "horizons": [1, 2, 3],
        "first_date": "2021-01-01",
        "last_date": "2026-01-01",
        "num_tests": 1,
        "baseline": {
            str(h): {"n": 1000, "up_rate": 0.50, "distinct_dates": 900}
            for h in (1, 2, 3)
        },
        "patterns": {"bullish|upper_wick|up3plus|up": rows},
    }


def test_current_stock_features_include_ma_wick_and_streak():
    closes = [float(i) for i in range(1, 65)]
    bars = [
        {
            "date": f"2026-01-{(i % 28) + 1:02d}",
            "open": close - 0.2,
            "high": close + 0.3,
            "low": close - 0.4,
            "close": close,
        }
        for i, close in enumerate(closes)
    ]
    bars[-1].update({"open": 63.8, "high": 70.0, "low": 63.7, "close": 64.0})

    features = latest_member_features(bars)

    assert features["ma30"] is not None
    assert features["ma60"] is not None
    assert features["streak"] == 63
    assert features["candle_pattern"] == "upper_wick"
    assert ma_pattern(120, 110, 100) == "bullish"
    assert signed_streak([10, 9, 8, 7]) == -3
    assert candle_pattern(10, 10.2, 7, 9.9) == "lower_wick"


def test_model_rebuilds_one_two_three_day_forward_samples():
    groups = {"G": ["AAA", "BBB", "CCC"]}
    bars_by = {}
    for offset, symbol in enumerate(groups["G"]):
        bars = []
        price = 100.0 + offset
        for i in range(130):
            price *= 1.002 if i % 7 not in (5, 6) else 0.998
            bars.append({
                "date": f"2025-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}",
                "open": price * 0.998,
                "high": price * 1.004,
                "low": price * 0.996,
                "close": price,
            })
        bars_by[symbol] = bars

    model = build_prediction_model(groups, bars_by)

    assert model["horizons"] == [1, 2, 3]
    assert model["symbols_evaluated"] == 3
    assert model["patterns"]
    assert all(str(h) in model["baseline"] for h in (1, 2, 3))
    assert model["last_date"] is not None


def test_current_sector_state_uses_same_equal_weight_persistence_as_model():
    snapshots = [
        {
            "date": f"2026-01-0{i}",
            "members": [
                {"day_pct": 2.0}, {"day_pct": 1.0}, {"day_pct": 0.5},
                {"day_pct": -0.1},
            ],
        }
        for i in range(1, 5)
    ]

    state = detect_current_sector_state(snapshots)

    assert state["ready"] is True
    assert state["direction"] == "up"
    assert state["up_days"] == 4
    assert state["weighting"] == "equal"


def test_recommendation_displays_probability_confidence_and_current_streak():
    groups, summaries, flows = _current_inputs()

    recs = generate_prediction_recommendations(
        groups, summaries, flows, _model()
    )

    assert len(recs) == 1
    text = recs[0].verdict + recs[0].basis
    assert "+1日上漲 70%" in text
    assert "+2日上漲 70%" in text
    assert "+3日上漲 70%" in text
    assert "信心" in text
    assert "連漲 4 日" in text
    assert "30MA > 60MA" in text
    assert "上引線" in text
    assert "板塊已達持續普漲共識" in text
    assert "近 5 日有 4 日同向" in text


def test_structured_signals_are_the_shared_source_for_each_forward_horizon():
    groups, summaries, flows = _current_inputs()

    signals = compute_prediction_signals(groups, summaries, flows, _model())

    assert [signal.horizon_sessions for signal in signals] == [1, 2, 3]
    assert all(signal.group == "CPU" for signal in signals)
    assert all(signal.symbol == "AMD" for signal in signals)
    assert all(signal.direction == "up" for signal in signals)
    assert all(signal.probability_up == 0.70 for signal in signals)
    assert all(signal.direction_probability == 0.70 for signal in signals)
    assert all(signal.baseline_probability_up == 0.50 for signal in signals)
    assert all(abs(signal.edge - 0.20) < 1e-12 for signal in signals)
    assert all(signal.ma_state == "bullish" for signal in signals)
    assert all(signal.sector_state == "up" for signal in signals)


def test_structured_down_signal_keeps_probability_up_semantics():
    groups, summaries, flows = _current_inputs()

    signals = compute_prediction_signals(
        groups,
        summaries,
        flows,
        _model(up_rate=0.30, early=0.32, late=0.28),
    )

    assert signals[0].direction == "down"
    assert signals[0].probability_up == 0.30
    assert signals[0].direction_probability == 0.70


def test_no_material_change_or_unstable_pattern_uses_no_space():
    groups, summaries, flows = _current_inputs()

    assert generate_prediction_recommendations(
        groups, summaries, flows, _model(up_rate=0.51, early=0.51, late=0.51)
    ) == []
    assert generate_prediction_recommendations(
        groups, summaries, flows, _model(up_rate=0.70, early=0.72, late=0.45)
    ) == []


def test_down_probability_is_reported_as_down_not_inverse_up_wording():
    groups, summaries, flows = _current_inputs()
    model = _model(up_rate=0.30, early=0.32, late=0.28)

    recs = generate_prediction_recommendations(groups, summaries, flows, model)

    assert len(recs) == 1
    assert "+1日下跌 70%" in recs[0].verdict
    assert recs[0].direction == "空"


def test_cached_model_is_scoped_to_group_universe_and_failed_fetch_is_throttled():
    from assettrack.storage import (
        load_sector_predictive_model,
        mark_sector_predictive_attempt,
        save_sector_predictive_cache,
        sector_predictive_cache_needs_refresh,
    )

    groups = {"CPU": ["AMD"]}
    with tempfile.TemporaryDirectory() as tmp, patch(
        "assettrack.storage.get_sector_config_dir", return_value=Path(tmp)
    ):
        save_sector_predictive_cache("u", groups, _model())
        assert load_sector_predictive_model("u", groups) is not None
        # Legacy caches without the dual-confirmation payload must rebuild once.
        assert sector_predictive_cache_needs_refresh("u", groups) is True

        legacy_confirmation = _model() | {
            "sector_confirmation": {"as_of": "2026-08-14", "groups": {}}
        }
        save_sector_predictive_cache("u", groups, legacy_confirmation)
        assert sector_predictive_cache_needs_refresh("u", groups) is True
        assert load_sector_predictive_model("u", {"CPU": ["INTC"]}) is None

        changed = {"CPU": ["INTC"]}
        assert sector_predictive_cache_needs_refresh("u", changed) is True
        mark_sector_predictive_attempt("u", changed)
        assert sector_predictive_cache_needs_refresh("u", changed) is False


def test_predictive_refresh_excludes_intraday_bar_from_model_and_truth():
    from assettrack import tui

    summary = {"members": [{"symbol": "AMD", "price": 120.0}]}
    history = {
        "AMD": [
            {"date": "2026-07-29", "close": 118.0},
            {"date": "2026-07-30", "close": 120.0},
        ]
    }
    with (
        patch("assettrack.storage.load_sector_groups", return_value={"CPU": ["AMD"]}),
        patch("assettrack.storage.prune_sector_history"),
        patch("assettrack.storage.append_sector_daily_snapshot"),
        patch("assettrack.storage.save_sector_summaries_cache"),
        patch("assettrack.storage.sector_predictive_cache_needs_refresh", return_value=True),
        patch("assettrack.storage.mark_sector_predictive_attempt"),
        patch("assettrack.storage.save_sector_predictive_cache"),
        patch("assettrack.storage.us_session_date", return_value="2026-07-30"),
        patch("assettrack.storage.us_session_complete", return_value=False),
        patch("assettrack.storage.append_symbol_daily_adjusted_closes") as append_truth,
        patch("assettrack.quotes.fetch_sector_members_data", return_value={"AMD": {"price": 120.0}}),
        patch("assettrack.quotes.fetch_fx_rates", return_value={}),
        patch("assettrack.quotes.fetch_sector_prediction_bars", return_value=history),
        patch("assettrack.sector_analysis.summarize_group", return_value=summary),
        patch("assettrack.sector_predictive.build_prediction_model", return_value=_model()) as build,
    ):
        tui._fetch_and_cache_sector_groups("alice")

    persisted_rows = list(append_truth.call_args.args[1])
    assert persisted_rows == [("2026-07-29", 118.0)]
    assert build.call_args.args[1] == {
        "AMD": [{"date": "2026-07-29", "close": 118.0}]
    }


def test_live_sector_fetch_carries_ma_candle_and_streak_into_summary_fields():
    import pandas as pd

    from assettrack.quotes import fetch_sector_members_data

    index = pd.date_range("2026-01-01", periods=65, freq="B")
    closes = [100.0 + i for i in range(65)]
    frame = pd.DataFrame({
        "Open": [v - 0.2 for v in closes],
        "High": [v + 0.3 for v in closes],
        "Low": [v - 0.4 for v in closes],
        "Close": closes,
        "Volume": [1_000_000] * 65,
    }, index=index)
    frame.loc[index[-1], ["Open", "High", "Low", "Close"]] = [163.8, 170.0, 163.7, 164.0]

    class FastInfo:
        market_cap = 100_000_000
        currency = "USD"
        last_price = 164.0
        previous_close = 163.0

        def __getitem__(self, key):
            if key in ("market_cap", "marketCap"):
                return self.market_cap
            raise KeyError(key)

    class Ticker:
        fast_info = FastInfo()

    with patch("assettrack.quotes.yf.download", return_value=frame), patch(
        "assettrack.quotes.yf.Ticker", return_value=Ticker()
    ):
        member = fetch_sector_members_data(["AMD"])["AMD"]

    assert member["ma30"] is not None
    assert member["ma60"] is not None
    assert member["streak"] == 64
    assert member["candle_pattern"] == "upper_wick"
    assert member["open"] == 163.8
    assert member["high"] == 170.0
    assert member["month_pct"] == round((164.0 / 143.0 - 1.0) * 100, 2)
