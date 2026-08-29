from datetime import date, datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import pandas as pd

from assettrack.performance import (
    BenchmarkClose,
    PortfolioPerformanceTracker,
    YFinanceBenchmarkPrices,
)
from assettrack.models import CashPosition, Position


class FixedBenchmarkPrices:
    def closing_prices(self, symbols, as_of):
        return {
            "QQQ": BenchmarkClose(date(2026, 7, 24), 500),
            "VT": BenchmarkClose(date(2026, 7, 24), 125),
        }


class ScheduledBenchmarkPrices:
    prices = {
        date(2026, 7, 27): {"QQQ": (date(2026, 7, 24), 500), "VT": (date(2026, 7, 24), 125)},
        date(2026, 8, 2): {"QQQ": (date(2026, 7, 31), 550), "VT": (date(2026, 7, 31), 130)},
        date(2026, 8, 3): {"QQQ": (date(2026, 7, 31), 550), "VT": (date(2026, 7, 31), 130)},
        date(2026, 8, 10): {"QQQ": (date(2026, 8, 7), 600), "VT": (date(2026, 8, 7), 135)},
    }

    def closing_prices(self, symbols, as_of):
        values = self.prices[as_of.date()]
        return {
            symbol: BenchmarkClose(*values[symbol])
            for symbol in symbols
        }


