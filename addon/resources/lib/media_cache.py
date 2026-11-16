"""
MediaCache — local LRU prefetch cache for Classic TV 3.0.

Pre-downloads upcoming media from Jellyfin to /storage/classictv_cache/
so that episodes play from the local SD card instead of the network.

Design:
  - In-memory dict[jellyfin_id, CacheEntry] backed by filesystem.
  - Prefetch thread runs every MEDIA_PREFETCH_INTERVAL_SECONDS (30 min).
  - LRU eviction keeps total size <= MEDIA_CACHE_MAX_BYTES (10 GB).
  - Active playback URL is never evicted (Requirement 19.7).

All log lines prefixed [ClassicTV][media_cache].
Requirements: 19.1, 19.2, 19.3, 19.4, 19.5, 19.6, 19.7, 19.8
"""

import os
import time
import threading
import urllib.request
from dataclasses import dataclass

import xbmc

from constants import (
    MEDIA_CACHE_MAX_BYTES,
    MEDIA_PREFETCH_INTERVAL_SECONDS,
)

_LOG_PREFIX = "[ClassicTV][media_cache]"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    """Represents one cached media file on the Sweet Potato SD card."""

    jellyfin_id: str
    local_path: str       # /storage/classictv_cache/{jellyfin_id}.bin
    size_bytes: int
    last_accessed: float  # Unix timestamp; updated on every get_local_url hit
    is_complete: bool     # True only after the .part rename succeeds


# ---------------------------------------------------------------------------
# MediaCache
# ---------------------------------------------------------------------------

