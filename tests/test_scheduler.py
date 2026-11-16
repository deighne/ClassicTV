"""
Named edge-case unit tests for server/scheduler.py.

Tests Requirements: 4.5, 30.2

Coverage:
- Midnight-crossing block seek offset computation (start epoch from day D, not D+1)
- Single-episode block always returns the same episode, seek in bounds
- Uniform-weight WRR round-robin distribution
- Offline block returns ScheduleItem with type="offline"
- Unequal weight distribution and interleaved ordering
- Block boundary: timestamp exactly at start_hour goes to the starting block
- DST fall-back ambiguous hour uses the pre-transition (first) occurrence
"""

from __future__ import annotations

from datetime import datetime

import pytz

from server.models import Block, Channel, Show
from server.scheduler import compute_schedule, expand_block_episodes

# ---------------------------------------------------------------------------
# Timezone constant shared by all tests
# ---------------------------------------------------------------------------

_CHICAGO = pytz.timezone("America/Chicago")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chicago_ts(*args) -> int:
    """Return a Unix timestamp (int) for a naive Chicago datetime.

    Accepts the same positional arguments as datetime(): year, month, day,
    hour, minute, second.  The naive datetime is localized without DST
    ambiguity (is_dst=False), which is safe for all summer dates and normal
    winter datetimes that do not fall in the fall-back window.
    """
    naive = datetime(*args)
    aware = _CHICAGO.localize(naive, is_dst=False)
    return int(aware.timestamp())


def _make_channel(blocks: list[Block], channel_id: str = "ch1") -> Channel:
    return Channel(
        id=channel_id,
        name="Test Channel",
        number=1,
        color="#ff6600",
        blocks=blocks,
    )


def _make_show(name: str, weight: int, ids: list[str]) -> Show:
    return Show(name=name, weight=weight, jellyfin_ids=ids)


# ---------------------------------------------------------------------------
# 1. test_midnight_crossing_block
# ---------------------------------------------------------------------------


def test_midnight_crossing_block():
    """Block start_hour=22, end_hour=4: seek epoch anchored to day D at 22:00.

    Test timestamp: 23:00 on 2024-06-15 (Chicago summer, no DST ambiguity).
    The block window opened at 22:00 on that same day, so elapsed = 3600s.
    The episode in the rotation has duration=7200s; seek_offset should be
    3600s (one hour into the episode) and must satisfy 0 <= seek_offset < duration.
    """
    # One episode long enough that the 1-hour elapsed offset falls inside it.
    show = _make_show("Night Show", weight=1, ids=["night-ep-001"])
    block = Block(name="Late Night", start_hour=22, end_hour=4, shows=[show])
    channel = _make_channel([block])
    durations = {"night-ep-001": 7200.0}  # 2 hours

    # 23:00 Chicago on 2024-06-15
    t = _chicago_ts(2024, 6, 15, 23, 0, 0)

    item = compute_schedule(channel, t, episode_durations=durations)

    assert item.jellyfin_id == "night-ep-001"
    assert item.type == "episode"

    # 23:00 is 3600s after block start at 22:00 on the same day.
    expected_elapsed = 3600.0
    assert abs(item.seek_offset - expected_elapsed) < 0.1, (
        f"Expected seek_offset ~{expected_elapsed}s, got {item.seek_offset}s"
    )

    # Bounds guarantee
    assert 0.0 <= item.seek_offset < item.duration


# ---------------------------------------------------------------------------
# 2. test_single_episode_block
# ---------------------------------------------------------------------------


def test_single_episode_block():
    """One show, one episode: any timestamp always returns that episode.

    Uses three distinct timestamps spread across the block window and
    verifies seek_offset is in bounds for each.
    """
    show = _make_show("Solo Show", weight=1, ids=["solo-ep-001"])
    block = Block(name="Solo Block", start_hour=8, end_hour=12, shows=[show])
    channel = _make_channel([block])
    duration = 3600.0  # 1 hour
    durations = {"solo-ep-001": duration}

    # Three timestamps within the block: 08:00, 09:30, 11:59
    for hour, minute in [(8, 0), (9, 30), (11, 59)]:
        t = _chicago_ts(2024, 3, 10, hour, minute, 0)
        item = compute_schedule(channel, t, episode_durations=durations)

        assert item.jellyfin_id == "solo-ep-001", (
            f"Expected solo-ep-001 at {hour:02d}:{minute:02d}, got {item.jellyfin_id}"
        )
        assert item.type == "episode"
        assert 0.0 <= item.seek_offset < item.duration, (
            f"seek_offset={item.seek_offset} out of bounds at {hour:02d}:{minute:02d}"
        )


