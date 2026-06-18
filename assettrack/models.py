from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field, model_validator


InstrumentType = Literal["stock", "option", "etf", "other"]


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
            
        return self

    @property
    def value(self) -> float:
        """Best available market value for this position."""
        if self.market_value is not None:
            return self.market_value
        if self.market_price is not None and self.quantity is not None:
            mult = self.multiplier if (self.instrument_type == "option" and self.multiplier is not None) else 1.0
            return self.market_price * self.quantity * mult
        return 0.0

    @property
    def total_cost(self) -> Optional[float]:
        if self.avg_cost is not None and self.quantity is not None:
            mult = self.multiplier if (self.instrument_type == "option" and self.multiplier is not None) else 1.0
            return self.avg_cost * self.quantity * mult
        return None

    @property
    def unrealized_pnl(self) -> Optional[float]:
        cost = self.total_cost
        if cost is not None:
            return self.value - cost
        return None

    @property
    def unrealized_pnl_pct(self) -> Optional[float]:
        cost = self.total_cost
        if cost and cost != 0:
            pnl = self.unrealized_pnl
            if pnl is not None:
                return (pnl / cost) * 100
        return None

    @property
    def daily_change(self) -> Optional[float]:
        """Today's net value change for the whole position (current value - prev-close value)."""
        if self.prev_close is not None and self.market_price is not None:
            mult = self.multiplier if (self.instrument_type == "option" and self.multiplier is not None) else 1.0
            return (self.market_price - self.prev_close) * self.quantity * mult
        return None

    @property
    def daily_change_pct(self) -> Optional[float]:
        """Today's price change percentage vs previous close."""
        if self.prev_close is not None and self.prev_close != 0 and self.market_price is not None:
            return (self.market_price - self.prev_close) / self.prev_close * 100
        return None

    def to_dict(self) -> dict:
        return self.model_dump(mode="json")


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
    last_manual_update: Optional[datetime] = None
