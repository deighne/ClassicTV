# Classic TV 3.0 — System Specification

**Status:** **HISTORICAL.** This documents the decommissioned 3.0 system (Python
Schedule_Service, `script.classictv.service` addon). The live system is Classic TV 4.0,
documented in [`AGENTS.md`](AGENTS.md) and
[`docs/classic-tv-4-architecture.md`](docs/classic-tv-4-architecture.md). The `server/`,
`addon/`, and `tests/` directories referenced below are reference-only.
**Date:** 2026-09-08  
**Author:** Dane Dickerson

---

## 1. What This System Does

Classic TV is a personal TV broadcast system. It simulates the experience of watching cable television circa 1990–2010 on a real CRT television. A library of TV shows, cartoons, and music videos plays continuously on themed "channels," synchronized to wall-clock time so that every viewer sees the same thing at the same moment. You tune to a channel and you're dropped into whatever is playing right now, mid-episode if necessary — exactly like real television.

The system runs 24 hours a day, 7 days a week, unattended.

---

## 2. Hardware

| Device | Role |
|--------|------|
| **Mac Mini (jfmac)** | Media server and brain. Runs Jellyfin (media library), the schedule service, and all web tooling. Always on. Accessible remotely via Cloudflare tunnel at `ssh.dane.photos`. |
| **Le Potato (Sweet Potato)** | Player. A small ARM single-board computer connected to the CRT via HDMI-to-component adapter. Runs CoreELEC 21.x (a Kodi-based embedded OS). Connected to the home LAN via WiFi. |
| **Sony KV-34HS510** | Display. A 34" widescreen CRT television. Connected to the Le Potato via component video. |
| **Sony RM-Y180** | IR remote. Controls channel up/down and guide. |

The Le Potato and jfmac are on the same home LAN (192.168.1.x). The Le Potato talks to jfmac directly at `http://192.168.1.207` for all media and schedule data. The Cloudflare tunnel is used only for remote SSH access to jfmac — it is never in the playback path.

---

## 3. Software Stack

| Layer | Technology |
|-------|-----------|
| Media library | **Jellyfin** — open source media server running on jfmac. Indexes all video files, provides HTTP streaming URLs and metadata. |
| Player OS | **CoreELEC** — a minimal Linux distribution built around Kodi, running on the Le Potato. |
| Player application | **Kodi** with a custom Classic TV addon (a Python plugin). |
| Schedule service | A lightweight Python HTTP server running on jfmac. The single source of truth for what is playing on each channel at any given moment. |
| Web tools | Static HTML/JS pages served from **tv.dane.photos** via Cloudflare Pages or a single nginx/caddy reverse proxy on jfmac. All browser-accessible tools live here — no bespoke servers, no port hunting. |
| Configuration | YAML files on jfmac. One file per channel. One global config file. |

---

## 4. Core Concept: The Schedule Service

The schedule service is the central innovation of v3.0 and the biggest departure from previous designs.

### What It Is

A small HTTP API running on jfmac (port 8300, proxied at `tv.dane.photos/api`). It has one primary job: given a channel name and a timestamp, tell the player what to play.

### The Primary Endpoint

```
GET /api/schedule/{channel_id}?t={unix_timestamp}
```

Response:
```json
{
  "channel_id": "nickelodeon",
  "item": {
    "jellyfin_id": "abc123...",
    "title": "Hey Arnold! - S02E04 - The Little Pink Book",
    "seek_offset": 742.3,
    "duration": 1320,
    "direct_play_url": "http://192.168.1.207:8096/Videos/abc123.../stream?..."
  },
  "next_item": {
    "jellyfin_id": "def456...",
    "title": "Hey Arnold! - S02E05 - The Vacant Lot",
    "direct_play_url": "http://192.168.1.207:8096/Videos/def456.../stream?..."
  },
  "generated_at": 1757289600
}
```

### What the Service Handles (That the Player Does Not)

