# Classic TV 4.0 — Mode Switching Spec Review & v4 Improvements

**Date:** September 18, 2026
**Reviewed document:** `docs/classic-tv-mode-switching-spec.md` (commit `9e663f7`)
**Method:** Read the spec against the actual `addon4` source, `tests4`, deploy scripts,
the v3/v4 architecture docs, and the live device + server state (Le Potato at
`192.168.1.237`, jfmac at `192.168.1.207`, Jellyfin `ClassicTV` plugin `10.11.0.6`).
**Status:** Review — recommended refinements and v4 improvements. No code changed.
**Follow-up (2026-09-20):** Several recommendations here have since landed independently:
the keymap was fixed/validated and a `<global>` guide entry added, the offline-retry thread
leak was fixed, and schedule-client JSON validation was added. The mode state machine itself
is still unimplemented and is now tracked as
[`tier-specs/tier-2-mode-state-machine.md`](tier-specs/tier-2-mode-state-machine.md). One
finding here (that `onPlayBackEnded` was assumed not to fire) was disproven on device —
callbacks do fire; see [`tier-specs/tier-1-callback-findings.md`](tier-specs/tier-1-callback-findings.md).

---

## 1. Executive summary

The mode-switching spec targets the single most important UX defect in the shipped 4.0
addon: there is no way for the user to leave Classic TV, so the watchdog fights them for
control of the player. The three-mode model (`TV_MODE` / `BROWSE_MODE` / `RESUME_PENDING`)
is the right shape and the EPG-toggle + auto-detect strategy is sound.

However, the spec is written against an **imagined codebase**, not the one that is
actually deployed. Several load-bearing assumptions are false or unverified:

1. The spec builds automatic detection on `onPlayBackStarted` matching a stored
   `_last_play_url`, but the current code stores no such URL, and the URL it *would*
   store (`plugin://…`) is never the string Kodi reports as the playing file.
2. `player.py` explicitly documents that `onPlayBackEnded`/`onPlayBackStopped` **do not
   fire** — while the spec makes them central to auto-resume. One of these is wrong.
3. The keymap only maps buttons inside `<FullscreenVideo>`, so the EPG toggle cannot be
   pressed from the Jellyfin/browse UI — exactly where the user needs it.
4. The channel bug and channel static overlays are **dead code** — never instantiated,
   no skin XML exists — yet the channel-change path and the spec both assume they work.
5. Live infrastructure is half-migrated: the v3 Python schedule service is still running
   under launchd on jfmac, there are no git tags, and the device runs a stale 2-channel
   list even though the server now serves 18 channels.

This document does two things: **(A)** tightens the spec into something implementable and
testable against the real code, and **(B)** lists broader v4 code-quality and
architectural improvements that should be folded into the same effort or scheduled
immediately after.

---

## 2. What is actually running live right now

This is the ground truth to design against. Measurements taken 2026-09-18.

### 2.1 Le Potato (Sweet Potato) — the player

- Reachable at `192.168.1.237`, CoreELEC 21.x.
- Addon deployed at `/storage/.kodi/addons/plugin.video.classictv/`, no `VERSION` file.
- **Deployed files are byte-identical to the current repo `HEAD` (`9e663f7`)** for
  `service.py`, `player.py`, `modes.py`, `constants.py`, and `keymaps/keyboard.xml`
  (verified by md5). So "what is running" == current source.
- Kodi has been up since **2026-09-16 23:33**. Consequence: the in-memory `ChannelList`
  was loaded when the server exposed **2 channels** (`sitcoms`, `anime`); the server now
  exposes **18**. Channel up/down on the live device still cycles only 2 channels until
  Kodi restarts. This is an operational finding, not a code bug, but it must be accounted
  for when testing mode changes.
- Observed behavior in `kodi.log`: the watchdog is the only thing driving playback;
  `onPlayBack*` callbacks are never logged (none are implemented). Channel up/down work
  and dispatch in ~0.3–0.5 s.
- Keymap `classictv.xml` loads despite being **malformed XML** (see §3.2.6). Kodi's
  TinyXML parser tolerates trailing junk after the root element.
- The addon was built with only **two active channels** in testing, so digit entry, guide
  toggle, and back/confirm have effectively never been exercised on hardware.

### 2.2 jfmac — the server