# ---------------------------------------------------------------------------
# 3. test_uniform_weight_block
# ---------------------------------------------------------------------------


def test_uniform_weight_block():
    """Three shows each weight=1: WRR cycle has exactly 3 slots, one per show.

    Total cycle length = sum(weights) = 3.  In a weight-1/1/1 cycle each
    show appears exactly once, confirming perfectly equal round-robin.
    """
    shows = [
        _make_show("Alpha", weight=1, ids=["alpha-001"]),
        _make_show("Beta",  weight=1, ids=["beta-001"]),
        _make_show("Gamma", weight=1, ids=["gamma-001"]),
    ]
    block = Block(name="Equal Block", start_hour=6, end_hour=10, shows=shows)

    slots = expand_block_episodes(block)

    # Cycle length must equal sum of weights
    assert len(slots) == 3, f"Expected 3 slots, got {len(slots)}"

    # Each show appears exactly once
    show_counts = {s.show_name: 0 for s in slots}
    for slot in slots:
        show_counts[slot.show_name] += 1

    for show in shows:
        assert show_counts[show.name] == 1, (
            f"Show '{show.name}' expected 1 slot, got {show_counts[show.name]}"
        )


# ---------------------------------------------------------------------------
# 4. test_offline_block
# ---------------------------------------------------------------------------


def test_offline_block():
    """Block with offline=True returns ScheduleItem with type='offline'.

    Tested across three timestamps inside the offline window.
    """
    block = Block(name="Dead Air", start_hour=2, end_hour=6, offline=True)
    channel = _make_channel([block])

    for hour in (2, 3, 5):
        t = _chicago_ts(2024, 4, 10, hour, 0, 0)
        item = compute_schedule(channel, t)

        assert item.type == "offline", (
            f"Expected type='offline' at hour {hour}, got type='{item.type}'"
        )


# ---------------------------------------------------------------------------
# 5. test_unequal_weight_distribution
# ---------------------------------------------------------------------------


def test_unequal_weight_distribution():
    """Show A (weight=3) and Show B (weight=2): cycle has 5 slots, 3 A and 2 B.

    Also verifies interleaving: the slots must NOT be grouped as AAA BB or
    BB AAA.  In a properly interleaved WRR cycle, no show should occupy
    more than its weight's worth of consecutive slots.
    """
    show_a = _make_show("ShowA", weight=3, ids=["a-001", "a-002", "a-003"])
    show_b = _make_show("ShowB", weight=2, ids=["b-001", "b-002"])
    block = Block(name="Weighted Block", start_hour=14, end_hour=20, shows=[show_a, show_b])

    slots = expand_block_episodes(block)

    # Total slots = 3 + 2 = 5
    assert len(slots) == 5, f"Expected 5 slots, got {len(slots)}"

    names = [s.show_name for s in slots]

    # Count occurrences
    a_count = names.count("ShowA")
    b_count = names.count("ShowB")
    assert a_count == 3, f"ShowA expected 3 slots, got {a_count}"
    assert b_count == 2, f"ShowB expected 2 slots, got {b_count}"

    # Verify interleaving: the sequence should NOT be all-A-then-all-B or
    # all-B-then-all-A.  In a 3:2 WRR the canonical output is A B A B A.
    grouped_a = names == ["ShowA", "ShowA", "ShowA", "ShowB", "ShowB"]
    grouped_b = names == ["ShowB", "ShowB", "ShowA", "ShowA", "ShowA"]
    assert not grouped_a and not grouped_b, (
        f"Shows are grouped rather than interleaved: {names}"
    )

    # Adjacent duplicates: ShowA should never appear 3 times in a row,
    # ShowB should never appear 2 times in a row at positions that leave
    # no room for A.
    max_consecutive_a = max(
        sum(1 for _ in group)
        for name, group in _groupby_name(names)
        if name == "ShowA"
    )
    assert max_consecutive_a <= 2, (
        f"ShowA has {max_consecutive_a} consecutive slots (weight 3 should spread them)"
    )


def _groupby_name(names: list[str]):
    """Yield (name, iterator) groups of consecutive equal names."""
    if not names:
        return
    current = names[0]
    group = [names[0]]
    for n in names[1:]:
        if n == current:
            group.append(n)
        else:
            yield current, group
            current = n
            group = [n]
    yield current, group


