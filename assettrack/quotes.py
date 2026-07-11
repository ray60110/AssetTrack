from __future__ import annotations

import time
import re
import os
import sys
import logging
import threading
from contextlib import contextmanager
from typing import Iterable, Optional

import yfinance as yf

from .models import Position

# Suppress yfinance internal logs
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


class NullWriter:
    def write(self, *args, **kwargs):
        pass
    def flush(self, *args, **kwargs):
        pass
    def isatty(self):
        return False


# bug#00054: sys.stdout/sys.stderr are process-global, so naively swapping them in a
# plain try/finally is NOT thread-safe. enrich_positions_with_quotes / fetch_earnings_calendar
# / fetch_active_etf_performance all call silence_output() from multiple ThreadPoolExecutor
# worker threads concurrently — with a naive swap, one thread can restore the *other*
# thread's NullWriter as if it were the real stdout, permanently silencing all output
# for the rest of the process (reproduced: 8 concurrent callers reliably corrupt stdout).
# Fix: track a shared, lock-guarded reference count. Only the first concurrent caller
# swaps stdout/stderr to the NullWriter, and only the last one to exit restores the
# real streams — the lock only guards the counter/swap, never the wrapped I/O itself,
# so callers still run fully concurrently.
_silence_lock = threading.Lock()
_silence_depth = 0
_real_stdout = None
_real_stderr = None


@contextmanager
def silence_output():
    """A context manager that redirects stdout and stderr to a NullWriter to silence
    noisy libraries. Safe to call concurrently from multiple threads (bug#00054)."""
    global _silence_depth, _real_stdout, _real_stderr
    null_writer = NullWriter()
    with _silence_lock:
        if _silence_depth == 0:
            _real_stdout = sys.stdout
            _real_stderr = sys.stderr
            sys.stdout = null_writer
            sys.stderr = null_writer
        _silence_depth += 1
    try:
        yield
    finally:
        with _silence_lock:
            _silence_depth -= 1
            if _silence_depth == 0:
                sys.stdout = _real_stdout
                sys.stderr = _real_stderr
                _real_stdout = None
                _real_stderr = None


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


def fetch_usdjpy_rate() -> float:
    """Get USD to JPY exchange rate from yfinance. Cached (same TTL mechanism as USDTWD)."""
    global _exchange_rate_cache
    if "USDJPY" in _exchange_rate_cache:
        return _exchange_rate_cache["USDJPY"]
    try:
        with silence_output():
            ticker = yf.Ticker("USDJPY=X")
            price = None
            try:
                fi = ticker.fast_info
                price = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
            except Exception:
                pass

            if price is None:
                hist = ticker.history(period="1d", auto_adjust=False)
                if not hist.empty:
                    price = float(hist["Close"].iloc[-1])

            if price is not None:
                _exchange_rate_cache["USDJPY"] = price
                return price
    except Exception:
        pass
    return 150.0  # Safe fallback


def cash_to_usd(amount: float, currency: str, rate_twd: float, rate_jpy: float) -> float:
    """Convert a cash amount in USD/TWD/JPY to USD equivalent."""
    if currency == "USD":
        return amount
    elif currency == "TWD":
        return amount / rate_twd if rate_twd > 0 else 0.0
    elif currency == "JPY":
        return amount / rate_jpy if rate_jpy > 0 else 0.0
    return amount



import math

def _clean_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        fv = float(v)
        if math.isnan(fv) or math.isinf(fv):
            return None
        return fv
    except (ValueError, TypeError):
        return None


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
                price = _clean_float(getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None))
            except Exception:
                pass

            if price is None:
                # Fallback to history (most recent close)
                hist = ticker.history(period="1d", auto_adjust=False)
                if not hist.empty:
                    price = _clean_float(hist["Close"].iloc[-1])

            return price
    except Exception:
        return None