class PortfolioPerformanceTrackingTests(unittest.TestCase):
    def test_new_account_can_opt_in_without_a_tracking_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = PortfolioPerformanceTracker(
                user="alice",
                data_dir=Path(tmp),
            )

            state = tracker.enable(
                enabled_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
                new_account=True,
            )

            self.assertTrue(state.enabled)
            self.assertFalse(state.has_tracking_gap)
            self.assertEqual(state.benchmarks, ("QQQ", "VT"))

            document = json.loads(tracker.path.read_text())
            self.assertTrue(
                document["userporfolioperf_trackingsys_toggle"]
            )
            self.assertIn("userportfolioperf_tracksys", document)
            self.assertEqual(document["usertotalAsset_tracking"], [])

    def test_enabling_an_existing_account_marks_and_preserves_a_tracking_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            tracker = PortfolioPerformanceTracker(user="alice", data_dir=data_dir)

            tracker.enable(
                enabled_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
                new_account=False,
            )
            tracker.disable(
                disabled_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            )
            tracker.enable(
                enabled_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
                new_account=False,
            )

            restored = PortfolioPerformanceTracker(
                user="alice",
                data_dir=data_dir,
            ).state()
            self.assertTrue(restored.enabled)
            self.assertTrue(restored.has_tracking_gap)
            self.assertEqual(
                restored.enabled_at,
                datetime(2026, 8, 10, tzinfo=timezone.utc),
            )

    def test_declared_deposit_is_persisted_with_bookkeeping_and_benchmark_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            tracker = PortfolioPerformanceTracker(
                user="alice",
                data_dir=data_dir,
                benchmark_prices=FixedBenchmarkPrices(),
            )
            tracker.enable(new_account=True)

            declared = tracker.declare_cash_flow(
                direction="deposit",
                amount=32_000,
                currency="TWD",
                amount_usd=1_000,
                fx_rate_to_usd=32,
                category="salary",
                channel="bank_transfer",
                broker="IBKR",
                account="MAIN",
                notes="七月投入",
                occurred_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
            )

            restored = PortfolioPerformanceTracker(
                user="alice",
                data_dir=data_dir,
            ).cash_flows()
            self.assertEqual(restored, [declared])
            self.assertEqual(declared.direction, "deposit")
            self.assertEqual(declared.amount_usd, 1_000)
            self.assertEqual(declared.category, "salary")
            self.assertEqual(declared.channel, "bank_transfer")
            self.assertEqual(declared.benchmark_prices, {"QQQ": 500, "VT": 125})

    def test_report_compares_cash_flow_adjusted_portfolio_with_shadow_benchmarks(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = PortfolioPerformanceTracker(
                user="alice",
                data_dir=Path(tmp),
                benchmark_prices=ScheduledBenchmarkPrices(),
            )
            tracker.enable(
                enabled_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
                new_account=True,
            )
            tracker.record_valuation(
                total_value_usd=10_000,
                recorded_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
            )
            tracker.declare_cash_flow(
                direction="deposit",
                amount=1_000,
                currency="USD",
                amount_usd=1_000,
                fx_rate_to_usd=1,
                category="salary",
                channel="bank_transfer",
                broker="IBKR",
                occurred_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
            )
            tracker.record_valuation(
                total_value_usd=12_100,
                recorded_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
            )

            report = tracker.report()
            comparisons = {
                item.symbol: item for item in report.comparisons
            }

            self.assertAlmostEqual(report.portfolio_return_pct, 11)
            self.assertAlmostEqual(
                comparisons["QQQ"].benchmark_value_usd,
                13_090.9090909,
            )
            self.assertAlmostEqual(
                comparisons["QQQ"].performance_gap_pct,
                -7.5694444444,
            )
            self.assertAlmostEqual(
                comparisons["QQQ"].benchmark_return_pct,
                20,
            )
            self.assertAlmostEqual(
                comparisons["VT"].benchmark_value_usd,
                11_838.4615385,
            )
            self.assertAlmostEqual(
                comparisons["VT"].performance_gap_pct,
                2.2092267706,
            )
            self.assertAlmostEqual(
                comparisons["VT"].benchmark_return_pct,
                8,
            )

    def test_tracked_position_purchase_cannot_exceed_matching_cash(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = PortfolioPerformanceTracker(
                user="alice",
                data_dir=Path(tmp),
            )
            tracker.enable(new_account=True)
            cash = [
                CashPosition(
                    broker="IBKR",
                    account="MAIN",
                    currency="USD",
                    amount=1_000,
                )
            ]
            purchase = Position(
                broker="IBKR",
                account="MAIN",
                symbol="AAPL",
                quantity=5,
                avg_cost=100,
                currency="USD",
            )

            positions, remaining_cash = tracker.apply_position_purchase(
                positions=[],
                cash_positions=cash,
                purchase=purchase,
            )

            self.assertEqual(positions, [purchase])
            self.assertEqual(remaining_cash[0].amount, 500)
            with self.assertRaisesRegex(ValueError, "可用現金不足"):
                tracker.apply_position_purchase(
                    positions=positions,
                    cash_positions=remaining_cash,
                    purchase=Position(
                        broker="IBKR",
                        account="MAIN",
                        symbol="QQQ",
                        quantity=2,
                        avg_cost=300,
                        currency="USD",
                    ),
                )

    def test_tracked_position_sale_converts_value_to_cash_instead_of_deleting_assets(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = PortfolioPerformanceTracker(
                user="alice",
                data_dir=Path(tmp),
            )
            tracker.enable(new_account=True)
            position = Position(
                broker="IBKR",
                account="MAIN",
                symbol="AAPL",
                quantity=5,
                avg_cost=100,
                market_price=110,
                currency="USD",
            )
            cash = [
                CashPosition(
                    broker="IBKR",
                    account="MAIN",
                    currency="USD",
                    amount=500,
                )
            ]

            positions, resulting_cash = tracker.apply_position_sale(
                positions=[position],
                cash_positions=cash,
                position=position,
                quantity=5,
            )

            self.assertEqual(positions, [])
            self.assertEqual(resulting_cash[0].amount, 1_050)

    def test_yfinance_adapter_uses_latest_market_close_before_sunday(self):
        history = pd.DataFrame(
            {"Close": [498.0, 500.0]},
            index=pd.to_datetime(["2026-07-23", "2026-07-24"]),
        )
        with patch("assettrack.performance.yf.Ticker") as ticker:
            ticker.return_value.history.return_value = history

            closes = YFinanceBenchmarkPrices().closing_prices(
                ["QQQ"],
                datetime(2026, 7, 26, tzinfo=timezone.utc),
            )

        self.assertEqual(closes["QQQ"], BenchmarkClose(date(2026, 7, 24), 500))

    def test_baseline_is_due_immediately_then_valuations_are_due_once_each_sunday(self):
        with tempfile.TemporaryDirectory() as tmp:
            tracker = PortfolioPerformanceTracker(
                user="alice",
                data_dir=Path(tmp),
                benchmark_prices=ScheduledBenchmarkPrices(),
            )
            tracker.enable(
                enabled_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
                new_account=True,
            )
            monday = datetime(2026, 7, 27, tzinfo=timezone.utc)
            sunday = datetime(2026, 8, 2, tzinfo=timezone.utc)

            self.assertTrue(tracker.valuation_due(monday))
            tracker.record_valuation(total_value_usd=10_000, recorded_at=monday)
            self.assertFalse(tracker.valuation_due(monday))
            self.assertTrue(tracker.valuation_due(sunday))
            tracker.record_valuation(total_value_usd=10_500, recorded_at=sunday)
            self.assertFalse(tracker.valuation_due(sunday))


class PerformanceTrackingTUITests(unittest.IsolatedAsyncioTestCase):
    async def test_registration_can_opt_in_to_performance_tracking(self):
        from textual.app import App
        from textual.widgets import Button, Checkbox, Input

        from assettrack import tui

        result_holder = []

        class HostApp(App):
            def on_mount(self):
                self.push_screen(tui.RegisterModal("alice"), result_holder.append)

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            tui, "get_data_dir", return_value=Path(tmp)
        ), patch.object(tui, "register_account"), patch.object(tui, "unlock_vault"):
            app = HostApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                modal = app.screen
                modal.query_one("#pwd1", Input).value = "secret"
                modal.query_one("#pwd2", Input).value = "secret"
                modal.query_one("#performance-tracking-toggle", Checkbox).value = True
                modal.query_one("#confirm", Button).press()
                await pilot.pause()

            state = PortfolioPerformanceTracker(
                user="alice",
                data_dir=Path(tmp),
            ).state()

        self.assertEqual(result_holder, [True])
        self.assertTrue(state.enabled)
        self.assertFalse(state.has_tracking_gap)

    async def test_dashboard_shortcut_opens_performance_comparison_page(self):
        from textual.app import App
        from textual.widgets import Static

        from assettrack import tui

        class HostApp(App):
            def __init__(self):
                super().__init__()
                self._fetch_activity = {}

            def _set_fetch_active(self, key, label):
                self._fetch_activity[key] = label

            def _clear_fetch_active(self, key):
                self._fetch_activity.pop(key, None)

            def on_mount(self):
                self.push_screen(
                    tui.DashboardScreen(
                        "alice",
                        positions=[],
                        cash_positions=[],
                        rate=32,
                    )
                )

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            tui, "get_data_dir", return_value=Path(tmp)
        ), patch.object(
            tui, "_get_cached_usdtwd_rate", return_value=32
        ), patch.object(
            tui, "fetch_usdtwd_rate", return_value=32
        ), patch(
            "assettrack.quotes.fetch_risk_free_rate", return_value=0.04
        ):
            app = HostApp()
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                app.screen.action_performance_tracking()
                await pilot.pause()

                self.assertIsInstance(app.screen, tui.PerformanceTrackingScreen)
                copy = app.screen.query_one("#performance-copy", Static)
                self.assertIn("對標", str(copy.render()))
                self.assertIn("QQQ", str(copy.render()))

    async def test_performance_page_can_confirm_cancelling_tracking(self):
        from textual.app import App
        from textual.widgets import Button, Static

        from assettrack import tui

        class HostApp(App):
            def on_mount(self):
                self.push_screen(
                    tui.PerformanceTrackingScreen(
                        "alice",
                        positions=[],
                        cash_positions=[],
                        rate=32,
                    )
                )

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            tui, "get_data_dir", return_value=Path(tmp)
        ):
            tracker = PortfolioPerformanceTracker(
                user="alice",
                data_dir=Path(tmp),
            )
            tracker.enable(new_account=True)

            app = HostApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.screen.action_disable_tracking()
                await pilot.pause()

                self.assertIsInstance(
                    app.screen,
                    tui.PerformanceTrackingCancelConfirmModal,
                )
                app.screen.query_one(
                    "#performance-cancel-confirm",
                    Button,
                ).press()
                await pilot.pause()

                self.assertIsInstance(app.screen, tui.PerformanceTrackingScreen)
                status = app.screen.query_one("#performance-status", Static)
                self.assertIn("績效追蹤已取消", str(status.render()))

            state = tracker.state()
            document = json.loads(tracker.path.read_text())

        self.assertFalse(state.enabled)
        self.assertIsNotNone(
            document["userportfolioperf_tracksys"].get("disabled_at")
        )

    async def test_cancelled_tracking_allows_direct_cash_management(self):
        from textual.app import App

        from assettrack import tui

        cash = CashPosition(
            broker="IBKR",
            account="MAIN",
            currency="USD",
            amount=1_000,
        )

        class HostApp(App):
            def __init__(self):
                super().__init__()
                self._fetch_activity = {}

            def _set_fetch_active(self, key, label):
                self._fetch_activity[key] = label

            def _clear_fetch_active(self, key):
                self._fetch_activity.pop(key, None)

            def on_mount(self):
                self.push_screen(
                    tui.DashboardScreen(
                        "alice",
                        positions=[],
                        cash_positions=[],
                        rate=32,
                    )
                )

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            tui, "get_data_dir", return_value=Path(tmp)
        ), patch.object(
            tui.DashboardScreen,
            "_maybe_record_performance_valuation",
        ), patch.object(
            tui, "_get_cached_usdtwd_rate", return_value=32
        ), patch.object(
            tui, "fetch_usdtwd_rate", return_value=32
        ), patch(
            "assettrack.quotes.fetch_risk_free_rate", return_value=0.04
        ), patch.object(
            tui, "load_manual_positions", return_value=([], [])
        ), patch.object(tui, "save_manual_positions") as save:
            tracker = PortfolioPerformanceTracker(
                user="alice",
                data_dir=Path(tmp),
            )
            tracker.enable(new_account=True)
            tracker.disable()

            app = HostApp()
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                app.screen._handle_add_position_result([cash])
                await pilot.pause()

        self.assertEqual(save.call_args.kwargs["cash_positions"], [cash])

    async def test_cash_flow_modal_collects_basic_deposit_bookkeeping(self):
        from textual.app import App
        from textual.widgets import Button, Input, Select

        from assettrack import tui

        result_holder = []

        class HostApp(App):
            def on_mount(self):
                self.push_screen(
                    tui.CashFlowModal("deposit"),
                    result_holder.append,
                )

        app = HostApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            modal = app.screen
            modal.query_one("#cash-flow-broker", Select).value = "IBKR"
            modal.query_one("#cash-flow-account", Input).value = "MAIN"
            modal.query_one("#cash-flow-amount", Input).value = "32000"
            modal.query_one("#cash-flow-currency", Select).value = "TWD"
            modal.query_one("#cash-flow-category", Select).value = "salary"
            modal.query_one("#cash-flow-channel", Select).value = "bank_transfer"
            modal.query_one("#cash-flow-notes", Input).value = "七月投入"
            modal.query_one("#cash-flow-confirm", Button).press()
            await pilot.pause()

        self.assertEqual(
            result_holder,
            [{
                "direction": "deposit",
                "broker": "IBKR",
                "account": "MAIN",
                "amount": 32000.0,
                "currency": "TWD",
                "category": "salary",
                "channel": "bank_transfer",
                "notes": "七月投入",
            }],
        )

    async def test_tracked_dashboard_purchase_debits_cash_instead_of_inflating_assets(self):
        from textual.app import App

        from assettrack import tui

        cash = [
            CashPosition(
                broker="IBKR",
                account="MAIN",
                currency="USD",
                amount=1_000,
            )
        ]
        purchase = Position(
            broker="IBKR",
            account="MAIN",
            symbol="AAPL",
            quantity=5,
            avg_cost=100,
            currency="USD",
        )

        class HostApp(App):
            def __init__(self):
                super().__init__()
                self._fetch_activity = {}

            def _set_fetch_active(self, key, label):
                self._fetch_activity[key] = label

            def _clear_fetch_active(self, key):
                self._fetch_activity.pop(key, None)

            def on_mount(self):
                self.push_screen(
                    tui.DashboardScreen(
                        "alice",
                        positions=[],
                        cash_positions=cash,
                        rate=32,
                    )
                )

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            tui, "get_data_dir", return_value=Path(tmp)
        ), patch.object(
            tui.DashboardScreen,
            "_maybe_record_performance_valuation",
        ), patch.object(
            tui, "_get_cached_usdtwd_rate", return_value=32
        ), patch.object(
            tui, "fetch_usdtwd_rate", return_value=32
        ), patch(
            "assettrack.quotes.fetch_risk_free_rate", return_value=0.04
        ), patch.object(
            tui, "load_manual_positions", return_value=([], cash)
        ), patch.object(tui, "save_manual_positions") as save:
            PortfolioPerformanceTracker(
                user="alice",
                data_dir=Path(tmp),
            ).enable(new_account=True)
            app = HostApp()
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                app.screen._handle_add_position_result([purchase])
                await pilot.pause()

        saved_positions = save.call_args.args[0]
        saved_cash = save.call_args.kwargs["cash_positions"]
        self.assertEqual(saved_positions[0].symbol, "AAPL")
        self.assertEqual(saved_cash[0].amount, 500)

    async def test_refresh_worker_can_update_encrypted_performance_ledger(self):
        from textual.app import App

        from assettrack import auth, tui

        class _MemoryKeyring:
            def __init__(self) -> None:
                self.secrets: dict[tuple[str, str], str] = {}

            def get_password(self, service, account):
                return self.secrets.get((service, account))

            def set_password(self, service, account, value):
                self.secrets[(service, account)] = value

            def delete_password(self, service, account):
                self.secrets.pop((service, account), None)

        class HostApp(App):
            def __init__(self):
                super().__init__()
                self.notifications: list[str] = []

            def notify(self, message, **kwargs):
                self.notifications.append(str(message))
                return super().notify(message, **kwargs)

            def on_mount(self):
                self.push_screen(
                    tui.PerformanceTrackingScreen(
                        "alice",
                        positions=[],
                        cash_positions=[],
                        rate=32,
                    )
                )

        keyring = _MemoryKeyring()
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            tui, "get_data_dir", return_value=Path(tmp)
        ), patch.object(
            auth.keyring, "get_password", keyring.get_password
        ), patch.object(
            auth.keyring, "set_password", keyring.set_password
        ), patch.object(
            auth.keyring, "delete_password", keyring.delete_password
        ), patch.object(
            auth, "PBKDF2_ITERATIONS", 1000
        ), patch.object(
            tui, "YFinanceBenchmarkPrices", FixedBenchmarkPrices
        ), patch.object(
            tui, "total_asset_value_usd", return_value=10_000.0
        ):
            auth.lock_vault()
            auth.register_account("alice", "correct-horse")
            auth.unlock_vault("alice", "correct-horse")
            tracker = PortfolioPerformanceTracker(
                user="alice",
                data_dir=Path(tmp),
                benchmark_prices=FixedBenchmarkPrices(),
            )
            tracker.enable(new_account=True)
            self.assertTrue(tracker.path.read_text().startswith(auth.TEXT_PREFIX))

            app = HostApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.screen.action_refresh_report()
                await pilot.pause(0.5)

            try:
                self.assertTrue(
                    all("資料保險庫尚未解鎖" not in note for note in app.notifications),
                    app.notifications,
                )
                self.assertEqual(len(tracker.valuations()), 1)
            finally:
                auth.lock_vault()


