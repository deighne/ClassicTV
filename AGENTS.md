# AGENTS.md — Classic TV

**Read this first.** It is the fastest way to understand what this project is, what is
actually running right now, how the pieces fit together, and how to build/test/deploy.
Keep it current when you change the system.

**Last updated:** 2026-11-15 (v4.11.1 deployed on Sweet Potato, Jellyfin 12.3.0 + plugin 12.0.0.13 installed on jfmac, `tv_auth` route guarding + `/manage/` + web smoke tests + Roku 4.0)

---

## 1. What this is

Classic TV is a personal broadcast-television simulator. Themed channels play 24/7,
synchronized to wall-clock time so tuning in drops you mid-episode like real cable. It
runs unattended on a Sony CRT driven by a Le Potato SBC.

The current live system is **Classic TV 4.0**. Classic TV 3.0 is decommissioned (its
Python schedule service is gone; see `legacy/`). The docs and directory names still say
"3.0" in places for historical reasons — trust this file and the 4.0 docs.

---

## 2. Two machines, two repositories

| | |
|---|---|
| **jfmac** | Mac Mini media server. `dane@192.168.1.207`. Runs Jellyfin (`:8096`), which now also hosts the Classic TV **schedule plugin**. Serves `tv.dane.photos` via Caddy from `~/projects/ClassicTV3.0/web`. Jellyfin data: `~/Library/Application Support/jellyfin`. Also runs three `launchd` daemons: `com.classictv.guide_renderer` (Channel 1 pre-rendered guide), `com.classictv.reencoder` (hardware remediation queue), and `com.classictv.status_monitor` (continuous status publishing). |
| **Sweet Potato** | Le Potato SBC, CoreELEC 21.3-Omega. `root@192.168.1.237`. Runs Kodi + the Classic TV addon. Connected to the Sony KV-34HS510 CRT. |
| **Remote access** | `ssh.dane.photos` (Cloudflare tunnel to jfmac); on the LAN use the IPs above. `192.168.1.237` may change after a reflash — check before assuming. |

Two git repos, no remotes configured (local only):

| Repo | Path (dev Mac) | Contents |
|---|---|---|
| **ClassicTV3.0** | `~/projects/ClassicTV3.0` | Kodi addon, web tools, deploy scripts, docs, channel config, this file |
| **Jellyfin.Plugin.ClassicTV** | `~/projects/Jellyfin.Plugin.ClassicTV` | C# Jellyfin server plugin (the schedule engine) |

---

## 3. Live state (as of 2026-11-12)

| Component | Version / location |
|---|---|
| Jellyfin server | jfmac `:8096`, Jellyfin **12.3.0** (upgraded from 10.11.6; backup at `~/jellyfin-backup-10.11.6-20261025-225551`) |
| ClassicTV Jellyfin plugin | **12.0.0.13** (`net10.0`, `targetAbi: 12.0.0.0`), installed at `~/Library/Application Support/jellyfin/plugins/ClassicTV_12.0.0.13/` |
| Classic TV Kodi addon | `plugin.video.classictv` **4.11.1**, at `/storage/.kodi/addons/plugin.video.classictv/` on the Sweet Potato |
| IR keymap | `/storage/.kodi/userdata/keymaps/classictv.xml` (deployed from `addon4/keymaps/keyboard.xml`) |
| Addon tags | `v4.0.0`–`v4.0.2`, `v4.1.0`–`v4.1.5`, `v4.2.0`–`v4.2.2`, `v4.3.0`, `v4.4.0`–`v4.4.2`, `v4.5.0`, `v4.5.1`, `v4.5.2`, `v4.5.3`, `v4.6.0`, `v4.6.1`, `v4.7.0`, `v4.7.1`, `v4.7.2`, `v4.8.0`, `v4.8.1`, `v4.9.0`, `v4.9.1`, `v4.9.2`, `v4.9.3`, `v4.10.0`, `v4.11.0`, `v4.11.1` (ClassicTV3.0); `v10.11.0.6`–`v10.11.0.16`, `v12.0.0.1`–`v12.0.0.13` (plugin) |
| Background daemons (jfmac) | `com.classictv.guide_renderer`, `com.classictv.reencoder`, `com.classictv.status_monitor`, `com.classictv.tv_auth` |
| v3 schedule service | **decommissioned** 2026-09-19 (`:8300` no longer runs) |

Both repos are on `main`. There is no CI and no remote — builds and deploys are manual.

---

## 4. Architecture (4.0)

