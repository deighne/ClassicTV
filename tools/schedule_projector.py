#!/usr/bin/env python3
"""
tools/schedule_projector.py

Pure-Python implementation of the Classic TV 4.0 ScheduleEngine.
Deterministic schedule calculation identical to Jellyfin.Plugin.ClassicTV.Schedule.ScheduleEngine.

Used to project the broadcast schedule forward in time (e.g. next 6h, 24h, 48h)
to detect upcoming crash hazards and prioritise background re-encoding queues.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import zoneinfo

CHICAGO_TZ = zoneinfo.ZoneInfo("America/Chicago")
FALLBACK_DURATION_SECONDS = 1800.0
SECONDS_PER_DAY = 86400


def parse_anchor(anchor_str: str) -> int:
    """Parse ISO8601 anchor string like '2024-01-01T00:00:00Z' into Unix seconds."""
    dt = datetime.fromisoformat(anchor_str.replace("Z", "+00:00"))
    return int(dt.timestamp())


def block_is_active(start_hour: int, end_hour: int, local_hour: int) -> bool:
    if end_hour > start_hour:
        return start_hour <= local_hour < end_hour
    else:
        return local_hour >= start_hour or local_hour < end_hour


def find_active_block(blocks: List[Dict[str, Any]], local_hour: int) -> Optional[Dict[str, Any]]:
    for block in blocks:
        s = block.get("startHour", 0)
        e = block.get("endHour", 24)
        if block_is_active(s, e, local_hour):
            return block
    return None


def block_duration_per_day(start_hour: int, end_hour: int) -> int:
    if end_hour > start_hour:
        return (end_hour - start_hour) * 3600
    else:
        return (24 - start_hour + end_hour) * 3600


def seconds_into_block_window(start_hour: int, end_hour: int, local_dt: datetime) -> float:
    seconds_into_day = local_dt.hour * 3600 + local_dt.minute * 60 + local_dt.second + (local_dt.microsecond / 1_000_000.0)
    if end_hour > start_hour:
        window_start = start_hour * 3600
        return max(0.0, seconds_into_day - window_start)
    else:
        window_start = start_hour * 3600
        if local_dt.hour >= start_hour:
            return seconds_into_day - window_start
        else:
            pre_midnight = SECONDS_PER_DAY - window_start
            return pre_midnight + seconds_into_day


def block_aware_elapsed(start_hour: int, end_hour: int, anchor_unix: int, unix_seconds: float, local_dt: datetime) -> float:
    b_dur = block_duration_per_day(start_hour, end_hour)
    if b_dur == SECONDS_PER_DAY:
        return max(0.0, unix_seconds - anchor_unix)

    total_wall = max(0.0, unix_seconds - anchor_unix)
    completed_days = int(total_wall / SECONDS_PER_DAY)
    today_block = seconds_into_block_window(start_hour, end_hour, local_dt)
    return (completed_days * b_dur) + today_block


def fnv1a_seed(channel_id: str, block_name: str, show_name: str, cycle_index: int) -> int:
    """FNV-1a 32-bit hash matching C# SeedFor in ScheduleEngine."""
    hash_val = 2166136261
    prime = 16777619

    def add_part(part: str):
        nonlocal hash_val
        for c in part:
            code = ord(c)
            hash_val ^= (code & 0xFF)
            hash_val = (hash_val * prime) & 0xFFFFFFFF
            hash_val ^= ((code >> 8) & 0xFF)
            hash_val = (hash_val * prime) & 0xFFFFFFFF
        hash_val ^= 0xFF
        hash_val = (hash_val * prime) & 0xFFFFFFFF

    add_part(channel_id or "")
    add_part(block_name or "")
    add_part(show_name or "")
    add_part(str(cycle_index))

    # Convert unsigned 32-bit to signed 32-bit int matching C# (int)hash
    if hash_val >= 0x80000000:
        return hash_val - 0x100000000
    return hash_val


class DotNetRandom:
    """
    Simulates .NET's System.Random (Knuth Subtractive Generator)
    so seeded show shuffles match C# bit-for-bit.
    """
    MBIG = 2147483647
    MSEED = 161803398

    def __init__(self, seed: int):
        self.seed_array = [0] * 56
        subtraction = self.MBIG if seed == -2147483648 else abs(seed)
        mj = (self.MSEED - subtraction) % self.MBIG
        if mj < 0:
            mj += self.MBIG
        self.seed_array[55] = mj
        mk = 1
        for i in range(1, 55):
            ii = (21 * i) % 55
            self.seed_array[ii] = mk
            mk = (mj - mk) % self.MBIG
            if mk < 0:
                mk += self.MBIG
            mj = self.seed_array[ii]

        for k in range(1, 5):
            for i in range(1, 56):
                val = (self.seed_array[i] - self.seed_array[1 + (i + 30) % 55]) % self.MBIG
                if val < 0:
                    val += self.MBIG
                self.seed_array[i] = val
        self.inext = 0
        self.inextp = 21

    def next(self, max_value: int) -> int:
        if max_value <= 0:
            return 0
        self.inext = (self.inext + 1) if self.inext < 55 else 1
        self.inextp = (self.inextp + 1) if self.inextp < 55 else 1
        ret_val = self.seed_array[self.inext] - self.seed_array[self.inextp]
        if ret_val == self.MBIG:
            ret_val -= 1
        if ret_val < 0:
            ret_val += self.MBIG
        self.seed_array[self.inext] = ret_val
        return int((ret_val / self.MBIG) * max_value)


def fisher_yates_shuffle(items: List[str], seed: int) -> List[str]:
    res = list(items)
    rng = DotNetRandom(seed)
    n = len(res)
    while n > 1:
        k = rng.next(n)
        n -= 1
        res[k], res[n] = res[n], res[k]
    return res


