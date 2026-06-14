from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from ..models import Position
from .base import BrokerAdapter


class IBKRBroker(BrokerAdapter):
    """
    Interactive Brokers adapter using ib_async (TWS API).

    Setup:
      pip install 'assettrack[ibkr]'
      - Start TWS or IB Gateway (paper trading recommended for testing: port 7497 / 4001)
      - In TWS/Gateway: File > Global Configuration > API > Settings > Enable "ActiveX and Socket Clients"
      - Optionally set a trusted IP (127.0.0.1) and note the port + clientId (use different clientId per script)

    Usage:
      broker = IBKRBroker(host="127.0.0.1", port=7497, client_id=42)
      positions = broker.fetch_positions(account="U1234567")  # or None for all / default
    """

    name = "ibkr"

    def __init__(
        self,
        user: str = "default",
        host: Optional[str] = None,
        port: Optional[int] = None,
        client_id: Optional[int] = None,
        timeout: float = 15,
    ):
        self.user = user
        import keyring

        # Load connection details from OS Keychain (keyring) first
        stored_host = keyring.get_password(f"assettrack_ibkr_{user}", "host")
        stored_port = keyring.get_password(f"assettrack_ibkr_{user}", "port")
        stored_client_id = keyring.get_password(f"assettrack_ibkr_{user}", "client_id")

        self.host = host or stored_host or os.getenv("IBKR_HOST", "127.0.0.1")

        env_port = os.getenv("IBKR_PORT")
        self.port = port or (int(stored_port) if stored_port else None) or (int(env_port) if env_port else 7497)

        env_client_id = os.getenv("IBKR_CLIENT_ID")
        self.client_id = client_id or (int(stored_client_id) if stored_client_id else None) or (int(env_client_id) if env_client_id else 42)
        self.timeout = timeout
        self._available = False

        try:
            from ib_async import IB  # noqa: F401

            self._available = True
        except ImportError:
            self._available = False

    def _connect(self):
        if not self._available:
            raise RuntimeError(
                "IBKR support not installed. Run:\n"
                "  pip install 'assettrack[ibkr]'\n\n"
                "Then start TWS or IB Gateway and enable API access."
            )
        from ib_async import IB, util

        ib = IB()
        try:
            ib.connect(
                host=self.host,
                port=self.port,
                clientId=self.client_id,
                timeout=self.timeout,
                readonly=True,  # safer for a tracker
            )
        except Exception as e:
            # Give a much clearer message for common setup issues
            raise RuntimeError(
                f"無法連線到 Interactive Brokers TWS / IB Gateway。\n\n"
                f"目前嘗試連線設定：\n"
                f"  Host: {self.host}\n"
                f"  Port: {self.port}\n"
                f"  Client ID: {self.client_id}\n\n"
                f"常見原因與解決方式：\n"
                f"1. 你還沒有啟動 TWS 或 IB Gateway（強烈建議先用 Paper Trading 測試）\n"
                f"2. 尚未在 TWS 裡開啟 API 權限：\n"
                f"   TWS → File → Global Configuration → API → Settings\n"
                f"   勾選 'Enable ActiveX and Socket Clients'\n"
                f"   建議把 'Trusted IP Addresses' 加入 127.0.0.1\n"
                f"3. Port 不對：\n"
                f"   - TWS Paper: 7497\n"
                f"   - TWS Live: 7496\n"
                f"   - IB Gateway Paper: 4001\n"
                f"   - IB Gateway Live: 4002\n"
                f"4. Client ID 被其他程式占用（換一個數字即可）\n"
                f"5. 你還沒在 TWS/Gateway 登入帳號\n\n"
                f"原始錯誤：{e}\n\n"
                f"你可以透過環境變數調整：\n"
                f"  export IBKR_HOST=127.0.0.1\n"
                f"  export IBKR_PORT=7497\n"
                f"  export IBKR_CLIENT_ID=42\n"
            ) from e

        # Give it a moment for initial data if needed
        util.sleep(0.5)
        return ib

    def fetch_positions(self, account: Optional[str] = None) -> list[Position]:
        from ib_async import util

        ib = self._connect()
        positions: list[Position] = []

        try:
            # Best data for market value + P&L comes from portfolio items
            # For multi-account, we may need reqAccountUpdates
            target_account = account or ""
            try:
                ib.reqAccountUpdates(True, target_account)
                util.sleep(1.0)
            except Exception:
                pass

            # portfolio() returns PortfolioItem with excellent fields
            port_items = ib.portfolio(target_account) if target_account else ib.portfolio()

            if not port_items:
                # Fallback to positions() which is lighter but may lack live marketValue
                raw_positions = ib.positions(target_account) if target_account else ib.positions()

                for rp in raw_positions:
                    contract = rp.contract
                    pos = self._contract_to_position(contract, rp.position, rp.avgCost, account or rp.account)
                    positions.append(pos)
            else:
                for item in port_items:
                    contract = item.contract
                    pos = self._contract_to_position(
                        contract,
                        item.position,
                        item.averageCost,
                        item.account,
                        market_price=item.marketPrice,
                        market_value=item.marketValue,
                        unrealized_pnl=item.unrealizedPNL,
                    )
                    positions.append(pos)

            # Try to also pull some cash / net liq for context (optional, can be added to snapshot later)
            # summary = ib.accountSummary(target_account)
            # ... look for "NetLiquidation", "TotalCashValue", etc.

            return positions

        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    def _contract_to_position(
        self,
        contract,
        quantity: float,
        avg_cost: Optional[float],
        account: Optional[str],
        market_price: Optional[float] = None,
        market_value: Optional[float] = None,
        unrealized_pnl: Optional[float] = None,
    ) -> Position:
        sec_type = getattr(contract, "secType", "STK")
        symbol = getattr(contract, "localSymbol", None) or getattr(contract, "symbol", "UNKNOWN")

        instrument_type: str = "stock"
        underlying = None
        expiry = None
        strike = None
        option_type = None

        if sec_type == "OPT":
            instrument_type = "option"
            underlying = getattr(contract, "symbol", None)
            # lastTradeDateOrContractMonth is like 20240621
            exp = getattr(contract, "lastTradeDateOrContractMonth", None)
            if exp and len(exp) >= 8:
                expiry = f"{exp[0:4]}-{exp[4:6]}-{exp[6:8]}"
            strike = getattr(contract, "strike", None)
            right = getattr(contract, "right", None)
            if right:
                option_type = "call" if str(right).upper().startswith("C") else "put"
        elif sec_type in ("ETF", "STK"):
            instrument_type = "etf" if "ETF" in str(sec_type).upper() or False else "stock"
        else:
            instrument_type = "other"

        pos = Position(
            broker="ibkr",
            account=account,
            symbol=symbol,
            instrument_type=instrument_type,  # type: ignore
            quantity=float(quantity),
            avg_cost=float(avg_cost) if avg_cost is not None else None,
            market_price=float(market_price) if market_price is not None else None,
            market_value=float(market_value) if market_value is not None else None,
            currency=getattr(contract, "currency", "USD"),
            underlying=underlying,
            expiry=expiry,
            strike=float(strike) if strike is not None else None,
            option_type=option_type,  # type: ignore
            last_updated=datetime.utcnow(),
            source="api",
        )

        # If we have unrealized from IB but no market_value, we can backfill value
        if market_value is None and unrealized_pnl is not None and pos.total_cost is not None:
            pos.market_value = pos.total_cost + unrealized_pnl

        return pos

    def fetch_account_summary(self, account: Optional[str] = None) -> dict:
        """Return a small dict with key account values (NetLiquidation, Cash, etc.)."""
        if not self._available:
            raise RuntimeError("Install assettrack[ibkr] first.")

        from ib_async import util

        ib = self._connect()
        try:
            target = account or ""
            ib.reqAccountUpdates(True, target)
            util.sleep(1)

            summary = ib.accountSummary(target)
            result = {}
            for sv in summary:
                result[sv.tag] = {"value": sv.value, "currency": sv.currency}
            return result
        finally:
            ib.disconnect()