- Jellyfin up; `ClassicTV` plugin **10.11.0.6** installed at
  `~/Library/Application Support/jellyfin/plugins/ClassicTV_10.11.0.6/`.
- `GET /ClassicTV/channels` returns 18 channels (IDs, not numbers).
- `GET /ClassicTV/channel/{id}` returns a resolved slot, e.g. `sitcoms` →
  `StartOffset ≈ 179.7`, `Duration ≈ 1496.5`, plus `NextItem`. Auth is anonymous.
- **The v3 Python schedule service is still running**: `launchctl` shows
  `com.classictv.schedule_service`, and `localhost:8300/health` returns
  `{"status": "ok", "version": "3.0.0"}`. The v4 architecture doc's Phase 3
  "decommission `schedule_service.py`" has not happened.
- The repo checkout on jfmac (`~/projects/ClassicTV3.0`) is at `cce5b15` (Sep 13) and its
  `VERSION` file contains a **commit SHA, not a release tag** — there are no tags at all.

### 2.3 Repository / release hygiene

- `git tag -l` → empty. `git remote -v` → empty. There is no remote and no tagged release.
- `deploy/deploy-sweet-potato-4.sh` syncs files and the keymap but **does not write a
  `VERSION` file and does not check for a clean working tree**, contradicting
  `CLASSIC_TV_3_SPEC.md` §17 ("Tag every deploy", "VERSION file on each device",
  "Never deploy uncommitted code").
- The C# server plugin source is **not in this repository** (`web/plugins/` holds only
  built zips + `manifest.json`). Schedule logic and client code are versioned separately,
  which is the root of many "which build is live?" problems.

### 2.4 Test suite

- `python -m pytest tests4/ -q` → **59 passed in ~57 s**. Passing is healthy; the
  duration is almost entirely real `time.sleep(WATCHDOG_INTERVAL_SECONDS + 0.5)` calls in
  `tests4/test_modes.py`.

---

## 3. Spec review

### 3.1 What the spec gets right

- **Problem framing.** The watchdog truly is a hostile actor against intentional browsing;
  the fix is a mode gate, not a tweak.
- **Three states are the minimum viable set.** `BROWSE_MODE` (user owns playback) and
  `RESUME_PENDING` (short grace before reclaiming) cleanly separate "don't interrupt" from
  "about to reclaim".
- **EPG as the mode toggle** is a good reuse of an unused physical button.
- **Pause handling** belongs in the watchdog; treating pause as a mode transition would be
  wrong.
- **"What doesn't change"** (watchdog interval, channel nav only in TV_MODE, startup
  always TV_MODE) is the correct conservative scope.

### 3.2 Factual errors and contradictions (must fix before implementation)

**3.2.1 `onPlayBackEnded` / `onPlayBackStopped` — the spec and the code disagree.**
`player.py:15-18` states the callbacks do not fire "because Classic TV no longer owns the
player". But `ClassicTVPlayer` subclasses `xbmc.Player` (`player.py:47`), and Kodi delivers
player callbacks to live `xbmc.Player` subclass instances. The spec's entire auto-resume
path (`onPlayBackEnded` → `RESUME_PENDING`) depends on them firing. **Resolve empirically
first**: add a temporary log to `onPlayBackStarted/Ended/Stopped/Paused` and confirm on
the device. If they fire, delete the misleading docstring; if they somehow do not, the
spec needs a different trigger (polling `isPlaying()` transition).

**3.2.2 Matching `_last_play_url` is the wrong correlation key.**
The spec says: "checks whether the file matches what it last triggered via `PlayMedia`."
Classic TV calls `PlayMedia(plugin://plugin.video.jellyfin/…)` (`player.py:108-126`). The
Jellyfin addon then calls `setResolvedUrl()` with a **direct stream URL**
(`http://192.168.1.207:8096/Videos/{id}/stream…`). `Player.getPlayingFile()` therefore
returns the stream URL, never the `plugin://` URL. `_last_play_url` — which does not exist
today anyway — would never match and every Classic-TV-initiated play would be misread as
foreign playback, immediately dumping the user into `BROWSE_MODE`. **Use the resolved
`jellyfin_item_id`**: track the item ID last requested, and compare it against the item ID
embedded in the playing file / reported by the Jellyfin addon's window property.

