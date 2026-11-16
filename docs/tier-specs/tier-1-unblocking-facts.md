# Tier 1 Spec — Unblocking Facts & Hygiene

**Status: COMPLETE.**
- 1.1 callback verification done — results in `tier-1-callback-findings.md`. Callbacks fire;
  Tier 2 should use the callback-driven variant. The callbacks in `player.py` remain
  log-only stubs until Tier 2 replaces them.
- 1.2 offline-retry thread leak fixed — `_start_offline_retry()` is now a single cancellable
  worker.
- Also delivered: keymap fixed and validated (now valid XML with a `<global>` guide entry),
  schedule-client JSON type validation, deploy-script improvements.

**Estimated scope (original):** Small. No new features, no new tests beyond verification scaffolding.

---

## 1.1 On-device callback verification

**Goal:** Determine empirically whether `xbmc.Player` subclass instances receive
`onPlayBackStarted`, `onPlayBackEnded`, `onPlayBackStopped`, and `onPlayBackPaused`
callbacks for playback initiated by the Jellyfin addon (not Classic TV).

**Why it blocks everything:** The mode-switching design in Tier 2 has two very different
implementations depending on the answer. If callbacks fire reliably, `BROWSE_MODE`
auto-detection and `RESUME_PENDING` auto-resume are callback-driven. If they do not,
both must be implemented as watchdog polling of `isPlaying()` transitions.

**What to do:**

1. Add four callback methods to `ClassicTVPlayer` (`player.py`):

```python
def onPlayBackStarted(self) -> None:
    xbmc.log("[ClassicTV][player] CALLBACK: onPlayBackStarted", xbmc.LOGINFO)

def onPlayBackEnded(self) -> None:
    xbmc.log("[ClassicTV][player] CALLBACK: onPlayBackEnded", xbmc.LOGINFO)

def onPlayBackStopped(self) -> None:
    xbmc.log("[ClassicTV][player] CALLBACK: onPlayBackStopped", xbmc.LOGINFO)

def onPlayBackPaused(self) -> None:
    xbmc.log("[ClassicTV][player] CALLBACK: onPlayBackPaused", xbmc.LOGINFO)
```

2. Deploy to the Sweet Potato.

