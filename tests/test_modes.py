"""
Unit tests for addon/resources/lib/modes.py.

Covers:
- TV → Movie transition: mode changes to MOVIE_MODE, watchdog stops, no new watchdog started
- Movie → TV transition: mode changes to TV_MODE with correct channel_id, watchdog started
- TV → Rachel transition: mode changes to RACHEL_MODE, watchdog stops, rachel poller started
- Rachel → TV transition: mode changes to TV_MODE, rachel poller stopped, new watchdog started
- _cancel_all() cancels all pending timers and clears the list
- _cancel_all() signals watchdog and rachel stop events
- Fresh events are created by _cancel_all() so the next mode starts clean

Requirements: 17.1, 17.2, 17.9
"""

import sys
import threading
import time


# ---------------------------------------------------------------------------
# Ensure Kodi stubs are in sys.modules before importing addon code.
# conftest.py is auto-loaded by pytest for the tests/ package, so stubs are
# already present by the time this module is collected.
# ---------------------------------------------------------------------------

sys.path.insert(0, "addon/resources/lib")

from modes import ModeState, TV_MODE, MOVIE_MODE, RACHEL_MODE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_modes() -> ModeState:
    """Return a fresh ModeState without any active background threads."""
    return ModeState()


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

class TestInitialState:
    def test_default_mode_is_tv(self):
        modes = _make_modes()
        assert modes.current == TV_MODE

    def test_default_channel_is_empty(self):
        modes = _make_modes()
        assert modes.current_channel == ""

    def test_watchdog_stop_starts_set(self):
        """No watchdog thread should be running at construction time."""
        modes = _make_modes()
        assert modes._watchdog_stop.is_set()

    def test_rachel_stop_starts_set(self):
        """No rachel poller thread should be running at construction time."""
        modes = _make_modes()
        assert modes._rachel_stop.is_set()

    def test_timers_list_is_empty(self):
        modes = _make_modes()
        assert modes._timers == []


# ---------------------------------------------------------------------------
# transition_to_tv()
# ---------------------------------------------------------------------------

class TestTransitionToTV:
    def test_sets_current_to_tv_mode(self):
        modes = _make_modes()
        modes.transition_to_tv("nickelodeon")
        assert modes.current == TV_MODE
        modes._watchdog_stop.set()

    def test_sets_current_channel(self):
        modes = _make_modes()
        modes.transition_to_tv("cartoon-network")
        assert modes.current_channel == "cartoon-network"
        modes._watchdog_stop.set()

    def test_watchdog_stop_is_not_set_after_transition(self):
        """After transition_to_tv, the watchdog event must be clear (thread running)."""
        modes = _make_modes()
        modes.transition_to_tv("nickelodeon")
        assert not modes._watchdog_stop.is_set()
        # Signal the watchdog thread to exit cleanly.
        modes._watchdog_stop.set()

    def test_watchdog_thread_is_started(self):
        """A live watchdog daemon thread should exist after transition_to_tv."""
        modes = _make_modes()
        modes.transition_to_tv("nickelodeon")
        watchdog_threads = [
            t for t in threading.enumerate()
            if t.name == "classictv-watchdog" and t.is_alive()
        ]
        modes._watchdog_stop.set()
        assert len(watchdog_threads) >= 1

    def test_previous_watchdog_is_cancelled_on_retune(self):
        """Calling transition_to_tv twice replaces the watchdog (Req 17.9)."""
        modes = _make_modes()
        modes.transition_to_tv("nickelodeon")
        old_stop_event = modes._watchdog_stop

        modes.transition_to_tv("cartoon-network")
        # The old stop event must have been set (signalling old thread to exit).
        assert old_stop_event.is_set()
        # A fresh, unset event is in place for the new watchdog.
        assert not modes._watchdog_stop.is_set()

        modes._watchdog_stop.set()

    def test_rachel_poller_is_cancelled_on_transition_from_rachel(self):
        """Rachel poller stop event must be set when entering TV from Rachel."""
        modes = _make_modes()
        modes.transition_to_rachel()
        old_rachel_stop = modes._rachel_stop

        modes.transition_to_tv("nickelodeon")
        assert old_rachel_stop.is_set()

        modes._watchdog_stop.set()

    def test_timers_cleared_before_starting_watchdog(self):
        """Any pending timers must be cancelled on transition_to_tv."""
        modes = _make_modes()
        # Add a dummy timer to the list.
        dummy = threading.Timer(60, lambda: None)
        modes._timers.append(dummy)

        modes.transition_to_tv("nickelodeon")
        assert modes._timers == []
        assert dummy.finished.is_set()

        modes._watchdog_stop.set()


