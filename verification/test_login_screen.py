"""LoginScreen uses a geometric mark and wordmark, not a bitmap logo."""
from __future__ import annotations

import unittest

from textual.app import App
from textual.widgets import Button, Input, Static

from assettrack import login_logo
from assettrack import tui


class LoginBrandTests(unittest.TestCase):
    def test_brand_mark_is_geometry_not_mosaic(self):
        mark = login_logo.brand_mark()
        self.assertIn("▸", mark)
        self.assertIn(login_logo.TEAL, mark)
        for mosaic in ("▀", "▄", "█"):
            self.assertNotIn(mosaic, mark)

    def test_wordmark_splits_asset_and_track(self):
        mark = login_logo.wordmark()
        self.assertIn("Asset", mark)
        self.assertIn("Track", mark)
        self.assertIn(login_logo.NAVY, mark)
        self.assertIn(login_logo.TEAL, mark)


class LoginScreenLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_screen_shows_wordmark_and_account_entry(self):
        class HostApp(App):
            def on_mount(self):
                self.push_screen(tui.LoginScreen("alice"))

        app = HostApp()
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            screen = app.screen
            mark = str(screen.query_one("#login-mark", Static).render())
            self.assertIn("▸", mark)
            title = str(screen.query_one("#login-title", Static).render())
            self.assertIn("Asset", title)
            self.assertIn("Track", title)
            self.assertIn(
                "SMART ASSET MANAGEMENT",
                str(screen.query_one("#login-tagline", Static).render()),
            )
            self.assertEqual(screen.query_one("#user-input", Input).value, "alice")
            self.assertIsInstance(screen.query_one("#login-btn", Button), Button)
            self.assertIs(screen.focused, screen.query_one("#user-input", Input))


if __name__ == "__main__":
    unittest.main()
