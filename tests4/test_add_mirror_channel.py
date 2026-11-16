"""Tests for tools/add_mirror_channel.py (pure config edits)."""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
)
from add_mirror_channel import number_conflict, upsert_mirror_channel  # noqa: E402


def _root():
    return {"channels": [{"id": "sitcoms", "name": "Sitcoms", "number": 15, "enabled": True, "blocks": []}]}


def test_adds_mirror_channel():
    root = _root()
    assert upsert_mirror_channel(root, "rachel", "Rachel's Channel", 67, "rachel") == "added"
    ch = root["channels"][-1]
    assert ch["mirrorUser"] == "rachel" and ch["number"] == 67
    assert ch["blocks"][0]["offline"] is True


def test_updates_existing_without_duplicating_and_keeps_other_fields():
    root = _root()
    root["channels"].append({"id": "rachel", "name": "Old", "number": 3, "extra": "keep", "blocks": []})
    assert upsert_mirror_channel(root, "rachel", "New", 67, "rachel") == "updated"
    rachels = [c for c in root["channels"] if c["id"] == "rachel"]
    assert len(rachels) == 1 and rachels[0]["name"] == "New" and rachels[0]["extra"] == "keep"


def test_number_conflict_detected_only_for_other_enabled_channels():
    root = _root()
    assert number_conflict(root, "rachel", 15) == "Sitcoms"
    assert number_conflict(root, "sitcoms", 15) is None
    root["channels"][0]["enabled"] = False
    assert number_conflict(root, "rachel", 15) is None