```
        jfmac 192.168.1.207
  ┌─────────────────────────────────────────────┐
  │ Jellyfin :8096                              │
  │  ├─ media library + streaming               │
  │  ├─ ClassicTV plugin (schedule engine)      │
  │  │    GET /ClassicTV/channels               │
  │  │    GET /ClassicTV/channel/{id}           │
  │  │    ScheduleCacheWorker (48h precompute)  │
  │  │    RefreshDurationsTask (run times)      │
  │  └─ Caddy → tv.dane.photos → web/           │
  └───────────────────────┬─────────────────────┘
                          │ HTTP (schedule + media)
  ┌───────────────────────▼─────────────────────┐
  │ Sweet Potato 192.168.1.237 (CoreELEC/Kodi)  │
  │  plugin.video.classictv (thin remote)       │
  │    IR remote (Sony RM-Y180) → NotifyAll     │
  │    channel up/down, digit entry, EPG        │
  │    fetch slot → delegate playback           │
  │  plugin.video.jellyfin (owns the player)    │
  └─────────────────────────────────────────────┘
```

**The single most important principle:** Classic TV never calls `seekTime()`. As of 4.9.0
(`DIRECT_HTTP_PLAYBACK = True`), `play_slot()` plays the Jellyfin HTTP byte-range stream
(`/Videos/{id}/stream?static=true`) directly via `xbmc.Player().play(url, listitem)` with
`StartOffset` set on the `ListItem`, and clears `Window(Home).Property(jellyfin_playing_id)`
so `plugin.video.jellyfin` ignores 24/7 background TV playback completely. This eliminates
the 3.5–7.5 s cold `CPythonInvoker` startup cost of spawning `plugin.video.jellyfin` on
every tune, prevents overnight Borg/service wedges in Browse Mode, and keeps random
scheduled episodes out of the user's "Continue Watching" row.

**Watchdog:** a daemon thread polls `player.isPlaying()` every 10s and re-fetches the slot
on stop. It is the backstop EOF recovery, and it can be suspended while the viewer browses.
`ClassicTVPlayer` subclasses `xbmc.Player`, and its callbacks now carry logic (verified on
device — see `tier-1-callback-findings.md`): `onPlayBackStarted` confirms playback and resets
the wedge counter; `onPlayBackStopped` enters browse suspension unless the stop was internal
to a channel change or followed a natural EOF; `onPlayBackEnded` records the EOF. `modes.py`
owns the three-mode state machine (TV / BROWSE / RESUME_PENDING, §11).

### Schedule engine semantics (read before touching the plugin)

`ScheduleEngine` is pure arithmetic. Given a channel, a unix time, and a duration map it
returns the current slot.

- **Full-catalog rotation.** `ExpandBlockEpisodes` emits **every episode of every show
  exactly once per cycle**, interleaved weighted-round-robin. The cycle duration is the sum
  of all episode durations and is **stable**, so `elapsed % cycleDuration` is well defined.
- **`weight` = spacing only.** It controls how often/early a show is picked, *not* how many
  episodes it contributes. Do not set weight equal to episode count. Every show in the
  production config currently has `weight: 1` (clean round-robin).
- **`startAnchor`** is a fixed origin (`2024-01-01T00:00:00Z` in production). Elapsed time
  is measured from it, so the schedule never resets daily.
- **Durations** come from `DurationCache`, filled by `RefreshDurationsTask` from
  `ILibraryManager`. Missing IDs fall back to 1800s.
- **Changing the episode set or a duration changes the cycle duration and therefore the
  current position.** The schedule will visibly "jump" after a config change, a duration
  refresh that resolves previously-fallback items, or an engine change. That is expected.

> **Historical bug (fixed 10.11.0.7):** `ExpandBlockEpisodes` used to emit only
> `sum(weight)` slots, so with `weight=1` every channel replayed only the first episode of
> each show forever. See §11 for the regression test.

---

## 5. Repository layout (ClassicTV3.0)

