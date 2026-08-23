from __future__ import annotations

import unittest
from unittest.mock import patch

from assettrack import sec_identity


class SECIdentityTests(unittest.TestCase):
    def test_identity_is_stored_in_keychain_and_isolated_by_assettrack_user(self):
        secrets: dict[tuple[str, str], str] = {}

        def get_password(service, account):
            return secrets.get((service, account))

        def set_password(service, account, value):
            secrets[(service, account)] = value

        with patch.object(
            sec_identity.keyring, "get_password", side_effect=get_password,
        ), patch.object(
            sec_identity.keyring, "set_password", side_effect=set_password,
        ):
            sec_identity.save_sec_identity(
                "alice",
                display_name="Alice Example",
                email="alice@example.com",
                consent=True,
            )

            self.assertEqual(
                sec_identity.load_sec_identity("alice")["email"],
                "alice@example.com",
            )
            self.assertIsNone(sec_identity.load_sec_identity("bob"))

    def test_identity_rejects_invalid_email_and_header_injection(self):
        with patch.object(sec_identity.keyring, "set_password") as write:
            invalid_values = (
                ("", "alice@example.com"),
                ("Alice Example", "not-an-email"),
                ("Alice\r\nX-Injected: yes", "alice@example.com"),
                ("Alice Example", "alice@example.com\r\nX-Injected: yes"),
            )
            for display_name, email in invalid_values:
                with self.subTest(display_name=display_name, email=email):
                    with self.assertRaises(ValueError):
                        sec_identity.save_sec_identity(
                            "alice",
                            display_name=display_name,
                            email=email,
                            consent=True,
                        )

        write.assert_not_called()

    def test_identity_can_be_masked_for_display_and_deleted(self):
        stored = (
            '{"version": 1, "display_name": "Alice Example", '
            '"email": "alice@example.com", "consent_version": 1}'
        )
        with patch.object(
            sec_identity.keyring, "get_password", return_value=stored,
        ), patch.object(
            sec_identity.keyring, "delete_password",
        ) as delete:
            summary = sec_identity.masked_sec_identity("alice")
            sec_identity.delete_sec_identity("alice")

        self.assertEqual(summary, "Alice Example <a***e@example.com>")
        delete.assert_called_once_with(
            sec_identity.SEC_IDENTITY_SERVICE, "alice",
        )

    def test_user_agent_is_built_only_from_the_selected_account(self):
        records = {
            "alice": (
                '{"version": 1, "display_name": "Alice Example", '
                '"email": "alice@example.com", "consent_version": 1}'
            ),
        }
        with patch.object(
            sec_identity.keyring,
            "get_password",
            side_effect=lambda service, account: records.get(account),
        ):
            self.assertEqual(
                sec_identity.build_sec_user_agent("alice"),
                "Alice Example alice@example.com",
            )
            with self.assertRaises(sec_identity.SECIdentityMissingError):
                sec_identity.build_sec_user_agent("bob")


class SECIdentityModalTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_identity_modal_explains_privacy_and_saves_with_consent(self):
        from textual.app import App
        from textual.widgets import Button, Checkbox, Input, Label

        from assettrack import tui

        class HostApp(App):
            def on_mount(self):
                self.push_screen(tui.SECIdentityModal("alice"))

        with patch.object(
            tui, "load_sec_identity", return_value=None,
        ), patch.object(tui, "save_sec_identity") as save:
            app = HostApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                screen = app.screen
                privacy_text = str(
                    screen.query_one("#sec-identity-privacy", Label).render()
                )
                self.assertIn("Keychain", privacy_text)
                self.assertIn("SEC", privacy_text)
                screen.query_one("#sec-identity-name", Input).value = (
                    "Alice Example"
                )
                screen.query_one("#sec-identity-email", Input).value = (
                    "alice@example.com"
                )
                screen.query_one("#sec-identity-consent", Checkbox).value = True
                screen.query_one("#sec-identity-save", Button).press()
                await pilot.pause()

        save.assert_called_once_with(
            "alice",
            display_name="Alice Example",
            email="alice@example.com",
            consent=True,
        )

    async def test_successful_login_guides_user_when_sec_identity_is_missing(self):
        from textual.app import App
        from textual.widgets import Button, Input

        from assettrack import tui

        class HostApp(App):
            def on_mount(self):
                self.push_screen(tui.LoginScreen(default_user="alice"))

        with patch.object(
            tui, "account_exists", return_value=False,
        ), patch.object(
            tui, "register_account",
        ), patch.object(
            tui, "unlock_vault",
        ), patch.object(
            tui, "load_sec_identity", return_value=None,
        ), patch.object(
            tui, "load_manual_positions", return_value=([], []),
        ):
            app = HostApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.screen.query_one("#login-btn", Button).press()
                await pilot.pause()
                register = app.screen
                register.query_one("#pwd1", Input).value = "test-password"
                register.query_one("#pwd2", Input).value = "test-password"
                register.query_one("#confirm", Button).press()
                await pilot.pause()

                self.assertIsInstance(app.screen, tui.SECIdentityModal)
                self.assertEqual(app.screen.user, "alice")

    async def test_deleting_sec_identity_requires_confirmation(self):
        from textual.app import App
        from textual.widgets import Button

        from assettrack import tui

        existing = {
            "display_name": "Alice Example",
            "email": "alice@example.com",
            "consent_version": 1,
        }

        class HostApp(App):
            def on_mount(self):
                self.push_screen(tui.SECIdentityModal("alice"))

        with patch.object(
            tui, "load_sec_identity", return_value=existing,
        ), patch.object(tui, "delete_sec_identity") as delete:
            app = HostApp()
            async with app.run_test() as pilot:
                await pilot.pause()
                app.screen.query_one(
                    "#sec-identity-delete", Button,
                ).press()
                await pilot.pause()
                self.assertIsInstance(
                    app.screen, tui.SECIdentityDeleteConfirmModal,
                )
                delete.assert_not_called()
                app.screen.query_one(
                    "#sec-identity-delete-confirm", Button,
                ).press()
                await pilot.pause()

        delete.assert_called_once_with("alice")


if __name__ == "__main__":
    unittest.main()
