import unittest

from assettrack.models import Position, default_option_multiplier


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

    def test_blank_option_multiplier_defaults_to_50_for_taiwan_markets(self):
        self.assertEqual(
            default_option_multiplier(currency="TWD", symbol="TXO", market="TW"),
            50.0,
        )
        taiwan = Position(
            broker="manual",
            symbol="TXO",
            instrument_type="option",
            quantity=2,
            avg_cost=100,
            market="TW",
            currency="TWD",
            underlying="TXO",
            expiry="2026-12-16",
            strike=18000,
            option_type="call",
        )
        self.assertEqual(taiwan.multiplier, 50.0)
        self.assertEqual(taiwan.total_cost, 10_000)

    def test_blank_option_multiplier_defaults_to_100_for_us_markets(self):
        self.assertEqual(
            default_option_multiplier(currency="USD", symbol="AAPL", market="US"),
            100.0,
        )
        us = Position(
            broker="manual",
            symbol="AAPL261218C00100000",
            instrument_type="option",
            quantity=1,
            avg_cost=5,
            market="US",
            currency="USD",
            underlying="AAPL",
            expiry="2026-12-18",
            strike=100,
            option_type="call",
        )
        self.assertEqual(us.multiplier, 100.0)
        self.assertEqual(us.total_cost, 500)


if __name__ == "__main__":
    unittest.main()
