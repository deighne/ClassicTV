# Classic TV 4.0 — Architecture

**Date:** September 9, 2026 (status updated 2026-09-20)
**Status:** **Implemented and live.** All three migration phases are complete and the v3
schedule service is decommissioned. For current versions, commands, and gotchas see
[`../AGENTS.md`](../AGENTS.md); this document explains the design and *why*.
**Predecessor:** Classic TV 3.0 (decommissioned; see `../legacy/`)

---

## Problem Statement

Classic TV 3.0 has three structural problems that stem from its architecture, not from bugs:

1. **The schedule service runs on a separate machine (jfmac).** This makes jfmac a required
   infrastructure dependency. If it's asleep, unreachable, or rebooted, the Le Potato has no
   content. It also introduces network latency on every channel change.

2. **Classic TV calls `xbmc.Player.play()` independently of the Jellyfin addon.** Both addons
   share one Kodi player instance but neither knows what the other is doing. This is the root
   cause of the "multiple players" problem: Jellyfin's player monitor, its `jellyfin_play.json`
   window property, and its session reporting all assume they own the active playback. When
   Classic TV initiates playback outside that path, Jellyfin's state machine is left in an
   inconsistent state.

3. **Seeking is driven by `seekTime()` called post-start**, requiring a daemon thread,
   retry logic, re-entrancy guards, and `RuntimeError` handling to work on Amlogic. The
   standard mechanism (`ListItem.setProperty("StartOffset", ...)`) does the same job
   synchronously before the decoder starts and requires none of that scaffolding. See
   `docs/seek-strategy-findings.md` for the full analysis.

All three are solvable by the same architectural shift.

---

## Proposed Architecture

### Overview

```
┌─────────────────────────────────────────────┐
│             Jellyfin Server                 │
│  ─────────────────────────────────────────  │
│  Media library + streaming (unchanged)      │
│                                             │
│  ClassicTV Server Plugin (.NET)             │
│  ├── Channel schedule definitions           │
│  ├── Background cache worker                │
│  │   (pre-computes slots days ahead)        │
│  └── REST endpoint                          │
│      GET /ClassicTV/channel/{id}            │
│      → { url, StartOffset, title,           │
│          duration, next_item }              │
└─────────────────────────────────────────────┘
                     ▲
                     │ HTTP (same LAN,
                     │ same host as media)
                     ▼
┌─────────────────────────────────────────────┐
│        Le Potato / CoreELEC                 │
│  ─────────────────────────────────────────  │
│  Jellyfin Kodi addon (plugin.video.jellyfin)│
│  ├── Auth, session management               │
│  ├── Library browsing (optional, unchanged) │
│  └── Owns xbmc.Player — sole player driver  │
│                                             │
│  Classic TV Kodi addon (plugin.video.classictv) │
│  ├── IR remote handling                     │
│  ├── Channel state machine (up/down)        │
│  ├── Channel bug / channel static overlays  │
│  ├── GET /ClassicTV/channel/{id} on demand  │
│  └── Delegates playback to Jellyfin addon   │
│      via plugin:// URL — never calls        │
│      xbmc.Player.play() directly            │
└─────────────────────────────────────────────┘
```

### Key Principle: One Player Owner

The Jellyfin addon owns `xbmc.Player`. Classic TV never touches it directly. To start
playback, Classic TV passes a Jellyfin item ID and start offset to the Jellyfin addon via its
`plugin://plugin.video.jellyfin/` URL scheme. Kodi routes this through the Jellyfin addon's
existing `actions.play()` path, which handles the ListItem construction, `StartOffset`,
`setResumePoint()`, session reporting, and audio/subtitle track selection — all correctly,
all already tested on this hardware.

This eliminates the multiple-players problem at the source. There is no second code path
creating a player. The Jellyfin addon's state machine is always in control.

---

## Component 1: ClassicTV Server Plugin (Jellyfin Plugin)

A standard Jellyfin server plugin written in C# (.NET 8), distributed as a `.dll` dropped
into the Jellyfin plugins directory. No separate process, no separate machine.

### Responsibilities

