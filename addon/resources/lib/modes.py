"""
Mode state machine for Classic TV 3.0 Player addon.

Maintains exactly one of three operating modes at all times:
  - TV_MODE   ("tv")     — default; Player controls playback via Schedule_Service
  - MOVIE_MODE ("movie") — Kodi native Jellyfin addon owns the session; Watchdog suspended
  - RACHEL_MODE ("rachel") — CH67 mirror mode; polling /rachel-anchor every 2 seconds

Any mode transition calls _cancel_all() first to guarantee no state leakage from
the previous mode (Requirement 17.9).
"""

import threading
from typing import Callable

import xbmc

from constants import (
    WATCHDOG_INTERVAL_SECONDS,
    RACHEL_POLL_INTERVAL_SECONDS,
    RACHEL_POLL_TIMEOUT_SECONDS,
    RACHEL_DRIFT_THRESHOLD_SECONDS,
)

# ---------------------------------------------------------------------------
# Module-level mode constants
# ---------------------------------------------------------------------------

TV_MODE = "tv"
MOVIE_MODE = "movie"
RACHEL_MODE = "rachel"

_LOG_PREFIX = "[ClassicTV][modes]"


# ---------------------------------------------------------------------------
# Watchdog loop (daemon thread target)
# ---------------------------------------------------------------------------

def _watchdog_loop(modes: "ModeState") -> None:
    """Daemon thread: check playback health every WATCHDOG_INTERVAL_SECONDS.

    Exits cleanly when modes._watchdog_stop is set (i.e. on any mode
    transition). Only fires the watchdog callback when in TV_MODE and the
    attached player is not actively playing.
    """
    while not modes._watchdog_stop.wait(WATCHDOG_INTERVAL_SECONDS):
        if modes.current != TV_MODE:
            continue
        player = getattr(modes, "player", None)
        if player is None:
            continue
        if not player.isPlaying():
            xbmc.log(
                f"{_LOG_PREFIX} watchdog detected stopped playback; resuming",
                xbmc.LOGWARNING,
            )
            try:
                modes._on_watchdog_trigger()
            except Exception:  # noqa: S110  # watchdog trigger must never crash the watchdog loop
                pass


# ---------------------------------------------------------------------------
# Rachel poller loop (daemon thread target — stub until task 13.1)
# ---------------------------------------------------------------------------

def _rachel_poller_loop(modes: "ModeState") -> None:
    """Daemon thread: polls /rachel-anchor every RACHEL_POLL_INTERVAL_SECONDS.

    Implements Requirements 18.1–18.6:
    - 18.1 Poll GET /rachel-anchor at the configured interval.
    - 18.2 On state="idle", stop playback and play the Offline_Clip.
    - 18.3 On state="playing" with a new jellyfin_id, play at the computed position.
    - 18.4 On state="playing" with the same jellyfin_id, apply drift correction.
    - 18.5 Rate-limit WARNING logs to at most one per 60 seconds.
    - 18.6 Exit cleanly when modes._rachel_stop is set.
    """
    import json
    import urllib.request
    import urllib.error
    import time

    prev_id = None
    last_warning_time = 0.0

    while not modes._rachel_stop.wait(RACHEL_POLL_INTERVAL_SECONDS):
        service_url = getattr(modes, "_schedule_service_url", "")
        if not service_url:
            continue

        url = f"{service_url}/rachel-anchor"
        try:
            with urllib.request.urlopen(url, timeout=RACHEL_POLL_TIMEOUT_SECONDS) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, urllib.error.HTTPError, Exception) as exc:
            now = time.time()
            if now - last_warning_time > 60:
                xbmc.log(
                    f"{_LOG_PREFIX} rachel poll failed: {exc}",
                    xbmc.LOGWARNING,
                )
                last_warning_time = now
            continue

        state = data.get("state", "idle")

        player = getattr(modes, "player", None)
        if player is None:
            continue

        if state == "idle":
            if player.isPlaying():
                player.stop()
                offline_url = getattr(modes, "_offline_clip_url", "")
                if offline_url:
                    import xbmcgui
                    li = xbmcgui.ListItem(label="Offline", path=offline_url)
                    player.play(offline_url, li)
            continue

        if state == "playing":
            jellyfin_id = data.get("jellyfin_id")
            anchor_pos = float(data.get("anchor_position", 0))
            anchor_ts = float(data.get("anchor_timestamp", time.time()))

            computed_pos = anchor_pos + (time.time() - anchor_ts)

            if jellyfin_id != prev_id:
                # New item — play at the computed position via a seek-ticks URL.
                jellyfin_url = getattr(modes, "_jellyfin_base_url", "http://192.168.1.207:8096")
                direct_url = f"{jellyfin_url}/Videos/{jellyfin_id}/stream?Static=true"

                import xbmcgui
                ticks = int(computed_pos * 10_000_000)
                url_with_seek = f"{direct_url}&StartTimeTicks={ticks}"
                li = xbmcgui.ListItem(label=f"Rachel: {jellyfin_id}", path=url_with_seek)
                player.play(url_with_seek, li)
                prev_id = jellyfin_id
                xbmc.log(
                    f"{_LOG_PREFIX} rachel new item {jellyfin_id} at {computed_pos:.1f}s",
                    xbmc.LOGINFO,
                )
            else:
                # Same item — drift correction if beyond threshold.
                actual_pos = player.getTime()
                if abs(actual_pos - computed_pos) > RACHEL_DRIFT_THRESHOLD_SECONDS:
                    player.seekTime(computed_pos)
                    xbmc.log(
                        f"{_LOG_PREFIX} rachel drift correction: seekTime({computed_pos:.1f}s)",
                        xbmc.LOGINFO,
                    )


