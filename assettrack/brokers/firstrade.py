from __future__ import annotations

from datetime import datetime
from typing import Optional

from ..models import Position
from .base import BrokerAdapter



class FirstradeBroker(BrokerAdapter):
    """
    Firstrade adapter (unofficial).

    Uses the reverse-engineered `firstrade` package.
    WARNING: Not official. Can break. Use at your own risk.

    pip install 'assettrack[firstrade]'

    The package supports login (2FA), get_positions(), get_account(), quotes, etc.
    See https://github.com/MaxxRK/firstrade-api
    """

    name = "firstrade"

    def __init__(self, user: str = "default"):
        self.user = user
        self._available = False
        try:
            import firstrade  # noqa: F401
            import keyring  # noqa: F401
            self._available = True
        except ImportError:
            self._available = False

    def fetch_positions(self, account: Optional[str] = None) -> list[Position]:
        if not self._available:
            raise RuntimeError(
                "Firstrade unofficial client not installed. "
                "pip install 'assettrack[firstrade]' (then store credentials safely)."
            )

        import os
        import keyring
        from firstrade.account import FTSession, FTAccountData
        from .firstrade_csv import _parse_option_symbol

        # Fetch credentials from OS Keychain (keyring) first, then fall back to env vars
        username = keyring.get_password(f"assettrack_firstrade_{self.user}", "username") or os.getenv("FIRSTRADE_USERNAME")
        password = keyring.get_password(f"assettrack_firstrade_{self.user}", "password") or os.getenv("FIRSTRADE_PASSWORD")
        pin = keyring.get_password(f"assettrack_firstrade_{self.user}", "pin") or os.getenv("FIRSTRADE_PIN") or ""
        mfa_secret = keyring.get_password(f"assettrack_firstrade_{self.user}", "mfa_secret") or os.getenv("FIRSTRADE_MFA_SECRET") or ""

        if not username or not password:
            raise RuntimeError(
                "請先使用 `assettrack set-credential --broker firstrade` 設定系統金鑰，\n"
                "或設定以下環境變數：\n"
                "  export FIRSTRADE_USERNAME=your_username\n"
                "  export FIRSTRADE_PASSWORD=your_password\n"
                "  (選填) export FIRSTRADE_PIN=your_pin\n"
                "  (選填) export FIRSTRADE_MFA_SECRET=your_totp_secret\n"
            )

        session = FTSession(
            username=username,
            password=password,
            pin=pin,
            mfa_secret=mfa_secret,
            save_session=True
        )

        try:
            logged_in = session.login()
            if not logged_in:
                raise RuntimeError("Firstrade 登入失敗。請檢查您的帳號、密碼或驗證碼設定。")
        except Exception as e:
            raise RuntimeError(f"Firstrade 登入過程中發生異常：{e}")

        account_data = FTAccountData(session)
        target_accounts = [account] if account else account_data.account_numbers

        positions: list[Position] = []

        for acc in target_accounts:
            try:
                res = account_data.get_positions(acc)
                
                # Defensive parsing of position items
                items = []
                if isinstance(res, dict):
                    if "items" in res:
                        items = res["items"]
                    elif "result" in res:
                        r = res["result"]
                        if isinstance(r, list):
                            items = r
                        elif isinstance(r, dict) and "items" in r:
                            items = r["items"]
                elif isinstance(res, list):
                    items = res

                for item in items:
                    symbol = str(item.get("symbol", "")).strip().upper()
                    if not symbol or symbol == "NAN":
                        continue

                    # Read quantity and prices defensively
                    qty_raw = item.get("quantity") or item.get("qty") or item.get("shares") or 0.0
                    quantity = float(qty_raw)

                    avg_cost_raw = item.get("avg_cost") or item.get("avgCost") or item.get("cost") or item.get("average_cost")
                    avg_cost = float(avg_cost_raw) if avg_cost_raw is not None else None

                    market_price_raw = item.get("market_price") or item.get("price") or item.get("last") or item.get("last_price")
                    market_price = float(market_price_raw) if market_price_raw is not None else None

                    market_value_raw = item.get("market_value") or item.get("value")
                    market_value = float(market_value_raw) if market_value_raw is not None else None

                    # Currency & Instrument Type
                    currency = "USD"
                    if symbol.endswith(".TW") or symbol.endswith(".TWO") or (symbol.isdigit() and len(symbol) >= 4):
                        currency = "TWD"

                    opt = _parse_option_symbol(symbol)
                    if opt.get("option_type"):
                        instrument_type = "option"
                    else:
                        if symbol.endswith(".TW") or symbol.endswith(".TWO") or (symbol.isdigit() and len(symbol) >= 4):
                            instrument_type = "stock"
                        elif symbol in {"SPY", "QQQ", "IWM", "DIA"}:
                            instrument_type = "etf"
                        else:
                            instrument_type = "stock"

                    pos = Position(
                        broker="firstrade",
                        account=acc,
                        symbol=symbol,
                        instrument_type=instrument_type,
                        quantity=quantity,
                        avg_cost=avg_cost,
                        market_price=market_price,
                        market_value=market_value,
                        currency=currency,
                        underlying=opt.get("underlying"),
                        expiry=opt.get("expiry"),
                        strike=opt.get("strike"),
                        option_type=opt.get("option_type"),
                        last_updated=datetime.utcnow(),
                        source="api",
                    )
                    positions.append(pos)
            except Exception as e:
                # Proceed to other accounts if one fails
                continue

        return positions

