from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator


InstrumentType = Literal["stock", "option", "etf", "other"]

# Supported cash currencies — at least USD, TWD, JPY as required
CashCurrency = Literal["USD", "TWD", "JPY"]


class CashPosition(BaseModel):
    """A cash holding stored at a broker/account in its native currency."""

    broker: str = Field(..., description="券商或銀行名稱")
    account: Optional[str] = Field(None, description="Account ID or nickname")
    currency: CashCurrency = Field("TWD", description="存款幣別 (USD / TWD / JPY)")
    amount: float = Field(..., gt=0, description="存款金額（正數）")
    notes: Optional[str] = Field(None, description="備註")
    last_updated: datetime = Field(default_factory=datetime.utcnow)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_bank(cls, data):
        """Read cash records written before broker/account joined the portfolio schema."""
        if isinstance(data, dict) and not data.get("broker") and data.get("bank"):
            data = dict(data)
            data["broker"] = data.pop("bank")
        return data

    @property
    def bank(self) -> str:
        """Backward-compatible name for callers that still label the institution a bank."""
        return self.broker

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")


def merge_cash_position(
    cash_positions: list[CashPosition],
    incoming: CashPosition,
) -> None:
    """Stack cash added to the same broker/account/currency holding."""
    incoming_key = (
        incoming.broker.casefold(),
        (incoming.account or "").casefold(),
        incoming.currency,
    )
    for existing in cash_positions:
        existing_key = (
            existing.broker.casefold(),
            (existing.account or "").casefold(),
            existing.currency,
        )
        if existing_key == incoming_key:
            existing.amount += incoming.amount
            existing.notes = incoming.notes or existing.notes
            existing.last_updated = incoming.last_updated
            return
    cash_positions.append(incoming)


def cash_value_usd(
    cash_positions: list[CashPosition],
    usdtwd_rate: float,
) -> Optional[float]:
    """Return USD-equivalent cash, or None when a currency cannot be valued."""
    if usdtwd_rate <= 0:
        return None
    total = 0.0
    for cash in cash_positions:
        if cash.currency == "USD":
            total += cash.amount
        elif cash.currency == "TWD":
            total += cash.amount / usdtwd_rate
        else:
            return None
    return total


def total_asset_value_usd(
    positions: list[Position],
    cash_positions: list[CashPosition],
    usdtwd_rate: float,
) -> Optional[float]:
    """Cash plus every positive long/short current market value in USD."""
    cash_total = cash_value_usd(cash_positions, usdtwd_rate)
    if cash_total is None:
        return None
    total = cash_total
    for position in positions:
        if position.market_price is None and position.market_value is None:
            return None
        value = position.value
        total += value if position.currency == "USD" else value / usdtwd_rate
    return total


def calculate_cash_ratio(
    positions: list[Position],
    cash_positions: list[CashPosition],
    usdtwd_rate: float,
) -> Optional[float]:
    """Portfolio cash percentage, or None when any market value is unavailable."""
    cash_total = cash_value_usd(cash_positions, usdtwd_rate)
    total = total_asset_value_usd(positions, cash_positions, usdtwd_rate)
    if cash_total is None or total is None or total <= 0:
        return None
    return cash_total / total * 100.0


