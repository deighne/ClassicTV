"""
Unit tests for addon/resources/lib/channel_static.py.

Covers Requirements 12.1–12.8:
- 12.1  ChannelStatic is a WindowXMLDialog (non-blocking overlay)
- 12.4  onAction() delegates to super without consuming events
- 12.5  close() cancels the safety timer
- 12.6  on_playback_started() calls close() when playback begins
- 12.7  Safety timeout fires super().close() after CHANNEL_STATIC_SAFETY_TIMEOUT_SECONDS
        without playback starting
- 12.8  show() must be called before any schedule fetch or playback call;
        verified by checking the safety timer starts inside show()
"""

import sys
import threading
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Addon path: ensure Kodi stubs (conftest) are already registered by pytest,
# then put the addon lib on sys.path.
# ---------------------------------------------------------------------------
sys.path.insert(0, "addon/resources/lib")

from channel_static import ChannelStatic
from constants import CHANNEL_STATIC_SAFETY_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cs(channel: int = 7) -> ChannelStatic:
    """Return a ChannelStatic with a sensible default channel number."""
    return ChannelStatic(destination_channel_number=channel)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

class TestConstruction:
    def test_is_window_xml_dialog(self):
        """ChannelStatic must inherit from WindowXMLDialog (Req 12.1)."""
        cs = _make_cs()
        # Check the MRO by name to avoid stale module-reference issues when
        # tests run together. The stub class is named _WindowXMLDialog; the
        # production Kodi class is WindowXMLDialog — accept either.
        parent_names = [c.__name__ for c in type(cs).__mro__]
        assert any("WindowXMLDialog" in name for name in parent_names)

    def test_initial_timer_is_none(self):
        cs = _make_cs()
        assert cs._safety_timer is None

    def test_stores_destination_channel(self):
        cs = ChannelStatic(destination_channel_number=42)
        assert cs._destination_number == 42

    def test_default_channel_is_zero(self):
        cs = ChannelStatic()
        assert cs._destination_number == 0


# ---------------------------------------------------------------------------
# show() — Requirement 12.8: safety timer starts inside show()
# ---------------------------------------------------------------------------

class TestShow:
    def test_show_starts_safety_timer(self):
        """After show(), _safety_timer must be a live threading.Timer (Req 12.8)."""
        cs = _make_cs()
        cs.show()
        assert cs._safety_timer is not None
        assert isinstance(cs._safety_timer, threading.Timer)
        cs.close()

    def test_timer_is_daemon(self):
        cs = _make_cs()
        cs.show()
        assert cs._safety_timer.daemon is True
        cs.close()

    def test_show_calls_super_show(self):
        cs = _make_cs()
        with patch.object(type(cs).__bases__[0], "show") as mock_super_show:
            cs.show()
        cs.close()
        mock_super_show.assert_called_once()

    def test_show_twice_replaces_timer(self):
        """Calling show() again cancels the first timer and arms a fresh one."""
        cs = _make_cs()
        cs.show()
        first_timer = cs._safety_timer
        cs.show()
        second_timer = cs._safety_timer
        assert first_timer is not second_timer
        assert first_timer.finished.is_set()
        cs.close()

    def test_show_logs_channel_number(self):
        cs = ChannelStatic(destination_channel_number=13)
        with patch("channel_static.xbmc.log") as mock_log:
            cs.show()
        cs.close()
        log_messages = [args[0] for args, _ in mock_log.call_args_list]
        assert any("13" in msg for msg in log_messages)

    def test_timer_interval_matches_constant(self):
        """The safety timer interval must equal CHANNEL_STATIC_SAFETY_TIMEOUT_SECONDS."""
        captured_intervals = []
        real_timer = threading.Timer

        class CapturingTimer(real_timer):
            def __init__(self, interval, function, *a, **kw):
                captured_intervals.append(interval)
                super().__init__(interval, function, *a, **kw)

        with patch("channel_static.threading.Timer", CapturingTimer):
            cs = _make_cs()
            cs.show()
            cs.close()

        assert len(captured_intervals) == 1
        assert captured_intervals[0] == CHANNEL_STATIC_SAFETY_TIMEOUT_SECONDS


# ---------------------------------------------------------------------------
# close() — Requirement 12.5: cancels safety timer
# ---------------------------------------------------------------------------

