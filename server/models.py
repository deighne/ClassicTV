"""
Data models for Classic TV 3.0.

This module defines all shared dataclasses used across the Schedule_Service,
scheduler, config loader, Jellyfin client, and media cache. Import from here
rather than defining parallel structures in individual modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Schedule API models
# ---------------------------------------------------------------------------


@dataclass
class ScheduleItem:
    """Current item returned by GET /schedule/{channel_id}?t=."""

    jellyfin_id: str          # Jellyfin item UUID
    title: str                # Human-readable: "Hey Arnold! S02E04 - The Little Pink Book"
    type: str                 # "episode" | "bumper" | "offline"
    seek_offset: float        # Seconds into the item; 0.0 <= seek_offset < duration
    duration: float           # Total item duration in seconds
    direct_play_url: str      # Full Jellyfin stream URL (may include StartTimeTicks)
    hls_transcode_url: str | None = field(default=None)  # Only present when fallback transcode is warranted


@dataclass
class NextItem:
    """Lightweight next-item payload embedded in ScheduleResponse."""

    jellyfin_id: str
    title: str
    direct_play_url: str


@dataclass
class ScheduleResponse:
    """Full HTTP response body for GET /schedule/{channel_id}?t=."""

    channel_id: str
    item: ScheduleItem
    next_item: NextItem
    generated_at: int         # Unix timestamp (integer seconds)


# ---------------------------------------------------------------------------
# Channel configuration models
# ---------------------------------------------------------------------------


@dataclass
class Show:
    """One show entry within a block's rotation."""

    name: str
    weight: int               # 1–1000 positive integer
    jellyfin_ids: list[str]   # Resolved episode IDs in playback order


@dataclass
class Block:
    """One time-window within a channel's broadcast day."""

    name: str                 # 1–64 characters
    start_hour: int           # 0–23 (America/Chicago local time)
    end_hour: int             # 1–24 (exclusive upper bound)
    shows: list[Show] | None = field(default=None)  # None when offline=True
    offline: bool = False     # True means Offline_Clip for this window


@dataclass
class Channel:
    """Parsed representation of a channel YAML file."""

    id: str                   # 1–64 characters
    name: str                 # 1–128 characters
    number: int               # 1–9999
    color: str                # "#rrggbb" lowercase hex
    blocks: list[Block] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Guide API models
# ---------------------------------------------------------------------------


@dataclass
class GuideItem:
    """Current-show summary for one channel in the /guide response."""

    title: str
    type: str                 # "episode" | "bumper" | "offline"
    minutes_remaining: int    # floor((end_of_slot - t) / 60)


@dataclass
class GuideItemNext:
    """Next-show summary for one channel in the /guide response."""

    title: str | None         # None when no next item


@dataclass
class GuideChannel:
    """One channel entry in the /guide response.

    Contains no file paths, seek offsets, or Jellyfin IDs.
    """

    id: str
    name: str
    number: int
    color: str                # "#rrggbb"
    now: GuideItem
    next: GuideItemNext | None = field(default=None)


# ---------------------------------------------------------------------------
# Rachel anchor model
# ---------------------------------------------------------------------------


@dataclass
class RachelAnchorResponse:
    """Response body for GET /rachel-anchor."""

    state: str                         # "playing" | "idle"
    jellyfin_id: str | None = field(default=None)
    anchor_position: float | None = field(default=None)   # Seconds into item at anchor_timestamp
    anchor_timestamp: float | None = field(default=None)  # Unix timestamp (float) when anchor was recorded
    total_duration: float | None = field(default=None)


# ---------------------------------------------------------------------------
# Scheduler internal models
# ---------------------------------------------------------------------------


@dataclass
class EpisodeSlot:
    """One episode slot in the expanded weighted rotation produced by expand_block_episodes()."""

    jellyfin_id: str
    title: str
    duration: float
    show_name: str


# ---------------------------------------------------------------------------
# Media cache model
# ---------------------------------------------------------------------------


@dataclass
class CacheEntry:
    """One entry in the Media_Cache LRU dictionary."""

    jellyfin_id: str
    local_path: str           # /storage/classictv_cache/{jellyfin_id}.ext
    size_bytes: int
    last_accessed: float      # Unix timestamp
    is_complete: bool


# ---------------------------------------------------------------------------
# Config validation error
# ---------------------------------------------------------------------------


@dataclass
class ConfigValidationError(Exception):
    """Raised by config_loader.parse() when a channel YAML file fails validation.

    Inherits from Exception so it can be raised and caught normally,
    while also being a dataclass for structured field access.
    """

    file: str    # Path to the YAML file that failed validation
    field: str   # Dot-separated field path, e.g. "blocks[0].start_hour"
    reason: str  # Human-readable explanation of the validation failure

    def __str__(self) -> str:
        return f"{self.file}: field '{self.field}' — {self.reason}"