class Position(BaseModel):
    """A single holding (stock, option, etc.)."""

    broker: str = Field(..., description="Source broker or 'manual'")
    account: Optional[str] = Field(None, description="Account ID or nickname")
    symbol: str = Field(..., description="Ticker or OCC option symbol (e.g. AAPL240621C00150000)")
    instrument_type: InstrumentType = "stock"
    quantity: float
    avg_cost: Optional[float] = None  # per share/contract
    market_price: Optional[float] = None
    market_value: Optional[float] = None
    prev_close: Optional[float] = None  # Previous trading day close price
    currency: str = "USD"
    # Option-specific (optional)
    underlying: Optional[str] = None
    expiry: Optional[str] = None  # YYYY-MM-DD
    strike: Optional[float] = None
    option_type: Optional[Literal["call", "put"]] = None
    multiplier: Optional[float] = None  # Contract multiplier (US options=100, Taiwan options=50, etc.)
    # Signed daily exposure multiple for ETFs.  Examples: 2.0 for a long 2x
    # fund, -1.0 for an inverse 1x fund, -3.0 for an inverse 3x fund.  None
    # means the quote refresh should infer it from the fund name/description;
    # exposure calculations conservatively treat an unresolved ETF as 1x.
    leverage_factor: Optional[float] = Field(None, ge=-10.0, le=10.0)
    # Extended metadata
    market: Optional[str] = None      # Market identifier: US / TW / HK / etc.
    exchange: Optional[str] = None    # Exchange: NYSE / NASDAQ / TSE / OTC / etc.
    sector: Optional[str] = None      # User-defined sector tag (e.g. 科技/半導體)
    cost_currency: Optional[str] = None  # Currency of avg_cost input (if differs from currency)
    notes: Optional[str] = None       # Free-form notes / memo
    last_updated: datetime = Field(default_factory=datetime.utcnow)
    source: str = "manual"  # "api", "csv", "manual"

    @model_validator(mode='after')
    def auto_populate_option_fields(self) -> Position:
        import re
        from datetime import datetime
        
        m = re.match(r"^([A-Z\s]{1,6})(\d{6})([CP])(\d{8})$", self.symbol.upper())
        if m:
            self.instrument_type = "option"
            if not self.underlying:
                self.underlying = m.group(1).strip()
            if not self.expiry:
                try:
                    expiry_dt = datetime.strptime(m.group(2), "%y%m%d")
                    self.expiry = expiry_dt.strftime("%Y-%m-%d")
                except ValueError:
                    pass
            if not self.strike:
                try:
                    self.strike = float(m.group(4)) / 1000.0
                except ValueError:
                    pass
            if not self.option_type:
                self.option_type = "call" if m.group(3) == "C" else "put"
                
        if self.instrument_type == "option" and not self.multiplier:
            # Default multiplier: 50.0 for TWD/Taiwan markets, otherwise 100.0
            is_tw = self.currency == "TWD" or self.symbol.endswith(".TW") or self.symbol.endswith(".TWO") or (self.market == "TW")
            self.multiplier = 50.0 if is_tw else 100.0

        if self.instrument_type == "etf" and self.leverage_factor == 0:
            raise ValueError("ETF leverage_factor cannot be zero")

        # bug#00050: an option's `symbol` must be the OCC/TW contract code, never the
        # bare underlying ticker — quote lookups (quotes.py) use `symbol` as-is for
        # options, so a bad symbol (e.g. "INTC" instead of "INTC260918C00150000")
        # silently fetches the underlying STOCK price instead of the option premium.
        # Whenever we have full option details, always (re)derive the canonical symbol
        # rather than trusting a possibly-wrong stored value. This previously excluded
        # TW options because cli.py and tui.py disagreed on the TW format (bug#00051);
        # now that cli.py has been removed entirely (bug#00056), tui.py's convention
        # (no strike suffix) is the sole remaining format, so TW is included too.
        is_tw_opt = self.currency == "TWD" or self.symbol.endswith(".TW") or self.symbol.endswith(".TWO") or (self.market == "TW")
        if (
            self.instrument_type == "option"
            and self.underlying and self.expiry and self.strike and self.option_type
        ):
            try:
                expiry_dt = datetime.strptime(self.expiry, "%Y-%m-%d")
                yy, mm, dd = expiry_dt.strftime("%y"), expiry_dt.strftime("%m"), expiry_dt.strftime("%d")
                cp = "C" if self.option_type == "call" else "P"
                if is_tw_opt:
                    canonical_symbol = f"{self.underlying}{yy}{mm}{dd}{cp}"
                else:
                    canonical_symbol = f"{self.underlying}{yy}{mm}{dd}{cp}{int(round(self.strike * 1000)):08d}"
                if self.symbol.upper() != canonical_symbol:
                    self.symbol = canonical_symbol
                    # The old symbol's cached quote (if any) belongs to the wrong
                    # instrument — clear it so the next refresh re-fetches correctly.
                    self.market_price = None
                    self.market_value = None
                    self.prev_close = None
            except (ValueError, TypeError):
                pass

        return self

    @property
    def value(self) -> float:
        """Best available market value for this position."""
        if self.market_value is not None:
            return abs(self.market_value)
        if self.market_price is not None and self.quantity is not None:
            mult = self.multiplier if (self.instrument_type == "option" and self.multiplier is not None) else 1.0
            return self.market_price * abs(self.quantity) * mult
        return 0.0

    @property
    def total_cost(self) -> Optional[float]:
        if self.avg_cost is not None and self.quantity is not None:
            mult = self.multiplier if (self.instrument_type == "option" and self.multiplier is not None) else 1.0
            return self.avg_cost * abs(self.quantity) * mult
        return None

    @property
    def unrealized_pnl(self) -> Optional[float]:
        if self.market_value is None and self.market_price is None:
            return None
        cost = self.total_cost
        if cost is not None:
            if self.quantity < 0:
                return cost - self.value
            return self.value - cost
        return None

    @property
    def unrealized_pnl_pct(self) -> Optional[float]:
        if self.market_value is None and self.market_price is None:
            return None
        cost = self.total_cost
        if cost is not None and cost != 0:
            pnl = self.unrealized_pnl
            if pnl is not None:
                return (pnl / abs(cost)) * 100
        return None

    @property
    def daily_change(self) -> Optional[float]:
        """Today's net value change for the whole position (current value - prev-close value)."""
        if self.prev_close is not None and self.market_price is not None:
            import math
            if math.isnan(self.prev_close) or math.isnan(self.market_price):
                return None
            mult = self.multiplier if (self.instrument_type == "option" and self.multiplier is not None) else 1.0
            direction = -1.0 if self.quantity < 0 else 1.0
            return (self.market_price - self.prev_close) * abs(self.quantity) * mult * direction
        return None

    @property
    def daily_change_pct(self) -> Optional[float]:
        """Today's price change percentage vs previous close."""
        if self.prev_close is not None and self.prev_close != 0 and self.market_price is not None:
            import math
            if math.isnan(self.prev_close) or math.isnan(self.market_price):
                return None
            direction = -1.0 if self.quantity < 0 else 1.0
            return (self.market_price - self.prev_close) / self.prev_close * 100 * direction
        return None

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")


