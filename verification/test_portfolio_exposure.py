import unittest
from datetime import date

from assettrack.exposure import calculate_portfolio_exposure
from assettrack.greeks import bs_greeks, bs_price
from assettrack.models import CashPosition, Position
from assettrack.quotes import infer_etf_leverage_factor


class PortfolioExposureTests(unittest.TestCase):
    def test_leveraged_and_inverse_etfs_affect_gross_and_net_exposure(self):
        positions = [
            Position(
                broker="manual",
                symbol="AAPL",
                instrument_type="stock",
                quantity=10,
                market_price=100,
            ),
            Position(
                broker="manual",
                symbol="LEV",
                instrument_type="etf",
                quantity=10,
                market_price=100,
                leverage_factor=-2,
            ),
        ]

        result = calculate_portfolio_exposure(positions, [], 32)

        self.assertTrue(result.complete)
        self.assertEqual(result.asset_value_usd, 2000)
        self.assertEqual(result.standard_exposure_usd, 1000)
        self.assertEqual(result.leveraged_etf_exposure_usd, 2000)
        self.assertEqual(result.gross_exposure_usd, 3000)
        self.assertEqual(result.net_exposure_usd, -1000)
        self.assertEqual(result.gross_ratio_pct, 150)
        self.assertEqual(result.net_ratio_pct, -50)

    def test_options_use_delta_equivalent_underlying_exposure(self):
        as_of = date(2026, 8, 15)
        dte = 30
        premium = bs_price(100, 100, dte, 0.30, "call", 0.04)
        expected_delta = bs_greeks(100, 100, dte, 0.30, "call", r=0.04)["delta"]
        option = Position(
            broker="manual",
            symbol="AAPL260914C00100000",
            instrument_type="option",
            quantity=2,
            market_price=premium,
            underlying="AAPL",
            expiry="2026-09-14",
            strike=100,
            option_type="call",
            multiplier=100,
        )
        cash = CashPosition(broker="manual", currency="USD", amount=10_000)

        result = calculate_portfolio_exposure(
            [option],
            [cash],
            32,
            underlying_prices={"AAPL": 100},
            risk_free_rate=0.04,
            as_of=as_of,
        )

        expected_exposure = expected_delta * 2 * 100 * 100
        self.assertTrue(result.complete)
        self.assertAlmostEqual(result.option_exposure_usd, expected_exposure, delta=2)
        self.assertAlmostEqual(result.gross_exposure_usd, expected_exposure, delta=2)
        self.assertAlmostEqual(result.net_exposure_usd, expected_exposure, delta=2)

    def test_missing_option_inputs_withhold_portfolio_ratios(self):
        option = Position(
            broker="manual",
            symbol="AAPL260914C00100000",
            instrument_type="option",
            quantity=1,
            market_price=5,
            underlying="AAPL",
            expiry="2026-09-14",
            strike=100,
            option_type="call",
            multiplier=100,
        )

        result = calculate_portfolio_exposure(
            [option],
            [],
            32,
            underlying_prices={},
            as_of=date(2026, 8, 15),
        )

        self.assertFalse(result.complete)
        self.assertEqual(result.unpriced, (option.symbol,))
        self.assertIsNone(result.gross_ratio_pct)
        self.assertIsNone(result.net_ratio_pct)

    def test_cash_reduces_exposure_ratio_without_adding_market_exposure(self):
        stock = Position(
            broker="manual",
            symbol="AAPL",
            quantity=10,
            market_price=100,
        )
        cash = CashPosition(broker="manual", currency="USD", amount=1000)

        result = calculate_portfolio_exposure([stock], [cash], 32)

        self.assertEqual(result.gross_exposure_usd, 1000)
        self.assertEqual(result.asset_value_usd, 2000)
        self.assertEqual(result.gross_ratio_pct, 50)


class ETFLeverageInferenceTests(unittest.TestCase):
    def test_infers_common_bull_bear_and_proshares_names(self):
        self.assertEqual(
            infer_etf_leverage_factor("Direxion Daily Semiconductor Bull 3X Shares"),
            3,
        )
        self.assertEqual(
            infer_etf_leverage_factor("Direxion Daily Semiconductor Bear 3X Shares"),
            -3,
        )
        self.assertEqual(infer_etf_leverage_factor("ProShares Ultra QQQ"), 2)
        self.assertEqual(infer_etf_leverage_factor("ProShares UltraShort QQQ"), -2)

    def test_infers_taiwan_name_or_symbol_conventions(self):
        self.assertEqual(infer_etf_leverage_factor("元大台灣50正2"), 2)
        self.assertEqual(infer_etf_leverage_factor("元大台灣50反1"), -1)
        self.assertEqual(infer_etf_leverage_factor(symbol="00631L.TW"), 2)
        self.assertEqual(infer_etf_leverage_factor(symbol="00632R.TW"), -1)

    def test_plain_etf_defaults_to_one(self):
        self.assertEqual(infer_etf_leverage_factor("Vanguard Total World Stock ETF"), 1)


if __name__ == "__main__":
    unittest.main()
