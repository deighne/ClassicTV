# Channel Bug MVP — Kodi Skinning

**Date:** September 20, 2026
**Status:** Spec — not implemented
**Scope:** Show the channel number and channel name for ~10 seconds after a channel change
**Related:** `docs/classic-tv-4-architecture.md` (look & feel as skinning), `tier-3-robustness.md` (overlays), `tier-5-deferred.md` §5.1 (skin overlays)

---

## 1. Goal

After the viewer tunes to a channel, display a small on-screen overlay showing:

- **Channel number** (large)
- **Channel name**

for about **10 seconds**, then hide it. Nothing else.

This is the "channel bug" — the little ident that appears when you change channels on
real cable. In Classic TV it is the single most important piece of visual feedback, because
after a channel change the CRT is otherwise just showing the new program with no
confirmation of what was tuned.

---

## 2. Non-goals (explicitly out of this MVP)

- **No show title, no accent color, no "now playing" text.** (The dead `ChannelBug` class
  was written for these; the MVP renders number + name only.)
- **No channel static / snow overlay.** That is a separate Tier 3 item.
- **No EPG/guide.**
- **No skin fork.** We are not forking Estuary or shipping a full custom skin. See §4.
- **No animation.** Static text, low-opacity background.
- **Not shown on EOF/watchdog recovery.** Only on an actual tune.

---

## 3. Current state (what exists today)

| Thing | State |
|---|---|
| `addon4/resources/lib/channel_bug.py` | `ChannelBug(xbmcgui.WindowXMLDialog)` exists; takes `(number, name, title, color)`; auto-dismiss via `threading.Timer`; **never instantiated**. |
| `addon4/resources/lib/channel_static.py` | Exists; referenced by `player._change_channel` but never assigned, so a no-op. |
| `addon4/service.py` | Never assigns `player._channel_bug` / `player._channel_static`. |
| Skin XML | **None.** `addon4/resources/` has only `lib/` and `settings/` — no `skins/` tree. |
| `constants.py` | `CHANNEL_BUG_DISPLAY_SECONDS = 6` (documented as CRT burn-in protection). |
| Channel change flow | `player._change_channel()` → `play_slot(slot)` → `PlayMedia(plugin://plugin.video.jellyfin/…)`. `play_slot()` updates `_current_channel_id` and logs the title. |

So the Python host class is ~80% there; what's missing is (a) skin XML, (b) a 10 s constant,
(c) instantiation + show/dismiss wiring, (d) the tune-only rule.

### Working reference on this exact device

The installed Jellyfin addon ships addon-owned dialogs at
`resources/skins/default/1080i/<name>.xml` and uses `<window id="3301" type="dialog">`.
That is the pattern this MVP follows, lowercase `default` path and all.

---

## 4. Approach

### Chosen for MVP — addon-owned `WindowXMLDialog`

The Classic TV addon ships its own XML under `resources/skins/default/1080i/` and shows it
with `xbmcgui.WindowXMLDialog("ChannelBug.xml", addon_path)`.

Why this over the alternatives:

- **Self-contained.** No skin fork, no modification of the active skin, nothing that a
  skin update will overwrite. Works identically on Estuary or any other skin.
- **Already half-built.** `channel_bug.py` is the right host; we change its content, not its
  architecture.
- **Declarative visuals.** The layout is XML — that is "skinning" in the sense that matters
  for maintenance, and a skin author can later take it over without touching Python.
- **Testable.** Labels are set via control IDs; the Python logic is unit-testable with the
  existing Kodi stubs.

### Target end-state — skin-owned overlay driven by window properties

The architecture doc's long-term direction is that look & feel becomes pure Kodi skinning,
where a skin renders an overlay that reacts to window properties. The MVP **also** sets those
properties (§7) so a skin can adopt the overlay with zero addon changes later. If/when that
happens, the addon stops showing its own dialog and the skin takes over.

### Rejected for MVP

- **Toast notification** (`Dialog().notification`): not skinning, wrong look, appears in the
  corner with system styling.
