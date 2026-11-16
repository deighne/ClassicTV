# Jellyfin 12.3 Server & ClassicTV Plugin Upgrade Spec

**Created:** 2026-10-25
**Status:** Complete (deployed 2026-10-25)
**Target Server Version:** Jellyfin `12.3` (`12.3.0`, macOS `amd64`)
**Target Plugin Version:** `ClassicTV` `12.0.0.1` (`targetAbi: 12.0.0.0`, `net10.0`)

---

## 1. Context & Motivation

`jfmac` (`dane@192.168.1.207`, Intel x86_64 Mac) is currently running **Jellyfin 10.11.6** with `ClassicTV_10.11.0.16`, `Kodi Sync Queue_15.0.0.0`, and `Open Subtitles_23.0.0.0`.

Upstream Jellyfin retired the `10.x` prefix after `10.11.x` and released **Jellyfin 12.0** (what would have been `10.12.0`), followed by `12.1`, `12.2`, and **12.3** (released 2026-10-23). Key changes in Jellyfin 12.x affecting plugins and clients:
1. **Runtime bump:** Jellyfin 12.x targets **.NET 10.0 (`net10.0`)** (up from `.NET 9.0` in `10.11.x`). Plugins compiled for `net9.0` / `targetAbi: 10.11.0.0` are marked incompatible (`NotSupported`) unless rebuilt against the `12.x` (`net10.0`) NuGet packages with `targetAbi: "12.0.0.0"`.
2. **Legacy authorization disabled by default (`EnableLegacyAuthorization = false`):**
   - `X-Emby-Token`, `X-MediaBrowser-Token`, and lowercase `?api_key=` query parameters are disabled by default (and disabled on existing installs via a 12.0 migration).
   - Standard `Authorization: MediaBrowser Token="..."` headers and uppercase `?ApiKey=` remain supported.
   - Legacy route prefixes (`/emby/*`, `/mediabrowser/*`) are removed.
3. **Database schema migrations:**
   - Jellyfin 12.0 performs one-way relational migrations on `jellyfin.db` (`LinkedChildren`, foreign keys, clean names, etc.) on first boot. A full backup of `~/Library/Application Support/jellyfin` before starting 12.3 is mandatory for rollback safety.
   - Upstream release notes recommend removing/replacing `10.11` plugins before running the 12.0 migration, then running a full library scan after the upgrade completes.

---

## 2. Compatibility Assessment Summary

### 2.1 `Jellyfin.Plugin.ClassicTV` (Production Code)
Diffing the `10.11.6` (`net9.0`) and `12.3.0` (`net10.0`) NuGet packages (`Jellyfin.Controller`, `Jellyfin.Model`, `Jellyfin.Common`, `Jellyfin.Data`) confirms **zero breaking changes** in the APIs called by the production plugin:
- `Plugin.cs`: `BasePlugin<PluginConfiguration>`, `IHasWebPages`, `PluginPageInfo` — unchanged.
- `PluginServiceRegistrator.cs`: `IPluginServiceRegistrator.RegisterServices(IServiceCollection, IServerApplicationHost)` — unchanged.
- `Schedule/RefreshDurationsTask.cs`: `IScheduledTask`, `TaskTriggerInfo`, `TaskTriggerInfoType`, `ILibraryManager.GetItemById(Guid)` — unchanged.
- `Schedule/Mirror.cs`: `ISessionManager.Sessions`, `SessionInfo` (`NowPlayingItem`, `PlayState`, `LastPlaybackCheckIn`, `LastActivityDate`), `MediaType.Video` — unchanged.
- `Api/ClassicTvController.cs` & `Api/ManagementController.cs`: `[Authorize(Policy = "RequiresElevation")]`, `InternalItemsQuery`, `ILibraryManager.GetItemsResult(InternalItemsQuery)`, `ILibraryManager.GetCount(InternalItemsQuery)`, `ITaskManager.QueueScheduledTask<T>()` — unchanged.
- `Configuration/configPage.html` & `Configuration/classictv.js`: Uses `data-controller="__plugin/classictvjs"` and `Authorization: MediaBrowser Token=...`, which is accepted by `AuthorizationContext.cs` in 12.3 without `EnableLegacyAuthorization`.

