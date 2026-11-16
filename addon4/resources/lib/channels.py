"""
Channel list for Classic TV 4.0.

Fetched once at startup from GET /ClassicTV/channels and held in memory.
Channels are ordered by the server's declared order; up/down navigation
wraps at the ends.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import xbmc

from schedule import ChannelInfo, ScheduleClient

_LOG_PREFIX = "[ClassicTV][channels]"


@dataclass
class Channel:
    """A single channel entry for navigation purposes."""

    id: str
    name: str
    number: int  # server number, else positional (next free on collision)
    mirror: bool = False  # plays what another user is watching


class ChannelList:
    """In-memory channel roster built from the schedule client.

    Args:
        client: ScheduleClient instance.
    """

    def __init__(self, client: ScheduleClient) -> None:
        self._channels: list[Channel] = []
        self._load(client)

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def _load(self, client: ScheduleClient) -> None:
        infos: list[ChannelInfo] = client.get_channels()
        if not infos:
            xbmc.log(
                f"{_LOG_PREFIX} no channels returned from server",
                xbmc.LOGWARNING,
            )
        # Server-assigned numbers win. An unassigned channel (0) keeps its
        # positional number (index + 1) unless a server-assigned or earlier
        # channel already holds it, in which case it takes the next free one,
        # so a partly numbered lineup never puts two channels on one number.
        taken = {info.number for info in infos if info.number > 0}
        numbered: list[Channel] = []
        for i, info in enumerate(infos):
            number = info.number
            if number <= 0:
                number = i + 1
                while number in taken:
                    number += 1
                taken.add(number)
            numbered.append(
                Channel(id=info.id, name=info.name, number=number, mirror=info.mirror)
            )
        self._channels = numbered
        xbmc.log(
            f"{_LOG_PREFIX} loaded {len(self._channels)} channel(s): "
            f"{[c.id for c in self._channels]}",
            xbmc.LOGINFO,
        )

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def all_channels(self) -> list[Channel]:
        return list(self._channels)

    def next_channel(self, current_id: str) -> Optional[Channel]:
        if not self._channels:
            return None
        idx = self._index_of(current_id)
        if idx is None:
            return self._channels[0]
        return self._channels[(idx + 1) % len(self._channels)]

    def prev_channel(self, current_id: str) -> Optional[Channel]:
        if not self._channels:
            return None
        idx = self._index_of(current_id)
        if idx is None:
            return self._channels[-1]
        return self._channels[(idx - 1) % len(self._channels)]

    def get_channel_by_number(self, number: int) -> Optional[Channel]:
        for ch in self._channels:
            if ch.number == number:
                return ch
        return None

    def has_longer_number(self, prefix: str) -> bool:
        """True if some channel number starts with *prefix* and is longer than it."""
        return any(
            str(c.number).startswith(prefix) and len(str(c.number)) > len(prefix)
            for c in self._channels
        )

    def get_channel_by_id(self, channel_id: str) -> Optional[Channel]:
        for ch in self._channels:
            if ch.id == channel_id:
                return ch
        return None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _index_of(self, channel_id: str) -> Optional[int]:
        for i, ch in enumerate(self._channels):
            if ch.id == channel_id:
                return i
        return None