- **Skin fork**: too heavy for a 10-second ident.
- **Python-drawn overlay** (`xbmcgui.ControlLabel` on `Window(10000)`): brittle, looks wrong,
  and bypasses skin rendering.

---

## 5. Files and layout

```
addon4/
  resources/
    skins/
      default/
        1080i/
          ChannelBug.xml          ← NEW: the overlay layout
    lib/
      channel_bug.py              ← REWRITE: number+name only, 10s, XML-backed
      constants.py                ← CHANNEL_BUG_DISPLAY_SECONDS = 10
      player.py                   ← show on tune; close previous on new tune
  service.py                      ← construct + wire the bug
tests4/
  test_channel_bug.py             ← NEW: unit tests
```

Only `1080i` is shipped. Kodi scales it to the device's resolution; 720p can be added later
if the layout looks soft, but it is not required for the MVP.

---

## 6. The XML

`resources/skins/default/1080i/ChannelBug.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<window id="3302" type="dialog">
  <coordinates>
    <!-- Bottom-right, small footprint: limits CRT phosphor exposure -->
    <origin x="1680" y="900" />
  </coordinates>
  <controls>
    <control type="image">
      <posx>0</posx><posy>0</posy><width>360</width><height>120</height>
      <texture border="8">white.png</texture>
      <colordiffuse>66000000</colordiffuse>   <!-- ~40% black, low phosphor load -->
    </control>

    <!-- Control 5000: channel number, large -->
    <control type="label">
      <description>Channel number</description>
      <posx>24</posx><posy>20</posy><width>160</width><height>80</height>
      <font>font13</font>
      <align>left</align>
      <textcolor>FFFFFFFF</textcolor>
    </control>

    <!-- Control 5001: channel name -->
    <control type="label">
      <description>Channel name</description>
      <posx>200</posx><posy>44</posy><width>140</width><height>40</height>
      <font>font13</font>
      <align>left</align>
      <textcolor>FFCCCCCC</textcolor>
    </control>
  </controls>
</window>
```

Notes:

- `<window id="3302">` — an addon dialog id in the same range the Jellyfin addon uses (3301).
  **Verify no collision** with other addons before shipping; change if needed.
- Control IDs **5000** (number) and **5001** (name) are the contract between Python and XML.
  A skin author adopting the overlay must use the same IDs, or the Python must be updated.
- Background is a tinted `white.png` at ~40% opacity — small, dim, cornered, to keep the
  CRT burn-in concern (see §9) minimal.
- Fonts/positions are rough; the implementer tunes them on the actual CRT.

---

## 7. Python design

### `channel_bug.py` (rewritten)

```python
class ChannelBug(xbmcgui.WindowXMLDialog):
    def __init__(self, addon_path: str) -> None:
        self._addon_path = addon_path
        self._timer: threading.Timer | None = None

    def show_for(self, number: int, name: str) -> None:
        # One overlay at a time: close any previous instance first.
        self.set_label(5000, str(number) if number > 0 else "CH")
        self.set_label(5001, name)
        _set_window_properties(number, name)   # §7.1
        try:
            self.show()
        except Exception as exc:
            log warning; return                 # missing/broken XML must not crash tuning
        self._restart_timer(CHANNEL_BUG_DISPLAY_SECONDS)

    def _on_timeout(self) -> None:
        self._timer = None
        self._clear_window_properties()
        try: super().close()
        except Exception: pass

    def onAction(self, action) -> None:   # never consume input
        super().onAction(action)
```

- **Show immediately after issuing the play command**, not on `onPlayBackStarted`. This shows
  the ident during stream bring-up and does not depend on callbacks. 10 s from issue means the
  overlay is visible through buffering and a few seconds of playback, then hides.
- Dismissal is owned by Python (`threading.Timer`), not the XML, so the timing is deterministic
  and unit-testable. (This matches the existing `channel_static.py` pattern.)
- `show_for()` is idempotent per tune: calling it again closes/reuses the window and restarts
  the timer.

### `player.py` wiring

