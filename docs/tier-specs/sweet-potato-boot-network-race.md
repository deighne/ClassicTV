# Sweet Potato Boot Network Race — Spec

**Date:** September 20, 2026
**Status:** Spec — not implemented
**Component:** Sweet Potato (CoreELEC/Kodi) boot sequencing + ClassicTV startup
**Related:** `AGENTS.md` §8, `docs/tier-specs/tier-3-robustness.md`

---

## 1. Problem

When the Sweet Potato cold-boots, the Classic TV system can come up with a black screen and
never recover, even though the network and Jellyfin are reachable a few seconds later.

Observed 2026-09-20 on a cold reboot:

```
[ClassicTV][player] play_slot channel='sitcoms' ...
JELLYFIN ... ConnectionError: ... 192.168.1.207:8096 ...
  Failed to establish a new connection: [Errno 101] Network is unreachable
JELLYFIN ... 404 Client Error for url: http://{jellyfin-server}/Users/.../Items/...
JELLYFIN connection_manager: Found Servers: [{'Address': 'http://{jellyfin-server}', ...}]
                      ^ literal placeholder, not a real address
```

The ClassicTV addon itself is healthy — it keeps fetching schedule slots and calling
`PlayMedia(plugin://plugin.video.jellyfin/...)`. The Jellyfin addon, which owns playback,
is stuck with an unresolved server (`http://{jellyfin-server}`) and 404s every item. The
watchdog then retries the same slot every 20 s, so the channel never plays. Only a Kodi
restart (after the network is up) clears it.

A second, independent failure mode was found the same day: if a scheduled episode no longer
exists in the library, the same watchdog loop occurs (fixed separately in plugin 10.11.0.15
by pruning missing episodes). This spec is about the boot-time network race only.

---

## 2. Root cause

Two facts combine.

### 2.1 Kodi does not reliably wait for connectivity

`kodi.service` is:

```
After=network-online.target
Wants=network-online.target
```

`network-online.target` is satisfied by `kodi-waitonnetwork.service`, which runs
`connmand-wait-online --timeout=${WAIT_NETWORK_TIME}`. On the device:

```
/storage/.cache/coreelec/network_wait → WAIT_NETWORK="true" WAIT_NETWORK_TIME="10"
```

So Kodi waits **at most 10 seconds** for connman to report "online". WiFi association +
DHCP + (in this case) the route to `192.168.1.207` can take longer than that on a cold boot.
When the timeout elapses, Kodi starts anyway.

### 2.2 The Jellyfin addon does not re-resolve its server when the network arrives

`plugin.video.jellyfin` resolves its server once, early in its service startup
(`connect.Connect()`). The only reconnect path in `entrypoint/service.py` is for
`System.OnSleep` / `System.OnWake` (suspend/resume):

```python
elif method == "System.OnWake":
    xbmc.sleep(10000)  # Allow network to wake up
    self.monitor.sleep = False
```

