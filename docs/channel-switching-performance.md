# Channel-switching performance

**Measured:** 2026-09-29/30 on the Sweet Potato (addon 4.5.x, Jellyfin addon 2.1.0, plugin 10.11.0.15).
**Status:** analysis and proposals. Only 2.1 (timing log line) is implemented, in addon 4.5.3.

## 1. Where the time goes

A tune (ch+/-, or digit entry) has these phases. Numbers come from `kodi.log` and the jfmac
Jellyfin log for real tunes; timestamps are UTC (Kodi) and -05:00 (Jellyfin) for the same instant.

| Phase | What happens | Measured |
|---|---|---|
| A. Stop + decoder release | `stop()`, poll `isPlaying()` up to 2 s | 0.1 to 1.7 s |
| B. Slot fetch | `GET /ClassicTV/channel/{id}` | ~0.03 s (server log shows the request ~0.25 s after the key) |
| C. `PlayMedia` to Jellyfin addon start | Kodi launches the Jellyfin addon plugin process for `mode=play` | **~7.5 s** |
| D. Stream start | Jellyfin `PlaybackInfo`, then Kodi opens the stream | ~1.3 s |

Evidence for C: on the tune at 01:51:30, `play_slot` ran at 01:51:30.74; jfmac logged the first
`PlaybackInfo` request (`MediaInfoHelper`) at 20:51:38.28, 7.5 s later; `onPlayBackStarted` came at
01:51:39.58.

Time from `play_slot` to `onPlayBackStarted` across the session:

| Event | play_slot to started |
|---|---|
| Boot, sitcoms (x3) | 6.0, 6.2, 6.1 s |
| ch+ / digit tunes | 6.4, 6.4, 8.8 s |
| Watchdog replay, same channel, next episode (no stop before it) | **2.8 s** |

**Conclusion:** our own code (phases A and B) is well under 2 s. About 6 to 9 s is the Jellyfin Kodi
addon starting playback. Optimizing Classic TV's own fetch and stop code cannot fix that; the
strategies below are ordered by expected payoff.

Unknown, needs measuring before acting: why the watchdog replay is 3 s faster. Candidates are a
warm plugin process, no decoder teardown in front of it, or a shorter path in the addon.

## 2. Strategies

### 2.1 Measure first (implemented in 4.5.3)

Every tune now logs one line when playback starts:

```
[ClassicTV][player] tune timing channel='anime' stop=0.31s fetch=0.04s launch=6.42s total=6.77s
```

- `stop`: key received to stop + decoder release finished (phase A).
- `fetch`: slot request (phase B).
- `launch`: `PlayMedia` issued to `onPlayBackStarted` (phases C + D, the Jellyfin addon).
- `total`: key to playing.

Watchdog replays and startup have no key, stop or fetch phase, so they log `launch` and `total` only.
Read them with `python3 tools/classictv-logs.py --since 1h | grep "tune timing"`. Use these numbers
to prove or disprove each strategy below (for example, before and after lowering the Jellyfin addon
log level in 2.4.1).

### 2.2 Debounce rapid surfing ("latest wins") (high value for surfing)

Today each ch+ press runs the whole sequence, and presses that arrive during a tune block on the
notification thread and then run in turn. Five quick presses cost five stop/fetch/`PlayMedia`
cycles, and each `PlayMedia` starts a heavy Jellyfin plugin process (phase C) that is then cancelled.

Proposal: on ch+/-, update the *target* channel immediately, show the ident for it, and start a short
timer (~400 to 600 ms). Only when the timer fires with no further presses do the stop/fetch/play. Digit
entry already behaves this way. This turns N tunes into 1 and removes wasted phase C work.
Risk: adds up to the debounce delay to a single tune. Mitigation: skip the delay for the first press
after a settled channel (only debounce presses that follow another within the window).

### 2.3 Instant feedback (perceived latency)

