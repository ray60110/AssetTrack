from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from assettrack import auth
from assettrack.models import CashPosition, Position
from assettrack import storage


class _MemoryKeyring:
    def __init__(self) -> None:
        self.secrets: dict[tuple[str, str], str] = {}

    def get_password(self, service, account):
        return self.secrets.get((service, account))

    def set_password(self, service, account, value):
        self.secrets[(service, account)] = value

    def delete_password(self, service, account):
        self.secrets.pop((service, account), None)


class AuthSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keyring = _MemoryKeyring()
        self._patches = [
            patch.object(auth.keyring, "get_password", self.keyring.get_password),
            patch.object(auth.keyring, "set_password", self.keyring.set_password),
            patch.object(auth.keyring, "delete_password", self.keyring.delete_password),
        ]
        for item in self._patches:
            item.start()
        self._iteration_patch = patch.object(auth, "PBKDF2_ITERATIONS", 1000)
        self._iteration_patch.start()
        auth.lock_vault()

    def tearDown(self) -> None:
        auth.lock_vault()
        self._iteration_patch.stop()
        for item in self._patches:
            item.stop()

    def test_register_stores_a_hash_not_the_password(self):
        auth.register_account("alice", "correct-horse")

        stored = self.keyring.get_password(auth.PASSWORD_SERVICE, "alice")
        self.assertIsNotNone(stored)
        self.assertNotEqual(stored, "correct-horse")
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertTrue(auth.account_exists("alice"))
        self.assertFalse(auth.account_exists("bob"))

    def test_verify_accepts_only_the_registered_password(self):
        auth.register_account("alice", "correct-horse")

        self.assertTrue(auth.verify_password("alice", "correct-horse"))
        self.assertFalse(auth.verify_password("alice", "wrong-password"))
        self.assertFalse(auth.verify_password("bob", "correct-horse"))

    def test_register_rejects_short_passwords(self):
        with self.assertRaises(ValueError):
            auth.register_account("alice", "secret")

    def test_legacy_short_plaintext_password_still_verifies_and_is_migrated(self):
        self.keyring.set_password(auth.PASSWORD_SERVICE, "alice", "short")

        self.assertTrue(auth.verify_password("alice", "short"))
        stored = self.keyring.get_password(auth.PASSWORD_SERVICE, "alice")
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertTrue(auth.verify_password("alice", "short"))
        self.assertFalse(auth.verify_password("alice", "other"))

    def test_legacy_plaintext_password_is_migrated_on_verify(self):
        self.keyring.set_password(auth.PASSWORD_SERVICE, "alice", "legacy-secret")

        self.assertTrue(auth.verify_password("alice", "legacy-secret"))
        stored = self.keyring.get_password(auth.PASSWORD_SERVICE, "alice")
        self.assertTrue(stored.startswith("pbkdf2_sha256$"))
        self.assertTrue(auth.verify_password("alice", "legacy-secret"))
        self.assertFalse(auth.verify_password("alice", "other-secret"))

    def test_touchid_is_not_enrolled_until_password_unlock(self):
        auth.register_account("alice", "correct-horse")
        self.assertFalse(auth.touchid_enrolled("alice"))

        auth.unlock_vault("alice", "correct-horse")
        self.assertTrue(auth.touchid_enrolled("alice"))
        self.assertTrue(auth.vault_is_unlocked())

        auth.lock_vault()
        self.assertFalse(auth.vault_is_unlocked())
        auth.unlock_vault_with_touchid("alice")
        self.assertTrue(auth.vault_is_unlocked())

    def test_touchid_cannot_unlock_an_account_that_never_authenticated(self):
        auth.register_account("alice", "correct-horse")
        with self.assertRaises(auth.AuthError):
            auth.unlock_vault_with_touchid("alice")

    def test_positions_file_is_encrypted_while_vault_is_unlocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(storage, "get_data_dir", return_value=Path(tmp)):
                auth.register_account("alice", "correct-horse")
                auth.unlock_vault("alice", "correct-horse")
                positions = [
                    Position(
                        broker="manual",
                        symbol="AAPL",
                        instrument_type="stock",
                        quantity=1,
                        avg_cost=100,
                        currency="USD",
                    )
                ]
                cash = [
                    CashPosition(broker="manual", currency="USD", amount=50)
                ]
                storage.save_manual_positions(positions, cash, user="alice")
                raw = Path(tmp, "alice_positions.json").read_text()
                self.assertTrue(raw.startswith(auth.TEXT_PREFIX))
                self.assertNotIn("AAPL", raw)
                loaded, loaded_cash = storage.load_manual_positions("alice")
                self.assertEqual(loaded[0].symbol, "AAPL")
                self.assertEqual(loaded_cash[0].amount, 50)

    def test_legacy_plaintext_positions_still_load_then_encrypt_on_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alice_positions.json"
            path.write_text(json.dumps({
                "positions": [{
                    "broker": "manual",
                    "symbol": "MSFT",
                    "instrument_type": "stock",
                    "quantity": 2,
                    "avg_cost": 10,
                    "currency": "USD",
                }],
                "cash_positions": [],
            }))
            with patch.object(storage, "get_data_dir", return_value=Path(tmp)):
                auth.register_account("alice", "correct-horse")
                auth.unlock_vault("alice", "correct-horse")
                loaded, _ = storage.load_manual_positions("alice")
                self.assertEqual(loaded[0].symbol, "MSFT")
                storage.save_manual_positions(loaded, [], user="alice")
                self.assertTrue(path.read_text().startswith(auth.TEXT_PREFIX))

    def test_sqlite_snapshot_is_encrypted_on_disk_while_vault_is_unlocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth.register_account("alice", "correct-horse")
            auth.unlock_vault("alice", "correct-horse")
            db_path = Path(tmp) / "alice_assettrack.db"
            store = storage.Storage(db_path=db_path, user="alice")
            from assettrack.models import PortfolioSnapshot
            from datetime import datetime

            snap = PortfolioSnapshot(
                timestamp=datetime(2026, 8, 23),
                total_value=1000,
                cash=100,
                by_broker={"manual": 1000},
                positions=[
                    Position(
                        broker="manual",
                        symbol="NVDA",
                        instrument_type="stock",
                        quantity=1,
                        avg_cost=100,
                        currency="USD",
                    )
                ],
            )
            store.save_snapshot(snap)
            on_disk = db_path.read_bytes()
            self.assertTrue(on_disk.startswith(auth.BINARY_PREFIX))
            self.assertNotIn(b"NVDA", on_disk)
            latest = store.get_latest_snapshot()
            self.assertEqual(latest.positions[0].symbol, "NVDA")

    def test_worker_thread_can_use_vault_unlocked_on_main_thread(self):
        auth.register_account("alice", "correct-horse")
        auth.unlock_vault("alice", "correct-horse")
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                blob = auth.encrypt_text("ledger")
                self.assertEqual(auth.decrypt_text(blob), "ledger")
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertEqual(errors, [])

    def test_touchid_unlock_in_worker_is_visible_on_main_thread(self):
        auth.register_account("alice", "correct-horse")
        auth.unlock_vault("alice", "correct-horse")
        auth.lock_vault()

        def worker() -> None:
            auth.unlock_vault_with_touchid("alice")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        self.assertTrue(auth.vault_is_unlocked())
        self.assertEqual(auth.decrypt_text(auth.encrypt_text("ok")), "ok")

    def test_corrupt_plaintext_positions_are_not_sealed_as_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alice_positions.json"
            path.write_text("{this is not valid positions json", encoding="utf-8")
            original = path.read_text(encoding="utf-8")
            with patch.object(storage, "get_data_dir", return_value=Path(tmp)):
                auth.register_account("alice", "correct-horse")
                auth.unlock_vault("alice", "correct-horse")
                with self.assertRaises(auth.AuthError):
                    storage.load_manual_positions("alice")
                with self.assertRaises(auth.AuthError):
                    storage.seal_user_files("alice")
                self.assertEqual(path.read_text(encoding="utf-8"), original)
                self.assertFalse(auth.is_encrypted_text(path.read_text(encoding="utf-8")))

    def test_valid_empty_positions_file_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alice_positions.json"
            path.write_text(
                json.dumps({"positions": [], "cash_positions": []}),
                encoding="utf-8",
            )
            with patch.object(storage, "get_data_dir", return_value=Path(tmp)):
                auth.register_account("alice", "correct-horse")
                auth.unlock_vault("alice", "correct-horse")
                positions, cash = storage.load_manual_positions("alice")
                self.assertEqual(positions, [])
                self.assertEqual(cash, [])

    def test_lost_data_key_does_not_look_like_an_empty_portfolio(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(storage, "get_data_dir", return_value=Path(tmp)):
                auth.register_account("alice", "correct-horse")
                auth.unlock_vault("alice", "correct-horse")
                storage.save_manual_positions(
                    [
                        Position(
                            broker="manual",
                            symbol="AAPL",
                            instrument_type="stock",
                            quantity=10,
                            avg_cost=100,
                            currency="USD",
                        )
                    ],
                    [CashPosition(broker="manual", currency="USD", amount=50)],
                    user="alice",
                )
                path = Path(tmp) / "alice_positions.json"
                original = path.read_text()

                self.keyring.secrets.pop((auth.DATA_KEY_SERVICE, "alice"))
                auth.lock_vault()
                auth.unlock_vault("alice", "correct-horse")

                with self.assertRaises(auth.AuthError):
                    storage.load_manual_positions("alice")
                with self.assertRaises(auth.AuthError):
                    storage.save_manual_positions([], [], user="alice")
                self.assertEqual(path.read_text(), original)

    def test_locking_the_vault_does_not_downgrade_encrypted_files_to_plaintext(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alice_positions.json"
            auth.register_account("alice", "correct-horse")
            auth.unlock_vault("alice", "correct-horse")
            auth.write_protected_text(path, '{"positions":[]}')
            ciphertext = path.read_text()
            self.assertTrue(ciphertext.startswith(auth.TEXT_PREFIX))

            auth.lock_vault()
            with self.assertRaises(auth.AuthError):
                auth.write_protected_text(path, '{"positions":[{"symbol":"WIPE"}]}')
            self.assertEqual(path.read_text(), ciphertext)
            self.assertNotIn("WIPE", path.read_text())

    def test_locking_the_vault_does_not_write_sqlite_plaintext_over_ciphertext(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alice_assettrack.db"
            auth.register_account("alice", "correct-horse")
            auth.unlock_vault("alice", "correct-horse")
            storage.Storage(db_path=db_path, user="alice")
            ciphertext = db_path.read_bytes()
            self.assertTrue(ciphertext.startswith(auth.BINARY_PREFIX))

            auth.lock_vault()
            with self.assertRaises(auth.AuthError):
                with auth.protected_sqlite(db_path) as con:
                    con.execute("create table if not exists t (x int)")
            self.assertEqual(db_path.read_bytes(), ciphertext)


class SECUserAgentGuardTests(unittest.TestCase):
    def test_headless_sec_user_agent_requires_explicit_allow(self):
        from assettrack import institutional

        with patch.dict(
            "os.environ",
            {"SEC_USER_AGENT": "Name email@example.com", "ASSETTRACK_ALLOW_SEC_USER_AGENT": ""},
            clear=False,
        ):
            with self.assertRaises(institutional.SECConfigurationError):
                institutional._sec_headers()

        with patch.dict(
            "os.environ",
            {
                "SEC_USER_AGENT": "Name email@example.com",
                "ASSETTRACK_ALLOW_SEC_USER_AGENT": "1",
            },
            clear=False,
        ):
            headers = institutional._sec_headers()
        self.assertEqual(headers["User-Agent"], "Name email@example.com")


class LoginDecryptFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_does_not_onboard_over_undecryptable_positions(self):
        from textual.app import App
        from textual.widgets import Label

        from assettrack import tui

        keyring = _MemoryKeyring()

        class HostApp(App):
            def on_mount(self):
                self.push_screen(tui.LoginScreen("alice"))

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            auth.keyring, "get_password", keyring.get_password
        ), patch.object(
            auth.keyring, "set_password", keyring.set_password
        ), patch.object(
            auth.keyring, "delete_password", keyring.delete_password
        ), patch.object(
            auth, "PBKDF2_ITERATIONS", 1000
        ), patch.object(
            storage, "get_data_dir", return_value=Path(tmp)
        ), patch.object(
            tui, "get_data_dir", return_value=Path(tmp)
        ), patch.object(
            tui, "load_sec_identity", return_value={"name": "Ada"}
        ):
            auth.lock_vault()
            auth.register_account("alice", "correct-horse")
            auth.unlock_vault("alice", "correct-horse")
            storage.save_manual_positions(
                [
                    Position(
                        broker="manual",
                        symbol="AAPL",
                        instrument_type="stock",
                        quantity=10,
                        avg_cost=100,
                        currency="USD",
                    )
                ],
                [],
                user="alice",
            )
            ciphertext = Path(tmp, "alice_positions.json").read_text()
            keyring.secrets.pop((auth.DATA_KEY_SERVICE, "alice"))
            auth.lock_vault()
            auth.unlock_vault("alice", "correct-horse")

            app = HostApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.screen._login_success("alice")
                await pilot.pause()

                self.assertIsInstance(app.screen, tui.LoginScreen)
                error = str(app.screen.query_one("#login-error-msg", Label).render())
                self.assertIn("無法讀取持倉檔", error)
                self.assertIn("以免覆蓋原資料", error)

            self.assertEqual(
                Path(tmp, "alice_positions.json").read_text(),
                ciphertext,
            )
            auth.lock_vault()


if __name__ == "__main__":
    unittest.main()