def expand_block_episodes(shows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Weighted Round-Robin expansion matching C# ScheduleEngine.ExpandBlockEpisodes.
    """
    valid_shows = [s for s in shows if len(s.get("jellyfinIds", [])) > 0]
    if not valid_shows:
        return []

    n = len(valid_shows)
    total_episodes = sum(len(s["jellyfinIds"]) for s in valid_shows)
    weight_sum = sum(s.get("weight", 1.0) for s in valid_shows)
    if weight_sum <= 0.0:
        weight_sum = float(n)

    deficit = [max(s.get("weight", 1.0), 0.0) / weight_sum for s in valid_shows]
    cursors = [0] * n
    remaining = [len(s["jellyfinIds"]) for s in valid_shows]
    slots = []

    for _ in range(total_episodes):
        best = -1
        for i in range(n):
            if remaining[i] <= 0:
                continue
            if best == -1 or deficit[i] > deficit[best]:
                best = i

        if best == -1:
            break

        show = valid_shows[best]
        jid = show["jellyfinIds"][cursors[best]]
        slots.append({
            "jellyfinId": jid,
            "title": show.get("name", ""),
            "showName": show.get("name", ""),
            "duration": 0.0,
        })

        cursors[best] += 1
        remaining[best] -= 1
        deficit[best] -= 1.0
        for i in range(n):
            deficit[i] += max(valid_shows[i].get("weight", 1.0), 0.0) / weight_sum

    return slots


def compute_schedule(
    channel: Dict[str, Any],
    unix_seconds: float,
    durations: Dict[str, float],
) -> Optional[Dict[str, Any]]:
    """
    Compute current slot for channel at unix_seconds.
    Returns dict with keys: channelId, jellyfinId, title, showName, startOffset, duration, airTime, endTime.
    """
    utc_dt = datetime.fromtimestamp(unix_seconds, tz=timezone.utc)
    local_dt = utc_dt.astimezone(CHICAGO_TZ)
    local_hour = local_dt.hour

    blocks = channel.get("blocks", [])
    block = find_active_block(blocks, local_hour)
    if not block or block.get("offline", False):
        return None

    shows = block.get("shows", [])
    if not shows:
        return None

    # Prune shows to only IDs present in durations (same as ScheduleEngine.PruneMissingEpisodes)
    pruned_shows = []
    for s in shows:
        kept = [jid for jid in s.get("jellyfinIds", []) if jid.lower() in durations]
        if kept:
            pruned_shows.append({
                "name": s.get("name", ""),
                "weight": s.get("weight", 1.0),
                "shuffle": s.get("shuffle", False),
                "jellyfinIds": kept,
            })

    if not pruned_shows:
        return None

    s_hour = block.get("startHour", 0)
    e_hour = block.get("endHour", 24)
    anchor_str = channel.get("startAnchor", "2024-01-01T00:00:00Z")
    anchor_unix = parse_anchor(anchor_str)

    elapsed = block_aware_elapsed(s_hour, e_hour, anchor_unix, unix_seconds, local_dt)

    # Cycle duration
    cycle_duration = sum(
        sum(durations.get(jid.lower(), FALLBACK_DURATION_SECONDS) for jid in s["jellyfinIds"])
        for s in pruned_shows
    )
    if cycle_duration <= 0.0:
        return None

    # Cycle index and shuffle
    cycle_index = int(elapsed / cycle_duration)
    shuffled_shows = []
    for s in pruned_shows:
        if s.get("shuffle", False) and len(s["jellyfinIds"]) > 0:
            seed = fnv1a_seed(channel.get("id", ""), block.get("name", ""), s.get("name", ""), cycle_index)
            shuffled_ids = fisher_yates_shuffle(s["jellyfinIds"], seed)
            shuffled_shows.append({**s, "jellyfinIds": shuffled_ids})
        else:
            shuffled_shows.append(s)

    # Expand slots
    slots = expand_block_episodes(shuffled_shows)
    if not slots:
        return None

    # Attach durations
    for slot in slots:
        slot["duration"] = durations.get(slot["jellyfinId"].lower(), FALLBACK_DURATION_SECONDS)

    position = elapsed % cycle_duration

    for slot in slots:
        position -= slot["duration"]
        if position < 0:
            seek_offset = slot["duration"] + position
            air_time = unix_seconds - seek_offset
            end_time = air_time + slot["duration"]
            return {
                "channelId": channel.get("id"),
                "channelName": channel.get("name"),
                "jellyfinId": slot["jellyfinId"],
                "title": slot["title"],
                "showName": slot["showName"],
                "startOffset": seek_offset,
                "duration": slot["duration"],
                "airTime": air_time,
                "endTime": end_time,
            }

    return None


def project_channel_schedule(
    channel: Dict[str, Any],
    start_time: float,
    lookahead_seconds: float,
    durations: Dict[str, float],
) -> List[Dict[str, Any]]:
    """
    Project future episode schedule for channel over lookahead_seconds.
    """
    scheduled_items = []
    current_t = start_time
    end_limit = start_time + lookahead_seconds

    seen_slots = set()

    while current_t < end_limit:
        slot = compute_schedule(channel, current_t, durations)
        if not slot:
            # Step forward by 15 min if offline
            current_t += 900
            continue

        slot_key = (slot["jellyfinId"], int(slot["airTime"]))
        if slot_key not in seen_slots:
            seen_slots.add(slot_key)
            scheduled_items.append(slot)

        # Advance to end of this episode + 1 second
        remaining = max(1.0, slot["duration"] - slot["startOffset"])
        current_t += remaining + 1.0

    return scheduled_items
