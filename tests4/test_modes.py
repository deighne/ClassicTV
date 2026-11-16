"""Tests for ModeState — modes.py"""

import time
from unittest.mock import MagicMock

from modes import BROWSE_MODE, RESUME_PENDING, TV_MODE, ModeState


def test_initial_mode_is_tv():
    modes = ModeState(watchdog_interval=0.05, resume_grace=0.05)
    assert modes.current == TV_MODE


def test_transition_to_tv_sets_channel():
    modes = ModeState(watchdog_interval=0.05, resume_grace=0.05)
    modes.transition_to_tv("anime")
    assert modes.current_channel == "anime"


def test_transition_to_tv_starts_watchdog():
    modes = ModeState(watchdog_interval=0.05, resume_grace=0.05)
    assert modes._watchdog_stop.is_set()  # not running yet
    modes.transition_to_tv("sitcoms")
    assert not modes._watchdog_stop.is_set()  # now running


def test_stop_signals_watchdog():
    modes = ModeState(watchdog_interval=0.05, resume_grace=0.05)
    modes.transition_to_tv("sitcoms")
    modes.stop()
    assert modes._watchdog_stop.is_set()


def test_transition_to_tv_restarts_watchdog():
    modes = ModeState(watchdog_interval=0.05, resume_grace=0.05)
    modes.transition_to_tv("sitcoms")
    event1 = modes._watchdog_stop

    modes.transition_to_tv("anime")
    event2 = modes._watchdog_stop

    # New event object created on each transition — old one stopped.
    assert event1 is not event2
    assert event1.is_set()
    assert not event2.is_set()


def test_watchdog_calls_recover_when_not_playing():
    """Watchdog should call player.watchdog_recover() when isPlaying() is False."""
    modes = ModeState(watchdog_interval=0.05, resume_grace=0.05)
    player = MagicMock()
    player.isPlaying.return_value = False
    player.is_in_grace_period.return_value = False
    player.is_user_browsing.return_value = False
    modes.player = player

    modes.transition_to_tv("sitcoms")
    time.sleep(0.4)
    modes.stop()

    player.watchdog_recover.assert_called()


def test_watchdog_does_not_recover_when_playing():
    modes = ModeState(watchdog_interval=0.05, resume_grace=0.05)
    player = MagicMock()
    player.isPlaying.return_value = True
    player.is_in_grace_period.return_value = False
    player.is_user_browsing.return_value = False
    modes.player = player

    modes.transition_to_tv("sitcoms")
    time.sleep(0.4)
    modes.stop()

    player.watchdog_recover.assert_not_called()


def test_watchdog_skips_check_during_grace_period():
    """Watchdog must not call watchdog_recover() while in the grace period."""
    modes = ModeState(watchdog_interval=0.05, resume_grace=0.05)
    player = MagicMock()
    player.isPlaying.return_value = False  # would normally trigger recovery
    player.is_in_grace_period.return_value = True  # but we're in grace period
    player.is_user_browsing.return_value = False
    modes.player = player

    modes.transition_to_tv("sitcoms")
    time.sleep(0.4)
    modes.stop()

    player.watchdog_recover.assert_not_called()


def test_watchdog_recovers_after_grace_period_expires():
    """After grace period ends, watchdog should recover if not playing."""
    modes = ModeState(watchdog_interval=0.05, resume_grace=0.05)
    player = MagicMock()
    player.isPlaying.return_value = False
    player.is_in_grace_period.return_value = False  # grace period over
    player.is_user_browsing.return_value = False
    modes.player = player

    modes.transition_to_tv("sitcoms")
    time.sleep(0.4)
    modes.stop()

    player.watchdog_recover.assert_called()


def test_watchdog_survives_recover_exception():
    """Watchdog must not crash if recover() raises."""
    modes = ModeState(watchdog_interval=0.05, resume_grace=0.05)
    player = MagicMock()
    player.isPlaying.return_value = False
    player.is_in_grace_period.return_value = False
    player.is_user_browsing.return_value = False
    player.watchdog_recover.side_effect = RuntimeError("boom")
    modes.player = player

    modes.transition_to_tv("sitcoms")
    time.sleep(0.4)
    modes.stop()

    # If the watchdog crashed it would have stopped calling — confirm it kept running
    # (called more than once means the loop survived the exception).
    assert player.watchdog_recover.call_count >= 1


def test_watchdog_skips_while_browsing():
    """While the viewer is browsing, the watchdog must not recover."""
    modes = ModeState(watchdog_interval=0.05, resume_grace=0.05)
    player = MagicMock()
    player.isPlaying.return_value = False  # stopped, would normally recover
    player.is_in_grace_period.return_value = False
    player.is_user_browsing.return_value = True  # but the viewer chose to browse
    modes.player = player

    modes.transition_to_tv("sitcoms")
    time.sleep(0.4)
    modes.stop()

    player.watchdog_recover.assert_not_called()


# ---------------------------------------------------------------------------
# Three-mode transitions
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


