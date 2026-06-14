from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import Position


class BrokerAdapter(ABC):
    name: str

    @abstractmethod
    def fetch_positions(self, account: Optional[str] = None) -> list[Position]:
        """Return current positions. Should enrich with market data if the source provides it."""
        ...

    def fetch_account_value(self, account: Optional[str] = None) -> float:
        """Optional: return total account value (cash + positions) if easily available."""
        return sum(p.value for p in self.fetch_positions(account))
