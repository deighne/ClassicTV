#!/usr/bin/env python3
"""tools/classictv-status.py — Classic TV 4.0 Status Monitor & Crash Autopsy

Queries Sweet Potato (Le Potato / CoreELEC Kodi), Jellyfin server (:8096),
and the Classic TV plugin. Detects unhandled reboots, playback stalls, and
hardware crashes, recording full post-mortem diagnostics to web/incidents.json.

Usage:
  python3 tools/classictv-status.py               # Print terminal status & latest autopsy
  python3 tools/classictv-status.py --json        # Print raw status.json
  python3 tools/classictv-status.py --incidents   # Show incident history in detail
  python3 tools/classictv-status.py --check-once  # Run one health check cycle and update web/status.json
  python3 tools/classictv-status.py --daemon      # Run continuous background monitoring loop (30s)
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import socket
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

# Paths & Defaults
REPO_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = REPO_ROOT / "web"
STATUS_JSON_PATH = WEB_DIR / "status.json"
INCIDENTS_JSON_PATH = WEB_DIR / "incidents.json"

DEFAULT_SWEET_POTATO_HOST = os.environ.get("SWEET_POTATO_IP", "127.0.0.1")
DEFAULT_JELLYFIN_URL = os.environ.get("JELLYFIN_BASE_URL", "http://127.0.0.1:8096")
SSH_USER = "root"
KODI_JSONRPC_PORT = 9090


def parse_uptime_seconds(uptime_str: str | None) -> int | None:
    """Parse Kodi System.Uptime string into integer seconds."""
    if not uptime_str:
        return None
    uptime_str = uptime_str.strip().lower()
    days = 0
    if "day" in uptime_str:
        parts = uptime_str.split("day", 1)
        try:
            m = re.search(r"\d+", parts[0])
            if m:
                days = int(m.group(0))
        except Exception:
            days = 0
        uptime_str = parts[1].lstrip("s, ")

    if "min" in uptime_str and ":" not in uptime_str:
        m = re.search(r"(\d+)\s*min", uptime_str)
        if m:
            return days * 86400 + int(m.group(1)) * 60

    time_parts = uptime_str.split(":")
    if len(time_parts) == 3:
        try:
            return days * 86400 + int(time_parts[0]) * 3600 + int(time_parts[1]) * 60 + int(time_parts[2])
        except ValueError:
            pass
    elif len(time_parts) == 2:
        try:
            return days * 86400 + int(time_parts[0]) * 60 + int(time_parts[1])
        except ValueError:
            pass

    return None


def get_ping_latency(host: str, timeout: float = 1.0) -> float | None:
    """Return ping round-trip time in milliseconds, or None if host is down."""
    try:
        res = subprocess.run(
            ["ping", "-c", "1", "-W", str(int(timeout * 1000)), host],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=timeout + 0.5,
        )
        if res.returncode == 0:
            match = re.search(r"time=([\d.]+)\s*ms", res.stdout)
            if match:
                return float(match.group(1))
            return 1.0
    except Exception:
        pass
    return None


def kodi_jsonrpc(host: str, method: str, params: dict | None = None, timeout: float = 2.0) -> dict | None:
    """Send a JSON-RPC request to Kodi's raw TCP port (9090) and return the result."""
    payload = {"jsonrpc": "2.0", "method": method, "id": 1}
    if params is not None:
        payload["params"] = params
    data = json.dumps(payload).encode("utf-8") + b"\n"

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect((host, KODI_JSONRPC_PORT))
            s.sendall(data)
            buf = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
                try:
                    resp = json.loads(buf.decode("utf-8"))
                    return resp.get("result")
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return None


