"""
Classic TV 4.0 — channel change and EOF recovery.

Classic TV 4.0 subclasses xbmc.Player but does NOT call play() or seekTime()
directly. All playback is delegated to the Jellyfin Kodi addon via:

    xbmc.executebuiltin("PlayMedia(plugin://plugin.video.jellyfin/
        ?mode=play&id=ITEM_ID&timestamp=OFFSET)")

This eliminates the multiple-players problem. The Jellyfin addon owns the
xbmc.Player instance, handles StartOffset, session reporting, and
audio/subtitle selection. Classic TV handles only channel navigation and
overlay UI.

Subclassing xbmc.Player gives Classic TV access to onPlayBack* callbacks
for any playback in Kodi, regardless of which addon initiated it. These
callbacks drive the mode state machine (modes.py): a viewer stop or foreign
playback enters browse mode; a natural end of browse playback starts the
resume countdown.

EOF recovery: the watchdog (polling isPlaying() every 10s) is the primary
recovery mechanism in TV mode.
"""

from __future__ import annotations

import json
import threading
import time

import xbmc
import xbmcaddon
import xbmcgui

from constants import (
    CHANNEL_CHANGE_FETCH_TIMEOUT_SECONDS,
    CHANNEL_DISAPPEARED_CHECK_INTERVAL,
    CHANNEL_STATIC_MIN_SECONDS,
    CHANNEL_STATIC_POST_AV_SECONDS,
    DECODER_RELEASE_POLL_INTERVAL_MS,
    DECODER_RELEASE_POLL_TIMEOUT_SECONDS,
    DIRECT_HTTP_PLAYBACK,
    EOF_RETRY_MAX,
    STOP_BEFORE_PLAY,
    EOF_RETRY_INTERVAL_SECONDS,
    EOF_OFFLINE_RETRY_INTERVAL_SECONDS,
    JELLYFIN_READY_TIMEOUT_SECONDS,
    JELLYFIN_READY_POLL_SECONDS,
    JELLYFIN_READY_NOTIFY_SECONDS,
    JELLYFIN_RELOAD_COOLDOWN_SECONDS,
    JELLYFIN_RELOAD_WAIT_SECONDS,
    PLAY_GRACE_SECONDS,
    PLAYBACK_CONFIRM_TIMEOUT_SECONDS,
    STARTUP_RETRY_MAX,
    SUBTITLE_STATE_LOG_DELAY_SECONDS,
    STARTUP_RETRY_INTERVAL_SECONDS,
    STOP_SOURCE_GUARD_SECONDS,
    WEDGE_FAILURE_THRESHOLD,
)
from browse_controls import describe_streams
from mirror import MirrorSync, lead_seconds, with_lead
from modes import TV_MODE
from schedule import ScheduleClient, ScheduleSlot

_LOG_PREFIX = "[ClassicTV][player]"


def _jsonrpc_failed(result: str) -> bool:
    """Return True if the JSON-RPC response indicates an error.

    Parses the JSON-RPC response string and returns True if:
    - JSON decode fails (malformed response)
    - an ``"error"`` key is present in the response object
    - ``data.get("result")`` is None (no result field)

    This avoids silently continuing after a failed RPC request.
    """
    try:
        data = json.loads(result)
        return "error" in data or data.get("result") is None
    except (json.JSONDecodeError, AttributeError):
        return True