def enrich_positions_with_quotes(positions: Iterable[Position]) -> list[Position]:
    """
    Fill in market_price / market_value / prev_close using yfinance where missing.
    Returns a new list of Position objects (does not mutate originals).

    bug#00052: previously this fetched one position at a time in a sequential loop
    with a `time.sleep(delay)` between each, so total refresh time scaled linearly
    with position count (N positions * (network latency + delay)). Each lookup is
    an independent, read-only yfinance call, so it's fetched concurrently instead —
    mirroring the ThreadPoolExecutor pattern already used by fetch_earnings_calendar
    / fetch_active_etf_performance in this module.
    """
    import concurrent.futures

    def _fetch_one(pos: Position) -> Position:
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
                        price = _clean_float(getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None))
                        prev_close = _clean_float(getattr(fi, "regular_market_previous_close", None) or getattr(fi, "previous_close", None))
                    except Exception:
                        pass

                    if price is None or prev_close is None:
                        # Fallback to history (most recent 2 closes)
                        hist = ticker.history(period="5d", auto_adjust=False)
                        if not hist.empty:
                            if price is None:
                                price = _clean_float(hist["Close"].iloc[-1])
                            if prev_close is None and len(hist) >= 2:
                                prev_close = _clean_float(hist["Close"].iloc[-2])

                    if price is not None:
                        p.market_price = price
                        mult = p.multiplier if (p.instrument_type == "option" and p.multiplier is not None) else 1.0
                        p.market_value = price * p.quantity * mult if p.quantity else None
                    if prev_close is not None:
                        p.prev_close = prev_close
            except Exception:
                pass
        return p

    positions = list(positions)
    if not positions:
        return []

    max_workers = min(10, len(positions))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        enriched = list(ex.map(_fetch_one, positions))
    return enriched



# bug#00048: fetch_beta() is called once per position on every dashboard render
# (TUI _build_metrics_panel), and ticker.info is a slow, uncached yfinance call.
# Without caching this blocks the main UI thread for N sequential network round-trips
# on every refresh. Cache results per symbol with a TTL so repeat lookups are instant.
_beta_cache: dict[str, tuple[Optional[float], float]] = {}
_BETA_CACHE_TTL = 6 * 3600  # 6 hours — beta changes slowly, no need to refetch often


def fetch_beta(symbol: str, instrument_type: str = "stock", underlying: Optional[str] = None, currency: str = "USD") -> Optional[float]:
    """
    Fetch the beta of a symbol from yfinance.
    For options, uses the underlying symbol instead.
    Returns None if unavailable. Cached per symbol (TTL) to avoid repeated blocking
    network calls on every dashboard render.
    """
    # For options, use the underlying stock's beta
    lookup_symbol = underlying if (instrument_type == "option" and underlying) else symbol
    yf_symbol = _normalize_symbol_for_yf(lookup_symbol, "stock", currency)

    cached = _beta_cache.get(yf_symbol)
    if cached is not None and (time.time() - cached[1]) < _BETA_CACHE_TTL:
        return cached[0]

    beta_val: Optional[float] = None
    try:
        with silence_output():
            ticker = yf.Ticker(yf_symbol)
            info = ticker.info
            beta = info.get("beta", None)
            if beta is not None:
                beta_val = float(beta)
    except Exception:
        pass

    _beta_cache[yf_symbol] = (beta_val, time.time())
    return beta_val


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


def _empty_perf(symbols: Iterable[str]) -> dict[str, dict]:
    return {
        s: {"price": None, "change_pct": None, "return_ytd": None, "return_1y": None}
        for s in symbols
    }


