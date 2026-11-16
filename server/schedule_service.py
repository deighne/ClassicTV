"""
HTTP Schedule Service for Classic TV 3.0.

Startup sequence:
    1. Validate required environment variables (JELLYFIN_API_KEY).
    2. Load channel configuration from CHANNEL_CONFIG_DIR.
    3. Start filesystem watcher on CHANNEL_CONFIG_DIR for hot-reload.
    4. Start Rachel anchor poller background thread.
    5. Bind HTTP server and begin accepting requests.

Public endpoints:
    GET /health           → {"status": "ok", "version": "3.0.0"}
    GET /schedule/<id>    → ScheduleResponse JSON  (implemented in task 6.2)
    GET /guide            → list[GuideChannel] JSON (implemented in task 6.3)
    GET /rachel-anchor    → RachelAnchorResponse JSON (implemented in task 6.4)

All log lines are prefixed [ClassicTV][schedule_service].
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from server import config_loader, jellyfin
from server.constants import (
    CONFIG_HOT_RELOAD_DEBOUNCE_SECONDS,
    RACHEL_ANCHOR_POLL_INTERVAL_SECONDS,
    SCHEDULE_SERVICE_PORT,
)
from server.models import Channel, RachelAnchorResponse, ScheduleItem

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

_LOG = "[ClassicTV][schedule_service]"


def _read_version() -> str:
    """Read the deploy version from the VERSION file in the repo root.

    Written by deploy/deploy-jfmac.sh using ``git describe --tags --always --dirty``.
    Falls back to 'dev' when running directly from a checkout without deploying
    (e.g. during local development or tests).
    """
    version_file = os.path.join(os.path.dirname(__file__), "..", "VERSION")
    try:
        with open(os.path.normpath(version_file)) as f:
            return f.read().strip()
    except OSError:
        return "dev"

# ---------------------------------------------------------------------------
# Module-level service state
# ---------------------------------------------------------------------------

#: Mapping of channel id → Channel, rebuilt on hot-reload.
_channel_registry: dict[str, Channel] = {}

#: Guards all reads/writes to _channel_registry.
_registry_lock: threading.Lock = threading.Lock()

#: Latest Rachel anchor result; None until first successful poll.
_rachel_state: RachelAnchorResponse | None = None

#: Guards all reads/writes to _rachel_state.
_rachel_lock: threading.Lock = threading.Lock()

#: Tracks the last time a Jellyfin-unreachable WARNING was emitted for Rachel.
#: Used to throttle log spam to at most once per 60 seconds.
_rachel_last_warning_time: float = 0.0

# ---------------------------------------------------------------------------
# Environment variable resolution
# ---------------------------------------------------------------------------


def _load_env() -> dict[str, Any]:
    """Read and validate all required environment variables.

    Returns a dict of resolved configuration values.

    Calls ``sys.exit(1)`` if a required variable is absent.
    """
    api_key = os.environ.get("JELLYFIN_API_KEY", "").strip()
    if not api_key:
        logger.error(
            "%s JELLYFIN_API_KEY environment variable is not set. "
            "Set it and restart the service.",
            _LOG,
        )
        sys.exit(1)

    base_url = os.environ.get("JELLYFIN_BASE_URL", "http://localhost:8096").rstrip("/")

    try:
        port = int(os.environ.get("SERVICE_PORT", str(SCHEDULE_SERVICE_PORT)))
    except ValueError:
        logger.error(
            "%s SERVICE_PORT must be an integer, got %r",
            _LOG,
            os.environ.get("SERVICE_PORT"),
        )
        sys.exit(1)

    config_dir = os.environ.get("CHANNEL_CONFIG_DIR", "channels")

    return {
        "api_key": api_key,
        "base_url": base_url,
        "port": port,
        "config_dir": config_dir,
    }


# ---------------------------------------------------------------------------
# Hot-reload filesystem watcher
# ---------------------------------------------------------------------------


class _ChannelConfigHandler(FileSystemEventHandler):
    """Watchdog event handler that debounces filesystem changes and reloads
    the channel registry after ``CONFIG_HOT_RELOAD_DEBOUNCE_SECONDS`` of
    inactivity.
    """

    def __init__(self, config_dir: str) -> None:
        super().__init__()
        self._config_dir = config_dir
        self._debounce_timer: threading.Timer | None = None
        self._timer_lock = threading.Lock()

    # ------------------------------------------------------------------
    # FileSystemEventHandler overrides
    # ------------------------------------------------------------------

    def on_any_event(self, event) -> None:  # type: ignore[override]
        """Restart the debounce timer on every filesystem event."""
        with self._timer_lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(
                CONFIG_HOT_RELOAD_DEBOUNCE_SECONDS,
                self._reload,
            )
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _reload(self) -> None:
        """Load channels and atomically swap the registry."""
        logger.info("%s hot-reload triggered; reloading %r", _LOG, self._config_dir)
        try:
            new_channels = config_loader.load_all_channels(self._config_dir)
        except Exception as exc:  # noqa: BLE001
            logger.error("%s hot-reload failed: %s", _LOG, exc)
            return

        with _registry_lock:
            global _channel_registry
            _channel_registry = new_channels

        jellyfin.clear_dead_id_cache()

        logger.info(
            "%s hot-reload complete; %d channel(s) now active",
            _LOG,
            len(new_channels),
        )


def _start_config_watcher(config_dir: str) -> Observer:
    """Create, configure, and start a watchdog ``Observer`` for *config_dir*.

    Returns the running ``Observer`` so the caller can join it on shutdown.
    """
    handler = _ChannelConfigHandler(config_dir)
    observer = Observer()
    observer.schedule(handler, path=config_dir, recursive=False)
    observer.daemon = True
    observer.start()
    logger.info(
        "%s filesystem watcher started on %r (debounce=%ds)",
        _LOG,
        config_dir,
        CONFIG_HOT_RELOAD_DEBOUNCE_SECONDS,
    )
    return observer


# ---------------------------------------------------------------------------
# Rachel anchor poller
# ---------------------------------------------------------------------------


def _find_rachel_session(sessions: list[dict]) -> dict | None:
    """Find Rachel's active playing session from the Jellyfin sessions list.

    Matches by ``RACHEL_JELLYFIN_USER_ID`` env var when set; otherwise falls
    back to a case-insensitive check for "rachel" in the ``UserName`` field.
    Skips sessions that have no ``NowPlayingItem`` or are paused.

    Args:
        sessions: Parsed list of session objects from ``GET /Sessions``.

    Returns:
        The first matching playing session dict, or ``None`` if not found.
    """
    rachel_user_id = os.environ.get("RACHEL_JELLYFIN_USER_ID", "")
    for session in sessions:
        # Skip sessions with no active media
        if "NowPlayingItem" not in session:
            continue
        # Skip paused sessions
        if session.get("PlayState", {}).get("IsPaused", True):
            continue
        # Match by user ID if configured, else match by username containing "rachel"
        if rachel_user_id:
            if session.get("UserId") == rachel_user_id:
                return session
        else:
            username = session.get("UserName", "")
            if "rachel" in username.lower():
                return session
    return None


def _poll_rachel() -> None:
    """Query the Jellyfin session API for Rachel's current playback state.

    Fetches ``GET /Sessions`` from Jellyfin, locates Rachel's session via
    :func:`_find_rachel_session`, and updates ``_rachel_state`` under
    ``_rachel_lock``.

    On network failure, sets state to "idle" and emits a throttled WARNING
    (at most once per 60 seconds) to avoid log spam (Requirement 3.6).
    """
    global _rachel_state, _rachel_last_warning_time

    url = f"{jellyfin._base_url()}/Sessions"

    try:
        sessions = jellyfin._get_json(url)
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        now = time.time()
        if now - _rachel_last_warning_time > 60:
            logger.warning(
                "%s Jellyfin unreachable while polling Rachel sessions: %s",
                _LOG,
                exc,
            )
            _rachel_last_warning_time = now
        with _rachel_lock:
            _rachel_state = RachelAnchorResponse(state="idle")
        return
    except Exception as exc:  # noqa: BLE001
        now = time.time()
        if now - _rachel_last_warning_time > 60:
            logger.warning(
                "%s Unexpected error polling Rachel sessions: %s",
                _LOG,
                exc,
            )
            _rachel_last_warning_time = now
        with _rachel_lock:
            _rachel_state = RachelAnchorResponse(state="idle")
        return

    session = _find_rachel_session(sessions if isinstance(sessions, list) else [])

    if session is not None:
        now_playing = session["NowPlayingItem"]
        play_state = session.get("PlayState", {})

        jellyfin_id: str = now_playing["Id"]
        anchor_position: float = play_state.get("PositionTicks", 0) / 10_000_000
        anchor_timestamp: float = time.time()
        total_duration: float = now_playing.get("RunTimeTicks", 0) / 10_000_000

        new_state = RachelAnchorResponse(
            state="playing",
            jellyfin_id=jellyfin_id,
            anchor_position=anchor_position,
            anchor_timestamp=anchor_timestamp,
            total_duration=total_duration,
        )
    else:
        new_state = RachelAnchorResponse(state="idle")

    with _rachel_lock:
        _rachel_state = new_state


def _rachel_poller_loop() -> None:
    """Background loop that calls ``_poll_rachel`` every
    ``RACHEL_ANCHOR_POLL_INTERVAL_SECONDS`` seconds.
    """
    while True:
        try:
            _poll_rachel()
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s Rachel poll error: %s", _LOG, exc)
        time.sleep(RACHEL_ANCHOR_POLL_INTERVAL_SECONDS)


def _start_rachel_poller() -> threading.Thread:
    """Start the Rachel poller as a daemon thread and return it."""
    t = threading.Thread(target=_rachel_poller_loop, name="rachel-poller", daemon=True)
    t.start()
    logger.info(
        "%s Rachel poller started (interval=%ds)",
        _LOG,
        RACHEL_ANCHOR_POLL_INTERVAL_SECONDS,
    )
    return t


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _send_json(handler: BaseHTTPRequestHandler, status: int, data: Any) -> None:
    """Write a JSON HTTP response.

    Sets ``Content-Type: application/json`` and encodes *data* with
    ``json.dumps``.  The response is sent in a single write.

    Args:
        handler: The active ``BaseHTTPRequestHandler`` instance.
        status:  HTTP status code (e.g. 200, 404).
        data:    JSON-serialisable Python object.
    """
    body = json.dumps(data).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


# ---------------------------------------------------------------------------
# Request handlers (stubs for tasks 6.2–6.4)
# ---------------------------------------------------------------------------


def _handle_health(handler: BaseHTTPRequestHandler) -> None:
    """GET /health — returns service version and liveness status."""
    _send_json(handler, 200, {"status": "ok", "version": "3.0.0"})


def _handle_schedule(handler: BaseHTTPRequestHandler, channel_id: str) -> None:
    """GET /schedule/<channel_id>?t=<unix_timestamp>

    1. Parse and validate the ``t`` query parameter.
    2. Look up ``channel_id`` in the channel registry.
    3. Compute the current schedule item via :func:`~server.scheduler.compute_schedule`.
    4. Validate direct-play support; fall back to Offline_Clip on failure.
    5. Build and return a full :class:`~server.models.ScheduleResponse` as JSON.
    """
    # ------------------------------------------------------------------
    # 1. Parse query string
    # ------------------------------------------------------------------
    parsed_url = urllib.parse.urlparse(handler.path)
    qs = urllib.parse.parse_qs(parsed_url.query)

    t_list = qs.get("t", [])
    if not t_list:
        _send_json(handler, 400, {"error": "invalid parameter 't': required"})
        return
    try:
        t = int(t_list[0])
    except (ValueError, TypeError):
        _send_json(handler, 400, {"error": "invalid parameter 't': must be an integer"})
        return

    # ------------------------------------------------------------------
    # 2. Channel registry look-up
    # ------------------------------------------------------------------
    with _registry_lock:
        channel = _channel_registry.get(channel_id)

    if channel is None:
        _send_json(handler, 404, {"error": f"channel not found: '{channel_id}'"})
        return

    # ------------------------------------------------------------------
    # 3. Compute the primary schedule item (two-pass, Requirement 41)
    # ------------------------------------------------------------------
    from server.scheduler import compute_schedule, expand_block_episodes, _find_active_block, _to_chicago_datetime

    # ------------------------------------------------------------------
    # 3. Fetch real episode duration for the current item (Requirement 41).
    #
    # Two-pass approach: run compute_schedule once with fallback durations
    # to identify which episode is currently playing, then fetch only that
    # episode's real duration from Jellyfin (cached after first hit), and
    # run compute_schedule again with the accurate duration.
    #
    # This avoids sending hundreds of IDs in one URL (HTTP 414) while
    # keeping per-request Jellyfin traffic to at most one call. Episodes
    # near the end of a long playlist are months away from playing and
    # do not need their durations pre-fetched.
    # ------------------------------------------------------------------
    try:
        rough_item = compute_schedule(channel, t)
        if rough_item.type != "offline":
            episode_durations = jellyfin.get_episode_durations([rough_item.jellyfin_id])
        else:
            episode_durations = {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s failed during two-pass duration fetch: %s", _LOG, exc)
        episode_durations = {}

    try:
        item = compute_schedule(channel, t, episode_durations=episode_durations)
    except Exception as exc:
        logger.error(
            "%s unhandled exception in compute_schedule channel=%s t=%d: %s",
            _LOG, channel_id, t, exc, exc_info=True,
        )
        _send_json(handler, 500, {"error": "internal server error"})
        return

    # ------------------------------------------------------------------
    # 4. Direct-play validation (skip for offline items)
    # ------------------------------------------------------------------
    if item.type != "offline":
        if not jellyfin.validate_direct_play(item.jellyfin_id):
            # Primary item failed validation — try remaining slots in the block
            logger.warning(
                "%s direct-play validation failed for %s; scanning block for fallback",
                _LOG, item.jellyfin_id,
            )

            fallback_item: "ScheduleItem | None" = None

            try:
                local_dt = _to_chicago_datetime(t)
                block = _find_active_block(channel, local_dt.hour)
                if block is not None and not block.offline:
                    slots = expand_block_episodes(block)
                    # Walk all slots; skip the one we already tried
                    for slot in slots:
                        if slot.jellyfin_id == item.jellyfin_id:
                            continue
                        if jellyfin.validate_direct_play(slot.jellyfin_id):
                            fallback_item = ScheduleItem(
                                jellyfin_id=slot.jellyfin_id,
                                title=slot.title,
                                type="episode",
                                seek_offset=0.0,
                                duration=slot.duration,
                                direct_play_url="",
                            )
                            logger.warning(
                                "%s using fallback item %s (title=%s) for channel=%s",
                                _LOG, slot.jellyfin_id, slot.title, channel_id,
                            )
                            break
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "%s error scanning block for fallback (channel=%s): %s",
                    _LOG, channel_id, exc, exc_info=True,
                )

            if fallback_item is None:
                # No valid item found — return Offline_Clip
                logger.warning(
                    "%s no valid fallback found for channel=%s; returning offline clip",
                    _LOG, channel_id,
                )
                from server.scheduler import _make_offline_clip
                item = _make_offline_clip()
            else:
                item = fallback_item

    # ------------------------------------------------------------------
    # 5. Build direct_play_url for the primary item
    # ------------------------------------------------------------------
    if item.type == "offline":
        offline_id = os.environ.get("OFFLINE_CLIP_JELLYFIN_ID", "OFFLINE")
        direct_play_url = jellyfin.get_stream_url(offline_id)
        item = ScheduleItem(
            jellyfin_id=offline_id,
            title=item.title,
            type="offline",
            seek_offset=0.0,
            duration=item.duration,
            direct_play_url=direct_play_url,
        )
    else:
        seek_ticks = int(item.seek_offset * 10_000_000) if item.seek_offset > 0 else None
        direct_play_url = jellyfin.get_stream_url(item.jellyfin_id, seek_ticks=seek_ticks)
        item = ScheduleItem(
            jellyfin_id=item.jellyfin_id,
            title=item.title,
            type=item.type,
            seek_offset=item.seek_offset,
            duration=item.duration,
            direct_play_url=direct_play_url,
            hls_transcode_url=item.hls_transcode_url,
        )

    # ------------------------------------------------------------------
    # 6. Compute next_item
    # ------------------------------------------------------------------
    next_item_data: dict[str, str] = {"jellyfin_id": "", "title": "Offline", "direct_play_url": ""}

    try:
        next_t = t + int(item.duration) if item.duration > 0 else t + 3600
        next_schedule = compute_schedule(channel, next_t)

        if next_schedule.type == "offline":
            # Try 1 hour out for a more useful next item
            next_schedule = compute_schedule(channel, t + 3600)

        if next_schedule.type != "offline":
            next_direct_play_url = jellyfin.get_stream_url(next_schedule.jellyfin_id)
            next_item_data = {
                "jellyfin_id": next_schedule.jellyfin_id,
                "title": next_schedule.title,
                "direct_play_url": next_direct_play_url,
            }
        # else: keep the offline fallback defined above
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "%s failed to compute next_item for channel=%s: %s",
            _LOG, channel_id, exc,
        )

    # ------------------------------------------------------------------
    # 7. Assemble response
    # ------------------------------------------------------------------
    generated_at = int(time.time())

    response_body = {
        "channel_id": channel_id,
        "item": {
            "jellyfin_id": item.jellyfin_id,
            "title": item.title,
            "type": item.type,
            "seek_offset": item.seek_offset,
            "duration": item.duration,
            "direct_play_url": item.direct_play_url,
            "hls_transcode_url": item.hls_transcode_url,
        },
        "next_item": next_item_data,
        "generated_at": generated_at,
    }

    logger.info(
        "%s channel=%s t=%d item=%s seek_offset=%.1f",
        _LOG, channel_id, t, item.jellyfin_id, item.seek_offset,
    )

    _send_json(handler, 200, response_body)


def _handle_guide(handler: BaseHTTPRequestHandler) -> None:
    """GET /guide?t=<unix_timestamp>

    Returns now/next information for every configured channel as a JSON
    ``{"channels": [...]}`` response.  No file paths, Jellyfin IDs, seek
    offsets, or direct-play URLs are included in the payload.

    Requirement 2.8: ``t`` must be a valid positive integer; otherwise HTTP 400.
    Requirement 2.9: response within 150ms for up to 50 channels (CPU-only path).
    """
    from math import floor
    from server.scheduler import compute_schedule

    # ------------------------------------------------------------------
    # 1. Parse and validate t (positive integer only, per Requirement 2.8)
    # ------------------------------------------------------------------
    parsed_url = urllib.parse.urlparse(handler.path)
    qs = urllib.parse.parse_qs(parsed_url.query)

    t_list = qs.get("t", [])
    if not t_list:
        _send_json(handler, 400, {"error": "invalid parameter 't': required"})
        return
    try:
        t = int(t_list[0])
    except (ValueError, TypeError):
        _send_json(handler, 400, {"error": "invalid parameter 't': must be a positive integer"})
        return
    if t <= 0:
        _send_json(handler, 400, {"error": "invalid parameter 't': must be a positive integer"})
        return

    # ------------------------------------------------------------------
    # 2. Snapshot the channel registry under lock
    # ------------------------------------------------------------------
    with _registry_lock:
        channels = list(_channel_registry.values())

    # ------------------------------------------------------------------
    # 3. Build guide entries for every channel
    # ------------------------------------------------------------------
    guide_channels: list[dict] = []

    for channel in channels:
        try:
            # Current item
            item = compute_schedule(channel, t)

            if item.type == "offline":
                now_entry: dict = {
                    "title": "Offline",
                    "type": "offline",
                    "minutes_remaining": 0,
                }
                next_entry = None
            else:
                minutes_remaining = max(0, floor((item.duration - item.seek_offset) / 60))
                now_entry = {
                    "title": item.title,
                    "type": item.type,
                    "minutes_remaining": minutes_remaining,
                }

                # Next item — look ahead by however much of this item remains
                time_remaining = max(1, int(item.duration - item.seek_offset))
                next_t = t + time_remaining
                try:
                    next_item = compute_schedule(channel, next_t)
                    if next_item.type == "offline":
                        next_entry = None
                    else:
                        next_entry = {"title": next_item.title}
                except Exception as next_exc:  # noqa: BLE001
                    logger.warning(
                        "%s failed to compute next item for channel=%s: %s",
                        _LOG, channel.id, next_exc,
                    )
                    next_entry = None

            logger.debug(
                "%s guide channel=%s now=%r minutes_remaining=%s",
                _LOG,
                channel.id,
                now_entry.get("title"),
                now_entry.get("minutes_remaining"),
            )

            guide_channels.append(
                {
                    "id": channel.id,
                    "name": channel.name,
                    "number": channel.number,
                    "color": channel.color,
                    "now": now_entry,
                    "next": next_entry,
                }
            )

        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s skipping channel=%s in guide due to error: %s",
                _LOG, channel.id, exc,
            )

    _send_json(handler, 200, {"channels": guide_channels})


def _handle_rachel_anchor(handler: BaseHTTPRequestHandler) -> None:
    """GET /rachel-anchor — return Rachel's current cached playback state.

    Reads ``_rachel_state`` under ``_rachel_lock`` (no network I/O — just a
    cache read) and serialises it to JSON.

    Response shapes:
    - Pre-first-poll (``_rachel_state`` is ``None``): ``{"state": "idle"}``
    - Idle: ``{"state": "idle"}``
    - Playing: full JSON with ``state``, ``jellyfin_id``, ``anchor_position``,
      ``anchor_timestamp``, and ``total_duration``.
    """
    with _rachel_lock:
        state = _rachel_state

    if state is None or state.state == "idle":
        _send_json(handler, 200, {"state": "idle"})
        return

    # state.state == "playing"
    _send_json(handler, 200, {
        "state": "playing",
        "jellyfin_id": state.jellyfin_id,
        "anchor_position": state.anchor_position,
        "anchor_timestamp": state.anchor_timestamp,
        "total_duration": state.total_duration,
    })


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTP server that spawns a new thread for each incoming request.

    ``daemon_threads = True`` ensures worker threads do not block process exit.
    """

    daemon_threads = True


