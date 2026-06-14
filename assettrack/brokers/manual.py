from __future__ import annotations

from typing import Optional

from ..models import Position
from ..storage import load_manual_positions
from .base import BrokerAdapter


class ManualBroker(BrokerAdapter):
    name = "manual"

    def __init__(self, user: str = "default"):
        self.user = user

    def fetch_positions(self, account: Optional[str] = None) -> list[Position]:
        positions = load_manual_positions(user=self.user)
        if account:
            positions = [p for p in positions if p.account == account or p.broker == account]
        # Tag them
        for p in positions:
            p.broker = p.broker or "manual"
            p.source = "manual"
        return positions
