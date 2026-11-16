"""
Pure-function schedule computation engine for Classic TV 3.0.

This module contains only pure functions with no I/O, no network calls,
and no global mutable state. All functions are deterministic: the same
inputs always produce the same outputs regardless of how many times they
are called or from which thread.

Functions intended for external use:
    expand_block_episodes(block) -> list[EpisodeSlot]
    compute_cycle_duration(channel, t) -> float
    compute_schedule(channel, t) -> ScheduleItem
"""

from __future__ import annotations

import os
from datetime import datetime, date, timedelta

import pytz

from server.models import Block, Channel, EpisodeSlot, ScheduleItem

# ---------------------------------------------------------------------------
# Timezone constant
# ---------------------------------------------------------------------------

_CHICAGO_TZ = pytz.timezone("America/Chicago")


# ---------------------------------------------------------------------------
# expand_block_episodes
# ---------------------------------------------------------------------------


def expand_block_episodes(block: Block) -> list[EpisodeSlot]:
    """Return one complete weighted rotation cycle for *block*.

    The list contains ``sum(show.weight for show in block.shows)`` slots,
    interleaved using the Weighted Round-Robin (WRR) algorithm so that
    shows appear proportionally and evenly spread across the cycle.

    Episodes within each show advance in order (S01E01, S01E02, …) and
    wrap back to index 0 after the last episode.

    Returns an empty list when ``block.offline`` is True or when
    ``block.shows`` is None or empty.

    Args:
        block: The :class:`~server.models.Block` to expand.

    Returns:
        A deterministic, ordered list of :class:`~server.models.EpisodeSlot`
        objects representing one full cycle.
    """
    if block.offline or not block.shows:
        return []

    shows = block.shows
    total_slots = sum(s.weight for s in shows)
    n = len(shows)

    # WRR state: deficit[i] starts at weight[i] / total_slots for each show,
    # so the highest-weight show is picked first without ties on the very first
    # slot.  Each round: pick argmax(deficit), emit an episode, subtract 1,
    # then add weight[i]/total_slots to every show's deficit.
    deficit = [s.weight / total_slots for s in shows]

    # Per-show episode cursor (index into jellyfin_ids list)
    cursors = [0] * n

    slots: list[EpisodeSlot] = []

    for _ in range(total_slots):
        # Pick the show with the highest deficit (stable: lowest index wins ties)
        best_idx = 0
        best_val = deficit[0]
        for i in range(1, n):
            if deficit[i] > best_val:
                best_val = deficit[i]
                best_idx = i

        show = shows[best_idx]

        # Emit one episode from this show
        ep_idx = cursors[best_idx] % len(show.jellyfin_ids)
        jellyfin_id = show.jellyfin_ids[ep_idx]
        cursors[best_idx] += 1

        slots.append(
            EpisodeSlot(
                jellyfin_id=jellyfin_id,
                title=show.name,
                duration=0.0,   # Duration is resolved externally (from Jellyfin media info)
                show_name=show.name,
            )
        )

        # Update deficits
        deficit[best_idx] -= 1.0
        for i in range(n):
            deficit[i] += shows[i].weight / total_slots

    return slots


# ---------------------------------------------------------------------------
# Active block helpers
# ---------------------------------------------------------------------------


def _to_chicago_datetime(t: int) -> datetime:
    """Convert a Unix timestamp to America/Chicago local time.

    DST fall-back ambiguity is resolved by always choosing the first
    (pre-transition) occurrence of an ambiguous wall-clock hour, which is
    consistent with Requirement 4.10 and the design's DST rule.

    Args:
        t: Unix timestamp in integer seconds.

    Returns:
        A timezone-aware :class:`datetime` in America/Chicago.
    """
    utc_dt = datetime.fromtimestamp(t, tz=pytz.utc)
    return utc_dt.astimezone(_CHICAGO_TZ)


def _block_is_active(block: Block, local_hour: int) -> bool:
    """Return True if *local_hour* falls within *block*'s active window.

    For normal blocks (``end_hour > start_hour``):
        active when ``start_hour <= local_hour < end_hour``

    For midnight-crossing blocks (``end_hour < start_hour``), clocks wrap:
        active when ``local_hour >= start_hour OR local_hour < end_hour``

    The block boundary rule (Requirement 4.7) is: a timestamp exactly at
    ``start_hour`` belongs to the block that starts at that hour.

    Args:
        block: The block to test.
        local_hour: The 0-23 hour value in America/Chicago local time.

    Returns:
        True if the block covers *local_hour*.
    """
    s = block.start_hour
    e = block.end_hour

    if e > s:
        # Normal block (no midnight crossing)
        return s <= local_hour < e
    else:
        # Midnight-crossing block
        return local_hour >= s or local_hour < e