The screen goes black at stop, and the ident appears only after `play_slot`. The viewer sees
nothing for ~0.3 to 2 s, then the ident over ~6 s of black. Show the ident for the *destination*
channel at the moment the key arrives (before stop), and keep it up until `onPlayBackStarted`.

Caution learned on 4.5.1: an open `WindowXMLDialog` changes where Kodi resolves remote keys, so
digits typed while it is up were lost (fixed in 4.5.2 by forwarding actions in
`ChannelBug.onAction`, unverified on device). Any feedback shown earlier must be verified to keep ch+/-
and digits working. A notification toast (`ToastDisplay`) has no such problem.

### 2.4 Cut the Jellyfin addon's start-up cost (highest potential, needs experiments)

Phase C dominates, so this is where big wins are. Options, safest first:

1. **Turn down the Jellyfin addon's logging.** `addon_data/plugin.video.jellyfin/settings.xml` has
   `logLevel=2` and `kodi.log` shows the addon dumping full item JSON and art URLs at DEBUG (about
   2,600 log lines in one 9 s tune). On an S905X writing to eMMC/SD that is real work on the hot path.
   Lower the level in the addon settings, then re-measure with 2.1. Cheap and reversible. The expected
   gain is unknown; it may be small.
2. **Direct paths / native mode** (`useDirectPaths=0` today, i.e. "addon mode"). In addon mode every
   play runs a plugin process that opens the Jellyfin and Kodi video databases, builds a ListItem and
   asks the server for `PlaybackInfo`. Native mode plays a library path directly and skips that
   round trip. It needs the media path to be reachable from the Sweet Potato (SMB/NFS from jfmac) and
   a library re-sync, and it changes how `StartOffset` (our `timestamp=` parameter) would be applied.
   Big change to the core principle in `AGENTS.md` section 4 (we hand Jellyfin a plugin URL). Treat as
   an experiment on a branch, not a tweak.
3. **Keep the plugin/DB path warm.** If the 2.8 s replay is due to a warm process, a periodic no-op
   invocation might help. Speculative; only try after 2.1 shows a warm/cold difference.
4. **Storage.** Confirm the Kodi userdata (`Database/`, `Thumbnails`) and log are not on a slow or
   contended device; DB opens show up as visible gaps in the log.

### 2.5 Trim our own stop and fetch (small; do last)

- **Fetch in parallel with stop.** The slot fetch (~0.03 s) does not depend on the stop. Start it on
  a thread first; saves the fetch time only (tens of ms). Not worth the complexity alone.
- **Prefetch neighbours.** Keep the slots for ch+1 and ch-1 warm. Cached slots need their
  `startOffset` advanced by elapsed time, and must expire at the slot end. Saves ~0.03 s per tune, so
  skip unless 2.1 shows fetch time is larger on a busy server.
- **Decoder wait.** The 2 s poll for decoder release is a safety guard added because jumping into video
  windows while the Amlogic decoder is tearing down has crashed the device (`AGENTS.md` section 8).
  Do not shorten it on a guess. Measure how long it actually takes (phase A ranged 0.1 to 1.7 s)
  and only then consider a smaller timeout.

### 2.6 Don't do

- Don't call `xbmc.Player.play()` or `seekTime()` ourselves to skip the plugin: the whole 4.0 design
  avoids the multiple-players problem (`docs/seek-strategy-findings.md`).
- Don't pre-start the next channel in a second player: the S905X decodes one stream at a time.
- Don't cache slots beyond their end time, or the schedule will show the wrong episode.

## 3. Suggested order

1. ~~2.1 Timing log line~~ (done, 4.5.3). Collect a baseline over several tunes first.
2. 2.4.1 Lower the Jellyfin addon log level and re-measure (settings change, no code).
3. 2.2 Debounce and 2.3 earlier feedback (code, addon-only, unit-testable).
4. 2.4.2 Native mode experiment only if phase C is still the bottleneck.

## 4. How to measure a tune by hand

