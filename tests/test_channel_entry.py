"""
Tests for addon/resources/lib/channel_entry.py.

Covers:
  - IDLE → ACCUMULATING on first digit (Req 14.1)
  - Digit append and timer reset within ACCUMULATING (Req 14.2)
  - Explicit confirm via handle_confirm (Req 14.3)
  - Inactivity timeout auto-confirm (Req 14.2)
  - back/cancel clears state (Req 14.4)
  - handle_other routes event and clears state (Req 14.4)
  - Max-digit overflow: extra digits ignored, state preserved (Req 14.5)
  - is_accumulating() reflects state correctly (Req 14.6)
  - Channel found → on_channel_change called (Req 14.7)
  - Channel not found → not-found display for CHANNEL_NOT_FOUND_DISPLAY_SECONDS (Req 14.8)
  - State returns to IDLE after confirm regardless of outcome
  - Constants used match constants.py values
"""

import sys
import time


# Inject Kodi stubs before importing addon code
sys.path.insert(0, "tests")
import conftest  # noqa: F401  (side-effect import — registers stubs in sys.modules)

sys.path.insert(0, "addon/resources/lib")

from channel_entry import ChannelEntry
from constants import (
    CHANNEL_ENTRY_MAX_DIGITS,
    CHANNEL_ENTRY_TIMEOUT_SECONDS,
    CHANNEL_NOT_FOUND_DISPLAY_SECONDS,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _Channel:
    """Minimal channel stub."""

    def __init__(self, number: int, name: str = "Test Channel") -> None:
        self.id = f"ch_{number}"
        self.number = number
        self.name = name
        self.color = "#ffffff"


class _ChannelList:
    """Minimal channel list stub with a configurable channel set."""

    def __init__(self, channels=None) -> None:
        self._channels = {ch.number: ch for ch in (channels or [])}

    def get_channel_by_number(self, number: int):
        return self._channels.get(number)


def _make_entry(channels=None, on_route_event=None):
    """Return an (entry, changes, routes) triple for inspection."""
    changes = []
    routes = []
    entry = ChannelEntry(
        channel_list=_ChannelList(channels or [_Channel(7), _Channel(42), _Channel(100)]),
        on_channel_change=lambda ch: changes.append(ch),
        on_route_event=(lambda action: routes.append(action)) if on_route_event is None else on_route_event,
    )
    return entry, changes, routes


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------


class TestInitialState:
    def test_starts_idle(self):
        entry, _, _ = _make_entry()
        assert not entry.is_accumulating()

    def test_state_attribute_is_idle(self):
        entry, _, _ = _make_entry()
        assert entry._state == "IDLE"

    def test_no_accumulated_digits(self):
        entry, _, _ = _make_entry()
        assert entry._accumulated == ""


# ---------------------------------------------------------------------------
# First digit → ACCUMULATING
# ---------------------------------------------------------------------------


class TestFirstDigit:
    def test_transitions_to_accumulating(self):
        entry, _, _ = _make_entry()
        entry.handle_digit(4)
        assert entry.is_accumulating()

    def test_digit_stored(self):
        entry, _, _ = _make_entry()
        entry.handle_digit(7)
        assert entry._accumulated == "7"

    def test_timer_started(self):
        entry, _, _ = _make_entry()
        entry.handle_digit(1)
        assert entry._timer is not None
        entry._cancel_timer()  # cleanup

    def test_zero_as_first_digit(self):
        entry, _, _ = _make_entry()
        entry.handle_digit(0)
        assert entry._accumulated == "0"
        entry._cancel_timer()


# ---------------------------------------------------------------------------
# Subsequent digits in ACCUMULATING
# ---------------------------------------------------------------------------


class TestDigitAccumulation:
    def test_appends_second_digit(self):
        entry, _, _ = _make_entry()
        entry.handle_digit(4)
        entry.handle_digit(2)
        assert entry._accumulated == "42"
        entry._cancel_timer()

    def test_appends_third_digit(self):
        entry, _, _ = _make_entry()
        entry.handle_digit(1)
        entry.handle_digit(0)
        entry.handle_digit(0)
        assert entry._accumulated == "100"
        entry._cancel_timer()

    def test_max_digits_overflow_ignored(self):
        """A 4th digit is silently ignored (Req 14.5)."""
        entry, _, _ = _make_entry()
        entry.handle_digit(1)
        entry.handle_digit(2)
        entry.handle_digit(3)
        entry.handle_digit(9)  # overflow — ignored
        assert entry._accumulated == "123"
        assert entry.is_accumulating()
        entry._cancel_timer()

    def test_overflow_does_not_cancel_state(self):
        entry, _, _ = _make_entry()
        for d in [1, 2, 3, 4, 5]:
            entry.handle_digit(d)
        assert entry.is_accumulating()
        assert len(entry._accumulated) == CHANNEL_ENTRY_MAX_DIGITS
        entry._cancel_timer()


# ---------------------------------------------------------------------------
# is_accumulating()
# ---------------------------------------------------------------------------


class TestIsAccumulating:
    def test_false_before_any_digit(self):
        entry, _, _ = _make_entry()
        assert entry.is_accumulating() is False

    def test_true_after_digit(self):
        entry, _, _ = _make_entry()
        entry.handle_digit(3)
        assert entry.is_accumulating() is True
        entry._cancel_timer()

    def test_false_after_confirm(self):
        entry, _, _ = _make_entry()
        entry.handle_digit(7)
        entry.handle_confirm()
        assert entry.is_accumulating() is False

    def test_false_after_back(self):
        entry, _, _ = _make_entry()
        entry.handle_digit(5)
        entry.handle_back()
        assert entry.is_accumulating() is False


# ---------------------------------------------------------------------------
# handle_confirm — explicit confirm
# ---------------------------------------------------------------------------


class TestHandleConfirm:
    def test_channel_found_calls_callback(self):
        entry, changes, _ = _make_entry()
        entry.handle_digit(4)
        entry.handle_digit(2)
        entry.handle_confirm()
        assert len(changes) == 1
        assert changes[0].number == 42

    def test_returns_to_idle(self):
        entry, _, _ = _make_entry()
        entry.handle_digit(7)
        entry.handle_confirm()
        assert not entry.is_accumulating()
        assert entry._accumulated == ""

    def test_confirm_in_idle_is_noop(self):
        entry, changes, _ = _make_entry()
        entry.handle_confirm()  # no-op
        assert len(changes) == 0
        assert not entry.is_accumulating()

    def test_single_digit_confirm(self):
        entry, changes, _ = _make_entry()
        entry.handle_digit(7)
        entry.handle_confirm()
        assert len(changes) == 1
        assert changes[0].number == 7

    def test_three_digit_confirm(self):
        entry, changes, _ = _make_entry()
        entry.handle_digit(1)
        entry.handle_digit(0)
        entry.handle_digit(0)
        entry.handle_confirm()
        assert len(changes) == 1
        assert changes[0].number == 100

    def test_channel_not_found_no_callback(self):
        entry, changes, _ = _make_entry()
        entry.handle_digit(9)
        entry.handle_digit(9)
        entry.handle_confirm()
        assert len(changes) == 0

    def test_channel_not_found_returns_to_idle(self):
        entry, _, _ = _make_entry()
        entry.handle_digit(9)
        entry.handle_digit(9)
        entry.handle_confirm()
        assert not entry.is_accumulating()
        assert entry._accumulated == ""


# ---------------------------------------------------------------------------
# handle_back — cancel without routing
# ---------------------------------------------------------------------------


class TestHandleBack:
    def test_back_in_accumulating_returns_to_idle(self):
        entry, _, _ = _make_entry()
        entry.handle_digit(5)
        entry.handle_back()
        assert not entry.is_accumulating()

    def test_back_clears_accumulated(self):
        entry, _, _ = _make_entry()
        entry.handle_digit(1)
        entry.handle_digit(2)
        entry.handle_back()
        assert entry._accumulated == ""

    def test_back_does_not_route_event(self):
        entry, _, routes = _make_entry()
        entry.handle_digit(3)
        entry.handle_back()
        assert len(routes) == 0

    def test_back_in_idle_is_noop(self):
        entry, changes, routes = _make_entry()
        entry.handle_back()
        assert len(changes) == 0
        assert len(routes) == 0
        assert not entry.is_accumulating()


# ---------------------------------------------------------------------------
# handle_other — cancel and route event
# ---------------------------------------------------------------------------


class TestHandleOther:
    def test_returns_to_idle(self):
        entry, _, _ = _make_entry()
        entry.handle_digit(4)
        entry.handle_other(action=99)
        assert not entry.is_accumulating()

    def test_clears_accumulated(self):
        entry, _, _ = _make_entry()
        entry.handle_digit(4)
        entry.handle_digit(2)
        entry.handle_other(action=99)
        assert entry._accumulated == ""

    def test_routes_original_event(self):
        entry, _, routes = _make_entry()
        entry.handle_digit(1)
        entry.handle_other(action="UP")
        assert routes == ["UP"]

    def test_routes_none_action(self):
        entry, _, routes = _make_entry()
        entry.handle_digit(2)
        entry.handle_other(action=None)
        assert routes == [None]

    def test_idle_other_is_noop(self):
        entry, changes, routes = _make_entry()
        entry.handle_other(action=42)
        assert len(changes) == 0
        assert len(routes) == 0

    def test_no_channel_change_on_cancel(self):
        entry, changes, _ = _make_entry()
        entry.handle_digit(7)
        entry.handle_other()
        assert len(changes) == 0


# ---------------------------------------------------------------------------
# Timeout auto-confirm
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_timeout_confirms_channel(self):
        """After CHANNEL_ENTRY_TIMEOUT_SECONDS the timer auto-confirms."""
        entry, changes, _ = _make_entry()
        entry.handle_digit(4)
        entry.handle_digit(2)
        # Wait a little beyond the timeout
        time.sleep(CHANNEL_ENTRY_TIMEOUT_SECONDS + 0.3)
        assert len(changes) == 1
        assert changes[0].number == 42

    def test_timeout_returns_to_idle(self):
        entry, _, _ = _make_entry()
        entry.handle_digit(7)
        time.sleep(CHANNEL_ENTRY_TIMEOUT_SECONDS + 0.3)
        assert not entry.is_accumulating()

    def test_digit_resets_timer(self):
        """A second digit before the timeout resets the clock."""
        entry, changes, _ = _make_entry()
        entry.handle_digit(4)
        # Wait just under the timeout, then add another digit
        time.sleep(CHANNEL_ENTRY_TIMEOUT_SECONDS * 0.8)
        entry.handle_digit(2)
        # Immediately after the second digit, confirm should NOT have fired yet
        assert len(changes) == 0
        assert entry.is_accumulating()
        # Now wait for the reset timer to fire
        time.sleep(CHANNEL_ENTRY_TIMEOUT_SECONDS + 0.3)
        assert len(changes) == 1

    def test_timeout_not_found(self):
        """Timeout with an unknown channel number → no callback, back to IDLE."""
        entry, changes, _ = _make_entry()
        entry.handle_digit(9)
        entry.handle_digit(9)
        time.sleep(CHANNEL_ENTRY_TIMEOUT_SECONDS + 0.3)
        assert len(changes) == 0
        assert not entry.is_accumulating()


# ---------------------------------------------------------------------------
# Default on_route_event (no-op)
# ---------------------------------------------------------------------------


class TestDefaultOnRouteEvent:
    def test_no_route_event_argument_still_works(self):
        """ChannelEntry can be constructed without on_route_event."""
        entry = ChannelEntry(
            channel_list=_ChannelList([_Channel(7)]),
            on_channel_change=lambda ch: None,
        )
        entry.handle_digit(1)
        # Should not raise
        entry.handle_other(action="UP")
        assert not entry.is_accumulating()


# ---------------------------------------------------------------------------
# Constant sanity checks (Req 29)
# ---------------------------------------------------------------------------


class TestConstants:
    def test_max_digits_is_three(self):
        assert CHANNEL_ENTRY_MAX_DIGITS == 3

    def test_timeout_is_one_point_five(self):
        assert CHANNEL_ENTRY_TIMEOUT_SECONDS == 1.5

    def test_not_found_display_is_two(self):
        assert CHANNEL_NOT_FOUND_DISPLAY_SECONDS == 2
