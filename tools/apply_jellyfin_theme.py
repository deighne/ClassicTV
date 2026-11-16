#!/usr/bin/env python3
"""Apply the Classic TV Jellyfin web theme (custom CSS + login banner).

Reads web/jellyfin-theme/custom.css and login-disclaimer.html and sets them as
the server's branding. Dry run by default (shows what would change); --apply
needs an admin API key in $JELLYFIN_API_KEY (Dashboard > API Keys) and saves the
current branding to web/jellyfin-theme/backup-<timestamp>.json first, so a bad
theme can be rolled back with --restore <file>. No Jellyfin restart is needed;
hard-refresh the browser afterwards.

    python3 tools/apply_jellyfin_theme.py
    JELLYFIN_API_KEY=... python3 tools/apply_jellyfin_theme.py --apply
    JELLYFIN_API_KEY=... python3 tools/apply_jellyfin_theme.py --restore web/jellyfin-theme/backup-....json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_HOST = os.environ.get("JELLYFIN_BASE_URL", "http://localhost:8096")
THEME_DIR = Path(__file__).resolve().parent.parent / "web" / "jellyfin-theme"


def build_branding(css: str, banner: str, current: dict) -> dict:
    """New branding options: our CSS and banner, everything else as-is."""
    new = dict(current)
    new["CustomCss"] = css
    new["LoginDisclaimer"] = banner
    return new


def _get_current(host: str) -> dict:
    # Public endpoint; returns CustomCss, LoginDisclaimer, SplashscreenEnabled.
    with urllib.request.urlopen(f"{host}/Branding/Configuration", timeout=15) as r:
        return json.load(r)


def _post(host: str, key: str, body: dict) -> None:
    req = urllib.request.Request(
        f"{host}/System/Configuration/branding",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f'MediaBrowser Token="{key}"',
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=15).read()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--restore", metavar="FILE")
    args = ap.parse_args()
    key = os.environ.get("JELLYFIN_API_KEY")

    if args.restore:
        if not key:
            print("Set JELLYFIN_API_KEY to restore.", file=sys.stderr)
            return 2
        _post(args.host, key, json.loads(Path(args.restore).read_text()))
        print(f"Restored branding from {args.restore}")
        return 0

    css = (THEME_DIR / "custom.css").read_text()
    banner = (THEME_DIR / "login-disclaimer.html").read_text()
    current = _get_current(args.host)
    new = build_branding(css, banner, current)

    print(f"CustomCss:       {len(current.get('CustomCss') or '')} -> {len(css)} chars")
    print(f"LoginDisclaimer: {len(current.get('LoginDisclaimer') or '')} -> {len(banner)} chars")
    if not args.apply:
        print("\nDry run. Re-run with --apply to save.")
        return 0
    if not key:
        print("Set JELLYFIN_API_KEY to apply.", file=sys.stderr)
        return 2

    backup = THEME_DIR / f"backup-{time.strftime('%Y%m%d-%H%M%S')}.json"
    backup.write_text(json.dumps(current, indent=2))
    print(f"Backup written to {backup}")
    _post(args.host, key, new)
    print("Applied. Hard-refresh the browser (Cmd-Shift-R).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
