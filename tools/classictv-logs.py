#!/usr/bin/env python3
"""
tools/classictv-logs.py

Merge Classic TV logs from the two machines that matter into one time-sorted
timeline, so a failure that spans the player and the server can be read in a
single pass instead of tailing two hosts and mentally interleaving timestamps.

Sources:
  * Sweet Potato (Le Potato / CoreELEC): Kodi log at /storage/.kodi/temp/kodi.log
    (and kodi.old.log). Timestamps are naive local time (America/Chicago).
  * jfmac: the newest Jellyfin log under
    ~/Library/Application Support/jellyfin/log/. Timestamps carry an offset.

By default only Classic TV lines are shown: Kodi's "[ClassicTV]..." and the
Jellyfin plugin's "Jellyfin.Plugin.ClassicTV..." / "ClassicTV ..." lines.
Use --all to include everything in the window, and --errors to show only
warnings/errors.

Examples:
    python3 tools/classictv-logs.py                  # last 60 min, Classic TV only
    python3 tools/classictv-logs.py --since 6h
    python3 tools/classictv-logs.py --errors --since 24h
    python3 tools/classictv-logs.py --all --since 15m
    python3 tools/classictv-logs.py --follow

Environment overrides:
    DEVICE_HOST   (default root@192.168.1.237)
    SERVER_HOST   (default dane@192.168.1.207)
    JELLYFIN_LOG_DIR (default ~/Library/Application Support/jellyfin/log)
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_DEVICE = os.environ.get("DEVICE_HOST", "root@kodi.local")
DEVICE_TZ = ZoneInfo(os.environ.get("DEVICE_TZ", "America/Chicago"))
DEFAULT_SERVER = os.environ.get("SERVER_HOST", "localhost")
DEFAULT_JELLYFIN_LOGDIR = os.environ.get(
    "JELLYFIN_LOG_DIR", "$HOME/Library/Application Support/jellyfin/log"
)

DEVICE_LOG = "/storage/.kodi/temp/kodi.log"
DEVICE_OLD_LOG = "/storage/.kodi/temp/kodi.old.log"

# Kodi:  "2026-09-19 23:39:23.591 T:3802     info <general>: [ClassicTV]..."
_KODI_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+)\b")

# Jellyfin: "[2026-09-19 19:06:52.186 -05:00] [INF] [19] Jellyfin.Plugin..."
_JELLYFIN_TS = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d+) ([+-]\d{2}:\d{2})\]"
)

_CLASSICTV_MARKERS = ("[ClassicTV]", "Jellyfin.Plugin.ClassicTV", "ClassicTV:")


@dataclass
class LogLine:
    ts: datetime  # timezone-aware UTC
    source: str  # "sweet-potato" | "jfmac"
    text: str  # the original line, trimmed of trailing newline
    level: str  # "WARN" | "ERROR" | "INFO"


def _run(cmd: list[str]) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (subprocess.TimeoutExpired, OSError) as exc:
        return 1, "", str(exc)


def _ssh(host: str, remote_cmd: str) -> tuple[int, str, str]:
    return _run(
        ["ssh", "-o", "ConnectTimeout=8", "-o", "BatchMode=yes", host, remote_cmd]
    )


def _parse_kodi_line(line: str) -> LogLine | None:
    m = _KODI_TS.match(line)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None
    # Kodi on CoreELEC logs in local time (America/Chicago).
    dt = dt.replace(tzinfo=DEVICE_TZ).astimezone(timezone.utc)
    return LogLine(ts=dt, source="sweet-potato", text=line.rstrip(), level=_level(line))


def _parse_jellyfin_line(line: str) -> LogLine | None:
    m = _JELLYFIN_TS.match(line)
    if not m:
        return None
    try:
        naive = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None
    sign = 1 if m.group(2).startswith("+") else -1
    hh, mm = (int(x) for x in m.group(2)[1:].split(":"))
    offset = timezone(sign * timedelta(hours=hh, minutes=mm))
    dt = naive.replace(tzinfo=offset).astimezone(timezone.utc)
    return LogLine(ts=dt, source="jfmac", text=line.rstrip(), level=_level(line))


def _level(line: str) -> str:
    low = line.lower()
    if any(m in low for m in ("error", "[err]", "fatal", "traceback", "exception")):
        return "ERROR"
    if any(m in low for m in ("warning", "warn", "[wrn]")):
        return "WARN"
    return "INFO"


def _wants(line: str, errors_only: bool, all_lines: bool) -> bool:
    if errors_only and _level(line) == "INFO":
        return False
    if not all_lines and not any(m in line for m in _CLASSICTV_MARKERS):
        return False
    return True


def fetch_device(
    host: str, max_lines: int, errors_only: bool, all_lines: bool
) -> list[LogLine]:
    # Grab a bounded tail of both current and rotated logs, then parse locally.
    remote = (
        f"for f in {DEVICE_OLD_LOG} {DEVICE_LOG}; do "
        f'[ -f "$f" ] && tail -n {max_lines} "$f"; done'
    )
    rc, out, err = _ssh(host, remote)
    if rc != 0 and not out:
        print(
            f"# warning: could not read device log from {host}: {err.strip()}",
            file=sys.stderr,
        )
        return []
    lines = []
    for raw in out.splitlines():
        parsed = _parse_kodi_line(raw)
        if parsed and _wants(parsed.text, errors_only, all_lines):
            lines.append(parsed)
    return lines


def fetch_server(
    host: str, logdir: str, max_lines: int, errors_only: bool, all_lines: bool
) -> list[LogLine]:
    # Normalise a literal ~/... to $HOME/... so it expands on the remote host
    # when wrapped in double quotes (and spaces in the path are preserved).
    remote_dir = (
        logdir.replace("~/", "$HOME/", 1) if logdir.startswith("~/") else logdir
    )
    remote = (
        f'd="{remote_dir}"; '
        f'f=$(ls -t "$d"/log_*.log 2>/dev/null | head -n 1); '
        f'[ -n "$f" ] && tail -n {max_lines} "$f"'
    )
    rc, out, err = _ssh(host, remote)
    if rc != 0 and not out:
        print(
            f"# warning: could not read Jellyfin log from {host}: {err.strip()}",
            file=sys.stderr,
        )
        return []
    lines = []
    for raw in out.splitlines():
        parsed = _parse_jellyfin_line(raw)
        if parsed and _wants(parsed.text, errors_only, all_lines):
            lines.append(parsed)
    return lines


def _fmt(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + "Z"


def _print(lines: list[LogLine]) -> list[LogLine]:
    for ln in lines:
        tag = f"[{ln.source}]"
        flag = ""
        if ln.level == "ERROR":
            flag = " !!"
        elif ln.level == "WARN":
            flag = " *"
        print(f"{_fmt(ln.ts)}  {tag:<15}{flag} {ln.text}")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Merge Classic TV logs from the Sweet Potato and jfmac.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument(
        "--since", default="60m", help="time window, e.g. 30m, 6h, 2d (default: 60m)"
    )
    ap.add_argument("--errors", action="store_true", help="show only warnings/errors")
    ap.add_argument(
        "--all", action="store_true", help="include non-ClassicTV lines in the window"
    )
    ap.add_argument(
        "--follow",
        action="store_true",
        help="poll and print new lines until interrupted",
    )
    ap.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="follow poll interval in seconds (default: 5)",
    )
    ap.add_argument(
        "--max-lines",
        type=int,
        default=4000,
        help="tail size per source before filtering (default: 4000)",
    )
    ap.add_argument("--device", default=DEFAULT_DEVICE, help="device ssh target")
    ap.add_argument("--server", default=DEFAULT_SERVER, help="server ssh target")
    ap.add_argument("--jellyfin-logdir", default=DEFAULT_JELLYFIN_LOGDIR)
    args = ap.parse_args()

    cutoff = _parse_since(args.since)

    def collect() -> list[LogLine]:
        lines = []
        lines += fetch_device(args.device, args.max_lines, args.errors, args.all)
        lines += fetch_server(
            args.server, args.jellyfin_logdir, args.max_lines, args.errors, args.all
        )
        lines = [ln for ln in lines if ln.ts >= cutoff]
        lines.sort(key=lambda ln: ln.ts)
        return lines

    if not args.follow:
        _print(collect())
        return 0

    # Follow mode: track the newest timestamp seen per source and print deltas.
    last_ts: datetime = cutoff
    print(
        f"# following Classic TV logs (since {args.since}); Ctrl-C to stop",
        file=sys.stderr,
    )
    try:
        while True:
            fresh = [ln for ln in collect() if ln.ts > last_ts]
            if fresh:
                _print(fresh)
                last_ts = max(ln.ts for ln in fresh)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n# stopped", file=sys.stderr)
    return 0


def _parse_since(value: str) -> datetime:
    now = datetime.now(timezone.utc)
    m = re.fullmatch(r"(\d+)([smhd])", value.strip().lower())
    if not m:
        print(
            f"error: --since must look like 30m / 6h / 2d (got {value!r})",
            file=sys.stderr,
        )
        sys.exit(2)
    qty = int(m.group(1))
    unit = m.group(2)
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * qty
    return now - timedelta(seconds=seconds)


if __name__ == "__main__":
    sys.exit(main())
