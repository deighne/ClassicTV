# Tier 4 Spec — Code Hygiene & Structural Improvements

**Status: PARTIAL.**
- **4.1 — attempted then REVERTED.** The command queue was implemented (commit `2651757`) and
  reverted (commit `0cc141f`) because it silently dropped key presses (a max-size-1 queue
  discards on full). Dispatch in `service.py` is currently direct; the queue scaffolding
  remains but is unused. A future attempt must not drop input — see the note in `service.py`.
- **4.2 — not done.**
- **4.3 — DONE.** v3 schedule service decommissioned; plist archived to `legacy/`.
- **4.4 — done in part; this file and `AGENTS.md` are the current reconciliation.**
- **4.5 — not done.** Channel numbers are still positional (`i + 1`) in the addon. See
  `channel-management-ui-spec.md` for the server-authoritative `number` field proposal.

**Dependencies:** None block Tier 4; Tier 2 restructures `modes.py` which Tier 4 builds on.

---

## 4.1 Non-blocking notification dispatch

**File:** `addon4/resources/lib/player.py`, `addon4/service.py`

**Problem:** `ClassicTVMonitor.onNotification()` dispatches `ch_up`/`ch_down` directly to
`_change_channel()`, which blocks for up to 5s (2s decoder poll + 3s fetch). Kodi's
notification dispatch thread is blocked during this time. The original v3 spec required
dispatch "within 1 ms" (Req 16).

**Fix:** Route channel changes through a single-slot command queue drained by a worker
thread. `onNotification` enqueues the intent and returns immediately.

```python
# In service.py, after constructing player:
import queue
_command_queue: queue.Queue = queue.Queue(maxsize=1)

def _command_worker() -> None:
    while True:
        try:
            cmd = _command_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        if cmd is None:
            break  # shutdown signal
        cmd()

_cmd_thread = threading.Thread(target=_command_worker, daemon=True, name="classictv-cmd")
_cmd_thread.start()

# In ClassicTVMonitor.onNotification():
# Replace: self._player.handle_ch_up()
# With:
try:
    _command_queue.put_nowait(self._player.handle_ch_up)
except queue.Full:
    xbmc.log("[ClassicTV][monitor] command queue full — ch_up dropped", xbmc.LOGWARNING)
```

`maxsize=1` means if the queue already has a pending channel change, the new button press
is dropped rather than queued. This is correct for a TV remote — if the user is mashing
ch+, the in-flight change takes priority over additional presses.

`toggle_mode` (Tier 2) should also go through this queue.

**Tests required:**
- `test_service_monitor.py`: `test_notification_returns_immediately` — verify
  `onNotification` returns before the command executes (mock the queue).

---

## 4.2 Constructor injection for collaborators

**Files:** `addon4/resources/lib/player.py`, `addon4/service.py`

**Current state:** `service.py` wires collaborators by assigning private attributes:
```python
player._schedule_client = schedule_client
player._channel_list = channel_list
player._modes = modes
modes.player = player
```

This circular injection pattern makes unit testing require careful setup and makes
collaborators easy to forget.

**Fix:** Introduce a `Runtime` container passed at construction:

```python
# addon4/resources/lib/runtime.py
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class Runtime:
    schedule_client: Optional["ScheduleClient"] = None
    channel_list: Optional["ChannelList"] = None
    modes: Optional["ModeState"] = None
    channel_static: Optional[object] = None
    channel_bug: Optional[object] = None
```

`ClassicTVPlayer.__init__(self, runtime: Runtime | None = None)` — reads from `runtime`
if provided, falls back to `None` for each field. Tests construct `Runtime` with only the
collaborators they need. `service.py` builds one `Runtime` and passes it everywhere.

This is a refactor, not a behavior change. Do it incrementally: add `Runtime` and pass it
while keeping the old attribute assignment for compatibility, then remove the old pattern.

---

## 4.3 Decommission v3 schedule service

**Files:** `deploy/deploy-jfmac.sh`, `launchd/com.classictv.schedule_service.plist`,
`server/`, `channels/`

