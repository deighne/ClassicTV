"""
Channel Bug overlay for Classic TV 4.0.

A `WindowXMLDialog` that shows channel identity information (channel number
and channel name) briefly after a deliberate tune action, then auto-dismisses.

The overlay is backed by `ChannelBug.xml` in the addon skin directory. If the
skin file is missing, or if Kodi raises on `show()` or `getControl()`, the
failure is logged at LOGWARNING and tuning continues normally.

CRT phosphor burn-in protection: the overlay is small (360×120 px), positioned
in the lower-right corner, uses ~40% opacity background, and off-white text.
See `CHANNEL_BUG_DISPLAY_SECONDS` in constants.py.
"""

import threading
import time

import xbmc
import xbmcgui

from constants import (
    CHANNEL_BUG_DISPLAY_SECONDS,
    CHANNEL_STATIC_FRAME_COUNT,
    CHANNEL_STATIC_FRAME_INTERVAL_SECONDS,
    CHANNEL_STATIC_MAX_SECONDS,
    CHANNEL_STATIC_MIN_SECONDS,
    CHANNEL_STATIC_POST_AV_SECONDS,
)

_LOG_PREFIX = "[ClassicTV][channel_bug]"

# Kodi resolves an addon-owned dialog under
# <addon>/resources/skins/<skin>/<res>/<file>. The shipped directory is
# lowercase "default", so the skin/res must be passed explicitly — omit them and
# Kodi looks under "Default"/"720p" and raises "XML File for Window is missing".
_XML_FILENAME = "ChannelBug.xml"
_SKIN = "default"
_RES = "1080i"


# Kodi action ids (xbmcgui.ACTION_*) for the keys Classic TV handles, mapped to
# the NotifyAll method names the keymap sends in FullscreenVideo.
_ACTION_TO_METHOD = {
    **{58 + n: f"num_{n}" for n in range(10)},  # ACTION_NUMBER_0..9
    **{140 + n: f"num_{n}" for n in range(2, 10)},  # ACTION_JUMP_SMS2..9 (global remote.xml)
    5: "ch_up",  # ACTION_PAGE_UP   (<pageplus>)
    6: "ch_down",  # ACTION_PAGE_DOWN (<pageminus>)
    7: "confirm",  # ACTION_SELECT_ITEM (<select> / OK)
    18: "guide",  # ACTION_SHOW_GUI (<guide>)
    10: "back",  # ACTION_PREVIOUS_MENU
    92: "back",  # ACTION_NAV_BACK
    110: "back",  # ACTION_BACKSPACE
    1: "left",  # ACTION_MOVE_LEFT
    2: "right",  # ACTION_MOVE_RIGHT
    3: "up",  # ACTION_MOVE_UP
    4: "down",  # ACTION_MOVE_DOWN
}


# ---------------------------------------------------------------------------
# Window property helpers (module-level)
# ---------------------------------------------------------------------------


def _format_channel_display(number_label: str) -> str:
    """Format the channel number for the Sony Trinitron OSD header (e.g. 'CH 04')."""
    raw = (number_label or "").strip()
    if not raw or raw.upper() == "CH":
        return "CH"
    if raw.isdigit():
        return f"CH {int(raw):02d}"
    if raw.upper().startswith("CH"):
        return raw
    return f"CH {raw}"


def _set_window_properties(
    number_label: str,
    name: str,
    title: str | None = None,
    badge: str | None = None,
    persist_active: bool = True,
) -> None:
    """Set ClassicTV overlay state properties on the Kodi home window.

    The XML labels bind to these, and skins/other addons can read them too.
    Derived from Window(10000), i.e. Window(Home).
    """
    w = xbmcgui.Window(10000)
    w.setProperty("ClassicTV.ChannelNumber", number_label)
    w.setProperty("ClassicTV.ChannelDisplay", _format_channel_display(number_label))
    w.setProperty("ClassicTV.ChannelName", name)
    if title is not None:
        w.setProperty("ClassicTV.NowPlaying", title)
    if badge is not None and badge != "":
        w.setProperty("ClassicTV.OSDBadge", badge)
    elif name == "Not Found":
        w.setProperty("ClassicTV.OSDBadge", "NOT FOUND")
    elif number_label.endswith("_"):
        w.setProperty("ClassicTV.OSDBadge", "DIRECT")
    else:
        w.setProperty("ClassicTV.OSDBadge", "STEREO")
    w.setProperty("ClassicTV.BugVisible", "1")
    if persist_active and not number_label.endswith("_") and name != "Not Found":
        w.setProperty("ClassicTV.ActiveChannelNumber", number_label)
        w.setProperty("ClassicTV.ActiveChannelName", name)