```
addon4/                     Kodi addon source (deployed to Sweet Potato)
  service.py                entry point, NotifyAll monitor, startup
  addon.xml                 addon id plugin.video.classictv, requires jellyfin addon
  keymaps/keyboard.xml      IR keymap → deployed to userdata/keymaps/classictv.xml
  resources/lib/
    player.py               playback delegation, watchdog, callbacks, browse suspension, boot resilience
    modes.py                ModeState — TV/BROWSE/RESUME_PENDING, locked transitions, watchdog, injectable timing
    schedule.py             HTTP client for /ClassicTV/* (incl. server health check)
    mirror.py               mirror channels: follow another Jellyfin user's playback (decide, MirrorSync)
    channels.py             channel roster (server number, positional fallback)
    constants.py            named constants
    channel_entry.py        digit entry state machine; shows "12_" via the channel-bug dialog
    browse_controls.py      browse-mode OK/d-pad -> pause, seek, OSD, subtitles; subtitle diagnostics
    channel_bug.py          channel ident overlay (number + name, 10s) — wired, skin-backed
    jellyfin_server.py      reads Jellyfin server id from addon data.json
  resources/skins/default/1080i/ChannelBug.xml   channel-bug layout (controls 5000/5001)
  resources/skins/default/media/white.png        solid texture used by the overlay
  resources/wait-for-jellyfin.sh                 boot gate script (polls /System/Info/Public)
  resources/classictv-wait-for-jellyfin.service  boot gate systemd unit (Before=kodi.service)
channels/                   channel config (v3 YAML + channels-v4.json for the plugin)
deploy/                     deploy-sweet-potato-4.sh, deploy-jfmac.sh, deploy-roku.sh, Caddyfile (reference)
docs/                       all documentation — see §10
ios/                        iOS ClassicTV app (Xcode project)
roku/                       Roku BrightScript channel (Classic TV 4.0)
server/                     DEPRECATED v3 Python schedule service (reference only)
addon/, tests/              DEPRECATED v3 addon + tests (reference only)
tests4/                     v4 addon & tool unit tests (Kodi stubbed)
tools/                      classictv-logs.py, classictv-status.py, tv_auth.py, smoke_web.mjs, send_key.py, populate_channel.py
web/                        tv.dane.photos static site (/, /watch/, /guide/, /channels/, /manage/, /status/, /crt/) + web/plugins/
legacy/                     decommissioned 3.0 artifacts
```

---

## 6. Commands

**Run the v4 addon & tool tests** (from the repo root; `.venv` recommended):
```bash
python3 -m pytest tests4/ -q          # loopback tests require local network/bypass sandbox
```

**Run the headless Chrome web smoke suite** (`tv.dane.photos` pages + auth flows):
```bash
node tools/smoke_web.mjs
```

**Package / sideload the Roku channel:**
```bash
bash deploy/deploy-roku.sh                        # builds dist/classictv-roku.zip
bash deploy/deploy-roku.sh <roku-ip> <password>   # builds + sideloads (rokudev)
```

**Deploy the addon to the Sweet Potato** (writes files, keymap, IR keymap):
```bash
bash deploy/deploy-sweet-potato-4.sh 192.168.1.237
```

**Deploy web content + plugin manifest to jfmac** (no plugin DLL; that is separate):
```bash
bash deploy/deploy-jfmac.sh
```

**Read logs from both machines as one timeline:**
```bash
python3 tools/classictv-logs.py --since 2h
python3 tools/classictv-logs.py --errors --since 24h
python3 tools/classictv-logs.py --follow
python3 tools/classictv-logs.py --all --since 15m   # include non-ClassicTV lines
```

**Send a simulated remote key to Kodi** (JSON-RPC :9090):
```bash
python3 tools/send_key.py --host 192.168.1.237 ch_up
```

**Plugin repo** (`~/projects/Jellyfin.Plugin.ClassicTV`):
```bash
dotnet test --configuration Release        # 233 tests
```

---

## 7. Building and deploying a plugin release

The plugin version is set by the `--version` flag to `jprm` (and mirrored in `build.yaml`).
The process, in order:

```bash
cd ~/projects/Jellyfin.Plugin.ClassicTV
mkdir -p artifacts
jprm plugin build "Jellyfin.Plugin.ClassicTV/" --output=./artifacts \
    --dotnet-configuration Release --dotnet-framework net10.0 --version 12.0.0.1

python3 scripts/patch_meta.py artifacts/classictv_12.0.0.1.zip   # adds "assemblies"

md5 -q artifacts/classictv_12.0.0.1.zip    # use for the manifest checksum

# Stage + publish
cp artifacts/classictv_12.0.0.1.zip ~/projects/ClassicTV3.0/web/plugins/
# add a versions[] entry in ~/projects/ClassicTV3.0/web/plugins/manifest.json
# (sourceUrl https://tv.dane.photos/plugins/classictv_12.0.0.1.zip, checksum, targetAbi 12.0.0.0)
cd ~/projects/ClassicTV3.0 && bash deploy/deploy-jfmac.sh

# Install directly on jfmac (catalog install also works, but this is what we do):
ssh dane@192.168.1.207 'PLUGINS="$HOME/Library/Application Support/jellyfin/plugins";
  rm -rf "$PLUGINS/ClassicTV_12.0.0.1"; mkdir -p "$PLUGINS/ClassicTV_12.0.0.1";
  unzip -q -o "$HOME/projects/ClassicTV3.0/web/plugins/classictv_12.0.0.1.zip" -d "$PLUGINS/ClassicTV_12.0.0.1";
  mv "$PLUGINS/ClassicTV_10.11.0.16" /tmp/ 2>/dev/null; true'

# Restart Jellyfin (see §8 for why osascript does not work over ssh)
ssh dane@192.168.1.207 'pkill -TERM -f "Jellyfin.app/Contents/MacOS"; \
  for i in $(seq 1 30); do lsof -nP -i :8096 | grep -q LISTEN || break; sleep 1; done; \
  open -a Jellyfin'
```

