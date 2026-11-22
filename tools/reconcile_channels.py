#!/usr/bin/env python3
"""
tools/reconcile_channels.py — Audit and repair stale Jellyfin item IDs in Classic TV channels.

When media files are replaced or re-scanned in Jellyfin, their Item GUIDs can change.
This script checks every channel block's `jellyfinIds` against Jellyfin's live library,
matches stale Movie IDs by title and stale Series episode lists by series name, and
optionally writes the updated configuration back via PUT /ClassicTV/manage/channels.

Dry-run by default. Pass --apply to save changes.

Usage:
    JELLYFIN_API_KEY=... python3 tools/reconcile_channels.py
    JELLYFIN_API_KEY=... python3 tools/reconcile_channels.py --apply
    JELLYFIN_API_KEY=... python3 tools/reconcile_channels.py --channels ghiblet,tcm --apply
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
import sys
import urllib.request

DEFAULT_HOST = os.environ.get("JELLYFIN_BASE_URL", "http://localhost:8096")


def norm_id(item_id: str) -> str:
    """Normalize a Jellyfin GUID to lowercase 32-char hex without hyphens."""
    return (item_id or "").replace("-", "").strip().lower()


def fmt_uuid(item_id: str) -> str:
    """Format a 32-char hex GUID as hyphenated 8-4-4-4-12 lowercase UUID."""
    h = norm_id(item_id)
    if len(h) != 32:
        return item_id
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def reconcile_config(
    cfg: dict,
    live_movies: list[dict],
    live_episodes: list[dict],
    channel_filter: set[str] | None = None,
) -> tuple[dict, list[dict]]:
    """Return (updated_cfg, changes) repairing stale `jellyfinIds` in place on a copy.

    Each entry in `changes` is a dict with:
      action: "movie_replaced" | "series_refreshed" | "unresolved"
      channel_id, channel_number, show_name, dead_count, detail
    """
    updated = deepcopy(cfg)
    valid_ids: set[str] = set()

    movies_by_title: dict[str, list[str]] = {}
    for m in live_movies:
        mid = m.get("Id", "")
        if not mid:
            continue
        valid_ids.add(norm_id(mid))
        key = (m.get("Name") or "").strip().lower()
        if key:
            movies_by_title.setdefault(key, []).append(fmt_uuid(mid))

    series_eps: dict[str, list[tuple[int, int, str]]] = {}
    for ep in live_episodes:
        eid = ep.get("Id", "")
        if not eid:
            continue
        valid_ids.add(norm_id(eid))
        sname = (ep.get("SeriesName") or "").strip().lower()
        if sname:
            season = ep.get("ParentIndexNumber") or 0
            ep_num = ep.get("IndexNumber") or 0
            series_eps.setdefault(sname, []).append((season, ep_num, fmt_uuid(eid)))

    series_by_name: dict[str, list[str]] = {
        k: [eid for _, _, eid in sorted(v)] for k, v in series_eps.items()
    }

    changes: list[dict] = []
    for ch in updated.get("channels", []):
        cid = ch.get("id", "")
        if channel_filter and cid not in channel_filter:
            continue
        cnum = ch.get("number", 0)
        for block in ch.get("blocks", []):
            for show in block.get("shows", []):
                jids = show.get("jellyfinIds") or []
                if not jids:
                    continue
                dead = [j for j in jids if norm_id(j) not in valid_ids]
                if not dead:
                    continue

                name = show.get("name") or ""
                key = name.strip().lower()
                movie_matches = movies_by_title.get(key, [])

                if len(jids) == 1 and len(movie_matches) == 1:
                    new_id = movie_matches[0]
                    show["jellyfinIds"] = [new_id]
                    changes.append({
                        "action": "movie_replaced",
                        "channel_id": cid,
                        "channel_number": cnum,
                        "show_name": name,
                        "dead_count": len(dead),
                        "detail": f"{dead[0]} -> {new_id}",
                    })
                elif key in series_by_name:
                    new_eps = series_by_name[key]
                    show["jellyfinIds"] = new_eps
                    changes.append({
                        "action": "series_refreshed",
                        "channel_id": cid,
                        "channel_number": cnum,
                        "show_name": name,
                        "dead_count": len(dead),
                        "detail": f"{len(jids)} -> {len(new_eps)} live episodes",
                    })
                else:
                    changes.append({
                        "action": "unresolved",
                        "channel_id": cid,
                        "channel_number": cnum,
                        "show_name": name,
                        "dead_count": len(dead),
                        "detail": f"{len(dead)} dead ID(s), no unique live match",
                    })

    return updated, changes


def _request(url: str, key: str, data: bytes | None = None, method: str | None = None):
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Authorization": f'MediaBrowser Token="{key}"',
        },
    )
    return urllib.request.urlopen(req, timeout=30)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--channels", default="", help="Comma-separated channel IDs to limit scope")
    ap.add_argument("--apply", action="store_true", help="Write repaired config to Jellyfin")
    args = ap.parse_args()

    key = os.environ.get("JELLYFIN_API_KEY", "")
    if not key:
        print("Set JELLYFIN_API_KEY (Dashboard > API Keys).", file=sys.stderr)
        return 2

    base = args.host.rstrip("/")
    with _request(f"{base}/ClassicTV/manage/channels", key) as r:
        cfg = json.loads(r.read())
    with _request(f"{base}/Items?Recursive=true&IncludeItemTypes=Movie", key) as r:
        live_movies = json.loads(r.read()).get("Items", [])
    with _request(f"{base}/Items?Recursive=true&IncludeItemTypes=Episode", key) as r:
        live_episodes = json.loads(r.read()).get("Items", [])

    ch_filter = {c.strip() for c in args.channels.split(",") if c.strip()} or None
    updated, changes = reconcile_config(cfg, live_movies, live_episodes, ch_filter)

    if not changes:
        print("All channel jellyfinIds match live Jellyfin items.")
        return 0

    for c in changes:
        print(f"[{c['action']}] ch {c['channel_number']} ({c['channel_id']}) — {c['show_name']}: {c['detail']}")

    repairable = [c for c in changes if c["action"] != "unresolved"]
    if not repairable:
        return 0

    if not args.apply:
        print(f"\nDry run: {len(repairable)} repairable show/movie entries. Re-run with --apply to save.")
        return 0

    payload = json.dumps(updated).encode("utf-8")
    with _request(f"{base}/ClassicTV/manage/channels", key, data=payload) as r:
        print(f"\nSaved updated config (HTTP {r.status}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