```bash
python3 tools/classictv-logs.py --all --since 5m | grep -E "ch_up|ch_down|confirmed channel|play_slot|onPlayBackStarted|ClassicTvController|MediaInfoHelper"
```

The interval `play_slot` to `onPlayBackStarted` is phases C + D. The interval from the jfmac
`ClassicTvController` line to the `MediaInfoHelper` line shows how long Kodi took to reach the
server after `play_slot`.

## 5. Incident: 2026-10-02 slow spells (Jellyfin addon jam)

Two stretches where starting a channel took far longer than the usual 4 to 9 s:

| Time (UTC) | Tune | Time to playing |
|---|---|---|
| 23:05:29 | ch+ to grown-up-cartoons | 26.6 s |
| 23:37:31 | GUIDE back to TV | 94.9 s |

What the Kodi log shows:

- Server side was fine. jfmac was ~86% idle, and every `PlaybackInfo` request it received was answered
  within a second.
- Kodi's side stalled. Jellyfin addon `?mode=play` scripts started, logged `[ subtitles/0 ]`, and
  then did nothing until Kodi killed them: `CPythonInvoker ... script didn't stop in 5 seconds`
  (three of them at 23:38:00, :32 and :37). A later invocation got through in under a second.
- A second Jellyfin addon script was running at the same time: a `mode=browse&type=movies` directory
  load for the Videos window (`GetDirectory ... failed` at 23:38:37; `Control 50 in window 10025 has been
  asked to focus` right after the stop at 23:05:28; a `RecentlyAdded` browse at 23:05:44). Both bad
  tunes came after the viewer had browsed the Jellyfin library. **Working theory (not proven):** stopping
  playback makes Kodi return to the last window, the Videos list, which reloads through the Jellyfin
  addon and competes with the play script for the same slow CPU/addon.
- The watchdog made it worse. Its 15 s grace was shorter than these starts, so it issued a replay at
  23:37:51, :38:26 and :39:01, each launching another plugin script into the jam, and counted two of
  the three failures that trigger the Jellyfin addon self-heal (a disable/enable of the addon).

Changes in 4.7.1: `PLAY_GRACE_SECONDS` 15 to 30, so the watchdog stops stacking replays on a slow
start (a genuinely failed start now recovers 15 s later).

Ideas, not done (each needs measuring with the `tune timing` line first):

- Make sure the "previous window" is Home, not the Videos list, before a channel change, for example
  by going Home right after the decoder releases, so a stop does not reload a Jellyfin list. Sample so
  far is small: launch averaged 6.7 s on tunes with the window-10025 error nearby versus 5.2 s without
  (6 tunes).
- Replace instead of stop-then-play when resuming from browse, so Kodi never leaves fullscreen. Risky:
  the stop plus decoder wait exists because of Amlogic decoder crashes.
- Raise the "script didn't stop" limit or reduce widget and library refreshes in the Jellyfin addon
  settings.

## 6. Measured baseline and the identical-URL stall (2026-10-03)

`tools/measure_tunes.py` simulates the remote over JSON-RPC and times key press to `Player.OnAVStart`
(8 alternating ch+/ch- tunes, 12 s apart, addon 4.7.1):

| Tune | Key to picture |
|---|---|
| 5 of 8 | 6.75 to 7.11 s (stop 0.8 s, `OnPlay` at about 6.3 s) |
| 3 of 8 | 33.4 to 33.6 s |

So the normal cost is about 6.9 s, and 5.5 s of it is the gap between the stop and Kodi starting
the new stream (Jellyfin addon launch), as in section 1.

The 33 s tunes were all repeats of a play URL that had been played about 35 s earlier. The channel's
slot offset only advances once a minute (the server cache is keyed by minute), so tuning back to a
channel within the same minute issues a byte-identical `PlayMedia(plugin://...)`. For those, the
Kodi log shows **no `entrypoint.default path:` line at all**: Kodi never ran the Jellyfin script.
Nothing started until the watchdog replayed after its grace period, with the next minute's offset, a
different URL, and that replay then started in 2.7 s. 4 of 4 identical-URL repeats failed and 0 of
about 8 different-URL plays did. The same pattern explains the 2026-10-02 incident in section 5
(the resume replayed offset 462.825 twice).

