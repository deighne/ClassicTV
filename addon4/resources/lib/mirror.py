"""
Mirror channels for Classic TV 4.0.

A mirror channel plays whatever a chosen Jellyfin user is watching right now,
at their position, instead of following a schedule. The plugin serves it through
the normal ``GET /ClassicTV/channel/{id}`` endpoint (``Mirror: true``); this
module keeps the viewer in step while the channel is tuned.

Classic TV never seeks, so staying in step works by re-issuing the play command
at the corrected position (the same ``PlayMedia`` path a channel change uses):

  * the mirrored user starts something else  -> play the new item
  * we drift too far from their position      -> play it again at their position
  * playback stopped on our side              -> play it again
  * they pause                                -> do nothing, keep playing
  * they stop for good                        -> say so and go back to TV

Starting a stream takes several seconds (Jellyfin addon start-up), so every
retune starts that much ahead of the mirrored position; otherwise we would land
that far behind and retune forever.
"""

from __future__ import annotations

import dataclasses
import threading

import xbmc

from constants import (
    MIRROR_DEFAULT_LEAD_SECONDS,
    MIRROR_DRIFT_RETUNE_SECONDS,
    MIRROR_IDLE_POLLS_BEFORE_LEAVING,
    MIRROR_MAX_LEAD_SECONDS,
    MIRROR_MIN_LEAD_SECONDS,
    MIRROR_POLL_INTERVAL_SECONDS,
)
from schedule import ScheduleSlot

_LOG_PREFIX = "[ClassicTV][mirror]"

OK = "ok"
RETUNE = "retune"
IDLE = "idle"


def decide(
    slot: ScheduleSlot | None,
    playing_item_id: str,
    playing_time: float | None,
    is_playing: bool,
    in_grace: bool,
    lead: float,
    drift_threshold: float = MIRROR_DRIFT_RETUNE_SECONDS,
) -> str:
    """What to do for the current mirrored slot: OK, RETUNE or IDLE.

    *playing_time* is the player's position in the current item, or None when
    unknown. *lead* is how far ahead of the mirrored position a retune starts, so
    the expected position of the stream is ``slot.start_offset`` once it is up.
    """
    if slot is None:
        return OK  # server unreachable: keep what is playing
    if not slot.jellyfin_item_id:
        return IDLE
    if slot.paused:
        return OK  # follow nothing while they are paused; drift is expected
    if slot.jellyfin_item_id != playing_item_id:
        return RETUNE
    if in_grace:
        return OK  # a retune is still starting up
    if not is_playing:
        return RETUNE
    if playing_time is not None and abs(playing_time - slot.start_offset) > drift_threshold:
        return RETUNE
    return OK


def lead_seconds(last_launch_seconds: float | None) -> float:
    """How far ahead of the mirrored position to start a retune."""
    if not last_launch_seconds or last_launch_seconds <= 0:
        return MIRROR_DEFAULT_LEAD_SECONDS
    return max(MIRROR_MIN_LEAD_SECONDS, min(MIRROR_MAX_LEAD_SECONDS, last_launch_seconds))


def with_lead(slot: ScheduleSlot, lead: float) -> ScheduleSlot:
    """Copy of *slot* started *lead* seconds ahead (kept inside the item)."""
    offset = slot.start_offset + lead
    if slot.duration > 1:
        offset = min(offset, slot.duration - 1)
    return dataclasses.replace(slot, start_offset=max(offset, 0.0))


class MirrorSync:
    """Background loop that keeps playback in step with a mirrored user.

    Runs only while a mirror channel is tuned in TV mode. ``start()`` replaces any
    previous loop; ``stop()`` is safe to call from anywhere, including the loop's
    own thread.
    """

    def __init__(self, player, interval: float = MIRROR_POLL_INTERVAL_SECONDS) -> None:
        self._player = player
        self._interval = interval
        self._stop = threading.Event()
        self._stop.set()
        self._lock = threading.Lock()

    def start(self, channel_id: str) -> None:
        with self._lock:
            self._stop.set()  # end any previous loop
            stop = threading.Event()
            self._stop = stop
        t = threading.Thread(
            target=self._run,
            args=(channel_id, stop),
            daemon=True,
            name="classictv-mirror",
        )
        t.start()
        xbmc.log(f"{_LOG_PREFIX} sync started channel={channel_id!r}", xbmc.LOGINFO)

    def stop(self) -> None:
        with self._lock:
            if not self._stop.is_set():
                xbmc.log(f"{_LOG_PREFIX} sync stopped", xbmc.LOGINFO)
            self._stop.set()

    @property
    def running(self) -> bool:
        return not self._stop.is_set()

    def _run(self, channel_id: str, stop: threading.Event) -> None:
        idle_polls = 0
        while not stop.wait(self._interval):
            try:
                idle_polls = self._tick(channel_id, idle_polls, stop)
            except Exception as exc:  # never let the loop die silently
                xbmc.log(f"{_LOG_PREFIX} tick raised: {exc}", xbmc.LOGWARNING)

    def _tick(self, channel_id: str, idle_polls: int, stop: threading.Event) -> int:
        player = self._player
        if player.is_user_browsing():
            return idle_polls
        client = player._schedule_client
        if client is None:
            return idle_polls
        slot = client.get_slot(channel_id)

        playing_time = None
        is_playing = player.isPlaying()
        if is_playing:
            try:
                playing_time = float(player.getTime())
            except Exception:
                playing_time = None

        lead = lead_seconds(player.last_launch_seconds())
        action = decide(
            slot,
            playing_item_id=player._last_played_item_id,
            playing_time=playing_time,
            is_playing=is_playing,
            in_grace=player.is_in_grace_period(),
            lead=lead,
        )

        if action == IDLE:
            idle_polls += 1
            if idle_polls >= MIRROR_IDLE_POLLS_BEFORE_LEAVING and not stop.is_set():
                xbmc.log(f"{_LOG_PREFIX} mirrored user is idle — leaving", xbmc.LOGINFO)
                player.leave_mirror(channel_id)
            return idle_polls

        if action == RETUNE and slot is not None and not stop.is_set():
            xbmc.log(
                f"{_LOG_PREFIX} retune item={slot.jellyfin_item_id!r}"
                f" position={slot.start_offset:.1f}s lead={lead:.1f}s"
                f" playing_time={playing_time}",
                xbmc.LOGINFO,
            )
            player.mirror_retune(with_lead(slot, lead))
        return 0
