"""
Unit tests for addon/resources/lib/channel_bug.py.

Covers:
- create_channel_bug returns a ChannelBug on success (Req 13.1)
- create_channel_bug returns None and logs WARNING when instantiation fails (Req 13.1)
- show() calls super().show() and starts the dismiss timer (Req 13.1, 13.3)
- close() cancels the timer and calls super().close() (Req 13.5)
- onAction() delegates to super (Req 13.4)
- Timer auto-dismiss fires close() after CHANNEL_BUG_DISPLAY_SECONDS (Req 13.3, 13.6)
- Rapid channel change: close old, show new, each gets its own timer (Req 13.5)
- Timer failure fallback calls super().close() immediately (Req 13.6)
"""

import sys
import threading
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Ensure Kodi stubs are in sys.modules before importing addon code.
# conftest.py handles this at session scope, but we also import it explicitly
# so the tests work when run in isolation.
# ---------------------------------------------------------------------------
# (conftest.py is auto-loaded by pytest for the tests/ package, so the stubs
# are already present by the time this module is collected.)

sys.path.insert(0, "addon/resources/lib")

from channel_bug import ChannelBug, create_channel_bug
from constants import CHANNEL_BUG_DISPLAY_SECONDS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bug(**kwargs) -> ChannelBug:
    """Create a ChannelBug with sensible defaults."""
    defaults = {
        "channel_number": 5,
        "channel_name": "Nickelodeon",
        "show_title": "Hey Arnold",
        "channel_color": "#ff6600",
    }
    defaults.update(kwargs)
    return ChannelBug(**defaults)


# ---------------------------------------------------------------------------
# Factory: create_channel_bug
# ---------------------------------------------------------------------------

class TestCreateChannelBug:
    def test_returns_channel_bug_on_success(self):
        bug = create_channel_bug(5, "Nick", "Hey Arnold", "#ff6600")
        assert isinstance(bug, ChannelBug)

    def test_stores_attributes(self):
        bug = create_channel_bug(7, "Cartoon Network", "Dexter's Lab", "#00ccff")
        assert bug._channel_number == 7
        assert bug._channel_name == "Cartoon Network"
        assert bug._show_title == "Dexter's Lab"
        assert bug._channel_color == "#00ccff"

    def test_returns_none_and_logs_warning_on_failure(self):
        """If ChannelBug.__init__ raises, factory returns None (Req 13.1)."""
        with patch("channel_bug.ChannelBug.__init__", side_effect=RuntimeError("boom")):
            with patch("channel_bug.xbmc.log") as mock_log:
                result = create_channel_bug(1, "X", "Y", "#000")
        assert result is None
        # Ensure a WARNING was logged
        logged_levels = [args[1] for args, _ in mock_log.call_args_list]
        import xbmc
        assert xbmc.LOGWARNING in logged_levels

    def test_initial_timer_is_none(self):
        bug = create_channel_bug(3, "PBS", "Sesame Street", "#ff0000")
        assert bug._dismiss_timer is None


# ---------------------------------------------------------------------------
# show()
# ---------------------------------------------------------------------------

class TestShow:
    def test_show_calls_super_show(self):
        bug = _make_bug()
        with patch.object(type(bug).__bases__[0], "show") as mock_super_show:
            bug.show()
        mock_super_show.assert_called_once()

    def test_show_starts_timer(self):
        bug = _make_bug()
        bug.show()
        assert bug._dismiss_timer is not None
        assert isinstance(bug._dismiss_timer, threading.Timer)
        # Clean up
        bug.close()

    def test_show_timer_is_daemon(self):
        bug = _make_bug()
        bug.show()
        assert bug._dismiss_timer.daemon is True
        bug.close()

    def test_show_logs_channel_number_and_title(self):
        bug = _make_bug(channel_number=42, show_title="SpongeBob")
        with patch("channel_bug.xbmc.log") as mock_log:
            bug.show()
        bug.close()
        log_args = [args[0] for args, _ in mock_log.call_args_list]
        assert any("ch=42" in msg and "SpongeBob" in msg for msg in log_args)

    def test_show_twice_replaces_timer(self):
        """Calling show() again cancels the previous timer (Req 13.5)."""
        bug = _make_bug()
        bug.show()
        first_timer = bug._dismiss_timer
        bug.show()
        second_timer = bug._dismiss_timer
        assert first_timer is not second_timer
        # The first timer should be cancelled (finished.is_set() is reliable;
        # is_alive() can still be True briefly after cancel() on some platforms).
        assert first_timer.finished.is_set()
        bug.close()


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------

