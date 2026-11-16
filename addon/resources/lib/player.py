"""
ClassicTVPlayer — core playback and seek strategy for Classic TV 3.0.

Responsibilities:
  - Play a schedule item from a Schedule_Service response, embedding
    StartTimeTicks in the URL for Strategy A seek (Requirement 11.3).
  - On onPlayBackStarted, dismiss Channel_Static, show Channel_Bug, then
    verify the achieved playback position and apply Strategy B (post-start
    seekTime retry) if the position is outside tolerance (Requirement 11.4).
  - Prefetch the next item into the MediaCache if available.
  - Log the target seek offset, strategy used, and achieved position for
    every item start (Requirement 11.6).

Circular-import avoidance: channel_bug and channel_static modules import
constants, not player, so the import graph is acyclic.  MediaCache is passed
in from the outside (set on _media_cache).

See docs/player-design-notes.md for the full seek strategy rationale.
"""

import time
import threading
import xbmc
import xbmcgui

from constants import (
    SEEK_MAX_RETRIES,
    SEEK_RETRY_INTERVAL_MS,
    SEEK_TOLERANCE_SECONDS,
    SEEK_CALLBACK_MAX_BLOCKING_MS,  # noqa: F401 — used in onPlayBackStarted thread spawn (Req 39)
    EOF_RETRY_MAX,
    EOF_RETRY_INTERVAL_SECONDS,
    EOF_OFFLINE_RETRY_INTERVAL_SECONDS,
    CHANNEL_CHANGE_FETCH_TIMEOUT_SECONDS,
    DECODER_RELEASE_POLL_INTERVAL_MS,
    DECODER_RELEASE_POLL_TIMEOUT_SECONDS,
    WATCHDOG_INTERVAL_SECONDS,
)

_LOG_PREFIX = "[ClassicTV][player]"


