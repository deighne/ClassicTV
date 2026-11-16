"""
Mode state machine for Classic TV 4.0.

Three modes, one owner of every transition:

    TV_MODE          Classic TV owns the player; the watchdog is running.
    BROWSE_MODE      The viewer left to browse or watch something else; the
                     watchdog is suspended and channel input is ignored.
    RESUME_PENDING   Browse playback finished on its own; after a short grace
                     Classic TV reclaims the player, unless the viewer starts
                     something else first.

TV_MODE runs a watchdog daemon thread that polls player.isPlaying() every
``watchdog_interval`` seconds. When playback stops unexpectedly it calls
player.watchdog_recover().

Every read and write of ``current`` holds ``_lock``. Callers use the
transition methods below and never assign ``current`` directly.
"""

from __future__ import annotations

import threading

import xbmc

from constants import RESUME_PENDING_GRACE_SECONDS, WATCHDOG_INTERVAL_SECONDS

TV_MODE = "tv"
BROWSE_MODE = "browse"
RESUME_PENDING = "resume"

_LOG_PREFIX = "[ClassicTV][modes]"


def _player_is_paused(player) -> bool:
    """True if Kodi reports the player paused (isPlaying() can be False then)."""
    try:
        return bool(xbmc.getCondVisibility("Player.Paused"))
    except Exception:
        return False


def _watchdog_loop(modes: "ModeState", stop: threading.Event) -> None:
    """Daemon thread: detect stopped playback and trigger recovery."""
    while not stop.wait(modes._watchdog_interval):
        if modes.current != TV_MODE:
            continue
        player = getattr(modes, "player", None)
        if player is None:
            continue
        # Skip the isPlaying() check while the Jellyfin addon is still
        # starting up the stream after a PlayMedia() call.
        if hasattr(player, "is_in_grace_period") and player.is_in_grace_period():
            continue
        # Viewer is browsing (stopped Classic TV deliberately): do not recover.
        if hasattr(player, "is_user_browsing") and player.is_user_browsing():
            continue
        stalled = hasattr(player, "is_av_stalled") and player.is_av_stalled() is True
        if not player.isPlaying() or stalled:
            if _player_is_paused(player):
                continue
            if stalled:
                xbmc.log(
                    f"{_LOG_PREFIX} watchdog: AV never started (stalled stream)"
                    " — triggering recovery",
                    xbmc.LOGWARNING,
                )
                if hasattr(player, "stop_for_recovery"):
                    player.stop_for_recovery()
            else:
                xbmc.log(
                    f"{_LOG_PREFIX} watchdog: playback stopped — triggering recovery",
                    xbmc.LOGWARNING,
                )
            try:
                player.watchdog_recover()
            except Exception as exc:
                xbmc.log(
                    f"{_LOG_PREFIX} watchdog: recovery raised: {exc}",
                    xbmc.LOGWARNING,
                )