3. Exercise four scenarios and record which callbacks fire in `kodi.log`:
   - (a) Classic TV `play_slot()` starts a stream — expect `onPlayBackStarted`.
   - (b) Episode ends at natural EOF — expect `onPlayBackEnded`.
   - (c) User navigates to Jellyfin addon, plays a movie — expect `onPlayBackStarted`
         (with a URL that does NOT match Classic TV's last `play_slot()` item ID).
   - (d) User stops or exits Jellyfin playback — expect `onPlayBackStopped`.

4. Record results in `docs/tier-specs/tier-1-callback-findings.md`.

**Expected outcomes and downstream decisions:**

| Finding | Tier 2 design |
|---|---|
| All 4 callbacks fire in all 4 scenarios | Callback-driven mode detection and auto-resume |
| `onPlayBackStarted` fires but `Ended/Stopped` do not | Hybrid: detect entry via callback, detect exit via watchdog |
| No callbacks fire for Jellyfin-initiated playback | Full polling: watchdog detects `isPlaying()` transitions |

**Definition of done:** `docs/tier-specs/tier-1-callback-findings.md` exists with
observations from all 4 scenarios. `player.py` retains the log stubs until Tier 2
replaces them with real logic.

---

## 1.2 Fix offline-retry thread leak

**File:** `addon4/resources/lib/player.py`, lines 320–347 (`_offline_retry_loop`)

**Problem:** `watchdog_recover()` calls `_offline_retry_loop()` which spawns a new
`threading.Thread` that loops forever. If the schedule service is unreachable, the watchdog
fires every 10s and each firing spawns another forever-loop. These threads accumulate and
never terminate.

**Fix:** Replace `_offline_retry_loop` with a single cancellable retry worker. The worker
must be cancelled when:
- A new `play_slot()` is called (the channel was recovered by other means)
- A channel change is initiated
- The addon shuts down
- The mode changes to `BROWSE_MODE` (Tier 2)

**Concrete changes to `player.py`:**

```python
# New state in __init__:
self._retry_stop: threading.Event = threading.Event()
self._retry_stop.set()  # no retry running initially

# Replace _offline_retry_loop with:
def _start_offline_retry(self, channel_id: str) -> None:
    """Start a single cancellable offline retry worker. Cancels any existing one."""
    self._retry_stop.set()  # stop any existing worker
    self._retry_stop = threading.Event()
    stop = self._retry_stop

    def _loop() -> None:
        while not stop.wait(EOF_OFFLINE_RETRY_INTERVAL_SECONDS):
            slot = self._schedule_client.get_slot(channel_id) if self._schedule_client else None
            if slot and slot.jellyfin_item_id:
                xbmc.log(f"{_LOG_PREFIX} offline retry: service recovered channel={channel_id!r}", xbmc.LOGINFO)
                self.play_slot(slot)
                return
        xbmc.log(f"{_LOG_PREFIX} offline retry: cancelled channel={channel_id!r}", xbmc.LOGDEBUG)

    t = threading.Thread(target=_loop, daemon=True, name="classictv-offline-retry")
    t.start()

# Cancel in play_slot() — add after self._last_play_time = time.monotonic():
self._retry_stop.set()
```

**Tests required:**
- `test_player.py`: `test_offline_retry_cancelled_by_play_slot` — start retry, call
  `play_slot()`, verify the retry thread stops (check `_retry_stop.is_set()`).
- `test_player.py`: `test_only_one_retry_thread_active` — call `watchdog_recover()` twice
  while service returns `None`; verify only one retry thread is alive (thread name or
  `_retry_stop` identity).

---

## 1.3 Fix malformed keymap XML + add global EPG mapping

**File:** `addon4/keymaps/keyboard.xml`

**Problems:**
1. The file has a valid `<keymap>` block that closes at `</keymap>`, followed by a
   duplicate `<keyboard>` block outside any enclosing element. This is ignored by Kodi's
   lenient XML parser but would fail standard validation. Delete lines 68 onward (the
   orphaned trailing block).

2. All mappings are inside `<FullscreenVideo>`. The EPG button therefore cannot be
   pressed from the Jellyfin browse UI (a directory window). Without a global mapping,
   the user can enter `BROWSE_MODE` but cannot leave it with the remote.

**Fix:**

Move the `guide` (EPG) mapping to a `<global>` section. All other bindings stay in
`<FullscreenVideo>` — ch+/ch-/digits should not interfere with normal Kodi navigation.

```xml
<keymap>
  <global>
    <!-- EPG button: mode toggle, works from any window -->
    <remote>
      <guide>NotifyAll(classictv.service, guide)</guide>
    </remote>
  </global>

  <FullscreenVideo>
    <remote>
      <pageplus>NotifyAll(classictv.service, ch_up)</pageplus>
      <pageminus>NotifyAll(classictv.service, ch_down)</pageminus>
      <zero>NotifyAll(classictv.service, num_0)</zero>
      ... (rest unchanged)
    </remote>
    <keyboard>
      ... (keyboard fallbacks unchanged)
    </keyboard>
  </FullscreenVideo>
</keymap>
```

**Verification:** After deploying and reloading keymaps, confirm:
- Pressing EPG in fullscreen video → `[ClassicTV][service] onNotification method='guide'` in log.
- Pressing EPG from Jellyfin library view → same log line.
- Pressing ch+ from Jellyfin library → no log line (correctly silent).

**No new tests required** (keymap behavior is verified on-device, not unit-testable).

---

## 1.4 Fix ScheduleClient JSON type validation

**File:** `addon4/resources/lib/schedule.py`, lines 89–97 and 110–132

**Problem:** `_get_json()` returns whatever `json.loads()` produces — which could be a
list, string, number, or `None`. `get_channels()` then calls `data.get("Channels", [])` and
`get_slot()` calls `data.get("ChannelId", ...)`. If the server returns a JSON array or
string at the root level, these raise `AttributeError` which is not caught, crashing the
startup path.

**Fix:** Add type validation in `_get_json`:

```python
def _get_json(self, url: str, timeout: float) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
        data = json.loads(raw)
        if not isinstance(data, dict):
            xbmc.log(
                f"{_LOG_PREFIX} unexpected JSON type {type(data).__name__} from {url}",
                xbmc.LOGWARNING,
            )
            return None
        return data
    except ...
```

**Tests required:**
- `test_schedule_client.py`: `test_get_channels_returns_empty_on_json_array_response` —
  server returns `[1, 2, 3]`; `get_channels()` returns `[]` without raising.
- `test_schedule_client.py`: `test_get_slot_returns_none_on_json_string_response` —
  server returns `"ok"`; `get_slot()` returns `None` without raising.

---

## 1.5 Add git tags, VERSION file, and clean-tree check to deploy script

**Files:** `deploy/deploy-sweet-potato-4.sh`, `.git/`

**Problems:**
- No git remote configured, no tags — "what is running" is unanswerable from the device.
- Deploy script does not write a `VERSION` file to the device.
- Deploy script does not fail on a dirty working tree, contrary to `CLASSIC_TV_3_SPEC.md` §17.

**Fix to deploy script:**

```bash
# Fail on dirty working tree
if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: working tree is dirty — commit or stash changes before deploying"
    exit 1
fi

# Write VERSION after rsync
DEPLOY_VERSION="$(git describe --tags --always --dirty)"
echo "${DEPLOY_VERSION}" | ssh "${SSH_USER}@${SWEET_POTATO_IP}" \
    "cat > ${ADDON_DIR}/VERSION"
echo "✓ VERSION written (${DEPLOY_VERSION})"
```

**Tag the first release:**
```bash
git tag -a v4.0.0 -m "Classic TV 4.0.0 — initial release"
```

**Verification:** After deploy, `ssh root@192.168.1.237 'cat /storage/.kodi/addons/plugin.video.classictv/VERSION'` returns a meaningful version string.

**No new tests required** (deploy script behavior is verified by inspection).
