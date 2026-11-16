"""
Direct digit channel entry state machine for Classic TV 3.0.

Implements the IDLE → ACCUMULATING → (CONFIRMED | TIMEOUT | CANCELLED)
state machine described in the design spec.  When the viewer presses
number keys, this module accumulates up to CHANNEL_ENTRY_MAX_DIGITS digits,
displays the partial entry as "CH N_", and either confirms automatically
after CHANNEL_ENTRY_TIMEOUT_SECONDS of inactivity or immediately when ENTER
is pressed (Requirements 14.1–14.8).

The on-screen "CH N_" display is delegated to an optional *display* object
(the ChannelBug dialog: ``show_text(label, name, seconds)`` / ``hide()``). With
no display, or if it raises, entry still works and only logs.
"""

import threading
from typing import Callable, Optional

import xbmc

from constants import (
    CHANNEL_ENTRY_AMBIGUOUS_TIMEOUT_SECONDS,
    CHANNEL_ENTRY_MAX_DIGITS,
    CHANNEL_ENTRY_TIMEOUT_SECONDS,
    CHANNEL_NOT_FOUND_DISPLAY_SECONDS,
)

_LOG_PREFIX = "[ClassicTV][channel_entry]"


class ChannelEntry:
    """Direct digit channel entry state machine.

    Accepts individual digit keypresses, accumulates them into a channel
    number, and calls *on_channel_change* when the entry is confirmed either
    explicitly (ENTER pressed) or implicitly (1.5 s inactivity timeout).

    When the accumulated number does not match any channel, displays a
    "CH N — Not Found" message for CHANNEL_NOT_FOUND_DISPLAY_SECONDS before
    returning to IDLE.

    Parameters
    ----------
    channel_list:
        Any object that implements ``get_channel_by_number(number: int)``.
        Typically a ``ChannelList`` instance.
    on_channel_change:
        Callback invoked with the resolved ``Channel`` object when a valid
        channel number is confirmed.
    display:
        Optional overlay with ``show_text(label, name, seconds)`` and
        ``hide()``. Failures are logged and never affect entry.
    on_route_event:
        Optional callback invoked with the triggering action when a
        non-digit, non-confirm, non-back key press cancels an in-progress
        digit entry.  Allows the caller to re-dispatch the event to its
        normal handler.  Defaults to a no-op.
    """

    def __init__(
        self,
        channel_list,
        on_channel_change: Callable,
        on_route_event: Optional[Callable] = None,
        display=None,
    ) -> None:
        self._display = display
        self._channel_list = channel_list
        self._on_channel_change = on_channel_change
        self._on_route_event: Callable = on_route_event or (lambda action: None)

        self._accumulated: str = ""
        self._timer: Optional[threading.Timer] = None
        self._state: str = "IDLE"  # "IDLE" | "ACCUMULATING"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def handle_digit(self, digit: int) -> None:
        """Called when a number key 0–9 is pressed.

        Requirement 14.1: start accumulation on first digit.
        Requirement 14.2: append subsequent digits (up to max); reset timer.
        Requirement 14.5: silently ignore keypresses beyond max digits.
        """
        if self._state == "IDLE":
            self._accumulated = str(digit)
            self._state = "ACCUMULATING"
            self._reset_timer()
            self._show_overlay()
        elif self._state == "ACCUMULATING":
            if len(self._accumulated) < CHANNEL_ENTRY_MAX_DIGITS:
                self._accumulated += str(digit)
                self._reset_timer()
                self._update_overlay()
            # At max digits: silently ignore additional keypresses.
            # Requirement 14.5: do not cancel or confirm on overflow.

    def handle_confirm(self) -> None:
        """Called when ENTER/OK is pressed.

        Immediately confirms the accumulated number (Requirement 14.3).
        No-op when in IDLE state.
        """
        if self._state == "ACCUMULATING":
            xbmc.log(
                f"{_LOG_PREFIX} confirm pressed with accumulated='{self._accumulated}'",
                xbmc.LOGINFO,
            )
            self._confirm()

    def handle_back(self) -> None:
        """Called when BACK/EXIT is pressed.

        Cancels digit accumulation and clears the overlay without routing the
        event (Requirement 14.4).  No-op when in IDLE state.
        """
        if self._state == "ACCUMULATING":
            xbmc.log(
                f"{_LOG_PREFIX} back pressed — cancelling entry for '{self._accumulated}'",
                xbmc.LOGINFO,
            )
            self._cancel_timer()
            self._accumulated = ""
            self._state = "IDLE"
            self._clear_overlay()

    def handle_other(self, action=None) -> None:
        """Called when any non-digit, non-confirm, non-back button is pressed.

        If currently ACCUMULATING, cancels the timer, clears the overlay,
        returns to IDLE, and routes the event to *on_route_event* so the
        caller can handle it normally (Requirement 14.4).  No-op when IDLE.
        """
        if self._state == "ACCUMULATING":
            xbmc.log(
                f"{_LOG_PREFIX} non-digit key — cancelling entry for '{self._accumulated}'",
                xbmc.LOGINFO,
            )
            self._cancel_timer()
            self._accumulated = ""
            self._state = "IDLE"
            self._clear_overlay()
            self._on_route_event(action)

    def is_accumulating(self) -> bool:
        """Return True while in ACCUMULATING state (Requirement 14.6)."""
        return self._state == "ACCUMULATING"

    # ------------------------------------------------------------------
    # Timer management
    # ------------------------------------------------------------------

    def _reset_timer(self) -> None:
        """Cancel any existing inactivity timer and start a fresh one.

        The timer fires _on_timeout after CHANNEL_ENTRY_TIMEOUT_SECONDS.
        The timer thread is daemon so it does not prevent process exit.
        """
        self._cancel_timer()
        timeout = CHANNEL_ENTRY_TIMEOUT_SECONDS
        # Give the viewer longer while a longer channel number is still
        # reachable ("1" could become 12); the channel list may not know.
        longer = getattr(self._channel_list, "has_longer_number", None)
        if (
            longer is not None
            and len(self._accumulated) < CHANNEL_ENTRY_MAX_DIGITS
            and longer(self._accumulated)
        ):
            timeout = CHANNEL_ENTRY_AMBIGUOUS_TIMEOUT_SECONDS
        self._timer = threading.Timer(
            timeout,
            self._on_timeout,
        )
        self._timer.daemon = True
        self._timer.start()

    def _cancel_timer(self) -> None:
        """Cancel and discard the inactivity timer if one is pending."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _on_timeout(self) -> None:
        """Fires when the inactivity timer expires (Requirement 14.2).

        Treated identically to an explicit confirm press.  Called on the
        timer's daemon thread.
        """
        xbmc.log(
            f"{_LOG_PREFIX} inactivity timeout for accumulated='{self._accumulated}'",
            xbmc.LOGINFO,
        )
        # Nullify timer reference first; _confirm() calls _cancel_timer()
        # which would otherwise try to cancel an already-fired timer.
        self._timer = None
        self._confirm()

    # ------------------------------------------------------------------
    # Confirmation logic
    # ------------------------------------------------------------------

    def _confirm(self) -> None:
        """Resolve the accumulated number and dispatch the result.

        Requirement 14.7: if the number matches a channel, call
        on_channel_change.  Requirement 14.8: if not found, show the
        not-found message for CHANNEL_NOT_FOUND_DISPLAY_SECONDS.

        Returns to IDLE before dispatching the callback so that the callback
        may safely call handle_digit() to begin a new entry.
        """
        self._cancel_timer()
        number = int(self._accumulated) if self._accumulated else 0
        self._accumulated = ""
        self._state = "IDLE"
        self._clear_overlay()

        channel = self._channel_list.get_channel_by_number(number)
        if channel:
            xbmc.log(
                f"{_LOG_PREFIX} confirmed channel {number} → '{channel.name}'",
                xbmc.LOGINFO,
            )
            self._on_channel_change(channel)
        else:
            xbmc.log(
                f"{_LOG_PREFIX} CH {number} — Not Found",
                xbmc.LOGWARNING,
            )
            self._show_not_found(number)
            not_found_timer = threading.Timer(
                CHANNEL_NOT_FOUND_DISPLAY_SECONDS,
                self._clear_overlay,
            )
            not_found_timer.daemon = True
            not_found_timer.start()

    # ------------------------------------------------------------------
    # Overlay display
    # ------------------------------------------------------------------

    def _draw(
        self,
        label: str,
        name: str = "",
        seconds: float = 0,
        title: str = "",
        badge: str = "",
    ) -> None:
        if self._display is None:
            return
        try:
            self._display.show_text(
                label, name, seconds, title=title, badge=badge
            )
        except TypeError:
            try:
                self._display.show_text(label, name, seconds)
            except Exception as exc:
                xbmc.log(
                    f"{_LOG_PREFIX} display.show_text raised: {exc}",
                    xbmc.LOGWARNING,
                )
        except Exception as exc:
            xbmc.log(
                f"{_LOG_PREFIX} display.show_text raised: {exc}",
                xbmc.LOGWARNING,
            )

    def _get_overlay_info(self) -> tuple[str, str, str]:
        """Resolve (name, title, badge) for the current accumulated digits."""
        channel = None
        if self._accumulated.isdigit():
            channel = self._channel_list.get_channel_by_number(int(self._accumulated))

        longer = getattr(self._channel_list, "has_longer_number", None)
        is_ambiguous = bool(longer is not None and longer(self._accumulated))

        if channel:
            name = channel.name
            badge = "DIRECT" if is_ambiguous else "TUNE"
            title = "Press ENTER to tune"
        else:
            if is_ambiguous:
                name = "Direct Channel Entry"
                badge = "DIRECT"
                title = "Enter next digit"
            else:
                name = "Not Found"
                badge = "NOT FOUND"
                title = "Press BACK to cancel"

        return name, title, badge

    def _show_overlay(self) -> None:
        """Show the initial digit entry overlay with a trailing cursor."""
        xbmc.log(f"{_LOG_PREFIX} showing: CH {self._accumulated}_", xbmc.LOGINFO)
        name, title, badge = self._get_overlay_info()
        self._draw(
            f"{self._accumulated}_",
            name,
            CHANNEL_ENTRY_AMBIGUOUS_TIMEOUT_SECONDS + 1,
            title=title,
            badge=badge,
        )

    def _update_overlay(self) -> None:
        """Update the overlay to reflect the current accumulated digits."""
        xbmc.log(f"{_LOG_PREFIX} update: CH {self._accumulated}_", xbmc.LOGINFO)
        name, title, badge = self._get_overlay_info()
        self._draw(
            f"{self._accumulated}_",
            name,
            CHANNEL_ENTRY_AMBIGUOUS_TIMEOUT_SECONDS + 1,
            title=title,
            badge=badge,
        )

    def _clear_overlay(self) -> None:
        """Hide the digit entry / not-found overlay."""
        xbmc.log(f"{_LOG_PREFIX} clearing overlay", xbmc.LOGINFO)
        if self._display is None:
            return
        try:
            self._display.hide()
        except Exception as exc:
            xbmc.log(f"{_LOG_PREFIX} display.hide raised: {exc}", xbmc.LOGWARNING)

    def _show_not_found(self, number: int) -> None:
        """Display the not-found message for the given channel number."""
        xbmc.log(f"{_LOG_PREFIX} CH {number} — Not Found", xbmc.LOGWARNING)
        self._draw(
            str(number),
            "Not Found",
            CHANNEL_NOT_FOUND_DISPLAY_SECONDS,
            title="No channel at this number",
            badge="NOT FOUND",
        )