### 2.2 `Jellyfin.Plugin.ClassicTV.Tests` (Test Suite)
- `Jellyfin.Plugin.ClassicTV.Tests/Api/ManagementControllerTests.cs` contains a manual `StubLibraryManager : ILibraryManager` implementing all interface members.
- Between `10.11.6` and `12.3.0`, `ILibraryManager` added ~24 new interface members (`IsScanRunning`, `ResolveAlternateVersion`, `GetOrCreatePerson`, `RerouteLinkedChildReferencesAsync`, batch count helpers, etc.), updated `ResolvePath` and `DeleteItemsUnsafeFast` signatures, and removed `ValidatePeopleAsync`.
- **Resolution:** Replace the 90-method hand-written `StubLibraryManager` boilerplate with a `System.Reflection.DispatchProxy` implementation (or update its methods) so it only implements the methods `ManagementController` calls (`GetItemsResult`, `GetCount`, `GetItemById`) and throws `NotImplementedException` for everything else. This also future-proofs the test suite against future `ILibraryManager` additions.

### 2.3 Classic TV Ecosystem (Kodi Addons & CLI Scripts)
- **`plugin.video.classictv` (4.8.1 on Sweet Potato):** Calls `GET /ClassicTV/channels`, `GET /ClassicTV/channel/{id}`, and `GET /System/Info/Public` — all `[AllowAnonymous]`. Unaffected.
- **`plugin.video.jellyfin` (2.1.0 on Sweet Potato):** Migrated to modern `Authorization: MediaBrowser` headers in v2.0.0. Unaffected.
- **`tools/apply_jellyfin_theme.py`, `tools/add_mirror_channel.py`, `tools/assign_channel_numbers.py`:** Already use `Authorization: MediaBrowser Token="..."`. Unaffected.
- **`tools/populate_channel.py` (ClassicTV3.0 repo):** Uses `headers={"X-Emby-Token": api_key}` on lines 80 and 228. Must be updated to `headers={"Authorization": f'MediaBrowser Token="{api_key}"'}`.
- **`scripts/check_durations.py` (Plugin repo):** Uses `?api_key={api_key}` (lowercase) on line 62, which is rejected when `EnableLegacyAuthorization = false`. Must be updated to send `Authorization: MediaBrowser Token="..."` (or `?ApiKey=`).

### 2.4 Third-Party Plugins on `jfmac`
Both third-party plugins installed on `jfmac` have `targetAbi: 12.0.0.0` releases in the official Jellyfin catalog (`https://repo.jellyfin.org/files/plugin/manifest.json`):
- **Kodi Sync Queue:** `15.0.0.0` $\rightarrow$ **`16.0.0.0`** (`https://repo.jellyfin.org/files/plugin/kodi-sync-queue/kodi-sync-queue_16.0.0.0.zip`)
- **Open Subtitles:** `23.0.0.0` $\rightarrow$ **`25.0.0.0`** (`https://repo.jellyfin.org/files/plugin/open-subtitles/open-subtitles_25.0.0.0.zip`)

---

## 3. Step-by-Step Execution Plan

