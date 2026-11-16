"""Tests for ClassicTVPlayer — player.py"""

from unittest.mock import MagicMock

from player import ClassicTVPlayer
from schedule import ScheduleSlot


def _make_slot(
    channel_id="sitcoms",
    title="Test Show",
    jellyfin_item_id="abc123",
    start_offset=847.3,
    duration=1295.0,
) -> ScheduleSlot:
    return ScheduleSlot(
        channel_id=channel_id,
        title=title,
        jellyfin_item_id=jellyfin_item_id,
        start_offset=start_offset,
        duration=duration,
    )


def _make_player(server_id="server-uuid-abc") -> ClassicTVPlayer:
    p = ClassicTVPlayer()
    p._jellyfin_server_id = server_id
    return p


def _make_confirming_player(server_id="server-uuid-abc") -> ClassicTVPlayer:
    """Make a player whose play_slot() auto-confirms playback.

    The real watchdog_recover() waits on _play_confirm_event after calling
    play_slot(). Tests that don't exercise the confirmation logic should use
    this helper so they don't block for PLAYBACK_CONFIRM_TIMEOUT_SECONDS.
    """
    p = ClassicTVPlayer()
    p._jellyfin_server_id = server_id
    original_play_slot = p.play_slot

    def _auto_confirm(slot):
        original_play_slot(slot)
        # play_slot() clears the event after executebuiltin; set it again to
        # simulate onPlayBackStarted() firing almost immediately.
        p._play_confirm_event.set()

    p.play_slot = _auto_confirm
    return p


# ---------------------------------------------------------------------------
# play_slot — direct HTTP byte-range stream + StartOffset (default) and
# legacy plugin:// fallback
# ---------------------------------------------------------------------------


def test_play_slot_plays_direct_http_stream_with_start_offset():
    import xbmcgui

    xbmcgui.Window._reset()
    xbmcgui.Window(10000).setProperty("jellyfin_playing_id", "stale-browse-item")

    player = _make_player("srv123")
    player._base_url = "http://192.168.1.207:8096"
    slot = _make_slot(jellyfin_item_id="item456", title="Rugrats", start_offset=300.0, duration=1400.0)

    player.play_slot(slot)

    assert len(player.play_calls) == 1
    stream_url, listitem = player.play_calls[0]
    assert stream_url == "http://192.168.1.207:8096/Videos/item456/stream?static=true"
    assert listitem is not None
    assert listitem.getProperty("StartOffset") == "300.000"
    assert listitem.getProperty("IsPlayable") == "true"
    assert listitem.getVideoInfoTag().title == "Rugrats"
    assert listitem.getVideoInfoTag().resume_point == (300.0, 1400.0)
    # Direct play must clear jellyfin_playing_id so plugin.video.jellyfin ignores it
    assert xbmcgui.Window(10000).getProperty("jellyfin_playing_id") == ""


def test_play_slot_calls_executebuiltin_with_correct_url_when_direct_disabled(monkeypatch):
    import player as player_mod
    import xbmc

    monkeypatch.setattr(player_mod, "DIRECT_HTTP_PLAYBACK", False)
    xbmc.executebuiltin.reset_mock()

    player = _make_player("srv123")
    slot = _make_slot(jellyfin_item_id="item456", start_offset=300.0)

    player.play_slot(slot)

    call_args = xbmc.executebuiltin.call_args[0][0]
    assert "PlayMedia(" in call_args
    assert "plugin://plugin.video.jellyfin/" in call_args
    assert "mode=play" in call_args
    assert "id=item456" in call_args
    assert "timestamp=300" in call_args
    # server= is intentionally omitted — see player.py play_slot() comment
    assert "server=" not in call_args


def test_play_slot_avoids_zero_timestamp():
    player = _make_player()
    slot = _make_slot(start_offset=0.0)

    player.play_slot(slot)

    _, listitem = player.play_calls[-1]
    assert float(listitem.getProperty("StartOffset")) > 0.0


def test_play_slot_sets_current_channel_id():
    player = _make_player()
    slot = _make_slot(channel_id="anime")
    player.play_slot(slot)
    assert player._current_channel_id == "anime"


def test_play_slot_sets_retry_stop():
    """play_slot() must cancel any in-flight offline retry."""
    player = _make_player()
    # Simulate an in-flight retry by clearing the stop event.
    player._retry_stop.clear()
    assert not player._retry_stop.is_set()

    slot = _make_slot()
    player.play_slot(slot)

    assert player._retry_stop.is_set()


def test_offline_retry_only_one_worker_at_a_time():
    """Calling _start_offline_retry twice must cancel the first worker."""
    player = _make_player()
    player._schedule_client = MagicMock()
    player._schedule_client.get_slot.return_value = None

    first_stop = player._retry_stop
    player._start_offline_retry("sitcoms")
    second_stop = player._retry_stop

    # First stop event must have been set (first worker cancelled).
    assert first_stop.is_set()
    # New event is running (not set yet).
    assert not second_stop.is_set()

    # Clean up: cancel the second worker too.
    second_stop.set()