def _fetch_active_etf_performance_batch(symbols: list[str]) -> dict[str, dict]:
    """Fetch performance for a single small-to-medium batch of symbols via one
    yf.download() call. Returns {symbol: {price, change_pct, return_ytd, return_1y}}
    with all-None values for any symbol yfinance couldn't serve."""
    import datetime

    res: dict[str, dict] = {}
    end_date = datetime.datetime.now()
    start_date = end_date - datetime.timedelta(days=400)

    try:
        with silence_output():
            data = yf.download(
                tickers=symbols,
                start=start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
                interval="1d",
                auto_adjust=True,
                actions=False,
                progress=False,
                group_by="ticker" if len(symbols) > 1 else "column",
            )

        for symbol in symbols:
            try:
                # Extract closing prices
                if len(symbols) > 1:
                    if symbol in data.columns.get_level_values(0):
                        close_series = data[symbol]["Close"].dropna()
                    elif "Close" in data.columns and symbol in data["Close"].columns:
                        close_series = data["Close"][symbol].dropna()
                    else:
                        res[symbol] = _empty_perf([symbol])[symbol]
                        continue
                else:
                    close_series = data["Close"].dropna()

                import pandas as _pd
                if isinstance(close_series, _pd.DataFrame):
                    if symbol in close_series.columns:
                        close_series = close_series[symbol]
                    else:
                        close_series = close_series.squeeze()

                if close_series.empty:
                    res[symbol] = _empty_perf([symbol])[symbol]
                    continue

                current_price = float(close_series.iloc[-1])

                # Daily change %
                if len(close_series) > 1:
                    prev_price = float(close_series.iloc[-2])
                    change_pct = (current_price - prev_price) / prev_price * 100 if prev_price > 0 else 0.0
                else:
                    change_pct = 0.0

                first_date = close_series.index[0]
                last_date = close_series.index[-1]
                history_days = (last_date - first_date).days

                # 1-Year return %: Only calculate if fund has enough history (approx 1 year)
                if history_days >= 360:
                    target_1y = last_date - datetime.timedelta(days=365)
                    idx_1y = close_series.index.get_indexer([target_1y], method="nearest")[0]
                    price_1y = float(close_series.iloc[idx_1y])
                    return_1y = (current_price - price_1y) / price_1y * 100 if price_1y > 0 else None
                else:
                    return_1y = None  # Too new for 1-Year return

                # YTD return %: Check if inception was after Jan 1st of current year
                this_year = datetime.datetime.now().year
                jan_1st = datetime.datetime(this_year, 1, 1)

                # If inception date is after Jan 1st of current year, calculate from inception
                if first_date.to_pydatetime().date() > jan_1st.date():
                    price_ytd = float(close_series.iloc[0])  # price at inception
                    return_ytd = (current_price - price_ytd) / price_ytd * 100 if price_ytd > 0 else None
                else:
                    idx_ytd = close_series.index.get_indexer([jan_1st], method="nearest")[0]
                    price_ytd = float(close_series.iloc[idx_ytd])
                    return_ytd = (current_price - price_ytd) / price_ytd * 100 if price_ytd > 0 else None

                res[symbol] = {
                    "price": current_price,
                    "change_pct": change_pct,
                    "return_ytd": return_ytd,
                    "return_1y": return_1y
                }
            except Exception:
                res[symbol] = _empty_perf([symbol])[symbol]
    except Exception:
        return _empty_perf(symbols)
    return res


def fetch_active_etf_performance(
    symbols: list[str],
    chunk_size: int = 15,
    max_retries: int = 1,
) -> dict[str, dict]:
    """Fetch current price, daily change %, YTD return %, and 1-Year return % for a list of ETFs.
    Returns {symbol: {price, change_pct, return_ytd, return_1y}}.

    bug#00058: previously this issued a *single* yf.download() call for the entire
    symbol list (up to ~84 tickers). One rate-limit/network blip on that one request
    silently nulled out performance for every ETF in the batch at once (yfinance
    degrades gracefully rather than raising, so callers never saw an error). Fixed
    by splitting into small chunks — bounding the blast radius of any one failure —
    with a single retry for chunks that come back completely empty, and a short
    pause between chunks to avoid re-triggering the same rate limit. This trades a
    little extra wall-clock time (bounded: at most 2x the chunk count network calls)
    for much better resilience; the whole call already runs off the UI thread.
    """
    if not symbols:
        return {}

    chunks = [symbols[i:i + chunk_size] for i in range(0, len(symbols), chunk_size)]
    res: dict[str, dict] = {}

    for i, chunk in enumerate(chunks):
        chunk_res = _fetch_active_etf_performance_batch(chunk)
        attempt = 0
        while attempt < max_retries and all(v.get("price") is None for v in chunk_res.values()):
            time.sleep(0.5)
            chunk_res = _fetch_active_etf_performance_batch(chunk)
            attempt += 1
        res.update(chunk_res)

        if i < len(chunks) - 1:
            time.sleep(0.3)  # gentle pacing between chunks

    return res


