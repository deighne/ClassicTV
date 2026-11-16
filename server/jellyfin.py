"""
Jellyfin HTTP client for Classic TV 3.0.

Provides direct-play validation and (in task 5.2) dead-ID resolution and
stream URL construction.  All network I/O uses stdlib ``urllib`` only — no
third-party HTTP libraries required.

Environment variables:
    JELLYFIN_BASE_URL   Base URL of the Jellyfin server (default: http://localhost:8096)
    JELLYFIN_API_KEY    Jellyfin API key — required for authenticated requests
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level passthrough constants
# ---------------------------------------------------------------------------

#: Container formats safe for direct-play without transcoding.
PASSTHROUGH_CONTAINERS: frozenset[str] = frozenset({"mkv", "mp4", "m4v"})

#: Video codecs that can be direct-played on the target hardware.
PASSTHROUGH_CODECS: frozenset[str] = frozenset({"h264", "hevc"})

#: Audio codecs that can be direct-played on the target hardware.
PASSTHROUGH_AUDIO: frozenset[str] = frozenset({"aac", "mp3", "ac3", "eac3"})

#: Request timeout in seconds for all Jellyfin API calls.
_REQUEST_TIMEOUT: int = 10

# ---------------------------------------------------------------------------
# Dead-ID resolution cache
# ---------------------------------------------------------------------------

#: In-memory cache mapping stale Jellyfin item IDs to their current replacements.
#: Populated by :func:`resolve_dead_id`; cleared by :func:`clear_dead_id_cache`.
_resolved_id_cache: dict[str, str] = {}

#: Lock protecting :data:`_resolved_id_cache` for concurrent access.
_resolved_id_cache_lock: threading.Lock = threading.Lock()

# ---------------------------------------------------------------------------
# Duration cache (Requirement 41)
# ---------------------------------------------------------------------------

#: TTL in seconds for the episode duration cache.  Durations rarely change;
#: 1 hour matches a typical library-rescan interval.
DURATION_CACHE_TTL_SECONDS: int = 3600

#: In-memory cache: jellyfin_id → (duration_seconds, cached_at_unix_time).
_duration_cache: dict[str, tuple[float, float]] = {}

#: Lock protecting :data:`_duration_cache` for concurrent access.
_duration_cache_lock: threading.Lock = threading.Lock()

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _base_url() -> str:
    """Return the Jellyfin base URL from the environment."""
    return os.environ.get("JELLYFIN_BASE_URL", "http://localhost:8096").rstrip("/")


def _api_key() -> str:
    """Return the Jellyfin API key from the environment."""
    return os.environ.get("JELLYFIN_API_KEY", "")


def _get_json(url: str) -> dict[str, Any]:
    """Perform an authenticated GET request and return parsed JSON.

    Raises:
        urllib.error.URLError: on connection failure or DNS error.
        urllib.error.HTTPError: on 4xx/5xx responses.
        json.JSONDecodeError: on invalid JSON in the response body.
        TimeoutError: if the server does not respond within _REQUEST_TIMEOUT seconds.
    """
    req = urllib.request.Request(
        url,
        headers={"X-Emby-Token": _api_key()},
    )
    with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT) as response:
        raw = response.read()
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_direct_play(jellyfin_id: str) -> bool:
    """Return True if *jellyfin_id* can be direct-played without transcoding.

    Queries ``GET /Items/{jellyfin_id}?Fields=MediaSources`` and inspects each
    ``MediaSource`` in the response:

    - ``Container`` must be in :data:`PASSTHROUGH_CONTAINERS`
    - At least one ``Video`` stream whose ``Codec`` is in :data:`PASSTHROUGH_CODECS`
    - At least one ``Audio`` stream whose ``Codec`` is in :data:`PASSTHROUGH_AUDIO`

    Returns ``True`` as soon as any ``MediaSource`` passes all checks.
    Returns ``False`` if Jellyfin is unreachable, returns an error status, or
    no ``MediaSource`` satisfies the codec requirements.

    Args:
        jellyfin_id: The Jellyfin item UUID to inspect.

    Returns:
        ``True`` if the item can be direct-played, ``False`` otherwise.
    """
    url = f"{_base_url()}/Items?Ids={jellyfin_id}&Fields=MediaSources"

    logger.debug("[ClassicTV][jellyfin] Checking direct-play for item %s", jellyfin_id)

    try:
        data = _get_json(url)
    except urllib.error.HTTPError as exc:
        logger.warning(
            "[ClassicTV][jellyfin] HTTP %s fetching MediaSources for %s: %s",
            exc.code,
            jellyfin_id,
            exc.reason,
        )
        return False
    except urllib.error.URLError as exc:
        logger.warning(
            "[ClassicTV][jellyfin] Jellyfin unreachable while fetching %s: %s",
            jellyfin_id,
            exc.reason,
        )
        return False
    except (TimeoutError, json.JSONDecodeError, KeyError) as exc:
        logger.warning(
            "[ClassicTV][jellyfin] Error fetching MediaSources for %s: %s",
            jellyfin_id,
            exc,
        )
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[ClassicTV][jellyfin] Unexpected error fetching %s: %s",
            jellyfin_id,
            exc,
        )
        return False

    # Jellyfin returns an Items list for /Items?Ids= — unwrap to get the single item
    items: list[dict[str, Any]] = data.get("Items", [])
    if not items:
        logger.warning(
            "[ClassicTV][jellyfin] No item returned for ID %s", jellyfin_id
        )
        return False

    media_sources: list[dict[str, Any]] = items[0].get("MediaSources", [])

    for source in media_sources:
        try:
            if not _source_passes(source):
                continue
        except (KeyError, TypeError) as exc:
            logger.warning(
                "[ClassicTV][jellyfin] Malformed MediaSource entry for %s: %s",
                jellyfin_id,
                exc,
            )
            continue

        logger.debug(
            "[ClassicTV][jellyfin] Direct-play validated for %s (container=%s)",
            jellyfin_id,
            source.get("Container", ""),
        )
        return True

    logger.debug(
        "[ClassicTV][jellyfin] No passing MediaSource found for %s",
        jellyfin_id,
    )
    return False


# ---------------------------------------------------------------------------
# Internal validation helpers
# ---------------------------------------------------------------------------


def _source_passes(source: dict[str, Any]) -> bool:
    """Return True if a single MediaSource dict passes all direct-play checks.

    Checks:
    1. ``Container`` (lowercased) is in :data:`PASSTHROUGH_CONTAINERS`.
    2. At least one Video stream with Codec in :data:`PASSTHROUGH_CODECS`.
    3. At least one Audio stream with Codec in :data:`PASSTHROUGH_AUDIO`.

    Args:
        source: A single entry from the ``MediaSources`` array.

    Returns:
        ``True`` if all checks pass, ``False`` otherwise.
    """
    container: str = (source.get("Container") or "").lower()
    if container not in PASSTHROUGH_CONTAINERS:
        logger.debug(
            "[ClassicTV][jellyfin] Container '%s' not in passthrough set", container
        )
        return False

    streams: list[dict[str, Any]] = source.get("MediaStreams", [])

    video_ok = any(
        s.get("Type") == "Video" and (s.get("Codec") or "").lower() in PASSTHROUGH_CODECS
        for s in streams
    )
    if not video_ok:
        logger.debug("[ClassicTV][jellyfin] No passthrough video stream found")
        return False

    audio_ok = any(
        s.get("Type") == "Audio" and (s.get("Codec") or "").lower() in PASSTHROUGH_AUDIO
        for s in streams
    )
    if not audio_ok:
        logger.debug("[ClassicTV][jellyfin] No passthrough audio stream found")
        return False

    return True


# ---------------------------------------------------------------------------
# Dead-ID resolution and stream URL construction
# ---------------------------------------------------------------------------


def resolve_dead_id(
    jellyfin_id: str,
    series_id: str,
    season: int,
    episode: int,
) -> str | None:
    """Resolve a stale Jellyfin item ID by looking up the episode in the library.

    When a Jellyfin library is rescanned, item UUIDs can change.  This function
    queries Jellyfin's ``/Items`` endpoint using the series, season, and episode
    numbers — which are stable — and returns the current UUID for that episode.

    Results are cached in :data:`_resolved_id_cache` for the lifetime of the
    process.  Call :func:`clear_dead_id_cache` on config hot-reload to discard
    stale entries.

    Args:
        jellyfin_id: The old (potentially dead) Jellyfin item UUID.
        series_id:   Jellyfin series UUID used to scope the search.
        season:      Season number (1-indexed).
        episode:     Episode number within the season (1-indexed).

    Returns:
        The current Jellyfin item UUID for the requested episode, or ``None``
        if Jellyfin is unreachable, returns an error, or the episode is not
        found.
    """
    with _resolved_id_cache_lock:
        cached = _resolved_id_cache.get(jellyfin_id)
    if cached is not None:
        logger.debug(
            "[ClassicTV][jellyfin] Cache hit for dead ID %s → %s",
            jellyfin_id,
            cached,
        )
        return cached

    url = (
        f"{_base_url()}/Items"
        f"?SeriesId={series_id}&Season={season}&Episode={episode}"
    )

    logger.debug(
        "[ClassicTV][jellyfin] Resolving dead ID %s via series=%s S%02dE%02d",
        jellyfin_id,
        series_id,
        season,
        episode,
    )

    try:
        data = _get_json(url)
    except urllib.error.HTTPError as exc:
        logger.warning(
            "[ClassicTV][jellyfin] HTTP %s resolving dead ID %s: %s",
            exc.code,
            jellyfin_id,
            exc.reason,
        )
        return None
    except urllib.error.URLError as exc:
        logger.warning(
            "[ClassicTV][jellyfin] Jellyfin unreachable resolving dead ID %s: %s",
            jellyfin_id,
            exc.reason,
        )
        return None
    except (TimeoutError, json.JSONDecodeError, KeyError) as exc:
        logger.warning(
            "[ClassicTV][jellyfin] Error resolving dead ID %s: %s",
            jellyfin_id,
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[ClassicTV][jellyfin] Unexpected error resolving dead ID %s: %s",
            jellyfin_id,
            exc,
        )
        return None

    items: list[dict[str, Any]] = data.get("Items", [])
    if not items:
        logger.warning(
            "[ClassicTV][jellyfin] No items returned resolving dead ID %s "
            "(series=%s S%02dE%02d)",
            jellyfin_id,
            series_id,
            season,
            episode,
        )
        return None

    new_id: str = items[0].get("Id", "")
    if not new_id:
        logger.warning(
            "[ClassicTV][jellyfin] First item has no 'Id' field resolving dead ID %s",
            jellyfin_id,
        )
        return None

    with _resolved_id_cache_lock:
        _resolved_id_cache[jellyfin_id] = new_id

    logger.info(
        "[ClassicTV][jellyfin] Resolved dead ID %s → %s",
        jellyfin_id,
        new_id,
    )
    return new_id


def get_stream_url(jellyfin_id: str, seek_ticks: int | None = None) -> str:
    """Return the direct-play stream URL for a Jellyfin item.

    This is a pure URL-construction helper — no HTTP requests are made.

    Args:
        jellyfin_id: The Jellyfin item UUID to stream.
        seek_ticks:  Optional start position in Jellyfin ticks
                     (1 second = 10,000,000 ticks).  When provided, a
                     ``StartTimeTicks`` query parameter is appended so Jellyfin
                     can attempt to begin delivery at that offset.

    Returns:
        A fully-formed stream URL, e.g.
        ``http://localhost:8096/Videos/abc123/stream?Static=true`` or
        ``http://localhost:8096/Videos/abc123/stream?Static=true&StartTimeTicks=7423000000``.
    """
    url = f"{_base_url()}/Videos/{jellyfin_id}/stream?Static=true"
    if seek_ticks is not None:
        url = f"{url}&StartTimeTicks={seek_ticks}"
    return url


def get_episode_durations(jellyfin_ids: list[str]) -> dict[str, float]:
    """Return a mapping of ``{jellyfin_id: duration_seconds}`` for the given IDs.

    Queries ``GET /Items?Ids={id1,id2,...}&Fields=RunTimeTicks`` and converts
    ``RunTimeTicks`` (Jellyfin ticks, 10,000,000 per second) to floating-point
    seconds.  Items with missing or zero ``RunTimeTicks`` are omitted so the
    caller's fallback logic applies.

    Results are cached per ID for :data:`DURATION_CACHE_TTL_SECONDS` (1 hour).
    The cache is invalidated by :func:`clear_dead_id_cache` so hot-reloaded
    channel configs always pick up fresh durations.

    Args:
        jellyfin_ids: List of Jellyfin item UUIDs to look up.

    Returns:
        Dict mapping each UUID that has a non-zero duration to its duration
        in seconds.  Returns an empty dict if the list is empty or if
        Jellyfin is unreachable.

    Requirements: 41.1, 41.3, 41.4
    """
    if not jellyfin_ids:
        return {}

    import time as _time
    now = _time.time()

    result: dict[str, float] = {}
    uncached_ids: list[str] = []

    # Check cache first
    with _duration_cache_lock:
        for jid in jellyfin_ids:
            entry = _duration_cache.get(jid)
            if entry is not None:
                duration, cached_at = entry
                if now - cached_at < DURATION_CACHE_TTL_SECONDS:
                    result[jid] = duration
                    continue
            uncached_ids.append(jid)

    if not uncached_ids:
        return result

    # Fetch uncached IDs from Jellyfin in a single request
    ids_param = ",".join(uncached_ids)
    url = f"{_base_url()}/Items?Ids={ids_param}&Fields=RunTimeTicks"

    logger.debug(
        "[ClassicTV][jellyfin] Fetching durations for %d episode(s)", len(uncached_ids)
    )

    try:
        data = _get_json(url)
    except urllib.error.HTTPError as exc:
        logger.warning(
            "[ClassicTV][jellyfin] HTTP %s fetching durations: %s", exc.code, exc.reason
        )
        return result
    except urllib.error.URLError as exc:
        logger.warning(
            "[ClassicTV][jellyfin] Jellyfin unreachable fetching durations: %s", exc.reason
        )
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[ClassicTV][jellyfin] Unexpected error fetching durations: %s", exc
        )
        return result

    with _duration_cache_lock:
        for item in data.get("Items", []):
            jid = item.get("Id", "")
            ticks = item.get("RunTimeTicks") or 0
            if not jid or not ticks:
                continue
            duration = ticks / 10_000_000.0
            _duration_cache[jid] = (duration, now)
            result[jid] = duration

    logger.debug(
        "[ClassicTV][jellyfin] Resolved durations for %d/%d episode(s)",
        len(result), len(jellyfin_ids),
    )
    return result


def clear_dead_id_cache() -> None:
    """Clear the in-memory dead-ID resolution cache and the duration cache.

    Should be called whenever the channel configuration is hot-reloaded so that
    any Jellyfin IDs that were replaced in the YAML files are not served from
    stale cache entries, and so freshly-imported episodes get their real
    durations on the next schedule request (Requirement 41.4).
    """
    with _resolved_id_cache_lock:
        _resolved_id_cache.clear()
    with _duration_cache_lock:
        _duration_cache.clear()
    logger.debug("[ClassicTV][jellyfin] Dead-ID and duration caches cleared")