- `_change_channel()` success path (after `play_slot(slot)`): look up the `Channel` by id in
  `ChannelList` (it already has `id`, `name`, `number`) and call
  `self._channel_bug.show_for(channel.number, channel.name)` if `_channel_bug` is set.
- `startup_play()` success path: same call (startup is a tune).
- `watchdog_recover()`: **no call** — same channel, no ident.
- Failure path (`Channel unavailable`): no call.
- Channel number: server-authoritative when > 0, positional fallback — that logic lives in
  `channels.py` already, so `Channel.number` is the right value to display.

### `service.py` wiring

Construct the bug once at startup and assign `player._channel_bug` (the collaborator is
already reserved on the player). Because `WindowXMLDialog` needs the addon path, use the
existing pattern of `xbmcaddon.Addon().getAddonInfo("path")`. Guard construction in
try/except so a bad XML never prevents the addon from starting.

### Settings (optional for MVP)

- `show_channel_bug` (bool, default true)
- `channel_bug_seconds` (int, default 10, range 3–20)
If skipped, hard-code 10 s via `constants.py`.

---

## 7.1 Window-property contract (enables the skin-owned end-state)

Set on every tune, cleared when the bug hides:

```
Window(10000).Property("ClassicTV.ChannelNumber")   = "12"
Window(10000).Property("ClassicTV.ChannelName")     = "Cartoon Network"
Window(10000).Property("ClassicTV.BugVisible")      = "1"   ("" when hidden)
```

A skin can render its own ident from these labels with **no addon changes** — this is the
future path from §4. Even if the addon-owned dialog is kept, these properties are cheap and
useful for debugging/skins.

---

## 8. Behaviour rules (acceptance criteria)

1. On `ch_up`, `ch_down`, digit tune, and startup: an overlay showing the new channel's
   number and name appears, and hides itself after ~10 s.
2. Overlay appears within ~200 ms of the play command being issued.
3. Rapid channel changes: only one overlay is ever visible; each change updates its text and
   restarts the 10 s timer.
4. EOF/watchdog recovery of the **same** channel does **not** show the overlay.
5. The overlay never consumes remote input — channel up/down/digits keep working while it is
   visible.
6. Playback is never blocked by the overlay (`show()`, not `doModal()`).
7. If the skin XML is missing or fails to load, the addon logs a WARNING and continues; tuning
   is unaffected.
8. Window properties are set while visible and cleared when hidden.

---

## 9. Burn-in — RESOLVED (10 s approved)

**Decision (2026-09-20): 10 seconds is approved.**

The existing `CHANNEL_BUG_DISPLAY_SECONDS = 6` is documented as hard CRT burn-in protection
for the Sony KV-34HS510, and `channel_bug.py` says "do not increase". The owner has confirmed
that 10 s is fine: the KV-34HS510 is a high-quality Sony WEGA 34" HD CRT, and 10 s of this
overlay is a brief exposure with no meaningful phosphor-retention risk.

Accordingly:

- `CHANNEL_BUG_DISPLAY_SECONDS` becomes **10**.
- The "do not increase" comment in `constants.py` is rewritten to state the approved value
  and the design constraints that keep it safe (small footprint, low opacity, corner, off-white
  text — see below).
- The design still uses a small footprint (360×120), a corner position, a ~40% opacity
  background, and off-white (not peak-white) text, so the exposure stays low regardless.

---

## 10. Test plan

**Unit (`tests4/test_channel_bug.py`, Kodi stubbed):**

- `show_for()` sets control 5000 to the number string and 5001 to the name.
- `show_for()` sets the three window properties; timeout clears them.
- `show_for()` twice cancels the first timer and starts a new one (only one pending).
- Missing/failing `show()` does not raise out of `show_for()`.
- `onAction()` delegates to super (does not consume).
- Player wiring: `_change_channel` success calls `show_for` with the right number/name;
  `watchdog_recover` does **not**; failed change does **not**.

**Device (manual, on the CRT):**

