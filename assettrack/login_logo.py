"""Login-gate brand tokens. Colours follow AssetTrack_logo, not the PNG pixels.

Half-block renderings of the eagle PNG look like mosaic in a terminal. The login
mark is therefore geometry and type: a round card (the circular frame), a
rightward chevron (the eagle's gaze), and the AssetTrack wordmark.
"""
from __future__ import annotations

# Sampled from AssetTrack_logo/assettrack_logo.png (canvas, wordmark, teal).
CANVAS = "#f2f5ee"
SCREEN_BG = "#071018"
NAVY = "#0a3550"
NAVY_MUTED = "#4d6d78"
TEAL = "#0e7484"
TEAL_BRIGHT = "#1a9aaa"
INK = "#164563"
ERROR = "#b42318"

# One-cell box drawing + chevron; never ▀▄█ (those read as mosaic).
BRAND_MARK = f"[{TEAL}]──────  ▸  ──────[/]"
WORDMARK = f"[b][{NAVY}]Asset[/][{TEAL}]Track[/][/b]"


def brand_mark() -> str:
    """Sparse lockup for the login kicker. No bitmap, no half-blocks."""
    return BRAND_MARK


def wordmark() -> str:
    """Asset in navy, Track in teal — the print wordmark as Rich markup."""
    return WORDMARK
