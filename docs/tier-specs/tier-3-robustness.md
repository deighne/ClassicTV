# Tier 3 Spec — Robustness and Reliability

**Status: 3.1 done in 4.5.0 (ChannelBug wired; ChannelStatic deleted; digit entry displays via ChannelBug), 3.2 done (injectable timing), 3.3 partly done (channel_entry tests added).**

Original status: NOT STARTED. Overlays remain unwired dead code (`ChannelStatic`/`ChannelBug` are
never instantiated and have no skin XML). Can be done in parallel with Tier 2, but the
`onPlayBackStarted` wiring it depends on overlaps with Tier 2.

**Dependencies:** None (independent of the mode state machine).

---

## 3.1 Wire or delete overlays

**Files:** `addon4/resources/lib/channel_static.py`, `addon4/resources/lib/channel_bug.py`,
`addon4/service.py`, `addon4/resources/lib/player.py`

**Current state:** Both classes are defined, wired onto `player` by `service.py`
(`player._channel_static`, `player._channel_bug`), but never called from `player.py`.
The channel-change path calls `self._channel_static.show()` but never calls
`on_playback_started()`. `_channel_bug` is never called at all.

**Decision required:** The architecture doc says overlays should eventually move to a
Kodi skin. For now, we have two options:

**Option A — Wire them properly for the current release:**
- Add `onPlayBackStarted()` to `ClassicTVPlayer` (already needed for Tier 2).
  Inside it, call `self._channel_static.on_playback_started()` and
  `self._channel_bug.show()` if both are set.
- `ChannelBug.show()` needs channel number, name, and show title — pass from the
  `ScheduleSlot` stored in `_current_slot` (new state, set in `play_slot()`).
- Skin XML files for `WindowXMLDialog` do not exist yet — these classes will not
  render visibly until skin XML is created. The safety timer and burn-in protection
  remain enforced in Python.
- **Consequence:** overlays work but are invisible until skin work is done.

**Option B — Delete Python overlay code; note it as deferred:**
- Remove `_channel_static` and `_channel_bug` from `service.py` wiring and
  `player.py` state.
- Add a comment in `player.py` and `classic-tv-4-architecture.md`: "Overlays are
  deferred to the skin layer per the 4.0 architecture decision."
- The CRT burn-in constraint is noted but unenforceable until skin work begins.

**Recommendation:** Option A — wire correctly. It enforces the safety timer now and
the code is already written. Skin XML can be a separate work item.

**Concrete changes for Option A:**

In `play_slot()`, store the slot:
```python
self._current_slot: ScheduleSlot | None = None
# In play_slot():
self._current_slot = slot
```

In `_change_channel()`, instantiate `ChannelBug` with current channel info before calling
`play_slot()` (we have the channel and slot data at that point):
```python
from channel_bug import create_channel_bug
ch = self._channel_list.get_channel_by_id(channel_id) if self._channel_list else None
if ch and slot:
    self._channel_bug = create_channel_bug(
        ch.number, ch.name, slot.title, "#ffffff"
    )
```

In `onPlayBackStarted()` (added in Tier 2):
```python
if self._channel_static is not None:
    try:
        self._channel_static.on_playback_started()
    except Exception as exc:
        xbmc.log(f"{_LOG_PREFIX} channel_static.on_playback_started() raised: {exc}", xbmc.LOGWARNING)
if self._channel_bug is not None:
    try:
        self._channel_bug.show()
    except Exception as exc:
        xbmc.log(f"{_LOG_PREFIX} channel_bug.show() raised: {exc}", xbmc.LOGWARNING)
```

**Tests required:**
- `tests4/test_channel_entry.py` already exists; add `test_channel_static.py` and
  `test_channel_bug.py` covering: `show()` arms timer, `close()` cancels timer,
  `on_playback_started()` cancels timer and closes, safety timeout fires if never dismissed.

---

## 3.2 Injectable timing for tests

**Files:** `addon4/resources/lib/modes.py`, `addon4/resources/lib/player.py`,
`addon4/resources/lib/constants.py`, `tests4/test_modes.py`

**Current problem:** `test_modes.py` has 9 tests that `time.sleep(WATCHDOG_INTERVAL_SECONDS + 0.5)` = ~10.5s each. Total suite time is ~57s. Adding Tier 2 timed tests will make this worse.

**Fix:** Make timing constants injectable via constructor parameters with defaults.

**`ModeState.__init__`:**
```python
def __init__(
    self,
    watchdog_interval: float = WATCHDOG_INTERVAL_SECONDS,
    resume_grace: float = RESUME_PENDING_GRACE_SECONDS,
) -> None:
    self._watchdog_interval = watchdog_interval
    self._resume_grace = resume_grace
```

Pass `self._watchdog_interval` to the watchdog loop instead of the global constant:
```python
while not modes._watchdog_stop.wait(modes._watchdog_interval):
```

**`ScheduleCacheWorker` already has this pattern** in the C# plugin — use the same
approach here.

**Tests:** All `test_modes.py` tests that create `ModeState()` should pass
`watchdog_interval=0.05, resume_grace=0.05`. Suite time drops from ~57s to ~2s.

---

## 3.3 Add tests for untested modules

**Currently untested:** `channel_entry.py`, `channel_static.py`, `channel_bug.py`,
`jellyfin_server.py`.

**`channel_entry.py` is the highest priority** — it has non-trivial state machine logic
with timer edges.

**`tests4/test_channel_entry.py`:**
- `test_first_digit_enters_accumulating`
- `test_second_digit_appends`
- `test_max_digits_silently_ignored`
- `test_confirm_resolves_channel`
- `test_back_cancels_and_stays_idle`
- `test_timeout_confirms`
- `test_timeout_resets_on_each_digit`
- `test_not_found_shows_then_clears`
- `test_is_accumulating_true_only_during_accumulation`
- `test_handle_other_while_accumulating_routes_event`

**`tests4/test_channel_static.py`:**
- `test_show_arms_safety_timer`
- `test_on_playback_started_cancels_timer_and_closes`
- `test_safety_timeout_closes_if_never_dismissed` (use injectable timeout)
- `test_close_cancels_timer`

**`tests4/test_channel_bug.py`:**
- `test_show_arms_dismiss_timer`
- `test_dismiss_timer_fires_and_closes` (use injectable timeout)
- `test_close_cancels_timer`
- `test_create_channel_bug_returns_none_on_failure`

**`tests4/test_jellyfin_server.py`:**
- `test_returns_server_id_from_valid_json` (mock `xbmcvfs.translatePath` + real file)
- `test_returns_empty_on_missing_file`
- `test_returns_empty_on_malformed_json`
