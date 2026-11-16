#!/usr/bin/env python3
import sys
import os
import json
import time
import sqlite3
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import schedule_projector

channels_path = os.path.expanduser("~/projects/ClassicTV3.0/channels/channels-v4.json")
if not os.path.exists(channels_path):
    channels_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "channels", "channels-v4.json")

with open(channels_path) as f:
    config = json.load(f)

channel = next(c for c in config["channels"] if c["id"] == "sitcoms")

db_path = os.path.expanduser("~/Library/Application Support/jellyfin/data/jellyfin.db")
db = sqlite3.connect(db_path)
cur = db.cursor()
cur.execute("SELECT replace(lower(Id), '-', ''), RunTimeTicks / 10000000.0 FROM BaseItems WHERE RunTimeTicks IS NOT NULL;")
durations = dict(cur.fetchall())

now = time.time()
proj = schedule_projector.compute_schedule(channel, now, durations)

req = urllib.request.urlopen("http://localhost:8096/ClassicTV/channel/sitcoms")
live = json.loads(req.read().decode())

p_off = proj["startOffset"]
l_off = live["StartOffset"]
print("Python projection:", proj["title"], proj["jellyfinId"], round(p_off, 2))
print("Live Plugin API:  ", live["Title"], live["JellyfinItemId"], round(l_off, 2))
diff = abs(p_off - l_off)
print("Difference:", round(diff, 2), "s")
if proj["jellyfinId"] == live["JellyfinItemId"] and diff < 2.0:
    print("SUCCESS: 100% schedule match!")
else:
    print("MISMATCH")
