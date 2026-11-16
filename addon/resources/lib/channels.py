"""
Channel list and metadata for the Classic TV 3.0 Player addon.

Loads the channel roster from the Schedule_Service's /guide endpoint at
startup, then provides fast in-memory lookup for channel-change navigation.

No schedule logic lives here — this module knows only which channels exist,
their display metadata, and their sort order by number (Requirements 5.1,
14.3).
"""

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import List, Optional

import xbmc

_LOG_PREFIX = "[ClassicTV][channels]"


@dataclass
class Channel:
    """Addon-side lightweight channel descriptor.

    This is NOT the server-side Channel model (server/models.py). It carries
    only the fields the Player needs for display and navigation.
    """

    id: str
    name: str
    number: int
    color: str


class ChannelList:
    """Loads and exposes the channel roster from the Schedule_Service.

    Usage::

        channels = ChannelList("http://192.168.1.207:8300")
        ch = channels.get_channel_by_number(67)
        next_ch = channels.next_channel(current_id="nickelodeon")
    """

    def __init__(self, schedule_service_url: str) -> None:
        self._url = schedule_service_url
        self._channels: List[Channel] = []
        self._load()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Fetch /guide and populate the sorted channel list.

        On any HTTP or parse error, logs a WARNING and leaves the list empty
        so callers degrade gracefully rather than crashing.
        """
        url = f"{self._url}/guide?t={int(time.time())}"
        xbmc.log(f"{_LOG_PREFIX} fetching channel list from {url}", xbmc.LOGINFO)

        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                raw = response.read()
        except urllib.error.URLError as exc:
            xbmc.log(
                f"{_LOG_PREFIX} HTTP error loading channel list: {exc}",
                xbmc.LOGWARNING,
            )
            return

        try:
            payload = json.loads(raw)
            channels: List[Channel] = []
            for item in payload["channels"]:
                channels.append(
                    Channel(
                        id=item["id"],
                        name=item["name"],
                        number=int(item["number"]),
                        color=item["color"],
                    )
                )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            xbmc.log(
                f"{_LOG_PREFIX} failed to parse channel list response: {exc}",
                xbmc.LOGWARNING,
            )
            return

        self._channels = sorted(channels, key=lambda ch: ch.number)
        xbmc.log(
            f"{_LOG_PREFIX} loaded {len(self._channels)} channel(s)",
            xbmc.LOGINFO,
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_channel_by_number(self, number: int) -> Optional[Channel]:
        """Return the Channel whose number matches, or None."""
        for ch in self._channels:
            if ch.number == number:
                return ch
        return None

    def next_channel(self, current_id: str) -> Optional[Channel]:
        """Return the channel immediately after *current_id* in number order.

        Wraps around at the end. Returns the first channel if *current_id* is
        not found. Returns None only when the channel list is empty.
        """
        if not self._channels:
            return None

        for idx, ch in enumerate(self._channels):
            if ch.id == current_id:
                return self._channels[(idx + 1) % len(self._channels)]

        # current_id not found — fall back to first
        return self._channels[0]

    def prev_channel(self, current_id: str) -> Optional[Channel]:
        """Return the channel immediately before *current_id* in number order.

        Wraps around at the start. Returns the last channel if *current_id* is
        not found. Returns None only when the channel list is empty.
        """
        if not self._channels:
            return None

        for idx, ch in enumerate(self._channels):
            if ch.id == current_id:
                return self._channels[(idx - 1) % len(self._channels)]

        # current_id not found — fall back to last
        return self._channels[-1]

    def all_channels(self) -> List[Channel]:
        """Return a shallow copy of the channel list sorted by number."""
        return list(self._channels)
