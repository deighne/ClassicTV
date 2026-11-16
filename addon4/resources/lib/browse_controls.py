"""
Playback controls for non-Classic-TV (browse mode) video.

The addon keymap claims OK, the d-pad and Back for every fullscreen video so
they can drive Classic TV channel entry. In browse mode that left the viewer
unable to pause, seek or toggle subtitles. While browsing, those keys are
handed here instead and mapped to the usual transport controls:

    OK     pause / resume
    Left   step back        (Kodi's small seek step)
    Right  step forward
    Up     show the on-screen display (progress, audio, subtitle buttons)
    Down   subtitles: next stream, then off; says so if the file has none

Classic TV mode is untouched: there these keys keep their TV meaning.
"""

from __future__ import annotations

import json

import xbmc
import xbmcgui

_LOG_PREFIX = "[ClassicTV][controls]"

# Keys the keymap routes to the service that mean something here.
BROWSE_KEYS = ("confirm", "up", "down", "left", "right")


def _rpc(method: str, params: dict | None = None) -> dict | None:
    """Call Kodi JSON-RPC; return the ``result`` dict or None on any failure."""
    try:
        raw = xbmc.executeJSONRPC(
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
            )
        )
        data = json.loads(raw)
    except Exception as exc:
        xbmc.log(f"{_LOG_PREFIX} rpc {method} failed: {exc}", xbmc.LOGDEBUG)
        return None
    if not isinstance(data, dict) or "error" in data:
        return None
    result = data.get("result")
    return result if isinstance(result, (dict, list)) else None


def _video_player_id() -> int | None:
    players = _rpc("Player.GetActivePlayers")
    if not isinstance(players, list):
        return None
    for p in players:
        if p.get("type") == "video":
            return p.get("playerid")
    return None


def subtitle_state(player_id: int) -> dict:
    """Subtitle properties for the active video, or {} if unavailable."""
    result = _rpc(
        "Player.GetProperties",
        {
            "playerid": player_id,
            "properties": ["subtitleenabled", "currentsubtitle", "subtitles"],
        },
    )
    return result if isinstance(result, dict) else {}


def _stream_label(stream: dict) -> str:
    name = stream.get("name") or ""
    lang = stream.get("language") or ""
    return " ".join(x for x in (lang, name) if x) or f"track {stream.get('index', 0) + 1}"


def describe_streams() -> str:
    """One-line summary of subtitle availability for the log (diagnostics)."""
    pid = _video_player_id()
    if pid is None:
        return "no video player"
    st = subtitle_state(pid)
    subs = st.get("subtitles") or []
    return (
        f"subtitle_streams={len(subs)} enabled={st.get('subtitleenabled')}"
        f" current={st.get('currentsubtitle', {}).get('index')}"
        f" available={[_stream_label(s) for s in subs]}"
    )


def _toast(message: str) -> None:
    try:
        xbmcgui.Dialog().notification(
            "Classic TV", message, xbmcgui.NOTIFICATION_INFO, 2000, False
        )
    except Exception:
        pass


class BrowseControls:
    """Translate browse-mode remote keys into transport and subtitle actions."""

    def handle(self, method: str) -> bool:
        """Run the control for *method*. Returns True if it was handled."""
        xbmc.log(f"{_LOG_PREFIX} browse key {method}", xbmc.LOGINFO)
        if method == "confirm":
            xbmc.executebuiltin("Action(PlayPause)")
        elif method == "left":
            xbmc.executebuiltin("Action(StepBack)")
        elif method == "right":
            xbmc.executebuiltin("Action(StepForward)")
        elif method == "up":
            xbmc.executebuiltin("Action(OSD)")
        elif method == "down":
            self.cycle_subtitles()
        else:
            return False
        return True

    def cycle_subtitles(self) -> None:
        """Next subtitle stream; after the last, off. Tells the viewer what happened."""
        pid = _video_player_id()
        if pid is None:
            return
        st = subtitle_state(pid)
        subs = st.get("subtitles") or []
        if not subs:
            _toast("No subtitles in this file")
            return
        enabled = bool(st.get("subtitleenabled"))
        current = (st.get("currentsubtitle") or {}).get("index", -1)
        if enabled and current >= len(subs) - 1:
            _rpc("Player.SetSubtitle", {"playerid": pid, "subtitle": "off"})
            _toast("Subtitles off")
            return
        target = current + 1 if enabled and current >= 0 else 0
        _rpc(
            "Player.SetSubtitle",
            {"playerid": pid, "subtitle": target, "enable": True},
        )
        _toast(f"Subtitles: {_stream_label(subs[target])}")
