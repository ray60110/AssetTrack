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


# ─────────────────────────────────────────────────────────────────────────────
# Timezone cache (shared by is_market_open and fetch_earnings_calendar)
# ─────────────────────────────────────────────────────────────────────────────

import zoneinfo as _zoneinfo  # noqa: E402

try:
    _TZ_TW = _zoneinfo.ZoneInfo("Asia/Taipei")
except Exception:
    _TZ_TW = None  # type: ignore[assignment]

try:
    _TZ_US = _zoneinfo.ZoneInfo("America/New_York")
except Exception:
    _TZ_US = None  # type: ignore[assignment]


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

SOX_TICKERS: list[str] = [
    "NVDA", "AVGO", "AMD", "QCOM", "INTC",
    "AMAT", "LRCX", "MU", "ASML", "TXN",
]
"""SOX 十大成分股清單（財報日曆追蹤用）。"""


# ─────────────────────────────────────────────────────────────────────────────
# Shared utility functions
# ─────────────────────────────────────────────────────────────────────────────

def draw_bar(value: float, max_value: float, width: int = 12) -> str:
    """Render a proportional Unicode block bar (█ / ░)."""
    if max_value <= 0:
        return "░" * width
    filled = round(min(value / max_value, 1.0) * width)
    return "█" * filled + "░" * (width - filled)


def nearest_price(price_map: dict, target_date) -> Optional[float]:
    """Binary-search price_map for the most-recent price on or before target_date."""
    sorted_dates = sorted(price_map.keys())
    lo, hi = 0, len(sorted_dates) - 1
    result = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if sorted_dates[mid] <= target_date:
            result = sorted_dates[mid]
            lo = mid + 1
        else:
            hi = mid - 1
    return price_map.get(result) if result is not None else None


def is_market_open(pos: Position) -> bool:
    """Return True if the exchange for this position is currently in regular trading hours."""
    from datetime import datetime as _dt
    is_tw = pos.currency == "TWD" or pos.symbol.endswith(".TW") or pos.symbol.endswith(".TWO")
    tz = _TZ_TW if is_tw else _TZ_US
    if tz is None:
        return False
    now = _dt.now(tz)
    if now.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    t = now.hour * 60 + now.minute
    # Taiwan 09:00–13:30 (540–810), US 09:30–16:00 (570–960)
    return (540 <= t <= 810) if is_tw else (570 <= t <= 960)


def group_positions_by_broker(
    positions: list[Position],
    rate: float,
) -> list[tuple[str, list[Position]]]:
    """
    Group positions by broker label (appends account if set).
    Each group is sorted by USD-equivalent value descending.
    Groups are sorted by their total USD value descending.
    Returns list of (broker_label, [Position, ...]) tuples.
    """
    groups: dict[str, list[Position]] = {}
    for p in positions:
        bk = f"{p.broker} ({p.account})" if p.account else p.broker
        groups.setdefault(bk, []).append(p)
    for bk in groups:
        groups[bk].sort(
            key=lambda p: (p.value if p.currency == "USD" else p.value / rate),
            reverse=True,
        )
    return sorted(
        groups.items(),
        key=lambda kv: sum(
            p.value if p.currency == "USD" else p.value / rate for p in kv[1]
        ),
        reverse=True,
    )


def fetch_earnings_calendar(
    symbols: list[str],
) -> dict[str, tuple[list, Optional[object], Optional[str], Optional[str]]]:
    """
    Fetch earnings calendar for multiple symbols from yfinance in parallel.

    Returns {symbol: (dates_list, info_date, time_str, period_str)} where:
    - dates_list : list[date] from t.calendar["Earnings Date"]
    - info_date  : precise date (GMT+8) from earningsTimestampStart
    - time_str   : "HH:MM" (GMT+8)
    - period_str : "盤前" | "盤後" based on US Eastern time
    """
    import concurrent.futures
    from datetime import datetime as _dt, timezone as _tz, timedelta

    def _fetch_one(symbol: str):
        try:
            with silence_output():
                t = yf.Ticker(symbol)
                cal = t.calendar
                dates = []
                if isinstance(cal, dict) and "Earnings Date" in cal:
                    dates = [d.date() if isinstance(d, _dt) else d for d in cal["Earnings Date"]]
                info = t.info
                ts = info.get("earningsTimestampStart") or info.get("earningsTimestamp")
                time_str = None
                info_date = None
                period_str = None
                if ts:
                    tz_gmt8 = _tz(timedelta(hours=8))
                    dt_gmt8 = _dt.fromtimestamp(ts, tz_gmt8)
                    info_date = dt_gmt8.date()
                    time_str = dt_gmt8.strftime("%H:%M")
                    dt_us = _dt.fromtimestamp(ts, _TZ_US)
                    period_str = "盤前" if dt_us.hour < 12 else "盤後"
                return symbol, dates, info_date, time_str, period_str
        except Exception:
            return symbol, [], None, None, None

    result: dict = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(symbols))) as ex:
        for sym, d, id_, ts_, ps_ in ex.map(_fetch_one, symbols):
            result[sym] = (d, id_, ts_, ps_)
    return result