def portfolio_unrealized_performance(
    positions: list[Position],
    cash_positions: list[CashPosition],
    usdtwd_rate: float,
) -> Optional[tuple[float, float]]:
    """Return USD unrealized P&L and return %, with cash as zero-return capital."""
    cash_cost = cash_value_usd(cash_positions, usdtwd_rate)
    if cash_cost is None:
        return None
    pnl_usd = 0.0
    cost_usd = cash_cost
    for position in positions:
        pnl = position.unrealized_pnl
        cost = position.total_cost
        if pnl is None or cost is None:
            return None
        if position.currency != "USD":
            pnl /= usdtwd_rate
            cost /= usdtwd_rate
        pnl_usd += pnl
        cost_usd += cost
    if cost_usd <= 0:
        return None
    return pnl_usd, pnl_usd / cost_usd * 100.0


@dataclass
class PortfolioSnapshot:
    """Point-in-time total value + breakdown."""

    timestamp: datetime
    total_value: float
    cash: float = 0.0
    by_broker: dict[str, float] = field(default_factory=dict)
    positions: list[Position] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "total_value": self.total_value,
            "cash": self.cash,
            "by_broker": self.by_broker,
            "positions": [p.to_dict() for p in self.positions],
            "notes": self.notes,
        }


class ManualPositionsFile(BaseModel):
    """Schema for positions.json (manual input)."""

    positions: list[Position]
    cash_positions: list[CashPosition] = Field(default_factory=list)
    last_manual_update: Optional[datetime] = None