There is no "network came online at boot" retry. If `connect.Connect()` fails, the addon
keeps the placeholder address and does not recover until Kodi restarts. (The ClassicTV
addon cannot fix the Jellyfin addon's internal state by asking for a slot again — the
schedule call succeeds, so from ClassicTV's point of view everything looks fine.)

### 2.3 Why the current safety nets don't catch it

- ClassicTV's `startup_play` retry loop only retries the **ClassicTV schedule** endpoint.
  That host is reachable by the time playback is attempted, so the retry succeeds and
  ClassicTV believes it started normally.
- The watchdog can't distinguish "the Jellyfin addon failed to resolve its server" from
  "the episode ended", so it replays into the same broken addon forever.
- The schedule-level failure (missing item) and the boot-level failure (unresolved server)
  produced the same visible symptom, which made this harder to diagnose.

---

## 3. Goals / non-goals

**Goals**
- A cold boot must reach playing TV without human intervention, even when WiFi/DHCP/Jellyfin
  take longer than 10 s to become reachable.
- The system must recover automatically if the network or Jellyfin becomes reachable after
  Kodi has already started.
- Failures must be visible (a waiting/offline overlay), never a silent black-screen loop.

**Non-goals**
- Changing the Jellyfin Kodi addon's source (out of our control; we can only restart it).
- Supporting boots where the network never comes up (that is an offline/fallback state,
  not this spec).
- Replacing the watchdog.

---

## 4. Options

### A. Increase the CoreELEC network wait timeout (cheap, partial)
Raise `WAIT_NETWORK_TIME` from 10 to e.g. 60. This narrows the window but does not
eliminate it: WiFi can still take arbitrarily long, and it makes every boot 10–60 s slower
in the worst case (the timeout is a cap, not a fixed delay). Persisting the value may also
fight the CoreELEC settings addon that regenerates `network_wait`.

### B. Wait for the Jellyfin **server** before starting Kodi (strong, boot-blocking)
A small systemd oneshot, ordered `Before=kodi.service` / `Wants=network-online.target`, that
polls `http://192.168.1.207:8096/System/Info/Public` (TCP connect or HTTP 200) with a cap
(e.g. 60 s) and then exits. Kodi simply does not start until the server answers. This is the
only option that removes the race entirely, at the cost of a slower boot and a hard-coded
LAN address (acceptable: it is already in the addon settings and deploy scripts).

### C. ClassicTV readiness gate + self-heal (defense in depth, no boot change)
On startup, before the first `PlayMedia`, poll the Jellyfin base URL until reachable; then
verify the Jellyfin addon actually starts playback and, if it does not while the server is
reachable, reload the Jellyfin addon.

- **Readiness gate:** `service.py` waits for `GET {jellyfin_base_url}/System/Info/Public`
  (bounded, with a visible "waiting for network" state) before `startup_play`.
- **Playback confirmation:** `play_slot()` already records a `PlaybackSession`; if
  `onPlayBackStarted` has not fired for the requested item within `PLAY_GRACE_SECONDS + N`,
  and the server is reachable, treat the Jellyfin addon as wedged.
- **Reload:** disable/enable the Jellyfin addon to force its service to restart, e.g. via
  JSON-RPC `Addons.SetAddonEnabled` (`false` then `true`) for `plugin.video.jellyfin`. Needs
  on-device validation; `RestartApp` is the fallback but is disruptive.

### D. Do nothing at boot; rely on a delayed Kodi restart
A CoreELEC autostart script that restarts Kodi N seconds after boot once the server is
reachable. Blunt, disruptive (double boot), and races with the user. Not recommended.

---

## 5. Recommended approach

**Layered, in priority order:**

1. **B — wait for the Jellyfin server before Kodi starts.** Eliminates the race for the
   common case. Implement as a systemd unit or a CoreELEC autostart hook; cap at ~60 s so a
   genuinely unreachable server still lets Kodi start (and C handles it).
2. **C — ClassicTV readiness gate and self-heal.** Covers the cases B can't (server slow,
   network dropped after boot, Jellyfin addon wedged independently). Always show a waiting
   state instead of a black loop.
3. **A — bump `WAIT_NETWORK_TIME`** to a modest value (e.g. 30) as a cheap backstop while B
   is implemented.

Do **not** rely on A alone.

---

## 6. Detailed design (recommended)

### 6.1 Boot gate for the Jellyfin server (B)

A unit ordered before Kodi:

```ini
# /storage/.config/system.d/classictv-wait-for-jellyfin.service (or /etc equivalent)
[Unit]
Description=Wait for the Jellyfin server before Kodi
Wants=network-online.target
After=network-online.target
Before=kodi.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/storage/.kodi/addons/plugin.video.classictv/resources/wait-for-jellyfin.sh
TimeoutStartSec=90

[Install]
WantedBy=kodi.target
```

`wait-for-jellyfin.sh` polls `http://192.168.1.207:8096/System/Info/Public` every 2 s up to
`JELLYFIN_WAIT_TIMEOUT` (default 60 s), exiting 0 on HTTP 200, and 0 anyway on timeout (so
boot is never blocked indefinitely; C then takes over). The host/port come from the addon
settings or a shared file, not hard-coded twice.

Exact CoreELEC mechanism (custom systemd drop-in vs. `autostart.sh`) to be confirmed; the
deploy script must install it.

### 6.2 ClassicTV readiness gate and self-heal (C)

In `addon4`:

- **Server readiness (startup):** before the first `startup_play`, poll
  `GET {jellyfin_base_url}/System/Info/Public` (reuse the HTTP client in `schedule.py`) with
  backoff, bounded by a new `JELLYFIN_READY_TIMEOUT_SECONDS` (e.g. 90 s). While waiting, show
  a simple "Classic TV — waiting for the media server" state (a notification or a minimal
  overlay; the channel-bug style can be reused later). Log each attempt.
- **Playback confirmation:** track the requested item per tune. If `onPlayBackStarted` (or
  the Jellyfin path playing) has not confirmed within `PLAYBACK_CONFIRM_TIMEOUT_SECONDS`,
  and the server is reachable, treat the Jellyfin addon as wedged.
