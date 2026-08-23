"""Account-scoped SEC request identity stored in the operating-system Keychain.

SEC asks automated clients to identify the requester with a name and contact
email.  These values are personally identifying data, so AssetTrack stores
them alongside login credentials in the OS Keychain, under a separate service
name.  They must never be copied into portfolio caches, logs, or source files.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone

import keyring


SEC_IDENTITY_SERVICE = "assettrack_sec_identity"
SEC_IDENTITY_CONSENT_VERSION = 1
_EMAIL_RE = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?$",
    re.IGNORECASE | re.ASCII,
)


class SECIdentityMissingError(RuntimeError):
    """Raised when the selected AssetTrack account has no SEC identity."""


def _has_control_characters(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 for char in value)


def _validate_identity(user: str, display_name: str, email: str) -> tuple[str, str, str]:
    account = user.strip()
    name = display_name.strip()
    address = email.strip().lower()
    if not account or len(account) > 128 or _has_control_characters(account):
        raise ValueError("AssetTrack 使用者帳號格式無效")
    if not name or len(name) > 120 or _has_control_characters(name):
        raise ValueError("SEC 識別名稱格式無效")
    if (
        len(address) > 254
        or _has_control_characters(address)
        or not _EMAIL_RE.fullmatch(address)
    ):
        raise ValueError("SEC 聯絡信箱格式無效")
    return account, name, address


def save_sec_identity(
    user: str,
    *,
    display_name: str,
    email: str,
    consent: bool,
) -> None:
    """Store one user's SEC identity after explicit consent."""
    if not consent:
        raise ValueError("必須同意將名稱與聯絡信箱傳送給 SEC")
    account, name, address = _validate_identity(user, display_name, email)
    payload = {
        "version": 1,
        "display_name": name,
        "email": address,
        "consent_version": SEC_IDENTITY_CONSENT_VERSION,
        "consented_at": datetime.now(timezone.utc).isoformat(),
    }
    keyring.set_password(
        SEC_IDENTITY_SERVICE,
        account,
        json.dumps(payload, ensure_ascii=False),
    )


def load_sec_identity(user: str) -> dict | None:
    """Load one user's SEC identity; never falls back to another account."""
    try:
        raw = keyring.get_password(SEC_IDENTITY_SERVICE, user)
    except Exception:
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        _, name, address = _validate_identity(
            user,
            str(payload.get("display_name") or ""),
            str(payload.get("email") or ""),
        )
    except ValueError:
        return None
    if payload.get("consent_version") != SEC_IDENTITY_CONSENT_VERSION:
        return None
    payload["display_name"] = name
    payload["email"] = address
    return payload


def masked_sec_identity(user: str) -> str | None:
    """Return a privacy-safe summary suitable for settings/status screens."""
    identity = load_sec_identity(user)
    if identity is None:
        return None
    local, domain = identity["email"].split("@", 1)
    if len(local) == 1:
        masked_local = f"{local}***"
    else:
        masked_local = f"{local[0]}***{local[-1]}"
    return f"{identity['display_name']} <{masked_local}@{domain}>"


def delete_sec_identity(user: str) -> None:
    """Delete one account's SEC identity from the OS Keychain."""
    try:
        keyring.delete_password(SEC_IDENTITY_SERVICE, user.strip())
    except keyring.errors.PasswordDeleteError:
        pass


def build_sec_user_agent(user: str) -> str:
    """Build the SEC request header for exactly one AssetTrack account."""
    identity = load_sec_identity(user)
    if identity is None:
        raise SECIdentityMissingError(
            "此 AssetTrack 帳號尚未建立 SEC 識別名稱與聯絡信箱"
        )
    return f"{identity['display_name']} {identity['email']}"
