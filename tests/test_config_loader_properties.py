"""
Property-based tests for server/config_loader.py using Hypothesis.

Feature: classic-tv-3
Properties covered:
    Property 4: YAML round-trip
    Property 5: Parser never raises unhandled exceptions
"""

from __future__ import annotations

import string

import pytest
import yaml
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from server.config_loader import parse, serialize
from server.models import Block, Channel, ConfigValidationError, Show

# ---------------------------------------------------------------------------
# Strategy helpers
# ---------------------------------------------------------------------------

# Printable ASCII excluding characters that confuse YAML parsers or
# produce pathological edge cases (e.g. leading/trailing whitespace,
# null bytes, lone colons, and curly braces).
_SAFE_CHARS = string.ascii_letters + string.digits + " _-.()/!?"

_safe_text = st.text(
    alphabet=_SAFE_CHARS,
    min_size=1,
    max_size=64,
).filter(lambda s: s.strip() != "")  # no all-whitespace strings


_jellyfin_id = st.text(
    alphabet=string.ascii_lowercase + string.digits + "-",
    min_size=1,
    max_size=36,
)


@st.composite
def _show_strategy(draw: st.DrawFn) -> Show:
    """Generate a valid Show dataclass."""
    name = draw(_safe_text)
    weight = draw(st.integers(min_value=1, max_value=1000))
    n_ids = draw(st.integers(min_value=1, max_value=3))
    jellyfin_ids = draw(st.lists(_jellyfin_id, min_size=n_ids, max_size=n_ids))
    return Show(name=name, weight=weight, jellyfin_ids=jellyfin_ids)


@st.composite
def _nonoverlapping_hour_ranges(draw: st.DrawFn, count: int) -> list[tuple[int, int]]:
    """Generate *count* non-overlapping [start_hour, end_hour) ranges within 0–24.

    Approach: partition the 24-hour day by picking *count* cut-points in
    [0, 24], sort them, then use consecutive pairs as (start, end).
    Each range is guaranteed to be non-empty (start < end) and
    non-overlapping because they are disjoint slices of the same day.
    """
    # Pick count+1 distinct boundary points in [0, 24]
    boundaries = draw(
        st.lists(
            st.integers(min_value=0, max_value=24),
            min_size=count + 1,
            max_size=count + 1,
            unique=True,
        ).map(sorted)
    )

    ranges: list[tuple[int, int]] = []
    for i in range(count):
        start = boundaries[i]
        end = boundaries[i + 1]
        if start < end:
            ranges.append((start, end))

    # Hypothesis may generate fewer than *count* non-empty ranges if
    # boundaries happen to be equal — filter those out and accept
    # whatever non-empty ranges remain (at least 1 guaranteed by unique).
    return ranges


@st.composite
def _block_strategy(draw: st.DrawFn, start_hour: int, end_hour: int) -> Block:
    """Generate a valid Block for the given [start_hour, end_hour) window."""
    name_text = st.text(alphabet=_SAFE_CHARS, min_size=1, max_size=64).filter(
        lambda s: s.strip() != ""
    )
    name = draw(name_text)
    is_offline = draw(st.booleans())

    if is_offline:
        return Block(
            name=name,
            start_hour=start_hour,
            end_hour=end_hour,
            shows=None,
            offline=True,
        )
    else:
        n_shows = draw(st.integers(min_value=1, max_value=3))
        shows = draw(st.lists(_show_strategy(), min_size=n_shows, max_size=n_shows))
        return Block(
            name=name,
            start_hour=start_hour,
            end_hour=end_hour,
            shows=shows,
            offline=False,
        )


@st.composite
def _channel_strategy(draw: st.DrawFn) -> Channel:
    """Generate a valid Channel dataclass with non-overlapping blocks.

    Validation constraints (from Requirement 5.2 / 5.3):
      - id: 1–64 printable ASCII
      - name: 1–128 printable ASCII
      - number: 1–9999
      - color: "#" + 6 lowercase hex digits
      - blocks: 1–4 non-overlapping blocks per channel (task spec allows 1–48)
    """
    channel_id = draw(
        st.text(alphabet=_SAFE_CHARS, min_size=1, max_size=64).filter(
            lambda s: s.strip() != ""
        )
    )
    channel_name = draw(
        st.text(alphabet=_SAFE_CHARS, min_size=1, max_size=128).filter(
            lambda s: s.strip() != ""
        )
    )
    number = draw(st.integers(min_value=1, max_value=9999))
    hex_chars = string.digits + "abcdef"
    color = "#" + draw(st.text(alphabet=hex_chars, min_size=6, max_size=6))

    # Generate 1–4 non-overlapping hour ranges then build a block for each.
    n_blocks = draw(st.integers(min_value=1, max_value=4))
    ranges = draw(_nonoverlapping_hour_ranges(count=n_blocks))

    # _nonoverlapping_hour_ranges may return fewer ranges than requested;
    # that's fine — we use however many came back (always ≥ 1).
    blocks: list[Block] = []
    for start, end in ranges:
        block = draw(_block_strategy(start_hour=start, end_hour=end))
        blocks.append(block)

    # Ensure we have at least one block (invariant guaranteed by strategy
    # design but be explicit to satisfy the type checker).
    if not blocks:
        blocks = [
            Block(name="Default", start_hour=0, end_hour=24, offline=True)
        ]

    return Channel(
        id=channel_id,
        name=channel_name,
        number=number,
        color=color,
        blocks=blocks,
    )


# ---------------------------------------------------------------------------
# Property 4: YAML round-trip
# ---------------------------------------------------------------------------


@given(channel=_channel_strategy())
@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_yaml_roundtrip(channel: Channel) -> None:
    """Feature: classic-tv-3, Property 4: YAML round-trip

    For all valid Channel objects:
        serialize(parse(serialize(channel))) == serialize(channel)

    Validates: Requirements 34.3
    """
    yaml_str = serialize(channel)
    parsed = parse(yaml_str)
    yaml_str_2 = serialize(parsed)
    assert yaml_str_2 == yaml_str, (
        f"Round-trip produced different YAML.\n"
        f"Original serialize:\n{yaml_str}\n"
        f"After parse+serialize:\n{yaml_str_2}"
    )


# ---------------------------------------------------------------------------
# Property 5: Parser never raises unhandled exceptions
# ---------------------------------------------------------------------------


@given(text=st.text())
@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_parser_never_raises_unhandled_exception(text: str) -> None:
    """Feature: classic-tv-3, Property 5: Parser never raises unhandled exceptions

    For arbitrary text input, parse(text) either:
      (a) returns a Channel, or
      (b) raises ConfigValidationError

    It MUST NOT raise yaml.YAMLError, KeyError, TypeError, or AttributeError.

    Validates: Requirements 34.4, 34.5
    """
    try:
        result = parse(text)
        # If it succeeds, the result must be a Channel instance.
        assert isinstance(result, Channel), (
            f"parse() returned non-Channel on success: {type(result)!r}"
        )
    except ConfigValidationError:
        # Expected: structured validation failure. All good.
        pass
    except (yaml.YAMLError, KeyError, TypeError, AttributeError) as exc:
        pytest.fail(
            f"parse() raised an unhandled {type(exc).__name__} instead of "
            f"ConfigValidationError.\nInput (first 200 chars): {text[:200]!r}\n"
            f"Exception: {exc}"
        )