def test_offline_retry_cancelled_on_play_slot():
    """A successful play_slot() cancels the retry worker."""
    player = _make_player()
    player._schedule_client = MagicMock()
    player._schedule_client.get_slot.return_value = None

    player._start_offline_retry("sitcoms")
    retry_stop = player._retry_stop
    assert not retry_stop.is_set()

    # Simulate service recovery via play_slot().
    player.play_slot(_make_slot())
    assert retry_stop.is_set()


def test_is_in_grace_period_true_immediately_after_play():
    player = _make_player()
    slot = _make_slot()
    player.play_slot(slot)
    assert player.is_in_grace_period() is True


def test_is_in_grace_period_false_before_any_play():
    player = _make_player()
    # _last_play_time is 0.0 at init — grace period long since expired
    assert player.is_in_grace_period() is False


def test_play_slot_clamps_offset_exceeding_duration():
    """If StartOffset >= Duration, offset must be capped to avoid instant EOF."""
    player = _make_player()
    # 800s offset on a 128s episode — would cause immediate EOF
    slot = _make_slot(start_offset=800.0, duration=128.0)
    player.play_slot(slot)
    _, listitem = player.play_calls[-1]
    clamped = float(listitem.getProperty("StartOffset"))
    assert clamped < 128.0  # must be less than duration


def test_play_slot_does_not_clamp_valid_offset():
    """A valid offset within duration must not be changed."""
    player = _make_player()
    slot = _make_slot(start_offset=60.0, duration=1800.0)
    player.play_slot(slot)
    _, listitem = player.play_calls[-1]
    assert abs(float(listitem.getProperty("StartOffset")) - 60.0) < 0.01


# ---------------------------------------------------------------------------
# startup_play
# ---------------------------------------------------------------------------


def test_startup_play_returns_true_on_success():
    player = _make_player()
    mock_client = MagicMock()
    mock_client.get_slot.return_value = _make_slot()
    player._schedule_client = mock_client

    result = player.startup_play("sitcoms")

    assert result is True
    mock_client.get_slot.assert_called_once_with("sitcoms")


def test_startup_play_returns_false_after_all_retries_fail():
    player = _make_player()
    mock_client = MagicMock()
    mock_client.get_slot.return_value = None
    player._schedule_client = mock_client

    result = player.startup_play("sitcoms")

    assert result is False


def test_startup_play_retries_on_failure_then_succeeds():
    player = _make_player()
    mock_client = MagicMock()
    # Fail twice then succeed
    mock_client.get_slot.side_effect = [None, None, _make_slot()]
    player._schedule_client = mock_client

    result = player.startup_play("sitcoms")

    assert result is True
    assert mock_client.get_slot.call_count == 3


# ---------------------------------------------------------------------------
# watchdog_recover
# ---------------------------------------------------------------------------


def test_watchdog_recover_fetches_and_plays():
    player = _make_confirming_player()
    player._current_channel_id = "sitcoms"
    mock_client = MagicMock()
    mock_client.get_slot.return_value = _make_slot()
    player._schedule_client = mock_client

    player.watchdog_recover()

    mock_client.get_slot.assert_called_with("sitcoms")
    assert len(player.play_calls) == 1


def test_watchdog_recover_no_op_when_no_channel():
    player = _make_player()
    player._current_channel_id = ""
    mock_client = MagicMock()
    player._schedule_client = mock_client

    player.watchdog_recover()

    mock_client.get_slot.assert_not_called()


def test_watchdog_recover_no_op_when_no_client():
    player = _make_player()
    player._current_channel_id = "sitcoms"
    player._schedule_client = None

    # Should not raise
    player.watchdog_recover()


# ---------------------------------------------------------------------------
# handle_ch_up / handle_ch_down
# ---------------------------------------------------------------------------


def test_handle_ch_up_calls_change_channel():
    player = _make_player()
    player._current_channel_id = "sitcoms"

    mock_channel_list = MagicMock()
    from channels import Channel

    mock_channel_list.next_channel.return_value = Channel(
        id="anime", name="Anime", number=2
    )
    player._channel_list = mock_channel_list

    mock_client = MagicMock()
    mock_client.get_slot.return_value = _make_slot(channel_id="anime")
    player._schedule_client = mock_client

    player.handle_ch_up()

    mock_channel_list.next_channel.assert_called_once_with("sitcoms")


def test_handle_ch_up_no_op_when_no_channel_list():
    player = _make_player()
    player._channel_list = None
    # Should not raise
    player.handle_ch_up()


def test_handle_ch_down_no_op_when_no_channel_list():
    player = _make_player()
    player._channel_list = None
    player.handle_ch_down()