Verify afterwards with `curl -s http://192.168.1.207:8096/ClassicTV/channels` and the
plugin load line in the Jellyfin log (use `tools/classictv-logs.py`).

> Only ever have **one** `ClassicTV_*` directory in the plugins folder. Two versions of the
> same GUID cause load conflicts.

---

## 8. Gotchas (things that will waste your time)

- **Restarting Jellyfin over SSH:** `osascript -e 'quit app "Jellyfin"'` does not work from a
  non-GUI session. Use `pkill -TERM -f "Jellyfin.app/Contents/MacOS"`, wait for `:8096` to
  free, then `open -a Jellyfin`. Verify only one process owns `:8096`.
- **`DurationCache` is in-memory.** It is repopulated automatically on every start by
  `RefreshDurationsTask`'s `StartupTrigger` (added in 10.11.0.8). Without that, schedules
  run on 1800s fallbacks until the daily 3am run and then shift.
- **Player callbacks carry logic.** `onPlayBackStarted` confirms a play (arms
  `_play_confirm_event`, resets `_consecutive_failures`); `onPlayBackStopped` sets browse
  suspension unless the stop is internal (`_internal_stop_time`) or follows an EOF
  (`_eof_time`); `onPlayBackEnded` records the EOF. Paused/Resumed are log-only.
  `onPlayBackResumed` + `onPlayBackStarted` fire together when switching streams (verified on
  device: `docs/tier-specs/tier-1-callback-findings.md`).
- **`ChannelBug` is live (never open `WindowXMLDialog` at the exact millisecond `DVDVideoCodecAmlogic` opens).**
  The channel ident (number + name for 10s after a tune) ships in
  `resources/skins/default/1080i/ChannelBug.xml` and `resources/skins/default/media/white.png` and is
  wired in `player.py` via `channel_bug.create_channel_bug()`. In 4.9.1, `_wait_before_overlay()` waits up
  to 500 ms for `onPlayBackStarted` (`~0.29 s` with direct HTTP playback) before calling
  `ChannelBug.show_for()`, and `ChannelBug.xml` uses only the lightweight 1×1 `white.png` texture.
  **Hardware warning:** Loading full-screen textures (`static_0..3.png`) on `osd0` (`LOAD_ON_GUI_INIT`)
  at the exact millisecond `VideoPlayer::OpenFile` opens `DVDVideoCodecAmlogic` on `vd1` locks up the
  Amlogic S905X kernel video driver (`amstream`/`di_cma`) and resets the board.
  When touching the bug: labels bind to `Window(Home)` properties
  (`ClassicTV.ChannelNumber` / `ClassicTV.ChannelName`) because `getControl().setLabel()`
  fails before the window is initialised; the texture must be bundled in `media/`; and
  construction must pass `("default", "1080i")` or Kodi reports "XML File for Window is missing".
- **Channel numbers are server-authoritative** (`number` in `GET /ClassicTV/channels`, 0 =
  unassigned). The addon uses the server number when > 0 and falls back to positional `i + 1`.
  Duplicate non-zero numbers among enabled channels are rejected by the plugin.
- **Disabled channels** (`enabled: false`) are filtered from `/ClassicTV/channels`, return 404
  from `/channel/{id}`, and are skipped by the cache worker. The addon tunes to the first
  available channel if its current channel disappears.
- **Keymap:** `addon4/keymaps/keyboard.xml` is deployed to
  `/storage/.kodi/userdata/keymaps/classictv.xml`. `<global>` maps EPG → `guide`; the same
  mapping is **repeated in `<FullscreenVideo>`** because the system `remote.xml` maps
  `<guide>` to `ActivateWindow(TVGuide)` there, which beats a user `<global>` mapping and
  pops "no PVR add-on enabled" (window-specific system mappings win). ch+/-, digits, OK,
  back and the d-pad live in `<FullscreenVideo>`. There is a stale `classictv_test.xml` risk
  on device.
- **Never pass `server=` in the Jellyfin play URL** — it causes a `KeyError` in fresh plugin
  contexts. `play_slot()` deliberately omits it (see the comment there).
- **`PlayMedia`, not `RunPlugin`.** RunPlugin was tried and dropped; the current path is
  `xbmc.executebuiltin("PlayMedia(plugin://...)")`.
