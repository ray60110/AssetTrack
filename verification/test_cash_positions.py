import unittest
from unittest.mock import patch

from assettrack.models import (
    CashPosition,
    Position,
    calculate_cash_ratio,
    merge_cash_position,
    normalize_holding_account,
    portfolio_unrealized_performance,
)


class CashPositionTests(unittest.TestCase):
    def test_legacy_bank_cash_record_loads_into_broker_architecture(self):
        cash = CashPosition.model_validate(
            {
                "bank": "Chase",
                "currency": "USD",
                "amount": 500,
            },
        )

        self.assertEqual(cash.broker, "Chase")
        self.assertEqual(cash.bank, "Chase")

    def test_same_broker_account_and_currency_cash_amounts_stack(self):
        cash_positions = [
            CashPosition(
                broker="IBKR",
                account="MAIN",
                currency="USD",
                amount=1000,
            ),
        ]

        merge_cash_position(
            cash_positions,
            CashPosition(
                broker="IBKR",
                account="MAIN",
                currency="USD",
                amount=250,
                notes="第二次新增",
            ),
        )

        self.assertEqual(len(cash_positions), 1)
        self.assertEqual(cash_positions[0].amount, 1250)
        self.assertEqual(cash_positions[0].notes, "第二次新增")

    def test_blank_and_placeholder_accounts_are_the_same_cash_holding(self):
        self.assertIsNone(normalize_holding_account(None))
        self.assertIsNone(normalize_holding_account(""))
        self.assertIsNone(normalize_holding_account("default"))
        self.assertEqual(normalize_holding_account("MAIN"), "MAIN")

        cash_positions = [
            CashPosition(
                broker="manual",
                account=None,
                currency="USD",
                amount=10_000,
            ),
        ]
        merge_cash_position(
            cash_positions,
            CashPosition(
                broker="manual",
                account="default",
                currency="USD",
                amount=1_600,
                notes="出售 AAPL",
            ),
        )

        self.assertEqual(len(cash_positions), 1)
        self.assertEqual(cash_positions[0].amount, 11_600)
        self.assertIsNone(cash_positions[0].account)

    def test_cash_ratio_uses_converted_cash_and_positive_long_short_values(self):
        positions = [
            Position(
                broker="IBKR",
                symbol="AAPL",
                quantity=10,
                market_price=600,
                currency="USD",
            ),
            Position(
                broker="IBKR",
                symbol="TSLA",
                quantity=-10,
                market_price=200,
                currency="USD",
            ),
        ]
        cash_positions = [
            CashPosition(
                broker="IBKR",
                account="MAIN",
                currency="USD",
                amount=1000,
            ),
            CashPosition(
                broker="FT",
                account="MAIN",
                currency="TWD",
                amount=32000,
            ),
        ]

        self.assertEqual(
            calculate_cash_ratio(positions, cash_positions, usdtwd_rate=32),
            20,
        )

    def test_cash_is_zero_pnl_but_part_of_portfolio_return_denominator(self):
        positions = [
            Position(
                broker="IBKR",
                symbol="AAPL",
                quantity=10,
                avg_cost=100,
                market_price=110,
            ),
            Position(
                broker="IBKR",
                symbol="TSLA",
                quantity=-10,
                avg_cost=100,
                market_price=80,
            ),
        ]
        cash_positions = [
            CashPosition(
                broker="IBKR",
                currency="USD",
                amount=1000,
            ),
        ]

        self.assertEqual(
            portfolio_unrealized_performance(
                positions,
                cash_positions,
                usdtwd_rate=32,
            ),
            (300, 10),
        )