- **Schedule computation.** The service knows the channel's 24-hour rotation and computes which item is playing at time `t` and what the seek offset is. The player just asks and plays.
- **Playability validation.** Before returning an item, the service verifies the Jellyfin ID is valid and the item supports direct play on the Le Potato. If it does not, the service substitutes a fallback item automatically.
- **Dead ID handling.** If Jellyfin re-indexes and an ID changes, the service detects it at query time and resolves the replacement. The player never receives a 404.
- **Bumpers.** Bumper clips are treated as regular schedule items. They appear as `type: "bumper"` in the schedule and have valid direct play URLs. The player plays them the same way it plays episodes.
- **The offline/fallback clip.** When no valid content can be found for a time slot, the service returns a special `type: "offline"` item — a looping "nothing on air" video. The player always has something to play.

### What the Service Does Not Handle

The service is stateless and has no knowledge of what the player is currently doing. It does not push to the player, does not track playback state, and does not make decisions about when to retune.

---

## 5. Channels

Each channel is defined in a YAML file on jfmac. Example:

```yaml
# channels/nickelodeon.yaml
id: nickelodeon
name: Nickelodeon
number: 4
color: "#FF6600"

blocks:
  - name: Morning
    start_hour: 6
    end_hour: 12
    shows:
      - name: "Hey Arnold!"
        weight: 3
      - name: "Rugrats"
        weight: 2
      - name: "Doug"
        weight: 1

  - name: Afternoon
    start_hour: 12
    end_hour: 18
    shows:
      - name: "Kenan & Kel"
        weight: 2
      - name: "All That"
        weight: 2

  - name: Overnight
    start_hour: 0
    end_hour: 6
    offline: true
```

Channels with `offline: true` during a block play the offline clip during that window. All times are local (America/Chicago).

The channel list and their numbers are defined in a single `channels.yaml` global config.

---

## 6. The Player Addon

The Classic TV addon runs inside Kodi on the Le Potato. It is deliberately simple — all the intelligence lives in the schedule service.

### Startup

1. Fetch the schedule for the default startup channel.
2. Play the returned item at the returned seek offset.

### During Playback

Nothing. The player does not poll. It does not check the clock. It does not manage state. It plays the item.

### On EOF (Episode Ends)

1. Fetch the schedule for the current channel at `now`.
2. Play what comes back.

This is the complete retune logic. The schedule service handles all edge cases — if we're mid-slot, the seek offset accounts for it. If we're at a bumper boundary, the bumper comes back. If the next episode can't play, the offline clip comes back.

### On Channel Change (CH UP / CH DOWN)

1. Stop current playback.
2. Wait for the Amlogic decoder to fully release (poll `isPlaying()` until false, max 2s, then proceed regardless).
3. Fetch the schedule for the new channel at `now`.
4. Show channel static overlay.
5. Play.

### Watchdog

A simple background check every 10 seconds: if nothing is playing, fetch the schedule and play. That's the complete watchdog — no loop detection, no hold mode, no thread management. The schedule service guarantees something playable comes back.

### Playback Strategy: Direct Play First

The player uses Jellyfin's **direct play** path as the primary and strongly preferred strategy. Do not use HLS transcode as the default.

**Direct play** means Kodi fetches the raw media file directly from Jellyfin's HTTP endpoint (`/Videos/{id}/stream?Static=true`) and plays it natively. No re-encoding, no ffmpeg, no segment buffering. The Amlogic S905X in the Le Potato has hardware H264 and HEVC decoders that handle this natively.

**Why not HLS by default?** The v2.0 system started with HLS-first (Jellyfin PlaybackInfo POST → `.m3u8` URL). It worked but had meaningful downsides: every channel switch required a PlaybackInfo network round-trip, ffmpeg had to initialize on jfmac to serve segments, and segment-based seeking was unreliable. Switching to direct play reduced channel switch time from ~7s to ~2-4s on the LAN and eliminated the ffmpeg dependency on the playback path entirely.

