#!/usr/bin/env python3
"""Assign channel numbers on the live Classic TV plugin.

Reads GET /ClassicTV/manage/channels, keeps every number already set, and gives
each unnumbered (0) channel the lowest free positive number in lineup order.
Dry run by default; --apply POSTs the updated config back (needs a Jellyfin API
key in $JELLYFIN_API_KEY, created in Dashboard > API Keys).

    python3 tools/assign_channel_numbers.py
    JELLYFIN_API_KEY=... python3 tools/assign_channel_numbers.py --apply

The POST replaces the whole channel config, so a backup of the current config is
written to channels/backup-<timestamp>.json before applying.
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


def assign_numbers(channels: list[dict]) -> list[dict]:
    """Return channels with every 0/missing number set to the lowest free one."""
    taken = {c.get("number", 0) for c in channels if c.get("number", 0) > 0}
    nxt = 1
    for c in channels:
        if c.get("number", 0) > 0:
            continue
        while nxt in taken:
            nxt += 1
        c["number"] = nxt
        taken.add(nxt)
    return channels


def _request(url: str, key: str | None, data: bytes | None = None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f'MediaBrowser Token="{key}"'
    req = urllib.request.Request(url, data=data, headers=headers)
    return urllib.request.urlopen(req, timeout=15)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--apply", action="store_true", help="POST the result")
    args = ap.parse_args()
    key = os.environ.get("JELLYFIN_API_KEY")

    url = f"{args.host}/ClassicTV/manage/channels"
    try:
        root = json.load(_request(url, key))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            print(
                "401 Unauthorized: the manage API needs a Jellyfin API key.\n"
                "Create one in Dashboard > API Keys and set $JELLYFIN_API_KEY.",
                file=sys.stderr,
            )
            return 2
        raise
    before = {c["id"]: c.get("number", 0) for c in root["channels"]}
    assign_numbers(root["channels"])

    for c in root["channels"]:
        mark = "" if before[c["id"]] == c["number"] else "  (new)"
        print(f'{c["number"]:>3}  {c["name"]}{mark}')

    if not args.apply:
        print("\nDry run. Re-run with --apply to save.")
        return 0
    if not key:
        print("Set JELLYFIN_API_KEY to apply.", file=sys.stderr)
        return 2

    backup = f"channels/backup-{time.strftime('%Y%m%d-%H%M%S')}.json"
    orig = json.load(_request(url, key))
    with open(backup, "w") as f:
        json.dump(orig, f, indent=2)
    print(f"\nBackup written to {backup}")
    _request(url, key, json.dumps(root).encode()).read()
    print("Applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
