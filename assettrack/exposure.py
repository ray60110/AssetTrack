"""Portfolio market exposure calculations for the Summary Dashboard.

Exposure is deliberately not the same as market value:

* stocks and ordinary ETFs contribute signed 1x market exposure;
* leveraged/inverse ETFs multiply that exposure by ``leverage_factor``;
* options contribute delta-equivalent underlying exposure, not premium value.

The denominator remains AssetTrack's tracked total asset value (securities plus
cash), so the resulting percentage answers: "how many dollars of market risk do
I currently carry per dollar of tracked assets?"
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Mapping, Optional
from zoneinfo import ZoneInfo

from .greeks import bs_greeks, implied_vol
from .models import CashPosition, Position, total_asset_value_usd


@dataclass(frozen=True)
class PortfolioExposure:
    """Delta-equivalent portfolio exposure, denominated in USD."""

    asset_value_usd: Optional[float]
    gross_exposure_usd: float
    net_exposure_usd: float
    standard_exposure_usd: float
    leveraged_etf_exposure_usd: float
    option_exposure_usd: float
    unpriced: tuple[str, ...]

    @property
    def complete(self) -> bool:
        return self.asset_value_usd is not None and not self.unpriced

    @property
    def gross_ratio_pct(self) -> Optional[float]:
        if not self.complete or not self.asset_value_usd or self.asset_value_usd <= 0:
            return None
        return self.gross_exposure_usd / self.asset_value_usd * 100.0

    @property
    def net_ratio_pct(self) -> Optional[float]:
        if not self.complete or not self.asset_value_usd or self.asset_value_usd <= 0:
            return None
        return self.net_exposure_usd / self.asset_value_usd * 100.0


def _position_value(position: Position) -> Optional[float]:
    """Return signed local-currency market value without fabricating a quote."""
    if position.market_price is not None:
        return position.market_price * position.quantity
    if position.market_value is not None:
        direction = -1.0 if position.quantity < 0 else 1.0
        return abs(position.market_value) * direction
    return None


def _usd(value: float, currency: str, usdtwd_rate: float) -> Optional[float]:
    if currency.upper() == "USD":
        return value
    if currency.upper() == "TWD" and usdtwd_rate > 0:
        return value / usdtwd_rate
    return None


def calculate_portfolio_exposure(
    positions: list[Position],
    cash_positions: list[CashPosition],
    usdtwd_rate: float,
    underlying_prices: Optional[Mapping[str, float]] = None,
    risk_free_rate: float = 0.04,
    as_of: Optional[date] = None,
) -> PortfolioExposure:
    """Calculate gross and net delta-equivalent market exposure.

    ``gross_exposure_usd`` sums absolute position exposures.  ``net_exposure_usd``
    preserves long/short, call/put, and inverse-ETF direction.  If an option
    lacks a real premium, underlying quote, or enough contract fields to derive
    Delta, it is listed in ``unpriced`` and ratios are withheld rather than
    presenting a partial portfolio as complete.
    """
    underlying_prices = {
        str(symbol).upper(): price for symbol, price in (underlying_prices or {}).items()
    }
    calculation_date = as_of or datetime.now(ZoneInfo("Asia/Taipei")).date()
    asset_value = total_asset_value_usd(positions, cash_positions, usdtwd_rate)
    gross = net = standard = leveraged_etf = option_total = 0.0
    unpriced: list[str] = []

    for position in positions:
        exposure_local: Optional[float] = None
        bucket = "standard"

        if position.instrument_type in ("stock", "etf"):
            base_value = _position_value(position)
            if base_value is None:
                unpriced.append(position.symbol)
                continue
            factor = 1.0
            if position.instrument_type == "etf":
                factor = position.leverage_factor if position.leverage_factor is not None else 1.0
            exposure_local = base_value * factor
            if position.instrument_type == "etf" and abs(factor) > 1.0:
                bucket = "leveraged_etf"

        elif position.instrument_type == "option":
            bucket = "option"
            spot = underlying_prices.get((position.underlying or "").upper())
            premium = position.market_price
            try:
                expiry = date.fromisoformat(position.expiry or "")
                dte = (expiry - calculation_date).days
            except ValueError:
                dte = 0
            if (
                spot is None
                or spot <= 0
                or premium is None
                or premium <= 0
                or not position.strike
                or not position.option_type
                or dte <= 0
            ):
                unpriced.append(position.symbol)
                continue
            iv = implied_vol(
                spot,
                position.strike,
                dte,
                premium,
                position.option_type,
                risk_free_rate,
            )
            greeks = (
                bs_greeks(
                    spot,
                    position.strike,
                    dte,
                    iv,
                    position.option_type,
                    r=risk_free_rate,
                )
                if iv is not None
                else None
            )
            delta = greeks.get("delta") if greeks else None
            if delta is None:
                unpriced.append(position.symbol)
                continue
            exposure_local = (
                delta * position.quantity * (position.multiplier or 100.0) * spot
            )

        else:
            base_value = _position_value(position)
            if base_value is None:
                unpriced.append(position.symbol)
                continue
            exposure_local = base_value

        exposure_usd = _usd(exposure_local, position.currency, usdtwd_rate)
        if exposure_usd is None:
            unpriced.append(position.symbol)
            continue

        gross += abs(exposure_usd)
        net += exposure_usd
        if bucket == "leveraged_etf":
            leveraged_etf += abs(exposure_usd)
        elif bucket == "option":
            option_total += abs(exposure_usd)
        else:
            standard += abs(exposure_usd)

    return PortfolioExposure(
        asset_value_usd=asset_value,
        gross_exposure_usd=gross,
        net_exposure_usd=net,
        standard_exposure_usd=standard,
        leveraged_etf_exposure_usd=leveraged_etf,
        option_exposure_usd=option_total,
        unpriced=tuple(unpriced),
    )
