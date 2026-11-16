# Kodi Addon Video Seeking: Technical Best Practices

**Status:** Still valid reference. Its conclusion — trust `StartOffset` over post-start
`seekTime()` — is exactly what 4.0 relies on (the Jellyfin addon sets `StartOffset` from the
`timestamp` parameter). The 3.0-specific recommendations at the bottom no longer apply to the
live code.
**Research date:** September 9, 2026
**Target platform:** CoreELEC 21.3-Omega (Kodi 21 / Omega) on Le Potato (Amlogic S905X)
**Reference addon:** Jellyfin Kodi addon v2.1.0+py3, installed and confirmed working on this device

---

## Background

Classic TV 3.0 needs to start video playback at a calculated mid-episode offset. This is a
common addon problem with a well-understood solution hierarchy. This document records what the
production ecosystem actually does and why, so we stop re-discovering it through trial and error.

---

## The Three Mechanisms

### 1. `ListItem.setProperty("StartOffset", str(seconds))`

The oldest and most widely supported method. Set a string property on the ListItem before
calling `xbmc.Player.play()`. Kodi reads this value when the player is initialized and
instructs the demuxer to seek before the first frame is rendered.

```python
listitem = xbmcgui.ListItem(path=url)
listitem.setProperty("StartOffset", "3600")  # start at 1 hour in
player.play(url, listitem)
```

**Key property:** The seek happens inside Kodi's playback pipeline, before `onPlayBackStarted`
fires. The player is already at the right position by the time your addon code gets control
back. This is a synchronous start-position signal, not an async seek command.

**Kodi version compatibility:** Kodi 18 (Leia) through 21 (Omega). No deprecation notices.
Still the primary mechanism used by every major streaming addon as of 2026.

**On Omega/CoreELEC 21.3:** Confirmed working. The Jellyfin addon uses this as its primary
seeking mechanism on this exact device.

---

### 2. `InfoTagVideo.setResumePoint(time, total_time)` (Kodi 20+)

Introduced in Kodi 20 (Nexus) as the replacement for the deprecated
`setProperty("ResumeTime")` / `setProperty("TotalTime")` pair. Sets metadata that Kodi stores
for library UI ("Resume from 0:37:15").

```python
tag = listitem.getVideoInfoTag()
tag.setResumePoint(3600.0, 7200.0)  # resume at 1hr, total runtime 2hr
```

**Key property:** Records where the item should resume for library tracking. By itself it does
NOT seek the player. Must be combined with `StartOffset` to actually start at that position.

**Kodi version compatibility:** Kodi 20+ only. On Kodi 18/19, use the deprecated
`setProperty("ResumeTime")` / `setProperty("TotalTime")` pair instead. JellyCon is actively
migrating to `setResumePoint()` (open issue, 2026).

**On Omega/CoreELEC 21.3:** Available. The Jellyfin addon calls this alongside `StartOffset`.

---

### 3. `xbmc.Player.seekTime(seconds)` called after start

A live, in-playback seek. The player is already running; you issue a seek command to move the
playhead.

```python
def onPlayBackStarted(self):
    xbmc.sleep(1000)
    self.seekTime(3600.0)
```

**Key property:** This is an async, post-decode seek. The decoder must be fully initialized
before `seekTime()` is safe. On Amlogic hardware (Le Potato, CoreELEC), the decoder has a
warm-up period during which `seekTime()` throws `RuntimeError` even though `onPlayBackStarted`
has already fired. This requires retry logic with sleep, a daemon thread (blocking the Kodi
callback thread on Amlogic causes a decoder doom loop), and a re-entrancy guard (because
`seekTime()` re-fires `onPlayBackStarted` on Amlogic).

**Kodi version compatibility:** Available in all Kodi versions. Reliable on x86 desktop.
Requires significant scaffolding on Amlogic/CoreELEC.

---

## What Production Addons Actually Do

### Jellyfin Kodi addon (v2.1.0+py3 — installed on the Sweet Potato)

This is the most directly relevant reference. Same Kodi version, same CoreELEC build, same
Jellyfin HTTP streams. Source: `/storage/.kodi/addons/plugin.video.jellyfin/jellyfin_kodi/objects/actions.py`

```python
# setResumePoint records resume metadata (replaces deprecated ResumeTime/TotalTime properties)
tag = listitem.getVideoInfoTag()
tag.setResumePoint(obj["Resume"] or 0, obj["Runtime"] or 0)

# StartOffset is what actually makes the player begin at the resume position
if obj["Resume"] and item.get("resumePlayback"):
    listitem.setProperty("StartOffset", str(obj["Resume"]))
else:
    listitem.setProperty("StartOffset", "0")
    listitem.setProperty("StartPercent", "0")
```