**Current state:** `com.classictv.schedule_service` is still running on jfmac under
launchd. `localhost:8300/health` returns `{"status": "ok", "version": "3.0.0"}`. The v4
Kodi addon never calls `:8300` — it calls the Jellyfin plugin at `:8096/ClassicTV/…`.
Running both is a latent confusion risk and keeps jfmac's Python schedule service alive
unnecessarily.

**Steps:**
1. Confirm the v4 addon does not reference `8300` anywhere: `grep -r "8300" addon4/`.
2. On jfmac: `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.classictv.schedule_service.plist`
3. Remove the plist from `launchd/` in the repo, or archive it under `legacy/`.
4. Update `deploy/deploy-jfmac.sh` to remove the service reload step, and add a check
   that the service is NOT running.
5. Update `docs/classic-tv-4-architecture.md` Phase 3 status to "complete".
6. Leave `server/` source in the repo for historical reference but add a `README` note
   that it is decommissioned as of v4.0.

**Verification:** After decommissioning, `curl http://192.168.1.207:8300/health`
returns a connection error. All channels still serve correctly via `:8096/ClassicTV/…`.

---

## 4.4 Documentation reconciliation

**Files:** `addon4/resources/lib/player.py` (docstring), `docs/classic-tv-4-architecture.md`,
`docs/classic-tv-mode-switching-spec.md`

**Specific fixes:**

1. `player.py:15-18` — module docstring says "Classic TV 4.0 does NOT subclass `xbmc.Player`"
   and "never calls `play()`". First sentence is false (`ClassicTVPlayer(xbmc.Player)` at
   line 47). Rewrite to: "Classic TV subclasses `xbmc.Player` to receive callbacks but
   never calls `play()` or `seekTime()` directly. All playback is via `PlayMedia()`."

2. `player.py:77` — docstring still says `RunPlugin`. Replace with `PlayMedia`.

3. `docs/classic-tv-4-architecture.md` — update migration table:
   - Phase 1: "COMPLETE — plugin 10.11.0.6 live, 18 channels configured"
   - Phase 3: "IN PROGRESS — v3 schedule service decommissioned (Tier 4.3)"
   - Q2 note: confirm `server_id` is intentionally omitted from plugin:// URL.

4. `docs/classic-tv-mode-switching-spec.md` — replace `_last_play_url` language with
   `_last_played_item_id` / `jellyfin_playing_id` window property as the correlation key
   (per Tier 2 spec). Mark open questions resolved once Tier 1.1 findings are in.

---

## 4.5 Stable channel numbers

**Files:** `addon4/resources/lib/channels.py`, `Jellyfin.Plugin.ClassicTV` (server plugin)

**Current state:** Channel numbers are `i + 1` (positional, `channels.py:51`). If a
channel is inserted in the middle of the list, all subsequent numbers shift.

**Fix — server-side:** Add a `number` field to `ChannelConfig` in the server plugin's
`Models.cs` and populate it in the `channels.json` config. The `ChannelListItem` response
from `GET /ClassicTV/channels` returns it.

**Fix — client-side:** `ChannelList._load()` reads `number` from the response instead of
using `i + 1`. Falls back to positional if field is absent (backward compatibility with
old server plugin versions).

**Channel number assignments for current 18 channels:**
These should be assigned in `channels-v4.json` and the plugin config. Suggested allocation
(modeled on real cable channel numbers from the era):

| # | Channel |
|---|---|
| 2 | Grown-Up Cartoons |
| 3 | It's 1997 |
| 4 | Sitcoms |
| 5 | Nickelodeon Cartoons |
| 6 | Edutainment 4 Kidz |
| 7 | Cartoon Network |
| 8 | Disney Cartoons |
| 9 | Anime |
| 11 | Superheroes |
| 12 | Classic Cartoons |
| 13 | Ooky Spooky |
| 14 | Live Action |
| 15 | Late Night |
| 22 | It's 2007 |
| 25 | It's 2017 |
| 33 | Edutainment |
| 40 | Trash TV |
| 55 | Granny's House |

Numbers are not required to be contiguous. Entering `9` on the remote goes to Anime
directly. Gaps are fine — "channel not found" handles unassigned numbers.

**This is a server plugin change (v10.11.0.7) + client change + config update.**
