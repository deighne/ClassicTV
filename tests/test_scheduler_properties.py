"""
Property-based tests for server/scheduler.py using Hypothesis.

Feature: classic-tv-3
Properties covered:
    Property 1: Seek Offset In Bounds
    Property 2: Cycle Invariant
    Property 3: Non-Negative Seek Offset
"""

from __future__ import annotations

import string

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from server.models import Block, Channel, Show
from server.scheduler import (
    _find_active_block,
    _to_chicago_datetime,
    compute_schedule,
    expand_block_episodes,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Floating-point tolerance for cycle-invariant seek comparison (seconds).
# Matches SEEK_TOLERANCE_SECONDS from addon constants.
SEEK_TOLERANCE_SECONDS: float = 2.0

# Timestamp range: 2020-01-01 to 2030-01-01 UTC (epoch seconds).
_T_MIN = 1577836800
_T_MAX = 1893456000

# ---------------------------------------------------------------------------
# Shared strategy helpers
# ---------------------------------------------------------------------------

_SAFE_CHARS = string.ascii_letters + string.digits + " _-.()/!?"

_safe_text = st.text(
    alphabet=_SAFE_CHARS,
    min_size=1,
    max_size=64,
).filter(lambda s: s.strip() != "")

_jellyfin_id = st.text(
    alphabet=string.ascii_lowercase + string.digits + "-",
    min_size=1,
    max_size=36,
)


@st.composite
def _show_strategy(draw: st.DrawFn) -> Show:
    """Generate a valid Show dataclass with 1–3 Jellyfin IDs."""
    name = draw(_safe_text)
    weight = draw(st.integers(min_value=1, max_value=10))
    n_ids = draw(st.integers(min_value=1, max_value=3))
    jellyfin_ids = draw(st.lists(_jellyfin_id, min_size=n_ids, max_size=n_ids))
    return Show(name=name, weight=weight, jellyfin_ids=jellyfin_ids)


@st.composite
def _channel_with_durations(draw: st.DrawFn) -> tuple[Channel, dict[str, float], int]:
    """Generate a (Channel, episode_durations, t) triple for scheduler testing.

    Channel layout:
      - Either one block covering [0, 24), or two blocks covering [0, 12) and [12, 24).
      - Each block is either offline or has 1–2 shows.

    episode_durations maps every jellyfin_id that appears in the channel's
    shows to a duration in [60.0, 7200.0] seconds.

    t is a Unix timestamp in the range 2020-01-01 to 2030-01-01 UTC.
    """
    channel_id = draw(
        st.text(alphabet=_SAFE_CHARS, min_size=1, max_size=32).filter(
            lambda s: s.strip() != ""
        )
    )
    channel_name = draw(
        st.text(alphabet=_SAFE_CHARS, min_size=1, max_size=64).filter(
            lambda s: s.strip() != ""
        )
    )
    number = draw(st.integers(min_value=1, max_value=9999))
    hex_chars = string.digits + "abcdef"
    color = "#" + draw(st.text(alphabet=hex_chars, min_size=6, max_size=6))

    # Choose one-block or two-block layout.
    two_blocks = draw(st.booleans())

    if two_blocks:
        hour_ranges = [(0, 12), (12, 24)]
    else:
        hour_ranges = [(0, 24)]

    blocks: list[Block] = []
    all_jellyfin_ids: list[str] = []

    for start, end in hour_ranges:
        is_offline = draw(st.booleans())
        block_name = draw(_safe_text)

        if is_offline:
            blocks.append(
                Block(
                    name=block_name,
                    start_hour=start,
                    end_hour=end,
                    shows=None,
                    offline=True,
                )
            )
        else:
            n_shows = draw(st.integers(min_value=1, max_value=2))
            shows = draw(st.lists(_show_strategy(), min_size=n_shows, max_size=n_shows))
            for show in shows:
                all_jellyfin_ids.extend(show.jellyfin_ids)
            blocks.append(
                Block(
                    name=block_name,
                    start_hour=start,
                    end_hour=end,
                    shows=shows,
                    offline=False,
                )
            )

    channel = Channel(
        id=channel_id,
        name=channel_name,
        number=number,
        color=color,
        blocks=blocks,
    )

    # Build episode_durations for all IDs that appear in the channel.
    # Use integer-valued durations (as floats) so that cycle_duration is
    # always an exact integer, making t + cycle_duration land at the same
    # position within the cycle without floating-point truncation error.
    episode_durations: dict[str, float] = {}
    for jid in all_jellyfin_ids:
        if jid not in episode_durations:
            episode_durations[jid] = float(
                draw(st.integers(min_value=60, max_value=7200))
            )

    t = draw(st.integers(min_value=_T_MIN, max_value=_T_MAX))

    return channel, episode_durations, t


# ---------------------------------------------------------------------------
# Property 1: Seek Offset In Bounds
# ---------------------------------------------------------------------------


@given(triple=_channel_with_durations())
@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_seek_offset_in_bounds(triple: tuple[Channel, dict[str, float], int]) -> None:
    """Feature: classic-tv-3, Property 1: Seek Offset In Bounds

    For all valid channels and timestamps:
        0 <= seek_offset < item.duration

    Offline items (type == "offline") have seek_offset == 0.0 and
    duration == 0.0 by definition; the bounds check is skipped for those.

    Validates: Requirements 4.3, 4.8, 30.3, 30.4
    """
    channel, episode_durations, t = triple
    item = compute_schedule(channel, t, episode_durations)

    if item.type == "offline":
        # Offline clips always report seek_offset=0.0 and duration=0.0.
        assert item.seek_offset == 0.0, (
            f"Offline item seek_offset expected 0.0, got {item.seek_offset}"
        )
        return

    assert item.duration > 0.0, (
        f"Non-offline item must have positive duration; got {item.duration}"
    )
    assert item.seek_offset >= 0.0, (
        f"seek_offset must be >= 0; got {item.seek_offset} for jellyfin_id={item.jellyfin_id}"
    )
    assert item.seek_offset < item.duration, (
        f"seek_offset must be < duration; got seek_offset={item.seek_offset}, "
        f"duration={item.duration} for jellyfin_id={item.jellyfin_id}"
    )


# ---------------------------------------------------------------------------
# Property 2: Cycle Invariant
# ---------------------------------------------------------------------------


@given(triple=_channel_with_durations())
@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_cycle_invariant(triple: tuple[Channel, dict[str, float], int]) -> None:
    """Feature: classic-tv-3, Property 2: Cycle Invariant

    For any valid channel and timestamp t where the active block is NOT offline:
        compute_schedule(channel, t) and compute_schedule(channel, t + cycle_duration)
        return the same jellyfin_id, and their seek offsets differ by less than
        SEEK_TOLERANCE_SECONDS (2.0 seconds).

    cycle_duration is the sum of all slot durations from expand_block_episodes
    on the active block, using the same episode_durations dict.

    This test constrains t so that t and t + cycle_duration share the same
    block_start_epoch (i.e., the cycle does not cross a calendar day boundary
    for the active block's window).

    Validates: Requirements 4.4, 30.3, 30.5
    """
    from server.scheduler import _block_start_epoch

    channel, episode_durations, t = triple

    local_dt = _to_chicago_datetime(t)
    active_block = _find_active_block(channel, local_dt.hour)

    # Skip when no block is active or the block is offline.
    assume(active_block is not None)
    assume(not active_block.offline)

    # Build expanded slots with resolved durations.
    slots = expand_block_episodes(active_block)
    assume(len(slots) > 0)

    # Build resolved slots using the episode_durations map.
    from server.models import EpisodeSlot
    resolved_slots: list[EpisodeSlot] = []
    for slot in slots:
        dur = episode_durations.get(slot.jellyfin_id, 1800.0)
        resolved_slots.append(
            EpisodeSlot(
                jellyfin_id=slot.jellyfin_id,
                title=slot.title,
                duration=dur,
                show_name=slot.show_name,
            )
        )

    cycle_duration = sum(s.duration for s in resolved_slots)
    assume(cycle_duration > 0.0)

    # Because episode durations are integer-valued floats, cycle_duration is
    # always an exact integer — safe to round and cast without precision loss.
    cycle_seconds = round(cycle_duration)
    assume(cycle_seconds > 0)

    t2 = t + cycle_seconds

    # The cycle invariant only holds when t and t + cycle_duration share the
    # same block_start_epoch.  _block_start_epoch resets at calendar midnight
    # (America/Chicago), so advancing by cycle_duration may cross into the next
    # calendar day and produce a different reference point.  Skip such cases.
    local_dt2 = _to_chicago_datetime(t2)
    active_block2 = _find_active_block(channel, local_dt2.hour)
    assume(active_block2 is active_block)

    block_start_t = _block_start_epoch(active_block, local_dt)
    block_start_t2 = _block_start_epoch(active_block, local_dt2)
    assume(block_start_t == block_start_t2)

    item_at_t = compute_schedule(channel, t, episode_durations)
    item_at_t2 = compute_schedule(channel, t2, episode_durations)

    # Both results must be non-offline (since the active block is not offline).
    assume(item_at_t.type != "offline")
    assume(item_at_t2.type != "offline")

    assert item_at_t.jellyfin_id == item_at_t2.jellyfin_id, (
        f"Cycle invariant broken: jellyfin_id at t={t} is {item_at_t.jellyfin_id!r}, "
        f"but at t+cycle_duration={t2} is {item_at_t2.jellyfin_id!r} "
        f"(cycle_duration={cycle_duration:.3f}s)"
    )

    seek_diff = abs(item_at_t.seek_offset - item_at_t2.seek_offset)
    assert seek_diff < SEEK_TOLERANCE_SECONDS, (
        f"Seek offsets differ by {seek_diff:.6f}s (tolerance={SEEK_TOLERANCE_SECONDS}s) "
        f"for jellyfin_id={item_at_t.jellyfin_id!r}: "
        f"seek_offset@t={item_at_t.seek_offset:.6f}, "
        f"seek_offset@t+cycle={item_at_t2.seek_offset:.6f}"
    )


# ---------------------------------------------------------------------------
# Property 3: Non-Negative Seek Offset
# ---------------------------------------------------------------------------


@given(triple=_channel_with_durations())
@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_nonnegative_seek_offset(triple: tuple[Channel, dict[str, float], int]) -> None:
    """Feature: classic-tv-3, Property 3: Non-Negative Seek Offset

    For all valid channels and timestamps:
        seek_offset >= 0

    This holds for offline items too (they return seek_offset=0.0).

    Validates: Requirements 4.8, 30.3
    """
    channel, episode_durations, t = triple
    item = compute_schedule(channel, t, episode_durations)

    assert item.seek_offset >= 0.0, (
        f"seek_offset must be non-negative; got {item.seek_offset} "
        f"for jellyfin_id={item.jellyfin_id!r}, type={item.type!r}"
    )