class ClassicTVPlayer(xbmc.Player):
    """Kodi Player subclass that drives Classic TV 3.0 playback.

    External collaborators are injected after construction rather than
    passed to __init__ so that the Player can be created early in the
    addon lifecycle without requiring all overlays to exist first.

    Attributes:
        _pending_seek:      Seek offset (seconds) for the currently playing
                            item, retained so onPlayBackStarted can verify
                            the achieved position.
        _current_item:      The schedule item dict most recently passed to
                            play_schedule_item().
        _current_channel_id: The channel_id string from the most recent
                            schedule response.
        _channel_static:    ChannelStatic instance or None.  If set,
                            on_playback_started() is called when playback
                            begins.
        _channel_bug:       ChannelBug instance or None.  If set, show() is
                            called when playback begins.
        _media_cache:       MediaCache instance or None.  If set, local URLs
                            are preferred and the next item is prefetched.
    """

    def __init__(self) -> None:
        super().__init__()
        self._pending_seek: float = 0.0
        self._current_item: dict = {}
        self._current_channel_id: str = ""
        self._channel_static = None   # ChannelStatic instance, set externally
        self._channel_bug = None      # ChannelBug instance, set externally
        self._media_cache = None      # MediaCache instance, set externally
        self._schedule_service_url: str = ""  # Set by service.py after construction
        self._channel_list = None             # ChannelList instance, set externally
        self._modes = None                    # ModeState instance, set externally
        self._offline_clip_url: str = ""      # URL for the offline clip, set externally
        # Requirement 37: guard against re-entrant seek calls.
        self._seek_in_progress: bool = False
        # Requirement 39: handle to the daemon seek thread spawned from
        # onPlayBackStarted so it can be inspected. play_schedule_item()
        # clears _seek_in_progress so the old thread terminates naturally.
        self._seek_thread: "threading.Thread | None" = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def play_schedule_item(self, schedule_response: dict) -> None:
        """Begin playback of a schedule item returned by the Schedule_Service.

        Implements the two-stage seek strategy (Requirements 11.1–11.6):
          Stage 1 — embed StartTimeTicks in the URL before calling play().
          Stage 2 — check achieved position in onPlayBackStarted and issue
                    seekTime() if the position is outside tolerance.

        Args:
            schedule_response: Parsed JSON dict from GET /schedule/{channel}.
                Must contain 'channel_id' and 'item' keys; 'item' must contain
                'jellyfin_id', 'title', 'seek_offset', 'duration', and
                'direct_play_url'.
        """
        channel_id: str = schedule_response.get("channel_id", "")
        item: dict = schedule_response["item"]

        title: str = item.get("title", "")
        seek_offset: float = float(item.get("seek_offset", 0.0))

        # ------------------------------------------------------------------
        # Resolve URL: prefer locally cached copy when available.
        # ------------------------------------------------------------------
        jellyfin_id: str = item.get("jellyfin_id", "")
        url: str = item["direct_play_url"]

        if self._media_cache:
            local_url = self._media_cache.get_local_url(jellyfin_id)
            if local_url:
                url = local_url
                xbmc.log(
                    f"{_LOG_PREFIX} using cached local URL for"
                    f" channel={channel_id!r} title={title!r}"
                    f" jellyfin_id={jellyfin_id!r}",
                    xbmc.LOGDEBUG,
                )

        # ------------------------------------------------------------------
        # Strategy 1 (Stage 1): embed StartTimeTicks in the URL.
        # Only appended when there is a meaningful seek offset and the
        # parameter is not already present (e.g., local file URLs).
        # Requirements 11.3, 10.1.
        # ------------------------------------------------------------------
        strategy_used = "direct"
        if seek_offset > 0:
            if "StartTimeTicks" not in url and url.startswith(("http://", "https://")):
                # Req 40.1: StartTimeTicks is a Jellyfin HTTP query parameter.
                # Never append it to local filesystem paths from the media cache.
                ticks = int(seek_offset * 10_000_000)
                url = f"{url}&StartTimeTicks={ticks}"
                strategy_used = "StartTimeTicks"
            else:
                strategy_used = "StartTimeTicks(existing)"

        # ------------------------------------------------------------------
        # Store state for onPlayBackStarted callbacks.
        # Clear the seek guard so the new play call can seek if needed
        # (Requirement 37).
        # ------------------------------------------------------------------
        self._pending_seek = seek_offset
        self._seek_in_progress = False
        self._seek_thread = None
        self._current_item = item
        self._current_channel_id = channel_id

        # ------------------------------------------------------------------
        # Build ListItem and start playback.
        # ------------------------------------------------------------------
        listitem = xbmcgui.ListItem(label=title, path=url)
        listitem.setInfo("video", {"title": title})
        listitem.setProperty("IsPlayable", "true")

        xbmc.log(
            f"{_LOG_PREFIX} play_schedule_item"
            f" channel={channel_id!r}"
            f" title={title!r}"
            f" seek_offset={seek_offset:.3f}s"
            f" strategy={strategy_used}"
            f" url={url!r}",
            xbmc.LOGINFO,
        )

        self.play(url, listitem)

    # ------------------------------------------------------------------
    # Kodi Player callbacks
    # ------------------------------------------------------------------

    def onPlayBackStarted(self) -> None:  # noqa: N802 (Kodi naming convention)
        """Called by Kodi when playback begins.

        Performs three actions in order:
          1. Dismisses Channel_Static if it is set.
          2. Shows Channel_Bug if it is set.
          3. Checks achieved position and applies Strategy B (seekTime retry)
             if the position is outside SEEK_TOLERANCE_SECONDS.

        Also kicks off prefetch of the next item if MediaCache is available.
        """
        title: str = self._current_item.get("title", "")
        channel_id: str = self._current_channel_id

        xbmc.log(
            f"{_LOG_PREFIX} onPlayBackStarted"
            f" channel={channel_id!r}"
            f" title={title!r}",
            xbmc.LOGINFO,
        )

        # 1. Dismiss Channel_Static (Requirement 12.6).
        if self._channel_static is not None:
            try:
                self._channel_static.on_playback_started()
            except Exception as exc:
                xbmc.log(
                    f"{_LOG_PREFIX} channel_static.on_playback_started() raised: {exc}",
                    xbmc.LOGWARNING,
                )

        # 2. Show Channel_Bug (Requirements 13.1, 13.2).
        if self._channel_bug is not None:
            try:
                self._channel_bug.show()
            except Exception as exc:
                xbmc.log(
                    f"{_LOG_PREFIX} channel_bug.show() raised: {exc}",
                    xbmc.LOGWARNING,
                )

        # 3. Verify seek accuracy (Strategy 2 — Stage 2).
        # Only attempt a corrective seek when the intended offset was large
        # enough to matter (> SEEK_TOLERANCE_SECONDS). This prevents a seek
        # attempt on items that are meant to start from the beginning.
        #
        # Requirement 37: guard against re-entrant calls. On the Amlogic
        # decoder (CoreELEC 21.3), seekTime() fires onPlayBackStarted again.
        # Without this guard, each seekTime() spawns a new concurrent seek
        # correction chain. The flag blocks re-entry; play_schedule_item()
        # clears it for the next play call.
        if self._seek_in_progress:
            xbmc.log(
                f"{_LOG_PREFIX} onPlayBackStarted: seek already in progress,"
                f" ignoring re-entrant call"
                f" channel={channel_id!r} title={title!r}",
                xbmc.LOGDEBUG,
            )
            return

        target = self._pending_seek
        achieved = self.getTime()

        if target > SEEK_TOLERANCE_SECONDS and abs(achieved - target) > SEEK_TOLERANCE_SECONDS:
            xbmc.log(
                f"{_LOG_PREFIX} Strategy A missed:"
                f" target={target:.3f}s achieved={achieved:.3f}s"
                f" channel={channel_id!r} title={title!r}"
                f" — spawning seek thread (Strategy B)",
                xbmc.LOGINFO,
            )
            # Requirement 39: spawn a daemon thread so this callback returns
            # immediately. Blocking the Kodi callback thread with xbmc.sleep()
            # causes the Amlogic decoder to time out and abort the stream.
            self._seek_in_progress = True

            def _seek_target(t: float = target) -> None:
                try:
                    self._seek_with_retry(t)
                finally:
                    self._seek_in_progress = False

            self._seek_thread = threading.Thread(target=_seek_target, daemon=True)
            self._seek_thread.start()
        else:
            xbmc.log(
                f"{_LOG_PREFIX} seek OK:"
                f" target={target:.3f}s achieved={achieved:.3f}s"
                f" strategy=StartTimeTicks"
                f" channel={channel_id!r} title={title!r}",
                xbmc.LOGINFO,
            )

        # 4. Prefetch next item into MediaCache (Requirement 28.1, 28.3).
        if self._media_cache:
            next_item = self._current_item.get("next_item")
            if next_item:
                next_jellyfin_id = next_item.get("jellyfin_id", "")
                next_url = next_item.get("direct_play_url", "")
                if next_jellyfin_id and next_url:
                    try:
                        self._media_cache.prefetch(next_jellyfin_id, next_url)
                        xbmc.log(
                            f"{_LOG_PREFIX} prefetch queued:"
                            f" jellyfin_id={next_jellyfin_id!r}"
                            f" title={next_item.get('title', '')!r}",
                            xbmc.LOGDEBUG,
                        )
                    except Exception as exc:
                        xbmc.log(
                            f"{_LOG_PREFIX} media_cache.prefetch() raised: {exc}",
                            xbmc.LOGWARNING,
                        )

    # ------------------------------------------------------------------
    # Internal seek helpers
    # ------------------------------------------------------------------

    def _seek_with_retry(self, target: float, attempts: int = 0) -> None:
        """Issue seekTime(target) and retry up to SEEK_MAX_RETRIES times.

        Implements Strategy B (Requirements 11.4, 11.5):
          - Retries up to SEEK_MAX_RETRIES times with SEEK_RETRY_INTERVAL_MS
            delay between attempts.
          - If the achieved position is within SEEK_TOLERANCE_SECONDS after a
            seek, the method returns successfully.
          - If retries are exhausted, logs an ERROR and returns — never raises.

        Args:
            target:   Target playback position in seconds.
            attempts: Number of attempts already made (for recursion tracking).
        """
        if attempts >= SEEK_MAX_RETRIES:
            try:
                achieved_str = f"{self.getTime():.1f}s"
            except Exception:  # noqa: BLE001
                achieved_str = "unknown (player stopped)"
            xbmc.log(
                f"{_LOG_PREFIX} seek failed after {SEEK_MAX_RETRIES} retries:"
                f" achieved={achieved_str} target={target:.1f}s"
                f" channel={self._current_channel_id!r}"
                f" title={self._current_item.get('title', '')!r}",
                xbmc.LOGERROR,
            )
            return

        # Requirement 42: bail out if a new play call has cleared the flag,
        # so stale seek threads don't interfere with the incoming episode.
        if not self._seek_in_progress:
            xbmc.log(
                f"{_LOG_PREFIX} seek thread stopping: _seek_in_progress cleared"
                f" (new play call started) channel={self._current_channel_id!r}",
                xbmc.LOGDEBUG,
            )
            return

        # Requirement 38: seekTime() throws RuntimeError on the Amlogic decoder
        # when the player state is not yet fully ready, even though
        # onPlayBackStarted has fired. Catch all exceptions, log a WARNING,
        # and count the attempt toward SEEK_MAX_RETRIES — never re-raise.
        try:
            self.seekTime(target)
        except Exception as exc:  # noqa: BLE001
            xbmc.log(
                f"{_LOG_PREFIX} seekTime() raised on attempt"
                f" {attempts + 1}/{SEEK_MAX_RETRIES}:"
                f" {exc}"
                f" channel={self._current_channel_id!r}"
                f" target={target:.1f}s — will retry",
                xbmc.LOGWARNING,
            )
            self._seek_with_retry(target, attempts + 1)
            return

        # Requirement 42: xbmc.sleep() and getTime() can both throw RuntimeError
        # when the Amlogic player stops between the seekTime() call and here.
        # Catch all exceptions from either call and return cleanly.
        try:
            xbmc.sleep(SEEK_RETRY_INTERVAL_MS)
            achieved = self.getTime()
        except Exception as exc:  # noqa: BLE001
            xbmc.log(
                f"{_LOG_PREFIX} getTime()/sleep() raised on attempt"
                f" {attempts + 1}/{SEEK_MAX_RETRIES}: {exc}"
                f" channel={self._current_channel_id!r} — player stopped, aborting seek",
                xbmc.LOGWARNING,
            )
            return
        if abs(achieved - target) > SEEK_TOLERANCE_SECONDS:
            xbmc.log(
                f"{_LOG_PREFIX} seek retry {attempts + 1}/{SEEK_MAX_RETRIES}:"
                f" target={target:.1f}s achieved={achieved:.1f}s"
                f" channel={self._current_channel_id!r}",
                xbmc.LOGDEBUG,
            )
            self._seek_with_retry(target, attempts + 1)
        else:
            xbmc.log(
                f"{_LOG_PREFIX} seek OK (Strategy B) after {attempts + 1} attempt(s):"
                f" target={target:.1f}s achieved={achieved:.1f}s"
                f" channel={self._current_channel_id!r}"
                f" title={self._current_item.get('title', '')!r}",
                xbmc.LOGINFO,
            )

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _fetch_schedule(self, channel_id: str, t: int, timeout: float = 5.0) -> "dict | None":
        """Make a single HTTP GET /schedule/{channel_id}?t={t} request.

        Returns the parsed JSON response dict, or None on any error.
        Uses self._schedule_service_url to construct the URL.
        """
        import json
        import urllib.request
        import urllib.error

        url = f"{self._schedule_service_url}/schedule/{channel_id}?t={t}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, json.JSONDecodeError, Exception) as exc:
            xbmc.log(
                f"{_LOG_PREFIX} schedule fetch failed"
                f" channel={channel_id!r} t={t}: {exc}",
                xbmc.LOGWARNING,
            )
            return None

    def _fetch_schedule_with_retry(
        self,
        channel_id: str,
        t: int,
        max_retries: int = EOF_RETRY_MAX,
        retry_interval: float = EOF_RETRY_INTERVAL_SECONDS,
    ) -> "dict | None":
        """Fetch schedule from the service, retrying on failure.

        Attempts up to max_retries times with retry_interval seconds between
        each attempt. Returns the parsed JSON response dict on the first
        success, or None after all attempts fail.

        Args:
            channel_id:    Channel ID to query.
            t:             Unix timestamp to pass as the ?t= parameter.
            max_retries:   Maximum number of attempts (default EOF_RETRY_MAX).
            retry_interval: Seconds to wait between attempts (default
                           EOF_RETRY_INTERVAL_SECONDS).
        """
        for attempt in range(1, max_retries + 1):
            response = self._fetch_schedule(channel_id, t)
            if response is not None:
                return response
            if attempt < max_retries:
                xbmc.log(
                    f"{_LOG_PREFIX} schedule fetch attempt {attempt}/{max_retries} failed,"
                    f" retrying in {retry_interval}s"
                    f" channel={channel_id!r}",
                    xbmc.LOGWARNING,
                )
                xbmc.sleep(int(retry_interval * 1000))
        xbmc.log(
            f"{_LOG_PREFIX} all {max_retries} schedule fetch attempts failed"
            f" channel={channel_id!r}",
            xbmc.LOGERROR,
        )
        return None

    # ------------------------------------------------------------------
    # EOF / retune callbacks (Requirements 7.1–7.5)
    # ------------------------------------------------------------------

    def onPlayBackEnded(self) -> None:  # noqa: N802
        """Called by Kodi when a media item reaches its end (EOF).

        Implements Requirement 7:
          - Fetch schedule for the current channel at the current wall-clock
            time within 500ms of EOF (Req 7.1).
          - On success, call play_schedule_item() (Req 7.2, 7.3).
          - On all retries failing (EOF_RETRY_MAX × EOF_RETRY_INTERVAL_SECONDS),
            play the Offline_Clip and retry every EOF_OFFLINE_RETRY_INTERVAL_SECONDS
            until the schedule service recovers (Req 7.4, 7.5).
        """
        channel_id = self._current_channel_id

        xbmc.log(
            f"{_LOG_PREFIX} onPlayBackEnded"
            f" channel={channel_id!r} — fetching schedule for retune",
            xbmc.LOGINFO,
        )

        t = int(time.time())
        response = self._fetch_schedule_with_retry(
            channel_id,
            t,
            max_retries=EOF_RETRY_MAX,
            retry_interval=EOF_RETRY_INTERVAL_SECONDS,
        )

        if response is not None:
            # Requirement 7.2: play the returned item at the returned seek offset.
            # Requirement 7.3: no local state — fresh query drives the decision.
            xbmc.log(
                f"{_LOG_PREFIX} EOF retune success"
                f" channel={channel_id!r} — calling play_schedule_item",
                xbmc.LOGINFO,
            )
            self.play_schedule_item(response)
            return

        # Requirement 7.4: all retries failed — fall back to Offline_Clip.
        xbmc.log(
            f"{_LOG_PREFIX} EOF retune failed after {EOF_RETRY_MAX} retries,"
            f" playing Offline_Clip"
            f" channel={channel_id!r}",
            xbmc.LOGWARNING,
        )
        self._play_offline_clip_and_retry(channel_id)

    def onPlayBackStopped(self) -> None:  # noqa: N802
        """Called by Kodi when playback is explicitly stopped.

        Delegates to onPlayBackEnded() for consistent EOF retune behavior.
        Only invoked in TV_Mode; Movie_Mode and Rachel_Mode stoppage is handled
        by their respective mode managers.
        """
        xbmc.log(
            f"{_LOG_PREFIX} onPlayBackStopped → delegating to EOF retune",
            xbmc.LOGINFO,
        )
        self.onPlayBackEnded()

    def _play_offline_clip_and_retry(self, channel_id: str) -> None:
        """Play the Offline_Clip and retry the schedule fetch every
        EOF_OFFLINE_RETRY_INTERVAL_SECONDS (30s) until the service recovers.

        Implements Requirement 7.5: once recovery succeeds, resumes normal
        Schedule_Item playback at the returned seek offset.

        This method returns immediately after starting the retry loop in a
        background thread so that the Kodi event loop is not blocked.
        """
        if self._offline_clip_url:
            xbmc.log(
                f"{_LOG_PREFIX} starting Offline_Clip playback"
                f" channel={channel_id!r}"
                f" url={self._offline_clip_url!r}",
                xbmc.LOGINFO,
            )
            listitem = xbmcgui.ListItem(label="Offline", path=self._offline_clip_url)
            listitem.setInfo("video", {"title": "Offline"})
            listitem.setProperty("IsPlayable", "true")
            self.play(self._offline_clip_url, listitem)
        else:
            xbmc.log(
                f"{_LOG_PREFIX} no Offline_Clip URL configured"
                f" channel={channel_id!r} — skipping offline playback",
                xbmc.LOGWARNING,
            )

        def _retry_loop() -> None:
            while True:
                xbmc.sleep(int(EOF_OFFLINE_RETRY_INTERVAL_SECONDS * 1000))
                xbmc.log(
                    f"{_LOG_PREFIX} retrying schedule fetch after offline fallback"
                    f" channel={channel_id!r}",
                    xbmc.LOGINFO,
                )
                t = int(time.time())
                response = self._fetch_schedule(channel_id, t)
                if response is not None:
                    xbmc.log(
                        f"{_LOG_PREFIX} schedule recovery succeeded"
                        f" channel={channel_id!r} — resuming normal playback",
                        xbmc.LOGINFO,
                    )
                    self.play_schedule_item(response)
                    return
                xbmc.log(
                    f"{_LOG_PREFIX} schedule still unavailable"
                    f" channel={channel_id!r} — continuing Offline_Clip",
                    xbmc.LOGWARNING,
                )

        t = threading.Thread(target=_retry_loop, daemon=True, name="classictv-offline-retry")
        t.start()

    # ------------------------------------------------------------------
    # Channel change (Requirements 8.1–8.8)
    # ------------------------------------------------------------------

    def handle_ch_up(self) -> None:
        """Navigate to the next channel in number order (Requirement 8).

        No-op when _channel_list is not set.
        """
        if self._channel_list is None:
            xbmc.log(
                f"{_LOG_PREFIX} handle_ch_up: channel_list not set — ignoring",
                xbmc.LOGWARNING,
            )
            return
        next_ch = self._channel_list.next_channel(self._current_channel_id)
        if next_ch:
            xbmc.log(
                f"{_LOG_PREFIX} handle_ch_up:"
                f" current={self._current_channel_id!r}"
                f" → next={next_ch.id!r}",
                xbmc.LOGINFO,
            )
            self._change_channel(next_ch.id)

    def handle_ch_down(self) -> None:
        """Navigate to the previous channel in number order (Requirement 8).

        No-op when _channel_list is not set.
        """
        if self._channel_list is None:
            xbmc.log(
                f"{_LOG_PREFIX} handle_ch_down: channel_list not set — ignoring",
                xbmc.LOGWARNING,
            )
            return
        prev_ch = self._channel_list.prev_channel(self._current_channel_id)
        if prev_ch:
            xbmc.log(
                f"{_LOG_PREFIX} handle_ch_down:"
                f" current={self._current_channel_id!r}"
                f" → prev={prev_ch.id!r}",
                xbmc.LOGINFO,
            )
            self._change_channel(prev_ch.id)

    def _change_channel(self, channel_id: str) -> None:
        """Execute the full channel change sequence (Requirements 8.1–8.8).

        Steps in order:
          1. Show Channel_Static overlay immediately — before any fetch or
             stop (Req 8.1, 12.8).
          2. Stop current playback.
          3. Poll isPlaying() every DECODER_RELEASE_POLL_INTERVAL_MS (200ms)
             for up to DECODER_RELEASE_POLL_TIMEOUT_SECONDS (2s) waiting for
             the Amlogic hardware decoder to release (Req 8.5).
          4. Fetch schedule for new channel with
             CHANNEL_CHANGE_FETCH_TIMEOUT_SECONDS (3s) timeout (Req 8.2).
          5a. On success: call play_schedule_item(); Channel_Static is
              dismissed automatically in onPlayBackStarted (Req 8.3).
          5b. On failure: dismiss Channel_Static, log "Channel unavailable",
              resume the previously tuned channel (Req 8.8).

        The Channel_Bug show (Req 8.6) happens inside onPlayBackStarted via
        self._channel_bug.show() — no special handling needed here.

        Args:
            channel_id: The channel ID to tune to.
        """
        previous_channel_id = self._current_channel_id

        xbmc.log(
            f"{_LOG_PREFIX} _change_channel:"
            f" from={previous_channel_id!r}"
            f" to={channel_id!r}",
            xbmc.LOGINFO,
        )

        # Step 1: Show Channel_Static BEFORE any fetch or stop (Req 8.1, 12.8).
        if self._channel_static is not None:
            try:
                self._channel_static.show()
            except Exception as exc:
                xbmc.log(
                    f"{_LOG_PREFIX} channel_static.show() raised: {exc}"
                    f" channel={channel_id!r}",
                    xbmc.LOGWARNING,
                )

        # Step 2: Stop current playback.
        self.stop()

        # Step 3: Poll isPlaying() for up to DECODER_RELEASE_POLL_TIMEOUT_SECONDS
        # to let the Amlogic hardware decoder release its lock (Req 8.5).
        poll_deadline = time.time() + DECODER_RELEASE_POLL_TIMEOUT_SECONDS
        while self.isPlaying() and time.time() < poll_deadline:
            xbmc.sleep(DECODER_RELEASE_POLL_INTERVAL_MS)

        if self.isPlaying():
            xbmc.log(
                f"{_LOG_PREFIX} decoder still held after"
                f" {DECODER_RELEASE_POLL_TIMEOUT_SECONDS}s poll —"
                f" proceeding with channel change regardless"
                f" channel={channel_id!r}",
                xbmc.LOGWARNING,
            )

        # Step 4: Fetch schedule for the new channel with a tight timeout
        # (Req 8.2, 8.7).
        t = int(time.time())
        response = self._fetch_schedule(
            channel_id,
            t,
            timeout=CHANNEL_CHANGE_FETCH_TIMEOUT_SECONDS,
        )

        # Step 5a: Success — begin playback. Channel_Static is dismissed inside
        # onPlayBackStarted (Req 8.3).
        if response is not None:
            xbmc.log(
                f"{_LOG_PREFIX} channel change fetch succeeded"
                f" channel={channel_id!r} — calling play_schedule_item",
                xbmc.LOGINFO,
            )
            self.play_schedule_item(response)
            return

        # Step 5b: Failure — dismiss Channel_Static, show error, resume previous
        # channel (Req 8.8).
        xbmc.log(
            f"{_LOG_PREFIX} channel change fetch failed or timed out"
            f" channel={channel_id!r} —"
            f" dismissing static and resuming previous channel={previous_channel_id!r}",
            xbmc.LOGWARNING,
        )

        if self._channel_static is not None:
            try:
                self._channel_static.close()
            except Exception as exc:
                xbmc.log(
                    f"{_LOG_PREFIX} channel_static.close() raised: {exc}"
                    f" channel={channel_id!r}",
                    xbmc.LOGWARNING,
                )

        # Show a brief "Channel unavailable" notification.
        try:
            dialog = xbmcgui.Dialog()
            dialog.notification(
                "Classic TV",
                "Channel unavailable",
                xbmcgui.NOTIFICATION_WARNING,
                3000,
                False,
            )
        except Exception as exc:
            xbmc.log(
                f"{_LOG_PREFIX} notification dialog raised: {exc}"
                f" channel={channel_id!r}",
                xbmc.LOGWARNING,
            )

        # Resume the previously tuned channel if we have one.
        if previous_channel_id:
            t_resume = int(time.time())
            resume_response = self._fetch_schedule(previous_channel_id, t_resume)
            if resume_response is not None:
                xbmc.log(
                    f"{_LOG_PREFIX} resuming previous channel={previous_channel_id!r}",
                    xbmc.LOGINFO,
                )
                self.play_schedule_item(resume_response)
            else:
                xbmc.log(
                    f"{_LOG_PREFIX} failed to resume previous channel={previous_channel_id!r}",
                    xbmc.LOGERROR,
                )

    # ------------------------------------------------------------------
    # Watchdog (Requirements 9.1–9.5)
    # ------------------------------------------------------------------

    def start_watchdog(self) -> None:
        """Start the background watchdog thread.

        Spawns a daemon thread running _watchdog_loop(). Call once from
        service.py after all modules are wired up. The thread runs for the
        lifetime of the process.
        """
        t = threading.Thread(
            target=self._watchdog_loop,
            daemon=True,
            name="classictv-watchdog",
        )
        t.start()
        xbmc.log(
            f"{_LOG_PREFIX} watchdog thread started"
            f" interval={WATCHDOG_INTERVAL_SECONDS}s",
            xbmc.LOGINFO,
        )

    def _watchdog_loop(self) -> None:
        """Background loop: check playback every WATCHDOG_INTERVAL_SECONDS.

        Implements Requirements 9.1–9.5:
          - Wakes every WATCHDOG_INTERVAL_SECONDS (10s).
          - If _modes.current != TV_MODE (i.e. Movie_Mode or Rachel_Mode),
            skip the resume action and continue the loop (Req 9.3).
          - If isPlaying() is False: fetch schedule and restart playback
            (Req 9.2).
          - On fetch failure, retry once after 5 seconds; if the retry also
            fails, skip until the next interval (Req 9.4).
          - No loop detection, hold management, or thread lifecycle beyond
            the check-and-resume (Req 9.5).
        """
        TV_MODE = "tv"
        WATCHDOG_RETRY_DELAY_SECONDS = 5

        while True:
            xbmc.sleep(int(WATCHDOG_INTERVAL_SECONDS * 1000))

            # Req 9.3: skip resume when not in TV_Mode.
            if self._modes is not None and getattr(self._modes, "current", TV_MODE) != TV_MODE:
                xbmc.log(
                    f"{_LOG_PREFIX} watchdog: skipping recovery"
                    f" mode={getattr(self._modes, 'current', '?')!r}"
                    f" (not TV_Mode)",
                    xbmc.LOGDEBUG,
                )
                continue

            if self.isPlaying():
                continue

            channel_id = self._current_channel_id
            xbmc.log(
                f"{_LOG_PREFIX} watchdog: playback stopped —"
                f" fetching schedule for recovery"
                f" channel={channel_id!r}",
                xbmc.LOGWARNING,
            )

            # Req 9.2: fetch and restart.
            t = int(time.time())
            response = self._fetch_schedule(channel_id, t)

            if response is not None:
                xbmc.log(
                    f"{_LOG_PREFIX} watchdog recovery succeeded"
                    f" channel={channel_id!r} — restarting playback",
                    xbmc.LOGINFO,
                )
                self.play_schedule_item(response)
                continue

            # Req 9.4: first fetch failed — retry once after 5 seconds.
            xbmc.log(
                f"{_LOG_PREFIX} watchdog recovery: first fetch failed,"
                f" retrying in {WATCHDOG_RETRY_DELAY_SECONDS}s"
                f" channel={channel_id!r}",
                xbmc.LOGWARNING,
            )
            xbmc.sleep(int(WATCHDOG_RETRY_DELAY_SECONDS * 1000))

            t = int(time.time())
            response = self._fetch_schedule(channel_id, t)

            if response is not None:
                xbmc.log(
                    f"{_LOG_PREFIX} watchdog recovery (retry) succeeded"
                    f" channel={channel_id!r} — restarting playback",
                    xbmc.LOGINFO,
                )
                self.play_schedule_item(response)
            else:
                # Req 9.4: both attempts failed — give up until next interval.
                xbmc.log(
                    f"{_LOG_PREFIX} watchdog recovery failed after retry —"
                    f" will try again at next interval"
                    f" channel={channel_id!r}",
                    xbmc.LOGERROR,
                )
