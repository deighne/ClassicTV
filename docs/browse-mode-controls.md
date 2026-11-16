# Browse-mode playback controls (pause, seek, subtitles)

**Status:** implemented in addon 4.6.0 (unit-tested; not yet verified on device).

## Problem

Outside Classic TV (BROWSE mode: a movie or show the viewer started from Jellyfin), the remote could
not pause, seek or toggle subtitles. Cause: `addon4/keymaps/keyboard.xml` binds OK, the d-pad and Back
in `<FullscreenVideo>` to `NotifyAll(classictv.service, ...)` for Classic TV channel entry, and that
window is also the one used for every fullscreen video. In browse mode the service received those
events and ignored them, replacing Kodi's native behaviour.

## What the keys do in browse mode

| Key | Action |
|---|---|
| OK | pause / resume (`Action(PlayPause)`) |
| Left | step back (`Action(StepBack)`, Kodi's small seek step) |
| Right | step forward (`Action(StepForward)`) |
| Up | on-screen display: progress, audio and subtitle buttons (`Action(OSD)`) |
| Down | subtitles: next stream, then off; a toast says "No subtitles in this file" if there are none |
| Back | unchanged (Kodi's Back) |

Classic TV mode is unchanged: those keys keep their TV meaning. The routing is in
`ClassicTVMonitor.dispatch` (`service.py`), the actions in `browse_controls.py`
(`BrowseControls`). Inside the OSD and other dialogs the keys are not hijacked, so Kodi's own
navigation works there.

## Why subtitles were "sometimes"

Subtitle availability varies per file, and nothing in Classic TV controls it:

- Some files have **no subtitle streams at all**. Checked on 2026-09-30: the movie playing at the
  time reported `subtitles: []` through JSON-RPC.
- Files that do have streams are shown or hidden by Kodi's own selection rules, not by us. This
  device has `locale.subtitlelanguage = original`, `subtitles.languages = English`, and the
  Jellyfin addon has `enableExternalSubs = true`. With "original", Kodi generally shows a subtitle
  only when it is flagged forced or default, or when the audio language differs from the original.
  Whether a given file carries those flags is up to its metadata.

Since 4.6.0 every playback logs a line three seconds after it starts, so the answer for any file is in
the log:

```
[ClassicTV][player] streams subtitle_streams=1 enabled=False current=None available=['eng English'] item='...'
```

- `subtitle_streams=0`: the file has none; nothing to enable.
- `subtitle_streams>0, enabled=False`: the file has subtitles and Kodi chose not to show them. Press Down.

Find them with `python3 tools/classictv-logs.py --since 6h | grep "streams subtitle"`.

If most files with subtitles turn out to be hidden by default, change Kodi's preferred subtitle
language (Settings > Player > Language > Preferred subtitle language) rather than adding code;
decide after reading some of those log lines.

## Dedicated transport buttons (not done)

The Sony RM-Y180 table (`ir-keymaps/sony-rmy180`) only has 19 keys: digits, d-pad, OK, ch+/-, guide
and exit. Play, pause, stop, fast-forward, rewind and subtitle buttons were never captured, so
pressing them does nothing. Once captured, Kodi's default keymap handles them natively (they are not
in the `<FullscreenVideo>` hijack list), including fast-forward and rewind speeds.

To capture them, on the Sweet Potato:

```bash
systemctl stop kodi eventlircd
bash /var/media/xtra/classictv-bootstrap/scan_remote.sh transport
systemctl start eventlircd kodi
```

It prompts for each button in turn (Ctrl-C when you run out of buttons; what it captured is kept in
`/tmp/remote_scancodes.txt`). Add those lines to `ir-keymaps/sony-rmy180` and redeploy.
Note that a transport button in Classic TV mode will also pause a channel; the watchdog tolerates a
pause, but the stream will no longer be in step with the wall clock until the next tune.
