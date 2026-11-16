"""Tests for mirror channels: mirror.py and the player/schedule integration."""

import time
from unittest.mock import MagicMock, patch

import pytest
import xbmcgui

import mirror
from channels import Channel, ChannelList
from mirror import IDLE, OK, RETUNE, MirrorSync, decide, lead_seconds, with_lead
from player import ClassicTVPlayer
from schedule import ChannelInfo, ScheduleClient, ScheduleSlot


def _slot(item="item-a", offset=100.0, duration=1800.0, paused=False, channel="rachel"):
    return ScheduleSlot(
        channel_id=channel,
        title="t",
        jellyfin_item_id=item,
        start_offset=offset,
        duration=duration,
        mirror=True,
        paused=paused,
    )


# --- decide ------------------------------------------------------------------


def _decide(slot, **kw):
    args = dict(
        playing_item_id="item-a", playing_time=100.0, is_playing=True, in_grace=False, lead=6
    )
    args.update(kw)
    return decide(slot, **args)


def test_in_step_is_ok():
    assert _decide(_slot(offset=105)) == OK


def test_unreachable_server_keeps_playing():
    assert _decide(None) == OK


def test_no_item_is_idle():
    assert _decide(_slot(item="")) == IDLE


def test_paused_mirror_is_left_alone_even_if_drifted():
    assert _decide(_slot(paused=True, offset=10), playing_time=500) == OK


def test_new_item_retunes():
    assert _decide(_slot(item="item-b")) == RETUNE


def test_drift_beyond_threshold_retunes():
    assert _decide(_slot(offset=100), playing_time=140) == RETUNE
    assert _decide(_slot(offset=100), playing_time=60) == RETUNE


def test_small_drift_is_tolerated():
    assert _decide(_slot(offset=100), playing_time=115) == OK


def test_stopped_playback_retunes_but_not_during_startup_grace():
    assert _decide(_slot(), is_playing=False) == RETUNE
    assert _decide(_slot(), is_playing=False, in_grace=True) == OK


def test_unknown_player_time_does_not_retune():
    assert _decide(_slot(), playing_time=None) == OK


# --- lead --------------------------------------------------------------------


def test_lead_defaults_and_clamps():
    assert lead_seconds(None) == mirror.MIRROR_DEFAULT_LEAD_SECONDS
    assert lead_seconds(0.5) == mirror.MIRROR_MIN_LEAD_SECONDS
    assert lead_seconds(99) == mirror.MIRROR_MAX_LEAD_SECONDS
    assert lead_seconds(6.4) == 6.4


def test_with_lead_adds_and_stays_inside_item():
    assert with_lead(_slot(offset=100, duration=1800), 6).start_offset == 106
    assert with_lead(_slot(offset=1798, duration=1800), 6).start_offset == 1799


# --- schedule client parsing ---------------------------------------------------


def test_schedule_client_parses_mirror_fields():
    client = ScheduleClient("http://x")
    with patch.object(
        client,
        "_get_json",
        return_value={
            "ChannelId": "rachel",
            "Title": "Show",
            "JellyfinItemId": "abc",
            "StartOffset": 12.5,
            "Duration": 100,
            "Mirror": True,
            "Paused": True,
        },
    ):
        slot = client.get_slot("rachel")
    assert slot.mirror is True and slot.paused is True


def test_schedule_client_defaults_for_normal_channels():
    client = ScheduleClient("http://x")
    with patch.object(
        client, "_get_json", return_value={"ChannelId": "c", "JellyfinItemId": "abc"}
    ):
        slot = client.get_slot("c")
    assert slot.mirror is False and slot.paused is False


def test_channel_list_carries_mirror_flag():
    client = MagicMock()
    client.get_channels.return_value = [
        ChannelInfo(id="a", name="A", number=1),
        ChannelInfo(id="rachel", name="Rachel", number=67, mirror=True),
    ]
    cl = ChannelList(client)
    assert cl.get_channel_by_id("rachel").mirror is True
    assert cl.get_channel_by_id("a").mirror is False


