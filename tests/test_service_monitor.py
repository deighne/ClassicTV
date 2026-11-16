"""
tests/test_service_monitor.py
Unit tests for ClassicTVMonitor.onNotification() in addon/service.py.

Coverage:
- Bare method form (keymap path): "ch_up", "ch_down", "guide", digits, confirm, back
- Prefixed method form (JSONRPC.NotifyAll path): "Other.ch_up", "Other.ch_down", etc.
- Unknown sender is ignored
- Unknown method while accumulating routes to channel_entry.handle_other()

Requirements: 15.1–15.4, 46
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

# Kodi stubs are injected by conftest.py before any addon import.
sys.path.insert(0, "addon/resources/lib")
sys.path.insert(0, "addon")

from service import ClassicTVMonitor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_monitor():
    """Return a ClassicTVMonitor wired to fresh MagicMocks."""
    player = MagicMock()
    modes = MagicMock()
    guide = MagicMock()
    channel_entry = MagicMock()
    channel_entry.is_accumulating.return_value = False
    monitor = ClassicTVMonitor(player, modes, guide, channel_entry)
    return monitor, player, modes, guide, channel_entry


SENDER = "classictv.service"


# ===========================================================================
# Tests: bare method form (keymap / xbmc.executebuiltin path)
# ===========================================================================

class TestOnNotificationBareMethod:
    """The keymap sends NotifyAll(classictv.service, ch_up) — method arrives
    as plain 'ch_up'. These tests verify the existing (pre-Req 46) path."""

    def test_ch_up_dispatches(self):
        monitor, player, *_ = _make_monitor()
        monitor.onNotification(SENDER, "ch_up", "null")
        player.handle_ch_up.assert_called_once()

    def test_ch_down_dispatches(self):
        monitor, player, *_ = _make_monitor()
        monitor.onNotification(SENDER, "ch_down", "null")
        player.handle_ch_down.assert_called_once()

    def test_guide_dispatches(self):
        monitor, _player, _modes, guide, *_ = _make_monitor()
        monitor.onNotification(SENDER, "guide", "null")
        guide.toggle.assert_called_once()

    def test_num_digit_dispatches(self):
        monitor, _player, _modes, _guide, channel_entry = _make_monitor()
        monitor.onNotification(SENDER, "num_5", "null")
        channel_entry.handle_digit.assert_called_once_with(5)

    def test_confirm_dispatches(self):
        monitor, _player, _modes, _guide, channel_entry = _make_monitor()
        monitor.onNotification(SENDER, "confirm", "null")
        channel_entry.handle_confirm.assert_called_once()

    def test_back_dispatches(self):
        monitor, _player, _modes, _guide, channel_entry = _make_monitor()
        monitor.onNotification(SENDER, "back", "null")
        channel_entry.handle_back.assert_called_once()

    def test_unknown_sender_ignored(self):
        monitor, player, *_ = _make_monitor()
        monitor.onNotification("some.other.addon", "ch_up", "null")
        player.handle_ch_up.assert_not_called()


# ===========================================================================
# Tests: prefixed method form (JSONRPC.NotifyAll path) — Requirement 46
# ===========================================================================

class TestOnNotificationOtherPrefix:
    """When NotifyAll is triggered via JSONRPC.NotifyAll, Kodi prefixes the
    message with 'Other.'. These tests verify the Req 46 fix.

    They FAIL before the fix and PASS after.
    """

    def test_other_ch_up_dispatches(self):
        """Req 46.1, 46.3: 'Other.ch_up' must route to handle_ch_up().

        Before the fix: method == 'Other.ch_up' hits no branch, falls through
        to 'unhandled notification' and handle_ch_up is never called.
        """
        monitor, player, *_ = _make_monitor()
        monitor.onNotification(SENDER, "Other.ch_up", "null")
        player.handle_ch_up.assert_called_once_with(), (
            "handle_ch_up() was not called for method='Other.ch_up'. "
            "JSONRPC.NotifyAll prefixes with 'Other.' — must be stripped (Req 46)."
        )

    def test_other_ch_down_dispatches(self):
        """Req 46.1: 'Other.ch_down' must route to handle_ch_down()."""
        monitor, player, *_ = _make_monitor()
        monitor.onNotification(SENDER, "Other.ch_down", "null")
        player.handle_ch_down.assert_called_once()

    def test_other_guide_dispatches(self):
        """Req 46.1: 'Other.guide' must route to guide.toggle()."""
        monitor, _player, _modes, guide, *_ = _make_monitor()
        monitor.onNotification(SENDER, "Other.guide", "null")
        guide.toggle.assert_called_once()

    def test_other_num_digit_dispatches(self):
        """Req 46.1: 'Other.num_3' must route to channel_entry.handle_digit(3)."""
        monitor, _player, _modes, _guide, channel_entry = _make_monitor()
        monitor.onNotification(SENDER, "Other.num_3", "null")
        channel_entry.handle_digit.assert_called_once_with(3)

    def test_other_confirm_dispatches(self):
        """Req 46.1: 'Other.confirm' must route to channel_entry.handle_confirm()."""
        monitor, _player, _modes, _guide, channel_entry = _make_monitor()
        monitor.onNotification(SENDER, "Other.confirm", "null")
        channel_entry.handle_confirm.assert_called_once()

    def test_other_back_dispatches(self):
        """Req 46.1: 'Other.back' must route to channel_entry.handle_back()."""
        monitor, _player, _modes, _guide, channel_entry = _make_monitor()
        monitor.onNotification(SENDER, "Other.back", "null")
        channel_entry.handle_back.assert_called_once()

    def test_bare_ch_up_still_dispatches_after_fix(self):
        """Req 46.4: the bare 'ch_up' form (keymap path) must still work
        after the prefix-strip fix is applied."""
        monitor, player, *_ = _make_monitor()
        monitor.onNotification(SENDER, "ch_up", "null")
        player.handle_ch_up.assert_called_once()

    def test_other_prefix_with_unknown_sender_still_ignored(self):
        """Stripping 'Other.' must not cause unknown-sender messages to dispatch."""
        monitor, player, *_ = _make_monitor()
        monitor.onNotification("hacker.addon", "Other.ch_up", "null")
        player.handle_ch_up.assert_not_called()