Fix in 4.7.2: every play URL carries a unique `&nonce=` so it can never match a recent one. Expected
result: the 33 s tunes become about 7 s. Re-measure with the tool above.

Also seen: the replay that worked took 2.7 s against 6.3 s for a normal tune. Something in the normal
path is slower than in the replay; worth finding.

Side issue found on the way: the server's `StartOffset` comes from a per-minute cache slot, so it can
be up to 59 s behind the true schedule position. Cheap fix in the plugin controller: add
`now - minute-floor` to a cache hit's offset.

## 7. Experiments (2026-10-04)

All numbers are `tools/measure_tunes.py`, key press to picture, alternating ch+/ch-.

**How to measure (important).** Restart Kodi with `systemctl restart kodi` between experiments. Do not
use `Addons.SetAddonEnabled` to restart only the Classic TV addon: Kodi cannot stop the old service's
threads ("script didn't stop in 5 seconds"), so every restart leaves a ghost copy of the addon whose
watchdog keeps replaying its own stale channel. After a few restarts channel changes took 30 to 54 s.
That contaminated the first runs of the experiments below, which were re-run after a clean restart.
A power-cycle-free clean state is `systemctl restart kodi` on the device.

| Experiment | Result | Verdict |
|---|---|---|
| Baseline, stock sequence (stop, wait for decoder, play), clean restart | median 7.14 s, 10 tunes; one 33 s stall | |
| Lower the Jellyfin addon log level | no noticeable effect (reported by the user) | no help |
| Go Home after the decoder releases | median 7.85 s vs 7.56 s baseline | no help, reverted |
| **Play directly, no stop first** (`STOP_BEFORE_PLAY = False`) | **median 3.84 s, 10/10**, then 20 more tunes at 5 s spacing: median 4.22 s, 20/20 | **adopted in 4.8.0** |

Why it helps: the old sequence stopped playback, waited for the decoder and only then issued `PlayMedia`,
so Kodi tore the old stream down and then started the Jellyfin addon from cold. Letting Kodi replace the
running stream overlaps those, and no `Player.OnStop` is raised in between. Across the 30 clean no-stop
tunes there were no stalls, no browse-mode false positives, no watchdog triggers and no new Kodi crash
logs, so the decoder-release guard (kept for mirror retunes and for entering browse mode) is not needed
for plain channel changes on this device. Set `STOP_BEFORE_PLAY = True` to go back.

Remaining cost is the Jellyfin addon start (about 3.3 s `OnPlay`, 4 s to picture).

Residual stall: in one clean stock-sequence run (with the unique-URL nonce from 4.7.2) one tune still
stalled for 33 s with the same signature (Kodi never ran the Jellyfin script until the watchdog replayed
after its 30 s grace). So the nonce reduced but did not remove it: 1 stall in 30 nonce tunes, against 3 in 8
before it. The no-stop run had none in 30. If stalls come back, an early single retry at about 14 s would
cut a stall from 33 s to about 20 s.

## 8. False browse after a slow start (4.8.1, 2026-10-10)

A GUIDE return on 2026-10-10 took 32.5 s to start. The 30 s grace had expired, so the first start
was matched against the Jellyfin id: the id did not match, and the addon dropped the viewer into browse
mode ("foreign playback"). Fix: the first start after our own `play_slot()` is always ours
(`_awaiting_start`), whatever the launch time. Later starts are still judged normally, so a start the
viewer triggers in Jellyfin is still caught. Tests: `test_slow_first_start_after_play_slot_stays_in_tv`,
`test_second_start_after_a_slow_one_is_judged_normally`.