# --- player integration -------------------------------------------------------


def _player(channels=None):
    p = ClassicTVPlayer()
    p._jellyfin_server_id = "srv"
    chans = channels or [
        Channel("sitcoms", "Sitcoms", 15),
        Channel("rachel", "Rachel's Channel", 67, mirror=True),
    ]
    cl = MagicMock()
    cl.get_channel_by_id.side_effect = lambda cid: next((c for c in chans if c.id == cid), None)
    cl.all_channels.return_value = chans
    p._channel_list = cl
    p._schedule_client = MagicMock()
    p._mirror = MagicMock()
    return p


@pytest.fixture
def notes(monkeypatch):
    out = []

    class _D:
        def notification(self, heading, message, *a):
            out.append(message)

    monkeypatch.setattr(xbmcgui, "Dialog", _D)
    return out


def test_idle_mirror_channel_does_not_interrupt_current_playback(notes):
    p = _player()
    p._current_channel_id = "sitcoms"
    p._schedule_client.get_slot.return_value = _slot(item="")
    p._playing = True
    p._change_channel("rachel")
    assert p._playing is True  # never stopped
    assert notes == ["Rachel's Channel is offline"]
    p._mirror.start.assert_not_called()


def test_tuning_an_active_mirror_starts_ahead_and_syncs():
    p = _player()
    p._current_channel_id = "sitcoms"
    p._schedule_client.get_slot.return_value = _slot(offset=100)
    p._last_launch = 5.0
    with patch.object(p, "play_slot") as play:
        p._change_channel("rachel")
    played = play.call_args.args[0]
    assert played.start_offset == pytest.approx(105.0)
    p._mirror.start.assert_called_once_with("rachel")
    assert p._pre_mirror_channel_id == "sitcoms"


def test_tuning_a_normal_channel_stops_mirror_sync():
    p = _player()
    p._current_channel_id = "rachel"
    p._schedule_client.get_slot.return_value = _slot(item="x", channel="sitcoms")
    with patch.object(p, "play_slot"):
        p._change_channel("sitcoms")
    p._mirror.stop.assert_called()
    p._mirror.start.assert_not_called()


def test_browse_stops_mirror_sync():
    p = _player()
    p.suspend_for_browse("test")
    p._mirror.stop.assert_called()


def test_watchdog_recovery_is_skipped_on_mirror_channel():
    p = _player()
    p._current_channel_id = "rachel"
    p.watchdog_recover()
    p._schedule_client.get_slot.assert_not_called()


def test_leave_mirror_returns_to_previous_channel(notes):
    p = _player()
    p._pre_mirror_channel_id = "sitcoms"
    with patch.object(p, "_change_channel") as change:
        p.leave_mirror("rachel")
    change.assert_called_once_with("sitcoms")
    assert notes == ["Rachel's Channel signed off"]


def test_leave_mirror_without_a_previous_channel_uses_first_normal_channel(notes):
    p = _player()
    with patch.object(p, "_change_channel") as change:
        p.leave_mirror("rachel")
    change.assert_called_once_with("sitcoms")


def test_mirror_retune_stops_then_plays():
    p = _player()
    p._playing = True
    with patch.object(p, "play_slot") as play:
        p.mirror_retune(_slot())
    assert p._playing is False
    play.assert_called_once()


# --- the sync loop --------------------------------------------------------------


def _loop_player(slots, playing_item="item-a", playing=True, time_pos=100.0):
    pl = MagicMock()
    pl.is_user_browsing.return_value = False
    pl.is_in_grace_period.return_value = False
    pl.isPlaying.return_value = playing
    pl.getTime.return_value = time_pos
    pl._last_played_item_id = playing_item
    pl.last_launch_seconds.return_value = 5.0
    pl._schedule_client.get_slot.side_effect = list(slots) + [slots[-1]] * 50
    return pl