def _find_active_block(channel: Channel, local_hour: int) -> Block | None:
    """Return the first block in *channel* that is active at *local_hour*, or None.

    When no block covers the given hour, None is returned and the caller
    should substitute an Offline_Clip.

    Args:
        channel: The channel whose blocks are searched.
        local_hour: The 0-23 hour in America/Chicago local time.

    Returns:
        The active :class:`~server.models.Block`, or None.
    """
    for block in channel.blocks:
        if _block_is_active(block, local_hour):
            return block
    return None


def _block_start_epoch(block: Block, local_dt: datetime) -> float:
    """Compute the Unix timestamp at which *block*'s current window started.

    For normal blocks the start is at ``(today, start_hour, 0, 0)`` in
    America/Chicago.  For midnight-crossing blocks the window may have
    started on the previous calendar day.

    DST handling: pytz ``localize(is_dst=True)`` always picks the
    pre-transition occurrence of an ambiguous hour, consistent with the
    design's fall-back rule (Requirement 4.10).

    Args:
        block: The block whose start epoch is needed.
        local_dt: The current America/Chicago datetime (already in the block's
                  active window).

    Returns:
        A float Unix timestamp for the moment the block window opened.
    """
    today: date = local_dt.date()
    local_hour = local_dt.hour

    if block.end_hour > block.start_hour:
        # Normal block — starts today at start_hour
        naive_start = datetime(today.year, today.month, today.day, block.start_hour, 0, 0)
    else:
        # Midnight-crossing block
        if local_hour >= block.start_hour:
            # We are before midnight — block started today
            naive_start = datetime(today.year, today.month, today.day, block.start_hour, 0, 0)
        else:
            # We are after midnight (local_hour < end_hour) — block started yesterday
            yesterday: date = today - timedelta(days=1)
            naive_start = datetime(
                yesterday.year, yesterday.month, yesterday.day, block.start_hour, 0, 0
            )

    # Localize with is_dst=True to always select pre-transition on DST fall-back
    aware_start = _CHICAGO_TZ.localize(naive_start, is_dst=True)
    return aware_start.timestamp()


# ---------------------------------------------------------------------------
# compute_cycle_duration
# ---------------------------------------------------------------------------


def compute_cycle_duration(channel: Channel, t: int) -> float:
    """Return the total duration in seconds of one full episode cycle for the
    active block at timestamp *t*.

    The cycle duration is the sum of :attr:`EpisodeSlot.duration` for every
    slot returned by :func:`expand_block_episodes` on the block that is
    active at *t*.

    When the active block is an offline block, or when no block covers *t*,
    the function returns ``0.0`` (there is no meaningful cycle to rotate
    through).

    Note:
        :func:`expand_block_episodes` populates ``duration`` as ``0.0``
        because individual episode durations are only known after a Jellyfin
        lookup.  Callers that need real cycle durations (i.e., the full
        ``compute_schedule`` implementation in task 3.2) must set
        ``slot.duration`` from their media-info cache before computing the
        sum.  This function sums whatever values are present on the slots,
        making it straightforward to test with synthetic durations.

    Args:
        channel: The channel configuration.
        t: Unix timestamp in integer seconds.

    Returns:
        Sum of all episode slot durations for one cycle of the active block.
        Returns ``0.0`` for offline or uncovered blocks.
    """
    local_dt = _to_chicago_datetime(t)
    block = _find_active_block(channel, local_dt.hour)

    if block is None or block.offline:
        return 0.0

    slots = expand_block_episodes(block)
    return sum(slot.duration for slot in slots)


# ---------------------------------------------------------------------------
# compute_schedule
# ---------------------------------------------------------------------------

#: Fallback duration (seconds) used when a jellyfin_id is absent from the
#: episode_durations mapping.  30 minutes matches a standard half-hour slot.
_FALLBACK_DURATION_SECONDS: float = 1800.0


def _make_offline_clip() -> ScheduleItem:
    """Return an Offline_Clip ScheduleItem using the current env configuration."""
    offline_id = os.environ.get("OFFLINE_CLIP_JELLYFIN_ID", "OFFLINE")
    return ScheduleItem(
        jellyfin_id=offline_id,
        title="Offline",
        type="offline",
        seek_offset=0.0,
        duration=0.0,
        direct_play_url="",
    )


