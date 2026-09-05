from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Literal, Protocol, Sequence
from uuid import uuid4

import yfinance as yf

from .auth import AuthError, read_protected_text, write_protected_text
from .models import CashPosition, Position, merge_cash_position


DEFAULT_BENCHMARKS = ("QQQ", "VT")


@dataclass(frozen=True)
class TrackingState:
    enabled: bool
    enabled_at: datetime | None
    has_tracking_gap: bool
    benchmarks: tuple[str, ...] = DEFAULT_BENCHMARKS


@dataclass(frozen=True)
class BenchmarkClose:
    market_date: date
    close: float


class BenchmarkPriceProvider(Protocol):
    def closing_prices(
        self,
        symbols: Sequence[str],
        as_of: datetime,
    ) -> dict[str, BenchmarkClose]: ...


class YFinanceBenchmarkPrices:
    """Read the latest real market close on or before the requested date."""

    def closing_prices(
        self,
        symbols: Sequence[str],
        as_of: datetime,
    ) -> dict[str, BenchmarkClose]:
        end_date = as_of.date() + timedelta(days=1)
        start_date = as_of.date() - timedelta(days=14)
        closes: dict[str, BenchmarkClose] = {}
        for symbol in symbols:
            history = yf.Ticker(symbol).history(
                start=start_date.isoformat(),
                end=end_date.isoformat(),
                auto_adjust=False,
            )
            if history is None or history.empty or "Close" not in history:
                continue
            series = history["Close"].dropna()
            if series.empty:
                continue
            value = float(series.iloc[-1])
            if value <= 0:
                continue
            market_timestamp = series.index[-1]
            market_date = (
                market_timestamp.date()
                if hasattr(market_timestamp, "date")
                else date.fromisoformat(str(market_timestamp)[:10])
            )
            closes[symbol] = BenchmarkClose(market_date, value)
        return closes


@dataclass(frozen=True)
class CashFlow:
    id: str
    occurred_at: datetime
    direction: Literal["deposit", "withdrawal"]
    amount: float
    currency: str
    amount_usd: float
    fx_rate_to_usd: float
    category: str
    channel: str
    broker: str
    account: str | None
    notes: str | None
    benchmark_prices: dict[str, float]
    benchmark_market_dates: dict[str, date]


@dataclass(frozen=True)
class ValuationSnapshot:
    recorded_at: datetime
    total_value_usd: float
    benchmark_prices: dict[str, float]
    benchmark_market_dates: dict[str, date]


@dataclass(frozen=True)
class BenchmarkComparison:
    symbol: str
    market_date: date
    benchmark_value_usd: float
    benchmark_return_pct: float
    value_gap_usd: float
    performance_gap_pct: float
    excess_return_percentage_points: float


@dataclass(frozen=True)
class PerformanceReport:
    baseline_at: datetime
    current_at: datetime
    portfolio_value_usd: float
    portfolio_return_pct: float
    comparisons: tuple[BenchmarkComparison, ...]


