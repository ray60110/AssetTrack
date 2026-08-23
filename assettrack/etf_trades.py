"""assettrack.etf_trades

Automated fetching and derivation of ETF trade/transaction history.
Resolves bug#00101 by:
1. Parsing official daily trade files for supported funds (e.g., ARK Invest).
2. Deriving day-over-day buy/sell trade records by diffing consecutive real daily
   holdings snapshots logged in `etf_cache/history/*.jsonl`.
3. Merging and deduplicating trades into `cached["history"]` for TUI rendering.
"""

from typing import Optional
import logging
from .storage import (
    load_etf_symbol_cache,
    save_etf_symbol_cache,
    load_etf_daily_snapshots,
)

logger = logging.getLogger(__name__)

# ARK Invest tickers supported for official daily trade retrieval
ARK_TICKERS = {"ARKK", "ARKQ", "ARKW", "ARKG", "ARKF", "ARKX", "PRNT", "IZRL"}


def _reported_shares(holding: dict | None, snapshot: dict) -> int | None:
    if not holding:
        return None
    explicit = holding.get("shares")
    if explicit is not None:
        try:
            return int(float(explicit))
        except (TypeError, ValueError):
            return None
    weight = holding.get("weight") or 0.0
    price = holding.get("price")
    aum = snapshot.get("aum")
    if aum and weight and price:
        return int((aum * (weight / 100.0)) / price)
    return None


def _position_metadata(holding: dict | None) -> dict:
    holding = holding or {}
    return {
        key: holding.get(key)
        for key in (
            "name", "issuer", "cusip", "figi", "instrument_type",
            "option_type", "expiration", "strike",
        )
        if holding.get(key) is not None
    }


def derive_trade_history_from_snapshots(symbol: str) -> list[dict]:
    """Derive day-over-day buy/sell trade records for `symbol` by diffing
    consecutive daily holdings snapshots stored under `etf_cache/history/{symbol}.jsonl`.

    Returns a list of trade dicts sorted descending by date:
    [
        {
            "date": "YYYY-MM-DD",
            "action": "BUY" | "SELL",
            "symbol": "NVDA",
            "shares": 15000,
            "price": 125.50,
            "weight_change": +0.35,
        },
        ...
    ]
    """
    snapshots = load_etf_daily_snapshots(symbol)
    if len(snapshots) < 2:
        return []

    trades: list[dict] = []

    for i in range(1, len(snapshots)):
        prev_snap = snapshots[i - 1]
        curr_snap = snapshots[i]
        curr_date = curr_snap.get("date", "")
        if not curr_date:
            continue

        prev_holdings = {
            h["symbol"]: h for h in prev_snap.get("holdings", []) if h.get("symbol")
        }
        curr_holdings = {
            h["symbol"]: h for h in curr_snap.get("holdings", []) if h.get("symbol")
        }

        all_syms = set(prev_holdings.keys()) | set(curr_holdings.keys())

        for s in sorted(all_syms):
            prev_h = prev_holdings.get(s)
            curr_h = curr_holdings.get(s)

            if prev_h and curr_h:
                w0 = prev_h.get("weight", 0.0) or 0.0
                w1 = curr_h.get("weight", 0.0) or 0.0
                dw = round(w1 - w0, 4)

                p0 = prev_h.get("price")
                p1 = curr_h.get("price")
                s0 = _reported_shares(prev_h, prev_snap)
                s1 = _reported_shares(curr_h, curr_snap)

                ds = (s1 - s0) if (s0 is not None and s1 is not None) else None

                # Significant change thresholds: weight change >= 0.10pp or share change != 0
                if abs(dw) >= 0.10 or (ds is not None and ds != 0):
                    action = "BUY" if (ds > 0 if ds is not None else dw > 0) else "SELL"
                    trade_shares = abs(ds) if ds is not None else None
                    trade = {
                        "date": curr_date,
                        "period_start": prev_snap.get("date"),
                        "period_end": curr_date,
                        "action": action,
                        "symbol": s,
                        "shares": trade_shares,
                        "price": p1 or p0,
                        "weight_change": round(dw, 2),
                        "value_change": (
                            float(curr_h["value"]) - float(prev_h["value"])
                            if curr_h.get("value") is not None and prev_h.get("value") is not None
                            else None
                        ),
                    }
                    trade.update(_position_metadata(curr_h or prev_h))
                    trades.append(trade)

            elif curr_h and not prev_h:
                # Newly added holding position
                w1 = curr_h.get("weight", 0.0) or 0.0
                p1 = curr_h.get("price")
                s1 = _reported_shares(curr_h, curr_snap)

                trade = {
                    "date": curr_date,
                    "period_start": prev_snap.get("date"),
                    "period_end": curr_date,
                    "action": "BUY",
                    "symbol": s,
                    "shares": s1,
                    "price": p1,
                    "weight_change": round(w1, 2),
                    "value_change": curr_h.get("value"),
                }
                trade.update(_position_metadata(curr_h))
                trades.append(trade)

            elif prev_h and not curr_h:
                # Closed/exited position
                w0 = prev_h.get("weight", 0.0) or 0.0
                p0 = prev_h.get("price")
                s0 = _reported_shares(prev_h, prev_snap)

                trade = {
                    "date": curr_date,
                    "period_start": prev_snap.get("date"),
                    "period_end": curr_date,
                    "action": "SELL",
                    "symbol": s,
                    "shares": s0,
                    "price": p0,
                    "weight_change": round(-w0, 2),
                    "value_change": (
                        -float(prev_h["value"]) if prev_h.get("value") is not None else None
                    ),
                }
                trade.update(_position_metadata(prev_h))
                trades.append(trade)

    # Sort descending by date, then symbol
    trades.sort(key=lambda t: (t.get("date", ""), t.get("symbol", "")), reverse=True)
    return trades


