import unittest

from assettrack.models import CashPosition, Position
from assettrack.performance import PortfolioPerformanceTracker
from assettrack.tui import DashboardScreen


class PositionValuationTests(unittest.TestCase):
    def test_short_stock_uses_positive_value_and_inverse_profit_direction(self):
        position = Position(
            broker="manual",
            symbol="AAPL",
            instrument_type="stock",
            quantity=-10,
            avg_cost=100,
            market_price=80,
            prev_close=90,
        )

        self.assertEqual(position.value, 800)
        self.assertEqual(position.total_cost, 1000)
        self.assertEqual(position.unrealized_pnl, 200)
        self.assertEqual(position.unrealized_pnl_pct, 20)
        self.assertEqual(position.daily_change, 100)

    def test_short_option_applies_multiplier_to_positive_value_and_profit(self):
        position = Position(
            broker="manual",
            symbol="AAPL261218C00100000",
            instrument_type="option",
            quantity=-2,
            avg_cost=5,
            market_price=3,
            prev_close=4,
            underlying="AAPL",
            expiry="2026-12-18",
            strike=100,
            option_type="call",
            multiplier=100,
        )

        self.assertEqual(position.value, 600)
        self.assertEqual(position.total_cost, 1000)
        self.assertEqual(position.unrealized_pnl, 400)
        self.assertEqual(position.unrealized_pnl_pct, 40)
        self.assertEqual(position.daily_change, 200)

    def test_short_daily_percentage_follows_inverse_profit_direction(self):
        position = Position(
            broker="manual",
            symbol="AAPL",
            instrument_type="stock",
            quantity=-10,
            market_price=80,
            prev_close=100,
        )

        self.assertEqual(position.daily_change_pct, 20)

    def test_taiwan_option_symbols_include_strike_so_nearby_contracts_stay_distinct(self):
        call_600 = Position(
            broker="manual",
            account="default",
            symbol="placeholder",
            instrument_type="option",
            quantity=1,
            avg_cost=12,
            currency="TWD",
            market="TW",
            underlying="2330",
            expiry="2026-09-18",
            strike=600,
            option_type="call",
        )
        call_650 = Position(
            broker="manual",
            account="default",
            symbol="placeholder",
            instrument_type="option",
            quantity=2,
            avg_cost=8,
            currency="TWD",
            market="TW",
            underlying="2330",
            expiry="2026-09-18",
            strike=650,
            option_type="call",
        )

        self.assertEqual(call_600.symbol, "2330260918C00600000")
        self.assertEqual(call_650.symbol, "2330260918C00650000")
        self.assertNotEqual(call_600.symbol, call_650.symbol)

    def test_legacy_taiwan_option_symbol_without_strike_is_rewritten_on_load(self):
        position = Position(
            broker="manual",
            symbol="2330260918C",
            instrument_type="option",
            quantity=1,
            currency="TWD",
            market="TW",
            underlying="2330",
            expiry="2026-09-18",
            strike=600,
            option_type="call",
        )

        self.assertEqual(position.symbol, "2330260918C00600000")

    def test_add_position_merge_keeps_distinct_taiwan_option_strikes(self):
        positions: list[Position] = []
        DashboardScreen._merge_position(
            positions,
            Position(
                broker="manual",
                account="default",
                symbol="placeholder",
                instrument_type="option",
                quantity=1,
                avg_cost=12,
                currency="TWD",
                market="TW",
                underlying="2330",
                expiry="2026-09-18",
                strike=600,
                option_type="call",
            ),
        )
        DashboardScreen._merge_position(
            positions,
            Position(
                broker="manual",
                account="default",
                symbol="placeholder",
                instrument_type="option",
                quantity=2,
                avg_cost=8,
                currency="TWD",
                market="TW",
                underlying="2330",
                expiry="2026-09-18",
                strike=650,
                option_type="call",
            ),
        )

        self.assertEqual(len(positions), 2)
        self.assertEqual(
            sorted((item.strike, item.quantity) for item in positions),
            [(600, 1), (650, 2)],
        )


class TaiwanOptionPurchaseTests(unittest.TestCase):
    def test_tracked_buy_of_second_taiwan_strike_does_not_absorb_the_first(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            tracker = PortfolioPerformanceTracker(
                user="alice",
                data_dir=Path(tmp),
            )
            tracker.enable(new_account=True)
            first = Position(
                broker="IBKR",
                account="MAIN",
                symbol="placeholder",
                instrument_type="option",
                quantity=1,
                avg_cost=12,
                currency="TWD",
                market="TW",
                underlying="2330",
                expiry="2026-09-18",
                strike=600,
                option_type="call",
                multiplier=50,
            )
            second = Position(
                broker="IBKR",
                account="MAIN",
                symbol="placeholder",
                instrument_type="option",
                quantity=1,
                avg_cost=8,
                currency="TWD",
                market="TW",
                underlying="2330",
                expiry="2026-09-18",
                strike=650,
                option_type="call",
                multiplier=50,
            )
            cash = [
                CashPosition(
                    broker="IBKR",
                    account="MAIN",
                    currency="TWD",
                    amount=2_000,
                )
            ]

            positions, cash = tracker.apply_position_purchase(
                positions=[],
                cash_positions=cash,
                purchase=first,
            )
            positions, cash = tracker.apply_position_purchase(
                positions=positions,
                cash_positions=cash,
                purchase=second,
            )

            self.assertEqual(len(positions), 2)
            self.assertEqual(
                sorted((item.strike, item.quantity, item.avg_cost) for item in positions),
                [(600, 1, 12), (650, 1, 8)],
            )
            self.assertEqual(cash[0].amount, 1_000)


if __name__ == "__main__":
    unittest.main()