**Channel schedule definitions**
Stored as JSON or a simple database table in the plugin's data directory. A channel definition
is: an ordered list of Jellyfin item IDs (episodes), a start anchor (e.g., "channel 4 begins
its rotation at midnight on January 1, 2024"), and a loop policy (repeat indefinitely).

From these inputs, "what is playing on channel 4 right now, and at what offset?" is pure
arithmetic. No external state required.

**Background cache worker**
A hosted service that runs on a configurable interval (e.g., every hour) and pre-computes
the schedule for the next 48 hours across all channels. Results are written to an in-memory
or on-disk cache keyed by `(channel_id, unix_minute)`. On-demand requests are cache lookups,
not real-time calculations.

**REST endpoint**
```
GET /ClassicTV/channel/{channelId}

Response 200:
{
  "channelId": "4",
  "title": "Cheers S03E07 - The Peterson Principle",
  "jellyfinItemId": "abc123",
  "streamUrl": "http://jellyfin.local:8096/Videos/abc123/stream.mp4?api_key=...",
  "startOffset": 1423.7,
  "duration": 1800.0,
  "nextItem": {
    "jellyfinItemId": "def456",
    "title": "Cheers S03E08 - ...",
    "streamUrl": "...",
    "startOffset": 0.0,
    "duration": 1800.0
  }
}
```

The response is a fully-resolved play command. The Kodi addon needs no schedule math
whatsoever. It asks what to play and receives a complete answer including the URL and the
exact `StartOffset` value.

**Note on `streamUrl` vs item ID:** The server plugin can return the raw Jellyfin stream URL
directly (with API key), or return just the item ID and let the Kodi addon resolve it through
the Jellyfin addon's own API client. The latter is cleaner for auth rotation. Both are viable;
the interface contract is the same either way.

### What jfmac's `schedule_service.py` does today that moves here

- Episode rotation logic (which episode in the sequence is playing at wall-clock time T)
- Offset calculation (how far into the episode we are at time T)
- Duration fetching from Jellyfin API
- Two-pass logic to handle episodes whose actual duration differs from metadata

All of this moves into the server plugin's background worker, running on the same host as the
Jellyfin media. The Le Potato is no longer dependent on any second machine.

---

## Component 2: Classic TV Kodi Addon (Redesigned)

### What stays

- IR remote handling (all the keymap work, `ir-keymaps/sony-rmy180`)
- Channel up/down state machine
- Channel bug overlay (`ChannelBug`)
- Channel static overlay (`ChannelStatic`)
- Watchdog loop

### What changes

**Playback delegation**

Current 3.0 flow:
```python
# Classic TV calls player directly
listitem = xbmcgui.ListItem(path=url)
self.play(url, listitem)
```

4.0 flow:
```python
# Classic TV asks the server plugin for a resolved play command, then
# passes it to the Jellyfin addon via plugin:// URL
schedule = requests.get(f"{jellyfin_host}/ClassicTV/channel/{channel_id}").json()
play_url = (
    "plugin://plugin.video.jellyfin/"
    f"?mode=play&id={schedule['jellyfinItemId']}"
    f"&server={server_id}"
)
# Set StartOffset on the ListItem so Jellyfin addon's set_playlist() picks it up
listitem = xbmcgui.ListItem(path=play_url)
listitem.setProperty("StartOffset", str(schedule["startOffset"]))
xbmc.executebuiltin(f"PlayMedia({play_url})")
# — or — let Jellyfin addon's own play action handle it end-to-end
```

The exact integration point (plugin URL params, window property, or JSONRPC call) depends on
what the Jellyfin addon exposes and needs to be confirmed against the installed version before
implementation. The key commitment is: **Classic TV never instantiates an `xbmc.Player`
subclass or calls `.play()` on one.**

**Seek logic removed entirely**

`play_schedule_item()`, `_seek_with_retry()`, `_seek_in_progress`, `_pending_seek`,
`_seek_thread` — all removed. The `StartOffset` property on the ListItem, set before the
Jellyfin addon's play path runs, handles the start position. This is what the Jellyfin addon
already does for resume. We are using the same mechanism for the same problem.

**Schedule fetch simplified**

The addon calls one endpoint and receives a complete play command. No duration math, no
two-pass fetch, no ticks-to-seconds conversion. The server plugin did all of that in the
background before we even asked.

### Dependency declaration (`addon.xml`)

```xml
<requires>
  <import addon="xbmc.python" version="3.0.0"/>
  <import addon="plugin.video.jellyfin" version="2.0.0"/>
</requires>
```

Kodi will refuse to enable Classic TV if the Jellyfin addon is not installed. This is a hard
guarantee, not a runtime check.

---

## Migration Path from 3.0

This is a rewrite of the Kodi addon and a new server-side component. The approach is to build
them in parallel with the existing 3.0 install untouched, validate the new stack, then switch.

**Phase 1: Server plugin — COMPLETE**
- Jellyfin server plugin implemented in C# (`~/projects/Jellyfin.Plugin.ClassicTV`)
- Schedule engine ported from `server/scheduler.py` — 67 tests passing
- REST endpoints `GET /ClassicTV/channels` and `GET /ClassicTV/channel/{id}` implemented
- Plugin deployed to jfmac via `tv.dane.photos/plugins/manifest.json`
- Startup safety resolved: `RefreshDurationsTask` is an `IScheduledTask` (library is ready
  before it runs) and, as of 10.11.0.8, carries a `StartupTrigger` so `DurationCache` is
  repopulated on every Jellyfin start
- See `~/projects/Jellyfin.Plugin.ClassicTV/DEPLOYMENT.md` for deploy details

**Phase 2: Kodi addon playback path — COMPLETE**
- Addon `plugin.video.classictv` declares `plugin.video.jellyfin` as a dependency
- Channel state machine calls `GET /ClassicTV/channel/{id}` and triggers playback via
  `PlayMedia(plugin://plugin.video.jellyfin/?mode=play&id=...&timestamp=...)`
  (PlayMedia was adopted over the originally-proposed RunPlugin)
- Start position comes from the Jellyfin addon's `StartOffset`; no seek machinery
- Prefetch decision gate (below): not implemented — inter-episode gap was acceptable

**Phase 3: Cut over and cleanup — COMPLETE**
- v3 addon and jfmac Schedule_Service decommissioned (2026-09-19)
- `com.classictv.schedule_service` stopped; plist archived to `legacy/`
- `deploy/deploy-jfmac.sh` updated: no longer manages the service, verifies it is NOT running
- v3 Python source remains in `server/` for reference only

### Post-migration fixes (2026-09-19)

- **Schedule rotation (plugin 10.11.0.7).** `ExpandBlockEpisodes` used to expand only
  `sum(weight)` slots. With every production show at `weight: 1`, each channel replayed only
  the first episode of each show forever. It now emits the full episode catalog once per
  cycle, with `weight` controlling spacing rather than slot count. See
  [`../AGENTS.md`](../AGENTS.md) §4.
- **Duration cache on restart (plugin 10.11.0.8).** The `StartupTrigger` described above,
  so a restart no longer schedules from 1800s fallbacks until the daily task.

---

## What This Does Not Change

- CoreELEC on Le Potato — no OS or hardware changes
- Jellyfin server on jfmac — still serves media; just loses the schedule service role
- IR remote configuration — all keymap work carries forward
- Channel bug / channel static overlays — **not yet wired** (dead code; no skin XML). See Tier 3.
- Test suite structure — unit tests for schedule logic move to the server plugin's test project

---

## Separation of Concerns (Full Picture)

Moving the schedule logic to the server plugin enables a clean four-layer separation where
each layer can change independently of the others:

| Layer | Owner | Technology |
|---|---|---|
| Media storage and streaming | Jellyfin server | Unchanged |
| Channel definitions and schedule | ClassicTV server plugin | C# / Jellyfin plugin SDK |
| Playback execution | Jellyfin Kodi addon | Unchanged |
| IR input and channel switching | ClassicTV Kodi addon | Python (thin) |
| Visual presentation | Kodi skin | XML / Kodi skin engine |

**Channel management moves into the Jellyfin dashboard.** Channel definitions, show
assignments, rotation order, and start anchors are all managed via the plugin's config page
in the Jellyfin admin UI, editable from any browser on the network. No SSH, no config file
edits, no redeploy to change what's on channel 4.

**Look and feel becomes pure Kodi skinning.** In Classic TV 3.0, the channel bug and channel
static overlays are Python code in the addon because Classic TV owns playback and has to
inject visuals directly. Once the Jellyfin addon owns the player, overlays can become a custom
Kodi skin or skin overlay that reacts to player state via standard Kodi infolabels. A skin
designer can work on the visual layer without touching any Python. A schedule change doesn't
touch the device. A Jellyfin addon update doesn't break Classic TV.

---

## Server Plugin: CI and Build Requirements

The schedule logic has caused repeated correctness bugs in Classic TV 3.0 (offset math errors,
duration mismatches, two-pass edge cases). The server plugin is where all of that arithmetic
lives in 4.0, and it must be covered by tests from day one. C# and the Jellyfin plugin SDK
provide excellent tooling for this.

### Project structure

```
Jellyfin.Plugin.ClassicTV/
├── Jellyfin.Plugin.ClassicTV/          # Plugin library
│   ├── Plugin.cs                        # IBasePlugin implementation
│   ├── PluginConfiguration.cs           # BasePluginConfiguration
│   ├── Configuration/
│   │   └── configPage.html              # Jellyfin dashboard config UI
│   ├── Api/
│   │   └── ClassicTvController.cs       # ControllerBase REST endpoints
│   ├── Schedule/
│   │   ├── ScheduleEngine.cs            # Pure arithmetic — channel/offset math
│   │   ├── ScheduleCache.cs             # In-memory slot cache
│   │   └── ScheduleCacheWorker.cs       # IHostedService background pre-compute
│   └── ServiceRegistrator.cs            # IPluginServiceRegistrator
└── Jellyfin.Plugin.ClassicTV.Tests/    # xUnit test project
    ├── Schedule/
    │   ├── ScheduleEngineTests.cs        # Offset math, rotation, edge cases
    │   └── ScheduleCacheTests.cs         # Cache hit/miss, expiry
    └── Api/
        └── ClassicTvControllerTests.cs   # Endpoint response shape
```

**The key structural rule:** `ScheduleEngine.cs` is pure functions with no Jellyfin
dependencies. It takes a channel definition and a wall-clock time, returns a schedule slot.
This makes it trivially unit-testable without mocking the Jellyfin host.

### SDK and toolchain

```
# From the official jellyfin/jellyfin-plugin-template (updated May 2026)
dotnet SDK: 9.0
Target framework: net9.0
NuGet packages:
  Jellyfin.Controller  (ExcludeAssets: runtime)
  Jellyfin.Model       (ExcludeAssets: runtime)

# CRITICAL: package version must match the installed Jellyfin server version
# or the plugin shows as "NotSupported" in the dashboard.
# Confirm with: cat /path/to/jellyfin/jellyfin_* | grep -i version
```

`ExcludeAssets: runtime` on both NuGet references is mandatory. Without it, the plugin
packages its own copies of the Jellyfin runtime assemblies, which causes the plugin to fail
registration because Jellyfin finds duplicate type definitions.

### Key Jellyfin plugin interfaces used

| Interface | Purpose |
|---|---|
| `BasePlugin<TConfig>` | Plugin entry point, name, GUID, version |
| `BasePluginConfiguration` | Dashboard-editable settings (channels JSON) |
| `IPluginServiceRegistrator` | Register services in the DI container at startup |
| `IHostedService` | Background cache pre-compute worker (runs continuously) |
| `ControllerBase` (ASP.NET) | REST endpoint (`GET /ClassicTV/channel/{id}`) |
| `ILibraryManager` | Query Jellyfin for item durations during cache build |

Note: `IBasePlugin` and `IHostedService` cannot be implemented by the same class. The
background worker must be a separate class registered via `IPluginServiceRegistrator`.

### Test requirements

**`Jellyfin.Plugin.ClassicTV.Tests` must pass on every commit before merge.**

Minimum coverage for the schedule engine:

```csharp
// Examples of required test cases
[Fact] void OffsetAtEpisodeStart_ReturnsZero()
[Fact] void OffsetMidEpisode_ReturnsCorrectSeconds()
[Fact] void OffsetAtEpisodeBoundary_WrapsToNextEpisode()
[Fact] void RotationWrapsAfterLastEpisode_ReturnsFirstEpisode()
[Fact] void EpisodeDurationMismatch_UsesActualNotMetadata()
[Theory] void MultipleChannels_AreIndependent(int channelId)
[Fact] void CacheHit_DoesNotCallLibraryManager()
[Fact] void CacheMiss_PopulatesFromLibraryManager()
[Fact] void ApiResponse_ContainsStartOffsetInSeconds()
[Fact] void ApiResponse_ContainsNextItem()
```

The duration-mismatch test is specifically required because this edge case caused the 3.0
two-pass fetch complexity. The 4.0 cache worker resolves actual durations from
`ILibraryManager` at cache-build time, so on-demand requests never encounter it.

### CI pipeline (GitHub Actions)

```yaml
# .github/workflows/ci.yml (server plugin repo)
name: CI
on: [push, pull_request]
jobs:
  build-and-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-dotnet@v4
        with:
          dotnet-version: '9.0.x'
      - run: dotnet restore
      - run: dotnet build --no-restore --configuration Release
      - run: dotnet test --no-build --configuration Release --logger "trx"
      - run: dotnet format --verify-no-changes  # style enforcement

  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-dotnet@v4
        with:
          dotnet-version: '9.0.x'
      - run: dotnet tool install -g dotnet-format
      - run: dotnet format --verify-no-changes
```

### Local dev workflow

```bash
# Build
dotnet build Jellyfin.Plugin.ClassicTV.sln

# Test (run once, no watch mode)
dotnet test --configuration Release

# Deploy to local Jellyfin for manual testing
dotnet build --configuration Release
cp Jellyfin.Plugin.ClassicTV/bin/Release/net9.0/Jellyfin.Plugin.ClassicTV.dll \
   ~/.local/share/jellyfin/plugins/ClassicTV/

# Verify format before commit
dotnet format --verify-no-changes
```

No `dotnet watch` or interactive test runners in automated contexts. Use `dotnet test` for
single-pass execution, consistent with the CI pipeline.

---

## Pre-Implementation Decisions (All Resolved)

---

### Q1: Does the Jellyfin addon honor `StartOffset` when invoked via plugin:// URL?

**RESOLVED — Yes, and the mechanism is cleaner than expected.**

Source-read of `/storage/.kodi/addons/plugin.video.jellyfin/jellyfin_kodi/entrypoint/default.py`
on the Sweet Potato reveals that the `mode=play` plugin:// URL accepts a `timestamp` query
parameter (in seconds, float). The entrypoint code at line 104 of `default.py`:

1. Fetches the item from the Jellyfin API via `api_client.get_item(params["id"])`
2. If `timestamp` is present, writes it into `item["UserData"]["PlaybackPositionTicks"]`
   and sets `item["resumePlayback"] = True`
3. Passes this into `Actions.play()` → `set_playlist()` → `set_listitem()` → `listitem_video()`
4. In `listitem_video()`, `obj["Resume"]` is derived from `PlaybackPositionTicks` and
   `item.get("resumePlayback")` is `True`, so `StartOffset` is set to the resume time

This means **Classic TV does not need to construct or pass a ListItem at all.** The entire
play-with-offset call is a single URL:

```python
play_url = (
    f"plugin://plugin.video.jellyfin/"
    f"?mode=play"
    f"&id={jellyfin_item_id}"
    f"&server={server_id}"
    f"&timestamp={start_offset_seconds:.3f}"
)
xbmc.executebuiltin(f"RunPlugin({play_url})")
```

The Jellyfin addon fetches the item, sets `StartOffset` on the ListItem from the timestamp,
and calls `xbmcplugin.setResolvedUrl()` — all within its own plugin execution context. Classic
TV never touches the player.

**One nuance:** if `timestamp=0.0` and the item `jellyfin_playing_id` is already playing that
same item, the code issues a `seekTime(0.0)` instead of a full restart. Classic TV needs to
pass a non-zero timestamp (or omit it entirely and let the Jellyfin addon start from the
server-stored resume point) for channel-change plays. For items meant to start from the
beginning, pass `timestamp=0.001` or clear `jellyfin_playing_id` first.

**Decision:** Use `plugin://plugin.video.jellyfin/?mode=play&id=...&timestamp=...` as the
sole playback mechanism. The fallback (window property injection) is not needed.

---

### Q2: Auth token handling — embed API key in URL vs return item ID only

**RESOLVED — item ID only. Q1 eliminates the question.**

Q1 established that the playback call is:

```
RunPlugin(plugin://plugin.video.jellyfin/?mode=play&id=<itemId>&server=<serverId>&timestamp=<offset>)
```

The Jellyfin Kodi addon handles everything from there: it calls `api_client.get_item(id)` with
its own authenticated session, builds the stream URL internally, and sets `StartOffset` itself.
Classic TV never sees a stream URL and never needs an API token.

The server plugin response therefore only needs to contain:

```json
{
  "jellyfinItemId": "abc123",
  "startOffset": 1423.7,
  "duration": 1800.0,
  "title": "Cheers S03E07",
  "nextItem": { "jellyfinItemId": "def456", "startOffset": 0.001, "duration": 1800.0 }
}
```

No `streamUrl`. No `apiKey`. The server plugin needs auth to query `ILibraryManager` and the
Jellyfin API internally (to fetch durations at cache-build time), but that is all handled
inside the plugin's own .NET code via the injected Jellyfin service interfaces — it never
needs to surface credentials to the client.

**The `serverId` in the plugin:// URL** is the Jellyfin server UUID. The Jellyfin Kodi addon
already has this stored from its initial server setup. Classic TV can read it from the Jellyfin
addon's settings at startup rather than hard-coding it. The server plugin does not need to
return it.

---

### Q3: Channel schedule storage format

**RESOLVED — JSON blob in plugin data directory, editable via dashboard config page.**

Channel definitions are stored as a JSON file in the plugin's data directory
(`IServerApplicationPaths.DataPath`), surfaced as a raw text field in the Jellyfin dashboard
config page via `IPluginConfigurationPage`. One JSON blob, editable in-browser, no redeploy
needed, no SQLite schema to manage.

```json
{
  "channels": [
    {
      "id": "4",
      "name": "Classic Sitcoms",
      "startAnchor": "2024-01-01T00:00:00Z",
      "items": [
        { "jellyfinItemId": "abc123" },
        { "jellyfinItemId": "def456" }
      ]
    }
  ]
}
```

The dashboard config page (`IPluginConfigurationPage`) is part of the standard plugin
template and costs almost nothing to wire up. A proper channel management UI
(add/remove/reorder episodes via form fields) is a v2 feature once the core works.

---

### Prefetch decision point (Phase 2 gate)

Classic TV 3.0's `MediaCache` prefetches the next episode to local storage to eliminate the
gap between episodes. Whether 4.0 needs this is determined during Phase 2, once the new
playback delegation path is running on the device for the first time.

**How to measure:** Let an episode play to its natural end via the new plugin:// delegation
path. Time the gap between the watchdog detecting end-of-playback, fetching the next schedule
slot from the server plugin, and `RunPlugin(...)` producing a visible first frame.

**Decision gate:**
- Gap is imperceptible (~1s or under): drop prefetch. Simpler system, one less moving part.
- Gap is noticeable: implement prefetch in the server plugin's background worker. The cache
  already pre-computes `nextItem`; the worker can also pre-warm the Jellyfin media buffer.
  Decision logic stays server-side so it can be tuned without a device redeploy.

Start Phase 2 without prefetch. Add it only if the gap measurement fails.

---

### Q5: Jellyfin server plugin SDK version

**RESOLVED — Jellyfin 10.11.6 on jfmac.**

Pin NuGet packages to `10.11.6` in the `.csproj`:

```xml
<PackageReference Include="Jellyfin.Controller" Version="10.11.6">
    <ExcludeAssets>runtime</ExcludeAssets>
</PackageReference>
<PackageReference Include="Jellyfin.Model" Version="10.11.6">
    <ExcludeAssets>runtime</ExcludeAssets>
</PackageReference>
```

Target framework remains `net9.0` per the plugin template. The Jellyfin server bundles its
own .NET runtime so no separate runtime install is needed on jfmac.

---

## Summary

Classic TV 4.0 fixes all three structural problems in one coherent change:
- The schedule service moves to a Jellyfin server plugin, eliminating the jfmac dependency
- The Kodi addon delegates playback to the Jellyfin addon, eliminating the multiple-players problem
- Seeking uses `StartOffset`, eliminating the retry/thread machinery

The Kodi addon becomes a thin channel remote control: ask the server what to play, hand it to
Jellyfin, handle IR input. The Jellyfin addon becomes what it was always meant to be for this
project: the single, authoritative player driver. Channel management lives in the Jellyfin
dashboard. Look and feel is a Kodi skin problem. Each layer is independently changeable.