# ---------------------------------------------------------------------------
# ModeState
# ---------------------------------------------------------------------------

class ModeState:
    """Operating-mode state machine for the Classic TV 3.0 Player.

    Attributes
    ----------
    current : str
        One of TV_MODE, MOVIE_MODE, or RACHEL_MODE.
    current_channel : str
        The channel ID most recently tuned in TV_Mode.
    player : xbmc.Player | None
        Injected by the service after construction; used by the watchdog.
    _on_watchdog_trigger : Callable[[], None]
        Callback invoked when the watchdog detects stopped playback.
        Defaults to a no-op; overridden by service.py.
    """

    def __init__(self) -> None:
        self.current: str = TV_MODE
        self.current_channel: str = ""

        # Player reference — injected after construction by service.py.
        self.player = None  # type: ignore[assignment]

        # Watchdog stop event starts set ("already stopped") so no watchdog
        # thread is running until an explicit transition_to_tv() call.
        self._watchdog_stop: threading.Event = threading.Event()
        self._watchdog_stop.set()

        # Rachel poller stop event likewise starts set.
        self._rachel_stop: threading.Event = threading.Event()
        self._rachel_stop.set()

        # Pending threading.Timer objects (e.g. digit-entry timeouts).
        self._timers: list = []

        # Watchdog trigger callback — overridden by service.py.
        self._on_watchdog_trigger: Callable[[], None] = lambda: None

        # Rachel mode config (set externally by service.py)
        self._schedule_service_url: str = ""
        self._offline_clip_url: str = ""
        self._jellyfin_base_url: str = "http://192.168.1.207:8096"

    # ------------------------------------------------------------------
    # Public transitions
    # ------------------------------------------------------------------

    def transition_to_tv(self, channel_id: str) -> None:
        """Enter TV_Mode for the given channel.

        Cancels all in-flight timers and threads, then starts the Watchdog
        daemon (Requirement 17.1, 17.2, 17.9).

        Parameters
        ----------
        channel_id:
            The channel ID to record as the currently tuned channel.
        """
        xbmc.log(
            f"{_LOG_PREFIX} transition_to_tv channel={channel_id!r}",
            xbmc.LOGINFO,
        )
        self._cancel_all()
        self.current = TV_MODE
        self.current_channel = channel_id
        self._start_watchdog()

    def transition_to_movie(self) -> None:
        """Enter Movie_Mode — suspends the Watchdog (Requirement 17.3, 17.6, 17.9)."""
        xbmc.log(f"{_LOG_PREFIX} transition_to_movie", xbmc.LOGINFO)
        self._cancel_all()
        self.current = MOVIE_MODE
        # Watchdog is intentionally NOT started here. Kodi's native player owns
        # the session; Classic TV must not interfere (Requirement 17.6).

    def transition_to_rachel(self) -> None:
        """Enter Rachel_Mode — suspends Watchdog, starts Rachel poller (Requirement 17.7, 17.9)."""
        xbmc.log(f"{_LOG_PREFIX} transition_to_rachel", xbmc.LOGINFO)
        self._cancel_all()
        self.current = RACHEL_MODE
        self._start_rachel_poller()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _cancel_all(self) -> None:
        """Cancel all in-flight timers and signal all daemon threads to stop.

        After this method returns:
        - Every pending threading.Timer has been cancelled.
        - _timers is empty.
        - _watchdog_stop is set (signals watchdog thread to exit).
        - _rachel_stop is set (signals Rachel poller to exit).
        - Fresh, unset Event objects are created for the next mode's threads.

        This is the guarantee made by Requirement 17.9.
        """
        # Cancel pending timers.
        for t in self._timers:
            t.cancel()
        self._timers.clear()

        # Signal background threads to exit.
        self._watchdog_stop.set()
        self._rachel_stop.set()

        # Replace events so the next mode starts with a clean, unset event.
        self._watchdog_stop = threading.Event()
        self._rachel_stop = threading.Event()

    def _start_watchdog(self) -> None:
        """Start the Watchdog daemon thread.

        Assumes _cancel_all() has already been called and self._watchdog_stop
        is a fresh, unset Event. Sets up a new stop event and launches the
        daemon thread (Requirement 17.1, 17.9).

        The player reference is accessed via self.player at runtime so the
        watchdog works correctly even if player is injected after this call.
        """
        # _cancel_all() already created a fresh Event; set it before reassigning
        # just to be explicit, then use it directly.
        self._watchdog_stop = threading.Event()
        t = threading.Thread(
            target=_watchdog_loop,
            args=(self,),
            daemon=True,
            name="classictv-watchdog",
        )
        t.start()
        xbmc.log(f"{_LOG_PREFIX} watchdog started", xbmc.LOGINFO)

    def _start_rachel_poller(self) -> None:
        """Start the Rachel poller daemon thread.

        Full implementation delivered in task 13.1. The stub thread parks on
        _rachel_stop so the thread lifecycle is testable now.
        """
        self._rachel_stop = threading.Event()
        t = threading.Thread(
            target=_rachel_poller_loop,
            args=(self,),
            daemon=True,
            name="classictv-rachel-poller",
        )
        t.start()
        xbmc.log(f"{_LOG_PREFIX} rachel poller started", xbmc.LOGINFO)