# ---------------------------------------------------------------------------
# transition_to_movie()
# ---------------------------------------------------------------------------

class TestTransitionToMovie:
    def test_sets_current_to_movie_mode(self):
        modes = _make_modes()
        modes.transition_to_movie()
        assert modes.current == MOVIE_MODE

    def test_watchdog_stop_is_set_after_transition(self):
        """No watchdog should run in Movie_Mode (Req 17.6).

        _cancel_all() signals the *old* stop event and then replaces it with a
        fresh, unset Event. We capture the old event reference before the
        transition to verify it was signalled, then wait for the thread to exit.
        """
        modes = _make_modes()
        # First enter TV mode so there is an active watchdog with a live event.
        modes.transition_to_tv("nickelodeon")
        old_watchdog_stop = modes._watchdog_stop  # capture before transition

        modes.transition_to_movie()
        # The OLD event was signalled (watchdog thread told to exit).
        assert old_watchdog_stop.is_set()
        # Wait briefly for the daemon thread to observe the stop signal and exit.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            live = [t for t in threading.enumerate()
                    if t.name == "classictv-watchdog" and t.is_alive()]
            if not live:
                break
            time.sleep(0.05)
        watchdog_threads = [
            t for t in threading.enumerate()
            if t.name == "classictv-watchdog" and t.is_alive()
        ]
        assert not watchdog_threads

    def test_no_watchdog_thread_started(self):
        """transition_to_movie must NOT start a watchdog thread (Req 17.6)."""
        modes = _make_modes()
        before = {t.name for t in threading.enumerate()}
        modes.transition_to_movie()
        after = {t.name for t in threading.enumerate()}
        new_threads = after - before
        watchdog_threads = {n for n in new_threads if "classictv-watchdog" in n}
        assert not watchdog_threads

    def test_rachel_stop_is_set_after_transition(self):
        """Rachel poller must also be cancelled when entering Movie_Mode."""
        modes = _make_modes()
        modes.transition_to_rachel()
        old_rachel_stop = modes._rachel_stop

        modes.transition_to_movie()
        assert old_rachel_stop.is_set()

    def test_timers_cleared_on_transition(self):
        modes = _make_modes()
        dummy = threading.Timer(60, lambda: None)
        modes._timers.append(dummy)

        modes.transition_to_movie()
        assert modes._timers == []
        assert dummy.finished.is_set()

    def test_current_channel_unchanged(self):
        """transition_to_movie should not alter current_channel."""
        modes = _make_modes()
        modes.transition_to_tv("nickelodeon")
        modes._watchdog_stop.set()  # stop watchdog before next transition
        modes._watchdog_stop = threading.Event()  # satisfy cancel_all check
        modes.transition_to_movie()
        # channel was set by the earlier tv transition; movie must not reset it
        assert modes.current_channel == "nickelodeon"


# ---------------------------------------------------------------------------
# transition_to_rachel()
# ---------------------------------------------------------------------------

class TestTransitionToRachel:
    def test_sets_current_to_rachel_mode(self):
        modes = _make_modes()
        modes.transition_to_rachel()
        assert modes.current == RACHEL_MODE
        modes._rachel_stop.set()

    def test_watchdog_stop_is_set_after_transition(self):
        """Watchdog must not run in Rachel_Mode (Req 17.7)."""
        modes = _make_modes()
        modes.transition_to_tv("nickelodeon")
        old_watchdog_stop = modes._watchdog_stop

        modes.transition_to_rachel()
        assert old_watchdog_stop.is_set()
        modes._rachel_stop.set()

    def test_rachel_poller_thread_is_started(self):
        """A rachel-poller daemon thread must be alive after transition."""
        modes = _make_modes()
        modes.transition_to_rachel()
        poller_threads = [
            t for t in threading.enumerate()
            if t.name == "classictv-rachel-poller" and t.is_alive()
        ]
        modes._rachel_stop.set()
        assert len(poller_threads) >= 1

    def test_rachel_stop_is_not_set_after_transition(self):
        """The rachel stop event is clear while the poller is running."""
        modes = _make_modes()
        modes.transition_to_rachel()
        assert not modes._rachel_stop.is_set()
        modes._rachel_stop.set()

    def test_timers_cleared_on_transition(self):
        modes = _make_modes()
        dummy = threading.Timer(60, lambda: None)
        modes._timers.append(dummy)

        modes.transition_to_rachel()
        assert modes._timers == []
        assert dummy.finished.is_set()
        modes._rachel_stop.set()


