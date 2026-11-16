"""Tests for tools/add_guide_channel.py (pure config edits and renumbering)."""

import os
import sys

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
)
from add_guide_channel import apply_guide_and_renumber  # noqa: E402


def _sample_root():
    return {
        "channels": [
            {"id": "disney-cartoons", "name": "Disney Cartoons", "number": 1, "enabled": True, "blocks": []},
            {"id": "anime", "name": "Anime", "number": 2, "enabled": True, "blocks": []},
            {"id": "nickelodeon-cartoons", "name": "Nickelodeon Cartoons", "number": 3, "enabled": True, "blocks": []},
            {"id": "cartoon-network", "name": "Cartoon Network", "number": 4, "enabled": True, "blocks": []},
            {"id": "edutainment-4-kidz", "name": "Edutainment 4 Kidz", "number": 5, "enabled": True, "blocks": []},
            {"id": "edutainment", "name": "Edutainment", "number": 6, "enabled": True, "blocks": []},
            {"id": "superheroes", "name": "Superheroes", "number": 7, "enabled": True, "blocks": []},
            {"id": "classic-cartoons", "name": "Classic Cartoons", "number": 8, "enabled": True, "blocks": []},
            {"id": "ooky-spooky", "name": "Ooky Spooky", "number": 9, "enabled": True, "blocks": []},
            {"id": "live-action", "name": "Live Action", "number": 10, "enabled": True, "blocks": []},
            {"id": "its-1997", "name": "It's 1997", "number": 12, "enabled": True, "blocks": []},
            {"id": "rachel", "name": "Rachel's Channel", "number": 67, "enabled": True, "mirrorUser": "rachel", "blocks": []},
        ]
    }


def test_adds_guide_and_renumbers_lineup_without_conflicts():
    root = _sample_root()
    res = apply_guide_and_renumber(root)
    assert res == "added"

    by_id = {c["id"]: c for c in root["channels"]}
    assert by_id["guide"]["number"] == 1
    assert by_id["guide"]["guide"] is True
    assert by_id["anime"]["number"] == 2
    assert by_id["disney-cartoons"]["number"] == 3
    assert by_id["nickelodeon-cartoons"]["number"] == 4
    assert by_id["cartoon-network"]["number"] == 5
    assert by_id["edutainment-4-kidz"]["number"] == 6
    assert by_id["edutainment"]["number"] == 7
    assert by_id["superheroes"]["number"] == 8
    assert by_id["classic-cartoons"]["number"] == 9
    assert by_id["ooky-spooky"]["number"] == 10
    assert by_id["live-action"]["number"] == 11
    assert by_id["its-1997"]["number"] == 12
    assert by_id["rachel"]["number"] == 67

    # Ensure zero duplicate numbers among enabled channels
    numbers = [c["number"] for c in root["channels"] if c.get("enabled")]
    assert len(numbers) == len(set(numbers))


def test_idempotent_when_run_twice():
    root = _sample_root()
    assert apply_guide_and_renumber(root) == "added"
    assert apply_guide_and_renumber(root) == "updated"
    guides = [c for c in root["channels"] if c["id"] == "guide"]
    assert len(guides) == 1