**3.2.3 The spec's touchpoint list does not match the code.**
The table proposes `player.py` "track `_last_play_url`; add `enter_browse_mode()`,
`resume_tv_mode()`" and `service.py` "change dispatch to `player.toggle_mode()`". In the
real code:
- `modes.py` owns the only mode value and the watchdog lifecycle; mode API should live
  there, not on the player.
- `service.py` has **no `guide` branch at all** (`service.py:81-98`); unknown methods fall
  through to `channel_entry.handle_other()` only while accumulating. So "currently
  dispatches `guide` to nothing" is approximately true but the fix is to add a branch, not
  to change an existing one.
- The keymap file is `addon4/keymaps/keyboard.xml` deployed to
  `special://userdata/keymaps/classictv.xml`; the spec calls it `userdata/keymaps/…` and
  omits it from the touchpoints.

**3.2.4 `PlayMedia` vs `RunPlugin` inconsistency.**
The spec problem statement correctly says `PlayMedia`. `player.py`'s module docstring and
`docs/classic-tv-4-architecture.md` Q1 still say `RunPlugin`. Git history
(`8b6d30e`) shows `PlayMedia` was adopted deliberately. Documentation must be reconciled so
future work does not "fix" it back.

**3.2.5 `server_id` is dead code.** `service.py:168-191` reads the Jellyfin server UUID and
assigns `player._jellyfin_server_id`, but `play_slot()` deliberately omits `server=`
(`player.py:102-113`, confirmed by test `test_play_slot_calls_executebuiltin_with_correct_url`).
Either remove the discovery code or document why it is retained.

**3.2.6 The keymap is malformed XML.**
`addon4/keymaps/keyboard.xml` closes its root `</keymap>` at line 67, then appends a second
copy of channel-navigation and keyboard entries at lines 69-110 with no enclosing element.
`python3 -m xml.dom.minidom` rejects it: *"junk after document element: line 71"*. Kodi
happens to load it because TinyXML is lenient, but this is fragile across Kodi versions and
tooling. Delete the trailing block.

### 3.3 Design gaps

**3.3.1 Keymap scope makes the EPG toggle unusable in browse mode.**
All mappings live inside `<FullscreenVideo>` (`keyboard.xml:21-66`). When the user is
browsing the Jellyfin library (a directory window, no fullscreen video), a `<guide>` press
is unmapped and never reaches `onNotification`. The user could therefore enter
`BROWSE_MODE` but not leave it with the remote. The spec does not address this. Mode
quality-of-life also benefits from wider mapping (e.g. digit entry to return to TV).

**3.3.2 No single owner for mode transitions.**
Today, `mode` changes are scattered: `service.py:210` at startup,
`player._change_channel` at `player.py:214`, and implicitly the watchdog at
`modes.py:26-49`. The spec adds `service.py` dispatch and `player.py` methods. This
guarantees state leakage. A mode should be mutated in exactly one place.

**3.3.3 RESUME_PENDING timer is underspecified and racy.**
The spec proposes a 5 s timer set on `onPlayBackEnded/Stopped` and cancelled by
`onPlayBackStarted`. It does not say:
- which object owns the timer (a `threading.Timer`? a watchdog-loop deadline?),
- what protects `current` from concurrent mutation by the Kodi callback thread and the
  watchdog thread (there is no lock anywhere in `modes.py`),
- what happens if a pause, a channel change, or an EPG press arrives during the window,
- how it interacts with `PLAY_GRACE_SECONDS = 15` (`constants.py:78`), which is longer
  than the proposed 5 s window.

**3.3.4 Watchdog fairness during RESUME_PENDING is not defined.**
`modes.py:29` gates recovery only on `current != TV_MODE`, but `current` is hard-coded
`"tv"` today. Once browse states exist, the guard must become "recover only in TV_MODE",
and the grace/offline-retry paths must not stack.

**3.3.5 Watchdog recovery has an unbounded thread leak (existing 4.0 bug).**
`player.watchdog_recover()` retries 3× then calls `_offline_retry_loop()` (`player.py:320`),
which spawns a **new daemon thread that loops forever**, calling `play_slot()` on success.
While the schedule service is down, the watchdog fires every 10 s and each firing spawns
another forever-loop because `play_slot()` is never called (so no grace is set). The result
is a growing pile of retry threads. This directly undermines any mode state machine and
should be fixed as part of this work.