class PortfolioPerformanceTracker:
    """Own the persisted, cash-flow-adjusted performance history for one user."""

    def __init__(
        self,
        user: str,
        data_dir: Path,
        benchmark_prices: BenchmarkPriceProvider | None = None,
    ) -> None:
        self.user = user
        self.benchmark_prices = benchmark_prices
        safe_user = (user or "default").replace("/", "_")
        self.path = data_dir / f"{safe_user}_total_asset_tracking.json"

    def _empty_document(self) -> dict:
        return {
            "version": 1,
            "userporfolioperf_trackingsys_toggle": False,
            "userportfolioperf_tracksys": {
                "enabled_at": None,
                "disabled_at": None,
                "has_tracking_gap": False,
                "benchmarks": list(DEFAULT_BENCHMARKS),
                "valuations": [],
            },
            "usertotalAsset_tracking": [],
        }

    def _read(self) -> dict:
        if not self.path.exists():
            return self._empty_document()
        try:
            document = json.loads(read_protected_text(self.path))
        except (OSError, json.JSONDecodeError):
            return self._empty_document()
        if not isinstance(document, dict):
            return self._empty_document()
        if "tracking" in document:
            legacy_tracking = document.get("tracking") or {}
            document = {
                "version": document.get("version", 1),
                "userporfolioperf_trackingsys_toggle": bool(
                    legacy_tracking.get("enabled", False)
                ),
                "userportfolioperf_tracksys": {
                    "enabled_at": legacy_tracking.get("enabled_at"),
                    "disabled_at": legacy_tracking.get("disabled_at"),
                    "has_tracking_gap": bool(
                        legacy_tracking.get("has_tracking_gap", False)
                    ),
                    "benchmarks": legacy_tracking.get("benchmarks")
                    or list(DEFAULT_BENCHMARKS),
                    "valuations": document.get("valuations", []),
                },
                "usertotalAsset_tracking": document.get("cash_flows", []),
            }
        return document

    def _write(self, document: dict) -> None:
        if self.path.exists():
            try:
                parsed = json.loads(read_protected_text(self.path))
            except AuthError:
                raise
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(
                    "無法解析績效追蹤檔，已拒絕覆寫以免遺失紀錄"
                ) from exc
            if not isinstance(parsed, dict):
                raise ValueError("無法解析績效追蹤檔，已拒絕覆寫以免遺失紀錄")
        write_protected_text(
            self.path,
            json.dumps(document, ensure_ascii=False, indent=2),
        )

    def state(self) -> TrackingState:
        document = self._read()
        tracking = document.get("userportfolioperf_tracksys", {})
        enabled_at = tracking.get("enabled_at")
        return TrackingState(
            enabled=bool(
                document.get("userporfolioperf_trackingsys_toggle", False)
            ),
            enabled_at=(
                datetime.fromisoformat(enabled_at) if enabled_at else None
            ),
            has_tracking_gap=bool(tracking.get("has_tracking_gap", False)),
            benchmarks=tuple(tracking.get("benchmarks") or DEFAULT_BENCHMARKS),
        )

    def enable(
        self,
        *,
        enabled_at: datetime | None = None,
        new_account: bool = False,
    ) -> TrackingState:
        at = enabled_at or datetime.now(timezone.utc)
        document = self._read()
        previous = document.get("userportfolioperf_tracksys", {})
        has_history = bool(previous.get("valuations")) or bool(
            previous.get("enabled_at")
        )
        state = TrackingState(
            enabled=True,
            enabled_at=at,
            has_tracking_gap=bool(previous.get("has_tracking_gap")) or (
                not new_account or has_history
            ),
            benchmarks=tuple(previous.get("benchmarks") or DEFAULT_BENCHMARKS),
        )
        document["userporfolioperf_trackingsys_toggle"] = True
        document["userportfolioperf_tracksys"] = {
            "enabled_at": at.isoformat(),
            "disabled_at": None,
            "has_tracking_gap": state.has_tracking_gap,
            "benchmarks": list(state.benchmarks),
            "valuations": previous.get("valuations", []),
        }
        self._write(document)
        return state

    def disable(
        self,
        *,
        disabled_at: datetime | None = None,
    ) -> TrackingState:
        document = self._read()
        tracking = document.get("userportfolioperf_tracksys", {})
        document["userporfolioperf_trackingsys_toggle"] = False
        tracking["disabled_at"] = (
            disabled_at or datetime.now(timezone.utc)
        ).isoformat()
        document["userportfolioperf_tracksys"] = tracking
        self._write(document)
        return self.state()

    def declare_cash_flow(
        self,
        *,
        direction: Literal["deposit", "withdrawal"],
        amount: float,
        currency: str,
        amount_usd: float,
        fx_rate_to_usd: float,
        category: str,
        channel: str,
        broker: str,
        account: str | None = None,
        notes: str | None = None,
        occurred_at: datetime | None = None,
    ) -> CashFlow:
        if not self.state().enabled:
            raise ValueError("Performance Tracking is not enabled")
        if direction not in ("deposit", "withdrawal"):
            raise ValueError("direction must be deposit or withdrawal")
        if amount <= 0 or amount_usd <= 0 or fx_rate_to_usd <= 0:
            raise ValueError("cash-flow amounts and exchange rate must be positive")
        if self.benchmark_prices is None:
            raise ValueError("benchmark price provider is required")

        at = occurred_at or datetime.now(timezone.utc)
        closes = self.benchmark_prices.closing_prices(
            self.state().benchmarks,
            at,
        )
        missing = set(self.state().benchmarks) - set(closes)
        if missing:
            raise ValueError(
                f"missing benchmark closing prices: {', '.join(sorted(missing))}"
            )
        flow = CashFlow(
            id=str(uuid4()),
            occurred_at=at,
            direction=direction,
            amount=float(amount),
            currency=currency.upper(),
            amount_usd=float(amount_usd),
            fx_rate_to_usd=float(fx_rate_to_usd),
            category=category,
            channel=channel,
            broker=broker,
            account=account,
            notes=notes,
            benchmark_prices={
                symbol: float(closes[symbol].close)
                for symbol in self.state().benchmarks
            },
            benchmark_market_dates={
                symbol: closes[symbol].market_date
                for symbol in self.state().benchmarks
            },
        )
        document = self._read()
        document.setdefault("usertotalAsset_tracking", []).append(
            {
                "id": flow.id,
                "occurred_at": flow.occurred_at.isoformat(),
                "direction": flow.direction,
                "amount": flow.amount,
                "currency": flow.currency,
                "amount_usd": flow.amount_usd,
                "fx_rate_to_usd": flow.fx_rate_to_usd,
                "category": flow.category,
                "channel": flow.channel,
                "broker": flow.broker,
                "account": flow.account,
                "notes": flow.notes,
                "benchmark_prices": flow.benchmark_prices,
                "benchmark_market_dates": {
                    symbol: market_date.isoformat()
                    for symbol, market_date in flow.benchmark_market_dates.items()
                },
            }
        )
        self._write(document)
        return flow

    def cash_flows(self) -> list[CashFlow]:
        flows = []
        for item in self._read().get("usertotalAsset_tracking", []):
            try:
                flows.append(
                    CashFlow(
                        id=item["id"],
                        occurred_at=datetime.fromisoformat(item["occurred_at"]),
                        direction=item["direction"],
                        amount=float(item["amount"]),
                        currency=item["currency"],
                        amount_usd=float(item["amount_usd"]),
                        fx_rate_to_usd=float(item["fx_rate_to_usd"]),
                        category=item["category"],
                        channel=item["channel"],
                        broker=item["broker"],
                        account=item.get("account"),
                        notes=item.get("notes"),
                        benchmark_prices={
                            symbol: float(value)
                            for symbol, value in item["benchmark_prices"].items()
                        },
                        benchmark_market_dates={
                            symbol: date.fromisoformat(value)
                            for symbol, value in item[
                                "benchmark_market_dates"
                            ].items()
                        },
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return flows

    def record_valuation(
        self,
        *,
        total_value_usd: float,
        recorded_at: datetime | None = None,
    ) -> ValuationSnapshot:
        if not self.state().enabled:
            raise ValueError("Performance Tracking is not enabled")
        if total_value_usd <= 0:
            raise ValueError("portfolio valuation must be positive")
        if self.benchmark_prices is None:
            raise ValueError("benchmark price provider is required")

        at = recorded_at or datetime.now(timezone.utc)
        closes = self.benchmark_prices.closing_prices(
            self.state().benchmarks,
            at,
        )
        missing = set(self.state().benchmarks) - set(closes)
        if missing:
            raise ValueError(
                f"missing benchmark closing prices: {', '.join(sorted(missing))}"
            )
        snapshot = ValuationSnapshot(
            recorded_at=at,
            total_value_usd=float(total_value_usd),
            benchmark_prices={
                symbol: float(closes[symbol].close)
                for symbol in self.state().benchmarks
            },
            benchmark_market_dates={
                symbol: closes[symbol].market_date
                for symbol in self.state().benchmarks
            },
        )
        document = self._read()
        tracking = document.setdefault("userportfolioperf_tracksys", {})
        tracking.setdefault("valuations", []).append(
            {
                "recorded_at": snapshot.recorded_at.isoformat(),
                "total_value_usd": snapshot.total_value_usd,
                "benchmark_prices": snapshot.benchmark_prices,
                "benchmark_market_dates": {
                    symbol: market_date.isoformat()
                    for symbol, market_date in snapshot.benchmark_market_dates.items()
                },
            }
        )
        document["userportfolioperf_tracksys"] = tracking
        self._write(document)
        return snapshot

    def valuations(self) -> list[ValuationSnapshot]:
        snapshots = []
        tracking = self._read().get("userportfolioperf_tracksys", {})
        for item in tracking.get("valuations", []):
            try:
                snapshots.append(
                    ValuationSnapshot(
                        recorded_at=datetime.fromisoformat(item["recorded_at"]),
                        total_value_usd=float(item["total_value_usd"]),
                        benchmark_prices={
                            symbol: float(value)
                            for symbol, value in item["benchmark_prices"].items()
                        },
                        benchmark_market_dates={
                            symbol: date.fromisoformat(value)
                            for symbol, value in item[
                                "benchmark_market_dates"
                            ].items()
                        },
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return sorted(snapshots, key=lambda item: item.recorded_at)

    def valuation_due(self, at: datetime) -> bool:
        """Capture the first baseline immediately, then at most once per Sunday."""
        if not self.state().enabled:
            return False
        valuations = self.valuations()
        if not valuations:
            return True
        if at.weekday() != 6:
            return False
        return valuations[-1].recorded_at.date() != at.date()

    def report(self) -> PerformanceReport:
        state = self.state()
        if state.enabled_at is None:
            raise ValueError("Performance Tracking has not been enabled")
        valuations = [
            item
            for item in self.valuations()
            if item.recorded_at >= state.enabled_at
        ]
        if not valuations:
            raise ValueError("no valuation baseline has been recorded")

        baseline = valuations[0]
        current = valuations[-1]
        flows = [
            item
            for item in self.cash_flows()
            if baseline.recorded_at < item.occurred_at <= current.recorded_at
        ]

        compounded_growth = 1.0
        for previous, following in zip(valuations, valuations[1:]):
            signed_flow = sum(
                flow.amount_usd
                * (1 if flow.direction == "deposit" else -1)
                for flow in flows
                if previous.recorded_at
                < flow.occurred_at
                <= following.recorded_at
            )
            compounded_growth *= (
                following.total_value_usd - signed_flow
            ) / previous.total_value_usd
        portfolio_return_pct = (compounded_growth - 1) * 100

        comparisons = []
        for symbol in state.benchmarks:
            baseline_price = baseline.benchmark_prices[symbol]
            current_price = current.benchmark_prices[symbol]
            units = baseline.total_value_usd / baseline_price
            for flow in flows:
                signed_amount = flow.amount_usd * (
                    1 if flow.direction == "deposit" else -1
                )
                units += signed_amount / flow.benchmark_prices[symbol]
            benchmark_value = units * current_price
            benchmark_return_pct = (
                current_price / baseline_price - 1
            ) * 100
            value_gap = current.total_value_usd - benchmark_value
            performance_gap_pct = value_gap / benchmark_value * 100
            comparisons.append(
                BenchmarkComparison(
                    symbol=symbol,
                    market_date=current.benchmark_market_dates[symbol],
                    benchmark_value_usd=benchmark_value,
                    benchmark_return_pct=benchmark_return_pct,
                    value_gap_usd=value_gap,
                    performance_gap_pct=performance_gap_pct,
                    excess_return_percentage_points=(
                        portfolio_return_pct - benchmark_return_pct
                    ),
                )
            )
        return PerformanceReport(
            baseline_at=baseline.recorded_at,
            current_at=current.recorded_at,
            portfolio_value_usd=current.total_value_usd,
            portfolio_return_pct=portfolio_return_pct,
            comparisons=tuple(comparisons),
        )

    def apply_position_purchase(
        self,
        *,
        positions: list[Position],
        cash_positions: list[CashPosition],
        purchase: Position,
    ) -> tuple[list[Position], list[CashPosition]]:
        if not self.state().enabled:
            raise ValueError("Performance Tracking is not enabled")
        if purchase.quantity <= 0 or purchase.avg_cost is None:
            raise ValueError("追蹤期間買進必須提供正數數量與成交價格")
        multiplier = (
            purchase.multiplier or 100
            if purchase.instrument_type == "option"
            else 1
        )
        required_cash = purchase.quantity * purchase.avg_cost * multiplier
        result_positions = [item.model_copy(deep=True) for item in positions]
        result_cash = [item.model_copy(deep=True) for item in cash_positions]
        cash = next(
            (
                item
                for item in result_cash
                if item.broker.casefold() == purchase.broker.casefold()
                and (item.account or "").casefold()
                == (purchase.account or "").casefold()
                and item.currency == purchase.currency
            ),
            None,
        )
        if cash is None or cash.amount < required_cash:
            raise ValueError(
                "可用現金不足；請先宣告入金，或選擇有足額現金的券商帳戶"
            )
        cash.amount -= required_cash
        if cash.amount == 0:
            result_cash.remove(cash)

        existing = next(
            (
                item
                for item in result_positions
                if item.broker.casefold() == purchase.broker.casefold()
                and (item.account or "").casefold()
                == (purchase.account or "").casefold()
                and item.symbol.upper() == purchase.symbol.upper()
            ),
            None,
        )
        if existing is None:
            result_positions.append(purchase)
        else:
            old_quantity = existing.quantity
            new_quantity = old_quantity + purchase.quantity
            existing.avg_cost = (
                old_quantity * (existing.avg_cost or 0)
                + purchase.quantity * purchase.avg_cost
            ) / new_quantity
            existing.quantity = new_quantity
            existing.market_price = None
            existing.market_value = None
            existing.prev_close = None
        return result_positions, result_cash

    def apply_position_sale(
        self,
        *,
        positions: list[Position],
        cash_positions: list[CashPosition],
        position: Position,
        quantity: float,
        execution_price: float | None = None,
    ) -> tuple[list[Position], list[CashPosition]]:
        if not self.state().enabled:
            raise ValueError("Performance Tracking is not enabled")
        if quantity <= 0 or quantity > abs(position.quantity):
            raise ValueError("賣出數量必須介於零與目前持有數量之間")
        price = execution_price or position.market_price
        if price is None or price <= 0:
            raise ValueError("賣出前需要有效的成交價格")
        multiplier = (
            position.multiplier or 100
            if position.instrument_type == "option"
            else 1
        )
        proceeds = quantity * price * multiplier
        result_positions = [item.model_copy(deep=True) for item in positions]
        result_cash = [item.model_copy(deep=True) for item in cash_positions]
        target = next(
            (
                item
                for item in result_positions
                if item.broker.casefold() == position.broker.casefold()
                and (item.account or "").casefold()
                == (position.account or "").casefold()
                and item.symbol.upper() == position.symbol.upper()
            ),
            None,
        )
        if target is None:
            raise ValueError("找不到要賣出的追蹤部位")
        if quantity == abs(target.quantity):
            result_positions.remove(target)
        else:
            target.quantity -= quantity if target.quantity > 0 else -quantity
            target.market_value = None
            target.prev_close = None

        merge_cash_position(
            result_cash,
            CashPosition(
                broker=position.broker,
                account=position.account,
                currency=position.currency,
                amount=proceeds,
                notes=f"出售 {position.symbol}",
            ),
        )
        return result_positions, result_cash