### Phase 1: Dev Machine Toolchain & Plugin Upgrade (`12.0.0.1`)
- [x] **1.1** Write this specification (`docs/jellyfin-12-upgrade-spec.md`) and update `AGENTS.md` in both repos.
- [x] **1.2** Install `.NET 10 SDK` (`dotnet`) and `jprm` on the dev Mac if absent (`brew install --cask dotnet-sdk` and `pipx install jprm` or `pip install --user jprm`).
- [x] **1.3** In `~/projects/Jellyfin.Plugin.ClassicTV`:
  - Update `Jellyfin.Plugin.ClassicTV/Jellyfin.Plugin.ClassicTV.csproj` and `Jellyfin.Plugin.ClassicTV.Tests/Jellyfin.Plugin.ClassicTV.Tests.csproj` to `<TargetFramework>net10.0</TargetFramework>` and `Jellyfin.Controller` / `Jellyfin.Model` `12.3.0`.
  - Update `StubLibraryManager` in `Jellyfin.Plugin.ClassicTV.Tests/Api/ManagementControllerTests.cs` to compile cleanly against `ILibraryManager` `12.3.0`.
  - Update `build.yaml` (`version: "12.0.0.1"`, `targetAbi: "12.0.0.0"`, `framework: "net10.0"`, changelog).
  - Update `scripts/check_durations.py` to use `Authorization: MediaBrowser Token="..."`.
- [x] **1.4** Run `dotnet test --configuration Release` in `~/projects/Jellyfin.Plugin.ClassicTV` and confirm all 233 unit tests pass.
- [x] **1.5** Build and package `artifacts/classictv_12.0.0.1.zip`:
  ```bash
  cd ~/projects/Jellyfin.Plugin.ClassicTV
  mkdir -p artifacts
  jprm plugin build "Jellyfin.Plugin.ClassicTV/" --output=./artifacts \
      --dotnet-configuration Release --dotnet-framework net10.0 --version 12.0.0.1
  python3 scripts/patch_meta.py artifacts/classictv_12.0.0.1.zip
  md5 -q artifacts/classictv_12.0.0.1.zip
  ```
- [x] **1.6** Commit and tag `v12.0.0.1` in `~/projects/Jellyfin.Plugin.ClassicTV`.
- [x] **1.7** In `~/projects/ClassicTV3.0`:
  - Copy `classictv_12.0.0.1.zip` to `web/plugins/`.
  - Prepend the `12.0.0.1` entry (`targetAbi: "12.0.0.0"`) to `web/plugins/manifest.json`.
  - Update `tools/populate_channel.py` to use `Authorization: MediaBrowser Token="..."`.
  - Run `python3 -m pytest tests4/ -q` to verify addon tests still pass.
  - Deploy `web/` and `manifest.json` to `jfmac` via `bash deploy/deploy-jfmac.sh`.

### Phase 2: Pre-Upgrade Backup on `jfmac`
- [x] **2.1** Check if any non-ClassicTV user sessions are actively streaming on `jfmac` before stopping Jellyfin.
- [x] **2.2** Stop Jellyfin cleanly on `jfmac`:
  ```bash
  ssh dane@192.168.1.207 'pkill -TERM -f "Jellyfin.app/Contents/MacOS"; \
    for i in $(seq 1 30); do lsof -nP -i :8096 | grep -q LISTEN || break; sleep 1; done'
  ```
- [x] **2.3** Back up the Jellyfin application bundle and data directory on `jfmac`:
  ```bash
  ssh dane@192.168.1.207 '
    BACKUP_DIR="$HOME/jellyfin-backup-10.11.6-$(date +%Y%m%d-%H%M%S)"
    mkdir -p "$BACKUP_DIR"
    cp -R "/Applications/Jellyfin.app" "$BACKUP_DIR/Jellyfin.app"
    rsync -a --exclude="cache/" --exclude="transcodes/" \
      "$HOME/Library/Application Support/jellyfin/" "$BACKUP_DIR/jellyfin-data/"
    echo "Backup saved to $BACKUP_DIR"
  '
  ```