def _set_static_properties(frame_index: int | None) -> None:
    """Set or clear ClassicTV CRT static properties on Window(10000)."""
    w = xbmcgui.Window(10000)
    if frame_index is None:
        w.setProperty("ClassicTV.StaticVisible", "")
        w.setProperty("ClassicTV.StaticFrame", "")
    else:
        w.setProperty("ClassicTV.StaticVisible", "1")
        w.setProperty("ClassicTV.StaticFrame", str(frame_index))


def _clear_window_properties() -> None:
    """Clear transient ClassicTV overlay state properties on the Kodi home window.

    Called when the overlay is dismissed (timer expiry or show() failure).
    Note: ClassicTV.ActiveChannelNumber and ClassicTV.ActiveChannelName are
    intentionally preserved so external monitors (status.json) can always report
    which channel is currently tuned after the 10-second OSD bug auto-dismisses.
    """
    w = xbmcgui.Window(10000)
    w.setProperty("ClassicTV.ChannelNumber", "")
    w.setProperty("ClassicTV.ChannelDisplay", "")
    w.setProperty("ClassicTV.ChannelName", "")
    w.setProperty("ClassicTV.NowPlaying", "")
    w.setProperty("ClassicTV.OSDBadge", "")
    w.setProperty("ClassicTV.BugVisible", "")
    w.setProperty("ClassicTV.StaticVisible", "")
    w.setProperty("ClassicTV.StaticFrame", "")


# ---------------------------------------------------------------------------
# ChannelBug
# ---------------------------------------------------------------------------