def estimate_shares(symbol: str, weight: float, aum: float | None, price: float | None = None) -> Optional[int]:
    """Share-count estimate derived from real AUM, holding weight, and the
    holding's real market price (dollar_value / real_price).

    bug#00061 follow-up: this used to fall back to a fixed assumed average
    price ($100 US / $150 TW) whenever no real price was supplied, which could
    misstate the share count by several multiples for any symbol priced far
    from that assumption (e.g. NVDA ~$140, BRK.A ~$500+). Per user decision,
    that fabricated fallback is removed — returns None (never fabricates a
    number) when AUM, weight, or a real price is unavailable."""
    if not aum or aum <= 0 or not weight or not price or price <= 0:
        return None

    value = aum * (weight / 100.0)
    shares = int(value / price)
    return shares if shares > 0 else None


def fetch_prices_batch(symbols: list[str], chunk_size: int = 20) -> dict[str, Optional[float]]:
    """Batch-fetch real current market price for many symbols via chunked
    yf.download() calls (same chunking approach as fetch_active_etf_performance,
    sized smaller since this only needs one field per symbol).

    bug#00061 follow-up: used to attach each ETF holding's *real* price (instead
    of a fixed assumed average) before estimate_shares()/the trend engine
    consumes it. Deliberately batched across the *union* of many ETFs' top
    holdings in one caller-side pass (many active ETFs share the same mega-cap
    names) rather than fetched one-by-one per ETF, to keep total request volume
    bounded — the same rate-limit lesson as bug#00058.

    Returns {symbol: price|None}; None for any symbol yfinance has no data for
    — never fabricated or defaulted.
    """
    if not symbols:
        return {}
    uniq = sorted(set(symbols))
    chunks = [uniq[i:i + chunk_size] for i in range(0, len(uniq), chunk_size)]
    result: dict[str, Optional[float]] = {s: None for s in uniq}

    for chunk in chunks:
        try:
            with silence_output():
                data = yf.download(
                    tickers=chunk,
                    period="5d",
                    interval="1d",
                    auto_adjust=True,
                    actions=False,
                    progress=False,
                    group_by="ticker" if len(chunk) > 1 else "column",
                )
            import pandas as _pd
            for sym in chunk:
                try:
                    if len(chunk) > 1:
                        if sym in data.columns.get_level_values(0):
                            close_series = data[sym]["Close"].dropna()
                        elif "Close" in data.columns and sym in data["Close"].columns:
                            close_series = data["Close"][sym].dropna()
                        else:
                            continue
                    else:
                        close_series = data["Close"].dropna()
                    if isinstance(close_series, _pd.DataFrame):
                        close_series = close_series[sym] if sym in close_series.columns else close_series.squeeze()
                    if not close_series.empty:
                        result[sym] = _clean_float(close_series.iloc[-1])
                except Exception:
                    continue
        except Exception:
            continue

    return result


def fetch_etf_holdings(symbol: str, aum: float | None = None) -> dict | None:
    """Fetch live top-holdings + full asset-class breakdown for an ETF via yfinance.
    Returns holdings=[]/asset_classes={} (no fallback/mock data) if yfinance has no
    data for this symbol.

    `top_holdings` alone only lists Yahoo's curated top-N *named* positions, which
    for most active ETFs are equities — a fund that also holds meaningful cash,
    bonds, or other (e.g. options overlays for covered-call ETFs) would otherwise
    look 100% stock-only. `asset_classes` gives the fund's true stock/bond/cash/
    preferred/convertible/other split so the holdings panel isn't limited to stocks.
    """
    import datetime as _dt_mod

    holdings = []
    asset_classes: dict = {}
    fund_name = symbol

    try:
        with silence_output():
            ticker = yf.Ticker(symbol)
            try:
                info = ticker.info
                fund_name = info.get("longName") or info.get("shortName") or symbol
            except Exception:
                fund_name = symbol

            fd = getattr(ticker, "funds_data", None)
            if fd is not None:
                top = getattr(fd, "top_holdings", None)
                if top is not None and not (hasattr(top, "empty") and top.empty):
                    for idx, row in top.iterrows():
                        raw_pct = row.get("Holding Percent")
                        holdings.append({
                            "symbol": str(idx),
                            "name": str(row.get("Name") or ""),
                            "weight": round(float(raw_pct) * 100, 4) if raw_pct is not None else 0.0,
                            "shares": None,
                        })

                try:
                    raw_classes = getattr(fd, "asset_classes", None)
                    if raw_classes:
                        asset_classes = {
                            k: round(float(v) * 100, 4)
                            for k, v in raw_classes.items()
                            if v is not None
                        }
                except Exception:
                    pass
    except Exception:
        pass

    # bug#00061 follow-up: share-count estimation now requires each holding's real
    # market price (estimate_shares() no longer has a fixed-average-price fallback),
    # which this function doesn't fetch. The caller batch-fetches real prices across
    # all ETFs' holdings (see quotes.fetch_prices_batch) and fills in "price"/"shares"
    # afterward — left as None/unset here rather than guessed.

    return {
        "name": fund_name,
        "holdings": holdings,
        "asset_classes": asset_classes,
        "as_of_date": _dt_mod.datetime.utcnow().strftime("%Y-%m-%d"),
    }


