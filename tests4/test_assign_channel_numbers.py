"""Tests for tools/assign_channel_numbers.py (pure numbering logic)."""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
)
from assign_channel_numbers import assign_numbers  # noqa: E402


def test_keeps_existing_and_fills_lowest_free():
    chans = [{"id": "a", "number": 2}, {"id": "b", "number": 0}, {"id": "c"}, {"id": "d", "number": 0}]
    assign_numbers(chans)
    assert [c["number"] for c in chans] == [2, 1, 3, 4]


def test_all_assigned_is_unchanged():
    chans = [{"id": "a", "number": 5}, {"id": "b", "number": 9}]
    assign_numbers(chans)
    assert [c["number"] for c in chans] == [5, 9]
