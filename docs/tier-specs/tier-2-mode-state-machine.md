# Tier 2 Spec — Mode State Machine

**Status: IMPLEMENTED in addon 4.5.0 (unit-tested; not yet verified on device).** See AGENTS.md §11 for deviations (browse-mode stop does not start the resume countdown; foreign-playback detection is conservative).

Original status: NOT STARTED — but unblocked.
Tier 1 is complete. `tier-1-callback-findings.md` confirms callbacks fire, so build the
**callback-driven** variant (Section A below), not the polling fallback. `modes.py` is still
single-mode (`TV_MODE` only) and `ClassicTVPlayer` still has log-only callbacks.

**Dependency:** Tier 1 complete; `docs/tier-specs/tier-1-callback-findings.md` exists.

This spec has two variants depending on Tier 1.1 findings. Section A is written for the
expected case (callbacks fire). Section B describes the fallback if they do not. Complete
Section A first; fall back to B only if findings require it.

---

## 2.0 Three-mode model

Replace the current single-mode system with three named modes:

```python
TV_MODE      = "tv"       # Classic TV owns the player; watchdog active
BROWSE_MODE  = "browse"   # User is browsing/watching something else; watchdog suspended
RESUME_PENDING = "resume" # Browse playback ended; brief grace before reclaiming
```

`TV_MODE` is the initial state on every startup. It is the only state in which the
watchdog calls `watchdog_recover()`, channel changes are processed, and digit entry
resolves to a channel.

---

## 2.1 Refactor `ModeState` as the single transition owner

**File:** `addon4/resources/lib/modes.py`

**Current problems:**
- `modes.current` is read by `player.py` (watchdog check), written by `player.py`
  (`modes.transition_to_tv()` inside `_change_channel()`), and the watchdog loop
  is a free function. State mutation is not guarded by a lock.
- Adding new modes means touching at least 3 files without a clear ownership boundary.

**Required changes:**

Add a `threading.RLock` to `ModeState`. All reads/writes of `current` must hold this lock.

Add a `RESUME_PENDING` state with an owned timer:

```python
self._resume_timer: threading.Timer | None = None
```

Expose a clean transition API. Remove direct `self.current = X` assignments from callers.

**New public interface for `ModeState`:**

```python
# Replaces transition_to_tv()
def enter_tv(self, channel_id: str) -> None: ...

# New
def enter_browse(self, reason: str = "") -> None: ...

# New — called when browse playback ends
def enter_resume_pending(self, channel_id: str) -> None: ...

# Called by ClassicTVPlayer.onPlayBackStarted
def on_playback_started(self, is_classictv_initiated: bool) -> None: ...

# Called by ClassicTVPlayer.onPlayBackEnded / onPlayBackStopped
def on_playback_ended(self) -> None: ...

# Already exists
def stop(self) -> None: ...

# Read-only; safe to call from any thread
@property
def current(self) -> str: ...
```

**`enter_tv(channel_id)`:**
- Acquires lock, cancels `_resume_timer`, sets `current = TV_MODE`, stores `current_channel`.
- Stops any existing watchdog, creates fresh `_watchdog_stop`, starts watchdog thread.

**`enter_browse(reason)`:**
- Acquires lock, cancels `_resume_timer`, sets `current = BROWSE_MODE`.
- Stops watchdog (`_watchdog_stop.set()`).
- Logs `[ClassicTV][modes] → BROWSE reason={reason}`.

**`enter_resume_pending(channel_id)`:**
- Acquires lock, asserts current is `BROWSE_MODE`, sets `current = RESUME_PENDING`.
- Starts `threading.Timer(RESUME_PENDING_GRACE_SECONDS, _on_resume_pending_timeout)`.
- Logs `[ClassicTV][modes] → RESUME_PENDING channel={channel_id}`.

**`_on_resume_pending_timeout()`:**
- Fires on the timer thread after `RESUME_PENDING_GRACE_SECONDS`.
- Acquires lock, checks `current == RESUME_PENDING` (it may have been cancelled).
- If still RESUME_PENDING: calls player's `startup_play(current_channel)` (injected
  callback or retrieved from `self.player`), then `enter_tv(current_channel)`.

**`on_playback_started(is_classictv_initiated)`:**
- If `is_classictv_initiated`: cancel `_resume_timer`, stay in current mode (no transition).
- If not `is_classictv_initiated` and current is `TV_MODE`: call `enter_browse("auto-detect")`.
- If current is `RESUME_PENDING`: cancel timer, call `enter_browse("new-playback")`.

**`on_playback_ended()`:**
- If current is `BROWSE_MODE`: call `enter_resume_pending(current_channel)`.
- Otherwise: no-op.

**Watchdog loop changes:**
- Gate on `current == TV_MODE` (currently `current != TV_MODE`).
- Also skip if `player.is_in_grace_period()` — already correct.
- Also skip if `player.isPlaying()` returns True (no change needed — already skipped).
- New: skip if `player.isPaused()` returns True (see 2.5).