def _run_loop(pl, seconds=0.5):
    sync = MirrorSync(pl, interval=0.02)
    sync.start("rachel")
    time.sleep(seconds)
    sync.stop()
    return sync


def test_loop_retunes_when_item_changes():
    pl = _loop_player([_slot(item="item-b", offset=50)])
    _run_loop(pl)
    assert pl.mirror_retune.called
    assert pl.mirror_retune.call_args.args[0].start_offset == pytest.approx(55.0)


def test_loop_leaves_after_consecutive_idle_polls():
    pl = _loop_player([_slot(item="")])
    _run_loop(pl)
    pl.leave_mirror.assert_called_with("rachel")


def test_loop_tolerates_a_brief_gap_between_episodes():
    pl = _loop_player([_slot(item=""), _slot(item=""), _slot(item="item-a", offset=100)])
    _run_loop(pl)
    pl.leave_mirror.assert_not_called()


def test_loop_does_nothing_while_in_step():
    pl = _loop_player([_slot(item="item-a", offset=102)])
    _run_loop(pl)
    pl.mirror_retune.assert_not_called()
    pl.leave_mirror.assert_not_called()


def test_loop_survives_exceptions_and_stops_cleanly():
    pl = _loop_player([_slot()])
    pl._schedule_client.get_slot.side_effect = RuntimeError("boom")
    sync = _run_loop(pl)
    assert sync.running is False


# --- ch+/- skips idle mirror channels --------------------------------------------


def _step_player(idle_ids):
    chans = [
        Channel("a", "A", 1),
        Channel("rachel", "Rachel", 67, mirror=True),
        Channel("b", "B", 68),
    ]
    client = MagicMock()
    client.get_channels.return_value = [
        ChannelInfo(id=c.id, name=c.name, number=c.number, mirror=c.mirror) for c in chans
    ]
    p = ClassicTVPlayer()
    p._channel_list = ChannelList(client)
    p._schedule_client = MagicMock()
    p._schedule_client.get_slot.side_effect = lambda cid, timeout=5.0: _slot(
        item="" if cid in idle_ids else "x", channel=cid
    )
    p._mirror = MagicMock()
    return p


def test_ch_up_skips_an_idle_mirror_channel():
    p = _step_player({"rachel"})
    p._current_channel_id = "a"
    with patch.object(p, "_change_channel") as change:
        p.handle_ch_up()
    change.assert_called_once_with("b")


def test_ch_down_skips_an_idle_mirror_channel():
    p = _step_player({"rachel"})
    p._current_channel_id = "b"
    with patch.object(p, "_change_channel") as change:
        p.handle_ch_down()
    change.assert_called_once_with("a")


def test_ch_up_tunes_an_active_mirror_channel():
    p = _step_player(set())
    p._current_channel_id = "a"
    with patch.object(p, "_change_channel") as change:
        p.handle_ch_up()
    change.assert_called_once_with("rachel")


def test_unreachable_server_does_not_skip_the_mirror_channel():
    p = _step_player(set())
    p._schedule_client.get_slot.side_effect = None
    p._schedule_client.get_slot.return_value = None
    p._current_channel_id = "a"
    with patch.object(p, "_change_channel") as change:
        p.handle_ch_up()
    change.assert_called_once_with("rachel")


def test_only_idle_mirror_besides_current_means_no_tune():
    chans = [Channel("a", "A", 1), Channel("rachel", "Rachel", 67, mirror=True)]
    client = MagicMock()
    client.get_channels.return_value = [
        ChannelInfo(id=c.id, name=c.name, number=c.number, mirror=c.mirror) for c in chans
    ]
    p = ClassicTVPlayer()
    p._channel_list = ChannelList(client)
    p._schedule_client = MagicMock()
    p._schedule_client.get_slot.return_value = _slot(item="")
    p._current_channel_id = "a"
    with patch.object(p, "_change_channel") as change:
        p.handle_ch_up()
    change.assert_not_called()
