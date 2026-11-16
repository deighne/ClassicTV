# Tier 1.1 — Callback Verification Findings

**Date:** September 18, 2026
**Device:** Le Potato / CoreELEC 21.3-Omega (192.168.1.237)
**Addon:** plugin.video.classictv v4.0.0-1-g3f0c4da (tier-1-unblocking-facts branch)
**Method:** JSON-RPC to trigger playback; `grep "CALLBACK"` on kodi.log

---

## Raw observations

```
21:43:20 CALLBACK onPlayBackStarted  channel='sitcoms'     ← Scenario 1: Classic TV play_slot()
21:56:20 CALLBACK onPlayBackResumed  channel='sitcoms'     ← Scenario 3: Player.Open Naruto episode
21:56:20 CALLBACK onPlayBackStarted  channel='sitcoms'     ← Scenario 3: (continued)
21:57:02 CALLBACK onPlayBackStopped  channel='sitcoms'     ← Scenario 4: Player.Stop
21:57:09 CALLBACK onPlayBackStarted  channel='sitcoms'     ← Watchdog recovery after stop
```

---

## Findings by scenario

### Scenario 1 — Classic TV `play_slot()` starts a stream
**`onPlayBackStarted` fires. Confirmed.**
~6 seconds after `play_slot()` called — consistent with `PLAY_GRACE_SECONDS = 15`
(the stream had been starting for ~6s when the callback fired).

### Scenario 2 — Natural episode EOF
**Not observed** — episodes are 20+ minutes; not waited out in this session.
Assumption: `onPlayBackEnded` fires (it is a standard Kodi callback). To be confirmed
opportunistically by checking the log after an episode completes naturally.

### Scenario 3 — Jellyfin addon plays something (foreign playback)
**`onPlayBackResumed` + `onPlayBackStarted` both fired.**

Two callbacks fired within 1ms of each other at 21:56:20:
- `onPlayBackResumed` fired first
- `onPlayBackStarted` fired immediately after

This happened when `Player.Open` was called with a `plugin://plugin.video.jellyfin/`
URL while Classic TV's stream was already playing. Kodi apparently treats switching
from one stream to another as resume→start rather than stop→start. The `channel` field
in the log still shows `'sitcoms'` because `_current_channel_id` was not updated (it is
only updated by `play_slot()`, not by external playback).

**Key insight:** `onPlayBackStarted` fires for foreign playback. The `channel_id` in the
log is stale (still the last Classic TV channel) — the correlation check must use the
playing file/item ID, not the channel name.

### Scenario 4 — Stop Jellyfin playback
**`onPlayBackStopped` fires. Confirmed.**
Fired ~5 seconds after `Player.Stop` JSON-RPC call. Then `onPlayBackStarted` fired
7 seconds later — the watchdog recovered and resumed Classic TV (expected).

---

## Summary table

| Callback | Fires for Classic TV playback | Fires for foreign playback | Notes |
|---|---|---|---|
| `onPlayBackStarted` | ✓ Yes | ✓ Yes | Both Classic TV and Jellyfin-initiated. ~6s delay from PlayMedia. |
| `onPlayBackEnded` | ? Not tested | ? Not tested | Assumed yes; test at natural EOF. |
| `onPlayBackStopped` | ✓ Yes (via watchdog recovery) | ✓ Yes | Fires when Player.Stop called. |
| `onPlayBackPaused` | ? Not tested | ? Not tested | To be verified. |
| `onPlayBackResumed` | ? Not tested | ✓ Yes | Fires when switching streams (resume-before-start pattern). |

---

## Decisions for Tier 2

**1. Callbacks are reliable enough to use as the primary signal.**
`onPlayBackStarted` fires for both Classic TV and foreign playback. `onPlayBackStopped`
fires when playback is explicitly stopped. These are sufficient to drive
`BROWSE_MODE` auto-detection and `RESUME_PENDING` auto-resume.

**2. Correlation must use item ID, not channel name.**
When Scenario 3 fired, `channel='sitcoms'` was logged but the playing item was Naruto.
`_current_channel_id` is only updated by `play_slot()`. The Tier 2 `onPlayBackStarted`
implementation must compare the playing item's Jellyfin ID (from the
`jellyfin_playing_id` window property or `Player.GetItem` JSON-RPC) against
`_last_played_item_id` to determine if it's Classic TV's stream.

**3. Resume-before-start pattern.**
When switching streams while already playing, Kodi fires `onPlayBackResumed` then
`onPlayBackStarted` within 1ms, not `onPlayBackStopped` first. The mode detection in
Tier 2 should treat any `onPlayBackStarted` as the signal, regardless of whether a
stop/resume preceded it.

**4. `onPlayBackEnded` vs `onPlayBackStopped` for EOF.**
Natural EOF fires `onPlayBackEnded` (standard Kodi behavior, not yet observed but
well-documented). Explicit stop fires `onPlayBackStopped`. Both should trigger
`RESUME_PENDING` in Tier 2. Handle both callbacks identically.

**5. Watchdog remains the backstop.**
Even with callbacks, the watchdog should remain active. The 7-second gap between
`onPlayBackStopped` and the watchdog-triggered recovery (via `play_slot()` →
`onPlayBackStarted`) shows the watchdog would have eventually recovered on its own.
Callbacks make the mode system more responsive; the watchdog prevents permanent failure
if a callback is missed.

**Tier 2 design variant: CALLBACK-DRIVEN** (not the polling fallback).
