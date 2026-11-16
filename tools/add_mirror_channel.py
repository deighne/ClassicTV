#!/usr/bin/env python3
"""Add (or update) a mirror channel on the live Classic TV plugin.

A mirror channel plays whatever one Jellyfin user is watching right now, at
their position. Needs plugin 10.11.0.16 or newer: an older plugin silently drops
the mirrorUser field and would create an ordinary offline channel.

Dry run by default; --apply POSTs the updated channel config (the manage API
replaces the whole config, so a backup is written first) and needs an admin API
key in $JELLYFIN_API_KEY (Dashboard > API Keys).

    python3 tools/add_mirror_channel.py
    JELLYFIN_API_KEY=... python3 tools/add_mirror_channel.py --apply
    JELLYFIN_API_KEY=... python3 tools/add_mirror_channel.py --user friend --number 67 --apply
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


def upsert_mirror_channel(root: dict, channel_id: str, name: str, number: int, user: str) -> str:
    """Add or update the mirror channel in *root* in place. Returns "added" or "updated"."""
    channel = {
        "id": channel_id,
        "name": name,
        "number": number,
        "enabled": True,
        "startAnchor": "2024-01-01T00:00:00Z",
        "mirrorUser": user,
        # Blocks are ignored for mirror channels; one offline block keeps the
        # config valid and readable in the dashboard.
        "blocks": [
            {"name": "All Day", "startHour": 0, "endHour": 24, "offline": True, "shows": []}
        ],
    }
    for i, existing in enumerate(root["channels"]):
        if existing["id"] == channel_id:
            root["channels"][i] = {**existing, **channel}
            return "updated"
    root["channels"].append(channel)
    return "added"


def number_conflict(root: dict, channel_id: str, number: int) -> str | None:
    """Name of another enabled channel already using *number*, if any."""
    for c in root["channels"]:
        if c["id"] != channel_id and c.get("enabled", True) and c.get("number", 0) == number:
            return c["name"]
    return None


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
    ap.add_argument("--id", default="mirror")
    ap.add_argument("--name", default="Mirror Channel")
    ap.add_argument("--number", type=int, default=67)
    ap.add_argument("--user", default="mirror", help="Jellyfin user to mirror")
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

    clash = number_conflict(root, args.id, args.number)
    if clash:
        print(f"Channel number {args.number} is already used by '{clash}'.", file=sys.stderr)
        return 2

    what = upsert_mirror_channel(root, args.id, args.name, args.number, args.user)
    verb = "add" if what == "added" else "update"
    print(f"Would {verb} channel {args.number} '{args.name}' mirroring user '{args.user}'")
    if not args.apply:
        print("\nDry run. Re-run with --apply to save.")
        return 0

    backup = f"channels/backup-{time.strftime('%Y%m%d-%H%M%S')}.json"
    with open(backup, "w") as f:
        json.dump(original, f, indent=2)
    print(f"Backup written to {backup}")
    _request(url, key, json.dumps(root).encode()).read()
    print("Applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