# ---------------------------------------------------------------------------
# _cancel_all()
# ---------------------------------------------------------------------------

class TestCancelAll:
    def test_clears_timers_list(self):
        modes = _make_modes()
        t1 = threading.Timer(60, lambda: None)
        t2 = threading.Timer(60, lambda: None)
        modes._timers.extend([t1, t2])

        modes._cancel_all()
        assert modes._timers == []

    def test_cancels_each_pending_timer(self):
        modes = _make_modes()
        t1 = threading.Timer(60, lambda: None)
        t2 = threading.Timer(60, lambda: None)
        modes._timers.extend([t1, t2])

        modes._cancel_all()
        assert t1.finished.is_set()
        assert t2.finished.is_set()

    def test_signals_watchdog_stop_event(self):
        """_cancel_all() must signal the *old* watchdog stop event.

        Because _cancel_all() replaces self._watchdog_stop with a fresh Event
        after signalling the old one, we must capture the pre-call reference.
        """
        modes = _make_modes()
        # Replace the default (set) event with an unset one to simulate an
        # active watchdog.
        old_event = threading.Event()  # unset
        modes._watchdog_stop = old_event

        modes._cancel_all()
        # The old event must have been set.
        assert old_event.is_set()

    def test_signals_rachel_stop_event(self):
        """_cancel_all() must signal the *old* rachel stop event.

        Same pattern as the watchdog test: capture the pre-call reference
        because _cancel_all() replaces self._rachel_stop after signalling it.
        """
        modes = _make_modes()
        old_event = threading.Event()  # unset
        modes._rachel_stop = old_event

        modes._cancel_all()
        assert old_event.is_set()

    def test_creates_fresh_watchdog_event(self):
        """After _cancel_all(), _watchdog_stop must be a new, unset Event (Req 17.9)."""
        modes = _make_modes()
        original_event = modes._watchdog_stop

        modes._cancel_all()
        # The event object is replaced.
        assert modes._watchdog_stop is not original_event
        # The NEW event starts unset so the next mode can use it.
        assert not modes._watchdog_stop.is_set()

    def test_creates_fresh_rachel_event(self):
        """After _cancel_all(), _rachel_stop must be a new, unset Event (Req 17.9)."""
        modes = _make_modes()
        original_event = modes._rachel_stop

        modes._cancel_all()
        assert modes._rachel_stop is not original_event
        assert not modes._rachel_stop.is_set()

    def test_cancel_all_with_no_timers_is_safe(self):
        """_cancel_all() on an empty timers list must not raise."""
        modes = _make_modes()
        modes._cancel_all()  # should not raise

    def test_cancel_all_multiple_times_is_idempotent(self):
        modes = _make_modes()
        modes._cancel_all()
        modes._cancel_all()  # second call must not raise or leave bad state
        assert modes._timers == []

    def test_already_fired_timer_cancel_is_safe(self):
        """Cancelling an already-fired timer must not raise."""
        modes = _make_modes()
        fired = threading.Event()
        t = threading.Timer(0.001, fired.set)
        t.start()
        fired.wait(timeout=1.0)  # let it fire
        modes._timers.append(t)

        modes._cancel_all()  # should not raise
        assert modes._timers == []


# ---------------------------------------------------------------------------
# Watchdog callback injection
# ---------------------------------------------------------------------------

class TestWatchdogCallback:
    def test_default_watchdog_trigger_is_noop(self):
        """Default _on_watchdog_trigger must be callable and not raise."""
        modes = _make_modes()
        modes._on_watchdog_trigger()  # should not raise

    def test_custom_watchdog_trigger_is_called(self):
        """Injected callback is called when the watchdog detects stopped playback."""
        modes = _make_modes()
        called = []
        modes._on_watchdog_trigger = lambda: called.append(True)

        # Manually invoke to confirm injection works without running real thread.
        modes._on_watchdog_trigger()
        assert called == [True]