# ---------------------------------------------------------------------------
# watchdog_recover — channel disappeared (task 11)
# ---------------------------------------------------------------------------


def _make_channel_list_mock(channel_ids: list) -> MagicMock:
    """Return a mock ChannelList whose all_channels() returns Channel stubs."""
    from channels import Channel

    mock_cl = MagicMock()
    mock_cl.all_channels.return_value = [
        Channel(id=cid, name=cid.title(), number=i + 1)
        for i, cid in enumerate(channel_ids)
    ]
    return mock_cl


def test_watchdog_recover_tunes_to_first_channel_when_disappeared():
    """After EOF retries fail AND channel_exists returns False, the player
    should call _change_channel with the first available channel's ID instead
    of starting the offline retry loop."""
    player = _make_player()
    player._current_channel_id = "sitcoms"

    # Slot fetches all fail (channel disabled → 404 → None)
    mock_client = MagicMock()
    mock_client.get_slot.return_value = None
    mock_client.channel_exists.return_value = False  # channel is gone
    player._schedule_client = mock_client

    # Channel list has one remaining channel
    player._channel_list = _make_channel_list_mock(["anime"])

    # Stub _change_channel so we can assert it was called without side effects
    player._change_channel = MagicMock()

    player.watchdog_recover()

    player._change_channel.assert_called_once_with("anime")
    # Offline retry must NOT have started (retry_stop should still be set
    # from __init__ — i.e., no new worker was launched)
    assert player._retry_stop.is_set()


def test_watchdog_recover_starts_offline_retry_when_channel_still_exists():
    """After EOF retries fail AND channel_exists returns True (server just
    temporarily unreachable), the player falls through to the offline retry
    loop as before."""
    player = _make_player()
    player._current_channel_id = "sitcoms"

    mock_client = MagicMock()
    mock_client.get_slot.return_value = None
    mock_client.channel_exists.return_value = True  # still exists
    player._schedule_client = mock_client

    player._channel_list = _make_channel_list_mock(["sitcoms", "anime"])
    player._change_channel = MagicMock()

    player.watchdog_recover()

    # _change_channel must NOT have been called — we go to offline retry
    player._change_channel.assert_not_called()
    # Offline retry worker was started — stop event is NOT set
    assert not player._retry_stop.is_set()
    # Clean up
    player._retry_stop.set()


def test_watchdog_recover_channel_disappeared_no_channel_list():
    """If channel disappeared but channel list is None, fall back to offline
    retry rather than crashing."""
    player = _make_player()
    player._current_channel_id = "sitcoms"

    mock_client = MagicMock()
    mock_client.get_slot.return_value = None
    mock_client.channel_exists.return_value = False
    player._schedule_client = mock_client
    player._channel_list = None  # no channel list wired up

    # Should not raise; falls back to offline retry
    player.watchdog_recover()

    # Offline retry was started
    assert not player._retry_stop.is_set()
    player._retry_stop.set()


def test_watchdog_recover_channel_disappeared_empty_channel_list():
    """If channel disappeared but the channel list is empty (all channels
    disabled?), fall back to offline retry instead of crashing."""
    player = _make_player()
    player._current_channel_id = "sitcoms"

    mock_client = MagicMock()
    mock_client.get_slot.return_value = None
    mock_client.channel_exists.return_value = False
    player._schedule_client = mock_client

    # Channel list wired up but empty
    player._channel_list = _make_channel_list_mock([])

    player.watchdog_recover()

    # Falls back to offline retry
    assert not player._retry_stop.is_set()
    player._retry_stop.set()


def test_watchdog_recover_still_plays_if_slot_available():
    """Regression: if get_slot succeeds on any attempt, play immediately
    without calling channel_exists or _change_channel."""
    player = _make_confirming_player()
    player._current_channel_id = "sitcoms"

    mock_client = MagicMock()
    mock_client.get_slot.return_value = _make_slot()
    player._schedule_client = mock_client
    player._channel_list = _make_channel_list_mock(["sitcoms"])
    player._change_channel = MagicMock()

    player.watchdog_recover()

    mock_client.channel_exists.assert_not_called()
    player._change_channel.assert_not_called()
    assert len(player.play_calls) == 1


# ---------------------------------------------------------------------------
# _tune_to_first_channel (task 11)
# ---------------------------------------------------------------------------


def test_tune_to_first_channel_calls_change_channel():
    """_tune_to_first_channel invokes _change_channel with the first channel."""
    player = _make_player()
    player._channel_list = _make_channel_list_mock(["news", "sitcoms"])
    player._change_channel = MagicMock()

    player._tune_to_first_channel()

    player._change_channel.assert_called_once_with("news")