class AddCashPositionModalTests(unittest.IsolatedAsyncioTestCase):
    async def test_action_add_position_modal_can_submit_usd_cash(self):
        from textual.app import App
        from textual.widgets import Button, Input, Select

        from assettrack import tui

        result_holder = []

        class HostApp(App):
            def on_mount(self):
                self.push_screen(
                    tui.AddPositionModal(),
                    result_holder.append,
                )

        app = HostApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            modal = app.screen
            modal.query_one("#add-type", Select).value = "cash"
            modal.query_one("#add-broker", Select).value = "IBKR"
            modal.query_one("#add-cash-account", Input).value = "MAIN"
            modal.query_one("#add-cash-currency", Select).value = "USD"
            modal.query_one("#add-cash-amount", Input).value = "1250.50"
            modal.query_one("#add-cash-notes", Input).value = "待部署資金"
            modal.query_one("#confirm", Button).press()
            await pilot.pause()

        self.assertEqual(len(result_holder), 1)
        result = result_holder[0]
        self.assertEqual(len(result), 1)
        cash = result[0]
        self.assertIsInstance(cash, CashPosition)
        self.assertEqual(cash.broker, "IBKR")
        self.assertEqual(cash.account, "MAIN")
        self.assertEqual(cash.currency, "USD")
        self.assertEqual(cash.amount, 1250.50)
        self.assertEqual(cash.notes, "待部署資金")

    async def test_blank_account_is_not_rewritten_as_default(self):
        from textual.app import App
        from textual.widgets import Button, Input

        from assettrack import tui

        result_holder = []

        class HostApp(App):
            def on_mount(self):
                self.push_screen(tui.AddPositionModal(), result_holder.append)

        app = HostApp()
        async with app.run_test(size=(100, 55)) as pilot:
            await pilot.pause()
            modal = app.screen
            modal.query_one("#add-symbol", Input).value = "AAPL"
            modal.query_one("#add-qty", Input).value = "10"
            modal.query_one("#add-cost", Input).value = "150"
            modal.query_one("#confirm", Button).press()
            await pilot.pause()

        self.assertEqual(len(result_holder), 1)
        result = result_holder[0]
        self.assertEqual(len(result), 1)
        position = result[0]
        self.assertIsInstance(position, Position)
        self.assertEqual(position.symbol, "AAPL")
        self.assertIsNone(position.account)

    async def test_add_etf_can_store_manual_exposure_factor(self):
        from textual.app import App
        from textual.widgets import Button, Input, Select

        from assettrack import tui

        result_holder = []

        class HostApp(App):
            def on_mount(self):
                self.push_screen(tui.AddPositionModal(), result_holder.append)

        app = HostApp()
        async with app.run_test(size=(100, 55)) as pilot:
            await pilot.pause()
            modal = app.screen
            modal.query_one("#add-symbol", Input).value = "SOXL"
            modal.query_one("#add-type", Select).value = "etf"
            modal.query_one("#add-leverage-factor", Input).value = "3"
            modal.query_one("#add-qty", Input).value = "10"
            modal.query_one("#add-cost", Input).value = "20"
            modal.query_one("#confirm", Button).press()
            await pilot.pause()

        self.assertEqual(len(result_holder), 1)
        result = result_holder[0]
        self.assertEqual(len(result), 1)
        position = result[0]
        self.assertIsInstance(position, Position)
        self.assertEqual(position.instrument_type, "etf")
        self.assertEqual(position.leverage_factor, 3)


class DashboardCashPositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_action_add_position_persists_cash(self):
        from textual.app import App
        from textual.widgets import Button, Input, Select

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
                    ),
                )

        with patch.object(
            tui, "_get_cached_usdtwd_rate", return_value=32,
        ), patch.object(
            tui, "fetch_usdtwd_rate", return_value=32,
        ), patch(
            "assettrack.quotes.fetch_risk_free_rate", return_value=0.04,
        ), patch.object(
            tui, "load_manual_positions", return_value=([], []),
        ), patch.object(tui, "save_manual_positions") as save:
            app = HostApp()
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                app.screen.action_add_position()
                await pilot.pause()
                modal = app.screen
                modal.query_one("#add-type", Select).value = "cash"
                modal.query_one("#add-broker", Select).value = "FT"
                modal.query_one("#add-cash-account", Input).value = "IRA"
                modal.query_one("#add-cash-currency", Select).value = "TWD"
                modal.query_one("#add-cash-amount", Input).value = "64000"
                modal.query_one("#confirm", Button).press()
                await pilot.pause()

        save.assert_called()
        positions = save.call_args.args[0]
        cash_positions = save.call_args.kwargs["cash_positions"]
        self.assertEqual(positions, [])
        self.assertEqual(len(cash_positions), 1)
        self.assertEqual(cash_positions[0].broker, "FT")
        self.assertEqual(cash_positions[0].account, "IRA")
        self.assertEqual(cash_positions[0].currency, "TWD")
        self.assertEqual(cash_positions[0].amount, 64000)

    async def test_cash_is_visible_in_holdings_and_cash_ratio_state(self):
        from rich.console import Console
        from textual.app import App
        from textual.widgets import DataTable, Static

        from assettrack import tui

        cash = CashPosition(
            broker="IBKR",
            account="MAIN",
            currency="TWD",
            amount=64000,
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
                        cash_positions=[cash],
                        rate=32,
                    ),
                )

        with patch.object(
            tui, "_get_cached_usdtwd_rate", return_value=32,
        ), patch.object(
            tui, "fetch_usdtwd_rate", return_value=32,
        ), patch(
            "assettrack.quotes.fetch_risk_free_rate", return_value=0.04,
        ):
            app = HostApp()
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                table = app.screen.query_one("#holdings-table", DataTable)
                rows = [
                    table.get_row_at(index)
                    for index in range(table.row_count)
                ]
                cash_row = rows[1]
                rendered_rows = " ".join(
                    " ".join(str(cell) for cell in row)
                    for row in rows
                )
                console = Console(record=True, width=120)
                console.print(
                    app.screen.query_one("#metrics-row", Static).content
                )
                metrics = console.export_text()

        self.assertIn("CASH TWD", rendered_rows)
        self.assertIn("現金", rendered_rows)
        self.assertEqual(str(cash_row[2]), "64,000.00")
        self.assertEqual(str(cash_row[5]), "[bold]$2,000.00[/bold]")
        self.assertIn("現金", metrics)
        self.assertIn("100.0%", metrics)

    def test_broker_panel_displays_leverage_ratio_and_total_exposure(self):
        from rich.console import Console

        from assettrack import tui

        positions = [
            Position(
                broker="IBKR",
                symbol="AAPL",
                quantity=10,
                market_price=100,
            ),
            Position(
                broker="IBKR",
                symbol="SOXL",
                instrument_type="etf",
                quantity=10,
                market_price=100,
                leverage_factor=3,
            ),
        ]
        console = Console(record=True, width=120)
        console.print(tui._build_broker_panel(positions, 32))
        rendered = console.export_text()

        self.assertIn("總曝險 2.00x", rendered)
        self.assertIn("倍數 ETF $3,000", rendered)
        self.assertIn("期權 Δ $0", rendered)
        self.assertIn("股票／普通 ETF $1,000", rendered)
        self.assertIn("淨 +2.00x", rendered)

    def test_metrics_panel_shows_asset_mix(self):
        from rich.console import Console

        from assettrack import tui

        positions = [
            Position(
                broker="IBKR",
                symbol="AAPL",
                quantity=10,
                market_price=100,
                avg_cost=90,
            ),
        ]
        cash = [
            CashPosition(broker="IBKR", currency="USD", amount=2000),
        ]
        console = Console(record=True, width=120)
        console.print(tui._build_metrics_panel(positions, 32, cash))
        rendered = console.export_text()

        self.assertIn("總資產", rendered)
        self.assertIn("$3,000.00", rendered)
        self.assertIn("股票 $1,000  33.3%", rendered)
        self.assertIn("現金 $2,000  66.7%", rendered)
        self.assertIn("1 股票", rendered)
        self.assertIn("1 現金", rendered)


if __name__ == "__main__":
    unittest.main()