**3.3.6 `onNotification` does blocking work.**
`handle_ch_up/down` → `_change_channel` blocks up to ~2 s polling decoder release plus up
to 3 s on a network fetch (`player.py:164-236`), all on Kodi's notification dispatch
thread. The v3 spec required dispatch "within 1 ms, measured by the absence of any blocking
I/O or sleep" (`requirements.md` Req 16). The mode toggle should not repeat this pattern.

**3.3.7 Digit entry semantics are built on sand.**
`ChannelList` assigns `number = i + 1` by server order (`channels.py:51-54`); there is no
real channel number. The spec's open question 1 ("should digit entry work in browse mode?")
is really a question about whether the system has stable channel numbers at all. If channel
numbers matter, the server should own them; positional numbering shifts whenever a channel
is inserted.

**3.3.8 No mode-state test strategy.**
`tests4/conftest.py` stubs `xbmc.Player` but provides no way to fire `onPlayBack*`
callbacks, and `test_modes.py` uses real sleeping. The spec asks for a "three-mode state
machine" test file but not for the mechanics (fake clock, callback injection) that make it
fast and deterministic.

---

## 4. Refinements to the spec (concrete)

### R1 — Establish callback behavior on-device first (blocking gate)

Add temporary logging to `ClassicTVPlayer.onPlayBackStarted/Ended/Stopped/Paused`, deploy,
and record which fire when (a) Classic TV starts a stream, (b) the episode ends naturally,
(c) the user starts a Jellyfin movie, (d) the Jellyfin addon is stopped by the user. The
rest of the mode design (auto-detect, auto-resume) hinges on this. If callbacks are
reliable, use them as the primary signal and keep the watchdog as backstop. If not, the
spec must be rewritten around `isPlaying()` transitions.

### R2 — Correlate playback by item ID, not URL

Define a `PlaybackSession` record owned by the player/mode controller:

```
{ channel_id, jellyfin_item_id, requested_at_monotonic, play_offset }
```

