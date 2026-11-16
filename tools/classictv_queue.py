#!/usr/bin/env python3
"""
tools/classictv_queue.py

Manages the Classic TV re-encoding priority queue.
Scans the live/future broadcast schedule across all 18 channels and boosts
the priority of problem episodes that are scheduled to air soonest.

Priority tiers:
  - Airing in < 1 hour:   Priority 10,000+ (EMERGENCY: immediate crash hazard!)
  - Airing in 1-3 hours:  Priority  5,000+ (CRITICAL)
  - Airing in 3-12 hours: Priority  2,000+ (HIGH)
  - Airing in 12-24 hrs:  Priority  1,000+ (ELEVATED)
  - Airing in 24-48 hrs:  Priority    500+ (UPCOMING)
  - Not in next 48 hrs:   Priority    100  (BACKGROUND)

Usage:
    python3 tools/classictv_queue.py --init             # Populate queue from library audit
    python3 tools/classictv_queue.py --scan             # Scan schedule & update priorities
    python3 tools/classictv_queue.py --top 20           # Show top priority items in queue
    python3 tools/classictv_queue.py --status           # Summary stats
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schedule_projector

QUEUE_FILE = os.environ.get(
    "CLASSICTV_QUEUE_FILE",
    os.path.expanduser("~/projects/ClassicTV3.0/web/reencode_queue.json")
)
DEFAULT_CHANNELS = os.path.expanduser("~/projects/ClassicTV3.0/channels/channels-v4.json")
DEFAULT_DB = os.path.expanduser("~/Library/Application Support/jellyfin/data/jellyfin.db")


def load_queue(path: str = QUEUE_FILE) -> Dict[str, Any]:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            sys.stderr.write(f"Warning: Failed to parse {path}: {e}\n")
    return {
        "updated_at": None,
        "total_items": 0,
        "pending": 0,
        "completed": 0,
        "reencoding": 0,
        "failed": 0,
        "items": {},  # item_id -> item_data
    }


def save_queue(queue: Dict[str, Any], path: str = QUEUE_FILE):
    queue["updated_at"] = datetime.now(timezone.utc).isoformat()
    # Update quick counts
    items = queue.get("items", {})
    queue["total_items"] = len(items)
    queue["pending"] = sum(1 for it in items.values() if it["status"] == "pending")
    queue["completed"] = sum(1 for it in items.values() if it["status"] == "completed")
    queue["reencoding"] = sum(1 for it in items.values() if it["status"] == "reencoding")
    queue["failed"] = sum(1 for it in items.values() if it["status"] == "failed")

    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    temp_path = f"{path}.tmp.{os.getpid()}"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2)
    os.replace(temp_path, path)


def reset_failed_jobs(queue_path: str = QUEUE_FILE) -> int:
    """Reset any jobs in 'failed' status back to 'pending' with 0 attempts."""
    queue = load_queue(queue_path)
    items = queue.get("items", {})
    reset_count = 0

    for item in items.values():
        if item.get("status") == "failed":
            item["status"] = "pending"
            item["attempts"] = 0
            item.pop("started_at", None)
            item.pop("completed_at", None)
            reset_count += 1

    if reset_count > 0:
        save_queue(queue, queue_path)

    return reset_count


def populate_queue_from_library(db_path: str = DEFAULT_DB, queue_path: str = QUEUE_FILE) -> int:
    """Scan jellyfin.db for all fatal hardware crash candidates and initialize queue."""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    sql = """
    SELECT 
        b.Id, b.SeriesName, b.Name, b.ParentIndexNumber, b.IndexNumber, b.Path,
        m.Codec, m.Profile, m.PixelFormat, m.BitDepth, m.Width, m.Height, m.Level
    FROM MediaStreamInfos m
    JOIN BaseItems b ON m.ItemId = b.Id
    WHERE m.Codec = 'hevc' AND m.StreamType = 1;
    """
    rows = cur.execute(sql).fetchall()
    conn.close()

    queue = load_queue(queue_path)
    items = queue.setdefault("items", {})

    added = 0
    for r in rows:
        item_id, series, name, s_idx, ep_idx, path, codec, prof, pix_fmt, depth, w, h, level = r
        clean_id = (item_id or "").replace("-", "").lower()
        if not path or not os.path.exists(path):
            continue

        is_10bit = ("10" in (pix_fmt or "")) or (depth and depth > 8)
        reasons = []
        if prof == "Main" and is_10bit:
            reasons.append("HEVC_PROFILE_10BIT_CONFLICT")
        if (series or "") == "Roseanne":
            reasons.append("HEVC_ROSEANNE_CRASH_VECTOR")
        if is_10bit and level and level >= 150:
            reasons.append(f"HEVC_10BIT_LEVEL5_OVERFLOW(L{int(level)})")
        if is_10bit and w and (w % 8 != 0):
            reasons.append(f"HEVC_10BIT_WIDTH_NOT_MOD8({w})")
        elif not is_10bit and w and (w % 8 != 0):
            reasons.append(f"HEVC_8BIT_WIDTH_NOT_MOD8({w})")

        if not reasons:
            continue

        if clean_id not in items:
            items[clean_id] = {
                "id": clean_id,
                "series": series or name,
                "season": s_idx,
                "episode": ep_idx,
                "title": name,
                "path": path,
                "width": w,
                "height": h,
                "issues": reasons,
                "status": "pending",
                "base_priority": 100,
                "effective_priority": 100,
                "next_air_time": None,
                "next_air_channel": None,
                "completed_at": None,
                "attempts": 0,
            }
            added += 1

    save_queue(queue, queue_path)
    return added


def calculate_priority(item: Dict[str, Any], seconds_away: Optional[float] = None) -> int:
    """
    Calculate dynamic priority:
    - Roseanne HEVC (default boot channel sitcoms crash vector): Base 10,000, boosted to 35,000+.
    - Issue 1 (Profile Main + 10-bit conflict - Judge Judy bug): 100% fatal kernel CMA buffer overflow.
      Given top priority tier (5,000 to 30,000+).
    - Issue 2 (Unaligned non-mod8 width): AXI bus stride error.
      Second priority tier (100 to 5,000).
    """
    issues = item.get("issues", [])
    is_roseanne = any("ROSEANNE" in x for x in issues) or item.get("series") == "Roseanne"
    is_level5 = any("LEVEL5" in x for x in issues)
    is_conflict = "HEVC_PROFILE_10BIT_CONFLICT" in issues

    if seconds_away is None:
        if is_roseanne or is_level5:
            return 10000
        if is_conflict:
            return 5000
        return 100

    if is_roseanne or is_level5:
        if seconds_away <= 3600:
            return 35000 + int(3600 - seconds_away)
        elif seconds_away <= 3 * 3600:
            return 25000 + int((3 * 3600 - seconds_away) / 2)
        elif seconds_away <= 12 * 3600:
            return 18000 + int((12 * 3600 - seconds_away) / 10)
        elif seconds_away <= 24 * 3600:
            return 12000 + int((24 * 3600 - seconds_away) / 20)
        else:
            return 10000
    elif is_conflict:
        if seconds_away <= 3600:  # < 1 hr
            return 30000 + int(3600 - seconds_away)
        elif seconds_away <= 3 * 3600:  # 1-3 hrs
            return 20000 + int((3 * 3600 - seconds_away) / 2)
        elif seconds_away <= 12 * 3600:  # 3-12 hrs
            return 15000 + int((12 * 3600 - seconds_away) / 10)
        elif seconds_away <= 24 * 3600:  # 12-24 hrs
            return 10000 + int((24 * 3600 - seconds_away) / 20)
        else:  # 24-48 hrs
            return 7500 + int((48 * 3600 - seconds_away) / 40)
    else:
        if seconds_away <= 3600:
            return 5000 + int(3600 - seconds_away)
        elif seconds_away <= 3 * 3600:
            return 2500 + int((3 * 3600 - seconds_away) / 2)
        elif seconds_away <= 12 * 3600:
            return 1000 + int((12 * 3600 - seconds_away) / 10)
        elif seconds_away <= 24 * 3600:
            return 500 + int((24 * 3600 - seconds_away) / 20)
        else:
            return 250


def scan_schedule_and_boost_priorities(
    channels_path: str = DEFAULT_CHANNELS,
    db_path: str = DEFAULT_DB,
    queue_path: str = QUEUE_FILE,
    lookahead_hours: float = 48.0,
) -> Dict[str, Any]:
    """
    Simulate the next 48h schedule and boost priority of items in the queue
    based on proximity to air time.
    """
    queue = load_queue(queue_path)
    items = queue.get("items", {})
    if not items:
        return {"scanned_channels": 0, "boosted_items": 0}

    # Reset schedule metadata for pending items
    for it in items.values():
        if it["status"] == "pending":
            it["next_air_time"] = None
            it["next_air_channel"] = None
            it["effective_priority"] = calculate_priority(it, None)

    # Load durations
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT replace(lower(Id), '-', ''), RunTimeTicks / 10000000.0 FROM BaseItems WHERE RunTimeTicks IS NOT NULL;")
    durations = dict(cur.fetchall())
    conn.close()

    # Load channels
    with open(channels_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    now = time.time()
    lookahead_seconds = lookahead_hours * 3600.0

    boosted = 0
    scanned_channels = 0

    for ch in config.get("channels", []):
        if not ch.get("enabled", True):
            continue
        scanned_channels += 1
        slots = schedule_projector.project_channel_schedule(ch, now, lookahead_seconds, durations)

        for slot in slots:
            jid = slot["jellyfinId"].lower()
            if jid in items:
                it = items[jid]
                if it["status"] != "pending":
                    continue

                air_time = slot["airTime"]
                seconds_away = max(0.0, air_time - now)

                # Keep earliest air time across any channel
                if it["next_air_time"] is None or air_time < it["next_air_time"]:
                    it["next_air_time"] = air_time
                    it["next_air_channel"] = ch.get("name")

                    it["effective_priority"] = calculate_priority(it, seconds_away)
                    boosted += 1

    save_queue(queue, queue_path)
    return {
        "scanned_channels": scanned_channels,
        "boosted_items": boosted,
        "lookahead_hours": lookahead_hours,
    }


def get_next_job(queue_path: str = QUEUE_FILE) -> Optional[Dict[str, Any]]:
    """Return the highest priority pending job from the queue."""
    queue = load_queue(queue_path)
    items = queue.get("items", {})

    pending = [it for it in items.values() if it["status"] == "pending"]
    if not pending:
        return None

    # Sort descending by effective_priority, then ascending by next_air_time
    pending.sort(
        key=lambda x: (
            -x.get("effective_priority", 100),
            x.get("next_air_time") if x.get("next_air_time") is not None else float("inf")
        )
    )
    return pending[0]


def print_top_queue(limit: int = 25, queue_path: str = QUEUE_FILE):
    queue = load_queue(queue_path)
    items = list(queue.get("items", {}).values())
    if not items:
        print("Queue is empty. Run with --init to populate.")
        return

    now = time.time()
    pending = [it for it in items if it["status"] == "pending"]
    pending.sort(
        key=lambda x: (
            -x.get("effective_priority", 100),
            x.get("next_air_time") if x.get("next_air_time") is not None else float("inf")
        )
    )

    print("=" * 80)
    print(f" CLASSIC TV RE-ENCODE QUEUE (Total: {len(items)}, Pending: {len(pending)}, Completed: {queue.get('completed', 0)})")
    print("=" * 80)
    print(f"{'PRIORITY':<10} {'AIRS IN':<12} {'CHANNEL':<20} {'SERIES / EPISODE'}")
    print("-" * 80)

    for it in pending[:limit]:
        prio = it.get("effective_priority", 100)
        air_t = it.get("next_air_time")
        if air_t is not None:
            diff_h = (air_t - now) / 3600.0
            airs_in = f"{diff_h:.1f}h" if diff_h >= 0 else "NOW"
        else:
            airs_in = "--"
        ch = it.get("next_air_channel") or "--"
        s_ep = f"S{it.get('season')}E{it.get('episode')}"
        name = f"{it.get('series')} {s_ep} - {it.get('title')}"
        print(f"{prio:<10} {airs_in:<12} {ch[:19]:<20} {name[:36]}")
    print("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="Classic TV Re-encoding Priority Queue Manager")
    parser.add_argument("--init", action="store_true", help="Initialize queue with all fatal library items")
    parser.add_argument("--scan", action="store_true", help="Scan schedule and boost upcoming problem episodes")
    parser.add_argument("--top", type=int, default=0, help="Print top N items in queue")
    parser.add_argument("--reset-failed", action="store_true", help="Reset all failed items back to pending")
    parser.add_argument("--status", action="store_true", help="Print queue summary stats")
    parser.add_argument("--queue-file", default=QUEUE_FILE, help="Path to reencode_queue.json")
    args = parser.parse_args()

    if args.init:
        added = populate_queue_from_library(queue_path=args.queue_file)
        print(f"Populated queue with {added} fatal items at {args.queue_file}")

    if args.scan:
        res = scan_schedule_and_boost_priorities(queue_path=args.queue_file)
        print(f"Scanned {res['scanned_channels']} channels over {res['lookahead_hours']}h: boosted {res['boosted_items']} items.")

    if args.reset_failed:
        count = reset_failed_jobs(queue_path=args.queue_file)
        print(f"Reset {count} failed items back to pending in {args.queue_file}")

    if args.status or (not args.init and not args.scan and args.top == 0):
        q = load_queue(args.queue_file)
        print(f"Queue Status ({args.queue_file}):")
        print(f"  Total Items : {q.get('total_items', 0)}")
        print(f"  Pending     : {q.get('pending', 0)}")
        print(f"  Re-encoding : {q.get('reencoding', 0)}")
        print(f"  Completed   : {q.get('completed', 0)}")
        print(f"  Failed      : {q.get('failed', 0)}")
        print(f"  Last Updated: {q.get('updated_at')}")

    if args.top > 0 or (not args.init and not args.scan and not args.status):
        limit = args.top if args.top > 0 else 20
        print_top_queue(limit=limit, queue_path=args.queue_file)


if __name__ == "__main__":
    main()