The pattern is:
1. `setResumePoint()` for library/UI metadata
2. `StartOffset` for the actual start position
3. **No `seekTime()` call anywhere for start position.** Not in `onPlayBackStarted`, not anywhere.

Netflix, JellyCon, Plex, and Emby addons follow the same pattern: `StartOffset` on the
ListItem before `play()`. None rely on `seekTime()` as primary start-position mechanism.

---

## Why Our Current Approach Is Wrong

Classic TV 3.0 currently uses `StartTimeTicks` as Strategy 1 and `seekTime()` as Strategy 2.

`StartTimeTicks` is a Jellyfin HTTP API query parameter. It is a server-side hint: the
Jellyfin server seeks to the nearest keyframe before the requested tick value and starts the
stream from there. The server chooses the keyframe, not Kodi. This is why the achieved
position after `StartTimeTicks` is frequently off by 30-90 seconds: the stream starts at a
different keyframe and Kodi's player clock reflects that.

Then `seekTime()` is called as a correction. On Amlogic this requires:
- A daemon thread (can't block the Kodi callback thread or the decoder aborts)
- A `RuntimeError` try/except (decoder not ready when `onPlayBackStarted` fires)
- Retry logic with sleep intervals
- A re-entrancy guard (`seekTime()` re-fires `onPlayBackStarted` on Amlogic)

This is four layers of complexity to compensate for the fact that Kodi never received a clean
start-position signal. `StartOffset` bypasses all of it. Kodi sets the start position before
the decoder initializes. By the time `onPlayBackStarted` fires, the player is already at the
right position.

---

## Recommended Implementation for Classic TV 3.0

```python
def play_schedule_item(self, schedule_response: dict) -> None:
    item = schedule_response["item"]
    seek_offset = float(item.get("seek_offset", 0.0))
    url = item["direct_play_url"]
    title = item.get("title", "")
    duration = float(item.get("duration", 0.0))

    listitem = xbmcgui.ListItem(label=title, path=url)
    listitem.setProperty("IsPlayable", "true")
    listitem.setInfo("video", {"title": title})

    # Library/UI resume metadata (Kodi 20+ API)
    if seek_offset > 0 and duration > 0:
        tag = listitem.getVideoInfoTag()
        tag.setResumePoint(seek_offset, duration)

    # Actual start position — set BEFORE play(), synchronous with decoder init
    listitem.setProperty("StartOffset", str(seek_offset) if seek_offset > 0 else "0")

    self.play(url, listitem)
    # onPlayBackStarted: verification/logging only, no seekTime() needed
```

`onPlayBackStarted` becomes a verification callback. Read `self.getTime()` there to confirm
the position landed correctly. Log a warning if it's outside tolerance. Do not drive
correction from it.

---

## What to Keep vs Remove

| Current code | Verdict | Reason |
|---|---|---|
| `StartTimeTicks` URL param | Remove | Jellyfin server keyframe hint, not a Kodi seek; imprecise by design |
| `_seek_with_retry()` daemon thread | Remove | Only required because `seekTime()` is unreliable; `StartOffset` doesn't need it |
| `_seek_in_progress` re-entrancy guard | Remove | Only exists to break `seekTime()` -> `onPlayBackStarted` -> `seekTime()` loops |
| `seekTime()` in `onPlayBackStarted` | Remove | Replaced by `StartOffset` set before `play()` |
| `_pending_seek` | Simplify | Still useful for verification logging in `onPlayBackStarted` |
| Position check in `onPlayBackStarted` | Keep as log-only | Useful diagnostic; just don't drive correction from it |

---

## API Compatibility Summary

| Mechanism | Kodi 18 | Kodi 19 | Kodi 20 | Kodi 21 (Omega) | Notes |
|---|---|---|---|---|---|
| `StartOffset` property | Yes | Yes | Yes | Yes | **Primary. Use this.** |
| `setResumePoint()` | No | No | Yes | Yes | Library metadata only; pair with `StartOffset` |
| `setProperty("ResumeTime")` | Yes | Yes | Deprecated | Deprecated | Logs warnings in Omega, still works |
| `seekTime()` after start | Yes | Yes | Yes | Yes (with caveats) | Unreliable on Amlogic without retry scaffolding |
| `StartTimeTicks` URL param | N/A | N/A | N/A | N/A | Jellyfin server hint, not a Kodi mechanism |

---

## Conclusion

The seeking complexity in Classic TV 3.0 is not a hardware bug or a Kodi quirk. It is a
consequence of using `seekTime()` as the primary seek driver when a better mechanism exists.
The Jellyfin addon — playing correctly on the same device with the same streams — does not use
`seekTime()` for start positions. It uses `StartOffset`, which Kodi applies synchronously
before the decoder starts. Switching to this mechanism eliminates the retry/thread
infrastructure and the classes of bugs that infrastructure produces.