- **Timezone:** the Sweet Potato clock is `America/Chicago` (matching jfmac); Jellyfin logs carry a local offset.
  `tools/classictv-logs.py` normalizes both to UTC.
- **No remote / no CI.** Builds, tests, and deploys are local and manual. Tags are the only
  record of what shipped.
- **`classictv-wait-for-jellyfin.service`** — A systemd `oneshot` unit ordered `Before=kodi.service` and `After=network-online.target` that polls `/System/Info/Public` every 2 s for up to 60 s before allowing Kodi to start. Installed to `/storage/.config/system.d/classictv-wait-for-jellyfin.service` (persists across CoreELEC OS updates). Always exits 0, so a permanently unreachable Jellyfin server or a polling failure never blocks the device.
- **`WAIT_NETWORK_TIME=30`** — Written to `/storage/.cache/coreelec/network_wait` by the deploy script. Raises the CoreELEC network wait from 10 s to 30 s so brief WiFi or DHCP delays don't cause a boot-time race.
- **Jellyfin addon self-heal via JSON-RPC.** `_reload_jellyfin_addon()` in `player.py`
  disables then re-enables `plugin.video.jellyfin` via `Addons.SetAddonEnabled` Kodi
  JSON-RPC, forcing a fresh `connect.Connect()` and clearing the placeholder server address
  (`http://{jellyfin-server}`). Rate-limited to one reload per 5 minutes
  (`JELLYFIN_RELOAD_COOLDOWN_SECONDS = 300`). Falls back to `RestartApp` if the RPC fails or
  if `onPlayBackStarted` does not fire within 25 s (`PLAYBACK_CONFIRM_TIMEOUT_SECONDS`).
- **`_consecutive_failures` threshold (3).** `watchdog_recover()` increments
  `_consecutive_failures` each time playback is not confirmed within
  `PLAYBACK_CONFIRM_TIMEOUT_SECONDS` and the server is reachable. At
  `WEDGE_FAILURE_THRESHOLD = 3` it resets the counter and calls `_reload_jellyfin_addon()`.
  Reset to 0 by `onPlayBackStarted()`.
- **Browse suspension and the EPG toggle.** `onPlayBackStopped` sets `_browse_suspend`, which
  makes the watchdog ignore stopped playback while the viewer browses Jellyfin/Kodi.
  `play_slot()` clears it; the global GUIDE button (`guide`) toggles back to TV.
  `enter_browse()` stops playback and waits for the Amlogic decoder to release before
  `ActivateWindow(Home)` — never jump into a video window while the decoder is tearing down
  (it can crash/hang the device).
- **Deleted library items are pruned (plugin 10.11.0.15).**
  `ScheduleEngine.PruneMissingEpisodes` drops episode IDs absent from the duration cache, so a
  removed/cleaned-up episode is never scheduled (previously it blacked out the channel and
  looped the watchdog). A channel whose blocks reference only deleted shows goes offline until
  shows are re-added via the picker.
- **Deploy scripts refuse a dirty working tree.** `deploy/deploy-sweet-potato-4.sh` exits if
  `git status --porcelain` is non-empty — commit before deploying (an earlier deploy silently
  no-oped for this reason).

---

## 9. Working agreements

- **Tests before deploy.** `tests4/` for the addon, `dotnet test` for the plugin. A failing
  test is a bug report — add a regression test before/with the fix.
- **Commit messages describe why.** One logical change per commit.
- **Tag every release.** `v4.X.Y` in ClassicTV3.0, `v10.11.0.N` in the plugin repo.
- **Don't hardcode an IP or a secret in source.** Hosts live in deploy scripts/settings;
  secrets in `.env` (jfmac has one; not committed).
- **Do not bring back the v3 `server/` path.** It is reference only.

---

## 10. Documentation index