def fetch_ark_daily_trades(symbol: str) -> list[dict]:
    """Fetch official daily trade records for ARK ETFs if available.
    Returns empty list if network error or non-ARK ticker.
    """
    if symbol.upper() not in ARK_TICKERS:
        return []

    import urllib.request
    import json

    url = f"https://raw.githubusercontent.com/cathiesark/ark-funds-data/main/data/{symbol.upper()}_trades.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AssetTrack/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                raw_data = json.loads(resp.read().decode("utf-8"))
                output = []
                for item in raw_data:
                    date_str = item.get("date") or item.get("Date")
                    action = (item.get("action") or item.get("direction") or "").upper()
                    sym = item.get("ticker") or item.get("symbol")
                    shares = item.get("shares")
                    price = item.get("price")
                    weight_change = item.get("weight_change") or item.get("weight_delta")

                    if date_str and action in ("BUY", "SELL") and sym:
                        output.append({
                            "date": str(date_str),
                            "action": action,
                            "symbol": str(sym),
                            "shares": int(shares) if shares is not None else None,
                            "price": float(price) if price is not None else None,
                            "weight_change": float(weight_change) if weight_change is not None else None,
                        })
                return output
    except Exception as e:
        logger.debug("Failed to fetch ARK trade records for %s: %s", symbol, e)

    return []


def update_etf_trade_history(symbol: str) -> list[dict]:
    """Derive and persist trade history for `symbol` into its per-ETF cache.

    Merges derived daily snapshot trades with any official fund trade records,
    deduplicates by (date, symbol, action), sorts descending by date, and writes
    the result back to `cached["history"]` in `data/etf_cache/{symbol}.json`.

    Returns the updated trade history list.
    """
    cached = load_etf_symbol_cache(symbol)

    derived_trades = derive_trade_history_from_snapshots(symbol)
    official_trades = fetch_ark_daily_trades(symbol)

    # Merge & deduplicate
    seen_keys = set()
    merged: list[dict] = []

    for t in official_trades + derived_trades:
        key = (t.get("date"), t.get("symbol"), t.get("action"))
        if key in seen_keys or not t.get("date") or not t.get("symbol"):
            continue
        seen_keys.add(key)
        merged.append(t)

    merged.sort(key=lambda x: (x.get("date", ""), x.get("symbol", "")), reverse=True)

    cached["history"] = merged
    save_etf_symbol_cache(symbol, cached)

    return merged