def fetch_options_snapshot(
    underlying: str,
    min_dte: int = 28,
    max_dte: int = 60,
    strike_band_pct: float = 15.0,
) -> dict:
    """Fetch a real, current options-chain snapshot for `underlying` via yfinance.

    Scope is deliberately bounded (per explicit user decision, bug#00061):
    expiries 28-60 days out only (very near-term contracts swing on gamma/theta
    noise rather than real positioning signal), and strikes within
    +/- strike_band_pct of spot (keeps the fetch volume bounded and focuses on
    where real volume/OI concentrates, instead of pulling the entire chain).

    yfinance's option_chain() is a live snapshot only — no history — so this
    fetch alone cannot tell you whether a position is being "built"; that
    requires comparing today's snapshot against a previous real one (see
    storage.append_options_daily_snapshot / options_analysis.py). This function
    just returns one honest, real, current data point.

    Returns {"spot_price": float|None, "contracts": [...]}. contracts is [] (no
    fallback/mock data) if yfinance has nothing usable for this underlying.
    Each contract: contractSymbol, type ("call"/"put"), strike, expiry,
    lastPrice, volume, openInterest, impliedVolatility — all real fields as
    reported by yfinance, nothing derived or estimated.
    """
    import datetime as _dt_mod

    contracts: list[dict] = []
    spot_price: Optional[float] = None

    try:
        with silence_output():
            ticker = yf.Ticker(underlying)

            try:
                fi = ticker.fast_info
                spot_price = _clean_float(getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None))
            except Exception:
                spot_price = None

            if spot_price is None:
                hist = ticker.history(period="1d", auto_adjust=False)
                if not hist.empty:
                    spot_price = _clean_float(hist["Close"].iloc[-1])

            if not spot_price or spot_price <= 0:
                return {"spot_price": None, "contracts": []}

            try:
                expiries = ticker.options
            except Exception:
                expiries = ()

            today = _dt_mod.date.today()
            qualifying = []
            for exp_str in expiries:
                try:
                    exp_date = _dt_mod.datetime.strptime(exp_str, "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    continue
                dte = (exp_date - today).days
                if min_dte <= dte <= max_dte:
                    qualifying.append(exp_str)

            lo = spot_price * (1 - strike_band_pct / 100.0)
            hi = spot_price * (1 + strike_band_pct / 100.0)

            for exp_str in qualifying:
                try:
                    chain = ticker.option_chain(exp_str)
                except Exception:
                    continue
                for df, opt_type in ((chain.calls, "call"), (chain.puts, "put")):
                    if df is None or df.empty:
                        continue
                    for _, row in df.iterrows():
                        strike = _clean_float(row.get("strike"))
                        if strike is None or not (lo <= strike <= hi):
                            continue
                        contracts.append({
                            "contractSymbol": str(row.get("contractSymbol", "")),
                            "type": opt_type,
                            "strike": strike,
                            "expiry": exp_str,
                            "lastPrice": _clean_float(row.get("lastPrice")),
                            "volume": _clean_float(row.get("volume")),
                            "openInterest": _clean_float(row.get("openInterest")),
                            "impliedVolatility": _clean_float(row.get("impliedVolatility")),
                        })
    except Exception:
        pass

    return {"spot_price": spot_price, "contracts": contracts}