- Tune up/down: ident appears, reads correctly, disappears at ~10 s.
- Digit tune: same.
- Rapid 5 ch_up: text follows, one ident, timer restarts.
- Press ch_up while the ident is visible: still changes channel (no input capture).
- Let an episode end: watchdog recovery does not flash the ident.
- Temporarily rename the XML: addon still tunes, logs a warning.

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| Window id 3302 collides with another addon | Check installed addons' XML; pick a free id. |
| Dialog captures focus/input | Acceptance #5; if it does, switch to `WindowXML` or the property-only skin path. |
| `threading.Timer` closes a GUI window off the main thread | Established codebase pattern (`channel_static.py`); wrap in try/except and confirm on device. |
| XML not found on CoreELEC (path case) | Mirror the Jellyfin addon exactly: `resources/skins/default/1080i/`. |
| 10 s burn-in | §9 decision; small/dim/corner design. |
| Number/first-frame timing | Show on command issue, not on callback; verify visually. |

---

## 12. Implementation order

1. Set `CHANNEL_BUG_DISPLAY_SECONDS = 10` (approved, §9) and update the rationale comment.
2. Ship `resources/skins/default/1080i/ChannelBug.xml` (control IDs 5000/5001).
3. Rewrite `channel_bug.py` for number+name only, XML-backed, with the property contract.
4. Wire construction in `service.py` and show on tune in `player.py`.
5. Add `tests4/test_channel_bug.py`; run `python3 -m pytest tests4/ -q`.
6. Deploy with `deploy/deploy-sweet-potato-4.sh`, restart Kodi, verify on the CRT.
7. Delete the unused show-title/color plumbing from `ChannelBug` (or leave it as a later
   enhancement once the MVP is proven).

**Out of scope, next after MVP:** show title + accent color, channel static/snow, and the
skin-owned property-only overlay (Tier 5 §5.1).

---

## 13. Implementation notes (2026-09-20)

The MVP was implemented and is live (`plugin.video.classictv` 4.2.0, tags v4.2.0–v4.2.2).
Three non-obvious pitfalls were hit and fixed; anyone touching this should know them.

1. **`WindowXMLDialog` needs explicit skin/res.** Construction must be
   `ChannelBug("ChannelBug.xml", addon_path, "default", "1080i")`. Without the
   `("default", "1080i")` pair Kodi resolves under `resources/skins/Default/...`, which does
   not match the shipped lowercase `default/` on a case-sensitive filesystem, and raises
   `XML File for Window is missing`. Construction is centralised in
   `channel_bug.create_channel_bug()`, with a regression test asserting the exact args.

2. **Don't set labels with `getControl()` before the window is shown.** A
   `WindowXMLDialog`'s controls are not addressable by id until it has been initialised, so
   `getControl(5000).setLabel()` raised `Non-Existent Control 5000`. The labels instead bind
   to `Window(Home)` properties:
   ```xml
   <label>$INFO[Window(Home).Property(ClassicTV.ChannelNumber)]</label>
   ```
   and `show_for()` publishes `ClassicTV.ChannelNumber` / `ClassicTV.ChannelName`. This also
   updates live if `show_for()` is called while the overlay is already visible.

3. **`_timer` must be initialised in `__init__`, not `onInit`.** `onInit` is not called
   synchronously by `show()`, so arming the timer immediately after `show()` raised
   `'ChannelBug' object has no attribute '_timer'`. `__init__` forwards the XML/skin/res args
   to Kodi (the Jellyfin dialog pattern) and sets `_timer` there.

Rendering fixes vs the original XML: the addon ships its own `media/white.png` (there is no
`$BUILTIN[Black]`; solid fills are `colordiffuse` on a real texture), and the panel is
anchored bottom-right with `<right>`/`<bottom>` plus a full-bleed transparent background,
because Kodi dialogs are full-screen and ignore `<coordinates>`.

Verified on device: `Loading skin file: .../ChannelBug.xml`, then
`[ClassicTV][channel_bug] show_for() ch=15 name='Sitcoms'`, then `auto-dismiss after 10s`.
Still to confirm visually: bottom-right placement and that the dialog does not capture
remote input (if it does, fall back to the property-only skin path).