**The seek problem.** Time-synced channels require starting playback mid-episode at a specific offset — e.g. "this episode started 14 minutes ago, play from 14:00." Getting this right quickly and reliably is one of the hardest parts of the system and is worth careful research before committing to an implementation approach. The v2.0 system used a pause-seek-retry loop after playback started, which worked but added latency. Whether v3.0 can do better — via `StartTimeTicks`, HLS with a start offset, or another mechanism — should be evaluated during the design phase with current Jellyfin and CoreELEC versions before any approach is baked in.

**HLS transcode as a fallback only.** The schedule service will validate playability before returning an item. If a file cannot direct-play, the service should return the offline clip rather than silently fall back to HLS transcode. HLS transcode is slow (~8-15s), occasionally produces broken streams on certain file types, and has caused decoder hangs on the Amlogic hardware.

**Content library hygiene matters.** The fastest path to avoiding transcode is ensuring all media files in the library are H264 or HEVC in MKV or MP4 containers with a single audio track. Files with multiple audio tracks, unusual containers (MOV, AVI, DivX), or unsupported codecs will fail direct play. When adding media to the library, verify the container and audio track count.

### IR Remote Routing

Button presses from the Sony RM-Y180 are received via Kodi's NotifyAll mechanism (keyboard.xml sends `NotifyAll(classictv.service, ch_up)` etc.) and dispatched directly to the channel change / guide functions. No subprocess, no HTTP relay for the IR path.

---

## 7. Look and Feel

These three UI elements define the character of the system. They make it feel like a real television and not a media player. All three must be preserved exactly in v3.0.

### Channel Static (Snow)

When you change channels, the screen fills with full-screen TV static — a looping noise animation with the channel number overlaid. It plays for as long as it takes for the new stream to start, then dismisses automatically when playback begins. An 8-second safety timeout dismisses it if playback never starts (e.g. the stream fails to open).

This is a `WindowXMLDialog` displayed non-blocking via `show()` (not `doModal()`). It renders on top of whatever was playing before without stopping it. It must never steal focus — any button press during static should queue to the next action, not be swallowed.

The static animation is a skin asset (a looping animated texture or video). It does not require network access or Jellyfin — it is fully local.

### Channel Bug

After tuning to a new channel, a small overlay appears in the corner showing:
- Channel number (large)
- Channel name
- Current show title
- Channel color accent

It auto-dismisses after 6 seconds. The 6-second timer exists specifically to prevent CRT phosphor burn-in — this is not aesthetic, it is a hardware protection measure. Do not make the bug persistent.

Like the static overlay, it is non-blocking and never steals focus. Rapid channel changes close the previous bug and show a new one cleanly.

### Direct Channel Number Entry

Pressing number keys on the remote accumulates digits on screen. After 1.5 seconds of inactivity, the system tunes to the entered number. Pressing Enter/OK tunes immediately without waiting.

Example: pressing 1, then 2 within 1.5 seconds tunes to channel 12. Pressing 1 and waiting tunes to channel 1.

If the entered number does not match any channel, the overlay briefly shows "CH XX — Not Found" then dismisses. No error sound, no disruption to current playback.

A small `WindowXMLDialog` overlay shows the accumulating number (e.g. "CH 1_" then "CH 12") in large type on screen while digits are being entered.

---

## 8. IR Remote

The Sony RM-Y180 (SAT/CABLE mode) is the primary input device. It controls channel navigation, the guide, and direct channel number entry. It works via the Le Potato's built-in IR receiver (meson-ir, kernel 4.9, Sony SIRC 15-bit protocol).

### Why This Matters

The IR remote is one of the best parts of the system — pressing a physical remote and having the channel change on a real CRT feels exactly right. It must stay fast and reliable in v3.0.

### How It Works

