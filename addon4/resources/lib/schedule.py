"""
Schedule client for Classic TV 4.0.

Fetches schedule slots and channel lists from the ClassicTV Jellyfin plugin.

Endpoints:
  GET {base}/ClassicTV/channels
      → { "Channels": [{"Id": str, "Name": str}, ...] }

  GET {base}/ClassicTV/channel/{channel_id}
      → { "ChannelId": str, "Title": str, "JellyfinItemId": str,
          "StartOffset": float, "Duration": float,
          "NextItem": {"JellyfinItemId": str, "Title": str,
                       "StartOffset": float, "Duration": float} | null,
          "GeneratedAt": int }

No auth required — the plugin's controller uses [AllowAnonymous].
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

import xbmc

_LOG_PREFIX = "[ClassicTV][schedule]"


# ---------------------------------------------------------------------------
# Data types returned to callers
# ---------------------------------------------------------------------------


@dataclass
class ScheduleSlot:
    """What is playing on a channel right now."""

    channel_id: str
    title: str
    jellyfin_item_id: str
    start_offset: float  # seconds into the episode
    duration: float  # total episode duration in seconds
    next_item: Optional["NextSlot"] = None
    mirror: bool = False  # mirror channel: following another user's playback
    paused: bool = False  # mirror channel: the mirrored session is paused


@dataclass
class NextSlot:
    """Lightweight next-item info bundled with the current slot."""

    jellyfin_item_id: str
    title: str
    start_offset: float
    duration: float


@dataclass
class ChannelInfo:
    """Channel identity as returned by GET /ClassicTV/channels."""

    id: str
    name: str
    number: int = 0  # server-assigned channel number; 0 means unassigned
    mirror: bool = False  # plays what another user is watching, not a schedule


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class ScheduleClient:
    """HTTP client for the ClassicTV Jellyfin plugin API.

    Args:
        base_url: Jellyfin server base URL, e.g. "http://192.168.1.207:8096".
    """

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        """Return the normalized Jellyfin server base URL (no trailing slash)."""
        return self._base_url

    def stream_url(self, item_id: str) -> str:
        """Return the direct HTTP byte-range stream URL for *item_id*.

        Uses Jellyfin's unauthenticated static video stream endpoint
        (``/Videos/{id}/stream?static=true``), which serves the container file
        directly via HTTP byte-range requests in ~5 ms without transcoding or
        spawning a cold ``plugin.video.jellyfin`` Python process.
        """
        return f"{self._base_url}/Videos/{item_id}/stream?static=true"

    def check_server_health(self, timeout: float = 5.0) -> bool:
        """Return True if the Jellyfin server is reachable and responding.

        Calls the unauthenticated ``/System/Info/Public`` endpoint, which is
        available even before a user logs in.  Any network or HTTP error is
        handled by ``_get_json`` and results in a ``None`` return, which maps
        to ``False`` here.

        Args:
            timeout: Request timeout in seconds.

        Returns:
            ``True`` if the server responded with a valid JSON object,
            ``False`` on any error or unreachable host.
        """
        result = self._get_json(f"{self._base_url}/System/Info/Public", timeout)
        return result is not None

    def get_channels(self, timeout: float = 5.0) -> list[ChannelInfo]:
        """Return the ordered channel list, or [] on any error."""
        url = f"{self._base_url}/ClassicTV/channels"
        data = self._get_json(url, timeout)
        if data is None:
            return []
        try:
            return [
                ChannelInfo(
                    id=c["Id"],
                    name=c["Name"],
                    number=int(c.get("Number", 0)),
                    mirror=bool(c.get("Mirror", False)),
                )
                for c in data.get("Channels", [])
            ]
        except (KeyError, TypeError) as exc:
            xbmc.log(
                f"{_LOG_PREFIX} get_channels: unexpected response shape: {exc}",
                xbmc.LOGWARNING,
            )
            return []

    def channel_exists(self, channel_id: str, timeout: float = 5.0) -> bool:
        """Return True if *channel_id* appears in the live channel list.

        The schedule API already filters out disabled channels, so a channel
        that is absent from this list is either disabled or deleted.  A
        network error is treated conservatively as "may still exist" (True)
        so that transient connectivity problems do not trigger a fallback tune.
        """
        infos = self.get_channels(timeout=timeout)
        if not infos:
            # Could not reach the server — assume the channel still exists to
            # avoid an unwanted tune-away on a temporary outage.
            return True
        return any(c.id == channel_id for c in infos)

    def get_slot(
        self,
        channel_id: str,
        timeout: float = 5.0,
    ) -> ScheduleSlot | None:
        """Return the current schedule slot for a channel, or None on error."""
        url = f"{self._base_url}/ClassicTV/channel/{channel_id}"
        data = self._get_json(url, timeout)
        if data is None:
            return None
        try:
            next_raw = data.get("NextItem")
            next_slot = None
            if next_raw and next_raw.get("JellyfinItemId"):
                next_slot = NextSlot(
                    jellyfin_item_id=next_raw["JellyfinItemId"],
                    title=next_raw.get("Title", ""),
                    start_offset=float(next_raw.get("StartOffset", 0.0)),
                    duration=float(next_raw.get("Duration", 0.0)),
                )
            return ScheduleSlot(
                channel_id=data.get("ChannelId", channel_id),
                title=data.get("Title", ""),
                jellyfin_item_id=data.get("JellyfinItemId", ""),
                start_offset=float(data.get("StartOffset", 0.0)),
                duration=float(data.get("Duration", 0.0)),
                next_item=next_slot,
                mirror=bool(data.get("Mirror", False)),
                paused=bool(data.get("Paused", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            xbmc.log(
                f"{_LOG_PREFIX} get_slot: unexpected response shape"
                f" channel={channel_id!r}: {exc}",
                xbmc.LOGWARNING,
            )
            return None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_json(self, url: str, timeout: float) -> dict | None:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                data = json.loads(resp.read())
            if not isinstance(data, dict):
                xbmc.log(
                    f"{_LOG_PREFIX} unexpected JSON type"
                    f" {type(data).__name__} from {url}",
                    xbmc.LOGWARNING,
                )
                return None
            return data
        except urllib.error.HTTPError as exc:
            xbmc.log(
                f"{_LOG_PREFIX} HTTP {exc.code} from {url}",
                xbmc.LOGWARNING,
            )
        except urllib.error.URLError as exc:
            xbmc.log(
                f"{_LOG_PREFIX} URL error fetching {url}: {exc.reason}",
                xbmc.LOGWARNING,
            )
        except json.JSONDecodeError as exc:
            xbmc.log(
                f"{_LOG_PREFIX} JSON decode error from {url}: {exc}",
                xbmc.LOGWARNING,
            )
        return None