- **Jellyfin addon reload:** attempt a single reload per N minutes (rate-limited) via
  JSON-RPC `Addons.SetAddonEnabled` for `plugin.video.jellyfin` (disable then enable). If the
  JSON-RPC is unavailable or fails, log and fall back to `RestartApp`. Verify on device that
  disabling/enabling the Jellyfin addon restarts its service and re-runs `connect.Connect()`.
- **Never black-loop:** while waiting/unrecoverable, keep a visible state and retry on a
  bounded backoff; do not spin the watchdog.

### 6.3 Constants

| Constant | Value | Why |
|---|---|---|
| `JELLYFIN_WAIT_TIMEOUT` (boot gate) | 60 s | covers a slow WiFi+DHCP cold boot without blocking forever |
| `JELLYFIN_READY_TIMEOUT_SECONDS` (addon) | 90 s | server may be slower than the network |
| `JELLYFIN_READY_POLL_SECONDS` | 2 s | responsive, low load |
| `PLAYBACK_CONFIRM_TIMEOUT_SECONDS` | ~25 s | `PLAY_GRACE_SECONDS` + margin; must exceed stream bring-up |
| Jellyfin addon reload cooldown | 5 min | avoid restart storms |

---

## 7. Acceptance criteria

1. Cold boot with a slow network: TV starts playing with no intervention once the network
   and Jellyfin are reachable.
2. Cold boot with the Jellyfin server initially down and brought up later: the addon shows a
   waiting state, then begins playing, without a Kodi restart.
3. Cold boot with Jellyfin reachable and the network normal: no added delay beyond the
   server answering (boot gate exits as soon as `/System/Info/Public` returns 200).
4. If the Jellyfin addon is wedged (server reachable, no playback start), Classic TV reloads
   it at most once per cooldown and then resumes playback.
5. No black-screen watchdog loop in any of the above; failures are visible.
6. The Jellyfin addon pointing at `http://{jellyfin-server}` is detected and resolved without
   a manual Kodi restart.

---

## 8. Test plan

- **Normal cold boot:** power cycle the Sweet Potato; confirm TV plays and the boot gate
  adds no more than ~1–2 s.
- **Slow network:** boot with WiFi disabled at the router (or disconnect the AP), wait, then
  restore; confirm the addon waits, recovers, and plays.
- **Server down at boot:** stop Jellyfin on jfmac, cold-boot the Sweet Potato, start
  Jellyfin after 60 s; confirm recovery without a Kodi restart.
- **Wedged addon:** simulate by starting Kodi with the server unreachable, then bring the
  server up; confirm the reload path fires and playback starts.
- **Timeout behavior:** leave the server down; confirm the boot gate gives up at its cap and
  Kodi still starts (with the addon waiting state), not a hang.

---

## 9. Risks

| Risk | Mitigation |
|---|---|
| Boot gate hangs if the server never answers | Hard cap (60 s) and exit 0 on timeout; addon gate takes over. |
| Hard-coded server address duplicated | Read from the addon setting / shared config; document in deploy. |
| `Addons.SetAddonEnabled` doesn't restart the Jellyfin service reliably | Verify on device; fall back to `RestartApp`; rate-limit. |
| A visible waiting overlay causes CRT retention | Same constraints as the channel bug (small, dim, corner) or use a short notification. |
| CoreELEC regenerates `network_wait` / resists custom units | Prefer a `Before=kodi.service` drop-in; confirm persistence across OS updates. |
| Reload storms during a long outage | Cooldown + bounded backoff. |

---

## 10. Open questions

1. Does disabling/re-enabling `plugin.video.jellyfin` via JSON-RPC actually re-run its
   service and `connect.Connect()`, or is a full `RestartApp` required? (Must test on device.)
2. What CoreELEC mechanism survives an OS update for the boot gate: a systemd drop-in under
   `/storage`, a CoreELEC autostart hook, or the built-in "wait for network" setting?
3. Should the readiness gate live in ClassicTV at all, or is a boot-level gate sufficient in
   practice? (Layering is safer, but ClassicTV code is our maintenance burden.)
4. Where should the "waiting for media server" visual come from — a `Dialog().notification`,
   the channel-bug overlay style, or a dedicated small skin window?

---

## 11. Implementation order

1. Bump `WAIT_NETWORK_TIME` to 30 as an interim mitigation and verify it helps.
2. Implement and validate the JSON-RPC Jellyfin-addon reload on a wedged boot (answers Q1).
3. Add the ClassicTV server-readiness gate + waiting state (C) and deploy.
4. Add the boot gate (B) using the mechanism decided in Q2.
5. Add the playback-confirmation + reload path (C) with cooldown.
6. Document all of it in `AGENTS.md` §8 (gotchas) and the deploy scripts.