The Le Potato's kernel receives IR signals and translates them to Linux key events via a keymap file (`/storage/.config/rc_keymaps/sony-rmy180`). Kodi reads those key events and fires them through its keymap (`addon/keymaps/keyboard.xml`), which maps each button to a `NotifyAll` call:

```xml
<channelup>NotifyAll(classictv.service, ch_up)</channelup>
<channeldown>NotifyAll(classictv.service, ch_down)</channeldown>
<epg>NotifyAll(classictv.service, guide)</epg>
<key id="1">NotifyAll(classictv.service, num_1)</key>
```

Kodi fires `NotifyAll` as an in-process event. The Classic TV service receives it via `onNotification()` on a `xbmc.Monitor` subclass and dispatches it to the appropriate handler in under 1ms.

**Important:** The previous approach (`RunScript(keypress.py, ch_up)`) spawned a new Python subprocess for each button press — ~1500ms latency on the Le Potato's ARM CPU. NotifyAll eliminates this entirely. Do not revert to RunScript for any reason.

### Button Map

| Button | Action |
|--------|--------|
| CH UP | Channel up |
| CH DOWN | Channel down |
| GUIDE / EPG | Toggle TV guide overlay |
| 0–9 | Direct channel number entry |
| ENTER / OK | Confirm channel number entry |
| EXIT / BACK | Close overlay |
| D-pad | Guide navigation |

### Direct Channel Number Entry

Pressing number keys accumulates digits with a 1.5-second timeout. Pressing 1 then 2 within 1.5 seconds tunes to channel 12. Pressing 1 then waiting tunes to channel 1. Pressing ENTER confirms immediately without waiting for the timeout.

### Deployment Note

The IR keymap file must be deployed to `/storage/.config/rc_keymaps/sony-rmy180` on the Le Potato and loaded via `ir-keytable -p SONY -w ...`. The provision script handles initial setup. The deploy script reloads the keymap on every deploy. The keymap format is the old non-TOML format — the Le Potato's kernel 4.9 does not support BPF and cannot use `.toml` keymaps.

---

## 9. Alternate Modes

The addon has three operating modes. Mode transitions must be clean — no state leakage between them.

### TV Mode (Default)

The normal state. The addon is in control of playback. A channel is tuned, the schedule service drives what plays, the watchdog keeps it alive. All of sections 5 and 6 apply.

### Movie Mode

The user launches a Jellyfin movie via Kodi's native Jellyfin addon (browsing the library, or from the guide's "Movies" entry). At that point **the Classic TV addon steps back entirely** — Kodi's built-in player and the Jellyfin addon own the session. The Classic TV addon sets a `_in_movie_mode` flag and suspends its watchdog.

When the movie ends (Kodi fires `onPlayBackEnded` or `onPlayBackStopped`), the addon detects that no Classic TV content is playing, clears the flag, and resumes the last tuned channel via the normal schedule fetch.

There is no custom movie player, no custom movie UI, no special handling of plugin:// URLs. Movie mode is default Kodi. The addon's only job is to resume TV when the movie is over.

### Rachel Mode (CH67)

Covered in the next section. Operationally it is TV Mode with a different data source — the rachel-anchor service instead of the schedule service.

---

## 10. Rachel Channel (CH67)

Rachel is a special channel that mirrors what a specific Jellyfin user (Rachel) is currently watching. It is the only channel that does not use the schedule service.

### How It Works

A separate service on jfmac (`rachel-anchor`) polls Rachel's active Jellyfin session every 2 seconds and exposes the current playback state:

```
GET /api/rachel-anchor
→ {
    "state": "playing",
    "jellyfin_id": "abc123...",
    "anchor_position": 1234.5,
    "anchor_timestamp": 1757289600.0,
    "total_duration": 2700.0
  }
```

When tuned to CH67, the addon polls this endpoint every 2 seconds and seeks to the computed position (`anchor_position + (now - anchor_timestamp)`). When Rachel's session is idle, CH67 plays the offline clip.