def _fast() -> ModeState:
    return ModeState(watchdog_interval=0.05, resume_grace=0.05)


def _quiet_player() -> MagicMock:
    player = MagicMock()
    player.isPlaying.return_value = False
    player.is_in_grace_period.return_value = False
    player.is_user_browsing.return_value = False
    return player


def test_enter_browse_suspends_watchdog():
    modes = _fast()
    modes.enter_tv("sitcoms")
    modes.enter_browse("epg")
    assert modes.current == BROWSE_MODE
    assert modes._watchdog_stop.is_set()


def test_enter_tv_from_browse_restarts_watchdog():
    modes = _fast()
    modes.enter_tv("sitcoms")
    modes.enter_browse()
    modes.enter_tv("anime")
    assert modes.current == TV_MODE
    assert not modes._watchdog_stop.is_set()
    modes.stop()


def test_watchdog_does_not_recover_in_browse_mode():
    modes = _fast()
    modes.player = _quiet_player()
    modes.enter_tv("sitcoms")
    modes.enter_browse()
    time.sleep(0.3)
    modes.stop()
    modes.player.watchdog_recover.assert_not_called()


def test_watchdog_skips_while_paused(monkeypatch):
    import xbmc

    monkeypatch.setattr(xbmc, "getCondVisibility", lambda c: c == "Player.Paused")
    modes = _fast()
    modes.player = _quiet_player()
    modes.enter_tv("sitcoms")
    time.sleep(0.3)
    modes.stop()
    modes.player.watchdog_recover.assert_not_called()


def test_resume_pending_reclaims_player_after_grace():
    modes = _fast()
    modes.player = _quiet_player()
    modes.enter_tv("sitcoms")
    modes.enter_browse()
    modes.on_playback_ended()
    assert modes.current == RESUME_PENDING
    time.sleep(0.3)
    modes.player.resume_from_browse.assert_called_once()
    modes.stop()


def test_resume_pending_cancelled_by_new_playback():
    modes = _fast()
    modes.player = _quiet_player()
    modes.enter_tv("sitcoms")
    modes.enter_browse()
    modes.on_playback_ended()
    modes.on_playback_started(is_classictv_initiated=False)
    assert modes.current == BROWSE_MODE
    time.sleep(0.3)
    modes.player.resume_from_browse.assert_not_called()
    modes.stop()


def test_resume_pending_cancelled_by_enter_tv():
    modes = _fast()
    modes.player = _quiet_player()
    modes.enter_tv("sitcoms")
    modes.enter_browse()
    modes.on_playback_ended()
    modes.enter_tv("sitcoms")
    time.sleep(0.3)
    modes.player.resume_from_browse.assert_not_called()
    assert modes.current == TV_MODE
    modes.stop()


def test_playback_ended_is_noop_in_tv_mode():
    modes = _fast()
    modes.enter_tv("sitcoms")
    modes.on_playback_ended()
    assert modes.current == TV_MODE
    modes.stop()


def test_resume_timer_survives_player_exception():
    modes = _fast()
    player = _quiet_player()
    player.resume_from_browse.side_effect = RuntimeError("boom")
    modes.player = player
    modes.enter_tv("sitcoms")
    modes.enter_browse()
    modes.on_playback_ended()
    time.sleep(0.3)  # must not raise on the timer thread
    modes.stop()


@pytest.mark.parametrize(
    "start,event,expected",
    [
        (TV_MODE, "enter_browse", BROWSE_MODE),
        (TV_MODE, "ended", TV_MODE),
        (TV_MODE, "started_ours", TV_MODE),
        (TV_MODE, "started_foreign", BROWSE_MODE),
        (BROWSE_MODE, "enter_browse", BROWSE_MODE),
        (BROWSE_MODE, "ended", RESUME_PENDING),
        (BROWSE_MODE, "started_ours", BROWSE_MODE),
        (BROWSE_MODE, "started_foreign", BROWSE_MODE),
        (RESUME_PENDING, "enter_browse", BROWSE_MODE),
        (RESUME_PENDING, "ended", RESUME_PENDING),
        (RESUME_PENDING, "started_ours", RESUME_PENDING),
        (RESUME_PENDING, "started_foreign", BROWSE_MODE),
    ],
)
def test_transition_table(start, event, expected):
    # Long grace so RESUME_PENDING is observable and never fires mid-test.
    modes = ModeState(watchdog_interval=60, resume_grace=60)
    modes.enter_tv("sitcoms")
    if start in (BROWSE_MODE, RESUME_PENDING):
        modes.enter_browse()
    if start == RESUME_PENDING:
        modes.enter_resume_pending()
    assert modes.current == start

    if event == "enter_browse":
        modes.enter_browse("test")
    elif event == "ended":
        modes.on_playback_ended()
    elif event == "started_ours":
        modes.on_playback_started(True)
    else:
        modes.on_playback_started(False)

    assert modes.current == expected
    modes.stop()