def compute_schedule(
    channel: Channel,
    t: int,
    episode_durations: dict[str, float] | None = None,
) -> ScheduleItem:
    """Compute the current schedule item for *channel* at Unix timestamp *t*.

    The algorithm follows the design specification:

    1. Convert *t* to America/Chicago local time.
    2. Find the active :class:`~server.models.Block` for the local hour.
       Midnight-crossing blocks (``end_hour < start_hour``) are handled
       by :func:`_block_is_active`.
    3. No active block or an offline block → return :func:`_make_offline_clip`.
    4. Compute ``block_start_epoch`` via :func:`_block_start_epoch`.
    5. ``elapsed = t - block_start_epoch``
    6. Build the expanded episode list; attach real durations from
       *episode_durations* (falls back to :data:`_FALLBACK_DURATION_SECONDS`
       for missing IDs).
    7. ``cycle_duration = sum(slot.duration for slot in slots)``
       If ``cycle_duration == 0.0`` → return Offline_Clip.
    8. ``position = elapsed % cycle_duration``
    9. Walk slots: subtract each slot's duration from *position*; the slot
       that makes *position* go negative is the current item.
       ``seek_offset = slot.duration + position``

    **Guarantee:** ``0 <= seek_offset < item.duration`` holds for all valid
    channels and timestamps.

    The returned :class:`~server.models.ScheduleItem` has:
    - ``type = "episode"`` (schedule service may enrich bumpers later)
    - ``direct_play_url = ""`` (filled by the schedule service)

    Args:
        channel: The channel configuration to evaluate.
        t: Unix timestamp in integer seconds.
        episode_durations: Optional mapping of ``jellyfin_id → duration_seconds``.
            When provided, used to resolve each slot's duration.  Missing IDs
            fall back to :data:`_FALLBACK_DURATION_SECONDS` (1800 s).

    Returns:
        A :class:`~server.models.ScheduleItem` for the item currently airing.
        Returns an Offline_Clip item when no active programme is scheduled.
    """
    # Step 1 — resolve local time
    local_dt = _to_chicago_datetime(t)

    # Step 2 — find active block
    block = _find_active_block(channel, local_dt.hour)

    # Step 3 — no block or explicit offline block
    if block is None or block.offline:
        return _make_offline_clip()

    # Step 4 — block start epoch
    block_start = _block_start_epoch(block, local_dt)

    # Step 5 — elapsed seconds since block started
    elapsed = t - block_start

    # Step 6 — expand episodes and attach durations
    slots = expand_block_episodes(block)
    if not slots:
        return _make_offline_clip()

    durations_map: dict[str, float] = episode_durations or {}
    resolved_slots: list[EpisodeSlot] = []
    for slot in slots:
        dur = durations_map.get(slot.jellyfin_id, _FALLBACK_DURATION_SECONDS)
        resolved_slots.append(
            EpisodeSlot(
                jellyfin_id=slot.jellyfin_id,
                title=slot.title,
                duration=dur,
                show_name=slot.show_name,
            )
        )

    # Step 7 — cycle duration; guard against zero (e.g. all durations resolved
    # to 0.0 somehow)
    cycle_duration = sum(s.duration for s in resolved_slots)
    if cycle_duration <= 0.0:
        return _make_offline_clip()

    # Step 8 — position within the current cycle
    position = elapsed % cycle_duration

    # Step 9 — walk the episode list to find the current item
    for slot in resolved_slots:
        position -= slot.duration
        if position < 0:
            # seek_offset = how far into the episode we are
            seek_offset = slot.duration + position  # position is negative here

            # Clamp to guarantee 0 <= seek_offset < duration (floating-point
            # edge cases near cycle boundaries could produce tiny negatives or
            # values exactly equal to duration).
            seek_offset = max(0.0, min(seek_offset, slot.duration - 1e-9))

            return ScheduleItem(
                jellyfin_id=slot.jellyfin_id,
                title=slot.title,
                type="episode",
                seek_offset=seek_offset,
                duration=slot.duration,
                direct_play_url="",
            )

    # Should be unreachable: floating-point drift could leave position at
    # exactly 0.0 after the final slot.  Return the last slot at seek_offset=0.
    last = resolved_slots[-1]
    return ScheduleItem(
        jellyfin_id=last.jellyfin_id,
        title=last.title,
        type="episode",
        seek_offset=0.0,
        duration=last.duration,
        direct_play_url="",
    )