| Document | What it is |
|---|---|
| `AGENTS.md` (this file) | Start here. Current state, layout, commands, gotchas. |
| `README.md` | Human-oriented project overview. |
| `docs/classic-tv-4-architecture.md` | 4.0 architecture and migration plan (phases now complete). |
| `docs/tier-specs/tier-1-unblocking-facts.md` | Callback verification etc. — **done** |
| `docs/tier-specs/tier-1-callback-findings.md` | Empirical callback results — **done** |
| `docs/tier-specs/tier-2-mode-state-machine.md` | TV/Browse/Resume modes — **not implemented** |
| `docs/tier-specs/tier-3-robustness.md` | Overlay wiring, other robustness — partial |
| `docs/tier-specs/tier-4-hygiene.md` | Command queue (reverted), v3 decommission (done) |
| `docs/tier-specs/tier-5-deferred.md` | Skin overlays and other longer-horizon items |
| `docs/tier-specs/channel-bug-skin-mvp.md` | MVP spec: channel number+name ident for ~10s after a tune |
| `docs/tier-specs/sweet-potato-boot-network-race.md` | Spec: Jellyfin addon fails to connect when Kodi boots before the network |
| `docs/tier-specs/channel-management-ui-spec.md` | In-dashboard channel editor spec (summary) |
| `.kiro/specs/channel-management-ui/` | Detailed channel editor spec: requirements/design/tasks (read the review corrections in `design.md`) |
| `docs/classic-tv-mode-switching-spec.md` | Original mode-switching proposal (superseded by tier 2) |
| `docs/classic-tv-mode-switching-review.md` | Review of the above |
| `docs/mirror-channel.md` | Mirror channel design (ch. 67): follow another Jellyfin user's playback |
| `docs/browse-mode-controls.md` | Pause/seek/subtitle keys in browse mode, why subtitles were inconsistent, capturing transport buttons |
| `docs/channel-switching-performance.md` | Measured tune latency (4 s since 4.8.0; was 7 s) and ranked strategies to improve it |
| `docs/direct-stream-and-crt-static-spec.md` | Direct HTTP byte-range playback (<1s tunes, fixes overnight browse wedge) + CRT static snow (4.9.0) |
| `docs/pre-rendered-guide-channel-spec.md` | Pre-rendered 90s Prevue-style scrolling video guide on Channel 1 (15m chunks, 1/3+2/3 layout, episode stills, smooth jazz) |
| `docs/jellyfin-12-upgrade-spec.md` | Jellyfin 12.3 + ClassicTV plugin 12.0.0.1 (`net10.0`) compatibility assessment and upgrade plan |
| `docs/seek-strategy-findings.md` | Why `StartOffset` beats `seekTime()` (still valid reference) |
| `docs/player-design-notes.md` | **Historical** 3.0 seek strategy notes |
| `CLASSIC_TV_3_SPEC.md` | **Historical** v3 system spec |
| `legacy/README.md` | What was decommissioned and why |
| Plugin `DEPLOYMENT.md` | Plugin build/deploy details |
| Plugin `docs/schedule-architecture.md` | Plugin schedule/duration architecture |
| Plugin `docs/start-anchor-spec.md` | Why the fixed anchor exists |

---

## 11. Implementation status (tiers)

- **Tier 1 — complete.** Callback verification (`tier-1-callback-findings.md`), offline-retry
  thread leak fixed (single cancellable worker), keymap fixed/validated and `<global>` guide
  added, schedule-client JSON type validation added, deploy improvements.
- **Tier 2 — complete in code (4.5.0), not yet verified on device.** `ModeState` owns
  TV / BROWSE / RESUME_PENDING. Viewer stop or foreign playback enters BROWSE; a natural end of
  browse playback starts a 5 s countdown (RESUME_PENDING) and then re-tunes; EPG toggles;
  ch+/-, digits are ignored outside TV mode; the watchdog skips paused playback. Foreign playback
  is detected conservatively (`_is_own_playback`: inside the play grace window, or an unreadable /
  matching `jellyfin_playing_id`, counts as ours). Deviation from the tier-2 spec: a *stop* (not
  an EOF) in browse mode does not start the resume countdown, so stopping a movie to pick another
  does not yank the viewer back.
- **Tier 3 — complete in code (4.5.0).** Digit entry now shows "CH N_" / "Not Found" on screen
  through the existing `ChannelBug` dialog (`show_text`/`hide`), so no new skin XML. The dead
  `ChannelStatic` overlay and its constant were deleted (no XML, no noise asset, unverifiable
  on the CRT); the ident bug covers the tune feedback.
- **Tier 4 — partial.** Non-blocking command queue was implemented then **reverted** because
  it silently dropped key presses; dispatch is currently direct. v3 schedule service
  decommissioned. 
- **Tier 5 — deferred.** Skin-based overlays, etc.
- **Schedule rotation fix (10.11.0.7/8, 2026-09-19):** full-catalog rotation + duration
  startup trigger. Regression test:
  `ComputeSchedule_AdvancesThroughFullCatalogAcrossCycles` in the plugin test project.
- **Channel management UI (10.11.0.15, 2026-09-20):** Jellyfin dashboard config page
  (`Configuration/configPage.html`) plus `/ClassicTV/manage/*` endpoints, server-side
  validation, and `number`/`enabled`/`shuffle` fields. The addon consumes `number` and
  handles disabled-while-tuned. See
  `docs/tier-specs/channel-management-ui-spec.md` and `.kiro/specs/channel-management-ui/`.
  **Deployed 2026-09-20.** Channels currently have `number: 0` (unassigned), so the addon
  falls back to positional numbering; assign numbers via the dashboard config page to enable
  server-authoritative digit entry.