def test_tune_to_first_channel_falls_back_when_no_channel_list():
    """Without a channel list, _tune_to_first_channel starts offline retry."""
    player = _make_player()
    player._current_channel_id = "sitcoms"
    player._channel_list = None
    player._change_channel = MagicMock()

    mock_client = MagicMock()
    mock_client.get_slot.return_value = None
    player._schedule_client = mock_client

    player._tune_to_first_channel()

    player._change_channel.assert_not_called()
    assert not player._retry_stop.is_set()
    player._retry_stop.set()


def test_tune_to_first_channel_falls_back_when_empty_list():
    """Empty channel list causes _tune_to_first_channel to start offline retry."""
    player = _make_player()
    player._current_channel_id = "sitcoms"
    player._channel_list = _make_channel_list_mock([])
    player._change_channel = MagicMock()

    mock_client = MagicMock()
    mock_client.get_slot.return_value = None
    player._schedule_client = mock_client

    player._tune_to_first_channel()

    player._change_channel.assert_not_called()
    assert not player._retry_stop.is_set()
    player._retry_stop.set()


# ---------------------------------------------------------------------------
# ChannelBug wiring
# ---------------------------------------------------------------------------


def test_change_channel_success_calls_show_for():
    """_change_channel() success calls show_for with the channel's number and name."""
    from channels import Channel

    player = _make_player()

    mock_client = MagicMock()
    mock_client.get_slot.return_value = _make_slot(channel_id="ch-cbs")
    player._schedule_client = mock_client

    mock_channel_list = MagicMock()
    mock_channel_list.get_channel_by_id.return_value = Channel(
        id="ch-cbs", name="CBS", number=3
    )
    player._channel_list = mock_channel_list

    mock_bug = MagicMock()
    player._channel_bug = mock_bug

    player._change_channel("ch-cbs")

    assert mock_bug.show_for.called
    mock_bug.show_for.assert_called_with(3, "CBS")


def test_change_channel_failure_does_not_call_show_for():
    """_change_channel() with no slot does NOT call show_for."""
    from channels import Channel

    player = _make_player()

    mock_client = MagicMock()
    mock_client.get_slot.return_value = None
    player._schedule_client = mock_client

    mock_channel_list = MagicMock()
    mock_channel_list.get_channel_by_id.return_value = Channel(
        id="ch-cbs", name="CBS", number=3
    )
    player._channel_list = mock_channel_list

    mock_bug = MagicMock()
    player._channel_bug = mock_bug

    # Avoid side-effects from _resume_channel trying to fetch again
    player._resume_channel = MagicMock()

    player._change_channel("ch-cbs")

    mock_bug.show_for.assert_not_called()


def test_watchdog_recover_does_not_call_show_for():
    """watchdog_recover() success branch does NOT call show_for."""
    player = _make_confirming_player()
    player._current_channel_id = "sitcoms"

    mock_client = MagicMock()
    mock_client.get_slot.return_value = _make_slot(channel_id="sitcoms")
    player._schedule_client = mock_client

    mock_bug = MagicMock()
    player._channel_bug = mock_bug

    player.watchdog_recover()

    mock_bug.show_for.assert_not_called()


def test_startup_play_success_calls_show_for():
    """startup_play() success calls show_for with the channel's number and name."""
    from channels import Channel

    player = _make_player()

    mock_client = MagicMock()
    mock_client.get_slot.return_value = _make_slot(channel_id="ch-cbs")
    player._schedule_client = mock_client

    mock_channel_list = MagicMock()
    mock_channel_list.get_channel_by_id.return_value = Channel(
        id="ch-cbs", name="CBS", number=3
    )
    player._channel_list = mock_channel_list

    mock_bug = MagicMock()
    player._channel_bug = mock_bug

    result = player.startup_play("ch-cbs")

    assert result is True
    assert mock_bug.show_for.called
    mock_bug.show_for.assert_called_with(3, "CBS")


# ---------------------------------------------------------------------------
# Browse suspension (minimal Tier 2 subset)
# ---------------------------------------------------------------------------


def test_viewer_stop_suspends_watchdog():
    """A stop not caused by Classic TV or EOF means the viewer left."""
    player = _make_player()
    player.onPlayBackStopped()
    assert player.is_user_browsing() is True


def test_internal_stop_does_not_suspend():
    """Stopping during a channel change is Classic TV's own action."""
    import time

    player = _make_player()
    player._internal_stop_time = time.monotonic()
    player.onPlayBackStopped()
    assert player.is_user_browsing() is False


def test_stop_after_eof_does_not_suspend():
    """A stop right after a natural EOF must let the watchdog advance."""
    player = _make_player()
    player.onPlayBackEnded()
    player.onPlayBackStopped()
    assert player.is_user_browsing() is False


def test_play_slot_clears_browse_suspension():
    player = _make_player()
    player.suspend_for_browse()
    assert player.is_user_browsing() is True
    player.play_slot(_make_slot())
    assert player.is_user_browsing() is False