**Add `RESUME_PENDING_GRACE_SECONDS = 5` to `constants.py` with this rationale:**
> After browse-mode playback ends, wait this long before reclaiming the player. Gives
> Kodi time to start a second episode if the user is mid-navigation. Must be less than
> WATCHDOG_INTERVAL_SECONDS (10s) so the watchdog doesn't also trigger recovery during
> this window — but the watchdog is suspended in BROWSE/RESUME_PENDING, so the constraint
> is actually PLAY_GRACE_SECONDS (15s) from the next play_slot() call.

---

## 2.2 Implement BROWSE_MODE entry from EPG button

**File:** `addon4/service.py`, `ClassicTVMonitor.onNotification()`

**Current state:** There is no `guide` branch. Unknown methods while accumulating route
to `channel_entry.handle_other()`. A `guide` press when not accumulating falls through
silently.

**Required change:** Add a `guide` dispatch branch that calls `player.toggle_mode()`.

```python
elif method == "guide":
    self._player.toggle_mode()
```

**Add `toggle_mode()` to `ClassicTVPlayer`:**

```python
def toggle_mode(self) -> None:
    """EPG button: toggle between TV_MODE and BROWSE_MODE."""
    if self._modes is None:
        return
    current = self._modes.current
    if current == TV_MODE:
        xbmc.log(f"{_LOG_PREFIX} toggle_mode: TV → BROWSE", xbmc.LOGINFO)
        self._modes.enter_browse(reason="epg")
        if self.isPlaying():
            self.stop()
        xbmc.executebuiltin("ActivateWindow(Home)")
    elif current in (BROWSE_MODE, RESUME_PENDING):
        xbmc.log(f"{_LOG_PREFIX} toggle_mode: BROWSE → TV", xbmc.LOGINFO)
        channel_id = self._modes.current_channel or self._current_channel_id
        self._modes.enter_tv(channel_id)
        if self._schedule_client and channel_id:
            slot = self._schedule_client.get_slot(
                channel_id, timeout=CHANNEL_CHANGE_FETCH_TIMEOUT_SECONDS
            )
            if slot and slot.jellyfin_item_id:
                self.play_slot(slot)
```

Note: `toggle_mode()` stops Classic TV playback before activating Home. It does NOT
stop any Jellyfin playback in progress — that is left to the user. When returning to TV
via toggle, the fetch is synchronous and on the notification thread. This is acceptable
for the toggle case (user explicitly asked); it is the same pattern as channel changes.

---

## 2.3 Implement BROWSE_MODE auto-detection from onPlayBackStarted

**File:** `addon4/resources/lib/player.py`

**Depends on:** Tier 1.1 confirming `onPlayBackStarted` fires for Jellyfin-initiated playback.

**Correlation key:** Not URL matching. Use the `jellyfin_item_id` from the last `play_slot()` call. The Jellyfin addon's window property `jellyfin_playing_id` (a Kodi window property set by the Jellyfin addon service) contains the currently playing item's Jellyfin ID.

**Add to `ClassicTVPlayer.__init__`:**
```python
self._last_played_item_id: str = ""
```

**In `play_slot()`, after building `play_url`:**
```python
self._last_played_item_id = slot.jellyfin_item_id
```

**Implement `onPlayBackStarted`:**
```python
def onPlayBackStarted(self) -> None:
    xbmc.log("[ClassicTV][player] CALLBACK: onPlayBackStarted", xbmc.LOGINFO)
    if self._modes is None:
        return
    # Determine if this is Classic TV's own playback or something foreign
    playing_id = xbmc.getInfoLabel("Window(home).Property(jellyfin_playing_id)")
    is_ours = (
        bool(self._last_played_item_id)
        and playing_id == self._last_played_item_id
    )
    xbmc.log(
        f"{_LOG_PREFIX} onPlayBackStarted:"
        f" playing_id={playing_id!r}"
        f" last_played={self._last_played_item_id!r}"
        f" is_ours={is_ours}",
        xbmc.LOGDEBUG,
    )
    self._modes.on_playback_started(is_classictv_initiated=is_ours)
```

**Fallback if `jellyfin_playing_id` is not populated:** Log a warning and treat as ours
(conservative — avoids spurious BROWSE_MODE entry on playback from our own `play_slot()`).

---

## 2.4 Implement RESUME_PENDING from onPlayBackEnded / onPlayBackStopped

**File:** `addon4/resources/lib/player.py`

**Depends on:** Tier 1.1 confirming these callbacks fire.

```python
def onPlayBackEnded(self) -> None:
    xbmc.log("[ClassicTV][player] CALLBACK: onPlayBackEnded", xbmc.LOGINFO)
    if self._modes is not None:
        self._modes.on_playback_ended()

def onPlayBackStopped(self) -> None:
    xbmc.log("[ClassicTV][player] CALLBACK: onPlayBackStopped", xbmc.LOGINFO)
    if self._modes is not None:
        self._modes.on_playback_ended()
```