On `onPlayBackStarted`, resolve the playing item ID (from `getPlayingFile()`, or better,
the Jellyfin addon's window property / JSON-RPC `Player.GetItem`) and compare it to the
active session's ID. Match → TV playback confirmed. No match → foreign playback →
`BROWSE_MODE`. Replace the spec's `_last_play_url` language entirely.

### R3 — Give `modes.py` sole ownership of state and transitions

Replace ad-hoc `transition_to_tv()` with an explicit transition API, e.g.:

```
ModeState.enter_tv(channel_id)
ModeState.enter_browse(reason)
ModeState.enter_resume_pending(channel_id)
ModeState.on_playback_started(item_id)
ModeState.on_playback_ended()
```

`player.py` and `service.py` should call these; neither should assign `current` directly.
Guard all state reads/writes with a single `threading.RLock`.

### R4 — Gate every input handler on mode

Add explicit mode checks:
- `ch_up` / `ch_down`: only in `TV_MODE` (spec already says this — make it real).
- digits / `confirm` / `back`: define per mode; see R9.
- `guide`: always handled; behavior depends on mode.
- watchdog recovery: only in `TV_MODE`.

This makes the mode the **single gate** for "does Classic TV act on the player", replacing
scattered `PLAY_GRACE_SECONDS` reasoning.

### R5 — Fix the keymap so the toggle works from anywhere

- Remove the malformed trailing block (`keyboard.xml:69-110`).
- Move the `guide` (EPG) mapping to a `<global>` section so it fires from the home screen,
  Jellyfin directory windows, and fullscreen video alike.
- Keep `ch_up`/`ch_down`/digits in `<FullscreenVideo>` so they do not interfere with normal
  Kodi navigation while browsing, unless R9 adds "digit returns to TV".

A `<global>` mapping is the difference between a mode you can enter and one you can escape.

### R6 — Define BROWSE entry/exit precisely

- **Enter (EPG, in TV_MODE):** set `BROWSE_MODE`, stop Classic TV playback via a
  non-blocking command (do not block the notification thread), then activate a window.
  Kodi cannot reliably return to "whichever was last active"; specify a concrete builtin
  (`ActivateWindow(Home)` or the Jellyfin plugin directory) and log the choice.
- **Exit (EPG, in BROWSE_MODE):** set `TV_MODE`, cancel any pending resume timer, fetch
  current slot, `play_slot()`.
- **Enter (auto, unrecognized playback):** set `BROWSE_MODE` without stopping playback.
- **Exit (auto, playback ended):** enter `RESUME_PENDING`.

### R7 — Make RESUME_PENDING a first-class, single-owner timer

- Own the deadline inside `ModeState` (a `threading.Timer` with a stored handle, or a
  watchdog deadline checked each tick). Do not let `player.py` create timers.
- Cancel on: `onPlayBackStarted`, `onPlayBackPaused`, EPG press, channel change, shutdown.
- On expiry: re-check that playback is genuinely stopped, then `enter_tv`.
- Raise or explicitly justify the 5 s window given `PLAY_GRACE_SECONDS = 15`. If the
  watchdog grace is 15 s, a 5 s resume window will race the watchdog's own gate; derive
  both from one documented model.

### R8 — Pause semantics after direct observation

The spec asks to check `isPlaying()` **and** `isPaused()`. Kodi's `isPlaying()` commonly
returns `True` while paused, but CoreELEC/Amlogic behavior should be confirmed. Either way,
document the observed behavior and make the watchdog skip recovery when paused. Do not
infer it.

### R9 — Decide digit-entry behavior with stable channel numbers

Recommended: entering a digit while in `BROWSE_MODE` explicitly returns to TV on that
channel — it is the fastest "get me out" gesture and matches real TV UX. **But only if**
channel numbers are server-authoritative. Add `number` to the server's channel response
instead of deriving `i + 1`. If that is out of scope now, keep "digits do nothing in
BROWSE_MODE" but state that positional numbering is a known limitation.

### R10 — Restate the watchdog contract

The watchdog should:
1. Do nothing unless mode is `TV_MODE`.
2. Skip while `is_in_grace_period()` or paused.
3. Never spawn unbounded retry threads (see §5.B).
4. Be the backstop, not the primary EOF signal, once R1 confirms callbacks.

### R11 — Startup and recovery invariants

- Always enter `TV_MODE` on service start (spec already says this).
- If the schedule fetch fails at startup, do **not** leave the system in an ambiguous mode;
  stay `TV_MODE` with the existing retry, and surface the failure dialog.
- On Kodi/addon shutdown, cancel all timers and stop the watchdog once, idempotently.

### R12 — Test plan for the state machine

- A transition table test covering every (state, event) pair, including no-ops.
- Callback injection: extend the `xbmc.Player` stub in `tests4/conftest.py` to let tests
  invoke `onPlayBackStarted/Ended/Stopped/Paused`.
- A fake clock / injectable sleeper so `RESUME_PENDING` and watchdog tests run in
  milliseconds, not 10 s each. This alone should cut suite time from ~57 s to a few seconds.
- Explicit regression test for the offline-retry thread-leak scenario.

---

## 5. Broader v4 code quality & architectural improvements

These are improvements that either block a clean mode implementation or materially raise
v4's reliability. Ordered by leverage.

### A. State and ownership

- **Introduce one `ModeController` (or make `ModeState` it) as the only owner of mode.**
  Everything else is an adapter that emits events. This removes the current scatter across
  `service.py`, `player.py`, and `modes.py`, and is the single most important structural
  change.
- **Make collaborators explicit, not private-attribute injection.** `service.py:187-191`
  sets `player._schedule_client`, `player._channel_list`, `player._modes`,
  `player._channel_static`, and `modes.player = player`. Replace with constructor
  injection or a small `Dependencies`/`Runtime` container. This is what makes the mode
  controller unit-testable without a running Kodi.
- **Define a playback event vocabulary** (`PlaybackStarted(item_id)`,
  `PlaybackEnded(reason)`, `PlaybackPaused`, `PlaybackStopped`) so the player, watchdog,
  and overlays all react to the same signal instead of each polling independently.

### B. Concurrency and lifecycle (fix existing defects)

- **Fix the offline-retry thread leak** (`player.py:320-347`). Replace with a single
  cancellable retry worker owned by the controller; ensure only one is ever alive; cancel
  it on mode change / channel change / shutdown.
- **Stop blocking Kodi's notification thread.** Route `ch_up`/`ch_down`/`toggle_mode` into
  a single-slot command queue drained by a worker thread. `onNotification` should set an
  intent and return immediately, satisfying the original 1 ms dispatch requirement.
- **Make watchdog generations race-free.** `modes.py:90-101` creates a new `Event` per
  transition while the old thread still references the old event; rapid transitions can
  briefly run two loops. Use a single long-lived watchdog thread that reads
  `current`/`stop_event` each tick, or join the old thread before starting a new one.
- **One shutdown path.** Today `modes.stop()` is called in two places
  (`service.py:234`, `service.py:255`); make shutdown idempotent and cancel all timers and
  worker threads from one place.

### C. Robustness of the schedule client (real bug)

- `ScheduleClient._get_json` returns `json.loads(...)`, which can be a **list, string, or
  number**. `get_channels` then calls `data.get("Channels", [])`
  (`schedule.py:91`) and `get_slot` calls `data.get(...)` (`schedule.py:111`), neither of
  which catches `AttributeError`. A malformed-but-valid JSON response crashes the startup
  path. Validate `isinstance(data, dict)` in `_get_json` and log a warning.
- **Cap response size** before `json.loads` (the scheduler endpoint is small by contract;
  a hostile/broken server should not be able to exhaust the Le Potato's RAM).
- **URL-encode** channel IDs in path construction.
- Consider a persistent `urllib` opener / connection reuse; three sequential fetches on a
  channel change should not each pay TCP setup.

### D. Overlay reality

- Currently `ChannelStatic` and `ChannelBug` are defined but **never instantiated**
  (`service.py` never assigns `player._channel_static`/`_channel_bug`), no skin XML files
  exist, and `onPlayBackStarted` never calls `channel_static.on_playback_started()`. The
  channel-change path silently no-ops on overlays.
- Either (a) wire them properly — instantiate, add the `WindowXMLDialog` XML assets,
  dismiss static from `onPlayBackStarted`, show the bug after tune — or (b) follow
  `classic-tv-4-architecture.md` and delete the Python overlays in favor of skin
  infolabels. Do not ship dead code that the changelog and architecture claim works.
- The CRT burn-in constraint (`CHANNEL_BUG_DISPLAY_SECONDS = 6`) is safety-critical; if
  overlays move to skin XML, that timeout must remain enforced in one place.

### E. Configuration and constants

- `PLAY_GRACE_SECONDS = 15` is large and undocumented relative to actual bring-up time.
  Measure the real time from `play_slot()` to first frame and derive the value with a
  comment; make it injectable so tests do not sleep.
- `settings.xml` exposes `default_channel` as free text with a blank default; settings
  validation would prevent a typo from silently falling back to channel index 0.
- Consider a `settings` migration/version field so future mode-related preferences can be
  added cleanly.

### F. Observability

- Log a single structured line on every mode transition: `mode old=TV new=BROWSE
  reason=epg channel=anime`.
- Expose current mode (and channel, item) as a Kodi window property so a skin or web
  dashboard can display it and so tests/tools can read it.
- The IR logger (`service.py:105-139`) is permanent debug scaffolding; gate it behind a
  setting or remove it. It reads a hard-coded `/dev/input/event2`, which will not be valid
  on every boot.

### G. Deployment and release hygiene

- **Create tags and a remote.** `git tag -a v4.0.0` and a real origin. "What is running"
  is currently unanswerable.
- **Make `deploy-sweet-potato-4.sh` write `VERSION`** and refuse to run on a dirty tree
  (`git status --porcelain`), per `CLASSIC_TV_3_SPEC.md` §17. Then the device can report its
  own version.
- **Version the C# plugin alongside this repo.** The plugin source is absent; only zips in
  `web/plugins/` are tracked. Bring it in (submodule or monorepo) and pin the manifest to a
  build artifact produced from a known commit. Add a checksum verify step to the install
  instructions.
- **Decommission the v3 schedule service** (launchd `com.classictv.schedule_service`,
  `:8300`) now that the Jellyfin plugin serves v4. Running both is a latent source of
  confusion (two "sources of truth") and keeps jfmac dependencies alive.
- **The config must be promoted through the plugin, not edited on jfmac.** Confirm the
  18-channel config lives in the plugin's data/config path and is reproducible from the
  repo; otherwise the live schedule is not reproducible.

### H. Testing

- Replace real sleeps with injectable intervals/clock (see R12). The mode work will add
  more timed tests; without this the suite becomes unusable.
- Add a conftest helper that simulates the `xbmc.Player` callback surface.
- Add a test that asserts "busy/unreachable service while in BROWSE_MODE does not spawn
  retry threads and does not resume TV".
- Consider a single integration smoke test run against `http://192.168.1.207:8096` behind
  an env flag, covering the plugin response shape as the client expects it. The current
  unit tests use a fake server whose field names could drift from the .NET plugin.

### I. Documentation consistency

- Reconcile `RunPlugin` vs `PlayMedia` across `player.py`, `classic-tv-4-architecture.md`,
  and this spec.
- Correct `player.py`'s "onPlayBackEnded does not fire" docstring once R1 is done.
- Note in `classic-tv-4-architecture.md` that Phase 1 shipped (plugin 10.11.0.6 live) and
  Phase 3 (decommission v3) is outstanding.
- Update `CLASSIC_TV_3_SPEC.md` references to `addon/` → `addon4/` and `tests/` → `tests4/`
  for the 4.0 tree, or mark the doc as v3-only.

---

## 6. Suggested sequencing

**Stage 0 — Facts and hygiene (small, unblocks everything)**
1. On-device callback verification (R1).
2. Remove malformed keymap block; add global EPG mapping (R5, §3.2.6).
3. Fix `schedule.py` JSON type validation (§5.C).
4. Fix the offline-retry thread leak (§3.3.5 / §5.B).
5. Add git tag, remote, deploy `VERSION` + clean-tree check (§5.G).

**Stage 1 — Mode state machine core**
6. Refactor `ModeState` into the single transition owner with locking (R3, §5.A).
7. Implement `BROWSE_MODE` / `RESUME_PENDING` transitions and timer (R6, R7).
8. Make the watchdog mode-gated and pause-aware (R8, R10).
9. Non-blocking notification dispatch (§5.B).

**Stage 2 — Correlation and overlays**
10. Item-ID playback correlation (R2).
11. Wire or delete overlays (§5.D).
12. Inject dependencies; convert tests to fake clock + callbacks (R12, §5.H).

**Stage 3 — Cleanup**
13. Decommission v3 schedule service (§5.G).
14. Bring the C# plugin source into version control (§5.G).
15. Documentation reconciliation (§5.I).

---

## 7. Open questions

1. **Callback reliability** (R1) — outcome determines whether auto-detect/auto-resume is
   callback-driven or poll-driven. This is the one true blocker.
2. **Window activation target** — Home vs. Jellyfin library when entering `BROWSE_MODE`.
3. **Should the mode persist across Kodi restarts?** Startup always TV is fine, but a
   crash during `BROWSE_MODE` should not fight the user on restart. Default to TV plus a
   short "do not auto-reclaim for N seconds after boot" guard if needed.
4. **Channel numbers** — accept positional numbering as a known limitation, or add
   server-authoritative numbers now (enables clean digit entry, see R9).
5. **Overlay direction** — keep Python `WindowXMLDialog` overlays or move to skin per the
   architecture doc. This decision affects whether the mode work touches overlay code.
6. **Resume window value** — 5 s is plausible, but it must be derived against
   `PLAY_GRACE_SECONDS`. Decide the single model that governs both.

---

## Appendix — Key file references

| Concern | Location |
|---|---|
| Player does not own `xbmc.Player` (claim) | `addon4/resources/lib/player.py:15-18`, `:47` |
| `PlayMedia` play construction | `addon4/resources/lib/player.py:108-126` |
| Offline retry thread leak | `addon4/resources/lib/player.py:320-347` |
| Watchdog loop / grace gate | `addon4/resources/lib/modes.py:26-49` |
| Watchdog start/stop/generation | `addon4/resources/lib/modes.py:90-101` |
| No `guide` dispatch / no overlay wiring | `addon4/service.py:81-98`, `:187-196` |
| Positional channel numbers | `addon4/resources/lib/channels.py:51-54` |
| Keymap only `<FullscreenVideo>` + junk tail | `addon4/keymaps/keyboard.xml:21-66`, `:69-110` |
| `PLAY_GRACE_SECONDS = 15` | `addon4/resources/lib/constants.py:78` |
| JSON type not validated | `addon4/resources/lib/schedule.py:89-97`, `:140-159` |
| Deploy missing VERSION/clean check | `deploy/deploy-sweet-potato-4.sh` |
| v3 release-engineering requirements | `CLASSIC_TV_3_SPEC.md:549-577` |
| Migration phases / decommission pending | `docs/classic-tv-4-architecture.md:224-248` |