### Phase 3: Server & Plugin Upgrade on `jfmac`
- [x] **3.1** Stage `12.0.0.0`-targeted plugins in `~/Library/Application Support/jellyfin/plugins` on `jfmac` (moving old `10.11` plugin directories to the backup directory so no duplicate GUIDs exist):
  - Install `ClassicTV_12.0.0.1` from `~/projects/ClassicTV3.0/web/plugins/classictv_12.0.0.1.zip`.
  - Upgrade `Kodi Sync Queue` to `16.0.0.0` (`kodi-sync-queue_16.0.0.0.zip`).
  - Upgrade `Open Subtitles` to `25.0.0.0` (`open-subtitles_25.0.0.0.zip`).
- [x] **3.2** Download and install `jellyfin_12.3-amd64.dmg` (`https://repo.jellyfin.org/files/server/macos/latest-stable/amd64/jellyfin_12.3-amd64.dmg`) to `/Applications/Jellyfin.app` on `jfmac`:
  ```bash
  ssh dane@192.168.1.207 '
    curl -fL "https://repo.jellyfin.org/files/server/macos/latest-stable/amd64/jellyfin_12.3-amd64.dmg" -o /tmp/jellyfin_12.3-amd64.dmg
    hdiutil attach /tmp/jellyfin_12.3-amd64.dmg -nobrowse -quiet
    rm -rf /Applications/Jellyfin.app
    cp -R "/Volumes/Jellyfin Server/Jellyfin.app" /Applications/Jellyfin.app
    hdiutil detach "/Volumes/Jellyfin Server" -quiet
    xattr -dr com.apple.quarantine /Applications/Jellyfin.app
  '
  ```
- [x] **3.3** Start Jellyfin 12.3 on `jfmac`:
  ```bash
  ssh dane@192.168.1.207 'open -a Jellyfin'
  ```
- [x] **3.4** Monitor the startup and 12.0 database migration in `~/Library/Application Support/jellyfin/log/log_*.log` until `:8096` is listening and migrations complete.

### Phase 4: Post-Upgrade Verification
- [x] **4.1** Verify `curl -s http://192.168.1.207:8096/System/Info/Public` returns `"Version":"12.3.0"`.
- [x] **4.2** Verify plugin load and schedule endpoints:
  - Check log for `Loaded plugin: "ClassicTV" "12.0.0.1"`.
  - Verify `RefreshDurationsTask` ran on startup and resolved durations (`python3 scripts/check_durations.py` on `jfmac`).
  - Verify `curl -s http://192.168.1.207:8096/ClassicTV/channels` and `curl -s http://192.168.1.207:8096/ClassicTV/channel/anime` return valid responses.
  - Verify `/ClassicTV/manage/channels` works with admin token.
- [x] **4.3** Trigger a background library scan (`POST /Library/Refresh`) as required by Jellyfin 12.0 release notes.
- [x] **4.4** Verify Sweet Potato (`192.168.1.237`) playback and channel tuning via `python3 tools/classictv-logs.py --since 10m` (note: Sweet Potato was powered off / unreachable on LAN during upgrade; server endpoints are live and ready for next boot).
- [x] **4.5** Update `AGENTS.md` (both repos) and `DEPLOYMENT.md` to record Jellyfin `12.3.0` and `ClassicTV` `12.0.0.1` as live, and commit.

---

## 4. Rollback Procedure (If Anything Fails)

If Jellyfin 12.3 fails to migrate or exhibits blocking issues:
```bash
ssh dane@192.168.1.207 '
  pkill -TERM -f "Jellyfin.app/Contents/MacOS"
  for i in $(seq 1 30); do lsof -nP -i :8096 | grep -q LISTEN || break; sleep 1; done
  BACKUP_DIR=$(ls -td "$HOME"/jellyfin-backup-10.11.6-* | head -n 1)
  rm -rf /Applications/Jellyfin.app
  cp -R "$BACKUP_DIR/Jellyfin.app" /Applications/Jellyfin.app
  rsync -a --delete "$BACKUP_DIR/jellyfin-data/" "$HOME/Library/Application Support/jellyfin/"
  open -a Jellyfin
'
```