def test_resume_from_browse_retunes():
    from unittest.mock import patch

    player = _make_player()
    player._current_channel_id = "sitcoms"
    player.suspend_for_browse()
    with patch.object(player, "_change_channel") as mock_change:
        player.resume_from_browse()
    assert player.is_user_browsing() is False
    mock_change.assert_called_once_with("sitcoms")


def test_enter_browse_suspends_and_activates_home():
    """enter_browse() suspends the watchdog and activates the Home window."""
    import xbmc

    xbmc.executebuiltin.reset_mock()
    player = _make_player()
    player.enter_browse()
    assert player.is_user_browsing() is True
    assert any(
        "ActivateWindow(Home)" in str(c) for c in xbmc.executebuiltin.call_args_list
    )


# ---------------------------------------------------------------------------
# Three-mode integration (player <-> ModeState)
# ---------------------------------------------------------------------------


def _moded_player():
    from modes import ModeState

    p = _make_player()
    p._modes = ModeState(watchdog_interval=60, resume_grace=60)
    p._modes.player = p
    p._modes.enter_tv("sitcoms")
    p._current_channel_id = "sitcoms"
    return p


def test_own_playback_within_grace_stays_in_tv(monkeypatch):
    import xbmc
    from modes import TV_MODE

    monkeypatch.setattr(xbmc, "getInfoLabel", lambda label: "some-other-id")
    p = _moded_player()
    p.play_slot(_make_slot(channel_id="sitcoms"))  # starts the grace window
    p.onPlayBackStarted()
    assert p._modes.current == TV_MODE
    assert p.is_user_browsing() is False
    p._modes.stop()


def test_foreign_playback_outside_grace_enters_browse(monkeypatch):
    import xbmc
    from modes import BROWSE_MODE

    monkeypatch.setattr(xbmc, "getInfoLabel", lambda label: "movie-123")
    p = _moded_player()
    p._last_played_item_id = "episode-1"
    p._last_play_time = 0.0  # long ago
    p.onPlayBackStarted()
    assert p._modes.current == BROWSE_MODE
    assert p.is_user_browsing() is True
    p._modes.stop()


def test_matching_id_outside_grace_is_ours(monkeypatch):
    import xbmc
    from modes import TV_MODE

    monkeypatch.setattr(xbmc, "getInfoLabel", lambda label: "episode-1")
    p = _moded_player()
    p._last_played_item_id = "episode-1"
    p._last_play_time = 0.0
    p.onPlayBackStarted()
    assert p._modes.current == TV_MODE
    p._modes.stop()


def test_unreadable_playing_id_is_treated_as_ours(monkeypatch):
    import xbmc
    from modes import TV_MODE

    monkeypatch.setattr(xbmc, "getInfoLabel", lambda label: "")
    p = _moded_player()
    p._last_played_item_id = "episode-1"
    p._last_play_time = 0.0
    p.onPlayBackStarted()
    assert p._modes.current == TV_MODE
    p._modes.stop()


def test_viewer_stop_enters_browse_mode():
    from modes import BROWSE_MODE

    p = _moded_player()
    p.onPlayBackStopped()
    assert p._modes.current == BROWSE_MODE
    p._modes.stop()


def test_browse_playback_end_starts_resume_countdown():
    from modes import RESUME_PENDING

    p = _moded_player()
    p.suspend_for_browse("test")
    p.onPlayBackEnded()
    assert p._modes.current == RESUME_PENDING
    p._modes.stop()


def test_tv_mode_playback_end_does_not_change_mode():
    from modes import TV_MODE

    p = _moded_player()
    p.onPlayBackEnded()
    assert p._modes.current == TV_MODE
    p._modes.stop()


def test_play_slot_returns_mode_machine_to_tv():
    from modes import TV_MODE

    p = _moded_player()
    p.suspend_for_browse("test")
    p.play_slot(_make_slot(channel_id="sitcoms"))
    assert p._modes.current == TV_MODE
    assert p.is_user_browsing() is False
    p._modes.stop()


def test_ch_up_and_down_are_noops_while_browsing():
    from unittest.mock import MagicMock, patch

    p = _moded_player()
    p._channel_list = MagicMock()
    p.suspend_for_browse("test")
    with patch.object(p, "_change_channel") as change:
        p.handle_ch_up()
        p.handle_ch_down()
    change.assert_not_called()
    p._modes.stop()


# ---------------------------------------------------------------------------
# Tune timing log
# ---------------------------------------------------------------------------


def _capture_logs(monkeypatch):
    import xbmc

    lines = []
    monkeypatch.setattr(xbmc, "log", lambda msg, level=1: lines.append(msg))
    return lines


