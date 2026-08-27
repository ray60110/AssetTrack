"""Account authentication and at-rest protection for personal financial files.

Passwords are stored as PBKDF2 hashes in the OS Keychain.  A separate data
encryption key unlocks positions, performance ledgers, and snapshot databases
for the current login only.  Touch ID may unlock an account only after that
account has already authenticated with a password on this device.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import keyring
from cryptography.fernet import Fernet, InvalidToken


PASSWORD_SERVICE = "assettrack_user_auth"
DATA_KEY_SERVICE = "assettrack_data_key"
TOUCHID_SERVICE = "assettrack_touchid"
MIN_PASSWORD_LENGTH = 8
PBKDF2_ITERATIONS = 390_000
TEXT_PREFIX = "ATENC1:"
BINARY_PREFIX = b"ATENC1\n"

# Login-session state must be visible to every thread: Textual workers
# persist the performance ledger, and Touch ID unlocks on a worker thread.
_vault_lock = threading.Lock()
_vault_key: Optional[bytes] = None
_vault_user: Optional[str] = None


class AuthError(RuntimeError):
    """Raised when an account cannot be unlocked or created."""


class VaultLocked(AuthError):
    """Raised when protected I/O runs without an unlocked vault."""


class VaultUserMismatch(AuthError):
    """Raised when protected I/O is for a different account than the unlocked vault."""


def _normalize_user(user: str) -> str:
    account = (user or "").strip()
    if not account or len(account) > 128 or any(ord(ch) < 32 or ord(ch) == 127 for ch in account):
        raise AuthError("AssetTrack 使用者帳號格式無效")
    if "/" in account or "\\" in account:
        raise AuthError("AssetTrack 使用者帳號格式無效")
    return account


def _hash_password(password: str, *, salt: Optional[bytes] = None) -> str:
    raw_salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        raw_salt,
        PBKDF2_ITERATIONS,
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${raw_salt.hex()}${digest.hex()}"


def _looks_hashed(stored: str) -> bool:
    return stored.startswith("pbkdf2_sha256$")


def account_exists(user: str) -> bool:
    try:
        account = _normalize_user(user)
    except AuthError:
        return False
    try:
        return bool(keyring.get_password(PASSWORD_SERVICE, account))
    except Exception:
        return False


def register_account(user: str, password: str) -> None:
    """Create a password hash and data key. Does not unlock the vault."""
    account = _normalize_user(user)
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"密碼至少需要 {MIN_PASSWORD_LENGTH} 個字元")
    if account_exists(account):
        raise AuthError("此帳號已存在")
    keyring.set_password(PASSWORD_SERVICE, account, _hash_password(password))
    _ensure_data_key(account)


def verify_password(user: str, password: str) -> bool:
    """Return True when the password matches; migrate a legacy plaintext hash."""
    try:
        account = _normalize_user(user)
    except AuthError:
        return False
    try:
        stored = keyring.get_password(PASSWORD_SERVICE, account)
    except Exception:
        return False
    if not stored:
        return False
    if not _looks_hashed(stored):
        if not hmac.compare_digest(stored, password):
            return False
        keyring.set_password(PASSWORD_SERVICE, account, _hash_password(password))
        return True
    try:
        _, iterations_s, salt_hex, digest_hex = stored.split("$", 3)
        iterations = int(iterations_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    if not hmac.compare_digest(candidate, expected):
        return False
    if iterations != PBKDF2_ITERATIONS:
        keyring.set_password(PASSWORD_SERVICE, account, _hash_password(password))
    return True


def _ensure_data_key(user: str) -> bytes:
    stored = keyring.get_password(DATA_KEY_SERVICE, user)
    if stored:
        return bytes.fromhex(stored)
    raw = os.urandom(32)
    keyring.set_password(DATA_KEY_SERVICE, user, raw.hex())
    return raw


def _fernet_from_key(key: bytes) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(key))


def _current_fernet() -> Fernet:
    with _vault_lock:
        key = _vault_key
    if key is None:
        raise VaultLocked("資料保險庫尚未解鎖")
    return _fernet_from_key(key)


def _vault_key_for(user: str) -> bytes:
    """Return the in-memory data key only when it belongs to ``user``."""
    account = _normalize_user(user)
    with _vault_lock:
        key = _vault_key
        vault_user = _vault_user
    if key is None or vault_user is None:
        raise VaultLocked("資料保險庫尚未解鎖")
    if vault_user != account:
        raise VaultUserMismatch("資料保險庫已由其他帳號解鎖")
    return key


def vault_is_unlocked() -> bool:
    with _vault_lock:
        return _vault_key is not None


def current_vault_user() -> Optional[str]:
    with _vault_lock:
        return _vault_user


def _set_vault(user: str, key: bytes) -> None:
    global _vault_key, _vault_user
    with _vault_lock:
        _vault_key = key
        _vault_user = user


def unlock_vault(user: str, password: str) -> None:
    """Verify the password, load the data key, and enroll Touch ID for this account."""
    account = _normalize_user(user)
    if not verify_password(account, password):
        raise AuthError("密碼驗證失敗")
    _set_vault(account, _ensure_data_key(account))
    keyring.set_password(TOUCHID_SERVICE, account, "enrolled")


def unlock_vault_with_touchid(user: str) -> None:
    """Unlock with the stored data key after device biometrics for an enrolled account."""
    account = _normalize_user(user)
    if not touchid_enrolled(account):
        raise AuthError("此帳號尚未以密碼啟用 Touch ID")
    _set_vault(account, _ensure_data_key(account))


def touchid_enrolled(user: str) -> bool:
    try:
        account = _normalize_user(user)
    except AuthError:
        return False
    try:
        return keyring.get_password(TOUCHID_SERVICE, account) == "enrolled"
    except Exception:
        return False


def lock_vault() -> None:
    global _vault_key, _vault_user
    with _vault_lock:
        _vault_key = None
        _vault_user = None


def encrypt_text(plaintext: str) -> str:
    token = _current_fernet().encrypt(plaintext.encode("utf-8"))
    return TEXT_PREFIX + token.decode("ascii")


def decrypt_text(blob: str) -> str:
    if not blob.startswith(TEXT_PREFIX):
        return blob
    try:
        return _current_fernet().decrypt(blob[len(TEXT_PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise AuthError("無法解密資料檔") from exc


def encrypt_bytes(data: bytes) -> bytes:
    return BINARY_PREFIX + _current_fernet().encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    if not data.startswith(BINARY_PREFIX):
        return data
    try:
        return _current_fernet().decrypt(data[len(BINARY_PREFIX):])
    except InvalidToken as exc:
        raise AuthError("無法解密資料庫") from exc


def is_encrypted_text(blob: str) -> bool:
    return blob.startswith(TEXT_PREFIX)


def _chmod_private(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_protected_text(path: Path, *, user: str) -> str:
    raw = path.read_text(encoding="utf-8")
    if not is_encrypted_text(raw):
        return raw
    key = _vault_key_for(user)
    try:
        return (
            _fernet_from_key(key)
            .decrypt(raw[len(TEXT_PREFIX):].encode("ascii"))
            .decode("utf-8")
        )
    except InvalidToken as exc:
        raise AuthError("無法解密資料檔") from exc


def write_protected_text(path: Path, text: str, *, user: str) -> None:
    """Encrypt ``text`` for ``user`` and replace ``path``. Never writes plaintext."""
    key = _vault_key_for(user)
    token = _fernet_from_key(key).encrypt(text.encode("utf-8"))
    payload = TEXT_PREFIX + token.decode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    _chmod_private(path)


@contextmanager
def protected_sqlite(path: Path, *, user: str) -> Iterator[sqlite3.Connection]:
    """Open a SQLite file, decrypting and re-encrypting with ``user``'s vault key."""
    key = _vault_key_for(user)
    fernet = _fernet_from_key(key)
    handle, tmp_name = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    working = Path(tmp_name)
    try:
        if path.exists():
            data = path.read_bytes()
            if data.startswith(BINARY_PREFIX):
                try:
                    data = fernet.decrypt(data[len(BINARY_PREFIX):])
                except InvalidToken as exc:
                    raise AuthError("無法解密資料庫") from exc
            working.write_bytes(data)
        _chmod_private(working)
        con = sqlite3.connect(str(working))
        try:
            yield con
            con.commit()
        finally:
            con.close()
        out = working.read_bytes() if working.exists() else b""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(BINARY_PREFIX + fernet.encrypt(out))
        _chmod_private(path)
    finally:
        working.unlink(missing_ok=True)
