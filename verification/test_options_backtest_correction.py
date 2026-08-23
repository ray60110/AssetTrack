from __future__ import annotations

from datetime import date
import unittest
from unittest.mock import patch

from assettrack import calibration
from assettrack import calibration_schedule
from assettrack.calibration_schedule import default_params, propose_adjustments
from assettrack.greeks import bs_price
from assettrack.options_analysis import (
    _repricing_decomp,
    compute_observed_regime,
    generate_grouped_analysis_card,
)


def _contract(
    symbol: str,
    *,
    option_type: str = "call",
    strike: float = 100.0,
    expiry: str = "2026-09-18",
    bid: float,
    ask: float,
    last: float,
    iv: float = 0.30,
) -> dict:
    return {
        "contractSymbol": symbol,
        "type": option_type,
        "strike": strike,
        "expiry": expiry,
        "bid": bid,
        "ask": ask,
        "lastPrice": last,
        "lastTradeDate": "2026-07-30 19:30:00+00:00",
        "openInterest": 1000,
        "impliedVolatility": iv,
    }


class OptionsBacktestCorrectionTests(unittest.TestCase):
    def test_detailed_degraded_card_stops_actionable_direction_and_explains_fix(self):
        verdict = {
            "ready": True,
            "direction": "多",
            "bias": 0.25,
            "call_pct": 75.0,
            "skew_score": 1,
            "bias_score": 1,
            "bias_n": 4,
            "conflict": False,
            "skew_unconfirmed": False,
            "near_earnings": False,
            "first_date": "2026-07-01",
            "last_date": "2026-07-31",
        }
        backtest = {
            "model_health": {
                "status": "degraded",
                "reason": "偏多分支最近三個 session 連續失配",
                "by_direction": {
                    "up": {
                        "status": "degraded",
                        "reason": "偏多分支最近三個 session 連續失配",
                    }
                },
            },
            "by_horizon": {
                5: {
                    "significance": {"up": {"significant_adj": True}},
                }
            },
            "probability_backtest": {
                "min_samples": 20,
                "by_horizon": {
                    5: {
                        "up": {
                            "n": 30,
                            "raw_n": 90,
                            "next_probability": 0.75,
                            "baseline_probability": 0.52,
                            "hit_rate": 0.67,
                            "baseline_hit_rate": 0.52,
                            "brier_skill": 0.08,
                        }
                    }
                },
            },
            "stability": {"consistent": True},
            "total_snapshot_days": 30,
        }

        with patch(
            "assettrack.calibration.backtest_verdicts",
            return_value=backtest,
        ):
            lines = generate_grouped_analysis_card(
                {"verdicts": {"AMD": verdict}},
                {"events": []},
                {"events": []},
                {"AMD": []},
            )

        rendered = "\n".join(lines)
        self.assertIn("⚠️ ⚪ 觀望", rendered)
        self.assertIn("模型原始預測 +5 個市場 session 上漲", rendered)
        self.assertIn("回測未通過", rendered)
        self.assertIn("偏多分支最近三個 session 連續失配", rendered)
        self.assertIn("如何修改", rendered)
        self.assertIn("bias_min_pct", rendered)

    def test_repricing_uses_executable_mid_not_stale_last_price(self):
        """A stale last trade must not manufacture a directional repricing residual."""
        d0, d1 = "2026-07-29", "2026-07-30"
        spot = 100.0
        p0 = bs_price(spot, 100.0, 51, 0.30, "call", r=0.04)
        p1 = bs_price(spot, 100.0, 50, 0.30, "call", r=0.04)
        c0 = _contract("TEST", bid=p0 - 0.01, ask=p0 + 0.01, last=p0 * 2)
        c1 = _contract("TEST", bid=p1 - 0.01, ask=p1 + 0.01, last=p1 * 0.2)

        out = _repricing_decomp(c0, c1, d0, d1, spot, spot, r=0.04)

        self.assertIsNotNone(out)
        self.assertEqual(out["price_source"], "mid")
        self.assertAlmostEqual(out["residual"], 0.0, places=9)

    def test_market_session_normalisation_deduplicates_weekend_captures(self):
        raw = [
            {
                "date": "2026-07-24",
                "spot_price": 100.0,
                "contracts": [_contract("A", bid=1.0, ask=1.1, last=1.05)],
            },
            {
                "date": "2026-07-26",
                "spot_price": 100.0,
                "contracts": [
                    {
                        **_contract("A", bid=1.0, ask=1.1, last=1.05),
                        "lastTradeDate": "2026-07-24 19:30:00+00:00",
                    }
                ],
            },
            {
                "date": "2026-07-27",
                "spot_price": 99.0,
                "contracts": [
                    {
                        **_contract("A", bid=1.2, ask=1.3, last=1.25),
                        "lastTradeDate": "2026-07-27 19:30:00+00:00",
                    }
                ],
            },
        ]

        out = calibration.normalise_option_snapshots(raw)

        self.assertEqual([s["date"] for s in out], ["2026-07-24", "2026-07-27"])
        self.assertEqual(out[0]["captured_date"], "2026-07-26")

    def test_forward_horizon_counts_market_sessions_not_calendar_days(self):
        self.assertEqual(
            calibration.trading_sessions_between(date(2026, 7, 24), date(2026, 7, 27)),
            1,
        )
        self.assertEqual(
            calibration.trading_sessions_between(date(2026, 7, 24), date(2026, 7, 31)),
            5,
        )

    def test_forward_horizon_excludes_exchange_holidays(self):
        # 2026-07-03 is the observed Independence Day market closure.
        self.assertEqual(
            calibration.trading_sessions_between(date(2026, 7, 2), date(2026, 7, 6)),
            1,
        )

    def test_backtest_does_not_stretch_missing_truth_to_a_longer_horizon(self):
        report = calibration.backtest_verdicts(
            {
                "TEST": [
                    {"date": "2026-07-01", "session_date": "2026-07-01", "spot_price": 100.0, "contracts": []},
                    {"date": "2026-07-06", "session_date": "2026-07-06", "spot_price": 110.0, "contracts": []},
                ]
            },
            horizons=(1,),
        )

        # 7/2 is the exact +1 session.  The next available snapshot is +2
        # sessions (7/3 is a holiday), so it must not be relabelled as +1.
        self.assertEqual(report["by_horizon"][1]["baseline_n"], 0)

    def test_backtest_passes_active_option_parameters(self):
        seen = []
        original = calibration.compute_directional_verdicts

        def fake_verdicts(_snapshots, **kwargs):
            seen.append(kwargs)
            return {"verdicts": {"ZZTEST": {"direction": "空"}}}

        calibration.compute_directional_verdicts = fake_verdicts
        self.addCleanup(
            lambda: setattr(calibration, "compute_directional_verdicts", original)
        )
        snaps = {
            "ZZTEST": [
                {"date": "2026-07-29", "spot_price": 100.0, "contracts": []},
                {"date": "2026-07-30", "spot_price": 90.0, "contracts": []},
            ]
        }

        calibration.backtest_verdicts(
            snaps,
            horizons=(1,),
            verdict_params={"bias_min_pct": 0.07},
        )

        self.assertTrue(seen)
        self.assertTrue(all(call["bias_min_pct"] == 0.07 for call in seen))

    def test_options_model_enters_calibration_proposal_when_degraded(self):
        active = default_params()
        report = {
            "model_health": {
                "status": "degraded",
                "reason": "最近已結算訊號連續失配",
                "recent_n": 8,
                "recent_hit_rate": 0.25,
            },
            "by_horizon": {},
        }

        proposals = propose_adjustments(active, {"options": report})

        option_change = next(p for p in proposals if p["family"] == "options")
        self.assertEqual(option_change["param"], "bias_min_pct")
        self.assertEqual(option_change["action"], "tighten")
        self.assertGreater(option_change["to"], option_change["from"])

    def test_degraded_health_intervenes_before_regular_cadence(self):
        state = {
            "active_params": default_params(),
            "last_calibrated": "2026-07-30",
            "cadence_days": 14,
            "pending": None,
            "history": [],
            "last_health_intervention": {},
        }
        report = {
            "model_health": {
                "status": "degraded",
                "reason": "偏多分支連續失配",
                "horizon": 1,
                "recent_n": 3,
                "recent_hit_rate": 0.0,
                "miss_streak": 3,
            },
            "by_horizon": {},
        }
        old_ensure = calibration_schedule.ensure_state
        old_save = calibration_schedule.save_calibration_state
        calibration_schedule.ensure_state = lambda _user: state
        calibration_schedule.save_calibration_state = lambda _user, _state: None
        self.addCleanup(
            lambda: setattr(calibration_schedule, "ensure_state", old_ensure)
        )
        self.addCleanup(
            lambda: setattr(calibration_schedule, "save_calibration_state", old_save)
        )

        updated = calibration_schedule.run_recalibration(
            "user",
            {"options": report},
            date(2026, 7, 30),
        )

        self.assertIsNotNone(updated["pending"])
        self.assertEqual(updated["pending"]["changes"][0]["family"], "options")

    def test_failed_bullish_branch_is_not_masked_by_successful_bearish_branch(self):
        records = []
        for idx, session in enumerate(("2026-07-27", "2026-07-28", "2026-07-29")):
            records.append({"date": session, "h": 1, "dir": "up", "hit": False})
            records.append({"date": session, "h": 1, "dir": "down", "hit": True})

        health = calibration.assess_model_health(records)

        self.assertEqual(health["status"], "degraded")
        self.assertEqual(health["by_direction"]["up"]["status"], "degraded")
        self.assertEqual(health["by_direction"]["down"]["status"], "healthy")

    def test_observed_regime_is_separate_and_reports_broad_decline(self):
        snapshots = {}
        for idx in range(8):
            end = 90.0 if idx < 6 else 101.0
            snapshots[f"S{idx}"] = [
                {"date": "2026-07-24", "spot_price": 100.0, "contracts": []},
                {"date": "2026-07-30", "spot_price": end, "contracts": []},
            ]

        regime = compute_observed_regime(snapshots)

        self.assertEqual(regime["state"], "down")
        self.assertEqual(regime["down_count"], 6)
        self.assertEqual(regime["ready_count"], 8)


if __name__ == "__main__":
    unittest.main()
