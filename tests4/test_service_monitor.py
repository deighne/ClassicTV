"""Tests for ClassicTVMonitor.onNotification — service.py"""

import sys
import os

# Ensure addon4 root is on path so service.py can be imported.
_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "addon4"
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import queue
from unittest.mock import MagicMock
from service import ClassicTVMonitor

SENDER = "classictv.service"


def _make_monitor():
    player = MagicMock()
    player.is_user_browsing.return_value = False
    channel_entry = MagicMock()
    channel_entry.is_accumulating.return_value = False
    cmd_queue = queue.Queue(maxsize=1)
    return (
        ClassicTVMonitor(player, channel_entry, cmd_queue),
        player,
        channel_entry,
        cmd_queue,
    )


# ---------------------------------------------------------------------------
# Channel navigation — direct dispatch (queue approach was reverted)
# ---------------------------------------------------------------------------


def test_ch_up_dispatches():
    monitor, player, _ce, _q = _make_monitor()
    monitor.onNotification(SENDER, "ch_up", "null")
    player.handle_ch_up.assert_called_once()


def test_ch_down_dispatches():
    monitor, player, _ce, _q = _make_monitor()
    monitor.onNotification(SENDER, "ch_down", "null")
    player.handle_ch_down.assert_called_once()


def test_ch_up_called_immediately():
    """ch_up must call handle_ch_up before onNotification returns."""
    monitor, player, _ce, _q = _make_monitor()
    monitor.onNotification(SENDER, "ch_up", "null")
    # If direct dispatch: called by now. Queue approach would not be.
    player.handle_ch_up.assert_called_once()


# ---------------------------------------------------------------------------
# Digit entry — synchronous
# ---------------------------------------------------------------------------


def test_digit_dispatches():
    monitor, _player, channel_entry, _q = _make_monitor()
    monitor.onNotification(SENDER, "num_7", "null")
    channel_entry.handle_digit.assert_called_once_with(7)


def test_confirm_dispatches():
    monitor, _player, channel_entry, _q = _make_monitor()
    monitor.onNotification(SENDER, "confirm", "null")
    channel_entry.handle_confirm.assert_called_once()


def test_back_dispatches_during_accumulation():
    monitor, _player, channel_entry, _q = _make_monitor()
    channel_entry.is_accumulating.return_value = True
    monitor.onNotification(SENDER, "back", "null")
    channel_entry.handle_back.assert_called_once()


def test_back_passthrough_when_not_accumulating():
    import xbmc

    monitor, _player, channel_entry, _q = _make_monitor()
    channel_entry.is_accumulating.return_value = False
    monitor.onNotification(SENDER, "back", "null")
    channel_entry.handle_back.assert_not_called()
    xbmc.executebuiltin.assert_called_with("Action(Back)")


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_wrong_sender_ignored():
    monitor, player, _ce, _q = _make_monitor()
    monitor.onNotification("other.addon", "ch_up", "null")
    player.handle_ch_up.assert_not_called()


def test_other_prefix_stripped():
    monitor, player, _ce, _q = _make_monitor()
    monitor.onNotification(SENDER, "Other.ch_up", "null")
    player.handle_ch_up.assert_called_once()


def test_unknown_method_while_accumulating_routes_to_handle_other():
    monitor, _player, channel_entry, _q = _make_monitor()
    channel_entry.is_accumulating.return_value = True
    monitor.onNotification(SENDER, "some_unknown", "null")
    channel_entry.handle_other.assert_called_once_with("some_unknown")


def test_all_digits_dispatch():
    for digit in range(10):
        monitor, _player, channel_entry, _q = _make_monitor()
        monitor.onNotification(SENDER, f"num_{digit}", "null")
        channel_entry.handle_digit.assert_called_once_with(digit)


# ---------------------------------------------------------------------------
# EPG toggle — browse suspension
# ---------------------------------------------------------------------------


def test_guide_enters_browse_when_watching():
    monitor, player, _ce, _q = _make_monitor()
    player.is_user_browsing.return_value = False
    monitor.onNotification(SENDER, "guide", "null")
    player.enter_browse.assert_called_once()


def test_guide_resumes_when_browsing():
    monitor, player, _ce, _q = _make_monitor()
    player.is_user_browsing.return_value = True
    monitor.onNotification(SENDER, "guide", "null")
    player.resume_from_browse.assert_called_once()
    player.suspend_for_browse.assert_not_called()


def test_select_startup_channel_prefers_last_channel():
    """Startup prefers last_channel when valid, then default_channel, then first."""
    from types import SimpleNamespace
    import service

    channels = [
        SimpleNamespace(id="sitcoms"),
        SimpleNamespace(id="cartoons"),
        SimpleNamespace(id="sci_fi"),
    ]
    assert service._select_startup_channel(channels, "sci_fi", "sitcoms") == "sci_fi"
    assert service._select_startup_channel(channels, "removed", "cartoons") == "cartoons"
    assert service._select_startup_channel(channels, "", "") == "sitcoms"
    assert service._select_startup_channel([], "sci_fi", "sitcoms") == "sci_fi"