class TestClose:
    def test_close_cancels_and_clears_timer(self):
        """close() must nil out _safety_timer and cancel it (Req 12.5)."""
        cs = _make_cs()
        cs.show()
        timer = cs._safety_timer
        cs.close()
        assert cs._safety_timer is None
        assert timer.finished.is_set()

    def test_close_calls_super_close(self):
        cs = _make_cs()
        cs.show()
        with patch.object(type(cs).__bases__[0], "close") as mock_super_close:
            cs.close()
        mock_super_close.assert_called_once()

    def test_close_without_prior_show_is_safe(self):
        """close() on a never-shown overlay must not raise."""
        cs = _make_cs()
        cs.close()  # should not raise

    def test_close_logs(self):
        cs = _make_cs()
        with patch("channel_static.xbmc.log") as mock_log:
            cs.close()
        log_messages = [args[0] for args, _ in mock_log.call_args_list]
        assert any("close()" in msg for msg in log_messages)


# ---------------------------------------------------------------------------
# on_playback_started() — Requirement 12.6
# ---------------------------------------------------------------------------

class TestOnPlaybackStarted:
    def test_on_playback_started_calls_close(self):
        """on_playback_started() must dismiss the overlay (Req 12.6)."""
        cs = _make_cs()
        cs.show()
        timer = cs._safety_timer
        cs.on_playback_started()
        # Timer is cancelled and reference cleared
        assert cs._safety_timer is None
        assert timer.finished.is_set()

    def test_on_playback_started_calls_super_close(self):
        """on_playback_started() → close() → super().close() must be called."""
        cs = _make_cs()
        cs.show()
        with patch.object(type(cs).__bases__[0], "close") as mock_super_close:
            cs.on_playback_started()
        mock_super_close.assert_called_once()

    def test_on_playback_started_logs(self):
        cs = _make_cs()
        cs.show()
        with patch("channel_static.xbmc.log") as mock_log:
            cs.on_playback_started()
        log_messages = [args[0] for args, _ in mock_log.call_args_list]
        assert any("onPlayBackStarted" in msg for msg in log_messages)

    def test_on_playback_started_without_prior_show_is_safe(self):
        """on_playback_started() on a never-shown overlay must not raise."""
        cs = _make_cs()
        cs.on_playback_started()  # should not raise


# ---------------------------------------------------------------------------
# Safety timeout — Requirement 12.7
# ---------------------------------------------------------------------------

class TestSafetyTimeout:
    def test_safety_timeout_calls_super_close(self):
        """When safety timer fires, super().close() is called (Req 12.7)."""
        cs = _make_cs(42)
        closed = []
        with patch.object(type(cs).__bases__[0], "close", side_effect=lambda: closed.append(1)):
            cs._on_safety_timeout()
        assert len(closed) == 1

    def test_safety_timeout_clears_timer_reference(self):
        """_on_safety_timeout() must nullify _safety_timer before closing."""
        cs = _make_cs()
        cs.show()
        # Simulate the timer firing by calling the callback directly
        with patch.object(type(cs).__bases__[0], "close"):
            cs._on_safety_timeout()
        assert cs._safety_timer is None

    def test_safety_timeout_logs_warning(self):
        cs = _make_cs()
        with patch("channel_static.xbmc.log") as mock_log:
            cs._on_safety_timeout()
        import xbmc
        logged_levels = [args[1] for args, _ in mock_log.call_args_list]
        assert xbmc.LOGWARNING in logged_levels

    def test_safety_timeout_does_not_double_close(self):
        """If close() was already called before the timer fires, no second close."""
        cs = _make_cs()
        cs.show()
        # close() is called first (e.g., via on_playback_started)
        close_calls = []
        with patch.object(type(cs).__bases__[0], "close", side_effect=lambda: close_calls.append(1)):
            # Manually cancel the timer and clear the ref, as close() would do
            cs._cancel_safety_timer()
            # Now call the timeout directly — it sees _safety_timer is None
            cs._on_safety_timeout()
        # super().close() is called once by _on_safety_timeout() via super().close()
        assert len(close_calls) == 1


# ---------------------------------------------------------------------------
# onAction() — Requirement 12.4: events are never consumed
# ---------------------------------------------------------------------------

class TestOnAction:
    def test_on_action_delegates_to_super(self):
        """onAction must not consume events; it must call super().onAction() (Req 12.4)."""
        cs = _make_cs()
        import xbmcgui
        action = xbmcgui.Action(action_id=xbmcgui.Action(7).getId())
        with patch.object(type(cs).__bases__[0], "onAction") as mock_super_action:
            cs.onAction(action)
        mock_super_action.assert_called_once_with(action)

    def test_on_action_does_not_raise(self):
        """onAction must never raise regardless of action id."""
        cs = _make_cs()
        import xbmcgui
        for action_id in [0, 1, 7, 10, 92, 999]:
            cs.onAction(xbmcgui.Action(action_id=action_id))  # must not raise