def test_change_channel_logs_phase_breakdown(monkeypatch):
    from unittest.mock import MagicMock

    lines = _capture_logs(monkeypatch)
    p = _make_player()
    p._current_channel_id = "sitcoms"
    p._schedule_client = MagicMock()
    p._schedule_client.get_slot.return_value = _make_slot(channel_id="anime")
    p._change_channel("anime")
    p.onPlayBackStarted()
    timing = [m for m in lines if "tune timing" in m]
    assert len(timing) == 1
    for field in ("stop=", "fetch=", "launch=", "total="):
        assert field in timing[0]
    assert "channel='anime'" in timing[0]


def test_timing_logged_once_per_tune(monkeypatch):
    lines = _capture_logs(monkeypatch)
    p = _make_player()
    p.play_slot(_make_slot())
    p.onPlayBackStarted()
    p.onPlayBackStarted()  # e.g. a later foreign start must not re-log
    assert len([m for m in lines if "tune timing" in m]) == 1


def test_watchdog_replay_logs_launch_only(monkeypatch):
    lines = _capture_logs(monkeypatch)
    p = _make_player()
    p.play_slot(_make_slot())
    p.onPlayBackStarted()
    msg = [m for m in lines if "tune timing" in m][0]
    assert "launch=" in msg and "stop=" not in msg and "fetch=" not in msg


def test_started_without_a_tune_logs_nothing(monkeypatch):
    lines = _capture_logs(monkeypatch)
    _make_player().onPlayBackStarted()
    assert not [m for m in lines if "tune timing" in m]


def test_replaying_the_same_slot_never_repeats_a_play_url(monkeypatch):
    """When using plugin:// delegation, every play URL must include a unique nonce."""
    import player as player_mod
    import xbmc

    monkeypatch.setattr(player_mod, "DIRECT_HTTP_PLAYBACK", False)
    xbmc.executebuiltin.reset_mock()
    p = _make_player()
    slot = _make_slot()
    p.play_slot(slot)
    p.play_slot(slot)
    urls = [c.args[0] for c in xbmc.executebuiltin.call_args_list if "PlayMedia" in c.args[0]]
    assert len(urls) == 2 and urls[0] != urls[1]


def test_change_channel_waits_for_player_open_before_showing_bug():
    """_change_channel() calls play_slot() before show_for() (via _wait_before_overlay),
    and onAVStarted() dismisses any static state via hide_static()."""
    from channels import Channel

    player = _make_player()
    call_order = []

    mock_client = MagicMock()

    def _get_slot(cid, **kw):
        call_order.append("get_slot")
        return _make_slot(channel_id=cid)

    mock_client.get_slot.side_effect = _get_slot
    player._schedule_client = mock_client

    mock_channel_list = MagicMock()
    mock_channel_list.get_channel_by_id.return_value = Channel(
        id="ch-cbs", name="CBS", number=3
    )
    player._channel_list = mock_channel_list

    mock_bug = MagicMock()
    mock_bug.show_for.side_effect = lambda num, name: call_order.append(("show_for", num, name))
    player._channel_bug = mock_bug

    player._change_channel("ch-cbs")

    assert ("show_for", 3, "CBS") in call_order
    assert call_order.index("get_slot") < call_order.index(("show_for", 3, "CBS"))

    player.onAVStarted()
    mock_bug.hide_static.assert_called_once()


def test_change_channel_failure_hides_static():
    """If get_slot() fails during _change_channel(), hide_static() is called."""
    from channels import Channel

    player = _make_player()
    mock_client = MagicMock()
    mock_client.get_slot.return_value = None
    player._schedule_client = mock_client

    mock_channel_list = MagicMock()
    mock_channel_list.get_channel_by_id.return_value = Channel(
        id="ch-cbs", name="CBS", number=3
    )
    player._channel_list = mock_channel_list

    mock_bug = MagicMock()
    player._channel_bug = mock_bug
    player._resume_channel = MagicMock()

    player._change_channel("ch-cbs")

    mock_bug.hide_static.assert_called_once()
    mock_bug.show_for.assert_not_called()


# ---------------------------------------------------------------------------
# Channel change: stop-before-play experiment flag
# ---------------------------------------------------------------------------


def _tune_player():
    from unittest.mock import MagicMock

    p = _make_player()
    p._current_channel_id = "sitcoms"
    p._schedule_client = MagicMock()
    p._schedule_client.get_slot.return_value = _make_slot(channel_id="anime")
    p._playing = True
    return p


def test_channel_change_replaces_the_stream_without_stopping_by_default():
    import player as player_mod

    assert player_mod.STOP_BEFORE_PLAY is False
    p = _tune_player()
    p._change_channel("anime")
    assert p._playing is True  # never stopped
    assert p._current_channel_id == "anime"


def test_channel_change_stops_first_when_flag_is_set(monkeypatch):
    import player as player_mod

    monkeypatch.setattr(player_mod, "STOP_BEFORE_PLAY", True)
    p = _tune_player()
    p._change_channel("anime")
    assert p._playing is False


# ---------------------------------------------------------------------------
# A slow start after our own play_slot is ours (2026-10-10 false browse)
# ---------------------------------------------------------------------------


