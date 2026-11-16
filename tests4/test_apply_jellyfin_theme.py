"""Tests for tools/apply_jellyfin_theme.py (pure branding merge)."""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
)
from apply_jellyfin_theme import build_branding  # noqa: E402


def test_replaces_css_and_banner_and_keeps_other_fields():
    current = {"CustomCss": "old", "LoginDisclaimer": "old", "SplashscreenEnabled": True}
    new = build_branding("css", "html", current)
    assert new == {"CustomCss": "css", "LoginDisclaimer": "html", "SplashscreenEnabled": True}
    assert current["CustomCss"] == "old"  # input not mutated
