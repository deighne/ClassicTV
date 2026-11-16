"""Tests for ChannelEntry — channel_entry.py (digit entry + on-screen display)."""

import time
from unittest.mock import MagicMock

import channel_entry as ce
from channels import Channel
from channel_entry import ChannelEntry


class _List:
    def __init__(self, *numbers):
        self._by = {n: Channel(id=f"ch{n}", name=f"Chan {n}", number=n) for n in numbers}

    def get_channel_by_number(self, n):
        return self._by.get(n)


def _entry(numbers=(2, 12), display=None):
    changed = []
    e = ChannelEntry(_List(*numbers), changed.append, display=display)
    return e, changed


def test_first_digit_starts_accumulating():
    e, _ = _entry()
    e.handle_digit(1)
    assert e.is_accumulating()
    e.handle_back()


def test_confirm_resolves_channel():
    e, changed = _entry()
    e.handle_digit(1)
    e.handle_digit(2)
    e.handle_confirm()
    assert [c.number for c in changed] == [12]
    assert not e.is_accumulating()


def test_extra_digits_beyond_max_are_ignored():
    e, changed = _entry(numbers=(123,))
    for d in (1, 2, 3, 4, 5):
        e.handle_digit(d)
    e.handle_confirm()
    assert [c.number for c in changed] == [123]


def test_back_cancels_without_tuning():
    e, changed = _entry()
    e.handle_digit(2)
    e.handle_back()
    assert not e.is_accumulating()
    assert changed == []


def test_timeout_confirms(monkeypatch):
    monkeypatch.setattr(ce, "CHANNEL_ENTRY_TIMEOUT_SECONDS", 0.05)
    e, changed = _entry()
    e.handle_digit(2)
    time.sleep(0.3)
    assert [c.number for c in changed] == [2]


def test_other_key_cancels_and_routes():
    routed = []
    e = ChannelEntry(_List(2), lambda c: None, on_route_event=routed.append)
    e.handle_digit(2)
    e.handle_other("up")
    assert routed == ["up"]
    assert not e.is_accumulating()


def test_handle_other_is_noop_when_idle():
    routed = []
    e = ChannelEntry(_List(2), lambda c: None, on_route_event=routed.append)
    e.handle_other("up")
    assert routed == []


def test_display_shows_partial_entry_with_cursor():
    display = MagicMock()
    e, _ = _entry(display=display)
    e.handle_digit(1)
    e.handle_digit(2)
    labels = [c.args[0] for c in display.show_text.call_args_list]
    assert labels == ["1_", "12_"]
    e.handle_back()
    display.hide.assert_called()


def test_display_shows_not_found():
    display = MagicMock()
    e, changed = _entry(numbers=(2,), display=display)
    e.handle_digit(9)
    e.handle_confirm()
    assert changed == []
    last = display.show_text.call_args_list[-1]
    assert last.args[:2] == ("9", "Not Found")


def test_display_passes_title_and_badge_to_overlay():
    display = MagicMock()
    e, _ = _entry(numbers=(12,), display=display)
    e.handle_digit(1)
    e.handle_digit(2)
    last = display.show_text.call_args_list[-1]
    assert last.args[0] == "12_"
    assert last.args[1] == "Chan 12"
    assert last.kwargs["title"] == "Press ENTER to tune"
    assert last.kwargs["badge"] in ("TUNE", "DIRECT")


def test_display_failure_never_breaks_entry():
    display = MagicMock()
    display.show_text.side_effect = RuntimeError("no window")
    display.hide.side_effect = RuntimeError("no window")
    e, changed = _entry(display=display)
    e.handle_digit(2)
    e.handle_confirm()
    assert [c.number for c in changed] == [2]


class _PrefixList(_List):
    def has_longer_number(self, prefix):
        return any(
            str(n).startswith(prefix) and len(str(n)) > len(prefix) for n in self._by
        )


def test_prefix_of_longer_channel_waits_for_second_digit(monkeypatch):
    """'1' with channel 12 present must not tune channel 1 after the short timeout."""
    monkeypatch.setattr(ce, "CHANNEL_ENTRY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(ce, "CHANNEL_ENTRY_AMBIGUOUS_TIMEOUT_SECONDS", 0.5)
    changed = []
    e = ChannelEntry(_PrefixList(1, 12), changed.append)
    e.handle_digit(1)
    time.sleep(0.2)  # past the short timeout, inside the long one
    assert changed == []
    e.handle_digit(2)
    e.handle_confirm()
    assert [c.number for c in changed] == [12]


def test_unambiguous_digit_uses_short_timeout(monkeypatch):
    monkeypatch.setattr(ce, "CHANNEL_ENTRY_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(ce, "CHANNEL_ENTRY_AMBIGUOUS_TIMEOUT_SECONDS", 5)
    changed = []
    e = ChannelEntry(_PrefixList(1, 12, 5), changed.append)
    e.handle_digit(5)
    time.sleep(0.3)
    assert [c.number for c in changed] == [5]


def test_has_longer_number_on_channel_list():
    from unittest.mock import MagicMock
    from channels import ChannelList

    cl = ChannelList(MagicMock(get_channels=lambda: []))
    cl._channels = [Channel("a", "A", 1), Channel("b", "B", 12), Channel("c", "C", 5)]
    assert cl.has_longer_number("1") is True
    assert cl.has_longer_number("5") is False
    assert cl.has_longer_number("12") is False