def test_slow_first_start_after_play_slot_stays_in_tv(monkeypatch):
    """Launch took 32 s on 2026-10-10, past the 30 s grace. The start must still
    count as ours, not drop the viewer into browse mode."""
    import xbmc
    from modes import TV_MODE

    monkeypatch.setattr(xbmc, "getInfoLabel", lambda label: "some-other-id")
    p = _moded_player()
    p.play_slot(_make_slot(channel_id="sitcoms"))
    p._last_play_time -= 60  # the start arrives long after the grace window
    p.onPlayBackStarted()
    assert p._modes.current == TV_MODE
    assert p.is_user_browsing() is False
    p._modes.stop()


def test_second_start_after_a_slow_one_is_judged_normally(monkeypatch):
    """Only the first start after our play command is automatically ours; a later
    foreign start must still be classified as foreign."""
    import xbmc
    from modes import BROWSE_MODE

    monkeypatch.setattr(xbmc, "getInfoLabel", lambda label: "movie-123")
    p = _moded_player()
    p.play_slot(_make_slot(channel_id="sitcoms"))
    p._last_play_time -= 60
    p.onPlayBackStarted()  # ours (slow start)
    p._last_played_item_id = "episode-1"
    p.onPlayBackStarted()  # foreign: viewer started something else
    assert p._modes.current == BROWSE_MODE
    p._modes.stop()


def test_premature_eof_falls_back_to_next_item_and_skips_on_retune():
    """When a truncated container file hits EOF within <20s while >30s remain in
    the schedule slot, onPlayBackEnded immediately advances to NextItem and
    records the truncated item so subsequent play_slot calls skip it."""
    from schedule import NextSlot, ScheduleSlot

    p = _moded_player()
    broken_slot = ScheduleSlot(
        channel_id="its-2017",
        title="Supernatural",
        jellyfin_item_id="truncated-item-9d21",
        start_offset=2058.14,
        duration=2552.81,
        next_item=NextSlot(
            jellyfin_item_id="next-item-4917",
            title="Supernatural Next",
            start_offset=45.33,
            duration=2554.68,
        ),
    )

    p.play_slot(broken_slot)
    assert p._last_played_item_id == "truncated-item-9d21"

    # Simulate immediate demuxer EOF on the truncated file.
    p.onPlayBackEnded()

    # Should have recorded the premature EOF and immediately started NextItem.
    assert "truncated-item-9d21" in p._premature_eof_items
    assert p._last_played_item_id == "next-item-4917"
    assert p._last_played_listitem._props.get("StartOffset") == "0.001"

    # A subsequent tune/recovery during the remainder of the truncated slot
    # must also skip directly to NextItem.
    later_broken_slot = ScheduleSlot(
        channel_id="its-2017",
        title="Supernatural",
        jellyfin_item_id="truncated-item-9d21",
        start_offset=2118.14,
        duration=2552.81,
        next_item=NextSlot(
            jellyfin_item_id="next-item-4917",
            title="Supernatural Next",
            start_offset=105.33,
            duration=2554.68,
        ),
    )
    p.play_slot(later_broken_slot)
    assert p._last_played_item_id == "next-item-4917"
    p._modes.stop()


def test_grace_period_ends_immediately_after_on_playback_ended():
    """Once onPlayBackEnded fires, the startup grace period is over so the
    watchdog does not wait 20+ seconds after a short end-of-episode play."""
    p = _moded_player()
    p.play_slot(_make_slot(channel_id="sitcoms", start_offset=1790.0, duration=1800.0))
    assert p.is_in_grace_period() is True
    p.onPlayBackEnded()
    assert p.is_in_grace_period() is False
    p._modes.stop()


def test_rapid_channel_changes_supersede_stale_in_flight_tune():
    """If a newer _change_channel starts while an older _change_channel is still
    fetching its slot, the older tune must exit without calling play_slot."""
    from unittest.mock import MagicMock

    p = _tune_player()
    played_channels = []

    def fake_get_slot(ch_id, timeout=3.0):
        if ch_id == "trash-tv":
            # Simulate a second rapid press arriving while trash-tv fetch is in flight.
            p._change_channel("sitcoms")
        return _make_slot(channel_id=ch_id, jellyfin_item_id=f"item-{ch_id}")

    p._schedule_client.get_slot = MagicMock(side_effect=fake_get_slot)
    orig_play_slot = p.play_slot

    def record_play(slot):
        played_channels.append(slot.channel_id)
        orig_play_slot(slot)

    p.play_slot = record_play
    p._change_channel("trash-tv")

    # Only the final channel ('sitcoms') should have been played.
    assert played_channels == ["sitcoms"]
    assert p._current_channel_id == "sitcoms"


