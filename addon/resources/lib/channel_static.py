"""
Channel_Static overlay for Classic TV 3.0.

Displays a full-screen looping noise animation with the destination channel
number in large type during channel changes. The overlay is non-blocking
(shown via show(), not doModal()) and dismisses itself when:
  - onPlayBackStarted fires for the new channel stream (Requirement 12.6), or
  - CHANNEL_STATIC_SAFETY_TIMEOUT_SECONDS elapse without playback starting
    (Requirement 12.7).

Button presses are never consumed — onAction() unconditionally delegates to
super() so the underlying focus owner receives every event (Requirement 12.4).

Requirement 12.8: show() must be called before any schedule fetch or playback
call. This class satisfies that constraint; enforcement is the caller's
responsibility.
"""

import threading

import xbmc
import xbmcgui

from constants import CHANNEL_STATIC_SAFETY_TIMEOUT_SECONDS


class ChannelStatic(xbmcgui.WindowXMLDialog):
    """Full-screen static overlay displayed during channel changes.

    Usage::

        static = ChannelStatic(destination_channel_number=7)
        static.show()                # call before schedule fetch (Req 12.8)
        # … fetch schedule, start playback …
        # Player calls static.on_playback_started() when onPlayBackStarted fires.

    The safety timer fires automatically after CHANNEL_STATIC_SAFETY_TIMEOUT_SECONDS
    if on_playback_started() is never called.
    """

    def __init__(self, destination_channel_number: int = 0) -> None:
        # WindowXMLDialog.__init__ is called by Kodi's XML loading framework
        # automatically — do NOT forward XML path arguments here.  In the stub
        # test environment xbmcgui.WindowXMLDialog.__init__ accepts no args.
        self._destination_number = destination_channel_number
        self._safety_timer: threading.Timer | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def show(self) -> None:
        """Display the overlay non-blocking and arm the safety timeout.

        Must be called before any schedule fetch or playback call
        (Requirement 12.8).  Returns immediately; the overlay is rendered
        asynchronously by Kodi's GUI thread.
        """
        self._start_safety_timer()
        super().show()
        xbmc.log(
            f"[ClassicTV][channel_static] show() called for channel {self._destination_number}",
            xbmc.LOGINFO,
        )

    def close(self) -> None:
        """Dismiss the overlay and cancel any pending safety timer."""
        self._cancel_safety_timer()
        super().close()
        xbmc.log("[ClassicTV][channel_static] close() called", xbmc.LOGINFO)

    def on_playback_started(self) -> None:
        """Dismiss the overlay when the new channel stream starts playing.

        Called by the Player from its onPlayBackStarted() callback.  Not a
        Kodi callback on this dialog — Kodi does not call this method
        directly (Requirement 12.6).
        """
        xbmc.log(
            "[ClassicTV][channel_static] onPlayBackStarted → closing",
            xbmc.LOGINFO,
        )
        self.close()

    # ------------------------------------------------------------------
    # Kodi dialog event handlers
    # ------------------------------------------------------------------

    def onAction(self, action) -> None:  # noqa: N802  (Kodi naming convention)
        """Forward every action to the base class without consuming it.

        Requirement 12.4: while the overlay is visible, remote button presses
        must reach the underlying focus owner as if the overlay were not present.
        """
        super().onAction(action)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _start_safety_timer(self) -> None:
        """Cancel any existing timer, then start a fresh one."""
        self._cancel_safety_timer()
        self._safety_timer = threading.Timer(
            CHANNEL_STATIC_SAFETY_TIMEOUT_SECONDS,
            self._on_safety_timeout,
        )
        self._safety_timer.daemon = True
        self._safety_timer.start()

    def _cancel_safety_timer(self) -> None:
        """Cancel and discard the pending safety timer if one exists."""
        if self._safety_timer is not None:
            self._safety_timer.cancel()
            self._safety_timer = None

    def _on_safety_timeout(self) -> None:
        """Close the overlay when the safety timeout fires.

        Called on the timer's daemon thread after CHANNEL_STATIC_SAFETY_TIMEOUT_SECONDS
        have elapsed without on_playback_started() being invoked (Requirement 12.7).

        Calls super().close() directly rather than self.close() to avoid
        attempting to cancel the timer that is currently executing.
        """
        xbmc.log(
            f"[ClassicTV][channel_static] safety timeout after "
            f"{CHANNEL_STATIC_SAFETY_TIMEOUT_SECONDS}s — closing",
            xbmc.LOGWARNING,
        )
        # Nullify the reference first so a concurrent close() call skips
        # cancel() on a timer that has already fired.
        self._safety_timer = None
        super().close()
