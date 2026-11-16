"""
TV Guide overlay for Classic TV 3.0.

Opens a full-screen channel guide when the Guide button is pressed. Uses
`xbmcgui.Dialog().select()` for maximum Kodi compatibility — no XML skin
required. Guide data is cached in memory for 30 seconds so rapid re-opens
are zero-latency (Requirement 16.4).

Design notes
------------
- `toggle()` is the single public entry point. The service.py dispatch
  handler calls it on every Guide button press.
- The overlay appears as a blocking `select()` dialog; the caller (service.py)
  is unblocked as soon as the dialog closes.
- A single `/guide?t={unix_timestamp}` request populates all channels in one
  fetch — no per-channel requests (Requirement 16.2).
- All list items are built in memory and passed to the dialog in a single
  call, not added one by one (Requirement 16.5 / Dialog.select() API).
- Fetch failure or 3-second timeout shows an error notification and returns
  without selecting a channel (Requirement 16.8).
"""

import json
import time
import urllib.error
import urllib.request

import xbmc
import xbmcgui

from constants import GUIDE_CACHE_TTL_SECONDS, GUIDE_FETCH_TIMEOUT_SECONDS

_LOG_PREFIX = "[ClassicTV][guide]"


class TVGuide:
    """TV Guide overlay backed by the Schedule_Service `/guide` endpoint.

    Parameters
    ----------
    schedule_service_url:
        Base URL of the running Schedule_Service, e.g.
        ``"http://192.168.1.207:8300"``.  No trailing slash.
    channel_change_callback:
        Callable invoked with a single ``channel_id: str`` argument when the
        viewer selects a channel.  The guide will close *before* calling this
        (Requirement 16.6).

    Example usage::

        guide = TVGuide("http://localhost:8300", player.change_channel)
        # Bound to the Guide button via service.py:
        guide.toggle()
    """

    def __init__(
        self,
        schedule_service_url: str,
        channel_change_callback,
    ) -> None:
        self._url = schedule_service_url
        self._on_channel_change = channel_change_callback
        self._cache_data: list | None = None
        self._cache_time: float = 0.0
        self._is_visible: bool = False

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def toggle(self) -> None:
        """Show or dismiss the guide.

        If the guide is currently open, close it. Otherwise open it.
        The guide opens immediately; data is fetched synchronously before the
        select() dialog appears. If cached data is fresh, no network request
        is made (Requirement 16.4).
        """
        if self._is_visible:
            xbmc.log(f"{_LOG_PREFIX} toggle() — guide already visible, closing", xbmc.LOGINFO)
            self._close()
            return
        self._open()

    # ------------------------------------------------------------------
    # Internal open / close
    # ------------------------------------------------------------------

    def _open(self) -> None:
        """Decide whether to use cache or fetch fresh data, then display."""
        now = time.time()
        if (
            self._cache_data is not None
            and (now - self._cache_time) < GUIDE_CACHE_TTL_SECONDS
        ):
            xbmc.log(
                f"{_LOG_PREFIX} using cached guide data "
                f"(age={now - self._cache_time:.1f}s)",
                xbmc.LOGINFO,
            )
            self._display(self._cache_data)
        else:
            self._fetch_and_display()

    def _close(self) -> None:
        """Mark the guide as no longer visible.

        The actual dialog is closed by Dialog.select() returning, so this
        just clears the visibility flag and logs.
        """
        self._is_visible = False
        xbmc.log(f"{_LOG_PREFIX} guide closed", xbmc.LOGINFO)

    # ------------------------------------------------------------------
    # Data fetch
    # ------------------------------------------------------------------

    def _fetch_and_display(self) -> None:
        """Fetch /guide and display the result.

        On any network error or timeout, shows an error notification and
        returns without displaying the channel list (Requirement 16.8).
        """
        url = f"{self._url}/guide?t={int(time.time())}"
        xbmc.log(f"{_LOG_PREFIX} fetching guide from {url}", xbmc.LOGINFO)

        try:
            with urllib.request.urlopen(url, timeout=GUIDE_FETCH_TIMEOUT_SECONDS) as resp:
                raw = resp.read()
        except urllib.error.URLError as exc:
            xbmc.log(
                f"{_LOG_PREFIX} fetch failed (URLError): {exc}",
                xbmc.LOGWARNING,
            )
            self._show_fetch_error()
            return
        except OSError as exc:
            # Covers socket.timeout and similar OS-level errors.
            xbmc.log(
                f"{_LOG_PREFIX} fetch failed (OSError): {exc}",
                xbmc.LOGWARNING,
            )
            self._show_fetch_error()
            return

        try:
            payload = json.loads(raw)
            channels: list = payload.get("channels", [])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            xbmc.log(
                f"{_LOG_PREFIX} failed to parse guide response: {exc}",
                xbmc.LOGWARNING,
            )
            self._show_fetch_error()
            return

        # Cache the successfully parsed response.
        self._cache_data = channels
        self._cache_time = time.time()
        xbmc.log(
            f"{_LOG_PREFIX} fetched {len(channels)} channel(s), cached at t={self._cache_time:.0f}",
            xbmc.LOGINFO,
        )

        self._display(channels)

    def _show_fetch_error(self) -> None:
        """Display a 2-second error notification when the guide fetch fails.

        Requirement 16.8: show error message, close after 2 seconds.
        The notification's built-in `time` parameter (ms) handles the 2s
        auto-close without requiring a separate timer.
        """
        xbmcgui.Dialog().notification(
            "Classic TV",
            "Guide unavailable",
            xbmcgui.NOTIFICATION_WARNING,
            2000,  # 2 seconds
        )

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def _display(self, channels: list) -> None:
        """Render the channel list using Dialog().select().

        All display strings are built in memory before the dialog is shown,
        satisfying Requirement 16.5 (single-call population). The select()
        API accepts the full list at once — there is no `addItem()` loop.

        Channel format per Requirement 16.3:
            CH {number} — {name}: {now_title} [{mins}m] [→ {next_title}]

        Parameters
        ----------
        channels:
            The ``channels`` array from the /guide JSON response.  Each
            entry must have at minimum ``number``, ``name``, ``id``, and
            optionally ``now`` / ``next`` sub-objects.
        """
        # Build display lines and an aligned channel_ids list in a single
        # pass — no GUI calls inside this loop (Requirement 16.5).
        display_lines: list[str] = []
        channel_ids: list[str] = []

        for ch in channels:
            now_obj = ch.get("now") or {}
            next_obj = ch.get("next") or {}

            # Requirement 16.3: "Offline" when now.type == "offline" or absent.
            now_type = now_obj.get("type", "")
            if now_type == "offline" or not now_obj:
                now_title = "Offline"
            else:
                now_title = now_obj.get("title") or "Offline"

            # Requirement 16.3: empty string when next.title is null.
            next_title = next_obj.get("title") or ""

            # Minutes remaining (optional decoration).
            mins = now_obj.get("minutes_remaining", 0)

            line = f"CH {ch.get('number', '?')} \u2014 {ch.get('name', '')}: {now_title}"
            if mins:
                line += f" ({mins}m)"
            if next_title:
                line += f" \u2192 {next_title}"

            display_lines.append(line)
            channel_ids.append(ch.get("id", ""))

        if not display_lines:
            xbmc.log(f"{_LOG_PREFIX} no channels to display", xbmc.LOGWARNING)
            return

        xbmc.log(
            f"{_LOG_PREFIX} opening guide dialog with {len(display_lines)} channel(s)",
            xbmc.LOGINFO,
        )

        # Mark visible before the blocking select() call so toggle() can
        # detect the open state if it is somehow re-entered.
        self._is_visible = True

        # Dialog().select() is blocking — it returns only when the viewer
        # presses ENTER, BACK, or an equivalent dismiss action.
        # The full list is passed in a single call (Requirement 16.5).
        selected = xbmcgui.Dialog().select("TV Guide", display_lines)

        # Guide is now closed.
        self._is_visible = False

        if selected >= 0:
            channel_id = channel_ids[selected]
            xbmc.log(
                f"{_LOG_PREFIX} viewer selected channel_id={channel_id!r} "
                f"(index={selected})",
                xbmc.LOGINFO,
            )
            # Requirement 16.6: guide is already closed (select() returned)
            # before we invoke the channel-change callback.
            self._on_channel_change(channel_id)
        else:
            xbmc.log(f"{_LOG_PREFIX} viewer dismissed guide without selecting", xbmc.LOGINFO)
