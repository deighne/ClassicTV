#!/usr/bin/env python3
"""Add (or update) the TV Guide channel on Channel 1 and apply the contiguous 1-20 channel numbering.

Channel numbering applied:
  1: guide ("TV Guide", guide=True)
  2: anime ("Anime")
  3: disney-cartoons ("Disney Cartoons")
  4: nickelodeon-cartoons ("Nickelodeon Cartoons")
  5: cartoon-network ("Cartoon Network")
  6: edutainment-4-kidz ("Edutainment 4 Kidz")
  7: edutainment ("Edutainment")
  8: superheroes ("Superheroes")
  9: classic-cartoons ("Classic Cartoons")
 10: ooky-spooky ("Ooky Spooky")
 11: live-action ("Live Action")
 12-20, 67: unchanged

Needs plugin 12.0.0.2 or newer (so the `guide` field is preserved).
Dry run by default; --apply POSTs the updated channel config to /ClassicTV/manage/channels.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_HOST = os.environ.get("JELLYFIN_BASE_URL", "http://localhost:8096")

TARGET_NUMBERS: dict[str, int] = {
    "guide": 1,
    "anime": 2,
    "disney-cartoons": 3,
    "nickelodeon-cartoons": 4,
    "cartoon-network": 5,
    "edutainment-4-kidz": 6,
    "edutainment": 7,
    "superheroes": 8,
    "classic-cartoons": 9,
    "ooky-spooky": 10,
    "live-action": 11,
}


def apply_guide_and_renumber(root: dict, guide_name: str = "TV Guide") -> str:
    """Upsert the `guide` channel at Ch 1 and renumber channels 2-11 in *root* in place."""
    for ch in root.get("channels", []):
        cid = ch.get("id", "")
        if cid in TARGET_NUMBERS:
            ch["number"] = TARGET_NUMBERS[cid]

    guide_entry = {
        "id": "guide",
        "name": guide_name,
        "number": 1,
        "enabled": True,
        "startAnchor": "2024-01-01T00:00:00Z",
        "guide": True,
        "blocks": [
            {"name": "All Day", "startHour": 0, "endHour": 24, "offline": True, "shows": []}
        ],
    }

    for i, existing in enumerate(root["channels"]):
        if existing.get("id") == "guide":
            root["channels"][i] = {**existing, **guide_entry}
            return "updated"

    root["channels"].insert(0, guide_entry)
    return "added"


def _request(url: str, key: str, data: bytes | None = None):
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f'MediaBrowser Token="{key}"',
        },
    )
    return urllib.request.urlopen(req, timeout=15)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--name", default="TV Guide")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    key = os.environ.get("JELLYFIN_API_KEY")
    if not key:
        print("Set JELLYFIN_API_KEY (Dashboard > API Keys).", file=sys.stderr)
        return 2

    url = f"{args.host}/ClassicTV/manage/channels"
    try:
        root = json.load(_request(url, key))
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code} reading channel config", file=sys.stderr)
        return 2
    original = json.loads(json.dumps(root))

    what = apply_guide_and_renumber(root, args.name)
    sorted_chs = sorted(
        [c for c in root["channels"] if c.get("enabled", True)],
        key=lambda c: c.get("number", 9999),
    )
    print(f"Action: {what} 'guide' channel and renumbered lineup:")
    for c in sorted_chs:
        tag = " [GUIDE]" if c.get("guide") else (" [MIRROR]" if c.get("mirrorUser") else "")
        print(f"  Ch {c.get('number', 0):02d}: {c.get('name')} ({c.get('id')}){tag}")

    if not args.apply:
        print("\nDry run. Re-run with --apply to save.")
        return 0

    os.makedirs("channels", exist_ok=True)
    backup = f"channels/backup-{time.strftime('%Y%m%d-%H%M%S')}.json"
    with open(backup, "w") as f:
        json.dump(original, f, indent=2)
    print(f"Backup written to {backup}")
    _request(url, key, json.dumps(root).encode()).read()
    print("Applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
