from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from ..models import Position


def _guess_column(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    """Return the first matching column name (case-insensitive, partial match ok)."""
    cols_lower = {c.lower().strip(): c for c in df.columns}
    for cand in candidates:
        cl = cand.lower()
        if cl in cols_lower:
            return cols_lower[cl]
        # partial / fuzzy
        for lower, orig in cols_lower.items():
            if cl in lower or lower in cl:
                return orig
    return None


def _parse_option_symbol(symbol: str, description: str = "") -> dict:
    """Try to extract option details from OCC symbol or description."""
    result = {"underlying": None, "expiry": None, "strike": None, "option_type": None}

    s = (symbol or "").upper().strip()
    desc = (description or "").upper()

    # OCC style with potential internal spaces (e.g., AAPL  240621C00150000)
    s_nospaces = re.sub(r"\s+", "", s)
    m = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", s_nospaces)
    if m:
        result["underlying"] = m.group(1)
        ymd = m.group(2)
        result["expiry"] = f"20{ymd[0:2]}-{ymd[2:4]}-{ymd[4:6]}"
        result["option_type"] = "call" if m.group(3) == "C" else "put"
        strike_str = m.group(4)
        result["strike"] = float(strike_str) / 1000.0
        return result

    # IBKR Local Symbol Style: "AAPL 21JUN24 150.0 C"
    m_ib = re.match(r"^([A-Z]+)\s+(\d{1,2})([A-Z]{3})(\d{2})\s+([\d.]+)\s+([CP])$", s)
    if m_ib:
        months = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
            "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12
        }
        underlying = m_ib.group(1)
        day = int(m_ib.group(2))
        month_str = m_ib.group(3)
        year = int(m_ib.group(4)) + 2000
        strike = float(m_ib.group(5))
        opt_char = m_ib.group(6)

        if month_str in months:
            result["underlying"] = underlying
            result["expiry"] = f"{year:04d}-{months[month_str]:02d}-{day:02d}"
            result["option_type"] = "call" if opt_char == "C" else "put"
            result["strike"] = strike
            return result

    # Description style: "TSLA 06/21/24 CALL 250" or "AAPL 07/19/24 PUT 180"
    m2 = re.search(r"([A-Z]+)\s+(\d{1,2}/\d{1,2}/\d{2,4})\s+(CALL|PUT)\s+([\d.]+)", desc)
    if m2:
        result["underlying"] = m2.group(1)
        date_str = m2.group(2)
        # normalize date
        try:
            dt = datetime.strptime(date_str, "%m/%d/%y")
        except ValueError:
            try:
                dt = datetime.strptime(date_str, "%m/%d/%Y")
            except ValueError:
                dt = None
        if dt:
            result["expiry"] = dt.strftime("%Y-%m-%d")
        result["option_type"] = "call" if "CALL" in m2.group(3) else "put"
        result["strike"] = float(m2.group(4))
        return result

    return result


def parse_positions_csv(csv_path: str | Path, broker: str = "firstrade") -> list[Position]:
    """
    Parse a "Download Account Information" CSV (from Tax Center or IBKR exports).

    Returns a list of Position objects tagged with the selected broker, source="csv".
    """
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"CSV not found: {path}")

    # Read with pandas, be flexible
    df = pd.read_csv(path)
    df.columns = [str(c).strip() for c in df.columns]

    # Column guessing
    col_symbol = _guess_column(df, ["Symbol", "Ticker", "Contract", "Financial Instrument"])
    col_desc = _guess_column(df, ["Description", "Security Description", "Name"])
    col_qty = _guess_column(df, ["Quantity", "Qty", "Shares", "Position"])
    col_cost = _guess_column(df, ["Cost Basis", "Average Cost", "Cost", "Avg Cost", "Price Cost", "Avg Price"])
    col_mkt = _guess_column(df, ["Market Value", "Current Value", "Market", "Value"])
    col_currency = _guess_column(df, ["Currency", "Curr"])

    if not col_symbol or not col_qty:
        raise ValueError(
            f"Could not find required columns (Symbol + Quantity) in CSV. "
            f"Columns seen: {list(df.columns)}. "
        )

    positions: list[Position] = []

    for _, row in df.iterrows():
        try:
            symbol = str(row.get(col_symbol, "")).strip()
            if not symbol or symbol.upper() == "NAN" or pd.isna(row.get(col_symbol)):
                continue

            qty_raw = row.get(col_qty)
            if pd.isna(qty_raw):
                continue
            quantity = float(qty_raw)

            desc = str(row.get(col_desc, "")) if col_desc else ""

            avg_cost = None
            cost_basis_total = None
            if col_cost:
                c = row.get(col_cost)
                if pd.notna(c):
                    try:
                        val = float(str(c).replace(",", "").replace("$", ""))
                        col_name_lower = (col_cost or "").lower()
                        if "basis" in col_name_lower or "total" in col_name_lower:
                            cost_basis_total = val
                        else:
                            avg_cost = val
                    except (ValueError, TypeError):
                        pass

            if cost_basis_total is not None and quantity and quantity != 0:
                avg_cost = cost_basis_total / quantity

            market_value = None
            if col_mkt:
                m = row.get(col_mkt)
                if pd.notna(m):
                    market_value = float(str(m).replace(",", "").replace("$", ""))

            # Smart currency and instrument type detection
            currency = "USD"
            if col_currency:
                cur = row.get(col_currency)
                if pd.notna(cur):
                    currency = str(cur).strip().upper() or "USD"
            else:
                # If symbol looks like a Taiwan stock (XXXX.TW / XXXX.TWO / pure 4+ digits)
                s_upper = symbol.upper()
                if s_upper.endswith(".TW") or s_upper.endswith(".TWO") or (s_upper.isdigit() and len(s_upper) >= 4):
                    currency = "TWD"

            # Detect options
            opt = _parse_option_symbol(symbol, desc)
            if opt.get("option_type"):
                instrument_type = "option"
            else:
                s_upper = symbol.upper()
                if s_upper.endswith(".TW") or s_upper.endswith(".TWO") or (s_upper.isdigit() and len(s_upper) >= 4):
                    instrument_type = "stock"
                elif s_upper in {"SPY", "QQQ", "IWM", "DIA"}:
                    instrument_type = "etf"
                else:
                    instrument_type = "stock"

            pos = Position(
                broker=broker.lower(),
                account=None,
                symbol=symbol.upper(),
                instrument_type=instrument_type,
                quantity=quantity,
                avg_cost=avg_cost,
                market_value=market_value,
                currency=currency,
                underlying=opt.get("underlying"),
                expiry=opt.get("expiry"),
                strike=opt.get("strike"),
                option_type=opt.get("option_type"),
                last_updated=datetime.utcnow(),
                source="csv",
            )
            positions.append(pos)
        except Exception:
            continue

    return positions


def parse_firstrade_positions_csv(csv_path: str | Path) -> list[Position]:
    """Deprecated: Wrapper for backwards compatibility."""
    return parse_positions_csv(csv_path, broker="firstrade")