class ClassicTVHandler(BaseHTTPRequestHandler):
    """Request handler for the Classic TV schedule service.

    Routes incoming GET requests to the appropriate handler function.
    All other methods return 405 Method Not Allowed.
    """

    # Silence the default access log — we do our own logging in handlers.
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102
        pass

    def do_GET(self) -> None:  # noqa: N802
        """Route GET requests to the appropriate handler."""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        try:
            if path == "/health":
                _handle_health(self)

            elif path.startswith("/schedule/"):
                # /schedule/<channel_id>
                channel_id = path[len("/schedule/"):]
                if not channel_id:
                    _send_json(self, 404, {"error": "channel_id is required"})
                else:
                    _handle_schedule(self, channel_id)

            elif path == "/guide":
                _handle_guide(self)

            elif path == "/rachel-anchor":
                _handle_rachel_anchor(self)

            else:
                _send_json(self, 404, {"error": f"not found: {path}"})

        except Exception as exc:  # noqa: BLE001
            logger.error("%s unhandled exception in handler: %s", _LOG, exc, exc_info=True)
            try:
                _send_json(self, 500, {"error": "internal server error"})
            except Exception:  # noqa: BLE001, S110
                pass


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main() -> None:
    """Run all startup steps and start serving HTTP requests.

    Step order:
        1. Load and validate environment variables.
        2. Load channel configuration.
        3. Start filesystem watcher for hot-reload.
        4. Start Rachel anchor poller thread.
        5. Start HTTP server.
    """
    # Step 1 — environment
    env = _load_env()
    port: int = env["port"]
    config_dir: str = env["config_dir"]

    # Stamp version/commit at startup so logs are unambiguous about which
    # code is running. Reads git HEAD first (works during development),
    # falls back to VERSION file (written by deploy scripts), then "unknown".
    _version = _read_version()
    logger.info("%s starting up version=%s port=%d config_dir=%r", _LOG, _version, port, config_dir)
    global _channel_registry
    channels = config_loader.load_all_channels(config_dir)
    with _registry_lock:
        _channel_registry = channels
    logger.info("%s loaded %d channel(s) from %r", _LOG, len(channels), config_dir)

    # Step 3 — filesystem watcher
    _start_config_watcher(config_dir)

    # Step 4 — Rachel poller
    _start_rachel_poller()

    # Step 5 — HTTP server
    server = _ThreadingHTTPServer(("", port), ClassicTVHandler)
    logger.info("%s listening on port %d", _LOG, port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("%s shutting down", _LOG)
        server.server_close()


if __name__ == "__main__":
    main()
