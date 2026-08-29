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


_exchange_rate_cache: dict[str, tuple[float, float]] = {}
_FX_CACHE_TTL = 3600  # 1 hour — UI may still show a staler disk value
_warmup_lock = threading.Lock()


def _warmup_cache_path():
    from .storage import get_data_dir
    return get_data_dir() / "quote_warmup_cache.json"


def _load_warmup_cache() -> dict:
    try:
        import json
        data = json.loads(_warmup_cache_path().read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_warmup_cache(data: dict) -> None:
    try:
        import json
        path = _warmup_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    except Exception:
        pass


def _fx_memory_rate(pair: str, *, require_fresh: bool) -> Optional[float]:
    raw = _exchange_rate_cache.get(pair)
    if raw is None:
        return None
    value, fetched_at = raw
    if require_fresh and (time.time() - fetched_at) >= _FX_CACHE_TTL:
        return None
    return value


def cached_usdtwd_rate(default: float = 32.0) -> float:
    """UI-safe last known USDTWD rate. Never hits the network.

    Prefers in-memory, then disk (even if older than the fetch TTL), then
    `default`. A background worker calls `fetch_usdtwd_rate()` to refresh.
    """
    memory = _fx_memory_rate("USDTWD", require_fresh=False)
    if memory is not None and memory > 0:
        return memory
    with _warmup_lock:
        disk = _load_warmup_cache().get("usdtwd") or {}
    rate = disk.get("rate")
    fetched_at = disk.get("fetched_at")
    if isinstance(rate, (int, float)) and rate > 0:
        ts = float(fetched_at) if isinstance(fetched_at, (int, float)) else 0.0
        _exchange_rate_cache["USDTWD"] = (float(rate), ts)
        return float(rate)
    return default


def fetch_usdtwd_rate() -> float:
    """Get USD to TWD exchange rate from yfinance. Cached."""
    global _exchange_rate_cache
    fresh = _fx_memory_rate("USDTWD", require_fresh=True)
    if fresh is not None:
        return fresh
    with _warmup_lock:
        disk = _load_warmup_cache().get("usdtwd") or {}
    disk_rate = disk.get("rate")
    disk_ts = disk.get("fetched_at")
    if (
        isinstance(disk_rate, (int, float))
        and disk_rate > 0
        and isinstance(disk_ts, (int, float))
        and (time.time() - float(disk_ts)) < _FX_CACHE_TTL
    ):
        _exchange_rate_cache["USDTWD"] = (float(disk_rate), float(disk_ts))
        return float(disk_rate)
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
                now = time.time()
                _exchange_rate_cache["USDTWD"] = (price, now)
                with _warmup_lock:
                    payload = _load_warmup_cache()
                    payload["usdtwd"] = {"rate": price, "fetched_at": now}
                    _save_warmup_cache(payload)
                return price
    except Exception:
        pass
    return cached_usdtwd_rate(default=32.0)


def fetch_usdjpy_rate() -> float:
    """Get USD to JPY exchange rate from yfinance. Cached (same TTL mechanism as USDTWD)."""
    global _exchange_rate_cache
    fresh = _fx_memory_rate("USDJPY", require_fresh=True)
    if fresh is not None:
        return fresh
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
                _exchange_rate_cache["USDJPY"] = (price, time.time())
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


def infer_etf_leverage_factor(
    name: str = "",
    description: str = "",
    symbol: str = "",
) -> float:
    """Infer a signed ETF daily exposure multiple from provider metadata.

    Fund names normally disclose leveraged/inverse objectives (``2x``, ``3X
    Bull``, ``UltraShort``, ``正2`` and so on).  Unknown/plain ETFs return 1x;
    callers can override this through ``Position.leverage_factor``.
    """
    text = f"{name} {description}".upper().replace("×", "X")
    compact_symbol = symbol.upper().replace(".TWO", "").replace(".TW", "")

    # Taiwan exchange naming convention: leveraged tickers end in L (normally
    # 正2), while inverse tickers end in R (normally 反1).  A provider name such
    # as 正2/反1 below remains authoritative when present.
    symbol_hint: Optional[float] = None
    if re.fullmatch(r"\d{4,6}L", compact_symbol):
        symbol_hint = 2.0
    elif re.fullmatch(r"\d{4,6}R", compact_symbol):
        symbol_hint = -1.0

    chinese = re.search(r"(正|多)(?:向)?\s*([123](?:\.\d+)?)", text)
    if chinese:
        return float(chinese.group(2))
    chinese = re.search(r"(反|空)(?:向)?\s*([123](?:\.\d+)?)", text)
    if chinese:
        return -float(chinese.group(2))

    multiple_match = re.search(r"(?<!\d)([123](?:\.\d+)?)\s*X\b", text)
    multiple = float(multiple_match.group(1)) if multiple_match else None
    inverse = bool(re.search(r"\b(?:BEAR|INVERSE|SHORT)\b", text))

    if multiple is None:
        if "ULTRAPRO" in text or re.search(r"\bTRIPLE\b", text):
            multiple = 3.0
        elif "ULTRASHORT" in text:
            multiple = 2.0
            inverse = True
        elif re.search(r"\bULTRA\b|\bDOUBLE\b", text):
            multiple = 2.0
        elif inverse:
            multiple = 1.0

    if multiple is not None:
        return -multiple if inverse else multiple
    return symbol_hint if symbol_hint is not None else 1.0


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
        needs_quote = p.market_price is None or p.market_value is None or p.prev_close is None
        needs_etf_factor = p.instrument_type == "etf" and p.leverage_factor is None
        if needs_quote or needs_etf_factor:
            yf_symbol = _normalize_symbol_for_yf(p.symbol, p.instrument_type, p.currency)
            try:
                with silence_output():
                    ticker = yf.Ticker(yf_symbol)
                    if needs_quote:
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

                    if needs_etf_factor:
                        try:
                            info = ticker.info or {}
                        except Exception:
                            info = {}
                        p.leverage_factor = infer_etf_leverage_factor(
                            str(info.get("longName") or info.get("shortName") or ""),
                            str(info.get("longBusinessSummary") or info.get("description") or ""),
                            p.symbol,
                        )
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
    Returns None if unavailable. Cached per symbol (TTL + disk). The dashboard
    render path uses `cached_beta()` and must not call this.
    """
    # For options, use the underlying stock's beta
    lookup_symbol = underlying if (instrument_type == "option" and underlying) else symbol
    yf_symbol = _normalize_symbol_for_yf(lookup_symbol, "stock", currency)

    cached = cached_beta(symbol, instrument_type, underlying, currency)
    if cached is not None or (
        yf_symbol in _beta_cache
        and (time.time() - _beta_cache[yf_symbol][1]) < _BETA_CACHE_TTL
    ):
        if cached is not None:
            return cached
        return _beta_cache[yf_symbol][0]

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

    now = time.time()
    _beta_cache[yf_symbol] = (beta_val, now)
    with _warmup_lock:
        payload = _load_warmup_cache()
        betas = dict(payload.get("betas") or {})
        betas[yf_symbol] = {"beta": beta_val, "fetched_at": now}
        payload["betas"] = betas
        _save_warmup_cache(payload)
    return beta_val


def cached_beta(
    symbol: str,
    instrument_type: str = "stock",
    underlying: Optional[str] = None,
    currency: str = "USD",
) -> Optional[float]:
    """UI-safe last known beta. Never hits the network. Expired disk values are ignored."""
    lookup_symbol = underlying if (instrument_type == "option" and underlying) else symbol
    yf_symbol = _normalize_symbol_for_yf(lookup_symbol, "stock", currency)

    cached = _beta_cache.get(yf_symbol)
    if cached is not None and (time.time() - cached[1]) < _BETA_CACHE_TTL:
        return cached[0]

    with _warmup_lock:
        item = (_load_warmup_cache().get("betas") or {}).get(yf_symbol) or {}
    fetched_at = item.get("fetched_at")
    if not isinstance(fetched_at, (int, float)):
        return None
    if (time.time() - float(fetched_at)) >= _BETA_CACHE_TTL:
        return None
    beta_val = item.get("beta")
    parsed = float(beta_val) if isinstance(beta_val, (int, float)) else None
    _beta_cache[yf_symbol] = (parsed, float(fetched_at))
    return parsed


_rf_cache: dict[str, tuple[float, float]] = {}
_RF_CACHE_TTL = 6 * 3600  # 6 hours — the short-rate barely moves intraday


def fetch_risk_free_rate(default: float = 0.04) -> float:
    """回傳無風險利率（小數，例如 0.043）供 Black-Scholes 計算使用。

    以 ^IRX（13 週美國國庫券殖利率，yfinance 報價為年化百分比，如 4.35 代表 4.35%）
    為來源，除以 100 轉為小數；抓取失敗或無資料時回退 `default`。以 6 小時 TTL 快取，
    避免每次渲染都發出阻塞性網路請求（比照 fetch_beta 的快取模式）。
    """
    cached = _rf_cache.get("^IRX")
    if cached is not None and (time.time() - cached[1]) < _RF_CACHE_TTL:
        return cached[0]

    rate = default
    try:
        with silence_output():
            ticker = yf.Ticker("^IRX")
            val: Optional[float] = None
            try:
                fi = ticker.fast_info
                val = _clean_float(getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None))
            except Exception:
                val = None
            if val is None:
                hist = ticker.history(period="5d", auto_adjust=False)
                if not hist.empty:
                    val = _clean_float(hist["Close"].iloc[-1])
            if val is not None and val > 0:
                rate = val / 100.0
    except Exception:
        pass

    _rf_cache["^IRX"] = (rate, time.time())
    return rate


def cached_risk_free_rate(default: float = 0.04) -> float:
    """Non-blocking: return the last fetched ^IRX rate if present in the module
    cache, else `default`. Does NOT trigger any network request, so it is safe to
    call on the UI render path (the Dashboard card uses this; a background worker
    calls fetch_risk_free_rate() to warm the same cache, keeping card and page
    aligned on one rate)."""
    cached = _rf_cache.get("^IRX")
    return cached[0] if cached is not None else default


# ── bug#00084: 已結束總經事件（CPI/FED）結論更新 ────────────────────────────
# CPI 月增/年增：FRED 官方已公佈指數值直接計算，非估算。
# FED 會議決議：FRED 官方目標利率區間（DFEDTARU/DFEDTARL）真實變動，非估算。
# 下次會議升降息機率：Fed Funds 期貨市場價格反推——真實市場定價，但屬簡化版
# 方法論（未依會議在當月中的日期位置做逐日加權，不等同 CME FedWatch 精確值），
# 已於呼叫端文字註明「僅供參考」。三者共通原則：任一步驟缺資料（缺
# FRED_API_KEY／API 失敗／期貨無報價）一律誠實回傳 None，不以預設值或估計值
# 填補（比照全專案「不臆測」慣例）。

_FRED_CACHE: dict[str, tuple[list, float]] = {}
_FRED_CACHE_TTL = 6 * 3600  # 6 hours — 總經數據非日內更新頻率
_FRED_FAILURES: dict[str, str] = {}


def fred_failure_reason(*series_ids: str) -> Optional[str]:
    """Return the most actionable failure reason for the requested FRED series."""
    priority = (
        "missing_key",
        "auth_error",
        "network_error",
        "http_error",
        "invalid_response",
        "no_data",
    )
    reasons = {_FRED_FAILURES.get(series_id) for series_id in series_ids}
    return next((reason for reason in priority if reason in reasons), None)


def fred_api_key() -> Optional[str]:
    """讀取 FRED_API_KEY 環境變數（可放在專案根目錄 .env，main() 已呼叫
    load_dotenv() 載入）。未設定時回傳 None，呼叫端須誠實顯示「尚未設定」
    而非假裝有資料。"""
    key = os.environ.get("FRED_API_KEY", "").strip()
    return key or None


def fetch_fred_series(series_id: str, limit: int = 15) -> Optional[list]:
    """向 FRED（Federal Reserve Economic Data）API 取得 series_id 最近 `limit`
    筆觀測值，回傳 [(date, value), ...]（新到舊排序），皆為 FRED 官方已公佈
    真實數值。無 API key、網路失敗、或該序列查無資料時回傳 None（不捏造）。"""
    api_key = fred_api_key()
    if not api_key:
        _FRED_FAILURES[series_id] = "missing_key"
        return None

    cache_key = f"{series_id}:{limit}"
    cached = _FRED_CACHE.get(cache_key)
    if cached is not None and (time.time() - cached[1]) < _FRED_CACHE_TTL:
        _FRED_FAILURES.pop(series_id, None)
        return cached[0]

    import requests
    from datetime import datetime as _dt

    try:
        resp = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={
                "series_id": series_id,
                "api_key": api_key,
                "file_type": "json",
                "sort_order": "desc",
                "limit": limit,
            },
            timeout=10,
        )
    except requests.RequestException:
        _FRED_FAILURES[series_id] = "network_error"
        return None

    if not resp.ok:
        reason = "http_error"
        try:
            error_message = str(resp.json().get("error_message", "")).lower()
        except (ValueError, AttributeError):
            error_message = ""
        if resp.status_code in (400, 401, 403) and "api_key" in error_message:
            reason = "auth_error"
        _FRED_FAILURES[series_id] = reason
        return None

    try:
        data = resp.json()
        out = []
        for obs in data.get("observations", []):
            raw_val = obs.get("value")
            if raw_val in (None, ".", ""):
                continue
            try:
                d = _dt.strptime(obs["date"], "%Y-%m-%d").date()
                out.append((d, float(raw_val)))
            except (ValueError, KeyError):
                continue
        if not out:
            _FRED_FAILURES[series_id] = "no_data"
            return None
    except (ValueError, AttributeError, TypeError):
        _FRED_FAILURES[series_id] = "invalid_response"
        return None

    _FRED_CACHE[cache_key] = (out, time.time())
    _FRED_FAILURES.pop(series_id, None)
    return out


def compute_cpi_conclusion() -> Optional[dict]:
    """回傳最新一期 CPI 的月增/年增百分比。月增用 CPIAUCSL（季節調整後），
    年增用 CPIAUCNS（未季調）——比照 BLS 官方新聞稿慣例（YoY 本身已隱含消去
    季節性，媒體慣例改採未季調數列）。皆由 FRED 官方已公佈指數值直接計算，
    非估算。資料不足或 API 不可用時回傳 None。"""
    sa = fetch_fred_series("CPIAUCSL", limit=3)
    # FRED may include one not-yet-released "." observation in the requested
    # limit. Ask for one extra row so 14 numeric periods remain available for
    # the previous month's YoY comparison.
    nsa = fetch_fred_series("CPIAUCNS", limit=15)
    if not sa or len(sa) < 2 or not nsa or len(nsa) < 13:
        return None

    latest_date, latest_sa = sa[0]
    prev_sa = sa[1][1]
    latest_nsa = nsa[0][1]
    yoy_base_nsa = nsa[12][1]
    if prev_sa == 0 or yoy_base_nsa == 0:
        return None

    mom_pct = (latest_sa / prev_sa - 1.0) * 100.0
    yoy_pct = (latest_nsa / yoy_base_nsa - 1.0) * 100.0
    prev_mom_pct = None
    if len(sa) >= 3 and sa[2][1] != 0:
        prev_mom_pct = (sa[1][1] / sa[2][1] - 1.0) * 100.0
    prev_yoy_pct = None
    if len(nsa) >= 14 and nsa[13][1] != 0:
        prev_yoy_pct = (nsa[1][1] / nsa[13][1] - 1.0) * 100.0

    return {
        "as_of": latest_date,
        "mom_pct": mom_pct,
        "yoy_pct": yoy_pct,
        "prev_mom_pct": prev_mom_pct,
        "mom_change_pp": mom_pct - prev_mom_pct if prev_mom_pct is not None else None,
        "prev_yoy_pct": prev_yoy_pct,
        "yoy_change_pp": yoy_pct - prev_yoy_pct if prev_yoy_pct is not None else None,
    }


def compute_core_cpi_conclusion() -> Optional[dict]:
    """回傳最新一期「核心 CPI」（排除食物與能源）的月增/年增百分比與較上期變動比較。
    月增用 CPILFESL（季節調整後），年增用 CPILFENS（未季調）——與 BLS 官方慣例一致。
    由 FRED 官方指數值直接計算，非估算。"""
    sa = fetch_fred_series("CPILFESL", limit=4)
    nsa = fetch_fred_series("CPILFENS", limit=15)
    if not sa or len(sa) < 2 or not nsa or len(nsa) < 13:
        return None

    latest_date, latest_sa = sa[0]
    prev_sa = sa[1][1]
    latest_nsa = nsa[0][1]
    yoy_base_nsa = nsa[12][1]
    if prev_sa == 0 or yoy_base_nsa == 0:
        return None

    mom_pct = (latest_sa / prev_sa - 1.0) * 100.0
    yoy_pct = (latest_nsa / yoy_base_nsa - 1.0) * 100.0

    prev_mom_pct = None
    mom_change_pp = None
    if len(sa) >= 3 and sa[2][1] > 0:
        prev_mom_pct = (sa[1][1] / sa[2][1] - 1.0) * 100.0
        mom_change_pp = mom_pct - prev_mom_pct

    prev_yoy_pct = None
    yoy_change_pp = None
    if len(nsa) >= 14 and nsa[13][1] > 0:
        prev_yoy_pct = (nsa[1][1] / nsa[13][1] - 1.0) * 100.0
        yoy_change_pp = yoy_pct - prev_yoy_pct

    if mom_change_pp is not None and mom_change_pp < 0:
        interpretation = (
            f"核心 CPI 月增率 ({mom_pct:.2f}%) 較上期 ({prev_mom_pct:.2f}%) 放緩 {abs(mom_change_pp):.2f}pp，"
            f"顯示核心物價漲幅收斂，通膨壓力減緩，有利聯準會維持降息與寬鬆空間。"
        )
    elif mom_change_pp is not None and mom_change_pp > 0:
        interpretation = (
            f"核心 CPI 月增率 ({mom_pct:.2f}%) 較上期 ({prev_mom_pct:.2f}%) 回升 {abs(mom_change_pp):.2f}pp，"
            f"反映粘性通膨反彈壓力，降息時程與寬鬆預期可能延後。"
        )
    else:
        interpretation = f"核心 CPI 月增率 ({mom_pct:.2f}%) 與上期相當，核心通膨趨勢維持平穩。"

    return {
        "as_of": latest_date,
        "mom_pct": mom_pct,
        "yoy_pct": yoy_pct,
        "prev_mom_pct": prev_mom_pct,
        "mom_change_pp": mom_change_pp,
        "prev_yoy_pct": prev_yoy_pct,
        "yoy_change_pp": yoy_change_pp,
        "interpretation": interpretation,
    }


def compute_pce_conclusion() -> Optional[dict]:
    """回傳最新一期「核心 PCE」（Fed 首要通膨指標）的月增/年增百分比與較上期變動比較。
    使用 PCEPILFE 指數計算，為官方已公佈真實數值。"""
    idx = fetch_fred_series("PCEPILFE", limit=15)
    if not idx or len(idx) < 13:
        return None

    latest_date, latest = idx[0]
    prev = idx[1][1]
    yoy_base = idx[12][1]
    if prev == 0 or yoy_base == 0:
        return None

    mom_pct = (latest / prev - 1.0) * 100.0
    yoy_pct = (latest / yoy_base - 1.0) * 100.0

    prev_mom_pct = None
    mom_change_pp = None
    if len(idx) >= 3 and idx[2][1] > 0:
        prev_mom_pct = (idx[1][1] / idx[2][1] - 1.0) * 100.0
        mom_change_pp = mom_pct - prev_mom_pct

    prev_yoy_pct = None
    yoy_change_pp = None
    if len(idx) >= 14 and idx[13][1] > 0:
        prev_yoy_pct = (idx[1][1] / idx[13][1] - 1.0) * 100.0
        yoy_change_pp = yoy_pct - prev_yoy_pct

    if mom_change_pp is not None and mom_change_pp < 0:
        interpretation = (
            f"核心 PCE 月增率 ({mom_pct:.2f}%) 較上期 ({prev_mom_pct:.2f}%) 放緩 {abs(mom_change_pp):.2f}pp，"
            f"強化 Fed 認為通膨邁向 2% 目標的信心，有利市場風險偏好。"
        )
    elif mom_change_pp is not None and mom_change_pp > 0:
        interpretation = (
            f"核心 PCE 月增率 ({mom_pct:.2f}%) 較上期 ({prev_mom_pct:.2f}%) 擴大 {abs(mom_change_pp):.2f}pp，"
            f"反映消費端通膨頑強，利率緊縮時間可能拉長。"
        )
    else:
        interpretation = f"核心 PCE 月增率 ({mom_pct:.2f}%) 與上期相當，通膨趨勢符合預期。"

    return {
        "as_of": latest_date,
        "mom_pct": mom_pct,
        "yoy_pct": yoy_pct,
        "prev_mom_pct": prev_mom_pct,
        "mom_change_pp": mom_change_pp,
        "prev_yoy_pct": prev_yoy_pct,
        "yoy_change_pp": yoy_change_pp,
        "interpretation": interpretation,
    }


def compute_unemployment_conclusion() -> Optional[dict]:
    """回傳最新一期失業率（FRED UNRATE）與較上期變動（百分點）比較與解析。"""
    ur = fetch_fred_series("UNRATE", limit=4)
    if not ur or len(ur) < 2:
        return None

    latest_date, latest = ur[0]
    prev = ur[1][1]
    change_pp = latest - prev

    prev_change_pp = None
    if len(ur) >= 3:
        prev_change_pp = prev - ur[2][1]

    if change_pp > 0:
        interpretation = (
            f"失業率 ({latest:.1f}%) 較上期 ({prev:.1f}%) 上升 {abs(change_pp):.1f}pp，"
            f"顯示就業市場鬆動與勞動降溫，減輕薪資通膨壓力、拉升降息預期。"
        )
    elif change_pp < 0:
        interpretation = (
            f"失業率 ({latest:.1f}%) 較上期 ({prev:.1f}%) 下降 {abs(change_pp):.1f}pp，"
            f"顯示勞工市場持續緊繃，經濟與就業韌性強勁。"
        )
    else:
        interpretation = f"失業率維持於 {latest:.1f}%，就業市場供需保持平衡。"

    return {
        "as_of": latest_date,
        "rate_pct": latest,
        "prev_pct": prev,
        "change_pp": change_pp,
        "prev_change_pp": prev_change_pp,
        "interpretation": interpretation,
    }


def compute_nfp_conclusion() -> Optional[dict]:
    """回傳最新一期非農就業（FRED PAYEMS）月增人數與較上月變動比較與解析。"""
    pe = fetch_fred_series("PAYEMS", limit=4)
    if not pe or len(pe) < 2:
        return None

    latest_date, latest_k = pe[0]
    prev_k = pe[1][1]
    latest_change = (latest_k - prev_k) * 1000.0

    prev_change = None
    change_diff = None
    if len(pe) >= 3:
        prev_change = (prev_k - pe[2][1]) * 1000.0
        change_diff = latest_change - prev_change

    if change_diff is not None and change_diff < 0:
        interpretation = (
            f"非農新增就業 ({latest_change / 1000.0:+,.0f}K) 較上月 ({prev_change / 1000.0:+,.0f}K) 放緩 {abs(change_diff) / 1000.0:,.0f}K，"
            f"就業增長適度降溫，有助緩解薪資壓力。"
        )
    elif change_diff is not None and change_diff > 0:
        interpretation = (
            f"非農新增就業 ({latest_change / 1000.0:+,.0f}K) 較上月 ({prev_change / 1000.0:+,.0f}K) 增加 {abs(change_diff) / 1000.0:,.0f}K，"
            f"顯示就業市場擴張加速，反映美國經濟軟著陸與經濟韌性。"
        )
    else:
        interpretation = f"非農新增就業 ({latest_change / 1000.0:+,.0f}K) 與上月增幅相近，就業成長步調穩定。"

    return {
        "as_of": latest_date,
        "change": latest_change,
        "level": latest_k * 1000.0,
        "prev_change": prev_change,
        "change_diff": change_diff,
        "interpretation": interpretation,
    }


def compute_fed_funds_rate_conclusion() -> Optional[dict]:
    """回傳最新有效聯邦資金利率（FRED FEDFUNDS）與較上期變動比較與解析。"""
    ff = fetch_fred_series("FEDFUNDS", limit=3)
    if not ff or len(ff) < 2:
        return None

    latest_date, latest = ff[0]
    prev = ff[1][1]
    change_pp = latest - prev

    if change_pp < 0:
        interpretation = f"有效聯邦資金利率 ({latest:.2f}%) 較上期下降 {abs(change_pp):.2f}pp，反映央行降息進入寬鬆週期。"
    elif change_pp > 0:
        interpretation = f"有效聯邦資金利率 ({latest:.2f}%) 較上期調升 {abs(change_pp):.2f}pp，反映央行維持升息緊縮姿態。"
    else:
        interpretation = f"有效聯邦資金利率維持在 {latest:.2f}%，聯準會處於政策觀望與既有緊縮效果評估期。"

    return {
        "as_of": latest_date,
        "rate_pct": latest,
        "prev_pct": prev,
        "change_pp": change_pp,
        "interpretation": interpretation,
    }


def fetch_latest_macro_readings() -> dict:
    """彙整 UpcomingEventsScreen 追蹤的重要總經指標「最新一期已公佈數值」，全部
    取自 FRED 官方 API。回傳 dict，key 為指標代號、value 為各 compute_* 函式的結果
    （查無資料或無 API key 時該項為 None，不捏造）：
        core_cpi      → compute_core_cpi_conclusion()
        core_pce      → compute_pce_conclusion()
        unemployment  → compute_unemployment_conclusion()
        nfp           → compute_nfp_conclusion()
        fed_funds     → compute_fed_funds_rate_conclusion()
    """
    return {
        "core_cpi": compute_core_cpi_conclusion(),
        "core_pce": compute_pce_conclusion(),
        "unemployment": compute_unemployment_conclusion(),
        "nfp": compute_nfp_conclusion(),
        "fed_funds": compute_fed_funds_rate_conclusion(),
    }


def compute_fed_decision_conclusion(meeting_date) -> Optional[dict]:
    """回傳某次 FOMC 會議前後，聯邦資金目標利率區間的真實變動（FRED
    DFEDTARU/DFEDTARL 官方每日數值，非估算）。取會議日前最近一筆與會議日起
    5 天內最新一筆做比較。查無足夠資料時回傳 None。"""
    from datetime import timedelta as _td

    upper = fetch_fred_series("DFEDTARU", limit=30)
    lower = fetch_fred_series("DFEDTARL", limit=30)
    if not upper or not lower:
        return None

    def _pick(series, before: bool):
        for d, v in series:  # series 為新到舊排序
            if before and d < meeting_date:
                return v
            if not before and meeting_date <= d <= meeting_date + _td(days=5):
                return v
        return None

    up_before, up_after = _pick(upper, True), _pick(upper, False)
    lo_before, lo_after = _pick(lower, True), _pick(lower, False)
    if None in (up_before, up_after, lo_before, lo_after):
        return None

    delta_bps = round((up_after - up_before) * 100)
    return {
        "range_before": (lo_before, up_before),
        "range_after": (lo_after, up_after),
        "delta_bps": delta_bps,
    }


_FED_FUTURES_MONTH_CODES = "FGHJKMNQUVXZ"  # CME 期貨月份代碼：Jan..Dec
_FED_FUTURES_CACHE: dict[str, tuple] = {}
_FED_FUTURES_CACHE_TTL = 3600  # 1 hour — 期貨盤中會變動


def _fedfunds_futures_symbol_candidates(meeting_date) -> list:
    """建構 CME 30-Day Fed Funds Futures（ZQ）對應會議月份合約的 Yahoo Finance
    代碼候選清單。Yahoo 對 CBOT 期貨代碼慣例可能隨時間調整，故列出多個候選
    依序嘗試，全數失敗才視為無資料（不捏造）。"""
    code = _FED_FUTURES_MONTH_CODES[meeting_date.month - 1]
    yy = meeting_date.year % 100
    return [
        f"ZQ{code}{yy:02d}.CBT",
        f"ZQ{code}{yy:02d}=F",
    ]


def fetch_fedfunds_futures_price(meeting_date) -> Optional[float]:
    """取得對應會議月份的 Fed Funds 期貨（ZQ）最新收盤價，用於推算市場隱含
    利率（100 - price）。找不到報價時回傳 None（不捏造）。"""
    cache_key = meeting_date.strftime("%Y-%m")
    cached = _FED_FUTURES_CACHE.get(cache_key)
    if cached is not None and (time.time() - cached[1]) < _FED_FUTURES_CACHE_TTL:
        return cached[0]

    price: Optional[float] = None
    for sym in _fedfunds_futures_symbol_candidates(meeting_date):
        try:
            with silence_output():
                t = yf.Ticker(sym)
                hist = t.history(period="5d", auto_adjust=False)
                if not hist.empty:
                    price = _clean_float(hist["Close"].iloc[-1])
                    if price is not None:
                        break
        except Exception:
            continue

    _FED_FUTURES_CACHE[cache_key] = (price, time.time())
    return price


def compute_next_fed_meeting_probability(meeting_date) -> Optional[dict]:
    """依 Fed Funds 期貨市場價格，簡化推算下次 FOMC 會議升息/降息/不變的機率。

    方法論（簡化版，非 CME FedWatch 完整逐日加權公式）：
      1. 取得會議當月 Fed Funds 期貨收盤價，隱含月平均利率 = 100 - price。
      2. 以 FRED DFF（有效聯邦資金利率）最新一筆真實數值為基準利率。
      3. 假設利率變動皆以 25bp 為單位，將期貨隱含利率與基準利率的差距換算
         為升息/降息機率（差距 25bp = 100% 機率）。
      此法未依會議發生在當月中的日期位置做逐日加權（CME 官方方法會依此加
      權），故僅供參考、非精確值，呼叫端文字需註明。任一步驟缺資料即誠實
      回傳 None。
    """
    futures_price = fetch_fedfunds_futures_price(meeting_date)
    if futures_price is None:
        return None

    implied_rate = 100.0 - futures_price

    dff = fetch_fred_series("DFF", limit=5)
    if not dff:
        return None
    base_rate = dff[0][1]

    delta_bps = (implied_rate - base_rate) * 100.0
    hike_prob = max(0.0, min(1.0, delta_bps / 25.0))
    cut_prob = max(0.0, min(1.0, -delta_bps / 25.0))
    hold_prob = max(0.0, 1.0 - hike_prob - cut_prob)

    return {
        "implied_rate": implied_rate,
        "base_rate": base_rate,
        "delta_bps": delta_bps,
        "hike_prob": hike_prob,
        "cut_prob": cut_prob,
        "hold_prob": hold_prob,
    }


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


def fetch_option_daily_closes(
    contract_symbol: str,
    start_date: "datetime",
    end_date: "datetime",
) -> list[tuple["date", float]]:
    """Daily last close for one OCC option symbol. Empty list on failure."""
    if not contract_symbol:
        return []
    return fetch_benchmark_history(contract_symbol, start_date, end_date)


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


EARNINGS_CALENDAR_MAX_WORKERS = 4


def earnings_calendar_workers(symbol_count: int) -> int:
    """Cap Yahoo earnings lookups so a login stampede cannot open one worker per ticker."""
    return max(1, min(EARNINGS_CALENDAR_MAX_WORKERS, int(symbol_count or 0)))


def fetch_earnings_calendar(
    symbols: list[str],
    timezone_name: str = "Asia/Taipei",
) -> dict[str, tuple[list, Optional[object], Optional[str], Optional[str]]]:
    """
    Fetch earnings calendar for multiple symbols from yfinance in parallel.

    Returns {symbol: (dates_list, info_date, time_str, period_str)} where:
    - dates_list : list[date] from t.calendar["Earnings Date"]
    - info_date  : precise date in ``timezone_name`` from earningsTimestampStart
    - time_str   : "HH:MM" in ``timezone_name``
    - period_str : "盤前" | "盤後" based on US Eastern time
    """
    import concurrent.futures
    from datetime import datetime as _dt, timedelta as _td, timezone as _timezone

    try:
        tz_target = _zoneinfo.ZoneInfo(timezone_name)
    except (KeyError, ValueError, TypeError):
        tz_target = _TZ_TW or _timezone(_td(hours=8))

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
                    dt_local = _dt.fromtimestamp(ts, tz_target)
                    info_date = dt_local.date()
                    time_str = dt_local.strftime("%H:%M")
                    dt_us = _dt.fromtimestamp(ts, _TZ_US)
                    period_str = "盤前" if dt_us.hour < 12 else "盤後"
                return symbol, dates, info_date, time_str, period_str
        except Exception:
            return symbol, [], None, None, None

    result: dict = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=earnings_calendar_workers(len(symbols))
    ) as ex:
        for sym, d, id_, ts_, ps_ in ex.map(_fetch_one, symbols):
            result[sym] = (d, id_, ts_, ps_)
    return result


def fetch_next_earnings_dates(underlyings: list[str]) -> dict[str, str]:
    """回傳 {underlying(大寫): 'YYYY-MM-DD'} 的下次(已知)財報日 (bug#00068)。

    薄包裝 fetch_earnings_calendar()——取其 info_date(精確下次財報日)，無則以
    calendar 的最近一個未來日期補上；查不到的標的不列入。供期權每日快照記錄財報日，
    讓 divergence 分析能標記「區間含財報」的預期性波動。
    """
    if not underlyings:
        return {}
    from datetime import datetime as _dt, date as _date
    data = fetch_earnings_calendar(list(underlyings))
    out: dict[str, str] = {}
    for sym, (dates_list, info_date, _t, _p) in data.items():
        chosen = info_date
        if chosen is None and dates_list:
            future = sorted(d for d in dates_list if isinstance(d, _date))
            chosen = future[0] if future else None
        if chosen is not None:
            try:
                out[sym.upper()] = chosen.strftime("%Y-%m-%d")
            except Exception:
                pass
    return out


def fetch_earnings_actuals(symbol: str) -> Optional[dict]:
    """Return latest reported Revenue/CAPEX/EBIT/FCF and same-quarter YoY.

    Current values are returned whenever available. YoY uses the quarter four
    columns earlier and remains ``None`` when the provider has fewer than five
    quarters. CAPEX is normalized to a positive spend amount before comparison.
    Legacy gross-profit/net-income YoY keys remain for compatibility.
    """
    try:
        with silence_output():
            t = yf.Ticker(symbol)
            income_df = t.quarterly_income_stmt
            cashflow_df = t.quarterly_cashflow
    except Exception:
        return None
    info = {}
    try:
        with silence_output():
            fetched_info = t.info
        if isinstance(fetched_info, dict):
            info = fetched_info
    except Exception:
        pass

    income_empty = income_df is None or income_df.empty
    cashflow_empty = cashflow_df is None or cashflow_df.empty
    if income_empty and cashflow_empty:
        return None

    def _clean_statement_value(value) -> Optional[float]:
        try:
            cleaned = float(value)
        except (TypeError, ValueError):
            return None
        if math.isnan(cleaned):
            return None
        return cleaned

    def _row_metric(df, row_names: tuple[str, ...], absolute: bool = False) -> Optional[dict]:
        if df is None or df.empty:
            return None
        row_name = next((name for name in row_names if name in df.index), None)
        if row_name is None:
            return None
        row = df.loc[row_name]
        try:
            latest = _clean_statement_value(row.iloc[0])
        except IndexError:
            return None
        if latest is None:
            return None
        if absolute:
            latest = abs(latest)

        prior = None
        if len(row) >= 5:
            prior = _clean_statement_value(row.iloc[4])
            if prior is not None and absolute:
                prior = abs(prior)
        yoy = None
        if prior not in (None, 0):
            yoy = (latest / prior - 1.0) * 100.0
        return {"value": latest, "prior_year_value": prior, "yoy_pct": yoy}

    revenue = _row_metric(income_df, ("Total Revenue",))
    capex = _row_metric(cashflow_df, ("Capital Expenditure", "Capital Expenditures"), absolute=True)
    ebit = _row_metric(income_df, ("EBIT",))
    fcf = _row_metric(cashflow_df, ("Free Cash Flow",))

    gross = _row_metric(income_df, ("Gross Profit",))
    net_income = _row_metric(income_df, ("Net Income", "Net Income Common Stockholders"))

    metrics = {
        "revenue": revenue,
        "capex": capex,
        "ebit": ebit,
        "fcf": fcf,
    }
    if not any(metrics.values()):
        return None

    period_label = None
    try:
        period_df = income_df if not income_empty else cashflow_df
        col0 = period_df.columns[0]
        period_label = f"{col0.year}Q{(col0.month - 1) // 3 + 1}"
    except Exception:
        pass

    return {
        "period": period_label,
        "currency": info.get("financialCurrency") or info.get("currency") or "USD",
        "metrics": metrics,
        "revenue_yoy": revenue["yoy_pct"] if revenue else None,
        "gross_profit_yoy": gross["yoy_pct"] if gross else None,
        "net_income_yoy": net_income["yoy_pct"] if net_income else None,
    }


def fetch_earnings_actuals_batch(symbols: list[str]) -> dict:
    """並行呼叫 fetch_earnings_actuals()，比照 fetch_earnings_calendar() 的平行
    抓取模式，避免逐檔序列等待拖慢畫面。"""
    import concurrent.futures
    result: dict = {}
    if not symbols:
        return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(symbols))) as ex:
        futs = {ex.submit(fetch_earnings_actuals, sym): sym for sym in symbols}
        for fut in concurrent.futures.as_completed(futs):
            sym = futs[fut]
            try:
                result[sym] = fut.result()
            except Exception:
                result[sym] = None
    return result


def _clean_number(value) -> Optional[float]:
    try:
        cleaned = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(cleaned):
        return None
    return cleaned


def _to_date(value):
    from datetime import date as date_type, datetime as dt_cls

    if isinstance(value, date_type) and not isinstance(value, dt_cls):
        return value
    try:
        ts = value.tz_convert("America/New_York") if getattr(value, "tzinfo", None) else value
    except (TypeError, ValueError, AttributeError):
        ts = value
    try:
        return ts.date()
    except AttributeError:
        return None


def _earnings_et_date(event_date, event_dt):
    if event_dt is None or _TZ_US is None:
        return event_date
    try:
        localized = event_dt
        if localized.tzinfo is None:
            localized = localized.replace(tzinfo=_TZ_US)
        return localized.astimezone(_TZ_US).date()
    except Exception:
        return event_date


def _is_pre_market(period: Optional[str], event_dt) -> bool:
    if period == "盤前":
        return True
    if period == "盤後":
        return False
    if event_dt is None or _TZ_US is None:
        return False
    try:
        localized = event_dt if event_dt.tzinfo is not None else event_dt.replace(tzinfo=_TZ_US)
        return localized.astimezone(_TZ_US).hour < 12
    except Exception:
        return False


def _series_value(row, *needles: str) -> Optional[float]:
    index = getattr(row, "index", [])
    for needle in needles:
        needle_l = needle.lower()
        for col in index:
            if needle_l in str(col).lower():
                return _clean_number(row[col])
    if hasattr(row, "get"):
        for needle in needles:
            found = _clean_number(row.get(needle))
            if found is not None:
                return found
    return None


def _verdict_from_earnings_row(row) -> Optional[str]:
    reported = _series_value(row, "Reported EPS", "reportedEPS")
    estimate = _series_value(row, "EPS Estimate", "epsEstimate")
    surprise = _series_value(row, "Surprise(%)", "surprisePercent")
    if reported is not None and estimate is not None:
        if reported > estimate:
            return "beat"
        if reported < estimate:
            return "miss"
        return "meet"
    if surprise is not None:
        if surprise > 0:
            return "beat"
        if surprise < 0:
            return "miss"
        return "meet"
    return None


def _match_earnings_row(dates_df, event_date, event_dt):
    if dates_df is None or getattr(dates_df, "empty", True):
        return None
    et_date = _earnings_et_date(event_date, event_dt)
    best = None
    best_delta = None
    for idx, row in dates_df.iterrows():
        row_date = _to_date(idx)
        if row_date is None:
            continue
        delta = abs((row_date - et_date).days)
        if delta <= 1 and (best_delta is None or delta < best_delta):
            best = row
            best_delta = delta
    return best


def _bar_close(bars, session):
    for session_date, close in bars:
        if session_date == session:
            return close
    return None


def _post_earnings_price_change(bars, event_date, event_dt, period: Optional[str]):
    """Close-to-close move from the pre-release session to +3 NYSE sessions.

    After-hours: start at the announcement session close. Pre-market: start at
    the previous session close. The end session is always three trading
    sessions after that baseline — Friday after-hours therefore lands on
    Wednesday, not Monday. Missing bars stay None.
    """
    from datetime import timedelta

    from .market_sessions import NYSESessionCalendar

    if not bars:
        return None, None
    et_date = _earnings_et_date(event_date, event_dt)
    pre_market = _is_pre_market(period, event_dt)
    cal = NYSESessionCalendar()
    if pre_market:
        start_session = cal.latest_session_on_or_before(et_date - timedelta(days=1))
    elif cal.is_session(et_date):
        start_session = et_date
    else:
        start_session = cal.latest_session_on_or_before(et_date)
    try:
        end_session = cal.shift(start_session, 3)
    except ValueError:
        return None, None
    start = _bar_close(bars, start_session)
    end = _bar_close(bars, end_session)
    if start in (None, 0) or end is None:
        return None, None
    return (end / start - 1.0) * 100.0, end_session


def fetch_earnings_reaction(
    symbol: str,
    event_date,
    event_dt=None,
    period: Optional[str] = None,
) -> Optional[dict]:
    """EPS vs estimate plus the close-to-close move over +3 NYSE sessions.

    After-hours: start from the announcement session close (still pre-release).
    Pre-market: start from the previous session close. The end close is the
    third trading session after that baseline. Missing EPS or an incomplete
    price window stays None — never invented.
    """
    from datetime import timedelta

    dates_df = None
    try:
        with silence_output():
            ticker = yf.Ticker(symbol)
            getter = getattr(ticker, "get_earnings_dates", None)
            if callable(getter):
                dates_df = getter(limit=12)
            elif getattr(ticker, "earnings_dates", None) is not None:
                dates_df = ticker.earnings_dates
    except Exception:
        dates_df = None

    row = _match_earnings_row(dates_df, event_date, event_dt)
    verdict = _verdict_from_earnings_row(row) if row is not None else None
    surprise = (
        _series_value(row, "Surprise(%)", "surprisePercent") if row is not None else None
    )

    et_date = _earnings_et_date(event_date, event_dt)
    bars = fetch_benchmark_history(
        symbol,
        et_date - timedelta(days=10),
        et_date + timedelta(days=21),
    )
    price_change_pct, price_end_date = _post_earnings_price_change(
        bars, event_date, event_dt, period
    )
    if verdict is None and price_change_pct is None:
        return None
    return {
        "verdict": verdict,
        "price_change_pct": price_change_pct,
        "price_end_date": price_end_date,
        "surprise_pct": surprise,
    }


def fetch_earnings_reactions_batch(items: list[tuple]) -> dict:
    """並行抓取已發生財報的 EPS 驚喜與 +3 個交易日股價變化。

    每筆為 ``(symbol, event_date, event_dt, period)``。回傳 key 為
    ``(symbol, event_date)``。
    """
    import concurrent.futures

    result: dict = {}
    if not items:
        return result

    def _one(item: tuple):
        symbol, event_date, event_dt, period = item[0], item[1], item[2], item[3]
        return (symbol, event_date), fetch_earnings_reaction(
            symbol, event_date, event_dt=event_dt, period=period
        )

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=earnings_calendar_workers(len(items))
    ) as ex:
        futs = {ex.submit(_one, item): item for item in items}
        for fut in concurrent.futures.as_completed(futs):
            item = futs[fut]
            key = (item[0], item[1])
            try:
                key, payload = fut.result()
                result[key] = payload
            except Exception:
                result[key] = None
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


def fetch_sector_members_data(symbols: list[str], chunk_size: int = 20) -> dict[str, dict]:
    """Batch-fetch real market data for sector-group members (類股板塊分析).

    Prices / returns / volume come from chunked yf.download() (6-month daily bars,
    same chunking approach as fetch_prices_batch). Market cap comes from per-symbol
    yfinance fast_info — acceptable here because sector groups are small (a few
    dozen names total across all groups), unlike the ~84-ticker ETF universe.

    Any field yfinance has no data for is left as None — never fabricated or
    defaulted. day_pct / week_pct / month_pct are real close-to-close % returns
    over 1 / ~5 / ~21 trading bars. The longer window also supplies real 30MA /
    60MA, current OHLC, upper/lower-wick classification and the signed up/down
    streak used by the 1–3 day conditional-probability model.

    Returns {symbol: {"price","prev_close","day_pct","week_pct","month_pct",
                      "volume","turnover","marketcap","open","high","low",
                      "ma30","ma60","streak","candle_pattern"}}.
    """
    if not symbols:
        return {}
    uniq = sorted(set(symbols))
    chunks = [uniq[i:i + chunk_size] for i in range(0, len(uniq), chunk_size)]
    empty = lambda: {
        "price": None, "prev_close": None, "day_pct": None, "week_pct": None,
        "month_pct": None, "volume": None, "turnover": None, "marketcap": None,
        "currency": None, "open": None, "high": None, "low": None,
        "ma30": None, "ma60": None, "streak": 0, "candle_pattern": "neutral",
    }
    result: dict[str, dict] = {s: empty() for s in uniq}

    for chunk in chunks:
        try:
            with silence_output():
                data = yf.download(
                    tickers=chunk,
                    period="6mo",
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
                            sub = data[sym]
                        else:
                            continue
                        closes = sub["Close"].dropna()
                        vols = sub["Volume"].dropna() if "Volume" in sub.columns else _pd.Series(dtype=float)
                    else:
                        sub = data
                        closes = data["Close"].dropna()
                        vols = data["Volume"].dropna() if "Volume" in data.columns else _pd.Series(dtype=float)
                    if isinstance(closes, _pd.DataFrame):
                        closes = closes[sym] if sym in closes.columns else closes.squeeze()
                    if closes.empty:
                        continue

                    r = result[sym]
                    price = _clean_float(closes.iloc[-1])
                    r["price"] = price
                    if len(closes) >= 30:
                        r["ma30"] = _clean_float(closes.iloc[-30:].mean())
                    if len(closes) >= 60:
                        r["ma60"] = _clean_float(closes.iloc[-60:].mean())
                    if len(closes) >= 2:
                        prev = _clean_float(closes.iloc[-2])
                        r["prev_close"] = prev
                        if price is not None and prev:
                            r["day_pct"] = round((price / prev - 1.0) * 100, 2)
                    if len(closes) >= 6:
                        wk = _clean_float(closes.iloc[-6])
                        if price is not None and wk:
                            r["week_pct"] = round((price / wk - 1.0) * 100, 2)
                    if len(closes) >= 22:
                        month = _clean_float(closes.iloc[-22])
                        if price is not None and month:
                            r["month_pct"] = round((price / month - 1.0) * 100, 2)
                    if not vols.empty:
                        vol = _clean_float(vols.iloc[-1])
                        r["volume"] = vol
                        if vol is not None and price is not None:
                            r["turnover"] = vol * price

                    # 當日 OHLC／影線與連漲跌只由真實日線計算；缺欄位便保留 None/neutral。
                    try:
                        opens = sub["Open"].dropna()
                        highs = sub["High"].dropna()
                        lows = sub["Low"].dropna()
                        if isinstance(opens, _pd.DataFrame):
                            opens = opens[sym] if sym in opens.columns else opens.squeeze()
                        if isinstance(highs, _pd.DataFrame):
                            highs = highs[sym] if sym in highs.columns else highs.squeeze()
                        if isinstance(lows, _pd.DataFrame):
                            lows = lows[sym] if sym in lows.columns else lows.squeeze()
                        r["open"] = _clean_float(opens.iloc[-1]) if not opens.empty else None
                        r["high"] = _clean_float(highs.iloc[-1]) if not highs.empty else None
                        r["low"] = _clean_float(lows.iloc[-1]) if not lows.empty else None
                        from .sector_predictive import candle_pattern, signed_streak
                        r["streak"] = signed_streak([float(v) for v in closes.tolist()])
                        r["candle_pattern"] = candle_pattern(
                            r["open"], r["high"], r["low"], price
                        )
                    except Exception:
                        pass
                except Exception:
                    continue
        except Exception:
            continue

    # Market cap per symbol via fast_info (bounded number of symbols). The same
    # fast_info call also serves as a price fallback: yfinance's batch download()
    # flakily drops tickers under load (returning close data for only a subset),
    # yet the per-symbol fast_info endpoint often still answers. So whenever the
    # batch download produced no price for a symbol, backfill price / prev_close /
    # day_pct from fast_info's real last_price / previous_close (never fabricated;
    # week/month % stay None because fast_info carries no history).
    for sym in uniq:
        try:
            with silence_output():
                fi = yf.Ticker(sym).fast_info
            mc = None
            for key in ("market_cap", "marketCap"):
                try:
                    mc = fi[key]
                except Exception:
                    mc = getattr(fi, "market_cap", None) if key == "market_cap" else mc
                if mc:
                    break
            result[sym]["marketcap"] = _clean_float(mc) if mc else None
            # bug#00085: marketcap is quoted in the listing's LOCAL currency, so the
            # currency must travel with it — otherwise a KRW-denominated cap (~1560x
            # the USD number) silently dominates every cap-weighted calculation.
            try:
                cur = getattr(fi, "currency", None)
            except Exception:
                cur = None
            result[sym]["currency"] = str(cur).upper() if cur else None

            r = result[sym]
            if r["price"] is None:
                px = _clean_float(
                    getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
                )
                if px is not None:
                    r["price"] = px
                    pc = _clean_float(
                        getattr(fi, "regular_market_previous_close", None)
                        or getattr(fi, "previous_close", None)
                    )
                    if pc:
                        r["prev_close"] = pc
                        r["day_pct"] = round((px / pc - 1.0) * 100, 2)
        except Exception:
            continue

    return result


def fetch_sector_prediction_bars(
    symbols: list[str],
    years: int = 5,
    chunk_size: int = 20,
) -> dict[str, list[dict]]:
    """下載個股多年日線供類股 1–3 日條件模型使用。

    回傳純 JSON 相容資料 ``{symbol: [{date, open, high, low, close}, ...]}``；
    無資料的代碼直接略過，不補值、不臆測。呼叫端以每日／板塊設定簽章快取，因此這個
    較長歷史請求不會跟著盤中 60 秒報價刷新重跑。
    """
    if not symbols:
        return {}
    uniq = sorted(set(symbols))
    result: dict[str, list[dict]] = {}
    for i in range(0, len(uniq), chunk_size):
        chunk = uniq[i:i + chunk_size]
        try:
            with silence_output():
                data = yf.download(
                    tickers=chunk,
                    period=f"{max(1, int(years))}y",
                    interval="1d",
                    auto_adjust=True,
                    actions=False,
                    progress=False,
                    group_by="ticker" if len(chunk) > 1 else "column",
                )
            for sym in chunk:
                try:
                    if len(chunk) > 1:
                        if sym not in data.columns.get_level_values(0):
                            continue
                        sub = data[sym]
                    else:
                        sub = data
                    bars = []
                    for ts, row in sub.iterrows():
                        close = _clean_float(row.get("Close"))
                        if close is None:
                            continue
                        date = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)[:10]
                        bars.append({
                            "date": date,
                            "open": _clean_float(row.get("Open")),
                            "high": _clean_float(row.get("High")),
                            "low": _clean_float(row.get("Low")),
                            "close": close,
                        })
                    if bars:
                        result[sym] = bars
                except Exception:
                    continue
        except Exception:
            continue
    return result


def fetch_fx_rates(currencies: list[str]) -> dict[str, float]:
    """Fetch real FX rates so foreign market caps can be normalised to USD
    (bug#00085). Returns {CURRENCY: usd_per_unit}, e.g. {"KRW": 0.00072}.

    yfinance quotes "<CUR>=X" as units-of-CUR per 1 USD, so usd_per_unit = 1/rate.
    USD is always 1.0. Any currency we cannot get a real rate for is simply absent
    from the result — the caller (sector_analysis.cap_weights) then falls back to
    equal weighting rather than fabricating a rate or summing mixed currencies.
    """
    out: dict[str, float] = {"USD": 1.0}
    wanted = sorted({str(c).upper() for c in currencies if c and str(c).upper() != "USD"})
    for cur in wanted:
        try:
            with silence_output():
                fi = yf.Ticker(f"{cur}=X").fast_info
                px = _clean_float(
                    getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
                )
            if px and px > 0:
                out[cur] = 1.0 / px
        except Exception:
            continue
    return out


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

    # bug#00061 follow-up: date-stamped in Taiwan time (matches
    # storage.taiwan_now(), which the ETF/options daily-snapshot system now
    # uses throughout) rather than UTC, so this "as of" date lines up with the
    # date the corresponding history-log line actually gets stored under.
    as_of_now = _dt_mod.datetime.now(_TZ_TW) if _TZ_TW is not None else _dt_mod.datetime.utcnow()

    return {
        "name": fund_name,
        "holdings": holdings,
        "asset_classes": asset_classes,
        "as_of_date": as_of_now.strftime("%Y-%m-%d"),
    }


def fetch_options_snapshot(
    underlying: str,
    min_dte: int = 1,
    max_dte: int = 60,
    strike_band_pct: float = 20.0,
) -> dict:
    """Fetch a real, current options-chain snapshot for `underlying` via yfinance.

    Scope (bug#00066, per user request「價內價外 60 日內」): expiries within 60 days
    (min_dte=1 excludes already-expired/0-DTE), strikes within +/- strike_band_pct
    of spot so both in-the-money and out-of-the-money contracts around spot are
    captured while keeping the fetch bounded (pulling the entire chain for every
    watched ticker every day is too slow / rate-limit-prone).

    yfinance's option_chain() is a live snapshot only — no history — so this
    fetch alone cannot tell you whether a position is being "built"; that
    requires comparing today's snapshot against a previous real one (see
    storage.append_options_daily_snapshot / options_analysis.py). This function
    just returns one honest, real, current data point.

    Returns {"spot_price": float|None, "contracts": [...]}. contracts is [] (no
    fallback/mock data) if yfinance has nothing usable for this underlying.
    Each contract: contractSymbol, type ("call"/"put"), strike, expiry,
    lastPrice, bid, ask, lastTradeDate, volume, openInterest,
    impliedVolatility — all real fields as reported by yfinance, nothing
    derived or estimated.
    """
    import datetime as _dt_mod

    contracts: list[dict] = []
    spot_price: Optional[float] = None
    session_date: Optional[str] = None

    try:
        with silence_output():
            ticker = yf.Ticker(underlying)

            try:
                fi = ticker.fast_info
                spot_price = _clean_float(getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None))
            except Exception:
                spot_price = None

            # The chain belongs to a US market session, not the Taiwan calendar
            # day on which the app happened to fetch it. Persist the last real
            # yfinance trading row as the session key so weekends/pre-open
            # refreshes cannot become extra backtest observations.
            try:
                hist = ticker.history(period="5d", auto_adjust=False)
            except Exception:
                hist = None
            if hist is not None and not hist.empty:
                try:
                    session_date = hist.index[-1].date().isoformat()
                except Exception:
                    session_date = None
                if spot_price is None:
                    spot_price = _clean_float(hist["Close"].iloc[-1])

            if not spot_price or spot_price <= 0:
                return {"spot_price": None, "contracts": [], "session_date": session_date}

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
                            # bug#00077: 保存雙邊報價與最後成交時間，讓預期波動/ATM IV
                            # 可改用 (bid+ask)/2 中間價、並判斷 lastPrice 是否過期。
                            "bid": _clean_float(row.get("bid")),
                            "ask": _clean_float(row.get("ask")),
                            "lastTradeDate": (str(row.get("lastTradeDate"))
                                              if row.get("lastTradeDate") is not None else None),
                            "volume": _clean_float(row.get("volume")),
                            "openInterest": _clean_float(row.get("openInterest")),
                            "impliedVolatility": _clean_float(row.get("impliedVolatility")),
                        })
    except Exception:
        pass

    return {
        "spot_price": spot_price,
        "contracts": contracts,
        "session_date": session_date,
    }