class ModeState:
    """Operating-mode state machine.

    Attributes:
        current_channel: Channel ID most recently tuned in TV_MODE.
        player:          Injected ClassicTVPlayer; used by the watchdog and to
                         reclaim the player when RESUME_PENDING expires.
    """

    def __init__(
        self,
        watchdog_interval: float = WATCHDOG_INTERVAL_SECONDS,
        resume_grace: float = RESUME_PENDING_GRACE_SECONDS,
    ) -> None:
        self._lock = threading.RLock()
        self._current: str = TV_MODE
        self.current_channel: str = ""
        self.player = None  # type: ignore[assignment]
        self._watchdog_interval = watchdog_interval
        self._resume_grace = resume_grace
        self._resume_timer: threading.Timer | None = None

        # Watchdog stop event starts set (no watchdog running until an
        # explicit enter_tv() call).
        self._watchdog_stop: threading.Event = threading.Event()
        self._watchdog_stop.set()

    @property
    def current(self) -> str:
        with self._lock:
            return self._current

    # ------------------------------------------------------------------
    # Transitions
    # ------------------------------------------------------------------

    def enter_tv(self, channel_id: str) -> None:
        """Enter TV_MODE for the given channel and (re)start the watchdog."""
        with self._lock:
            xbmc.log(
                f"{_LOG_PREFIX} → TV channel={channel_id!r}",
                xbmc.LOGINFO,
            )
            self._cancel_resume_timer()
            self._stop_watchdog()
            self._current = TV_MODE
            self.current_channel = channel_id
            self._start_watchdog()

    # Older name, kept for callers written before the three-mode model.
    transition_to_tv = enter_tv

    def enter_browse(self, reason: str = "") -> None:
        """Enter BROWSE_MODE: cancel any pending resume and stop the watchdog."""
        with self._lock:
            self._cancel_resume_timer()
            self._stop_watchdog()
            self._current = BROWSE_MODE
            xbmc.log(f"{_LOG_PREFIX} → BROWSE reason={reason}", xbmc.LOGINFO)

    def enter_resume_pending(self) -> None:
        """Enter RESUME_PENDING from BROWSE_MODE and arm the reclaim timer.

        No-op from any other mode: only browse playback can be "pending resume".
        """
        with self._lock:
            if self._current != BROWSE_MODE:
                return
            self._current = RESUME_PENDING
            self._cancel_resume_timer()
            self._resume_timer = threading.Timer(
                self._resume_grace, self._on_resume_timeout
            )
            self._resume_timer.daemon = True
            self._resume_timer.start()
            xbmc.log(
                f"{_LOG_PREFIX} → RESUME_PENDING channel={self.current_channel!r}"
                f" in {self._resume_grace}s",
                xbmc.LOGINFO,
            )

    def on_playback_started(self, is_classictv_initiated: bool) -> None:
        """Called from the player's onPlayBackStarted with who started it."""
        with self._lock:
            if is_classictv_initiated:
                self._cancel_resume_timer()
                return
            if self._current in (TV_MODE, RESUME_PENDING):
                self.enter_browse(
                    "auto-detect" if self._current == TV_MODE else "new-playback"
                )

    def on_playback_ended(self) -> None:
        """Called when playback reaches a natural end (onPlayBackEnded).

        In BROWSE_MODE this means whatever the viewer was watching finished, so
        start the grace countdown back to TV. In any other mode it is a no-op:
        in TV_MODE the watchdog owns episode-end recovery.
        """
        with self._lock:
            if self._current == BROWSE_MODE:
                self.enter_resume_pending()

    def stop(self) -> None:
        """Stop the watchdog and any pending resume (addon shutdown)."""
        with self._lock:
            self._cancel_resume_timer()
            self._stop_watchdog()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _on_resume_timeout(self) -> None:
        """Timer thread: reclaim the player if still RESUME_PENDING."""
        with self._lock:
            if self._current != RESUME_PENDING:
                return
            self._resume_timer = None
            player = self.player
        xbmc.log(f"{_LOG_PREFIX} resume grace elapsed — reclaiming player", xbmc.LOGINFO)
        if player is None:
            return
        try:
            # resume_from_browse() re-tunes, and the channel change calls
            # enter_tv() on success, which completes the transition.
            player.resume_from_browse()
        except Exception as exc:
            xbmc.log(f"{_LOG_PREFIX} resume raised: {exc}", xbmc.LOGWARNING)

    def _cancel_resume_timer(self) -> None:
        if self._resume_timer is not None:
            self._resume_timer.cancel()
            self._resume_timer = None

    def _start_watchdog(self) -> None:
        stop = threading.Event()
        self._watchdog_stop = stop
        t = threading.Thread(
            target=_watchdog_loop,
            args=(self, stop),
            daemon=True,
            name="classictv-watchdog",
        )
        t.start()

    def _stop_watchdog(self) -> None:
        self._watchdog_stop.set()