class TestClose:
    def test_close_cancels_timer(self):
        """close() must nil out _dismiss_timer and cancel it so it won't fire."""
        bug = _make_bug()
        bug.show()
        timer = bug._dismiss_timer
        bug.close()
        # The timer reference is cleared
        assert bug._dismiss_timer is None
        # threading.Timer.cancel() sets the internal Event; the callback will
        # not be called even if the thread is still winding down.
        assert timer.finished.is_set()

    def test_close_calls_super_close(self):
        bug = _make_bug()
        bug.show()
        with patch.object(type(bug).__bases__[0], "close") as mock_super_close:
            bug.close()
        mock_super_close.assert_called_once()

    def test_close_without_prior_show_is_safe(self):
        """close() on a never-shown bug must not raise."""
        bug = _make_bug()
        bug.close()  # should not raise

    def test_close_logs(self):
        bug = _make_bug()
        with patch("channel_bug.xbmc.log") as mock_log:
            bug.close()
        log_args = [args[0] for args, _ in mock_log.call_args_list]
        assert any("close()" in msg for msg in log_args)


# ---------------------------------------------------------------------------
# onAction()
# ---------------------------------------------------------------------------

class TestOnAction:
    def test_on_action_delegates_to_super(self):
        """onAction must not intercept; it calls super().onAction() (Req 13.4)."""
        bug = _make_bug()
        import xbmcgui
        action = xbmcgui.Action(action_id=3)
        with patch.object(type(bug).__bases__[0], "onAction") as mock_super_action:
            bug.onAction(action)
        mock_super_action.assert_called_once_with(action)


# ---------------------------------------------------------------------------
# Auto-dismiss timer (Req 13.3, 13.6)
# ---------------------------------------------------------------------------

class TestAutoDismissTimer:
    def test_timer_duration_is_channel_bug_display_seconds(self):
        """The timer interval must equal CHANNEL_BUG_DISPLAY_SECONDS exactly."""
        created_timers = []
        real_timer = threading.Timer

        class CapturingTimer(real_timer):
            def __init__(self, interval, function, *args, **kwargs):
                created_timers.append(interval)
                super().__init__(interval, function, *args, **kwargs)

        with patch("channel_bug.threading.Timer", CapturingTimer):
            bug = _make_bug()
            bug.show()
            bug.close()

        assert len(created_timers) == 1
        assert created_timers[0] == CHANNEL_BUG_DISPLAY_SECONDS

    def test_timeout_calls_super_close(self):
        """When the timer fires, super().close() is called (Req 13.3)."""
        bug = _make_bug()
        closed = []

        with patch.object(type(bug).__bases__[0], "close", side_effect=lambda: closed.append(1)):
            bug._on_dismiss_timeout()

        assert len(closed) == 1

    def test_timeout_logs(self):
        bug = _make_bug()
        with patch("channel_bug.xbmc.log") as mock_log:
            bug._on_dismiss_timeout()
        log_args = [args[0] for args, _ in mock_log.call_args_list]
        assert any("auto-dismiss" in msg for msg in log_args)


# ---------------------------------------------------------------------------
# Timer failure fallback (Req 13.6)
# ---------------------------------------------------------------------------

class TestTimerFailureFallback:
    def test_timer_failure_calls_super_close_immediately(self):
        """If threading.Timer() raises, super().close() is called as fallback."""
        bug = _make_bug()
        closed = []

        with patch("channel_bug.threading.Timer", side_effect=OSError("no threads")):
            with patch.object(type(bug).__bases__[0], "close", side_effect=lambda: closed.append(1)):
                with patch.object(type(bug).__bases__[0], "show"):
                    with patch("channel_bug.xbmc.log") as mock_log:
                        bug.show()

        # super().close() must have been called as fallback
        assert len(closed) >= 1
        # A WARNING must be logged
        logged_levels = [args[1] for args, _ in mock_log.call_args_list]
        import xbmc
        assert xbmc.LOGWARNING in logged_levels


# ---------------------------------------------------------------------------
# Rapid channel change (Req 13.5)
# ---------------------------------------------------------------------------

class TestRapidChannelChange:
    def test_close_old_then_show_new(self):
        """Simulate a rapid channel change: close old bug, show new one."""
        old_bug = create_channel_bug(5, "Nick", "Hey Arnold", "#ff6600")
        new_bug = create_channel_bug(7, "CN", "Dexter", "#00ccff")

        assert old_bug is not None
        assert new_bug is not None

        old_bug.show()
        old_timer = old_bug._dismiss_timer

        # Channel change: close old, show new
        old_bug.close()
        new_bug.show()

        # Old timer is gone; new timer is active
        assert old_bug._dismiss_timer is None
        # The old timer's finished event is set (cancel() was called)
        assert old_timer.finished.is_set()
        assert new_bug._dismiss_timer is not None
        assert new_bug._dismiss_timer.is_alive()

        new_bug.close()

    def test_old_and_new_bugs_have_independent_timers(self):
        bug1 = create_channel_bug(1, "A", "Show A", "#111")
        bug2 = create_channel_bug(2, "B", "Show B", "#222")
        assert bug1 is not None and bug2 is not None

        bug1.show()
        bug2.show()

        assert bug1._dismiss_timer is not bug2._dismiss_timer

        bug1.close()
        bug2.close()