class DeclaredCashFlowPersistenceTests(unittest.TestCase):
    def test_follow_up_valuation_failure_does_not_fail_or_duplicate_a_deposit(self):
        from assettrack import storage, tui

        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            cash = [
                CashPosition(
                    broker="IBKR",
                    account="MAIN",
                    currency="USD",
                    amount=1_000,
                )
            ]
            with patch.object(tui, "get_data_dir", return_value=data_dir), patch.object(
                storage, "get_data_dir", return_value=data_dir
            ), patch.object(
                tui, "YFinanceBenchmarkPrices", FixedBenchmarkPrices
            ), patch.object(
                tui, "total_asset_value_usd", return_value=1_500.0
            ):
                tracker = PortfolioPerformanceTracker(
                    user="alice",
                    data_dir=data_dir,
                    benchmark_prices=FixedBenchmarkPrices(),
                )
                tracker.enable(new_account=True)
                storage.save_manual_positions([], cash, user="alice")
                declaration = {
                    "direction": "deposit",
                    "amount": 500,
                    "currency": "USD",
                    "broker": "IBKR",
                    "account": "MAIN",
                    "category": "salary",
                    "channel": "bank_transfer",
                }

                def boom(self, **kwargs):
                    raise ValueError("missing benchmark closing prices: QQQ")

                with patch.object(
                    PortfolioPerformanceTracker, "record_valuation", boom
                ):
                    _positions, updated_cash = tui._process_declared_cash_flow(
                        user="alice",
                        positions=[],
                        cash_positions=cash,
                        rate=32,
                        declaration=declaration,
                    )

                restored_positions, restored_cash = storage.load_manual_positions(
                    "alice"
                )
                restored_flows = tracker.cash_flows()

        self.assertEqual(updated_cash[0].amount, 1_500)
        self.assertEqual(restored_positions, [])
        self.assertEqual(restored_cash[0].amount, 1_500)
        self.assertEqual(len(restored_flows), 1)
        self.assertEqual(restored_flows[0].amount, 500)


if __name__ == "__main__":
    unittest.main()