The rachel-anchor service is co-located with the schedule service on jfmac — same process, different route.

---

## 11. TV Guide

A full-screen guide overlay accessible via the Guide button on the remote. Shows now/next for all channels. Must feel instant — button press to visible guide in under 150ms on the Le Potato.

### Why v2.0 Was Slow

The v2.0 guide fetched each channel's sidecar file individually (20+ HTTP requests, each 1-2MB of JSON), parsed them on the Le Potato, ran schedule math per channel, and then built the Kodi dialog by calling `addItem()` in a loop — each call triggering a GUI redraw on the main thread. The result was 300-500ms to open, occasional memory pressure, and rate-limit errors under rapid re-opens.

### v3.0 Approach: One Call, Dumb Renderer

The schedule service exposes a dedicated guide endpoint:

```
GET /api/guide?t={unix_timestamp}
```

Response — the complete now/next for every channel, pre-computed server-side:

```json
{
  "channels": [
    {
      "id": "nickelodeon",
      "name": "Nickelodeon",
      "number": 4,
      "color": "#FF6600",
      "now": {
        "title": "Hey Arnold! - The Little Pink Book",
        "type": "episode",
        "minutes_remaining": 8
      },
      "next": {
        "title": "Hey Arnold! - The Vacant Lot",
        "type": "episode"
      }
    }
  ]
}
```

One HTTP request. No per-channel fetches. No schedule math on the Le Potato. The response is small by design — only what the guide UI needs to render, nothing more.

### Rendering

The guide dialog populates its list in a single pass from the flat channel array. All items are created in memory first, then added to the Kodi `ListContainer` in one call. No incremental GUI updates, no per-item network calls.

The guide response is also cached in memory for 30 seconds — rapid re-opens (accidental double-press) serve from cache with zero network overhead.

Selecting a channel from the guide tunes to it immediately and closes the overlay.

---

## 12. Media Cache and Prefetch