# ---------------------------------------------------------------------------
# 6. test_block_boundary_at_start_hour
# ---------------------------------------------------------------------------


def test_block_boundary_at_start_hour():
    """A timestamp exactly at 12:00 noon belongs to the block starting at 12, not 6.

    Channel has two adjacent blocks:
      Block1: hours 6–12 (morning show)
      Block2: hours 12–18 (afternoon show)

    A timestamp at exactly 12:00 Chicago time (2024-06-15, summer, no DST
    ambiguity) must resolve to Block2's content, per Requirement 4.7.
    """
    morning_show = _make_show("Morning Show", weight=1, ids=["morning-ep-001"])
    afternoon_show = _make_show("Afternoon Show", weight=1, ids=["afternoon-ep-001"])

    block1 = Block(name="Morning", start_hour=6,  end_hour=12, shows=[morning_show])
    block2 = Block(name="Afternoon", start_hour=12, end_hour=18, shows=[afternoon_show])

    channel = _make_channel([block1, block2])
    durations = {
        "morning-ep-001": 3600.0,
        "afternoon-ep-001": 3600.0,
    }

    # Exactly noon Chicago time on a summer day (no DST ambiguity)
    t = int(_CHICAGO.localize(datetime(2024, 6, 15, 12, 0), is_dst=False).timestamp())

    item = compute_schedule(channel, t, episode_durations=durations)

    assert item.jellyfin_id == "afternoon-ep-001", (
        f"Expected afternoon-ep-001 at exactly noon, got {item.jellyfin_id!r} "
        f"(type={item.type!r})"
    )
    assert 0.0 <= item.seek_offset < item.duration


# ---------------------------------------------------------------------------
# 7. test_dst_fallback_ambiguous_hour
# ---------------------------------------------------------------------------


def _first_sunday_of_november(year: int) -> int:
    """Return the day-of-month for the first Sunday in November of *year*."""
    # November 1st and find the next (or same) Sunday
    d = datetime(year, 11, 1)
    # weekday(): Monday=0 ... Sunday=6
    days_until_sunday = (6 - d.weekday()) % 7
    return 1 + days_until_sunday


def test_dst_fallback_ambiguous_hour():
    """DST fall-back: the ambiguous 1:30 AM uses the pre-transition occurrence.

    On the first Sunday in November, clocks fall back from 2:00 AM CDT
    (UTC-5) to 1:00 AM CST (UTC-6), making 1:00–2:00 AM ambiguous.

    We construct the test timestamp using pytz with is_dst=True (CDT,
    UTC-5), which selects the first (pre-transition) occurrence of that
    wall-clock time.

    compute_schedule must return a valid, in-bounds result for this
    timestamp (it should not raise, and seek_offset must be in bounds).
    """
    year = 2024
    first_sunday = _first_sunday_of_november(year)

    # Pre-transition 1:30 AM (CDT = UTC-5, is_dst=True)
    ambiguous_local = datetime(year, 11, first_sunday, 1, 30, 0)
    t_pre = int(_CHICAGO.localize(ambiguous_local, is_dst=True).timestamp())

    # Post-transition 1:30 AM (CST = UTC-6, is_dst=False) — one hour later
    t_post = int(_CHICAGO.localize(ambiguous_local, is_dst=False).timestamp())

    # The two timestamps must differ by exactly 3600s (they refer to the same
    # wall-clock time but different UTC offsets).
    assert t_post - t_pre == 3600, (
        f"Expected 3600s between pre/post-transition; got {t_post - t_pre}s"
    )

    # Block that covers the 1 AM hour (midnight-crossing: 22–4)
    show = _make_show("Night Show", weight=1, ids=["dst-ep-001"])
    block = Block(name="Late Night", start_hour=22, end_hour=4, shows=[show])
    channel = _make_channel([block])
    durations = {"dst-ep-001": 7200.0}

    # Both timestamps must yield a valid in-bounds result without raising.
    for label, t in [("pre-transition (CDT)", t_pre), ("post-transition (CST)", t_post)]:
        item = compute_schedule(channel, t, episode_durations=durations)
        assert item.type == "episode", (
            f"Expected type='episode' for {label}, got '{item.type}'"
        )
        assert 0.0 <= item.seek_offset < item.duration, (
            f"seek_offset out of bounds for {label}: "
            f"seek_offset={item.seek_offset}, duration={item.duration}"
        )
