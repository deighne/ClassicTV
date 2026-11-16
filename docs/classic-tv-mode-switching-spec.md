# Classic TV 4.0 — Mode Switching Spec

**Date:** September 17, 2026
**Status:** **Superseded.** This was the original proposal. The implementation-level plan
(three-mode state machine, callback-driven) now lives in
[`tier-specs/tier-2-mode-state-machine.md`](tier-specs/tier-2-mode-state-machine.md), and the
callback assumptions written here were verified in
[`tier-specs/tier-1-callback-findings.md`](tier-specs/tier-1-callback-findings.md).
**Not implemented as of 2026-09-20.** Kept for historical context.
See also the review: [`classic-tv-mode-switching-review.md`](classic-tv-mode-switching-review.md).

---

## Problem

The Classic TV addon is a service that starts at login and runs a watchdog that polls
`isPlaying()` every 10 seconds. When playback stops for any reason, the watchdog fetches
the current channel slot and fires `PlayMedia(plugin://plugin.video.jellyfin/...)`.

There is currently no concept of the user intentionally leaving Classic TV. If the user
stops playback to browse their Jellyfin library, the watchdog will restart the channel
within 10 seconds. Regular Kodi/Jellyfin use is effectively blocked.

---

## Modes

Three modes replace the current single-mode system:

### `TV_MODE`
Default. Classic TV owns the player. Watchdog active. Any stopped playback triggers
channel recovery.

### `BROWSE_MODE`
User is browsing Jellyfin or watching something they chose. Watchdog suspended.
Classic TV does not interfere with playback.

### `RESUME_PENDING`
Transitional. Browse-mode playback has ended. Classic TV is waiting a short grace
period before resuming the channel, in case the user is mid-navigation to another item.
If nothing starts within the grace window, Classic TV resumes.

---

## Triggers

### Entering BROWSE_MODE

**Trigger A — EPG button (manual):**
User presses the EPG/Guide button on the Sony RM-Y180. This is currently mapped to
`guide` but the guide isn't implemented. Repurpose it as the mode toggle.

- If in `TV_MODE`: stop current playback, suspend watchdog, enter `BROWSE_MODE`.
  Kodi returns to the home screen or the Jellyfin addon, whichever was last active.
- If in `BROWSE_MODE`: immediately resume Classic TV (transition to `TV_MODE`,
  fetch current slot, `PlayMedia`).

**Trigger B — `onPlayBackStarted` with unrecognised content (automatic):**
`ClassicTVPlayer` subclasses `xbmc.Player` and receives all player callbacks regardless
of which addon initiated playback. When `onPlayBackStarted` fires, Classic TV checks
whether the file matches what it last triggered via `PlayMedia`. If it doesn't match,
the user (or Jellyfin) started something independently — Classic TV enters `BROWSE_MODE`
automatically, without stopping the playback.

This handles the case where the user navigates to Jellyfin and plays a movie directly,
without pressing the EPG toggle first.

### Returning to TV_MODE

**From BROWSE_MODE via EPG button:**
Immediate. Stop current Jellyfin playback, fetch current channel slot, `PlayMedia`.

**From BROWSE_MODE via `onPlayBackEnded` / `onPlayBackStopped` (automatic):**
When browse-mode playback ends, enter `RESUME_PENDING`. Start a 5-second timer.
- If `onPlayBackStarted` fires within 5 seconds (user started another item): cancel
  timer, stay in `BROWSE_MODE`.
- If timer expires without new playback: resume Classic TV (`TV_MODE`).

This means: finish watching a movie → Classic TV automatically resumes within 5 seconds.
Navigate between episodes → Classic TV waits patiently.

---

## Pause handling

`onPlayBackPaused` while in `TV_MODE`: reset the watchdog grace period. Do not
treat a pause as a mode transition. The watchdog should not fire while paused.
Check `isPlaying()` AND `isPaused()` in the watchdog loop — skip recovery if paused.

---

## What doesn't change

- Watchdog interval (10s) — unchanged in `TV_MODE`
- Channel up/down, digit entry — only active in `TV_MODE` (buttons do nothing in
  `BROWSE_MODE` to avoid interrupting Jellyfin browsing)
- Startup sequence — always enters `TV_MODE`
- Server plugin, schedule engine, duration cache — unchanged

---

## Implementation touchpoints

| File | Change |
|---|---|
| `addon4/resources/lib/modes.py` | Add `BROWSE_MODE`, `RESUME_PENDING`; add pause-aware watchdog check |
| `addon4/resources/lib/player.py` | Add `onPlayBackStarted`, `onPlayBackEnded`, `onPlayBackStopped`, `onPlayBackPaused` callbacks; track `_last_play_url`; add `enter_browse_mode()`, `resume_tv_mode()` |
| `addon4/resources/lib/constants.py` | Add `RESUME_PENDING_GRACE_SECONDS = 5` |
| `addon4/service.py` | Wire EPG button to mode toggle (currently dispatches to `guide` — change dispatch to `player.toggle_mode()`) |
| `tests4/test_player.py` | Tests for automatic browse mode detection, resume pending timer, pause handling |
| `tests4/test_modes.py` | Tests for three-mode state machine |

---

## Open questions before implementation

1. **Should digit entry (direct channel number) work in BROWSE_MODE?**
   Current proposal: no — digit presses do nothing in BROWSE_MODE. Entering a digit
   could be treated as an implicit "go back to Classic TV on this channel" trigger, but
   that's more complex and can be added later.

2. **What should happen if the watchdog fires while in RESUME_PENDING?**
   Proposed: treat RESUME_PENDING as "not playing, about to play" — watchdog skips
   recovery during this window, same as the existing grace period.

3. **Is 5 seconds the right RESUME_PENDING window?**
   5 seconds is long enough that normal inter-episode navigation (Jellyfin loading the
   next item) won't prematurely resume Classic TV. Short enough that the CRT doesn't sit
   black for long after intentionally finishing something.

---

## Sony RM-Y180 remote mapping

The EPG button is already mapped in `userdata/keymaps/classictv.xml`:
```xml
<guide>NotifyAll(classictv.service, guide)</guide>
```

In `service.py`, `ClassicTVMonitor.onNotification` currently dispatches `guide` to
nothing (guide overlay not implemented). Change this dispatch to call
`player.toggle_mode()`.

No hardware changes needed. No new keymap entries.