To minimize buffering on the Le Potato (which has limited RAM and an SD card), the addon maintains a local media cache on `/storage` (the SD card's data partition, up to 10GB).

### Prefetch

A background thread runs every 30 minutes. It calls `GET /api/schedule/{channel_id}?t=now+3600` for each channel to find what's coming up in the next hour, then pre-downloads those items to local storage via Jellyfin's direct download URL. Pre-downloaded items play from local storage, bypassing the network entirely.

### Eviction

LRU eviction — oldest items by last-accessed time are removed when the cache exceeds its size limit. Items currently playing are never evicted.

---

## 13. Web Tools at tv.dane.photos

All browser-accessible tools for managing and calibrating the system live at `tv.dane.photos`. This is a Cloudflare Pages site (or a simple nginx reverse proxy on jfmac — TBD based on whether tools need server-side logic).

No tool has its own server or its own port. No guessing "was it 8765 or 8766?"

### Tools

| URL | Purpose |
|-----|---------|
| `tv.dane.photos` | Dashboard — system status, what's playing on each channel, service health |
| `tv.dane.photos/guide` | Web-based TV guide — same data as the on-screen guide |
| `tv.dane.photos/crt` | KV-34HS510 geometry and convergence calibration guide — interactive HTML tool for aligning the CRT's geometry controls (pincushion, trapezoid, convergence) with annotated diagrams and a bug reporting form. Currently served from an ad-hoc server; this is its permanent home. |
| `tv.dane.photos/channels` | Channel manager — edit channel configs, adjust show weights, preview schedules |
| `tv.dane.photos/api` | Schedule service API (proxied from jfmac:8300) |

Any future HTML utility — test patterns, diagnostics, admin tools — goes here. Never a new server, never a new port.

### Hosting Model

Static HTML/JS tools (CRT guide, etc.) are committed to the repo under `web/` and deployed to Cloudflare Pages automatically on push to `main`. Tools that need live data (dashboard, channel manager) proxy to the schedule service API on jfmac via a Cloudflare Worker or a simple nginx config.

---

## 14. Services on jfmac

All services run as launchd agents and restart automatically on failure.

| Service | Port | Purpose |
|---------|------|---------|
| Jellyfin | 8096 | Media library and streaming |
| Classic TV Schedule Service | 8300 | Schedule API + rachel-anchor |
| cloudflared | — | Cloudflare tunnel for remote SSH |

That's it. No playlist daemon, no rachel sync server, no channel manager server, no guide server, no ad-hoc HTTP servers.

---

## 15. Repository Structure

```
classic-tv-3.0/
├── addon/                    Kodi addon (deployed to Le Potato)
│   ├── service.py            Entry point — starts on Kodi boot
│   └── resources/lib/
│       ├── player.py         Playback logic — fetch schedule, play, handle EOF
│       ├── guide.py          TV guide dialog — fetches /api/guide, renders flat list
│       ├── modes.py          Mode state machine — TV / Movie / Rachel
│       ├── channels.py       Channel list and metadata
│       ├── channel_static.py Full-screen snow overlay during channel switches
│       ├── channel_bug.py    Corner overlay showing channel/show info after tune
│       ├── channel_entry.py  Direct channel number entry — digit accumulation, 1.5s timeout
│       ├── media_cache.py    Local prefetch and LRU cache
│       └── remote_server.py  HTTP server for Android/external remotes (:8899)
│
├── server/                   Runs on jfmac
│   ├── schedule_service.py   Main HTTP server — schedule API + rachel-anchor
│   ├── scheduler.py          Schedule computation engine (pure functions)
│   ├── jellyfin.py           Jellyfin API client
│   └── config_loader.py      Loads and validates channel YAML configs
│
├── channels/                 Channel configuration (YAML, one file per channel)
│   ├── nickelodeon.yaml
│   ├── cartoon_network.yaml
│   ├── live_action.yaml
│   └── ...
│
├── web/                      Static web tools (deployed to tv.dane.photos)
│   ├── index.html            Dashboard
│   ├── crt/                  CRT geometry guide
│   └── channels/             Channel manager UI
│
├── deploy/                   Deployment scripts
│   ├── deploy-sweet-potato.sh
│   └── deploy-jfmac.sh
│
├── setup/                    Fresh device provisioning
│   ├── provision-sweet-potato.sh
│   └── provision-jfmac.sh
│
├── tests/                    Test suite
│   ├── test_scheduler.py     Schedule computation (pure function tests)
│   ├── test_player.py        Player logic
│   └── test_schedule_api.py  API integration tests
│
└── docs/
    ├── CLASSIC_TV_3_SPEC.md  This document
    └── KV-34HS510-GEOMETRY-CONVERGENCE-GUIDE.md
```

---

## 16. What Does Not Exist in v3.0

These concepts from v2.0 are intentionally absent:

| v2.0 Concept | Why It's Gone |
|--------------|---------------|
| Sidecar JSON files | Replaced by the live schedule API. No more stale files, no more daily regeneration, no more cache invalidation. |
| Playlist daemon | Subsumed by the schedule service. |
| PlaybackInfo cache | The schedule service validates playability server-side. The player never needs to ask Jellyfin about capabilities directly. |
| Hold mode | The schedule service always returns something playable. There is no scenario where the player has nothing to play. |
| `compute_sync_offset` | The schedule service handles all time math. |
| `bumper_jellyfin_ids.json` | Bumpers are regular items in the channel config. |
| Multiple launchd services | One service process on jfmac covers everything except Jellyfin. |
| Ad-hoc web servers on arbitrary ports | Everything lives at tv.dane.photos. |
| `channels.json` + `show_metadata.json` + `bumper_jellyfin_ids.json` split | One YAML file per channel. |

---

## 17. Engineering Practices

### Code Quality

**Pure functions where possible.** The schedule computation engine (`scheduler.py`) must be entirely pure functions with no side effects and no I/O. Given the same channel config and timestamp, it returns the same result. This makes it trivially testable and easy to reason about. The same applies to any schedule math on the addon side.

**Single responsibility.** Each module has one job. The player plays. The scheduler schedules. The guide renders. No module reaches into another module's domain. If you find yourself importing player functions from the guide, something is wrong.

**No magic numbers in code.** Timeouts, durations, limits, and ports are named constants at the top of the file they belong to, or in a shared `constants.py`. The 6-second channel bug timeout, the 1.5-second digit entry timeout, the 8-second static safety timeout — all named, all documented with why that specific value was chosen.

**Logging is diagnostic, not decorative.** Every log line must answer "why did this happen?" not just "this happened." Log the channel, the item, the reason, the seek offset. Use a consistent `[ClassicTV][module]` prefix on every line so grep works. Log at `INFO` for normal operations, `WARNING` for recoverable problems, `ERROR` for failures that affect the user.

---

### Testing

**Test the scheduler exhaustively.** Schedule computation is pure functions — there is no excuse for not having 100% coverage. Every edge case (gap at midnight, empty block, single-episode show, show with more weight than others, offline block) must have an explicit test.

**Test the player's response to schedule service responses.** Mock the schedule service and verify the player does the right thing for every possible response: valid item, offline item, network error, malformed JSON. The player must handle all of these without crashing or hanging.

**Do not test Kodi internals.** Tests that require a running Kodi instance are integration tests and belong in a separate suite run only against the real device. Unit tests must run without Kodi installed — stub `xbmc`, `xbmcgui`, `xbmcaddon` in `sys.modules` before importing any addon code.

**Property-based tests for schedule math.** Use Hypothesis for the scheduler. Properties like "the result is always within the item's duration," "advancing by exactly one cycle_duration produces an identical result," and "the seek offset is never negative" should hold for all inputs. These catch bugs that example-based tests miss.

**Tests live next to the code they test.** `tests/test_scheduler.py` tests `server/scheduler.py`. `tests/test_player.py` tests `addon/resources/lib/player.py`. The test file structure mirrors the source structure.

**A failing test is a bug report.** When a production issue is found (dead ID, decoder hang, wrong seek offset), write a failing test that reproduces it before fixing it. The fix is not complete until the test passes. This is how v2.0 bugs like the bumper loop, the hold mode thread leak, and the `compute_sync_offset` cycle drift were documented and fixed.

---

### Local Development Environment

The system has three distinct runtime environments. Code must work correctly in all three.

**1. Local Mac (development)**

The developer's Mac runs the server-side code (`server/`) and the web tools (`web/`) natively. Kodi addon code (`addon/`) is tested via the unit test suite with Kodi stubs — no Kodi installation required. The schedule service runs at `localhost:8300`. The developer uses Jellyfin at `localhost:8096` (pointing at a subset of the media library for faster iteration).

Environment variables configure which Jellyfin instance to use. No hardcoded IPs in source code.

**2. jfmac (staging/production server)**

The schedule service runs as a launchd agent. Jellyfin runs at `localhost:8096`. The Le Potato connects to `192.168.1.207:8300` for schedule data and `192.168.1.207:8096` for media.

**3. Sweet Potato / Le Potato (player)**

CoreELEC with the Kodi addon installed. No Python development environment — code is deployed as files, not installed as packages.

**Local test command:**

```bash
python3 -m pytest tests/ -q
```

All tests must pass on the developer's Mac without any running services or devices. Integration tests that require jfmac or the Sweet Potato are in a separate `tests/integration/` directory and are run explicitly.

---

### Git Workflow

**`main` is always deployable.** Every commit to `main` must leave the system in a working state. No "WIP" commits to main. Feature work happens on branches.

**Branch naming:** `feature/short-description` or `fix/short-description`. Keep it lowercase, no ticket numbers needed — this is a personal project, not a corporate repo.

**Commit messages describe why, not what.** The diff shows what changed. The commit message explains why. "Fix EOF loop on bumper slots" is better than "Update sidecar_cache.py."

**One logical change per commit.** Don't bundle a bug fix with a refactor with a new feature. If the commit message needs "and" in it, it's probably two commits.

**Tag every deploy.** After deploying to jfmac or the Sweet Potato, tag the commit: `git tag -a v3.X.Y -m "brief description"`. Semantic versioning: patch for fixes, minor for new features, major for architecture changes. This makes it trivially easy to answer "what's actually running on the device right now."

**VERSION file on each device.** The deploy script writes the current git tag to `~/classic-tv-3.0/VERSION` on jfmac and `/storage/.kodi/addons/script.classictv.service/VERSION` on the Sweet Potato. Check what's deployed without SSHing into log files.

---

### Deployment

**Two deploy scripts, one each:**

`deploy/deploy-sweet-potato.sh` — syncs the addon directory to the Sweet Potato via scp, wipes `__pycache__`, syncs the IR keymap, and restarts Kodi via `kill -9 $(pgrep kodi.bin)`. Writes the VERSION file.

`deploy/deploy-jfmac.sh` — syncs the server directory to jfmac, restarts the schedule service via launchctl, and writes the VERSION file.

Web tools deploy automatically via Cloudflare Pages on push to `main` — no deploy script needed.

**Never deploy uncommitted code.** The deploy scripts should verify the working tree is clean (`git status --porcelain`) and refuse to run if there are uncommitted changes. This prevents "I deployed something I didn't mean to" and keeps VERSION files meaningful.

**Provision scripts for fresh devices.** `setup/provision-sweet-potato.sh` sets up a fresh CoreELEC install from scratch: installs the addon, loads the IR keymap, configures `rc_maps.cfg` for auto-load on boot, sets the Jellyfin server address. Running this script on a freshly flashed SD card should produce a working player with no manual steps.

---

## 18. Open Questions

1. **Channel manager write-back.** The `tv.dane.photos/channels` UI needs to write to channel YAML files on jfmac. Options: commit to git and redeploy (clean but slower), or write directly via an authenticated API endpoint on the schedule service (faster but needs auth). Decision pending.

2. **Schedule regeneration interval.** The schedule service computes on demand, but channel rotations use a shuffled queue per show to avoid repeats. That queue state needs to persist across service restarts. Options: persist to a small SQLite file, or recompute from a deterministic seed (simpler, slightly less ideal repeat distribution). Decision pending.

3. **tv.dane.photos hosting.** Tools that only need to display data (CRT guide, static dashboard) can be pure Cloudflare Pages with no jfmac dependency. Tools that need live data need either a Cloudflare Worker proxy or direct access to jfmac. The right split between static and dynamic is worth deciding before building the web layer.

4. **Kodi webserver.** Enabling Kodi's built-in JSON-RPC webserver (port 8080) allows richer remote control. In v2.0 this was inconsistently enabled. v3.0 should decide: always on (enabled in provisioning) or not at all.

5. **Mid-episode seek strategy.** Time-synced channels require seeking to a specific offset (e.g. 14 minutes into a 22-minute episode) at playback start. This is one of the most latency-sensitive parts of the system and should be researched fresh before implementation. Specifically: does Jellyfin's `StartTimeTicks` parameter work reliably for direct play streams in the current Jellyfin version? Does HLS with a start offset perform better or worse than direct play + post-start seek on the Amlogic S905X? What is the actual measured channel-switch-to-synced-playback time for each approach on CoreELEC 21.x? The v2.0 system used a pause-seek-retry loop which worked but added latency — the spec agent should evaluate current options and recommend the best approach before committing to an implementation.