def test_guide_chunk_slot_builds_classic_tv_guide_stream_url():
    """Guide channel chunk IDs ('guide-<ts>') route to /ClassicTV/guide/stream/<id>
    while normal Jellyfin GUIDs route to /Videos/<id>/stream?static=true."""
    p = _make_player()
    guide_slot = _make_slot(
        channel_id="guide",
        title="TV Guide",
        jellyfin_item_id="guide-1794182400",
        start_offset=450.0,
        duration=900.0,
    )
    p.play_slot(guide_slot)
    assert (
        p._last_played_url
        == "http://192.168.1.207:8096/ClassicTV/guide/stream/guide-1794182400"
    )
    assert p._last_played_listitem._props.get("StartOffset") == "450.000"


def test_on_av_started_ignores_pre_play_callback_from_previous_stream():
    """If onAVStarted fires before play_slot during a tune (from the outgoing stream),
    it must not prematurely dismiss the new tune's static."""
    p = _make_player()
    mock_bug = MagicMock()
    p._channel_bug = mock_bug
    p._awaiting_av = False

    p.onAVStarted()
    mock_bug.hide_static.assert_not_called()

    # Once play_slot runs, _awaiting_av is True and onAVStarted dismisses static.
    p.play_slot(_make_slot(title="DuckTales"))
    p.onAVStarted()
    mock_bug.hide_static.assert_called_once()


def test_is_av_stalled_detects_missing_on_av_started():
    """is_av_stalled() is True only when isPlaying() is True past grace period
    and onAVStarted() has not fired for the active tune."""
    p = _make_player()
    p.isPlaying = lambda: True

    # Before any play_slot(), not stalled.
    assert not p.is_av_stalled()

    p.play_slot(_make_slot(title="I Love Lucy"))
    # Within grace period -> not stalled.
    assert not p.is_av_stalled()

    # Past grace period, no onAVStarted yet -> stalled!
    p._last_play_time -= 60.0
    assert p.is_av_stalled()

    # Once onAVStarted fires -> no longer stalled.
    p.onAVStarted()
    assert not p.is_av_stalled()


def test_stop_for_recovery_does_not_enter_browse():
    """stop_for_recovery() marks an internal stop so onPlayBackStopped()
    does not suspend the watchdog or enter BROWSE mode."""
    p = _make_player()
    stopped = []
    p.isPlaying = lambda: len(stopped) == 0
    p.stop = lambda: stopped.append(True)

    p.stop_for_recovery()
    assert stopped == [True]

    # Simulated onPlayBackStopped right after internal stop must not suspend.
    p.onPlayBackStopped()
    assert not p._browse_suspend


def test_watchdog_recovers_when_av_stalled():
    """ModeState watchdog stops and recovers a stalled stream even when
    isPlaying() returns True."""
    import time
    from unittest.mock import patch
    from resources.lib.modes import ModeState

    p = MagicMock()
    p.isPlaying.return_value = True
    p.is_in_grace_period.return_value = False
    p.is_user_browsing.return_value = False
    p.is_av_stalled.return_value = True

    with patch("resources.lib.modes._player_is_paused", return_value=False):
        modes = ModeState(watchdog_interval=0.05, resume_grace=0.05)
        modes.player = p
        modes.transition_to_tv("sitcoms")
        time.sleep(0.15)
        modes.stop()

    p.stop_for_recovery.assert_called()
    p.watchdog_recover.assert_called()


def test_play_slot_persists_last_channel_only_on_change():
    """play_slot() persists last_channel when tuning a channel and avoids
    redundant writes when re-tuning the same channel."""
    from unittest.mock import patch

    p = _make_player()
    mock_addon = MagicMock()
    slot_a = _make_slot(channel_id="cartoon_network", jellyfin_item_id="ep1")
    slot_a2 = _make_slot(channel_id="cartoon_network", jellyfin_item_id="ep2")
    slot_b = _make_slot(channel_id="nick_at_nite", jellyfin_item_id="ep3")

    with patch("resources.lib.player.xbmcaddon.Addon", return_value=mock_addon):
        p.play_slot(slot_a)
        p.play_slot(slot_a2)
        p.play_slot(slot_b)

    assert mock_addon.setSetting.call_args_list == [
        (("last_channel", "cartoon_network"),),
        (("last_channel", "nick_at_nite"),),
    ]


def test_browse_roundtrip_resumes_last_opened_channel():
    """Entering browse mode and returning resumes the last opened channel."""
    from unittest.mock import patch

    p = _make_player()
    p._current_channel_id = "nickelodeon"
    p.isPlaying = lambda: False

    with patch("resources.lib.player.xbmc.executebuiltin"):
        p.enter_browse()
    assert p._browse_suspend is True
    assert p._current_channel_id == "nickelodeon"

    with patch.object(p, "_change_channel") as mock_change:
        p.resume_from_browse()
    assert p._browse_suspend is False
    mock_change.assert_called_once_with("nickelodeon")




