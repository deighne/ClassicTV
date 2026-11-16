# Direct HTTP Stream Playback & Amlogic Decoder Stabilization — Classic TV 4.9.0 / 4.9.1

**Date:** November 1, 2026  
**Status:** Implemented & verified on Sweet Potato in v4.9.1  
**Related:** `AGENTS.md`, `docs/channel-switching-performance.md`, `docs/seek-strategy-findings.md`

---

## 1. Problems Addressed

1. **Overnight Browse Mode / Jellyfin Auth Breakdown:**
   - Every tune and 24/7 episode transition previously ran `PlayMedia(plugin://plugin.video.jellyfin/?mode=play&id=...&timestamp=...)`, spawning a cold `CPythonInvoker` process 48+ times per day.
   - When 3 consecutive plays exceeded `PLAYBACK_CONFIRM_TIMEOUT_SECONDS` (25 s), `watchdog_recover()` called `_reload_jellyfin_addon()`, which issued `Addons.SetAddonEnabled(False)` followed `JELLYFIN_RELOAD_WAIT_SECONDS = 2` seconds later by `Addons.SetAddonEnabled(True)`.
   - On CoreELEC, Kodi's `CPythonInvoker` waits **5 seconds** (`script didn't stop in 5 seconds - lets kill it`) before force-killing a service script. Re-enabling after 2 seconds raced with the teardown of the old `plugin.video.jellyfin` service instance, wiping out `Window(Home)` session/auth properties and leaving Browse Mode broken the next morning.

2. **4–9 s Channel Switch Latency (and 30–95 s Stalls):**
   - Spawning `plugin.video.jellyfin/default.py` on the Amlogic S905X Cortex-A53 cost **3.5–7.5 s** of Python import, SQLite, and HTTP overhead before `ffmpeg` even received a stream URL, whereas opening the HTTP byte-range stream itself takes **0.29 s** (`0.78 s` to first video frame).

3. **Overlay Input Focus & Amlogic Hardware Decoder Contention:**
   - `ChannelBug` did not forward `confirm` (`OK`) or `guide` (`EPG`) actions while open.
   - On the Amlogic S905X (`CoreELEC 21.3`), opening `ChannelBug.xml` (`LOAD_ON_GUI_INIT` on `osd0`) with 4 full-screen `static_0..3.png` textures at the exact same millisecond as `VideoPlayer::OpenFile` (`DVDVideoCodecAmlogic` on `vd1`, e.g. `01:19:30.069` vs `01:19:30.070` in `kodi.log`) locked up the kernel video driver (`amstream`/`di_cma`) and caused cold-boot hardware resets.

---

## 2. Architecture in v4.9.1

### 2.1 Direct HTTP Byte-Range Playback (`DIRECT_HTTP_PLAYBACK = True`)

`ClassicTVPlayer.play_slot()` constructs the direct Jellyfin static stream URL:

```
http://{base_url}/Videos/{jellyfin_item_id}/stream?static=true
```

and plays it directly via `xbmc.Player().play(stream_url, listitem)` with:

- `listitem.setContentLookup(False)` and `listitem.setProperty("IsFolder", "false")` — matches `plugin.video.jellyfin`'s `actions.py` so Kodi skips synchronous HTTP MIME-type sniffing on the main GUI thread.
- `listitem.setProperty("StartOffset", f"{offset:.3f}")` — applied synchronously by Kodi's C++ demuxer before the first video frame is decoded (no post-start `seekTime()` calls).
- `listitem.getVideoInfoTag()` (`setTitle`, `setMediaType("episode")`, `setResumePoint(offset, duration)`) — Kodi 21 Omega native `InfoTagVideo` API (zero deprecation warnings).
- `Window(10000).setProperty("jellyfin_playing_id", "")` — cleared before `self.play()` so `plugin.video.jellyfin`'s player monitor ignores Classic TV's 24/7 background playback completely (zero multiple-player conflict, zero Borg state churn, zero pollution of the user's "Continue Watching" row).
- `JELLYFIN_RELOAD_WAIT_SECONDS` raised from `2` to `7` (exceeding Kodi's 5 s `CPythonInvoker` kill timeout) so any explicit reload waits for the old service instance to fully exit before starting a new one.

### 2.2 Empirical Device Timings & Amlogic `osd0`/`vd1` Sequencing (`v4.9.1`)

- **Measured on Sweet Potato (`kodi.log`):**
  - `play_slot()` to `onPlayBackStarted`: **0.29 s** (down from 7.77 s in v4.8.1).
  - `play_slot()` to `onAVStarted` (first decoded video frame at `StartOffset`): **0.78 s**.
- **Sequencing `ChannelBug.xml` after `onPlayBackStarted` (`_wait_before_overlay`):**
  - Because direct HTTP playback confirms in `~0.29 s`, `_change_channel()` and `startup_play()` call `_wait_before_overlay()` (waiting up to 500 ms in 50 ms steps for `_play_confirm_event`) *before* calling `ChannelBug.show_for()`.
  - `ChannelBug.xml` uses only the lightweight 1×1 `white.png` corner panel (no full-screen static PNG textures), and `startup_play()` waits 3 s at cold boot so GLES/CEC/ALSA initialization finishes before `amstream` opens.

### 2.3 Premature-EOF Recovery & Rapid Channel-Change Coalescing (`v4.9.2`)

- **Truncated container detection (`_premature_eof_items`):** If an MKV file on disk is truncated before its header `Duration` (e.g. *Supernatural* S11E09 `9d2179f2868d18a2a7333e706e670ebb`, whose header reports `2552.8 s` while the file ends at `1237.9 s`), seeking past the truncation point causes `CFileCache::Seek` to fail and `onPlayBackEnded()` to fire within `< 20 s` while `> 30 s` remain in the scheduled slot. `onPlayBackEnded()` records the item's failed offset in `_premature_eof_items` and immediately calls `play_slot()`, which substitutes `slot.next_item` at `StartOffset = 0.001 s` and skips the truncated item on any subsequent tune or watchdog recovery during that window.
- **Immediate post-EOF grace clear:** `is_in_grace_period()` returns `False` as soon as `onPlayBackEnded()` fires (`_eof_time >= _last_play_time`), so the watchdog never waits up to 30 s after an early episode end.
- **Rapid channel-change coalescing (`_tune_seq`):** `_change_channel()` updates `_current_channel_id` and increments `_tune_seq` immediately on entry, and aborts before `play_slot()` / `ChannelBug.show_for()` if superseded by a newer button press while `get_slot()` was in flight.

### 2.4 GPU-Animated CRT Static Between Channels (`v4.9.3`)

- **Single 15 KB texture (`static_tile.png`, 128×128 grayscale):** Replaces the 4 full-screen `static_0..3.png` textures from `v4.9.0`. Two overlapping `<control type="image">` layers (`5010` and `5011`) in `ChannelBug.xml` reference `static_tile.png` and are animated entirely in C++ GLES by Kodi's `<animation effect="slide" ... pulse="true" condition="true">Conditional</animation>` and `<animation effect="fade" ... pulse="true" condition="true">Conditional</animation>` tags — zero Python background thread and zero `Window(10000).setProperty()` loop.
- **No static on cold boot (`startup_play`):** `startup_play()` calls `show_for()` only *after* `onPlayBackStarted` (`_wait_before_overlay()`), pre-warming `ChannelBug.xml` (`LOAD_ON_GUI_INIT`) safely once `DVDVideoCodecAmlogic` is already running.
- **Sequenced `osd0` before `vd1` on channel changes:** `_change_channel()` calls `show_static(target.number, target.name)` followed by `xbmc.sleep(35)` (~2 frames at 60 Hz) before `get_slot()` and `play_slot()`, ensuring `osd0` finishes drawing before `DVDVideoCodecAmlogic` opens `vd1`. When `onAVStarted()` fires (~500–650 ms later), `hide_static()` clears `ClassicTV.StaticVisible` in a single property write while leaving the corner channel badge visible for its 10 s countdown.
