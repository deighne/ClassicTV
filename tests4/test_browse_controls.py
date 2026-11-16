"""Tests for browse_controls.py and its routing in the service monitor."""

import json
import queue
from unittest.mock import MagicMock

import pytest
import xbmc

import browse_controls as bc


class _Rpc:
    """Fake Kodi JSON-RPC that records calls and serves canned results."""

    def __init__(self, props=None, players=None):
        self.calls = []
        self.props = props if props is not None else {}
        self.players = players if players is not None else [{"playerid": 1, "type": "video"}]

    def __call__(self, raw):
        req = json.loads(raw)
        self.calls.append((req["method"], req["params"]))
        if req["method"] == "Player.GetActivePlayers":
            return json.dumps({"id": 1, "result": self.players})
        if req["method"] == "Player.GetProperties":
            return json.dumps({"id": 1, "result": self.props})
        return json.dumps({"id": 1, "result": "OK"})

    def set_calls(self):
        return [p for m, p in self.calls if m == "Player.SetSubtitle"]


@pytest.fixture
def toasts(monkeypatch):
    out = []

    class _D:
        def notification(self, heading, message, *a):
            out.append(message)

    monkeypatch.setattr(bc.xbmcgui, "Dialog", _D)
    return out


def _use(monkeypatch, rpc):
    monkeypatch.setattr(xbmc, "executeJSONRPC", rpc)
    xbmc.executebuiltin.reset_mock()
    return rpc


def _builtins():
    return [c.args[0] for c in xbmc.executebuiltin.call_args_list]


@pytest.mark.parametrize(
    "key,builtin",
    [
        ("confirm", "Action(PlayPause)"),
        ("left", "Action(StepBack)"),
        ("right", "Action(StepForward)"),
        ("up", "Action(OSD)"),
    ],
)
def test_transport_keys(monkeypatch, key, builtin):
    _use(monkeypatch, _Rpc())
    assert bc.BrowseControls().handle(key) is True
    assert _builtins() == [builtin]


def test_unknown_key_not_handled(monkeypatch):
    _use(monkeypatch, _Rpc())
    assert bc.BrowseControls().handle("guide") is False


def test_no_subtitles_says_so_and_changes_nothing(monkeypatch, toasts):
    rpc = _use(monkeypatch, _Rpc(props={"subtitles": [], "subtitleenabled": False}))
    bc.BrowseControls().handle("down")
    assert toasts == ["No subtitles in this file"]
    assert rpc.set_calls() == []


def test_enables_first_stream_when_off(monkeypatch, toasts):
    rpc = _use(
        monkeypatch,
        _Rpc(
            props={
                "subtitles": [{"index": 0, "language": "eng", "name": "English"}],
                "subtitleenabled": False,
                "currentsubtitle": {},
            }
        ),
    )
    bc.BrowseControls().handle("down")
    assert rpc.set_calls() == [{"playerid": 1, "subtitle": 0, "enable": True}]
    assert toasts == ["Subtitles: eng English"]


def test_advances_to_next_stream(monkeypatch, toasts):
    subs = [{"index": 0, "language": "eng"}, {"index": 1, "language": "jpn"}]
    rpc = _use(
        monkeypatch,
        _Rpc(props={"subtitles": subs, "subtitleenabled": True, "currentsubtitle": {"index": 0}}),
    )
    bc.BrowseControls().handle("down")
    assert rpc.set_calls() == [{"playerid": 1, "subtitle": 1, "enable": True}]
    assert toasts == ["Subtitles: jpn"]


def test_turns_off_after_last_stream(monkeypatch, toasts):
    subs = [{"index": 0, "language": "eng"}]
    rpc = _use(
        monkeypatch,
        _Rpc(props={"subtitles": subs, "subtitleenabled": True, "currentsubtitle": {"index": 0}}),
    )
    bc.BrowseControls().handle("down")
    assert rpc.set_calls() == [{"playerid": 1, "subtitle": "off"}]
    assert toasts == ["Subtitles off"]


def test_no_video_player_is_a_noop(monkeypatch, toasts):
    rpc = _use(monkeypatch, _Rpc(players=[]))
    bc.BrowseControls().handle("down")
    assert toasts == [] and rpc.set_calls() == []


def test_rpc_failure_never_raises(monkeypatch, toasts):
    monkeypatch.setattr(xbmc, "executeJSONRPC", lambda raw: "not json")
    bc.BrowseControls().handle("down")
    assert bc.describe_streams() == "no video player"


def test_describe_streams_reports_availability(monkeypatch):
    _use(
        monkeypatch,
        _Rpc(props={"subtitles": [{"index": 0, "language": "eng"}], "subtitleenabled": False}),
    )
    text = bc.describe_streams()
    assert "subtitle_streams=1" in text and "enabled=False" in text


# --- routing in the service monitor -----------------------------------------


def _monitor(browsing):
    from service import ClassicTVMonitor

    player = MagicMock()
    player.is_user_browsing.return_value = browsing
    entry = MagicMock()
    entry.is_accumulating.return_value = False
    controls = MagicMock()
    controls.handle.return_value = True
    m = ClassicTVMonitor(player, entry, queue.Queue(maxsize=1), controls=controls)
    return m, player, entry, controls


@pytest.mark.parametrize("key", ["confirm", "up", "down", "left", "right"])
def test_browse_keys_go_to_controls_when_browsing(key):
    m, player, entry, controls = _monitor(browsing=True)
    m.onNotification("classictv.service", key, "null")
    controls.handle.assert_called_once_with(key)
    entry.handle_confirm.assert_not_called()


def test_confirm_still_confirms_channel_entry_in_tv_mode():
    m, player, entry, controls = _monitor(browsing=False)
    m.onNotification("classictv.service", "confirm", "null")
    controls.handle.assert_not_called()
    entry.handle_confirm.assert_called_once()


def test_dpad_ignored_in_tv_mode():
    m, player, entry, controls = _monitor(browsing=False)
    m.onNotification("classictv.service", "left", "null")
    controls.handle.assert_not_called()
