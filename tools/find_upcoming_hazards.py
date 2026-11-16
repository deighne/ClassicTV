#!/usr/bin/env python3
"""
Find problem episodes scheduled to air in the next 24/48 hours across all Classic TV channels.
"""

import os
import sys
import json
import time
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schedule_projector
import audit_media_library

channels_path = os.path.expanduser("~/projects/ClassicTV3.0/channels/channels-v4.json")
if not os.path.exists(channels_path):
    channels_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "channels", "channels-v4.json")

with open(channels_path) as f:
    config = json.load(f)

db_path = os.path.expanduser("~/Library/Application Support/jellyfin/data/jellyfin.db")
db = sqlite3.connect(db_path)
cur = db.cursor()

# 1. Load durations
cur.execute("SELECT replace(lower(Id), '-', ''), RunTimeTicks / 10000000.0 FROM BaseItems WHERE RunTimeTicks IS NOT NULL;")
durations = dict(cur.fetchall())

# 2. Load all fatal problem items
sql_streams = """
SELECT 
    b.Id, b.SeriesName, b.Name, b.ParentIndexNumber, b.IndexNumber, b.Path,
    m.Codec, m.Profile, m.PixelFormat, m.BitDepth, m.Width, m.Height
FROM MediaStreamInfos m
JOIN BaseItems b ON m.ItemId = b.Id
WHERE m.Codec = 'hevc' AND m.StreamType = 1;
"""
rows = cur.execute(sql_streams).fetchall()
problem_items = {}
for r in rows:
    item_id, series, name, s_idx, ep_idx, path, codec, prof, pix_fmt, depth, w, h = r
    clean_id = (item_id or "").replace("-", "").lower()
    is_10bit = ("10" in (pix_fmt or "")) or (depth and depth > 8)

    reasons = []
    if prof == "Main" and is_10bit:
        reasons.append("HEVC_PROFILE_10BIT_CONFLICT")
    if is_10bit and w and (w % 8 != 0):
        reasons.append(f"HEVC_10BIT_WIDTH_NOT_MOD8({w})")

    if reasons:
        problem_items[clean_id] = {
            "id": clean_id,
            "series": series or name,
            "title": name,
            "season": s_idx,
            "episode": ep_idx,
            "path": path,
            "issues": reasons,
        }

print(f"Total FATAL problem items identified in library: {len(problem_items)}")

# 3. Project schedule forward 24 hours (86,400s)
now = time.time()
lookahead = 24 * 3600

upcoming_hazards = []

for ch in config.get("channels", []):
    if not ch.get("enabled", True):
        continue
    slots = schedule_projector.project_channel_schedule(ch, now, lookahead, durations)
    for slot in slots:
        jid = slot["jellyfinId"].lower()
        if jid in problem_items:
            prob = problem_items[jid]
            hours_away = (slot["airTime"] - now) / 3600.0
            upcoming_hazards.append({
                "channel": ch.get("name"),
                "channel_id": ch.get("id"),
                "hours_away": hours_away,
                "air_time_unix": slot["airTime"],
                "series": prob["series"],
                "season": prob["season"],
                "episode": prob["episode"],
                "title": prob["title"],
                "path": prob["path"],
                "issues": prob["issues"],
            })

upcoming_hazards.sort(key=lambda x: x["hours_away"])

print(f"\nFATAL EPISODES AIRING IN THE NEXT 24 HOURS: {len(upcoming_hazards)}")
print("-" * 80)
for h in upcoming_hazards[:30]:
    in_time = f"in {h['hours_away']:.1f}h" if h['hours_away'] >= 0 else "NOW"
    print(f"[{in_time:<8}] {h['channel']:<22} | {h['series']} S{h['season']}E{h['episode']} - {h['title']}")
    print(f"           Issue: {', '.join(h['issues'])}")