Note: if Tier 1.1 finds these do NOT fire, the watchdog must detect the `BROWSE_MODE →
isPlaying() == False` transition instead. In that case, add a state check to the watchdog:

```python
# Fallback watchdog logic if callbacks don't fire:
if modes.current == BROWSE_MODE and not player.isPlaying():
    modes.enter_resume_pending(modes.current_channel)
```

---

## 2.5 Pause-aware watchdog

**File:** `addon4/resources/lib/modes.py`, `_watchdog_loop()`

**Add to watchdog loop:**
```python
player = getattr(modes, "player", None)
if player is None:
    continue
if player.is_in_grace_period():
    continue
if not player.isPlaying():
    # Check Kodi doesn't report False while paused on some builds
    try:
        if xbmc.getCondVisibility("Player.Paused"):
            continue
    except Exception:
        pass
    # Genuine stop — trigger recovery
    ...
```

**Add `onPlayBackPaused` and `onPlayBackResumed` to `ClassicTVPlayer`:**
```python
def onPlayBackPaused(self) -> None:
    xbmc.log("[ClassicTV][player] CALLBACK: onPlayBackPaused", xbmc.LOGINFO)

def onPlayBackResumed(self) -> None:
    xbmc.log("[ClassicTV][player] CALLBACK: onPlayBackResumed", xbmc.LOGINFO)
```

---

## 2.6 Gate input handlers on mode

**File:** `addon4/resources/lib/player.py`, `addon4/service.py`

`ch_up`, `ch_down`, digit entry — only act in `TV_MODE`:

```python
def handle_ch_up(self) -> None:
    if self._modes and self._modes.current != TV_MODE:
        return
    ...

def handle_ch_down(self) -> None:
    if self._modes and self._modes.current != TV_MODE:
        return
    ...
```

In `ClassicTVMonitor.onNotification()`, gate digit/confirm/back on mode:

```python
if method in ("num_0", ..., "num_9", "confirm", "back"):
    if self._player._modes and self._player._modes.current != TV_MODE:
        return  # silently ignore digits in browse mode
    ...
```

---

## 2.7 Tests required

**`tests4/test_modes.py` additions:**

- `test_enter_browse_suspends_watchdog` — `enter_browse()` sets `_watchdog_stop`.
- `test_enter_tv_from_browse_restarts_watchdog` — `enter_tv()` after `enter_browse()` starts a new watchdog.
- `test_resume_pending_fires_after_grace` — mock `player.startup_play`, call `enter_resume_pending()`, advance fake time past `RESUME_PENDING_GRACE_SECONDS`, verify `startup_play` called.
- `test_resume_pending_cancelled_by_new_playback` — `enter_resume_pending()` then `on_playback_started(is_classictv_initiated=False)`, verify timer cancelled and mode is `BROWSE_MODE`.
- `test_on_playback_ended_enters_resume_pending_from_browse` — `enter_browse()` then `on_playback_ended()`, verify mode is `RESUME_PENDING`.
- `test_on_playback_ended_noop_in_tv_mode` — `on_playback_ended()` while in `TV_MODE`, verify no state change.
- `test_watchdog_skips_while_paused` — player reports `isPaused() == True`, watchdog does not call `watchdog_recover()`.
- Transition table test: a 3×N matrix of (state, event) pairs asserting final state for every valid and invalid transition.

**`tests4/test_player.py` additions:**

- `test_toggle_mode_tv_to_browse` — mock `modes.enter_browse`, verify called when `toggle_mode()` in `TV_MODE`.
- `test_toggle_mode_browse_to_tv` — mock slot fetch and `play_slot`, verify called when `toggle_mode()` in `BROWSE_MODE`.
- `test_on_playback_started_ours_no_transition` — `_last_played_item_id` matches window property → `modes.on_playback_started(is_classictv_initiated=True)`.
- `test_on_playback_started_foreign_enters_browse` — IDs don't match → `modes.on_playback_started(is_classictv_initiated=False)`.
- `test_ch_up_noop_in_browse_mode` — `handle_ch_up()` while in `BROWSE_MODE` does not call `_change_channel()`.

**`tests4/conftest.py` additions:**

- Extend `_Player` stub with `isPaused() -> bool` (default `False`).
- Add `onPlayBackStarted`, `onPlayBackEnded`, `onPlayBackStopped`, `onPlayBackPaused`, `onPlayBackResumed` as callable stubs that tests can invoke directly.

**Fake clock / injectable timer:**

To avoid real sleeping in `test_modes.py`, make `RESUME_PENDING_GRACE_SECONDS` injectable:

```python
# In ModeState.__init__:
def __init__(self, resume_grace: float = RESUME_PENDING_GRACE_SECONDS) -> None:
    self._resume_grace = resume_grace
```

Tests pass `resume_grace=0.01` to test timer expiry in milliseconds.
Apply the same pattern to `WATCHDOG_INTERVAL_SECONDS` via an existing or new constructor
parameter — this eliminates the 10s+ real sleeps in `test_modes.py`.