- **Viewer browse suspension (addon 4.4.0, 2026-09-20):** `onPlayBackStopped` sets a browse flag that makes the watchdog ignore stopped playback, so browsing no longer gets yanked back to a channel. Guarded against Classic TV's own channel-change stops (`_internal_stop_time`) and natural EOFs (`_eof_time`); `play_slot()` clears it; the global EPG button (`guide`) toggles back to Classic TV. Minimal Tier 2 subset — full `BROWSE_MODE`/`RESUME_PENDING` is still unimplemented. Superseded by the full three-mode machine in 4.5.0.
- **Sweet Potato boot network race (addon 4.4.0, 2026-09-20):** Three-layer boot-resilience system. Layer 1: `classictv-wait-for-jellyfin.service` (systemd `oneshot` boot gate); Layer 2: `_wait_for_jellyfin_server()` (ClassicTV Python readiness gate with 90 s cap and OSD notification); Layer 3: `_reload_jellyfin_addon()` self-heal path triggered by `_consecutive_failures >= WEDGE_FAILURE_THRESHOLD (3)` in `watchdog_recover()`. Also raises `WAIT_NETWORK_TIME` to 30 s. All Python logic covered by 23 tests in `tests4/test_boot_resilience.py` (18 example-based + 5 Hypothesis property tests).
- **Channel numbers (4.5.0):** the addon's positional fallback now skips numbers already taken
  (live Anime is `2`, which used to collide with the second channel's positional `2`).
  `tools/assign_channel_numbers.py` numbers every unassigned channel on the live plugin (dry run by
  default; `--apply` needs `$JELLYFIN_API_KEY` and writes a backup first). **Not applied yet.**
- **Digit entry timeout (4.5.1):** while the typed digits are the start of a longer channel number
  ("1" with channels 10-18), entry waits 3.5 s instead of 1.5 s, so two-digit channels are reachable
  on the slow IR remote. Found on device: pressing "1" tuned channel 1 before "2" arrived.
- **Keys are lost while a Classic TV dialog is open (4.5.2):** the 4.5.0 digit-entry display used
  the `ChannelBug` dialog; with it open, the second digit never reached the service (found on
  device: `1` shown, then nothing, on two attempts). Kodi appears to resolve remote keys in the open
  dialog's context, where the `<FullscreenVideo>` NotifyAll mappings don't apply. Fixes: digit entry
  now shows as a notification toast (`ToastDisplay`, no input focus), and `ChannelBug.onAction`
  translates digit (58-67) and ch+/- (5/6) actions to `dispatch()` so the remote also works during
  the 10 s ident. The action ids are unverified on device; `onAction id=` is logged at DEBUG.
- **Browse-mode playback controls (4.6.0):** the `<FullscreenVideo>` keymap claims OK/d-pad for
  channel entry in *every* fullscreen video, which left browse-mode playback with no pause, seek or
  subtitle control. In browse mode the service now maps OK = pause, Left/Right = step, Up = OSD,
  Down = cycle subtitles (`browse_controls.py`). Each playback logs `streams subtitle_streams=N ...`
  3 s after it starts, to explain the "sometimes subtitles" behaviour. Physical play/pause/FF/REW
  buttons still need their IR scancodes captured (`tools/scan_remote.sh transport`). See
  `docs/browse-mode-controls.md`.
- **Rachel's channel / mirror channels (addon 4.7.0, plugin 10.11.0.16):** a channel with `mirrorUser`
  plays what that Jellyfin user is watching, at their position (replaces the 3.0 `rachel-anchor`).
  Plugin: `Schedule/Mirror.cs`; addon: `mirror.py`. Needs the plugin installed and
  `tools/add_mirror_channel.py --apply` before the channel exists. See `docs/rachel-mirror-channel.md`.
- **Faster channel changes (4.8.0):** a channel change no longer stops playback and waits for the decoder
  first; it issues `PlayMedia` directly and Kodi replaces the stream (`STOP_BEFORE_PLAY = False`).
  Measured median key-to-picture 4.0 s against 7.1 s over 30 clean tunes, with no stalls or decoder
  trouble. Mirror retunes and entering browse mode still stop first. Use `tools/measure_tunes.py`, and
  restart Kodi with `systemctl restart kodi`, never by disabling/enabling the addon (that leaves a ghost
  copy of the service running). See `docs/channel-switching-performance.md` sections 6 and 7.
- **Direct HTTP stream playback & Amlogic decoder stabilization (4.9.0 / 4.9.1 / 4.9.2):** `play_slot()` plays
  `http://{base}/Videos/{id}/stream?static=true` directly via `xbmc.Player().play(url, listitem)` with
  `StartOffset`, `setContentLookup(False)`, and Kodi 21 `InfoTagVideo` (`DIRECT_HTTP_PLAYBACK = True`),
  bypassing `plugin.video.jellyfin` during 24/7 TV mode. Measured on the Sweet Potato (`kodi.log`):
  `play_slot()` to `onPlayBackStarted` is **0.19–0.42 s** (down from 7.77 s in 4.8.1), and first video frame
  (`onAVStarted`) renders in **0.60–0.76 s**. Keeps `plugin.video.jellyfin` untouched overnight so Browse Mode
  stays healthy, and raises `JELLYFIN_RELOAD_WAIT_SECONDS` from 2 s to 7 s (past Kodi's 5 s
  `CPythonInvoker` kill timeout). `4.9.1` removes full-screen `static_0..3.png` textures from
  `ChannelBug.xml`, adds a 3 s cold-boot settle pause in `startup_play()`, and waits for
  `_play_confirm_event` (`_wait_before_overlay()`) before opening `ChannelBug.xml` so `osd0` GUI init
  never collides with `DVDVideoCodecAmlogic` (`vd1`) open. `4.9.2` detects truncated MKV files that hit
  premature EOF (`played < 20s` with `> 30s` remaining), immediately advances to `NextItem` at `offset=0.001s`
  (and skips the truncated item on subsequent retunes), ends `is_in_grace_period()` on `onPlayBackEnded`,
  and serializes/supersedes rapid `_change_channel()` calls via `_tune_seq`. See
  `docs/direct-stream-and-crt-static-spec.md`.
- **Sony Trinitron Green OSD & Extended CRT Static (4.11.0 / 4.11.1):** ChannelBug redesigned with Sony Trinitron
  phosphor green (`#22FF44`) OSD card (`528×152 px`), `CH 04` formatted display, `STEREO` indicator badge,
  full title display, and persistent `Window(Home).Property(ClassicTV.ActiveChannelNumber)` for status
  reporting even after the 10 s OSD auto-dismisses. Static hold extended to `min=1.4s`, `post_av=0.75s`, and
  guarded with `_awaiting_av` so Amlogic video plane (`vd1`) re-configuration is completely hidden behind analog
  snow, eliminating the black gap and initial stutter during channel changes. The full-bleed dummy anchor in
  `ChannelBug.xml` uses a zero-draw `<control type="label">` to avoid the opaque white box bug in Kodi's GLES parser
  caused by evaluating `colordiffuse="00000000"` on images as unset.
- **Hardware Remediation Queue & Re-encoder Daemon:** `tools/classictv_worker.py` daemonized on `jfmac`
  via `launchd` (`com.classictv.reencoder`), scanning 48h broadcast schedules to prioritize re-encoding
  problematic files (such as VideoToolbox Level 5.0 10-bit HEVC) to clean H.264 High@L3.1 with `.orig.mkv` backup.
  Added `reset_orphaned_jobs()` for automated recovery of stranded jobs across reboots/restarts.
- **TV Guide Channel 1 Pre-Renderer (16:9 Widescreen):** `tools/guide_renderer.py` continuously renders rolling
  15-minute 90s Prevue-style chunks to `classictv-guide/` cache with smooth jazz audio, maintaining a 3.5h–4h forward buffer.
  Rendered natively at **1280×720 (16:9)** to match the widescreen geometry of the Sony KV-34HS510 CRT (eliminating 4:3 pillarboxes)
  with wider channel title fields, 16:9 preview thumbnails (106×60), and 590 px title regions. Daemonized on `jfmac` via `launchd`
  (`com.classictv.guide_renderer`).
- **Fixed 1080p @ 59.94 Hz Output Lock:** Kodi's `videoplayer.adjustrefreshrate` is set to `0` (Off) in memory and
  `/storage/.kodi/userdata/guisettings.xml`, with `/storage/.config/autostart.sh` setting `frac_rate_policy=1` and mode `1080p60hz`
  (enforced by `deploy/deploy-sweet-potato-4.sh`). This keeps the SBC locked to 1080p at 59.94 Hz (`sync_duration 2997/50`),
  providing exact 2:2 / 3:2 frame cadence for broadcast 29.97/23.976 fps material while completely stopping Kodi from renegotiating
  HDMI timing between 59.94 Hz and 60.00 Hz on every tune (preventing CRT deflection yoke clicking, signal dropout `OnLostDisplay`,
  and buffer timeouts).
- **Status Monitor Daemon:** `tools/classictv-status.py` daemonized on `jfmac` via `launchd`
  (`com.classictv.status_monitor`), continuously updating `web/status.json` every 30s to feed `https://tv.dane.photos/status/`.