def query_kodi(host: str) -> dict:
    """Gather live Kodi and player state via JSON-RPC."""
    state = {
        "reachable": False,
        "kodi_version": None,
        "uptime": None,
        "current_window": None,
        "player_state": "offline",
        "channel_number": None,
        "channel_name": None,
        "playing_item": None,
    }

    # Application version
    app_props = kodi_jsonrpc(host, "Application.GetProperties", {"properties": ["name", "version"]})
    if not app_props or not isinstance(app_props, dict):
        return state
    state["reachable"] = True
    v = app_props.get("version") or {}
    state["kodi_version"] = f"{v.get('major', '')}.{v.get('minor', '')} ({v.get('tag', '')})".strip()

    # System labels & window properties
    labels = kodi_jsonrpc(
        host,
        "XBMC.GetInfoLabels",
        {
            "labels": [
                "System.Uptime",
                "System.CurrentWindow",
                "Player.Title",
                "Player.Time",
                "Player.Duration",
                "VideoPlayer.Title",
                "Window(Home).Property(ClassicTV.ActiveChannelNumber)",
                "Window(Home).Property(ClassicTV.ActiveChannelName)",
                "Window(Home).Property(ClassicTV.ChannelNumber)",
                "Window(Home).Property(ClassicTV.ChannelName)",
                "Window(Home).Property(ClassicTV.StaticVisible)",
                "Window(Home).Property(ClassicTV.BugVisible)",
            ]
        },
    )
    safe_labels = labels if isinstance(labels, dict) else {}
    if safe_labels:
        state["uptime"] = safe_labels.get("System.Uptime")
        state["current_window"] = safe_labels.get("System.CurrentWindow")
        state["channel_number"] = (
            safe_labels.get("Window(Home).Property(ClassicTV.ActiveChannelNumber)")
            or safe_labels.get("Window(Home).Property(ClassicTV.ChannelNumber)")
            or None
        )
        state["channel_name"] = (
            safe_labels.get("Window(Home).Property(ClassicTV.ActiveChannelName)")
            or safe_labels.get("Window(Home).Property(ClassicTV.ChannelName)")
            or None
        )
        state["static_visible"] = bool(safe_labels.get("Window(Home).Property(ClassicTV.StaticVisible)"))

    # Active players
    active = kodi_jsonrpc(host, "Player.GetActivePlayers")
    if active and isinstance(active, list) and len(active) > 0 and isinstance(active[0], dict):
        player_id = active[0].get("playerid", 1)
        player_type = active[0].get("type", "video")

        item_info = kodi_jsonrpc(
            host,
            "Player.GetItem",
            {"playerid": player_id, "properties": ["title", "showtitle", "file", "duration"]},
        )
        props = kodi_jsonrpc(
            host,
            "Player.GetProperties",
            {"playerid": player_id, "properties": ["time", "totaltime", "percentage", "speed"]},
        )

        speed = (props or {}).get("speed", 1) if isinstance(props, dict) else 1
        state["player_state"] = "paused" if speed == 0 else "playing"

        item = (item_info.get("item") if isinstance(item_info, dict) else None) or {}
        time_obj = (props.get("time") if isinstance(props, dict) else None) or {}
        total_obj = (props.get("totaltime") if isinstance(props, dict) else None) or {}

        def fmt_time(t):
            if not isinstance(t, dict):
                return None
            return f"{t.get('hours', 0):02d}:{t.get('minutes', 0):02d}:{t.get('seconds', 0):02d}"

        t_str = fmt_time(time_obj) or safe_labels.get("Player.Time")
        d_str = fmt_time(total_obj) or safe_labels.get("Player.Duration")

        state["playing_item"] = {
            "title": item.get("title") or safe_labels.get("Player.Title") or "Unknown",
            "show": item.get("showtitle") or None,
            "file": item.get("file") or None,
            "time": t_str,
            "duration": d_str,
            "percentage": round(props.get("percentage", 0.0), 1) if isinstance(props, dict) and props.get("percentage") is not None else 0.0,
            "type": player_type,
        }
    else:
        # No player active
        if state["current_window"] in ("Home", "Videos", "Settings", "AddonBrowser"):
            state["player_state"] = "browse_mode"
        else:
            state["player_state"] = "stopped"

    return state


