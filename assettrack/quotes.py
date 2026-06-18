from __future__ import annotations

import time
import re
import os
import sys
import logging
from contextlib import contextmanager
from typing import Iterable, Optional

import yfinance as yf

from .models import Position

# Suppress yfinance internal logs
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


@contextmanager
def silence_output():
    """A context manager that redirects stdout and stderr to devnull to silence noisy libraries."""
    old_stdout = sys.stdout
    old_stderr = sys.stderr
    try:
        with open(os.devnull, "w") as devnull:
            sys.stdout = devnull
            sys.stderr = devnull
            yield
    except Exception:
        sys.stdout = old_stdout
        sys.stderr = old_stderr
        raise
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr


_exchange_rate_cache: dict[str, float] = {}


def fetch_usdtwd_rate() -> float:
    """Get USD to TWD exchange rate from yfinance. Cached."""
    global _exchange_rate_cache
    if "USDTWD" in _exchange_rate_cache:
        return _exchange_rate_cache["USDTWD"]
    try:
        with silence_output():
            ticker = yf.Ticker("USDTWD=X")
            price = None
            try:
                fi = ticker.fast_info
                price = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
            except Exception:
                pass

            if price is None:
                # Fallback to history (most recent close)
                hist = ticker.history(period="1d", auto_adjust=False)
                if not hist.empty:
                    price = float(hist["Close"].iloc[-1])

            if price is not None:
                _exchange_rate_cache["USDTWD"] = price
                return price
    except Exception:
        pass
    return 32.0  # Safe fallback


def _normalize_symbol_for_yf(symbol: str, instrument_type: str, currency: str = "USD") -> str:
    """Best-effort mapping. Options often need special handling."""
    s = symbol.strip().upper()
    if currency.upper() == "TWD" and s.isdigit():
        return f"{s}.TW"
    if instrument_type == "option":
        # Remove internal spaces which can happen in IBKR exports (e.g. "AAPL  240621C00150000")
        s = re.sub(r"\s+", "", s)
        return s
    return s


def fetch_price(symbol: str, instrument_type: str = "stock", currency: str = "USD") -> Optional[float]:
    """Return latest price for a symbol. Returns None on failure."""
    yf_symbol = _normalize_symbol_for_yf(symbol, instrument_type, currency)
    try:
        with silence_output():
            ticker = yf.Ticker(yf_symbol)
            # Try fast info first
            price = None
            try:
                fi = ticker.fast_info
                price = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
            except Exception:
                pass

            if price is None:
                # Fallback to history (most recent close)
                hist = ticker.history(period="1d", auto_adjust=False)
                if not hist.empty:
                    price = float(hist["Close"].iloc[-1])

            return price
    except Exception:
        return None





def enrich_positions_with_quotes(positions: Iterable[Position], delay: float = 0.2) -> list[Position]:
    """
    Fill in market_price / market_value / prev_close using yfinance where missing.
    Returns a new list of Position objects (does not mutate originals).
    """
    enriched = []
    for pos in positions:
        p = pos.model_copy(deep=True)
        if p.market_price is None or p.market_value is None or p.prev_close is None:
            yf_symbol = _normalize_symbol_for_yf(p.symbol, p.instrument_type, p.currency)
            try:
                with silence_output():
                    ticker = yf.Ticker(yf_symbol)
                    price = None
                    prev_close = None
                    try:
                        fi = ticker.fast_info
                        price = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
                        prev_close = getattr(fi, "previous_close", None) or getattr(fi, "regular_market_previous_close", None)
                    except Exception:
                        pass

                    if price is None:
                        # Fallback to history (most recent 2 closes)
                        hist = ticker.history(period="5d", auto_adjust=False)
                        if not hist.empty:
                            price = float(hist["Close"].iloc[-1])
                            if len(hist) >= 2:
                                prev_close = float(hist["Close"].iloc[-2])

                    if price is not None:
                        p.market_price = price
                        mult = p.multiplier if (p.instrument_type == "option" and p.multiplier is not None) else 1.0
                        p.market_value = price * p.quantity * mult if p.quantity else None
                    if prev_close is not None:
                        p.prev_close = prev_close
            except Exception:
                pass
            time.sleep(delay)  # be nice to free APIs
        enriched.append(p)
    return enriched