class MediaCache:
    """Local LRU media prefetch cache.

    Collaborators are injected after construction to avoid circular imports:
      _player        — xbmc.Player (or stub); used during eviction to check
                       the currently playing file so it is never evicted.

    Usage:
        cache = MediaCache(player=player)
        cache.start_prefetch_thread(schedule_service_url, channel_ids)
    """

    # Default cache directory for CoreELEC (/storage partition).
    # On other platforms (macOS dev, Linux without /storage), fall back to
    # a user-writable temp directory so the addon starts cleanly.
    _COREEL_CACHE_DIR = "/storage/classictv_cache"
    _FALLBACK_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".classictv_cache")
    CACHE_DIR = _COREEL_CACHE_DIR if os.path.exists("/storage") else _FALLBACK_CACHE_DIR

    def __init__(self, player=None) -> None:
        self._player = player
        self._cache: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        os.makedirs(self.CACHE_DIR, exist_ok=True)
        self._load_existing()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_local_url(self, jellyfin_id: str) -> str | None:
        """Return the local path for a fully downloaded item, or None.

        Also updates last_accessed so that recently-played items survive
        LRU eviction longer (Requirement 19.5).
        """
        with self._lock:
            entry = self._cache.get(jellyfin_id)
            if entry is None or not entry.is_complete:
                return None
            entry.last_accessed = time.time()
            return entry.local_path

    def prefetch(self, jellyfin_id: str, direct_play_url: str) -> None:
        """Stream direct_play_url to a .part file; rename on success.

        If the item is already fully cached, returns immediately (idempotent).
        On any download failure, the .part file is deleted and a WARNING is
        logged so the Player falls back to the network URL (Requirement 19.4).
        """
        with self._lock:
            existing = self._cache.get(jellyfin_id)
            if existing is not None and existing.is_complete:
                return  # already cached — nothing to do

        local_path = os.path.join(self.CACHE_DIR, f"{jellyfin_id}.bin")
        part_path = local_path + ".part"

        try:
            with urllib.request.urlopen(direct_play_url, timeout=30) as resp:
                size = 0
                with open(part_path, "wb") as fh:
                    while True:
                        chunk = resp.read(1024 * 1024)  # 1 MB chunks
                        if not chunk:
                            break
                        fh.write(chunk)
                        size += len(chunk)

            os.rename(part_path, local_path)

            entry = CacheEntry(
                jellyfin_id=jellyfin_id,
                local_path=local_path,
                size_bytes=size,
                last_accessed=time.time(),
                is_complete=True,
            )
            with self._lock:
                self._cache[jellyfin_id] = entry

            xbmc.log(
                f"{_LOG_PREFIX} prefetch complete: {jellyfin_id} "
                f"({size} bytes)",
                xbmc.LOGINFO,
            )

        except Exception as exc:
            xbmc.log(
                f"{_LOG_PREFIX} prefetch failed for {jellyfin_id}: {exc}",
                xbmc.LOGWARNING,
            )
            if os.path.exists(part_path):
                try:
                    os.unlink(part_path)
                except OSError:
                    pass

    def evict_if_needed(self) -> None:
        """LRU eviction: remove oldest items until total <= MEDIA_CACHE_MAX_BYTES.

        The currently playing file is never removed (Requirement 19.7).
        If all cached items are currently playing a WARNING is logged and
        eviction is skipped for this cycle (Requirement 19.8).
        """
        with self._lock:
            total = sum(e.size_bytes for e in self._cache.values())
            if total <= MEDIA_CACHE_MAX_BYTES:
                return

            # Determine the URL that is actively playing so we never evict it.
            current_url = ""
            if self._player is not None:
                try:
                    if self._player.isPlaying():
                        current_url = self._player.getPlayingFile()
                except Exception:  # noqa: S110  # getPlayingFile() may raise if nothing is playing; that's fine
                    pass

            while total > MEDIA_CACHE_MAX_BYTES:
                candidates = [
                    e for e in self._cache.values()
                    if e.local_path != current_url
                ]
                if not candidates:
                    xbmc.log(
                        f"{_LOG_PREFIX} all cache items playing "
                        "— skipping eviction",
                        xbmc.LOGWARNING,
                    )
                    return

                oldest = min(candidates, key=lambda e: e.last_accessed)
                total -= oldest.size_bytes
                try:
                    os.unlink(oldest.local_path)
                except OSError:
                    pass
                del self._cache[oldest.jellyfin_id]
                xbmc.log(
                    f"{_LOG_PREFIX} evicted {oldest.jellyfin_id}",
                    xbmc.LOGINFO,
                )

    def start_prefetch_thread(
        self,
        schedule_service_url: str,
        channel_ids: list[str],
    ) -> None:
        """Spawn the background prefetch daemon thread (Requirement 19.2).

        The thread runs a prefetch pass every MEDIA_PREFETCH_INTERVAL_SECONDS
        (1800 s = 30 min).  It is a daemon thread so it does not prevent
        Kodi from exiting.
        """
        thread = threading.Thread(
            target=self._prefetch_loop,
            args=(schedule_service_url, channel_ids),
            daemon=True,
            name="ClassicTV-PrefetchThread",
        )
        thread.start()
        xbmc.log(
            f"{_LOG_PREFIX} prefetch thread started "
            f"(interval={MEDIA_PREFETCH_INTERVAL_SECONDS}s, "
            f"channels={channel_ids})",
            xbmc.LOGINFO,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_existing(self) -> None:
        """Scan CACHE_DIR on startup and register any complete .bin files.

        .part files are treated as incomplete and ignored (they will be
        cleaned up the next time prefetch() tries to overwrite them).
        """
        try:
            entries = os.listdir(self.CACHE_DIR)
        except OSError:
            return

        for filename in entries:
            if not filename.endswith(".bin"):
                continue
            jellyfin_id = filename[:-4]  # strip ".bin"
            full_path = os.path.join(self.CACHE_DIR, filename)
            try:
                size = os.path.getsize(full_path)
                mtime = os.path.getmtime(full_path)
            except OSError:
                continue

            entry = CacheEntry(
                jellyfin_id=jellyfin_id,
                local_path=full_path,
                size_bytes=size,
                last_accessed=mtime,
                is_complete=True,
            )
            self._cache[jellyfin_id] = entry

        xbmc.log(
            f"{_LOG_PREFIX} loaded {len(self._cache)} existing cache entries",
            xbmc.LOGINFO,
        )

    def _prefetch_loop(
        self,
        schedule_service_url: str,
        channel_ids: list[str],
    ) -> None:
        """Background thread body: run a prefetch pass every 30 minutes.

        Each pass queries GET /schedule/{channel_id}?t=now+3600 to find
        the item playing in one hour, then prefetches it if not cached.
        evict_if_needed() is called after each successful download.
        """
        while True:
            self._run_prefetch_pass(schedule_service_url, channel_ids)
            # Sleep in short intervals so the thread reacts promptly to
            # process shutdown (daemon=True handles the hard case).
            _sleep_interruptible(MEDIA_PREFETCH_INTERVAL_SECONDS)

    def _run_prefetch_pass(
        self,
        schedule_service_url: str,
        channel_ids: list[str],
    ) -> None:
        """Execute one prefetch pass across all active channels.

        For each channel: fetch the schedule 1 hour ahead, extract
        next_item, and download if not already in the cache.
        """
        t_ahead = int(time.time()) + 3600

        for channel_id in channel_ids:
            url = (
                f"{schedule_service_url.rstrip('/')}"
                f"/schedule/{channel_id}?t={t_ahead}"
            )
            try:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    import json
                    data = json.loads(resp.read().decode())

                next_item = data.get("next_item")
                if not next_item:
                    continue

                jellyfin_id = next_item.get("jellyfin_id")
                direct_play_url = next_item.get("direct_play_url")
                if not jellyfin_id or not direct_play_url:
                    continue

                # Only prefetch if not already fully cached.
                with self._lock:
                    existing = self._cache.get(jellyfin_id)
                    already_cached = existing is not None and existing.is_complete

                if not already_cached:
                    xbmc.log(
                        f"{_LOG_PREFIX} prefetching next item for "
                        f"channel {channel_id}: {jellyfin_id}",
                        xbmc.LOGINFO,
                    )
                    self.prefetch(jellyfin_id, direct_play_url)
                    self.evict_if_needed()

            except Exception as exc:
                xbmc.log(
                    f"{_LOG_PREFIX} prefetch pass failed for "
                    f"channel {channel_id}: {exc}",
                    xbmc.LOGWARNING,
                )


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _sleep_interruptible(total_seconds: float) -> None:
    """Sleep in 1-second ticks so the thread wakes promptly on process exit."""
    end = time.time() + total_seconds
    while time.time() < end:
        time.sleep(1.0)