class ChannelBug(xbmcgui.WindowXMLDialog):
    """Non-blocking channel-info & CRT static overlay (Requirements 1.1–1.4).

    Shows the channel number and name for `CHANNEL_BUG_DISPLAY_SECONDS`, then
    auto-closes via a daemon `threading.Timer`. Calling `show_for()` while the
    overlay is already visible cancels the previous timer and restarts it
    (idempotent).

    Also drives full-screen CRT snow (`show_static()` / `hide_static()`) during
    channel transitions by setting `Window(Home).Property(ClassicTV.StaticVisible)`
    while Kodi's C++ GLES skin engine animates `static_tile.png` on the GPU.

    Content is injected through Window(Home) properties (`ClassicTV.ChannelNumber`
    / `ClassicTV.ChannelName`), which the XML labels bind to. This avoids
    `getControl().setLabel()`, which is unreliable before the dialog window has
    been initialised.

    Prefer the :func:`create_channel_bug` factory, which supplies the required
    skin/res arguments and never raises. Direct construction must pass all four
    positional arguments::

        ChannelBug("ChannelBug.xml", addon_path, "default", "1080i")
    """

    def __init__(self, *args, **kwargs) -> None:
        # Forward the XML/skin/res arguments to Kodi, then set up our own state.
        # (Same pattern the Jellyfin dialogs use.) This must be __init__, not
        # onInit(): onInit is not called synchronously by show(), so _timer would
        # be missing when show_for() arms it.
        super().__init__(*args, **kwargs)
        self._timer: threading.Timer | None = None
        self._static_timer: threading.Timer | None = None
        self._static_stop: threading.Event = threading.Event()
        self._static_stop.set()
        self._static_shown_at: float = 0.0
        self._static_min_seconds: float = 0.0
        self._static_post_av_seconds: float = 0.0
        # Callable(method: str) set by the service; receives "num_N", "ch_up",
        # "ch_down", "confirm", "back", "guide", d-pad for keys that reach this
        # dialog (see onAction).
        self.on_key = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def show_static(
        self,
        number: int = 0,
        name: str = "",
        max_seconds: float = CHANNEL_STATIC_MAX_SECONDS,
        min_seconds: float = 0.0,
        post_av_seconds: float = 0.0,
    ) -> None:
        """Display full-screen animated CRT static immediately at tune start (t=0ms).

        Optionally pre-populates the channel number and name on the HUD so the
        viewer sees the destination channel badge over the snow immediately on
        remote keypress. Static is animated on the GPU by Kodi's C++ skin engine
        and dismissed when ``hide_static()`` is called (in ``onAVStarted``) —
        after any configured *min_seconds* / *post_av_seconds* hold — or after
        *max_seconds*.
        """
        if number > 0 or name:
            number_label = str(number) if number > 0 else "CH"
            _set_window_properties(number_label, name, title="")

        if self._static_timer is not None:
            self._static_timer.cancel()
            self._static_timer = None
        self._static_stop.set()
        stop_event = threading.Event()
        self._static_stop = stop_event
        self._static_shown_at = time.monotonic()
        self._static_min_seconds = max(0.0, float(min_seconds))
        self._static_post_av_seconds = max(0.0, float(post_av_seconds))
        _set_static_properties(0)

        try:
            self.show()
        except Exception as exc:
            xbmc.log(
                f"{_LOG_PREFIX} show_static() failed: {exc}; clearing static",
                xbmc.LOGWARNING,
            )
            stop_event.set()
            self._static_min_seconds = 0.0
            self._static_post_av_seconds = 0.0
            _set_static_properties(None)
            return

        def _tick_static() -> None:
            frame = 1
            while not stop_event.wait(CHANNEL_STATIC_FRAME_INTERVAL_SECONDS):
                _set_static_properties(frame % max(1, CHANNEL_STATIC_FRAME_COUNT))
                frame += 1

        ticker = threading.Thread(
            target=_tick_static, name="ClassicTV-StaticTick", daemon=True
        )
        ticker.start()

        if max_seconds > 0:
            self._static_timer = threading.Timer(
                max_seconds, self._finish_hide_static
            )
            self._static_timer.daemon = True
            self._static_timer.start()

    def _finish_hide_static(self) -> None:
        """Clear static window properties and close dialog if bug is not visible."""
        self._static_stop.set()
        self._static_min_seconds = 0.0
        self._static_post_av_seconds = 0.0
        if self._static_timer is not None:
            self._static_timer.cancel()
            self._static_timer = None
        _set_static_properties(None)
        if (
            self._timer is None
            and not xbmcgui.Window(10000).getProperty("ClassicTV.BugVisible")
        ):
            try:
                super().close()
            except Exception as exc:
                xbmc.log(f"{_LOG_PREFIX} hide_static() close failed: {exc}", xbmc.LOGDEBUG)

    def hide_static(self, immediate: bool = False) -> None:
        """Dismiss CRT static snow while leaving the channel bug intact.

        When *min_seconds* or *post_av_seconds* were supplied to ``show_static()``
        and *immediate* is False, schedules the dismissal after the remaining
        hold window so the Amlogic hardware decoder and CRendererAML finish
        settling behind the static snow.
        """
        if not immediate and not self._static_stop.is_set():
            elapsed = max(0.0, time.monotonic() - self._static_shown_at)
            wait = max(
                self._static_post_av_seconds,
                self._static_min_seconds - elapsed,
            )
            if wait > 0.0:
                if self._static_timer is not None:
                    self._static_timer.cancel()
                self._static_timer = threading.Timer(
                    wait, self._finish_hide_static
                )
                self._static_timer.daemon = True
                self._static_timer.start()
                xbmc.log(
                    f"{_LOG_PREFIX} holding static for {wait:.2f}s"
                    f" post-AV settle (elapsed={elapsed:.2f}s)",
                    xbmc.LOGINFO,
                )
                return
        self._finish_hide_static()

    def show_for(self, number: int, name: str, title: str | None = None) -> None:
        """Display the overlay for the given channel and restart the timer.

        Idempotent: calling again with new values cancels the previous timer,
        updates the displayed content, and restarts the 10-second countdown.

        Args:
            number: Server-assigned channel number. 0 means unassigned; the
                    string "CH" is shown instead of "0" (Requirement 1.4).
            name:   Channel display name, e.g. "NBC".
            title:  Optional program title; if None, preserves any title set by
                    play_slot().
        """
        number_label = str(number) if number > 0 else "CH"

        # Step 1: publish properties the XML labels bind to (Req 4.1)
        _set_window_properties(number_label, name, title=title)

        # Step 2: show the dialog — bail and clear properties on failure (Req 5.1)
        try:
            self.show()
        except Exception as exc:
            xbmc.log(
                f"{_LOG_PREFIX} show() failed: {exc}; clearing properties",
                xbmc.LOGWARNING,
            )
            self._static_stop.set()
            _clear_window_properties()
            return

        # Step 3: arm the auto-dismiss timer (Req 2.1, 2.2, 2.3)
        self._restart_timer()

        xbmc.log(
            f"{_LOG_PREFIX} show_for() ch={number_label} name={name!r}",
            xbmc.LOGINFO,
        )

    def _run_on_key(self, method: str) -> None:
        try:
            self.on_key(method)
        except Exception as exc:
            xbmc.log(f"{_LOG_PREFIX} on_key raised: {exc}", xbmc.LOGWARNING)

    def show_text(
        self,
        number_label: str,
        name: str = "",
        seconds: float = 0,
        title: str | None = None,
        badge: str | None = None,
    ) -> None:
        """Show arbitrary text in the ident panel (used for digit entry).

        The number label carries the text (e.g. ``"12_"``); the name label is
        optional (e.g. ``"Not Found"``). Auto-dismisses after *seconds*
        (default CHANNEL_BUG_DISPLAY_SECONDS) and restarts that countdown on
        every call, so a stream of digit presses keeps it up.
        """
        _set_window_properties(
            number_label,
            name,
            title=title if title is not None else "",
            badge=badge,
            persist_active=False,
        )
        try:
            self.show()
        except Exception as exc:
            xbmc.log(f"{_LOG_PREFIX} show_text() failed: {exc}", xbmc.LOGWARNING)
            _clear_window_properties()
            return
        self._restart_timer(seconds or CHANNEL_BUG_DISPLAY_SECONDS)

    def hide(self) -> None:
        """Dismiss the panel and static now and cancel the auto-dismiss timer."""
        self._static_stop.set()
        if self._static_timer is not None:
            self._static_timer.cancel()
            self._static_timer = None
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        _clear_window_properties()
        try:
            super().close()
        except Exception as exc:
            xbmc.log(f"{_LOG_PREFIX} hide() failed: {exc}", xbmc.LOGDEBUG)

    # ------------------------------------------------------------------
    # Timer helpers
    # ------------------------------------------------------------------

    def _restart_timer(self, seconds: float = CHANNEL_BUG_DISPLAY_SECONDS) -> None:
        """Cancel any existing timer and start a new daemon timer (Req 2.2, 2.3)."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._timer = threading.Timer(seconds, self._on_timeout)
        self._timer.daemon = True
        self._timer.start()

    def _on_timeout(self) -> None:
        """Called by the timer thread after `CHANNEL_BUG_DISPLAY_SECONDS` (Req 2.1, 2.4)."""
        self._timer = None
        self._static_stop.set()
        if self._static_timer is not None:
            self._static_timer.cancel()
            self._static_timer = None
        _clear_window_properties()
        try:
            super().close()
        except Exception as exc:
            xbmc.log(f"{_LOG_PREFIX} close() failed: {exc}", xbmc.LOGDEBUG)
        xbmc.log(
            f"{_LOG_PREFIX} auto-dismiss after {CHANNEL_BUG_DISPLAY_SECONDS}s",
            xbmc.LOGINFO,
        )

    # ------------------------------------------------------------------
    # Input passthrough (Req 3.1)
    # ------------------------------------------------------------------

    def onAction(self, action) -> None:
        """Keep the remote working while the ident or static is on screen.

        While this dialog is open Kodi resolves remote keys in the dialog's
        context, where the addon keymap's FullscreenVideo mappings do not apply,
        so digits, ch+/-, confirm, back, guide, and d-pad arrive here as plain
        actions instead of our NotifyAll messages. Translate all keys Classic TV
        cares about and hand them to ``on_key``. Every action id is logged at
        DEBUG to make key routing problems diagnosable on the device.
        """
        try:
            action_id = action.getId()
        except Exception:
            action_id = None
        xbmc.log(f"{_LOG_PREFIX} onAction id={action_id}", xbmc.LOGDEBUG)
        method = _ACTION_TO_METHOD.get(action_id)
        if method is not None and self.on_key is not None:
            # Off the GUI thread: a channel change blocks on a network fetch.
            threading.Thread(
                target=self._run_on_key, args=(method,), daemon=True
            ).start()
            return
        super().onAction(action)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_channel_bug(addon_path: str) -> "ChannelBug | None":
    """Construct a :class:`ChannelBug` with the correct skin/res arguments.

    Returns ``None`` (and logs a WARNING) if construction fails, so a bad or
    missing XML never prevents the addon from starting or tuning. Centralising
    construction here is what keeps the ``("default", "1080i")`` arguments from
    being forgotten — see the module-level note.
    """
    try:
        return ChannelBug(_XML_FILENAME, addon_path, _SKIN, _RES)
    except Exception as exc:
        xbmc.log(
            f"{_LOG_PREFIX} construction failed: {exc}; overlay disabled",
            xbmc.LOGWARNING,
        )
        return None


# ---------------------------------------------------------------------------
# Toast display for digit entry
# ---------------------------------------------------------------------------


class ToastDisplay:
    """Digit-entry display that uses Kodi notification toasts.

    A toast never takes input focus, unlike a WindowXMLDialog, so digits typed
    while it is showing still reach the keymap. Same interface as ChannelBug's
    ``show_text``/``hide`` so ChannelEntry can use either.
    """

    def show_text(
        self,
        number_label: str,
        name: str = "",
        seconds: float = 0,
        title: str | None = None,
        badge: str | None = None,
    ) -> None:
        parts = [p for p in (number_label, name, title) if p]
        text = " — ".join(parts[:2])
        xbmcgui.Dialog().notification(
            "Channel",
            text,
            xbmcgui.NOTIFICATION_INFO,
            int(min(seconds or 2, 2) * 1000),
            False,
        )

    def hide(self) -> None:
        pass  # toasts expire on their own