def fetch_beta(symbol: str, instrument_type: str = "stock", underlying: Optional[str] = None, currency: str = "USD") -> Optional[float]:
    """
    Fetch the beta of a symbol from yfinance.
    For options, uses the underlying symbol instead.
    Returns None if unavailable.
    """
    # For options, use the underlying stock's beta
    lookup_symbol = underlying if (instrument_type == "option" and underlying) else symbol
    yf_symbol = _normalize_symbol_for_yf(lookup_symbol, "stock", currency)
    try:
        with silence_output():
            ticker = yf.Ticker(yf_symbol)
            info = ticker.info
            beta = info.get("beta", None)
            if beta is not None:
                return float(beta)
    except Exception:
        pass
    return None


def fetch_benchmark_history(
    symbol: str,
    start_date: "datetime",
    end_date: "datetime",
) -> list[tuple["date", float]]:
    """
    Fetch daily adjusted close prices for a benchmark index/ETF between two dates.

    Returns a list of (date, close_price) tuples sorted ascending.
    Returns an empty list on any failure.
    """
    from datetime import date as date_type, timedelta

    # Extend end by 1 day so yfinance includes end_date itself
    end_extended = end_date + timedelta(days=1)
    try:
        with silence_output():
            ticker = yf.Ticker(symbol)
            hist = ticker.history(
                start=start_date.strftime("%Y-%m-%d"),
                end=end_extended.strftime("%Y-%m-%d"),
                auto_adjust=True,
                actions=False,
            )
        if hist.empty:
            return []
        result: list[tuple[date_type, float]] = []
        for ts, row in hist.iterrows():
            # ts is a Timestamp; normalise to date
            try:
                d = ts.date()
            except Exception:
                d = ts
            result.append((d, float(row["Close"])))
        result.sort(key=lambda x: x[0])
        return result
    except Exception:
        return []


def fetch_historical_prices_weekly(
    symbols: list[str],
    start_date: "datetime",
    end_date: "datetime",
) -> "dict[str, dict]":
    """
    Batch-download weekly closing prices for multiple symbols via yfinance.

    Returns {symbol: {date: close_price}} where date is a datetime.date object.
    Uses yf.download() for efficiency. Falls back to per-ticker on failure.
    Non-USD symbols (e.g. 0050.TW) are included as-is.
    """
    import math
    from datetime import date as date_type, timedelta

    if not symbols:
        return {}

    # Extend end by 3 days so yfinance includes end_date itself
    end_extended = end_date + timedelta(days=3)
    result: dict[str, dict] = {s: {} for s in symbols}

    try:
        import yfinance as yf
        import pandas as pd
        with silence_output():
            raw = yf.download(
                tickers=symbols,
                start=start_date.strftime("%Y-%m-%d"),
                end=end_extended.strftime("%Y-%m-%d"),
                interval="1wk",
                auto_adjust=True,
                actions=False,
                progress=False,
                group_by="ticker",
            )

        if raw is not None and not raw.empty:
            for sym in symbols:
                try:
                    close_col = None
                    if isinstance(raw.columns, pd.MultiIndex):
                        if sym in raw.columns.get_level_values(0):
                            close_col = raw[sym]["Close"]
                        elif sym in raw.columns.get_level_values(1):
                            close_col = raw["Close"][sym]
                    else:
                        if "Close" in raw.columns:
                            close_col = raw["Close"]

                    if close_col is not None:
                        for ts, v in close_col.items():
                            try:
                                d = ts.date()
                            except Exception:
                                d = ts
                            if v is not None and not (hasattr(v, "isna") and v.isna()):
                                try:
                                    val = float(v)
                                    if not math.isnan(val):
                                        result[sym][d] = val
                                except (TypeError, ValueError):
                                    pass
                except Exception:
                    continue

    except Exception:
        # Per-ticker fallback
        for sym in symbols:
            try:
                hist_list = fetch_benchmark_history(sym, start_date, end_date)
                if hist_list:
                    # Downsample to weekly (keep every 5th trading day ≈ weekly)
                    prev_week = None
                    for d, price in hist_list:
                        iso_week = (d.year, d.isocalendar()[1])
                        if iso_week != prev_week:
                            if price is not None and not math.isnan(price):
                                result[sym][d] = price
                            prev_week = iso_week
            except Exception:
                pass

    return result


def current_portfolio_value(positions: list[Position]) -> float:
    return sum(p.value for p in positions)


