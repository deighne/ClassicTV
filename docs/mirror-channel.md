# Mirror channels

**Status:** built (addon 4.7.0, plugin 10.11.0.16). Unit-tested; not yet verified on device. The plugin
must be installed and the channel added before it exists on the TV (see "Turning it on").

## What it is

A mirror channel (e.g. Channel 67) plays whatever a target Jellyfin user (e.g. `alice`) is watching right now, at their position, so the
viewer watches along. Any channel can be a mirror of any user: set `mirrorUser` on the channel.

## How it works

**Plugin** (`Schedule/Mirror.cs`, `Api/ClassicTvController.cs`). A channel with `mirrorUser` is served
by the normal `GET /ClassicTV/channel/{id}`. Instead of a schedule it reads that user's live Jellyfin
session (`ISessionManager`) and returns the item, her position extrapolated to now, and whether she is
paused. `GET /ClassicTV/channels` reports `Mirror: true` for these channels. Rules in
`MirrorResolver`:

- video only; the newest session wins if she is on several devices
- a playing session that has not reported for 90 s is treated as gone; paused for over 10 min likewise
- idle looks like an offline channel (empty item id)

**Addon** (`mirror.py`, `player.py`). Tuning to the channel plays her item at her position plus a
lead. While it is tuned, a loop (`MirrorSync`) polls every 3 s and decides (`mirror.decide`):

| She... | The channel... |
|---|---|
| starts a new item | plays the new item |
| is more than 20 s away from us | plays it again at her position |
| pauses | keeps playing; drift is ignored until she resumes |
| stops (3 idle polls, about 9 s) | says "signed off" and returns to the previous channel |
| is idle when you tune in | says "is offline" and does not interrupt what you were watching |

Classic TV never seeks, so keeping in step means re-issuing the play command (same path as a channel
change, including the decoder wait). A stream takes several seconds to start, so every start is
`lead` seconds ahead of her position, where `lead` is the measured launch time from the last tune
(2 to 12 s, 6 s before the first measurement). Without that the viewer would land that far behind
and retune in a loop. Thresholds are in `constants.py` (`MIRROR_*`).

Entering browse mode stops the sync; GUIDE back to TV re-tunes the channel and restarts it.

## Turning it on

1. Install plugin 10.11.0.16 on jfmac (`AGENTS.md` section 7; Jellyfin restarts, which interrupts
   any playing stream).
2. Add the channel, with an admin API key (Dashboard > API Keys):

   ```bash
   JELLYFIN_API_KEY=... python3 tools/add_mirror_channel.py          # dry run
   JELLYFIN_API_KEY=... python3 tools/add_mirror_channel.py --apply
   ```

   Do this *after* step 1: an older plugin silently drops `mirrorUser`.
3. Deploy addon 4.7.0 to the Sweet Potato and reboot it.

Test by having Rachel play something, then tune 67 (digits `6` `7`).

## Privacy and limits

- `/ClassicTV/channel/rachel` is anonymous like the other channel endpoints, so anyone on the LAN can see
  the title of what she is watching. It is not exposed through `tv.dane.photos` (Caddy serves only the
  static site), but Jellyfin's port 8096 is open on the LAN.
- A retune costs several seconds of black, so drift correction is deliberately loose (20 s).
- Position comes from her client's progress reports (about every 10 s); the plugin extrapolates between
  reports, so a seek on her side takes up to a few seconds to show up.
- Audio-only sessions are ignored.

## ch+/ch- and an idle mirror channel (4.7.1)

Stepping onto an idle mirror channel with ch+/ch- used to refuse to tune and leave you where you were, so
you could never get past channel 67. ch+/ch- now skip a mirror channel whose user is idle (the server is
asked first; if it cannot be reached the channel is not skipped). Typing the number still tunes to it and
shows the "is offline" message.