def query_jellyfin(base_url: str, timeout: float = 2.0) -> dict:
    """Gather Jellyfin server and ClassicTV plugin status via HTTP."""
    jf_state = {
        "online": False,
        "version": None,
        "server_name": None,
        "classictv_plugin_online": False,
        "channel_count": 0,
        "channels": [],
    }

    # Public server info
    try:
        req = urllib.request.Request(f"{base_url.rstrip('/')}/System/Info/Public", headers={"User-Agent": "ClassicTV-Status/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                jf_state["online"] = True
                jf_state["version"] = data.get("Version")
                jf_state["server_name"] = data.get("ServerName")
    except Exception:
        return jf_state

    # ClassicTV channels
    try:
        req = urllib.request.Request(f"{base_url.rstrip('/')}/ClassicTV/channels", headers={"User-Agent": "ClassicTV-Status/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                body = json.loads(resp.read().decode("utf-8"))
                channels = body.get("Channels", body) if isinstance(body, dict) else body
                if isinstance(channels, list):
                    jf_state["classictv_plugin_online"] = True
                    jf_state["channel_count"] = len(channels)
                    jf_state["channels"] = [
                        {
                            "id": c.get("id") or c.get("Id"),
                            "number": c.get("number") or c.get("Number"),
                            "name": c.get("name") or c.get("Name"),
                            "enabled": c.get("enabled", c.get("Enabled", True)),
                        }
                        for c in channels
                    ]
    except Exception:
        pass

    return jf_state


def query_guide_status(now_unix: int | None = None) -> dict:
    """Inspect the pre-rendered TV Guide cache directory on disk."""
    if now_unix is None:
        now_unix = int(time.time())

    chunk_sec = 900
    target_horizon_chunks = 16  # 4 hours
    current_chunk_start = (now_unix // chunk_sec) * chunk_sec
    current_chunk_id = f"guide-{current_chunk_start}"

    cache_dir_str = os.environ.get(
        "CLASSICTV_GUIDE_CACHE_DIR",
        os.path.expanduser("~/Library/Application Support/jellyfin/data/classictv-guide"),
    )
    cache_dir = Path(cache_dir_str)
    preview_path = WEB_DIR / "guide" / "preview.mp4"

    state = {
        "cache_dir": str(cache_dir),
        "current_chunk_id": current_chunk_id,
        "active_chunk_ready": False,
        "rendering_in_progress": False,
        "chunks_total": 0,
        "forward_chunks": 0,
        "target_chunks": target_horizon_chunks,
        "forward_hours": 0.0,
        "total_size_mb": 0.0,
        "latest_chunk_end": None,
        "preview_available": preview_path.exists(),
    }

    if not cache_dir.exists():
        return state

    chunk_re = re.compile(r"^guide-(\d+)\.mp4$", re.IGNORECASE)
    total_bytes = 0
    chunk_timestamps: list[int] = []

    try:
        for entry in cache_dir.iterdir():
            if entry.name.endswith(".tmp.mp4"):
                state["rendering_in_progress"] = True
                continue
            m = chunk_re.match(entry.name)
            if not m:
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            if st.st_size <= 0:
                continue
            ts = int(m.group(1))
            chunk_timestamps.append(ts)
            total_bytes += st.st_size
    except Exception:
        return state

    chunk_timestamps.sort()
    state["chunks_total"] = len(chunk_timestamps)
    state["total_size_mb"] = round(total_bytes / (1024 * 1024), 1)
    state["active_chunk_ready"] = current_chunk_start in chunk_timestamps

    forward = [ts for ts in chunk_timestamps if ts >= current_chunk_start]
    state["forward_chunks"] = len(forward)
    state["forward_hours"] = round((len(forward) * chunk_sec) / 3600.0, 2)
    if forward:
        latest_end = max(forward) + chunk_sec
        state["latest_chunk_end"] = datetime.datetime.fromtimestamp(
            latest_end, tz=datetime.timezone.utc
        ).isoformat()

    return state


def load_incidents() -> list:
    """Load historical incident records."""
    if not INCIDENTS_JSON_PATH.exists():
        return []
    try:
        with open(INCIDENTS_JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_incidents(incidents: list) -> None:
    """Save incident records safely."""
    INCIDENTS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = INCIDENTS_JSON_PATH.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(incidents, f, indent=2)
    temp_path.replace(INCIDENTS_JSON_PATH)


def probe_media_file(file_path: str) -> dict:
    """Run ffprobe on a local media file to extract codec/profile/dimensions and S905X warnings."""
    default = {
        "codec": "unknown",
        "profile": "n/a",
        "dimensions": "n/a",
        "pixel_format": "n/a",
        "warnings": [],
    }
    if not file_path or file_path == "Unavailable" or not os.path.exists(file_path):
        return default
    try:
        out = subprocess.check_output(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_streams",
                "-select_streams",
                "v:0",
                file_path,
            ],
            timeout=5,
            text=True,
        )
        streams = (json.loads(out).get("streams") or [])
        if not streams:
            return default
        s = streams[0]
        codec = s.get("codec_name") or "unknown"
        profile = s.get("profile") or "n/a"
        w, h = s.get("width"), s.get("height")
        dims = f"{w}x{h}" if w and h else "n/a"
        pix = s.get("pix_fmt") or "n/a"
        warnings = []
        if "10" in pix or "10" in str(profile):
            warnings.append("10-bit video stream (can stress Amlogic S905X decoder)")
        if codec == "hevc" and (s.get("level") or 0) >= 150:
            warnings.append(f"HEVC level {s.get('level')} (>= 5.0)")
        return {
            "codec": codec,
            "profile": str(profile),
            "dimensions": dims,
            "pixel_format": pix,
            "warnings": warnings,
        }
    except Exception:
        return default


def fetch_kodi_old_log_tail(sweet_host: str) -> list[str]:
    """Best-effort SSH fetch of recent errors/ClassicTV lines from kodi.old.log."""
    try:
        out = subprocess.check_output(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=4",
                f"{SSH_USER}@{sweet_host}",
                "tr -d '\\000' < /storage/.kodi/temp/kodi.old.log 2>/dev/null | grep -iE 'error|fatal|ClassicTV|OutputPicture|amlogic' | tail -n 12",
            ],
            timeout=6,
            text=True,
        )
        return [line.strip() for line in out.splitlines() if line.strip()]
    except Exception:
        return []


def record_incident(
    incidents: list,
    *,
    incident_type: str,
    severity: str,
    summary: str,
    timestamp: str,
    prev_sp: dict,
    evidence: list[str],
    resolved: bool = False,
    sweet_host: str | None = None,
) -> list:
    """Prepend a new incident to incidents.json unless a matching one was recently recorded."""
    if incidents:
        latest = incidents[0]
        if latest.get("type") == incident_type and latest.get("timestamp") == timestamp:
            return incidents
        try:
            latest_dt = datetime.datetime.fromisoformat(latest.get("timestamp", ""))
            curr_dt = datetime.datetime.fromisoformat(timestamp)
            if latest.get("type") == incident_type and abs((curr_dt - latest_dt).total_seconds()) < 900:
                return incidents
        except Exception:
            pass

    playing = prev_sp.get("playing_item") or {}
    season = playing.get("season")
    episode = playing.get("episode")
    se_str = f"S{int(season):02d}E{int(episode):02d}" if isinstance(season, int) and isinstance(episode, int) and season >= 0 and episode >= 0 else ""

    file_path = playing.get("file") or "Unavailable"
    full_evidence = list(evidence)
    if sweet_host:
        full_evidence.extend(fetch_kodi_old_log_tail(sweet_host))

    entry = {
        "timestamp": timestamp,
        "type": incident_type,
        "severity": severity,
        "resolved": resolved,
        "summary": summary,
        "channel": {
            "number": prev_sp.get("channel_number") or "?",
            "name": prev_sp.get("channel_name") or "Unknown",
        },
        "item": {
            "series": playing.get("showtitle") or playing.get("title") or "Unknown",
            "season_episode": se_str,
            "title": playing.get("title") or "Unknown",
            "file_path": file_path,
        },
        "file_analysis": probe_media_file(file_path),
        "evidence": full_evidence,
    }
    updated = [entry] + list(incidents)
    updated = updated[:50]
    save_incidents(updated)
    return updated


def update_status_json(sweet_host: str, jf_url: str) -> dict:
    """Perform a full check and update web/status.json."""
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now_dt.isoformat()

    prev_status = None
    if STATUS_JSON_PATH.exists():
        try:
            with open(STATUS_JSON_PATH, "r", encoding="utf-8") as f:
                prev_status = json.load(f)
        except Exception:
            prev_status = None

    ping_ms = get_ping_latency(sweet_host)
    kodi_state = query_kodi(sweet_host) if ping_ms is not None else {"reachable": False, "player_state": "offline"}
    jf_state = query_jellyfin(jf_url)
    incidents = load_incidents()

    # Gap and Reboot Detection
    prev_sp = (prev_status or {}).get("sweet_potato", {})
    prev_tracking = (prev_status or {}).get("tracking", {})
    prev_uptime = prev_sp.get("uptime")
    prev_uptime_sec = parse_uptime_seconds(prev_uptime)
    curr_online = ping_ms is not None
    curr_uptime = kodi_state.get("uptime")
    curr_uptime_sec = parse_uptime_seconds(curr_uptime)

    offline_since = prev_tracking.get("offline_since")
    last_seen_online = prev_tracking.get("last_seen_online")
    last_reboot_at = prev_tracking.get("last_reboot_at")
    reboot_reason = prev_tracking.get("reboot_reason")

    alert = None

    if curr_online:
        last_seen_online = now_iso
        # Check if Sweet Potato just recovered from an offline gap
        if offline_since:
            try:
                off_dt = datetime.datetime.fromisoformat(offline_since)
                gap_sec = (now_dt - off_dt).total_seconds()
            except Exception:
                gap_sec = 0.0
            if gap_sec >= 120:
                last_reboot_at = now_iso
                reboot_reason = f"Recovered from {int(gap_sec // 60)}m offline gap"
                incidents = record_incident(
                    incidents,
                    incident_type="OFFLINE_RECOVERY",
                    severity="WARNING",
                    summary=f"Sweet Potato recovered from {int(gap_sec // 60)}m offline gap",
                    timestamp=now_iso,
                    prev_sp=prev_sp,
                    evidence=[reboot_reason, f"Previous uptime: {prev_uptime or 'unknown'}", f"Current uptime: {curr_uptime or 'unknown'}"],
                    resolved=True,
                    sweet_host=sweet_host,
                )
            offline_since = None

        # Check for sudden uptime reset (e.g. current uptime < previous uptime)
        if curr_uptime_sec is not None and prev_uptime_sec is not None:
            if curr_uptime_sec < (prev_uptime_sec - 10):
                last_reboot_at = now_iso
                reboot_reason = f"Uptime reset from {prev_uptime} to {curr_uptime}"
                incidents = record_incident(
                    incidents,
                    incident_type="UNEXPECTED_REBOOT",
                    severity="CRITICAL",
                    summary=f"Sweet Potato rebooted unexpectedly ({prev_uptime} -> {curr_uptime})",
                    timestamp=now_iso,
                    prev_sp=prev_sp,
                    evidence=[reboot_reason, f"Previous player state: {prev_sp.get('player_state') or 'unknown'}"],
                    resolved=True,
                    sweet_host=sweet_host,
                )
        elif curr_uptime_sec is not None and curr_uptime_sec < 180 and not last_reboot_at:
            last_reboot_at = now_iso
            reboot_reason = f"Fresh boot detected (uptime {curr_uptime})"

        # Keep reboot alert visible for 30 minutes after event
        if last_reboot_at:
            try:
                rb_dt = datetime.datetime.fromisoformat(last_reboot_at)
                since_rb = (now_dt - rb_dt).total_seconds()
                if since_rb < 1800:
                    alert = {
                        "level": "warning",
                        "type": "REBOOT_DETECTED",
                        "title": "Recent Reboot Detected",
                        "timestamp": last_reboot_at,
                        "message": reboot_reason or "Sweet Potato recently rebooted.",
                    }
            except Exception:
                pass
    else:
        # Device is currently offline
        if not offline_since:
            # If there is a recent incident, anchor offline_since to the crash timestamp
            if incidents and incidents[0].get("timestamp"):
                offline_since = incidents[0]["timestamp"]
            else:
                offline_since = (prev_status or {}).get("timestamp", now_iso)

        try:
            off_dt = datetime.datetime.fromisoformat(offline_since)
            gap_sec = (now_dt - off_dt).total_seconds()
        except Exception:
            gap_sec = 0.0

        if gap_sec >= 120:
            minutes_off = int(gap_sec // 60)
            alert = {
                "level": "critical",
                "type": "OFFLINE_GAP",
                "title": f"Sweet Potato Offline ({minutes_off}m)",
                "timestamp": offline_since,
                "gap_seconds": gap_sec,
                "message": f"Sweet Potato has been unreachable for {minutes_off} minutes (crash occurred at {offline_since}).",
            }
            incidents = record_incident(
                incidents,
                incident_type="OFFLINE_GAP",
                severity="CRITICAL",
                summary=f"Sweet Potato went unreachable (offline since {offline_since})",
                timestamp=offline_since,
                prev_sp=prev_sp,
                evidence=[f"Last seen online: {last_seen_online or 'unknown'}", f"Offline duration: {minutes_off}m"],
                resolved=False,
            )

    # Load re-encode queue status if present
    queue_data = None
    queue_file = WEB_DIR / "reencode_queue.json"
    if queue_file.exists():
        try:
            with open(queue_file, "r", encoding="utf-8") as f:
                q = json.load(f)
                items = q.get("items", {})
                reencoding_item = next((it for it in items.values() if it.get("status") == "reencoding"), None)
                next_up = None
                pending = [it for it in items.values() if it.get("status") == "pending"]
                if pending:
                    pending.sort(key=lambda x: (-x.get("effective_priority", 100), x.get("next_air_time") or float("inf")))
                    next_up = pending[0]

                queue_data = {
                    "total_items": q.get("total_items", len(items)),
                    "pending": q.get("pending", len(pending)),
                    "completed": q.get("completed", 0),
                    "reencoding": q.get("reencoding", 0),
                    "failed": q.get("failed", 0),
                    "updated_at": q.get("updated_at"),
                    "current_job": {
                        "series": reencoding_item.get("series"),
                        "season": reencoding_item.get("season"),
                        "episode": reencoding_item.get("episode"),
                        "title": reencoding_item.get("title"),
                        "channel": reencoding_item.get("next_air_channel"),
                        "priority": reencoding_item.get("effective_priority"),
                    } if reencoding_item else None,
                    "next_up": {
                        "series": next_up.get("series"),
                        "season": next_up.get("season"),
                        "episode": next_up.get("episode"),
                        "title": next_up.get("title"),
                        "channel": next_up.get("next_air_channel"),
                        "priority": next_up.get("effective_priority"),
                    } if next_up else None,
                }
        except Exception:
            pass

    guide_data = query_guide_status(int(now_dt.timestamp()))

    status_data = {
        "timestamp": now_iso,
        "sweet_potato": {
            "host": sweet_host,
            "online": curr_online,
            "ping_ms": ping_ms,
            "uptime": curr_uptime,
            "kodi_version": kodi_state.get("kodi_version"),
            "player_state": kodi_state.get("player_state"),
            "channel_number": kodi_state.get("channel_number"),
            "channel_name": kodi_state.get("channel_name"),
            "current_window": kodi_state.get("current_window"),
            "playing_item": kodi_state.get("playing_item"),
            "alert": alert,
        },
        "tracking": {
            "offline_since": offline_since,
            "last_seen_online": last_seen_online,
            "last_reboot_at": last_reboot_at,
            "reboot_reason": reboot_reason,
        },
        "jellyfin": {
            "url": jf_url,
            "online": jf_state.get("online", False),
            "version": jf_state.get("version"),
            "server_name": jf_state.get("server_name"),
            "classictv_plugin_online": jf_state.get("classictv_plugin_online", False),
            "channel_count": jf_state.get("channel_count", 0),
        },
        "incidents_count": len(incidents),
        "latest_incident": incidents[0] if incidents else None,
        "reencode_queue": queue_data,
        "guide_channel": guide_data,
    }

    WEB_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = STATUS_JSON_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(status_data, f, indent=2)
    tmp_path.replace(STATUS_JSON_PATH)

    return status_data


def print_terminal_summary(status: dict) -> None:
    """Pretty-print live status and autopsy findings to stdout."""
    sp = status.get("sweet_potato", {})
    jf = status.get("jellyfin", {})
    inc = status.get("latest_incident")

    # ANSI Colors
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RESET = "\033[0m"

    print(f"\n{BOLD}{CYAN}=== Classic TV 4.0 System Status ==={RESET}")
    print(f"{DIM}Timestamp: {status.get('timestamp')}{RESET}\n")

    # Sweet Potato
    sp_status = f"{GREEN}● ONLINE{RESET}" if sp.get("online") else f"{RED}● OFFLINE{RESET}"
    ping_str = f"({sp.get('ping_ms'):.1f} ms)" if sp.get("ping_ms") is not None else ""
    uptime_str = f"| Uptime: {sp.get('uptime')}" if sp.get("uptime") else ""
    print(f"{BOLD}Sweet Potato ({sp.get('host')}):{RESET} {sp_status} {ping_str} {uptime_str}")

    alert = sp.get("alert")
    if alert:
        alert_col = RED if alert.get("level") == "critical" else YELLOW
        print(f"  {alert_col}{BOLD}⚠ ALERT [{alert.get('title')}]: {alert.get('message')}{RESET}")

    player_state = sp.get("player_state", "unknown").upper()
    state_color = GREEN if player_state == "PLAYING" else (YELLOW if "BROWSE" in player_state or player_state == "PAUSED" else RED)
    print(f"  Player Mode : {state_color}{player_state}{RESET} (Window: {sp.get('current_window', 'N/A')})")

    if sp.get("channel_name") or sp.get("channel_number"):
        ch_num = sp.get("channel_number") or "?"
        ch_name = sp.get("channel_name") or "Unknown"
        print(f"  Channel     : {BOLD}CH {ch_num} — {ch_name}{RESET}")

    item = sp.get("playing_item")
    if item:
        title = item.get("title") or "Unknown"
        show = f"{item.get('show')} — " if item.get("show") else ""
        elapsed = item.get("time") or "00:00"
        dur = item.get("duration") or "00:00"
        pct = item.get("percentage", 0)
        print(f"  Now Playing : {BOLD}{show}{title}{RESET} ({elapsed} / {dur}, {pct}%)")

    # Jellyfin
    jf_status = f"{GREEN}● ONLINE{RESET}" if jf.get("online") else f"{RED}● OFFLINE{RESET}"
    plugin_status = f"{GREEN}● ACTIVE{RESET}" if jf.get("classictv_plugin_online") else f"{RED}● OFFLINE{RESET}"
    print(f"\n{BOLD}Jellyfin Server:{RESET} {jf_status} (v{jf.get('version', 'N/A')})")
    print(f"  ClassicTV Plugin : {plugin_status} ({jf.get('channel_count', 0)} channels configured)")

    # Re-encode Queue
    rq = status.get("reencode_queue")
    if rq:
        print(f"\n{BOLD}{CYAN}=== Hardware Remediation Queue ==={RESET}")
        q_pend = rq.get("pending", 0)
        q_comp = rq.get("completed", 0)
        q_tot = rq.get("total_items", 0)
        print(f"  Queue Progress: {GREEN}{q_comp} completed{RESET} / {YELLOW}{q_pend} pending{RESET} ({q_tot} total)")
        cur_j = rq.get("current_job")
        if cur_j:
            print(f"  {BOLD}Re-encoding Now:{RESET} {CYAN}{cur_j['series']} S{cur_j['season']}E{cur_j['episode']}{RESET} — {cur_j['title']} (Priority: {cur_j['priority']})")
        next_j = rq.get("next_up")
        if next_j:
            ch_str = f"on {next_j['channel']}" if next_j.get("channel") else ""
            print(f"  {BOLD}Next in Line  :{RESET} {next_j['series']} S{next_j['season']}E{next_j['episode']} — {next_j['title']} {ch_str} (Priority: {next_j['priority']})")

    # TV Guide Channel (Ch 1)
    gc = status.get("guide_channel")
    if gc:
        print(f"\n{BOLD}{CYAN}=== TV Guide Channel 1 (Pre-Renderer) ==={RESET}")
        active_str = f"{GREEN}● READY{RESET}" if gc.get("active_chunk_ready") else f"{YELLOW}● STANDBY / WARMING{RESET}"
        if gc.get("rendering_in_progress"):
            active_str += f" {CYAN}(Rendering next chunk...){RESET}"
        print(f"  Current Slot  : {BOLD}{gc.get('current_chunk_id')}{RESET} ({active_str})")
        print(
            f"  Forward Buffer: {BOLD}{gc.get('forward_hours', 0)}h{RESET} "
            f"({gc.get('forward_chunks', 0)} / {gc.get('target_chunks', 16)} chunks, {gc.get('total_size_mb', 0)} MB on disk)"
        )

    # Incidents / Autopsy
    print(f"\n{BOLD}{CYAN}=== Incident & Crash Diagnostics ==={RESET}")
    if inc:
        sev = inc.get("severity", "WARNING")
        sev_color = RED if sev == "CRITICAL" else YELLOW
        print(f"{BOLD}Latest Incident:{RESET} [{sev_color}{sev}{RESET}] {inc.get('timestamp')}")
        print(f"  {BOLD}Type{RESET}     : {inc.get('type')}")
        print(f"  {BOLD}Summary{RESET}  : {inc.get('summary')}")
        if inc.get("item"):
            it = inc["item"]
            print(f"  {BOLD}Culprit{RESET}  : {it.get('series', '')} {it.get('season_episode', '')} — {it.get('title', '')}")
            print(f"  {BOLD}File{RESET}     : {it.get('file_path', 'N/A')}")
        if inc.get("file_analysis"):
            fa = inc["file_analysis"]
            print(f"  {BOLD}Format{RESET}   : {fa.get('codec')} ({fa.get('profile')}), {fa.get('dimensions')}, {fa.get('pixel_format')}")
            for w in fa.get("warnings", []):
                print(f"    {YELLOW}⚠ {w}{RESET}")
        if inc.get("resolved"):
            print(f"  {BOLD}Status{RESET}   : {GREEN}✓ RESOLVED{RESET} — {inc.get('resolution_note')}")
    else:
        print(f"  {GREEN}✓ No hardware crashes or incidents recorded.{RESET}")
    print()


def print_incidents_detail() -> None:
    """Print all incidents with full evidence log."""
    incidents = load_incidents()
    if not incidents:
        print("No incident history recorded.")
        return

    print(f"\n=== Incident History ({len(incidents)} total) ===\n")
    for i, inc in enumerate(incidents, 1):
        print(f"[{i}] {inc.get('timestamp')} | {inc.get('type')} ({inc.get('severity')})")
        print(f"    Summary   : {inc.get('summary')}")
        it = inc.get("item", {})
        print(f"    Item      : {it.get('series', '')} {it.get('season_episode', '')} {it.get('title', '')}")
        print(f"    File      : {it.get('file_path')}")
        fa = inc.get("file_analysis", {})
        if fa:
            print(f"    Encoding  : {fa.get('codec')} {fa.get('dimensions')} {fa.get('pixel_format')}")
            for w in fa.get("warnings", []):
                print(f"      - {w}")
        if inc.get("evidence"):
            print("    Evidence  :")
            for ev in inc["evidence"]:
                print(f"      • {ev}")
        if inc.get("resolved"):
            print(f"    Resolution: {inc.get('resolution_note')}")
        print("-" * 70)


def main():
    parser = argparse.ArgumentParser(description="Classic TV Status Monitor & Crash Autopsy")
    parser.add_argument("--host", default=DEFAULT_SWEET_POTATO_HOST, help="Sweet Potato IP address")
    parser.add_argument("--jellyfin", default=DEFAULT_JELLYFIN_URL, help="Jellyfin base URL")
    parser.add_argument("--json", action="store_true", help="Output raw status JSON")
    parser.add_argument("--incidents", action="store_true", help="Print incident history")
    parser.add_argument("--check-once", action="store_true", help="Run single check, update status.json and exit")
    parser.add_argument("--daemon", action="store_true", help="Run background monitor loop every 30s")
    parser.add_argument("--interval", type=int, default=30, help="Daemon check interval in seconds (default: 30)")
    args = parser.parse_args()

    if args.incidents:
        print_incidents_detail()
        return

    if args.check_once:
        data = update_status_json(args.host, args.jellyfin)
        print(f"Status updated at {data['timestamp']} -> {STATUS_JSON_PATH}")
        return

    if args.daemon:
        print(f"Starting Classic TV status monitor daemon (interval: {args.interval}s)...")
        while True:
            try:
                update_status_json(args.host, args.jellyfin)
            except Exception as exc:
                sys.stderr.write(f"Monitor error: {exc}\n")
                traceback.print_exc()
            time.sleep(args.interval)

    # Default action: check once, update JSON, and print terminal summary
    data = update_status_json(args.host, args.jellyfin)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print_terminal_summary(data)


if __name__ == "__main__":
    main()
