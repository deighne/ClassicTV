# Player Design Notes — Classic TV 3.0

## Status

**HISTORICAL / SUPERSEDED.** This describes the v3.0 `StartTimeTicks` + post-start
`seekTime()` approach. Classic TV 4.0 does none of this: it delegates playback to the
Jellyfin addon via `PlayMedia(plugin://plugin.video.jellyfin/...&timestamp=OFFSET)` and the
Jellyfin addon applies `StartOffset` synchronously. There is no seek machinery in the 4.0
addon. Kept for historical context only — see
[`seek-strategy-findings.md`](seek-strategy-findings.md) and [`../AGENTS.md`](../AGENTS.md).

---

## Seek Strategy Overview (Requirements 11.1–11.6)

The Player addon must begin every episode at the exact broadcast position calculated
by the Schedule_Service (`seek_offset` field in the schedule response). The accepted
tolerance is ±2 seconds, achieved within 3 seconds of the Kodi `open()` call
(Requirements 11.1, 11.2).

### The Core Problem

Jellyfin's `Static=true` direct-play endpoint accepts a `StartTimeTicks` query
parameter (`ticks = seconds * 10,000,000`). If Kodi/Jellyfin honors it, the stream
starts at the requested position with zero seek latency. If the parameter is silently
ignored (which can happen depending on Kodi version, Jellyfin version, and codec), the
stream starts from the beginning and a post-start seek is needed — adding visible
latency and Amlogic decoder risk.

We do not know empirically which behavior occurs on the target hardware. Hardware
testing resolves this.

### Strategy A: StartTimeTicks (Requirement 11.3)

Embed the seek offset in the direct-play URL:

```
/Videos/{id}/stream?Static=true&api_key=...&StartTimeTicks={offset * 10_000_000}
```

**If this works:** zero-latency seek, no decoder risk, no retry logic needed in the
hot path. The fallback logic (Strategy B) still runs as a safety check after
`onPlayBackStarted` but will be a no-op in practice.

**If this does not work:** `getTime()` after `onPlayBackStarted` will be near 0
regardless of the requested offset, and Strategy B takes over.

### Strategy B: Post-Start seekTime() (Requirement 11.4)

After `onPlayBackStarted` fires, issue `player.seekTime(seek_offset)`. Retry up to 3
times at 500ms intervals. If the position is still outside ±2s after all retries, log
an ERROR and continue from the last achieved position (Requirement 11.5).

```python
def _seek_with_retry(self, target: float, attempts: int = 0):
    if attempts >= SEEK_MAX_RETRIES:   # 3
        log ERROR achieved=player.getTime() target=target
        return
    player.seekTime(target)
    xbmc.sleep(SEEK_RETRY_INTERVAL_MS)  # 500ms
    if abs(player.getTime() - target) > SEEK_TOLERANCE_SECONDS:  # 2.0
        self._seek_with_retry(target, attempts + 1)
```

**Risk:** The `onPlayBackStarted` callback on the Amlogic decoder may fire before the
decoder is fully initialized. A `xbmc.sleep(300)` grace period before the first
`seekTime()` call may be necessary and will add ~300ms latency to every channel
change. Hardware testing will determine whether the grace period is needed.

### Recommended Two-Stage Implementation

Implement both strategies regardless of hardware test outcome. Stage 1 always
attempts `StartTimeTicks`. Stage 2 always checks the achieved position after
`onPlayBackStarted` and seeks if needed. This is the safest design:

- If Strategy A works on real hardware, Stage 2 is a no-op check that costs ~50ms.
- If Strategy A does not work, Stage 2 corrects the position transparently.

```
player.play(url_with_start_ticks)
  └─ onPlayBackStarted fires
       └─ check getTime() once (after optional grace period)
            ├─ within ±2s → log "strategy=StartTimeTicks, OK"
            └─ outside ±2s → _seek_with_retry(target)
                  └─ up to 3 retries × 500ms
                       ├─ within ±2s → log "strategy=PostStartSeek, OK"
                       └─ exhausted → log ERROR, continue
```

All starts are logged with strategy, target, and achieved position (Requirement 11.6).

---

## Constants (pending hardware validation)

| Constant | Current Value | Rationale |
|---|---|---|
| `SEEK_TOLERANCE_SECONDS` | `2.0` | Requirement 11.1 |
| `SEEK_MAX_RETRIES` | `3` | Requirement 11.4 |
| `SEEK_RETRY_INTERVAL_MS` | `500` | Requirement 11.4 |
| `SEEK_GRACE_PERIOD_MS` | `0` (TBD) | May need ~300ms on Amlogic; hardware test will confirm |

`SEEK_GRACE_PERIOD_MS` is the only constant whose value depends on hardware results.
If the Amlogic decoder fires `onPlayBackStarted` before it can accept `seekTime()`,
increase this to 300ms. If seeks are reliable at 0ms, leave it at 0.

---

## Strategy C: HLS Transcode (Ruled Out)

Jellyfin's HLS transcode endpoint (`/Videos/{id}/master.m3u8?StartTimeTicks=...`)
handles seek offsets more reliably than direct play, but has an 8–15 second startup
delay and has caused decoder hangs on Amlogic hardware in the field. Not used as the
primary strategy. May be reconsidered only if both Strategy A and Strategy B prove
unreliable on the target hardware.

---

## Decision Log

| Date | Decision | Rationale |
|---|---|---|
| Pre-hardware-test | Implement two-stage A+B | Defensive; correct regardless of whether StartTimeTicks is honored |
| Pre-hardware-test | Rule out HLS transcode | Latency and Amlogic stability concerns outweigh seek reliability benefit |
| TBD (post-test) | Confirm SEEK_GRACE_PERIOD_MS | Depends on Amlogic decoder initialization timing |
| TBD (post-test) | Confirm chosen primary strategy | Depends on seek-strategy-findings.md results |

---

## Related Documents

- `docs/seek-strategy-findings.md` — Hardware test results (pending)
- `tests/seek_validation.py` — Validation script used to produce findings
- `.kiro/specs/classic-tv-3/requirements.md` — Requirements 11.1–11.6
- `.kiro/specs/classic-tv-3/design.md` — Full seek strategy section
