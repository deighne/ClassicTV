"""
Channel Bug overlay for Classic TV 3.0.

A `WindowXMLDialog` that shows channel identity information (channel number,
channel name, current show title, and the channel's accent color) briefly
after tuning, then auto-dismisses.

The 6-second display limit is a hard requirement — CRT phosphor burn-in
protection for the Sony KV-34HS510.  See Requirement 13.6 and the rationale
comment in constants.py for the `CHANNEL_BUG_DISPLAY_SECONDS` constant.
"""

import threading

import xbmc
import xbmcgui

from constants import CHANNEL_BUG_DISPLAY_SECONDS


class ChannelBug(xbmcgui.WindowXMLDialog):
    """Non-blocking channel-info overlay (Requirement 13.1).

    Displays channel number (≥48pt equivalent), channel name, current show
    title, and the channel's configured accent color (Requirement 13.2).
    Auto-closes after `CHANNEL_BUG_DISPLAY_SECONDS` via a daemon
    `threading.Timer` (Requirement 13.3).

    Usage:
        bug = create_channel_bug(5, "Nickelodeon", "Hey Arnold", "#ff6600")
        if bug:
            bug.show()
        # …later, on channel change…
        if bug:
            bug.close()
    """

    def __init__(
        self,
        channel_number: int,
        channel_name: str,
        show_title: str,
        channel_color: str,
    ) -> None:
        # WindowXMLDialog.__init__ is handled by the Kodi C-extension machinery;
        # calling super().__init__() here would invoke WindowXMLDialog's default
        # constructor which expects xml_filename, addon_path, etc.  The Kodi
        # binding builds those from the addon manifest at display time, so we
        # skip the call and store our own state directly.
        self._channel_number = channel_number
        self._channel_name = channel_name
        self._show_title = show_title
        self._channel_color = channel_color
        self._dismiss_timer: threading.Timer | None = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def show(self) -> None:
        """Display the overlay and start the auto-dismiss timer (Req 13.1, 13.3)."""
        super().show()
        self._start_dismiss_timer()
        xbmc.log(
            f"[ClassicTV][channel_bug] show() ch={self._channel_number}"
            f" title={self._show_title!r}",
            xbmc.LOGINFO,
        )

    def close(self) -> None:
        """Cancel any pending timer and close the overlay (Req 13.5)."""
        self._cancel_dismiss_timer()
        super().close()
        xbmc.log("[ClassicTV][channel_bug] close() called", xbmc.LOGINFO)

    def onAction(self, action) -> None:
        """Delegate all remote actions to the parent so that button presses
        reach playback controls as if the overlay were not present (Req 13.4).
        """
        super().onAction(action)

    # ------------------------------------------------------------------
    # Timer helpers
    # ------------------------------------------------------------------

    def _start_dismiss_timer(self) -> None:
        """Start (or restart) the auto-dismiss timer (Req 13.3, 13.6)."""
        self._cancel_dismiss_timer()
        try:
            self._dismiss_timer = threading.Timer(
                CHANNEL_BUG_DISPLAY_SECONDS,  # CRT phosphor burn-in protection — do not increase
                self._on_dismiss_timeout,
            )
            self._dismiss_timer.daemon = True
            self._dismiss_timer.start()
        except Exception as exc:
            xbmc.log(
                f"[ClassicTV][channel_bug] timer mechanism failed: {exc};"
                " closing immediately",
                xbmc.LOGWARNING,
            )
            # Fallback: close immediately if timer creation fails (Req 13.6)
            super().close()

    def _cancel_dismiss_timer(self) -> None:
        """Cancel and discard any outstanding dismiss timer."""
        if self._dismiss_timer is not None:
            self._dismiss_timer.cancel()
            self._dismiss_timer = None

    def _on_dismiss_timeout(self) -> None:
        """Called by the timer thread after `CHANNEL_BUG_DISPLAY_SECONDS` (Req 13.3)."""
        xbmc.log(
            f"[ClassicTV][channel_bug] auto-dismiss after {CHANNEL_BUG_DISPLAY_SECONDS}s",
            xbmc.LOGINFO,
        )
        # Use super().close() directly: self.close() would try to cancel the
        # timer that just fired, which is harmless but unnecessary.
        super().close()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_channel_bug(
    channel_number: int,
    channel_name: str,
    show_title: str,
    channel_color: str,
) -> "ChannelBug | None":
    """Safely instantiate a `ChannelBug`, returning ``None`` on failure.

    If instantiation raises for any reason (e.g., Kodi WindowXMLDialog
    constructor error in a future Kodi version), this function logs a WARNING
    and returns ``None`` so the caller can continue without the overlay
    (Requirement 13.1).
    """
    try:
        return ChannelBug(channel_number, channel_name, show_title, channel_color)
    except Exception as exc:
        xbmc.log(
            f"[ClassicTV][channel_bug] failed to instantiate ChannelBug: {exc}",
            xbmc.LOGWARNING,
        )
        return None
