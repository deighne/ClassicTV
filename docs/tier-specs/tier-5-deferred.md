# Tier 5 Spec — Longer-Horizon Items

**Status:** Deferred. Still not blocking v4.0. Current state per item:
- **5.1 Skin overlays** — not done (overlays remain dead code; see Tier 3).
- **5.2 Plugin source in version control** — partially done. The plugin source is its own
  git repo (`~/projects/Jellyfin.Plugin.ClassicTV`) and is tagged (`v10.11.0.8`), but it is
  not linked to ClassicTV3.0 (no submodule/monorepo) and has no remote or CI.
- **5.3 Integration smoke test** — not done.
- **5.4–5.6** — not done. Note: 5.5 (window-property observability) is a prerequisite for
  Tier 2's BROWSE auto-detection if `jellyfin_playing_id` is unavailable.

**Dependencies:** Tiers 1–4 complete; architectural decisions made (overlays, plugin source)

These items are real work but are not blocking v4.0 stability or the mode-switching
feature. They should each be tracked as standalone work items.

---

## 5.1 Skin-based overlays

**Context:** `classic-tv-4-architecture.md` says "look and feel becomes pure Kodi skinning."
`ChannelBug` and `ChannelStatic` are currently Python `WindowXMLDialog` classes with no
skin XML. Tier 3 wires them correctly, but they render nothing visible until XML exists.

**What this involves:**
- A custom Kodi skin (or skin overlay) that provides the `ChannelBug.xml` and
  `ChannelStatic.xml` `WindowXMLDialog` layouts.
- `ChannelBug.xml` needs: channel number label (≥48pt), channel name, show title, accent
  color. The Python class already passes these as constructor args; the skin XML reads
  them via control IDs.
- `ChannelStatic.xml` needs: full-screen noise animation (could be a `.gif` or a
  `VisualEffect` tag if Kodi supports it), destination channel number overlay.
- The 6-second `CHANNEL_BUG_DISPLAY_SECONDS` burn-in protection must remain enforced in
  Python, not in the skin. The skin should not control dismissal timing.

**Note:** A skin overlay approach (modifying the existing Estuary skin) vs a full custom
skin is a design decision for this work item.

---

## 5.2 Bring server plugin source into version control

**Context:** `web/plugins/` contains only built zips and `manifest.json`. The C# source
lives in `~/projects/Jellyfin.Plugin.ClassicTV` — a separate, unlinked repo with no remote.

**Options:**
- **Git submodule** in `ClassicTV3.0/server-plugin/` pointing to the plugin repo
  once it has a remote. Most flexible; keeps histories separate.
- **Monorepo** — move plugin source into `ClassicTV3.0/server-plugin/`. Simpler for
  this project's scale.

**Either way:** The plugin repo needs a GitHub remote, consistent tagging alongside
`ClassicTV3.0` tags, and a CI pipeline that produces the zip artifact.

---

## 5.3 Integration smoke test against live server

**Context:** `tests4/test_schedule_client.py` uses a fake HTTP server. Field names in the
fake server responses could drift from the .NET plugin's actual output.

**What this involves:**
- A test module `tests4/test_integration.py` gated behind an `--integration` flag or
  an env var (`CLASSICTV_INTEGRATION=1`).
- Tests call `http://192.168.1.207:8096/ClassicTV/channels` and
  `http://192.168.1.207:8096/ClassicTV/channel/sitcoms` directly.
- Assert response field names match what `ScheduleClient` expects (case sensitivity,
  camelCase vs PascalCase, null vs absent `NextItem`).
- Run manually before each plugin version bump; could be run in CI with a VPN.

---

## 5.4 Digit-entry guide / direct channel access from browse mode

**Context:** Tier 4.5 gives channels stable numbers. Once numbers are stable, entering a
digit while in `BROWSE_MODE` could be a fast "return to channel N" gesture — matching
real TV UX. This was deferred in Tier 2 pending stable numbers.

**What this involves:**
- In `ClassicTVMonitor.onNotification()`: lift the mode gate on digit entry — allow
  digits in `BROWSE_MODE`.
- On confirm: call `_change_channel(ch.id)` which stops Jellyfin playback and resumes
  Classic TV on the requested channel.
- The keymap already routes digits from `<FullscreenVideo>` only. Digits while browsing
  a Jellyfin directory would not reach Classic TV — this is actually fine, since the
  user may be typing into a search field. Add the `<global>` digit mappings only if the
  "return to TV from browse" gesture is explicitly desired.

---

## 5.5 Kodi window property observability

**Context:** There is no way for external tools (skin, web dashboard, tests) to read the
current mode, channel, or playing item ID from Classic TV.

**What this involves:**
```python
# Set these in ModeState.enter_tv() / enter_browse() / etc.:
xbmc.executebuiltin("SetProperty(classictv.mode, tv, home)")
xbmc.executebuiltin("SetProperty(classictv.channel, sitcoms, home)")
```

Read via: `xbmc.getInfoLabel("Window(home).Property(classictv.mode)")`

Enables: skin elements that react to Classic TV mode, web dashboard that shows current
state, and test assertions without grepping log files.

---

## 5.6 Settings migration / validation

**Context:** `addon4/resources/settings.xml` has `default_channel` as free text with no
default. A typo silently falls back to the first channel.

**What this involves:**
- Add a `settings_version` field to detect future migrations.
- Add basic validation in `service.py`: if `default_channel` is set but not in the
  channel list, log a warning and fall back to channel index 0 (current behavior) rather
  than silently succeeding.
- `settings.xml` could expose a dropdown populated from the channel list — requires the
  Kodi settings API for dynamic values, which is more involved.