class ClassicTVPlayer(xbmc.Player):
    """Minimal xbmc.Player subclass.

    Does not call play() or seekTime(). Holds state needed by the watchdog
    and channel change sequence: current channel ID and the Jellyfin server
    ID read at startup.
    """

    def __init__(self) -> None:
        super().__init__()
        self._current_channel_id: str = ""
        self._persisted_channel_id: str = ""
        self._base_url: str = "http://192.168.1.207:8096"  # set externally by service.py
        self._jellyfin_server_id: str = ""  # set externally by service.py
        self._channel_list = None  # ChannelList, set externally
        self._modes = None  # ModeState, set externally
        self._schedule_client: ScheduleClient | None = None  # set externally
        self._channel_bug = None  # ChannelBug, set externally
        # Timestamp of the last play_slot() call (monotonic seconds).
        # The watchdog skips isPlaying() checks for PLAY_GRACE_SECONDS after
        # a play command — PlayMedia() returns immediately and Kodi needs time
        # to route the command to the Jellyfin addon and start the stream.
        self._last_play_time: float = 0.0
        # Cancellation event for the offline retry worker (1.2).
        # Starts set (no retry running). Set again by play_slot() to cancel
        # any in-flight retry.
        self._retry_stop: threading.Event = threading.Event()
        self._retry_stop.set()
        # Boot resilience: playback confirmation event.
        # Cleared by play_slot() after executebuiltin(), set by onPlayBackStarted().
        # The watchdog and _reload_jellyfin_addon() wait on this to confirm the
        # Jellyfin addon successfully started the stream.
        self._play_confirm_event: threading.Event = threading.Event()
        # Monotonic timestamp of the last _reload_jellyfin_addon() call.
        # 0.0 means no reload has occurred this session.
        self._last_reload_time: float = 0.0
        # Count of consecutive watchdog recoveries where the server was reachable
        # but onPlayBackStarted() never fired within PLAYBACK_CONFIRM_TIMEOUT_SECONDS.
        # Reset to 0 by onPlayBackStarted(). Triggers a Jellyfin addon reload at
        # WEDGE_FAILURE_THRESHOLD.
        self._consecutive_failures: int = 0
        # User-browse suspension: True when the viewer has stopped Classic TV
        # playback to browse Jellyfin/Kodi. The watchdog ignores stopped playback
        # while this is set, so it no longer yanks the viewer back to a channel.
        # Cleared whenever Classic TV tunes (play_slot) or the EPG toggle resumes.
        self._browse_suspend: bool = False
        # Timestamps used to classify onPlayBackStopped: a stop caused by
        # Classic TV's own channel change, or by a natural episode EOF, must not
        # be mistaken for the viewer leaving.
        self._internal_stop_time: float = 0.0
        self._eof_time: float = 0.0
        # Jellyfin item id of the last play_slot(); compared with the Jellyfin
        # addon's jellyfin_playing_id window property to tell Classic TV's own
        # playback from something the viewer started.
        self._last_played_item_id: str = ""
        self._last_played_url: str = ""
        self._last_played_listitem = None
        self._last_slot: ScheduleSlot | None = None
        # Maps jellyfin_item_id -> lowest start_offset that hit premature EOF
        # (truncated/corrupted container on disk).
        self._premature_eof_items: dict[str, float] = {}
        self._play_seq: int = 0
        self._tune_seq: int = 0
        # True from play_slot() until the first onPlayBackStarted after it. That
        # first start is ours by definition, however long the Jellyfin addon took.
        self._awaiting_start: bool = False
        # True when onAVStarted() is allowed to dismiss CRT static for the active tune.
        # Cleared between show_static() and play_slot() so a stray callback from the
        # outgoing stream cannot prematurely dismiss the new tune's static.
        self._awaiting_av: bool = True
        # True once onAVStarted() has fired for the current play_slot() tune.
        self._av_started: bool = False
        # Monotonic timestamps for the tune in flight, logged as one "tune
        # timing" line when playback starts. See docs/channel-switching-performance.md.
        self._tune_marks: dict[str, float] = {}
        # PlayMedia-to-playing time of the last tune; mirror channels start this
        # far ahead of the mirrored position to cover stream start-up.
        self._last_launch: float | None = None
        # Keeps a tuned mirror channel in step with the mirrored user.
        self._mirror = MirrorSync(self)
        # Last non-mirror channel, so leaving a mirror channel has somewhere to go.
        self._pre_mirror_channel_id: str = ""

    # ------------------------------------------------------------------
    # Direct stream playback — fast HTTP byte-range + StartOffset
    # ------------------------------------------------------------------

    def _build_stream_url(self, item_id: str) -> str:
        """Build the direct Jellyfin HTTP byte-range video stream URL for *item_id*."""
        base = self._base_url
        if self._schedule_client is not None:
            client_base = getattr(self._schedule_client, "_base_url", None)
            if isinstance(client_base, str) and client_base:
                base = client_base
        base_clean = base.rstrip("/")
        if item_id.startswith("guide-"):
            return f"{base_clean}/ClassicTV/guide/stream/{item_id}"
        return f"{base_clean}/Videos/{item_id}/stream?static=true"

    def play_slot(self, slot: ScheduleSlot) -> None:
        """Start playback of a schedule slot at its mid-episode StartOffset.

        When ``DIRECT_HTTP_PLAYBACK`` is True (default), plays the Jellyfin HTTP
        byte-range stream (``/Videos/{id}/stream?static=true``) directly via
        ``self.play(url, listitem)`` with ``StartOffset`` set on the ``ListItem``.
        Kodi's demuxer seeks synchronously before the first frame is decoded,
        avoiding both ``seekTime()`` and the 3.5–7.5 s ``CPythonInvoker`` cold-start
        penalty of spawning ``plugin.video.jellyfin`` on every tune.

        Because ``Window(Home).Property(jellyfin_playing_id)`` is cleared before
        direct play, ``plugin.video.jellyfin`` ignores Classic TV's playback
        completely — no multiple-player conflict, no overnight Borg state wedge,
        and no polluting the user's "Continue Watching" row.
        """
        item_id = slot.jellyfin_item_id
        cutoff = self._premature_eof_items.get(item_id)
        if (
            cutoff is not None
            and slot.start_offset >= cutoff
            and slot.next_item is not None
            and slot.next_item.jellyfin_item_id
        ):
            xbmc.log(
                f"{_LOG_PREFIX} skipping premature-EOF item {item_id!r}"
                f" (truncated at <={cutoff:.1f}s, requested {slot.start_offset:.1f}s)"
                f" — advancing to NextItem {slot.next_item.jellyfin_item_id!r}",
                xbmc.LOGWARNING,
            )
            slot = ScheduleSlot(
                channel_id=slot.channel_id,
                title=slot.next_item.title or slot.title,
                jellyfin_item_id=slot.next_item.jellyfin_item_id,
                start_offset=0.001,
                duration=slot.next_item.duration,
                next_item=None,
            )
            item_id = slot.jellyfin_item_id

        self._last_slot = slot

        # Guard: StartOffset must be < Duration.
        # If the cache was built with fallback durations (1800s) and the real
        # episode is shorter, the offset may exceed the actual runtime causing
        # Jellyfin to end playback immediately. Cap to 90% of duration.
        start_offset = slot.start_offset
        if slot.duration > 0 and start_offset >= slot.duration:
            xbmc.log(
                f"{_LOG_PREFIX} clamping offset {start_offset:.1f}s"
                f" (duration={slot.duration:.1f}s) channel={slot.channel_id!r}",
                xbmc.LOGWARNING,
            )
            start_offset = slot.duration * 0.9

        offset = max(start_offset, 0.001)  # avoid 0.0 edge case

        self._play_seq += 1
        stream_url = self._build_stream_url(item_id)

        xbmc.log(
            f"{_LOG_PREFIX} play_slot"
            f" channel={slot.channel_id!r}"
            f" title={slot.title!r}"
            f" item={item_id!r}"
            f" offset={offset:.3f}s"
            f" direct={DIRECT_HTTP_PLAYBACK}",
            xbmc.LOGINFO,
        )

        self._current_channel_id = slot.channel_id
        self._persist_last_channel(slot.channel_id)
        self._last_played_item_id = item_id
        self._last_play_time = time.monotonic()
        self._internal_stop_time = self._last_play_time
        # A Classic TV tune always ends browse suspension.
        if self._browse_suspend:
            xbmc.log(
                f"{_LOG_PREFIX} play_slot: clearing browse suspension",
                xbmc.LOGINFO,
            )
        self._browse_suspend = False
        # Whatever path got us here (watchdog, offline retry, startup), the
        # mode machine must be back in TV_MODE with its watchdog running.
        if self._modes is not None and self._modes.current != TV_MODE:
            self._modes.enter_tv(slot.channel_id)
        # Cancel any in-flight offline retry — a successful play_slot() means
        # the service is back and we don't need the retry loop anymore.
        self._retry_stop.set()
        self._mark("play")
        self._awaiting_start = True
        self._awaiting_av = True
        self._av_started = False
        self._play_confirm_event.clear()

        try:
            w = xbmcgui.Window(10000)
            w.setProperty("ClassicTV.NowPlaying", slot.title or "")
            if self._channel_list is not None:
                ch = self._channel_list.get_channel_by_id(slot.channel_id)
                if ch is not None:
                    num_str = str(ch.number) if ch.number > 0 else "CH"
                    w.setProperty("ClassicTV.ActiveChannelNumber", num_str)
                    w.setProperty("ClassicTV.ActiveChannelName", ch.name)
        except Exception:
            pass

        if DIRECT_HTTP_PLAYBACK:
            # Clear jellyfin_playing_id so plugin.video.jellyfin's Player monitor
            # does not claim Classic TV's direct stream or report it to the user's
            # Jellyfin profile.
            try:
                xbmcgui.Window(10000).setProperty("jellyfin_playing_id", "")
            except Exception:
                pass

            listitem = xbmcgui.ListItem(label=slot.title, path=stream_url)
            listitem.setProperty("IsPlayable", "true")
            listitem.setProperty("IsFolder", "false")
            listitem.setProperty("StartOffset", f"{offset:.3f}")
            if hasattr(listitem, "setContentLookup"):
                listitem.setContentLookup(False)
            if hasattr(listitem, "getVideoInfoTag"):
                try:
                    tag = listitem.getVideoInfoTag()
                    tag.setTitle(slot.title)
                    if hasattr(tag, "setMediaType"):
                        tag.setMediaType("episode")
                    if slot.duration > 0:
                        tag.setResumePoint(offset, float(slot.duration))
                except Exception:
                    pass
            else:
                listitem.setInfo("video", {"title": slot.title, "mediatype": "episode"})

            self._last_played_url = stream_url
            self._last_played_listitem = listitem
            self.play(stream_url, listitem)
        else:
            play_url = (
                f"plugin://plugin.video.jellyfin/"
                f"?mode=play"
                f"&id={item_id}"
                f"&timestamp={offset:.3f}"
                f"&nonce={int(time.time())}-{self._play_seq}"
            )
            self._last_played_url = play_url
            self._last_played_listitem = None
            xbmc.executebuiltin(f"PlayMedia({play_url})")

    def _mark(self, name: str) -> None:
        self._tune_marks[name] = time.monotonic()

    def _log_tune_timing(self) -> None:
        """Log the phase breakdown of the tune that just started, then reset.

        Phases: stop (stop + decoder release), fetch (slot request), launch
        (PlayMedia issued to onPlayBackStarted, i.e. Jellyfin addon start-up and
        stream open). Tunes that skip a phase (watchdog replay, startup) omit it.
        """
        marks, self._tune_marks = self._tune_marks, {}
        if "play" not in marks:
            return
        started = time.monotonic()
        parts = []
        prev = marks.get("key")
        for label, name in (("stop", "stopped"), ("fetch", "fetched")):
            if name in marks and prev is not None:
                parts.append(f"{label}={marks[name] - prev:.2f}s")
                prev = marks[name]
        self._last_launch = started - marks["play"]
        parts.append(f"launch={started - marks['play']:.2f}s")
        parts.append(f"total={started - marks.get('key', marks['play']):.2f}s")
        xbmc.log(
            f"{_LOG_PREFIX} tune timing channel={self._current_channel_id!r} "
            + " ".join(parts),
            xbmc.LOGINFO,
        )

    def last_launch_seconds(self) -> float | None:
        """PlayMedia-to-playing time of the last tune, or None if not measured."""
        return self._last_launch

    def _log_stream_state_later(self, own: bool) -> None:
        """Log subtitle availability shortly after any playback starts.

        Kodi has not populated stream properties at onPlayBackStarted, so look a
        moment later on a worker thread. Answers "why did this file (not) get
        subtitles" from the log: no streams, streams but none enabled, etc.
        """

        # _last_played_item_id is Classic TV's last item; it only describes this
        # playback when the viewer did not start something else.
        def _run() -> None:
            try:
                xbmc.log(
                    f"{_LOG_PREFIX} streams {describe_streams()}"
                    f" source={'classictv' if own else 'browse'}"
                    + (f" item={self._last_played_item_id!r}" if own else ""),
                    xbmc.LOGINFO,
                )
            except Exception as exc:
                xbmc.log(f"{_LOG_PREFIX} stream state failed: {exc}", xbmc.LOGDEBUG)

        t = threading.Timer(SUBTITLE_STATE_LOG_DELAY_SECONDS, _run)
        t.daemon = True
        t.start()

    def is_in_grace_period(self) -> bool:
        """Return True if a play command was issued recently enough that the
        watchdog should not check isPlaying() yet.

        PlayMedia() returns immediately — the Jellyfin addon starts the stream
        asynchronously. Checking isPlaying() too soon always returns False and
        causes the watchdog to loop. Once onPlayBackEnded fires for that play,
        the stream has already finished and the grace period ends immediately.
        """
        if self._eof_time >= self._last_play_time > 0.0:
            return False
        return time.monotonic() - self._last_play_time < PLAY_GRACE_SECONDS

    def is_av_stalled(self) -> bool:
        """True if playback is active past the grace period without AV rendering.

        Catches streams where onPlayBackStarted() fired (so isPlaying() is True)
        but CFileCache::Seek or codec init stalled before onAVStarted() ever fired.
        """
        if self._last_play_time <= 0.0:
            return False
        if self.is_in_grace_period():
            return False
        if self._av_started:
            return False
        return self.isPlaying()

    def stop_for_recovery(self) -> None:
        """Stop a stalled stream internally so onPlayBackStopped does not enter BROWSE."""
        self._internal_stop_time = time.monotonic()
        if self.isPlaying():
            self.stop()
        deadline = time.time() + DECODER_RELEASE_POLL_TIMEOUT_SECONDS
        while self.isPlaying() and time.time() < deadline:
            xbmc.sleep(DECODER_RELEASE_POLL_INTERVAL_MS)

    # ------------------------------------------------------------------
    # Browse mode
    # ------------------------------------------------------------------

    def is_user_browsing(self) -> bool:
        """True while Classic TV does not own the player (BROWSE_MODE or
        RESUME_PENDING); the watchdog must not treat stopped playback as an
        unexpected stop and channel input is ignored."""
        if self._browse_suspend:
            return True
        return self._modes is not None and self._modes.current != TV_MODE

    def suspend_for_browse(self, reason: str = "viewer") -> None:
        """Enter browse mode — the viewer is browsing, not watching TV."""
        if not self._browse_suspend:
            xbmc.log(
                f"{_LOG_PREFIX} browse: watchdog suspended",
                xbmc.LOGINFO,
            )
        self._browse_suspend = True
        self._mirror.stop()
        try:
            w = xbmcgui.Window(10000)
            w.setProperty("ClassicTV.ActiveChannelNumber", "")
            w.setProperty("ClassicTV.ActiveChannelName", "")
        except Exception:
            pass
        if self._modes is not None:
            self._modes.enter_browse(reason)

    def enter_browse(self) -> None:
        """Suspend the watchdog, stop Classic TV playback, and step aside to Home.

        Waits for the Amlogic decoder to release before activating Home — the
        same guard the channel-change sequence uses. Jumping into a video window
        (e.g. the Jellyfin movies list) while the decoder is still tearing down
        has crashed/hung the device.
        """
        if self._channel_bug is not None and hasattr(self._channel_bug, "hide"):
            self._channel_bug.hide()
        self.suspend_for_browse("epg")
        self._internal_stop_time = time.monotonic()
        if self.isPlaying():
            self.stop()
            deadline = time.time() + DECODER_RELEASE_POLL_TIMEOUT_SECONDS
            while self.isPlaying() and time.time() < deadline:
                xbmc.sleep(DECODER_RELEASE_POLL_INTERVAL_MS)
            # Give the Amlogic hardware decoder driver a brief settling window
            # (350ms) to finish unmapping DMA / VPU video planes before the Mali
            # GPU flips to Home, avoiding a kernel VPU/framebuffer race condition.
            xbmc.sleep(350)
        xbmc.executebuiltin("ActivateWindow(Home)")

    def resume_from_browse(self) -> None:
        """Leave browse suspension and retune the current (or first) channel."""
        xbmc.log(f"{_LOG_PREFIX} browse: resuming Classic TV", xbmc.LOGINFO)
        self._browse_suspend = False
        channel_id = self._current_channel_id
        if not channel_id and self._channel_list is not None:
            channels = self._channel_list.all_channels()
            channel_id = channels[0].id if channels else ""
        if channel_id:
            self._change_channel(channel_id)

    # ------------------------------------------------------------------
    # Kodi player callbacks
    #
    # ClassicTVPlayer subclasses xbmc.Player, so Kodi delivers these callbacks
    # for ALL playback regardless of which addon initiated it. onPlayBackStarted
    # confirms a play command (boot resilience) and detects playback Classic TV
    # did not start; onPlayBackStopped/Ended drive browse mode and the resume
    # countdown. Paused/Resumed are log-only. on-device behaviour is recorded
    # in docs/tier-specs/tier-1-callback-findings.md.
    # ------------------------------------------------------------------

    def _is_own_playback(self, awaiting_start: bool = False) -> bool:
        """Classify a just-started stream as Classic TV's own or foreign.

        Conservative: the first start after our own play_slot() is ours, and so
        is anything inside the play grace window, and a start whose Jellyfin id
        matches the last slot or cannot be read. Only a readable, different id
        outside those counts as something the viewer started.

        The grace window alone is not enough: a slow start (30+ s, seen on
        2026-10-10) outlasts it, and the viewer was wrongly dropped into browse.
        """
        if awaiting_start or self.is_in_grace_period():
            return True
        playing_id = xbmc.getInfoLabel("Window(Home).Property(jellyfin_playing_id)")
        if not playing_id or not self._last_played_item_id:
            return True
        return playing_id == self._last_played_item_id

    def onAVStarted(self) -> None:  # noqa: N802
        """Called by Kodi when the first video frame is actually rendered."""
        xbmc.log(
            f"{_LOG_PREFIX} CALLBACK onAVStarted"
            f" channel={self._current_channel_id!r}",
            xbmc.LOGINFO,
        )
        if not self._awaiting_av:
            xbmc.log(
                f"{_LOG_PREFIX} onAVStarted: ignoring pre-play callback from previous stream",
                xbmc.LOGDEBUG,
            )
            return
        self._av_started = True
        if self._channel_bug is not None and hasattr(self._channel_bug, "hide_static"):
            self._channel_bug.hide_static()

    def onPlayBackStarted(self) -> None:  # noqa: N802
        xbmc.log(
            f"{_LOG_PREFIX} CALLBACK onPlayBackStarted"
            f" channel={self._current_channel_id!r}",
            xbmc.LOGINFO,
        )
        self._play_confirm_event.set()
        self._consecutive_failures = 0
        self._log_tune_timing()
        awaiting, self._awaiting_start = self._awaiting_start, False
        own = self._is_own_playback(awaiting_start=awaiting)
        self._log_stream_state_later(own)
        if self._modes is not None:
            if not own:
                xbmc.log(
                    f"{_LOG_PREFIX} onPlayBackStarted: foreign playback — browse mode",
                    xbmc.LOGINFO,
                )
                self._browse_suspend = True
            self._modes.on_playback_started(is_classictv_initiated=own)

    def onPlayBackEnded(self) -> None:  # noqa: N802
        # Natural episode EOF: the watchdog must still advance the channel, so
        # this is deliberately NOT treated as the viewer leaving. Record the time
        # so a following onPlayBackStopped (some Kodi builds fire both) is not
        # mistaken for a user stop either.
        self._eof_time = time.monotonic()
        xbmc.log(
            f"{_LOG_PREFIX} CALLBACK onPlayBackEnded"
            f" channel={self._current_channel_id!r}",
            xbmc.LOGINFO,
        )
        # Detect premature EOF caused by a truncated/corrupted container file
        # whose header duration exceeds the actual media data on disk.
        last_slot = self._last_slot
        if (
            not self.is_user_browsing()
            and last_slot is not None
            and self._last_play_time > 0.0
        ):
            played_for = self._eof_time - self._last_play_time
            expected_remaining = last_slot.duration - last_slot.start_offset
            if played_for < 20.0 and expected_remaining > 30.0:
                item_id = last_slot.jellyfin_item_id
                prev = self._premature_eof_items.get(item_id)
                self._premature_eof_items[item_id] = (
                    min(prev, last_slot.start_offset)
                    if prev is not None
                    else last_slot.start_offset
                )
                xbmc.log(
                    f"{_LOG_PREFIX} premature EOF after {played_for:.1f}s"
                    f" (expected {expected_remaining:.1f}s remaining)"
                    f" item={item_id!r} channel={self._current_channel_id!r}",
                    xbmc.LOGWARNING,
                )
                if (
                    last_slot.next_item is not None
                    and last_slot.next_item.jellyfin_item_id
                ):
                    self.play_slot(last_slot)
                    return

        # In browse mode this starts the countdown back to TV; in TV mode the
        # mode machine ignores it and the watchdog advances the episode.
        if self._modes is not None:
            self._modes.on_playback_ended()

    def onPlayBackStopped(self) -> None:  # noqa: N802
        now = time.monotonic()
        xbmc.log(
            f"{_LOG_PREFIX} CALLBACK onPlayBackStopped"
            f" channel={self._current_channel_id!r}",
            xbmc.LOGINFO,
        )

        # A stop caused by Classic TV's own channel change is not the viewer
        # leaving.
        if now - self._internal_stop_time < STOP_SOURCE_GUARD_SECONDS:
            xbmc.log(
                f"{_LOG_PREFIX} onPlayBackStopped: internal to a channel change — ignored",
                xbmc.LOGDEBUG,
            )
            return

        # A stop right after a natural EOF is the end of an episode, which the
        # watchdog must recover from — not a user stop.
        if now - self._eof_time < STOP_SOURCE_GUARD_SECONDS:
            xbmc.log(
                f"{_LOG_PREFIX} onPlayBackStopped: follows EOF — watchdog will recover",
                xbmc.LOGDEBUG,
            )
            return

        # Viewer-initiated stop (e.g. Back out of fullscreen to browse). A stop
        # in browse mode changes nothing: the viewer stays in control until they
        # press GUIDE or something finishes on its own.
        self.suspend_for_browse("viewer-stop")
        xbmc.log(
            f"{_LOG_PREFIX} viewer stopped playback — watchdog suspended"
            f" (press EPG to return to Classic TV)",
            xbmc.LOGINFO,
        )

    def onPlayBackPaused(self) -> None:  # noqa: N802
        xbmc.log(
            f"{_LOG_PREFIX} CALLBACK onPlayBackPaused"
            f" channel={self._current_channel_id!r}",
            xbmc.LOGINFO,
        )

    def onPlayBackResumed(self) -> None:  # noqa: N802
        xbmc.log(
            f"{_LOG_PREFIX} CALLBACK onPlayBackResumed"
            f" channel={self._current_channel_id!r}",
            xbmc.LOGINFO,
        )

    # ------------------------------------------------------------------
    # Channel navigation
    # ------------------------------------------------------------------

    def _step_channel(self, step):
        """Next channel in the direction of *step* (``next_channel`` or
        ``prev_channel``), skipping mirror channels with nobody to mirror.

        Without the skip, ch+/- onto an idle mirror channel refuses to tune and
        the viewer can never get past it. Digit entry still tunes to it directly
        (and gets the "is offline" message).
        """
        start = self._current_channel_id
        cur = start
        for _ in range(len(self._channel_list.all_channels()) or 1):
            ch = step(cur)
            if not ch or ch.id == start:
                return None
            if ch.mirror is True and self._mirror_is_idle(ch.id):
                xbmc.log(
                    f"{_LOG_PREFIX} skipping idle mirror channel {ch.id!r}",
                    xbmc.LOGINFO,
                )
                cur = ch.id
                continue
            return ch
        return None

    def _mirror_is_idle(self, channel_id: str) -> bool:
        """True only when the server says nobody is playing on this mirror channel."""
        if self._schedule_client is None:
            return False
        slot = self._schedule_client.get_slot(
            channel_id, timeout=CHANNEL_CHANGE_FETCH_TIMEOUT_SECONDS
        )
        return slot is not None and not slot.jellyfin_item_id

    def handle_ch_up(self) -> None:
        if self._channel_list is None or self.is_user_browsing():
            return
        next_ch = self._step_channel(self._channel_list.next_channel)
        if next_ch:
            xbmc.log(
                f"{_LOG_PREFIX} ch_up: {self._current_channel_id!r} → {next_ch.id!r}",
                xbmc.LOGINFO,
            )
            self._change_channel(next_ch.id)

    def handle_ch_down(self) -> None:
        if self._channel_list is None or self.is_user_browsing():
            return
        prev_ch = self._step_channel(self._channel_list.prev_channel)
        if prev_ch:
            xbmc.log(
                f"{_LOG_PREFIX} ch_down: {self._current_channel_id!r} → {prev_ch.id!r}",
                xbmc.LOGINFO,
            )
            self._change_channel(prev_ch.id)

    def _persist_last_channel(self, channel_id: str) -> None:
        """Persist the most recently opened channel ID across Kodi restarts."""
        if not channel_id or channel_id == self._persisted_channel_id:
            return
        self._persisted_channel_id = channel_id
        try:
            xbmcaddon.Addon().setSetting("last_channel", channel_id)
        except Exception as exc:
            xbmc.log(
                f"{_LOG_PREFIX} failed to persist last_channel={channel_id!r}: {exc}",
                xbmc.LOGWARNING,
            )

    def _change_channel(self, channel_id: str) -> None:
        """Execute the full channel change sequence.

        1. Stop current playback.
        2. Poll until the Amlogic decoder releases its lock (up to 2s).
        3. Fetch the new channel slot (3s timeout).
        4a. On success: enter TV mode, play_slot(), show the channel bug.
        4b. On failure: show a notification and resume the previous channel.
        """
        previous_channel_id = self._current_channel_id
        self._tune_seq += 1
        my_seq = self._tune_seq
        self._current_channel_id = channel_id
        self._tune_marks = {}
        self._mark("key")
        target = (
            self._channel_list.get_channel_by_id(channel_id)
            if self._channel_list is not None
            else None
        )

        xbmc.log(
            f"{_LOG_PREFIX} _change_channel: {previous_channel_id!r} → {channel_id!r}",
            xbmc.LOGINFO,
        )

        # A mirror channel with nobody to mirror has nothing to show. Find out
        # before touching playback, so the current channel is not interrupted.
        if target is not None and target.mirror is True and self._schedule_client is not None:
            probe = self._schedule_client.get_slot(
                channel_id, timeout=CHANNEL_CHANGE_FETCH_TIMEOUT_SECONDS
            )
            if probe is not None and not probe.jellyfin_item_id:
                if self._tune_seq == my_seq:
                    self._current_channel_id = previous_channel_id
                xbmc.log(
                    f"{_LOG_PREFIX} mirror channel {channel_id!r} is idle — not tuning",
                    xbmc.LOGINFO,
                )
                xbmcgui.Dialog().notification(
                    "Classic TV",
                    f"{target.name} is offline",
                    xbmcgui.NOTIFICATION_INFO,
                    3000,
                )
                return

        # Leaving (or re-entering) a channel ends any mirror sync for the old one.
        self._mirror.stop()

        # Step 1: stop current playback. Record that this stop is internal so
        # onPlayBackStopped does not mistake it for the viewer leaving.
        self._internal_stop_time = time.monotonic()
        if STOP_BEFORE_PLAY and self.isPlaying():
            self.stop()

        # Step 2: wait for Amlogic decoder to release.
        deadline = time.time() + DECODER_RELEASE_POLL_TIMEOUT_SECONDS
        while STOP_BEFORE_PLAY and self.isPlaying() and time.time() < deadline:
            xbmc.sleep(DECODER_RELEASE_POLL_INTERVAL_MS)
        if self.isPlaying():
            xbmc.log(
                f"{_LOG_PREFIX} decoder still held after"
                f" {DECODER_RELEASE_POLL_TIMEOUT_SECONDS}s — proceeding anyway",
                xbmc.LOGWARNING,
            )

        self._mark("stopped")

        # Show GPU-animated CRT static + destination channel badge immediately
        # on osd0 and wait ~2 frames (35 ms) so the GUI texture upload finishes
        # before self.play() opens DVDVideoCodecAmlogic on vd1.
        self._awaiting_av = False
        if (
            self._channel_bug is not None
            and target is not None
            and hasattr(self._channel_bug, "show_static")
        ):
            self._channel_bug.show_static(
                target.number,
                target.name,
                min_seconds=CHANNEL_STATIC_MIN_SECONDS,
                post_av_seconds=CHANNEL_STATIC_POST_AV_SECONDS,
            )
            xbmc.sleep(35)

        # Step 3: fetch new channel slot.
        slot = None
        if self._schedule_client is not None:
            slot = self._schedule_client.get_slot(
                channel_id,
                timeout=CHANNEL_CHANGE_FETCH_TIMEOUT_SECONDS,
            )

        if self._tune_seq != my_seq:
            xbmc.log(
                f"{_LOG_PREFIX} _change_channel: tune to {channel_id!r}"
                f" superseded by newer tune — skipping",
                xbmc.LOGINFO,
            )
            return

        self._mark("fetched")
        if slot is not None and slot.jellyfin_item_id:
            # Step 4a: success.
            if self._modes is not None:
                self._modes.transition_to_tv(channel_id)
            if target is not None and target.mirror is True:
                # Start ahead of their position to cover stream start-up.
                slot = with_lead(slot, lead_seconds(self._last_launch))
            self.play_slot(slot)
            if target is not None and target.mirror is True:
                if previous_channel_id and previous_channel_id != channel_id:
                    prev = (
                        self._channel_list.get_channel_by_id(previous_channel_id)
                        if self._channel_list is not None
                        else None
                    )
                    if prev is None or prev.mirror is not True:
                        self._pre_mirror_channel_id = previous_channel_id
                self._mirror.start(channel_id)
            if self._channel_bug is not None and target is not None:
                self._wait_before_overlay()
                if self._tune_seq == my_seq:
                    self._channel_bug.show_for(target.number, target.name)
        else:
            # Step 4b: failure — resume previous channel.
            self._current_channel_id = previous_channel_id
            self._awaiting_av = True
            if self._channel_bug is not None and hasattr(self._channel_bug, "hide_static"):
                self._channel_bug._static_min_seconds = 0.0
                self._channel_bug._static_post_av_seconds = 0.0
                self._channel_bug.hide_static()
            xbmc.log(
                f"{_LOG_PREFIX} channel change failed for {channel_id!r}"
                f" — resuming {previous_channel_id!r}",
                xbmc.LOGWARNING,
            )
            xbmcgui.Dialog().notification(
                "Classic TV",
                "Channel unavailable",
                xbmcgui.NOTIFICATION_WARNING,
                2000,
            )
            if previous_channel_id:
                self._resume_channel(previous_channel_id)

    def _wait_before_overlay(self) -> None:
        """Wait briefly for VideoPlayer::OpenFile / DVDVideoCodecAmlogic to
        finish opening before showing ChannelBug.xml on the GUI thread.

        On the Amlogic S905X, opening ChannelBug.xml at the exact same millisecond
        as VideoPlayer::OpenFile (0.17s after self.play()) collides osd0 GUI
        initialization with vd1 amstream codec init. Waiting until
        onPlayBackStarted (~0.29s) or 500ms prevents the collision.
        """
        for _ in range(10):
            if self._play_confirm_event.is_set():
                return
            xbmc.sleep(50)

    def _resume_channel(self, channel_id: str) -> None:
        """Fetch and play the current slot for a channel (used after failed change)."""
        if self._schedule_client is None:
            return
        slot = self._schedule_client.get_slot(channel_id)
        if slot is not None and slot.jellyfin_item_id:
            self.play_slot(slot)

    # ------------------------------------------------------------------
    # Mirror channels (see mirror.py)
    # ------------------------------------------------------------------

    def _is_mirror_channel(self, channel_id: str) -> bool:
        if self._channel_list is None or not channel_id:
            return False
        ch = self._channel_list.get_channel_by_id(channel_id)
        return ch is not None and ch.mirror is True

    def mirror_retune(self, slot: ScheduleSlot) -> None:
        """Replay a mirrored slot (item changed, drifted, or playback stopped).

        Stops first and waits for the Amlogic decoder, like a channel change, so
        the new stream never starts while the old one is tearing down.
        """
        self._internal_stop_time = time.monotonic()
        if self.isPlaying():
            self.stop()
            deadline = time.time() + DECODER_RELEASE_POLL_TIMEOUT_SECONDS
            while self.isPlaying() and time.time() < deadline:
                xbmc.sleep(DECODER_RELEASE_POLL_INTERVAL_MS)
        self.play_slot(slot)

    def leave_mirror(self, channel_id: str) -> None:
        """The mirrored user stopped watching: say so and go back to TV."""
        name = channel_id
        if self._channel_list is not None:
            ch = self._channel_list.get_channel_by_id(channel_id)
            if ch is not None:
                name = ch.name
        xbmcgui.Dialog().notification(
            "Classic TV",
            f"{name} signed off",
            xbmcgui.NOTIFICATION_INFO,
            3000,
        )
        target = self._pre_mirror_channel_id
        if (not target or self._is_mirror_channel(target)) and self._channel_list:
            others = [
                c for c in self._channel_list.all_channels() if c.mirror is not True
            ]
            target = others[0].id if others else ""
        if target:
            self._change_channel(target)

    # ------------------------------------------------------------------
    # Startup playback
    # ------------------------------------------------------------------

    def _wait_for_jellyfin_server(self, timeout_seconds: float) -> None:
        """Poll the Jellyfin health endpoint until it answers or the timeout elapses.

        Called from startup_play() before the schedule retry loop. Shows a
        visible notification on the CRT every JELLYFIN_READY_NOTIFY_SECONDS so
        the user knows why there is a delay. Always returns None — the gate is
        best-effort and never permanently blocks the caller.

        Exits early if Kodi signals an abort (device shutdown / addon disable).
        """
        deadline = time.monotonic() + timeout_seconds
        monitor = xbmc.Monitor()
        last_notify = 0.0
        attempt = 0
        while time.monotonic() < deadline:
            if monitor.abortRequested():
                xbmc.log(
                    f"{_LOG_PREFIX} readiness gate: Kodi abort requested — stopping",
                    xbmc.LOGINFO,
                )
                return
            attempt += 1
            if self._schedule_client is not None:
                healthy = self._schedule_client.check_server_health()
            else:
                healthy = False
            xbmc.log(
                f"{_LOG_PREFIX} readiness gate: attempt={attempt} healthy={healthy}",
                xbmc.LOGDEBUG,
            )
            if healthy:
                xbmc.log(
                    f"{_LOG_PREFIX} readiness gate: server ready after"
                    f" {attempt} attempt(s)",
                    xbmc.LOGINFO,
                )
                return
            now = time.monotonic()
            if now - last_notify >= JELLYFIN_READY_NOTIFY_SECONDS:
                xbmcgui.Dialog().notification(
                    "Classic TV",
                    "Waiting for media server...",
                    xbmcgui.NOTIFICATION_INFO,
                    8000,
                )
                last_notify = now
            xbmc.sleep(int(JELLYFIN_READY_POLL_SECONDS * 1000))
        xbmc.log(
            f"{_LOG_PREFIX} readiness gate: timed out after"
            f" {timeout_seconds:.0f}s — proceeding anyway",
            xbmc.LOGWARNING,
        )

    def startup_play(self, channel_id: str) -> bool:
        """Attempt to start playback at addon startup.

        Tries up to STARTUP_RETRY_MAX times with STARTUP_RETRY_INTERVAL_SECONDS
        between attempts. Returns True on success, False after all retries fail.
        """
        # NEW: wait for the Jellyfin server to be reachable first.
        self._wait_for_jellyfin_server(JELLYFIN_READY_TIMEOUT_SECONDS)

        # Check for Kodi abort before proceeding.
        if xbmc.Monitor().abortRequested():
            return False

        # Give Kodi's GLES/CEC/ALSA subsystems a brief settle window on cold boot
        # before opening DVDVideoCodecAmlogic directly (previously absorbed by the
        # 6-8 s CPythonInvoker startup of plugin.video.jellyfin).
        if DIRECT_HTTP_PLAYBACK:
            xbmc.sleep(3000)

        for attempt in range(1, STARTUP_RETRY_MAX + 1):
            xbmc.log(
                f"{_LOG_PREFIX} startup fetch attempt {attempt}/{STARTUP_RETRY_MAX}"
                f" channel={channel_id!r}",
                xbmc.LOGINFO,
            )
            slot = None
            if self._schedule_client is not None:
                slot = self._schedule_client.get_slot(channel_id)
            if slot is not None and slot.jellyfin_item_id:
                xbmc.log(
                    f"{_LOG_PREFIX} startup fetch succeeded (attempt {attempt})",
                    xbmc.LOGINFO,
                )
                self.play_slot(slot)
                if self._channel_bug is not None and self._channel_list is not None:
                    ch = self._channel_list.get_channel_by_id(channel_id)
                    if ch is not None:
                        self._wait_before_overlay()
                        self._channel_bug.show_for(ch.number, ch.name)
                return True
            if attempt < STARTUP_RETRY_MAX:
                xbmc.log(
                    f"{_LOG_PREFIX} startup attempt {attempt} failed;"
                    f" retrying in {STARTUP_RETRY_INTERVAL_SECONDS}s",
                    xbmc.LOGWARNING,
                )
                xbmc.sleep(STARTUP_RETRY_INTERVAL_SECONDS * 1000)

        xbmc.log(
            f"{_LOG_PREFIX} all {STARTUP_RETRY_MAX} startup attempts failed",
            xbmc.LOGERROR,
        )
        return False

    # ------------------------------------------------------------------
    # Watchdog recovery
    # ------------------------------------------------------------------

    def _reload_jellyfin_addon(self) -> bool:
        """Disable then re-enable plugin.video.jellyfin via JSON-RPC.

        Returns True if the reload sequence was attempted (cooldown not active),
        False if the cooldown prevented the attempt.
        """
        now = time.monotonic()
        elapsed = now - self._last_reload_time
        if elapsed < JELLYFIN_RELOAD_COOLDOWN_SECONDS and self._last_reload_time > 0.0:
            remaining = JELLYFIN_RELOAD_COOLDOWN_SECONDS - elapsed
            xbmc.log(
                f"{_LOG_PREFIX} reload cooldown: {remaining:.0f}s remaining — skipping",
                xbmc.LOGWARNING,
            )
            return False

        xbmc.log(
            f"{_LOG_PREFIX} _reload_jellyfin_addon: starting reload"
            f" (last_reload={elapsed:.0f}s ago)",
            xbmc.LOGINFO,
        )

        # Step 1: disable.
        disable_rpc = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "Addons.SetAddonEnabled",
                "params": {"addonid": "plugin.video.jellyfin", "enabled": False},
            }
        )
        result = xbmc.executeJSONRPC(disable_rpc)
        if _jsonrpc_failed(result):
            xbmc.log(
                f"{_LOG_PREFIX} _reload_jellyfin_addon: disable step failed:"
                f" {result!r} — falling back to RestartApp",
                xbmc.LOGERROR,
            )
            xbmc.executebuiltin("RestartApp")
            return True

        # Step 2: wait for the addon to unload.
        xbmc.sleep(JELLYFIN_RELOAD_WAIT_SECONDS * 1000)

        # Step 3: re-enable.
        enable_rpc = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "Addons.SetAddonEnabled",
                "params": {"addonid": "plugin.video.jellyfin", "enabled": True},
            }
        )
        result = xbmc.executeJSONRPC(enable_rpc)
        if _jsonrpc_failed(result):
            xbmc.log(
                f"{_LOG_PREFIX} _reload_jellyfin_addon: re-enable step failed:"
                f" {result!r} — falling back to RestartApp",
                xbmc.LOGERROR,
            )
            xbmc.executebuiltin("RestartApp")
            return True

        self._last_reload_time = time.monotonic()
        xbmc.log(
            f"{_LOG_PREFIX} _reload_jellyfin_addon: reload issued;"
            f" waiting for onPlayBackStarted ({PLAYBACK_CONFIRM_TIMEOUT_SECONDS}s)",
            xbmc.LOGINFO,
        )

        # Show waiting state while Jellyfin addon reinitialises.
        xbmcgui.Dialog().notification(
            "Classic TV",
            "Waiting for media server...",
            xbmcgui.NOTIFICATION_INFO,
            PLAYBACK_CONFIRM_TIMEOUT_SECONDS * 1000,
        )

        # Wait for playback to confirm after the re-enable.
        confirmed = self._play_confirm_event.wait(PLAYBACK_CONFIRM_TIMEOUT_SECONDS)
        if not confirmed:
            xbmc.log(
                f"{_LOG_PREFIX} _reload_jellyfin_addon: playback not confirmed after"
                f" {PLAYBACK_CONFIRM_TIMEOUT_SECONDS}s — invoking RestartApp",
                xbmc.LOGERROR,
            )
            xbmc.executebuiltin("RestartApp")

        return True

    def watchdog_recover(self) -> None:
        """Called by the watchdog when playback has stopped unexpectedly.

        Fetches the current slot for the active channel and replays it.
        This is the sole EOF recovery mechanism in 4.0 — onPlayBackEnded
        does not fire because Classic TV does not own the player.

        If all retries fail, check whether the channel still exists in the
        live channel list.  If it has disappeared (disabled or deleted), tune
        to the first available channel rather than starting the offline retry
        loop.
        """
        channel_id = self._current_channel_id
        if not channel_id or self._schedule_client is None:
            return
        if self._is_mirror_channel(channel_id):
            return  # the mirror sync loop owns recovery on these channels

        xbmc.log(
            f"{_LOG_PREFIX} watchdog recovery for channel={channel_id!r}",
            xbmc.LOGINFO,
        )

        for attempt in range(1, EOF_RETRY_MAX + 1):
            slot = self._schedule_client.get_slot(channel_id)
            if slot is not None and slot.jellyfin_item_id:
                self.play_slot(slot)
                # NEW: wait for playback confirmation.
                confirmed = self._play_confirm_event.wait(
                    PLAYBACK_CONFIRM_TIMEOUT_SECONDS
                )
                if confirmed:
                    # _consecutive_failures already reset by onPlayBackStarted.
                    return
                # Playback not confirmed — check if server is up (wedge vs outage).
                server_healthy = (
                    self._schedule_client is not None
                    and self._schedule_client.check_server_health()
                )
                if server_healthy:
                    self._consecutive_failures += 1
                    xbmc.log(
                        f"{_LOG_PREFIX} watchdog: playback unconfirmed"
                        f" (server healthy), consecutive_failures={self._consecutive_failures}",
                        xbmc.LOGWARNING,
                    )
                    if self._consecutive_failures >= WEDGE_FAILURE_THRESHOLD:
                        xbmc.log(
                            f"{_LOG_PREFIX} watchdog: threshold reached — invoking self-heal",
                            xbmc.LOGWARNING,
                        )
                        self._consecutive_failures = 0
                        self._reload_jellyfin_addon()
                else:
                    # Server unreachable — treat as network outage, not wedge.
                    # Fall through to the existing offline retry path.
                    pass
                return
            if attempt < EOF_RETRY_MAX:
                xbmc.sleep(int(EOF_RETRY_INTERVAL_SECONDS * 1000))

        # All fast retries exhausted — check whether the channel disappeared.
        if not self._schedule_client.channel_exists(channel_id):
            xbmc.log(
                f"{_LOG_PREFIX} channel disappeared (disabled or deleted):"
                f" channel={channel_id!r} — tuning to first available channel",
                xbmc.LOGWARNING,
            )
            self._tune_to_first_channel()
            return

        xbmc.log(
            f"{_LOG_PREFIX} watchdog recovery failed after {EOF_RETRY_MAX} attempts"
            f" channel={channel_id!r} — starting offline retry",
            xbmc.LOGWARNING,
        )
        self._start_offline_retry(channel_id)

    def _tune_to_first_channel(self) -> None:
        """Tune to the first available channel in the channel list.

        Used when the currently-tuned channel has disappeared (disabled or
        deleted).  If the channel list is empty or unavailable, falls back to
        the offline retry on the current channel.
        """
        if self._channel_list is None:
            xbmc.log(
                f"{_LOG_PREFIX} _tune_to_first_channel: no channel list — falling back to offline retry",
                xbmc.LOGWARNING,
            )
            self._start_offline_retry(self._current_channel_id)
            return

        channels = self._channel_list.all_channels()
        if not channels:
            xbmc.log(
                f"{_LOG_PREFIX} _tune_to_first_channel: channel list is empty — staying in offline retry",
                xbmc.LOGWARNING,
            )
            self._start_offline_retry(self._current_channel_id)
            return

        first = channels[0]
        xbmc.log(
            f"{_LOG_PREFIX} _tune_to_first_channel: tuning to {first.id!r}",
            xbmc.LOGINFO,
        )
        self._change_channel(first.id)

    def _start_offline_retry(self, channel_id: str) -> None:
        """Start a single cancellable offline retry worker.

        Cancels any existing worker first — only one retry loop is ever alive.
        The worker stops when the service recovers (play_slot() is called,
        which sets _retry_stop) or when stop() is called on shutdown.

        Every CHANNEL_DISAPPEARED_CHECK_INTERVAL failed attempts the worker
        also checks whether the channel still exists in the channel list.  If
        the channel has been disabled or deleted, the worker tunes to the first
        available channel and exits rather than looping indefinitely.
        """
        # Cancel any existing worker.
        self._retry_stop.set()
        self._retry_stop = threading.Event()
        stop = self._retry_stop

        def _loop() -> None:
            attempt = 0
            while not stop.wait(EOF_OFFLINE_RETRY_INTERVAL_SECONDS):
                attempt += 1
                slot = None
                if self._schedule_client is not None:
                    slot = self._schedule_client.get_slot(channel_id)
                if slot is not None and slot.jellyfin_item_id:
                    xbmc.log(
                        f"{_LOG_PREFIX} offline retry: service recovered"
                        f" channel={channel_id!r}",
                        xbmc.LOGINFO,
                    )
                    self.play_slot(slot)
                    return
                xbmc.log(
                    f"{_LOG_PREFIX} offline retry: service still unavailable"
                    f" channel={channel_id!r}",
                    xbmc.LOGWARNING,
                )
                # Periodically check whether the channel itself disappeared.
                if attempt % CHANNEL_DISAPPEARED_CHECK_INTERVAL == 0:
                    if (
                        self._schedule_client is not None
                        and not self._schedule_client.channel_exists(channel_id)
                    ):
                        xbmc.log(
                            f"{_LOG_PREFIX} offline retry: channel disappeared"
                            f" channel={channel_id!r} — tuning to first available",
                            xbmc.LOGWARNING,
                        )
                        self._tune_to_first_channel()
                        return
            xbmc.log(
                f"{_LOG_PREFIX} offline retry: cancelled channel={channel_id!r}",
                xbmc.LOGDEBUG,
            )

        t = threading.Thread(target=_loop, daemon=True, name="classictv-offline-retry")
        t.start()
