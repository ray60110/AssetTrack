from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assettrack import institutional
from assettrack.analysis import (
    compute_etf_selection_tilt,
    compute_symbol_trends,
    rank_symbol_trends,
)
from assettrack.etf_trades import derive_trade_history_from_snapshots
from assettrack import storage
from assettrack.tui import _fetch_and_cache_etf_symbols


class InstitutionalETFAnalysisTests(unittest.TestCase):
    def test_sec_requests_require_a_declared_contact_user_agent(self):
        with patch.dict(os.environ, {"SEC_USER_AGENT": ""}):
            with self.assertRaisesRegex(
                RuntimeError, "SEC_USER_AGENT.*名稱.*聯絡信箱",
            ):
                institutional._sec_headers()

    def test_classify_holdings_uses_actual_asset_mix(self):
        self.assertEqual(
            institutional.classify_holdings({"stockPosition": 82.0}), "股票型")
        self.assertEqual(
            institutional.classify_holdings({"bondPosition": 70.0}), "債券型")
        self.assertEqual(institutional.classify_holdings({
            "stockPosition": 45.0,
            "bondPosition": 40.0,
        }), "多重資產")
        self.assertEqual(institutional.classify_holdings({
            "stockPosition": 65.0,
            "otherPosition": 25.0,
        }), "衍生性／另類")

    def test_dynamic_universe_filters_aum_and_explicit_active_description(self):
        import yfinance as yf

        class FakeQuery:
            def __init__(self, operator, operands):
                self.operator = operator
                self.operands = operands

        summaries = {
            "ACTV": "The fund is actively managed and selects securities.",
            "INDX": "The fund tracks the performance of an index.",
        }

        class FakeTicker:
            def __init__(self, symbol):
                self.info = {"longBusinessSummary": summaries[symbol]}

        quotes = {"quotes": [
            {"symbol": "ACTV", "fundNetAssets": 6_000_000_000, "longName": "Active ETF"},
            {"symbol": "INDX", "fundNetAssets": 9_000_000_000, "longName": "Index ETF"},
            {"symbol": "SMOL", "fundNetAssets": 4_000_000_000, "longName": "Small Active ETF"},
        ]}
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(institutional, "get_data_dir", return_value=Path(temp_dir)), \
                patch.object(institutional, "load_etf_symbol_cache", return_value={
                    "asset_classes": {"stockPosition": 90.0},
                    "holdings": [{"symbol": "AAPL", "weight": 10.0}],
                }), \
                patch.object(yf, "ETFQuery", FakeQuery), \
                patch.object(yf, "Ticker", FakeTicker), \
                patch.object(yf, "screen", return_value=quotes):
            result = institutional.refresh_active_etf_universe()

            self.assertEqual(result["status"], "ok")
            self.assertEqual(
                [item["symbol"] for item in result["records"]], ["ACTV"])
            saved = json.loads(
                (Path(temp_dir) / "active_etf_universe.json").read_text())
            self.assertEqual(saved["minimum_aum"], 5_000_000_000)

    def test_parse_13f_preserves_exact_security_and_marks_option_limits(self):
        xml = b"""<?xml version="1.0"?>
        <informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
          <infoTable>
            <nameOfIssuer>EXAMPLE INC</nameOfIssuer>
            <titleOfClass>COM</titleOfClass>
            <cusip>123456789</cusip>
            <figi>BBG000TEST01</figi>
            <value>150000000</value>
            <shrsOrPrnAmt><sshPrnamt>3000000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
            <putCall>CALL</putCall>
          </infoTable>
        </informationTable>"""

        holdings = institutional.parse_13f_information_table(xml, "2026-03-31")

        self.assertEqual(holdings[0]["symbol"], "BBG000TEST01:CALL")
        self.assertEqual(holdings[0]["name"], "CALL EXAMPLE INC COM")
        self.assertEqual(holdings[0]["cusip"], "123456789")
        self.assertEqual(holdings[0]["instrument_type"], "option")
        self.assertEqual(holdings[0]["option_type"], "CALL")
        self.assertIsNone(holdings[0]["expiration"])
        self.assertIsNone(holdings[0]["strike"])
        self.assertEqual(holdings[0]["shares"], 3_000_000.0)
        self.assertEqual(holdings[0]["value"], 150_000_000.0)

    def test_fetch_13f_builds_quarterly_snapshots_from_live_filing_shape(self):
        target = {"id": "13F:1", "name": "Test Manager", "cik": "1"}
        filings = [
            {
                "accessionNumber": "0000000001-26-000001",
                "reportDate": "2026-03-31",
                "filingDate": "2026-05-15",
                "primaryDocument": "primary_doc.xml",
            },
        ]
        xml = b"""<informationTable>
          <infoTable>
            <nameOfIssuer>EXAMPLE INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
            <cusip>123456789</cusip><value>75000000</value>
            <shrsOrPrnAmt><sshPrnamt>1000000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
          </infoTable>
          <infoTable>
            <nameOfIssuer>SECOND INC</nameOfIssuer><titleOfClass>COM</titleOfClass>
            <cusip>987654321</cusip><value>25000000</value>
            <shrsOrPrnAmt><sshPrnamt>500000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
          </infoTable>
        </informationTable>"""
        with patch.object(institutional, "_recent_13f_filings", return_value=filings), \
                patch.object(
                    institutional, "_information_table_url",
                    return_value="https://sec.example/infotable.xml",
                ):
            result = institutional.fetch_hedge_fund_filings(
                target, get_bytes=lambda url: xml)

        self.assertEqual(result["report_date"], "2026-03-31")
        self.assertEqual(result["filing_date"], "2026-05-15")
        self.assertEqual(result["aum"], 100_000_000)
        self.assertEqual(result["holdings"][0]["weight"], 75.0)
        self.assertEqual(result["holdings"][1]["weight"], 25.0)
        self.assertEqual(result["status_message"], "SEC 13F 季度申報；非即時交易資料")

    def test_13f_fetch_uses_selected_account_sec_identity(self):
        target = {"id": "13F:1", "name": "Test Manager", "cik": "1"}
        submissions = json.dumps({"filings": {"recent": {
            "form": ["13F-HR"],
            "accessionNumber": ["0000000001-26-000001"],
            "reportDate": ["2026-03-31"],
            "filingDate": ["2026-05-15"],
            "primaryDocument": ["primary_doc.xml"],
        }}}).encode()
        index = json.dumps({"directory": {"item": [
            {"name": "primary_doc.xml", "size": 2000},
            {"name": "infotable.xml", "size": 20000},
        ]}}).encode()
        xml = b"""<informationTable><infoTable>
          <nameOfIssuer>EXAMPLE INC</nameOfIssuer>
          <titleOfClass>COM</titleOfClass><cusip>123456789</cusip>
          <value>100000000</value><shrsOrPrnAmt>
          <sshPrnamt>1000000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType>
          </shrsOrPrnAmt></infoTable></informationTable>"""
        stored = (
            '{"version": 1, "display_name": "Alice Example", '
            '"email": "alice@example.com", "consent_version": 1}'
        )
        sent_user_agents = []

        class FakeResponse:
            headers = {}

            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return self.payload

        def fake_urlopen(request, timeout):
            sent_user_agents.append(request.get_header("User-agent"))
            if "data.sec.gov/submissions" in request.full_url:
                return FakeResponse(submissions)
            if request.full_url.endswith("/index.json"):
                return FakeResponse(index)
            if request.full_url.endswith("/infotable.xml"):
                return FakeResponse(xml)
            raise AssertionError(request.full_url)

        with patch(
            "assettrack.sec_identity.keyring.get_password",
            return_value=stored,
        ), patch.object(
            institutional.urllib.request, "urlopen", side_effect=fake_urlopen,
        ), patch("time.sleep"):
            result = institutional.fetch_hedge_fund_filings(
                target, user="alice",
            )

        self.assertTrue(result["holdings"])
        self.assertEqual(
            sent_user_agents,
            ["Alice Example alice@example.com"] * 3,
        )

    def test_same_day_13f_error_is_retryable_until_holdings_exist(self):
        failed = {
            "id": "13F:1",
            "data_status": "error",
            "holdings": [],
            "last_checked": "2026-07-24T09:00:00",
        }
        recovered = {
            "13F:1": {
                "id": "13F:1",
                "data_status": "ok",
                "holdings": [{"symbol": "CUSIP1:SH"}],
            },
        }
        target = ({"id": "13F:1", "name": "Test Manager", "cik": "1"},)
        with patch.object(institutional, "HEDGE_FUND_TARGETS", target), \
                patch.object(institutional, "taiwan_now") as now, \
                patch.object(
                    institutional, "load_hedge_fund_cache",
                    return_value=failed,
                ), \
                patch.object(
                    institutional, "refresh_hedge_fund_filings",
                    return_value=recovered,
                ) as refresh:
            now.return_value.isoformat.return_value = "2026-07-24T10:00:00"
            now.return_value.strftime.return_value = "2026-07-24"
            result = institutional.ensure_hedge_fund_filings()

        refresh.assert_called_once_with()
        self.assertEqual(result["13F:1"]["data_status"], "ok")
        self.assertTrue(result["13F:1"]["holdings"])

    def test_13f_refresh_retries_transient_failure_before_caching_error(self):
        target = ({"id": "13F:1", "name": "Test Manager", "cik": "1"},)
        recovered = {
            "id": "13F:1",
            "name": "Test Manager",
            "cik": "1",
            "source_type": "13f",
            "category": "13F 對沖基金",
            "aum": 100_000_000,
            "holdings": [{"symbol": "CUSIP1:SH", "value": 100_000_000}],
            "report_date": "2026-03-31",
            "filing_date": "2026-05-15",
            "holdings_as_of_date": "2026-03-31",
            "snapshots": [{
                "date": "2026-03-31",
                "aum": 100_000_000,
                "holdings": [{"symbol": "CUSIP1:SH", "value": 100_000_000}],
            }],
            "data_status": "ok",
            "status_message": "SEC 13F 季度申報；非即時交易資料",
        }
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(institutional, "HEDGE_FUND_TARGETS", target), \
                patch.object(
                    institutional, "get_data_dir", return_value=Path(temp_dir),
                ), \
                patch.object(
                    institutional,
                    "fetch_hedge_fund_filings",
                    side_effect=[RuntimeError("temporary SEC failure"), recovered],
                ) as fetch, \
                patch.object(institutional, "append_etf_daily_snapshot"), \
                patch(
                    "assettrack.etf_trades.derive_trade_history_from_snapshots",
                    return_value=[],
                ), \
                patch("time.sleep"):
            result = institutional.refresh_hedge_fund_filings()

        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result["13F:1"]["data_status"], "ok")
        self.assertTrue(result["13F:1"]["holdings"])

    def test_all_four_13f_targets_flow_from_http_payloads_into_cache_and_history(self):
        xml = b"""<informationTable>
          <infoTable>
            <nameOfIssuer>EXAMPLE INC</nameOfIssuer>
            <titleOfClass>COM</titleOfClass>
            <cusip>123456789</cusip>
            <value>100000000</value>
            <shrsOrPrnAmt>
              <sshPrnamt>1000000</sshPrnamt>
              <sshPrnamtType>SH</sshPrnamtType>
            </shrsOrPrnAmt>
          </infoTable>
        </informationTable>"""

        def fake_http(url):
            if url.startswith("https://data.sec.gov/submissions/"):
                return json.dumps({"filings": {"recent": {
                    "form": ["13F-HR", "13F-HR"],
                    "accessionNumber": [
                        "0000000000-26-000002",
                        "0000000000-26-000001",
                    ],
                    "reportDate": ["2026-03-31", "2025-12-31"],
                    "filingDate": ["2026-05-15", "2026-02-14"],
                    "primaryDocument": ["primary_doc.xml", "primary_doc.xml"],
                }}}).encode()
            if url.endswith("/index.json"):
                return json.dumps({"directory": {"item": [
                    {"name": "primary_doc.xml", "size": 2000},
                    {"name": "infotable.xml", "size": 20000},
                ]}}).encode()
            if url.endswith("/infotable.xml"):
                return xml
            raise AssertionError(f"unexpected SEC URL: {url}")

        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(
                    institutional, "get_data_dir", return_value=Path(temp_dir),
                ), \
                patch.object(
                    storage, "get_data_dir", return_value=Path(temp_dir),
                ), \
                patch.object(institutional, "_get_url", side_effect=fake_http), \
                patch("time.sleep"):
            result = institutional.refresh_hedge_fund_filings()
            snapshots = {
                target["id"]: storage.load_etf_daily_snapshots(target["id"])
                for target in institutional.HEDGE_FUND_TARGETS
            }

        self.assertEqual(set(result), {
            "13F:1350694",
            "13F:1423053",
            "13F:1273087",
            "13F:1791786",
        })
        for target in institutional.HEDGE_FUND_TARGETS:
            item = result[target["id"]]
            self.assertEqual(item["data_status"], "ok")
            self.assertEqual(item["holdings"][0]["cusip"], "123456789")
            self.assertEqual(len(snapshots[target["id"]]), 2)

    def test_incomplete_etf_holdings_cache_is_not_fresh(self):
        incomplete = {
            "aum": 6_000_000_000,
            "price": 100.0,
            "holdings": [],
            "asset_classes": {},
            "holdings_as_of_date": "2026-07-24",
            "data_status": "partial",
            "last_refreshed": "2026-07-24T09:00:00",
        }
        with patch.object(
            storage, "load_etf_symbol_cache", return_value=incomplete,
        ), patch.object(storage, "taiwan_now") as now:
            now.return_value.strftime.return_value = "2026-07-24"
            fresh = storage.etf_symbol_cache_fresh("MISS")

        self.assertFalse(fresh)

    def test_etf_fetch_retries_empty_holdings_before_caching(self):
        import yfinance as yf

        class FakeTicker:
            info = {
                "totalAssets": 6_000_000_000,
                "longName": "Retry Active ETF",
            }

            def __init__(self, symbol):
                self.symbol = symbol

        empty = {
            "name": "Retry Active ETF",
            "holdings": [],
            "asset_classes": {},
            "as_of_date": "2026-07-24",
        }
        complete = {
            "name": "Retry Active ETF",
            "holdings": [{
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "weight": 10.0,
                "shares": None,
            }],
            "asset_classes": {"stockPosition": 100.0},
            "as_of_date": "2026-07-24",
        }
        performance = {
            "RETRY": {
                "price": 100.0,
                "change_pct": 1.0,
                "return_ytd": 5.0,
                "return_1y": 10.0,
            },
        }
        with patch.object(yf, "Ticker", FakeTicker), \
                patch("assettrack.quotes.fetch_active_etf_performance",
                      return_value=performance), \
                patch("assettrack.quotes.fetch_etf_holdings",
                      side_effect=[empty, complete]) as fetch_holdings, \
                patch("assettrack.quotes.fetch_prices_batch",
                      return_value={"AAPL": 200.0}), \
                patch("assettrack.storage.load_etf_symbol_cache",
                      return_value={}), \
                patch("assettrack.storage.save_etf_symbol_cache"), \
                patch("assettrack.etf_trades.update_etf_trade_history",
                      return_value=[]), \
                patch(
                    "assettrack.tui.append_etf_daily_snapshot",
                ) as append_snapshot, \
                patch("time.sleep"):
            result = _fetch_and_cache_etf_symbols(["RETRY"])

        self.assertEqual(fetch_holdings.call_count, 2)
        self.assertFalse(
            append_snapshot.call_args.kwargs.get("coalesce_unchanged", False))
        cached = result["etf_cache"]["RETRY"]
        self.assertEqual(cached["data_status"], "ok")
        self.assertEqual(cached["holdings"][0]["symbol"], "AAPL")

    def test_etf_failed_retries_preserve_last_valid_holdings(self):
        import yfinance as yf

        class FakeTicker:
            info = {
                "totalAssets": 6_000_000_000,
                "longName": "Cached Active ETF",
            }

            def __init__(self, symbol):
                self.symbol = symbol

        empty = {
            "name": "Cached Active ETF",
            "holdings": [],
            "asset_classes": {},
            "as_of_date": "2026-07-24",
        }
        previous = {
            "aum": 6_000_000_000,
            "price": 99.0,
            "holdings": [{"symbol": "MSFT", "weight": 8.0}],
            "asset_classes": {"stockPosition": 90.0},
            "holdings_as_of_date": "2026-07-23",
            "data_status": "ok",
        }
        performance = {
            "CACHE": {
                "price": 100.0,
                "change_pct": 1.0,
                "return_ytd": 5.0,
                "return_1y": 10.0,
            },
        }
        with patch.object(yf, "Ticker", FakeTicker), \
                patch(
                    "assettrack.quotes.fetch_active_etf_performance",
                    return_value=performance,
                ), \
                patch(
                    "assettrack.quotes.fetch_etf_holdings",
                    return_value=empty,
                ) as fetch_holdings, \
                patch(
                    "assettrack.quotes.fetch_prices_batch",
                    return_value={},
                ), \
                patch(
                    "assettrack.storage.load_etf_symbol_cache",
                    return_value=previous,
                ), \
                patch("assettrack.storage.save_etf_symbol_cache"), \
                patch(
                    "assettrack.etf_trades.update_etf_trade_history",
                    return_value=[],
                ), \
                patch("time.sleep"):
            result = _fetch_and_cache_etf_symbols(["CACHE"])

        self.assertEqual(fetch_holdings.call_count, 3)
        cached = result["etf_cache"]["CACHE"]
        self.assertEqual(cached["data_status"], "retryable")
        self.assertEqual(cached["holdings"][0]["symbol"], "MSFT")
        self.assertEqual(cached["holdings_as_of_date"], "2026-07-23")

    def test_etf_old_price_does_not_hide_current_performance_failure(self):
        import yfinance as yf

        class FakeTicker:
            info = {
                "totalAssets": 6_000_000_000,
                "longName": "Price Retry ETF",
            }

            def __init__(self, symbol):
                self.symbol = symbol

        holdings = {
            "name": "Price Retry ETF",
            "holdings": [{"symbol": "AAPL", "weight": 10.0}],
            "asset_classes": {"stockPosition": 100.0},
            "as_of_date": "2026-07-24",
        }
        previous = {
            "aum": 6_000_000_000,
            "price": 99.0,
            "holdings": [{"symbol": "MSFT", "weight": 8.0}],
            "asset_classes": {"stockPosition": 90.0},
            "holdings_as_of_date": "2026-07-23",
            "data_status": "ok",
        }
        with patch.object(yf, "Ticker", FakeTicker), \
                patch(
                    "assettrack.quotes.fetch_active_etf_performance",
                    return_value={"PRICE": {"price": None}},
                ) as fetch_performance, \
                patch(
                    "assettrack.quotes.fetch_etf_holdings",
                    return_value=holdings,
                ), \
                patch(
                    "assettrack.quotes.fetch_prices_batch",
                    return_value={"AAPL": 200.0},
                ), \
                patch(
                    "assettrack.storage.load_etf_symbol_cache",
                    return_value=previous,
                ), \
                patch("assettrack.storage.save_etf_symbol_cache"), \
                patch(
                    "assettrack.etf_trades.update_etf_trade_history",
                    return_value=[],
                ), \
                patch("assettrack.tui.append_etf_daily_snapshot"), \
                patch("time.sleep"):
            result = _fetch_and_cache_etf_symbols(["PRICE"])

        self.assertEqual(fetch_performance.call_count, 3)
        cached = result["etf_cache"]["PRICE"]
        self.assertEqual(cached["price"], 99.0)
        self.assertEqual(cached["data_status"], "retryable")
        self.assertIn("price", cached["missing_fields"])

    def test_position_level_report_totals_all_etfs(self):
        snapshots = {}
        for index in range(4):
            snapshots[f"ETF{index}"] = [
                {
                    "date": "2026-07-10",
                    "aum": 6_000_000_000,
                    "holdings": [{
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "weight": 5.0,
                        "price": 200.0,
                        "shares": 1_500_000,
                        "value": 300_000_000,
                        "instrument_type": "stock",
                    }],
                },
                {
                    "date": "2026-07-24",
                    "aum": 6_000_000_000,
                    "holdings": [{
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "weight": 6.0,
                        "price": 210.0,
                        "shares": 1_700_000,
                        "value": 357_000_000,
                        "instrument_type": "stock",
                    }],
                },
            ]

        report = compute_symbol_trends(
            snapshots, window_days=14, as_of="2026-07-24")
        aapl = report["symbols"]["AAPL"]
        tilt = compute_etf_selection_tilt(report)

        self.assertEqual(aapl["position"]["name"], "Apple Inc.")
        self.assertEqual(aapl["etfs_up"], ["ETF0", "ETF1", "ETF2", "ETF3"])
        self.assertEqual(aapl["buy_value"], 228_000_000)
        self.assertEqual(report["flow_totals"]["positions_bought"], 4)
        self.assertEqual(report["flow_totals"]["buy_value"], 228_000_000)
        self.assertEqual(tilt["aggregate"]["etfs_long"], 4)
        self.assertEqual(
            rank_symbol_trends(report, min_etfs_evaluated=4)[0][0], "AAPL")

    def test_identical_daily_observations_are_one_state_not_analysis_ready(self):
        repeated = []
        for date, price, aum in (
            ("2026-07-22", None, 6_000_000_000),
            ("2026-07-23", 200.0, 6_050_000_000),
            ("2026-07-24", 201.0, 6_100_000_000),
        ):
            holding = {"symbol": "AAPL", "weight": 5.0}
            if price is not None:
                holding["price"] = price
            repeated.append({
                "date": date,
                "aum": aum,
                "holdings": [holding],
            })

        report = compute_symbol_trends(
            {"ETF1": repeated},
            window_days=14,
            as_of="2026-07-25",
        )

        coverage = report["etf_coverage"]["ETF1"]
        self.assertEqual(coverage["observations_in_window"], 3)
        self.assertEqual(coverage["distinct_states"], 1)
        self.assertFalse(coverage["ready"])
        self.assertEqual(report["etfs_ready_count"], 0)
        self.assertTrue(coverage["comparable"])
        self.assertEqual(report["etfs_comparable_count"], 1)
        self.assertEqual(len(report["raw_contributions"]), 1)
        self.assertEqual(report["raw_contributions"][0]["direction"], "flat")
        self.assertEqual(report["symbols"]["AAPL"]["net_value_delta"], 5_000_000)

    def test_asset_class_only_change_does_not_ready_precise_holdings_analysis(self):
        report = compute_symbol_trends({
            "ETF1": [
                {
                    "date": "2026-07-23",
                    "aum": 6_000_000_000,
                    "holdings": [{"symbol": "AAPL", "weight": 5.0}],
                    "asset_classes": {"stockPosition": 90.0, "cashPosition": 10.0},
                },
                {
                    "date": "2026-07-24",
                    "aum": 6_000_000_000,
                    "holdings": [{"symbol": "AAPL", "weight": 5.0}],
                    "asset_classes": {"stockPosition": 91.0, "cashPosition": 9.0},
                },
            ],
        }, window_days=14, as_of="2026-07-25")

        coverage = report["etf_coverage"]["ETF1"]
        self.assertEqual(coverage["distinct_states"], 1)
        self.assertFalse(coverage["ready"])
        self.assertTrue(coverage["comparable"])
        self.assertEqual(len(report["raw_contributions"]), 1)
        self.assertEqual(report["raw_contributions"][0]["direction"], "flat")
        self.assertEqual(
            rank_symbol_trends(report, min_etfs_evaluated=1),
            [],
        )

    def test_all_flat_positions_are_not_ranked_as_precise_trade_signals(self):
        report = compute_symbol_trends({
            "ETF1": [
                {
                    "date": "2026-07-23",
                    "aum": 6_000_000_000,
                    "holdings": [{
                        "symbol": "AAPL",
                        "weight": 5.0,
                        "price": 200.0,
                        "shares": 1_500_000,
                    }],
                },
                {
                    "date": "2026-07-24",
                    "aum": 6_000_000_000,
                    "holdings": [{
                        "symbol": "AAPL",
                        "weight": 5.2,
                        "price": 201.0,
                        "shares": 1_550_000,
                    }],
                },
            ],
        }, window_days=14, as_of="2026-07-25")

        self.assertEqual(report["symbols"]["AAPL"]["consensus"], "flat")
        self.assertEqual(
            rank_symbol_trends(report, min_etfs_evaluated=1),
            [],
        )

    def test_identical_new_etf_snapshots_are_coalesced_until_state_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            storage, "get_data_dir", return_value=Path(temp_dir),
        ):
            storage.append_etf_daily_snapshot(
                "ETF1",
                [{"symbol": "AAPL", "weight": 5.0}],
                6_000_000_000,
                snapshot_date="2026-07-23",
                coalesce_unchanged=True,
            )
            storage.append_etf_daily_snapshot(
                "ETF1",
                [{"symbol": "AAPL", "weight": 5.0, "price": 201.0}],
                6_000_000_000,
                snapshot_date="2026-07-24",
                coalesce_unchanged=True,
            )
            unchanged = storage.load_etf_daily_snapshots("ETF1")

            storage.append_etf_daily_snapshot(
                "ETF1",
                [{"symbol": "AAPL", "weight": 6.0, "price": 202.0}],
                6_000_000_000,
                snapshot_date="2026-07-25",
                coalesce_unchanged=True,
            )
            changed = storage.load_etf_daily_snapshots("ETF1")

        self.assertEqual(len(unchanged), 1)
        self.assertEqual(unchanged[0]["date"], "2026-07-24")
        self.assertEqual(unchanged[0]["first_observed_date"], "2026-07-23")
        self.assertEqual(unchanged[0]["last_observed_date"], "2026-07-24")
        self.assertEqual(unchanged[0]["holdings"][0]["price"], 201.0)
        self.assertEqual(len(changed), 2)
        self.assertEqual(changed[-1]["date"], "2026-07-25")

    def test_disappearing_position_is_a_real_sell_signal(self):
        report = compute_symbol_trends({
            "ETF1": [
                {
                    "date": "2026-07-10",
                    "aum": 6_000_000_000,
                    "holdings": [{
                        "symbol": "NVDA",
                        "weight": 10.0,
                        "shares": 5_000_000,
                        "value": 600_000_000,
                    }],
                },
                {
                    "date": "2026-07-24",
                    "aum": 6_000_000_000,
                    "holdings": [],
                },
            ],
        }, window_days=14, as_of="2026-07-24")

        contribution = report["raw_contributions"][0]
        self.assertEqual(contribution["direction"], "down")
        self.assertEqual(contribution["share_delta"], -5_000_000)
        self.assertEqual(contribution["value_delta"], -600_000_000)

    def test_13f_history_diff_uses_reported_shares_value_and_period(self):
        snapshots = [
            {
                "date": "2025-12-31",
                "aum": 1_000_000,
                "holdings": [{
                    "symbol": "CUSIP1:SH",
                    "name": "EXAMPLE INC COM",
                    "weight": 10.0,
                    "shares": 1_000,
                    "value": 100_000,
                }],
            },
            {
                "date": "2026-03-31",
                "aum": 1_300_000,
                "holdings": [{
                    "symbol": "CUSIP1:SH",
                    "name": "EXAMPLE INC COM",
                    "weight": 20.0,
                    "shares": 2_000,
                    "value": 260_000,
                }],
            },
        ]
        with patch(
            "assettrack.etf_trades.load_etf_daily_snapshots",
            return_value=snapshots,
        ):
            history = derive_trade_history_from_snapshots("13F:TEST")

        self.assertEqual(history[0]["action"], "BUY")
        self.assertEqual(history[0]["shares"], 1_000)
        self.assertEqual(history[0]["value_change"], 160_000)
        self.assertEqual(history[0]["period_start"], "2025-12-31")
        self.assertEqual(history[0]["period_end"], "2026-03-31")


class AdvancedAnalysisScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_position_table_lists_buy_sell_and_flat_status_with_period_change(self):
        from textual.app import App

        from assettrack import tui

        snapshots = [
            {
                "date": "2026-07-23",
                "aum": 6_000_000_000,
                "holdings": [
                    {
                        "symbol": "AAPL",
                        "weight": 5.0,
                        "price": 200.0,
                        "shares": 1_500_000,
                    },
                    {
                        "symbol": "MSFT",
                        "weight": 6.0,
                        "price": 400.0,
                        "shares": 900_000,
                    },
                    {
                        "symbol": "NVDA",
                        "weight": 4.0,
                        "price": 120.0,
                        "shares": 2_000_000,
                    },
                ],
            },
            {
                "date": "2026-07-24",
                "aum": 6_000_000_000,
                "holdings": [
                    {
                        "symbol": "AAPL",
                        "weight": 5.2,
                        "price": 201.0,
                        "shares": 1_550_000,
                    },
                    {
                        "symbol": "MSFT",
                        "weight": 5.0,
                        "price": 410.0,
                        "shares": 800_000,
                    },
                    {
                        "symbol": "NVDA",
                        "weight": 5.0,
                        "price": 125.0,
                        "shares": 2_400_000,
                    },
                ],
            },
        ]

        class HostApp(App):
            def on_mount(self):
                self.push_screen(tui.AdvancedAnalysisScreen("default"))

        with patch.object(
            tui, "active_etf_symbols", return_value=["ETF1"],
        ), patch.object(
            tui, "load_etf_daily_snapshots", return_value=snapshots,
        ), patch.object(
            # bug#00123: the screen now also renders a SEC 13F section. This
            # test covers the ETF section only, and the patched loader above
            # would otherwise hand these ETF snapshots to every filer id too.
            tui, "hedge_fund_records", return_value=[],
        ), patch.object(
            tui, "_active_params", return_value={"etf": {}},
        ):
            app = HostApp()
            async with app.run_test(size=(160, 45)) as pilot:
                await pilot.pause()
                screen = app.screen
                table = screen.query_one("#aa-table")
                empty = screen.query_one("#aa-empty")

                self.assertTrue(table.display)
                self.assertFalse(empty.display)
                self.assertEqual(table.row_count, 4)

                rows = [
                    [str(cell) for cell in table.get_row_at(index)]
                    for index in range(table.row_count)
                ]
                by_symbol = {
                    symbol: next(row for row in rows if symbol in row[0])
                    for symbol in ("AAPL", "MSFT", "NVDA")
                }
                self.assertIn("持平", by_symbol["AAPL"][-1])
                self.assertIn("看跌", by_symbol["MSFT"][-1])
                self.assertIn("看漲", by_symbol["NVDA"][-1])
                self.assertIn("$12.0M", by_symbol["AAPL"][-2])


if __name__ == "__main__":
    unittest.main()
