#!/usr/bin/env python3
"""
tools/audit_media_library.py

Audit the Jellyfin media library for video encoding anomalies known to crash
the Amlogic S905X hardware decoder / VPU (Le Potato / Sweet Potato running CoreELEC).

Crash Vectors Identified:
  1. Profile-Main 10-bit Contradiction (Judge Judy S01E02 bug):
     - HEVC stream header SPS claims Profile 'Main' (strictly 8-bit per H.265 spec),
       but stream contains 10-bit YUV samples (yuv420p10le).
     - Amlogic vhevc driver allocates an 8-bit CMA canvas buffer, causing 10-bit
       decode writes to overflow contiguous kernel memory and instantly freeze/reboot the SoC.
     - Severity: FATAL / CRITICAL (100% reproduceable crash on tune).

  2. Non-Mod-8 Width 10-bit HEVC (Cheers S05E24 bug):
     - 10-bit YUV (yuv420p10le) with width not divisible by 8 (e.g. 1270 px).
     - 1270 * 1.25 bytes/pixel = 1587.5 bytes per row (fractional byte stride).
     - Hardware DMA canvas raster controller triggers unhandled AXI bus error,
       freezing the VPU and locking up the board.
     - Severity: FATAL / CRITICAL (100% reproduceable crash on tune).

  3. Non-Mod-16 Width 10-bit HEVC:
     - 10-bit YUV with width divisible by 8 but not 16.
     - VPU canvas stride memory layout misalignment risk.
     - Severity: HIGH RISK.

  4. Non-Mod-8 / Non-Mod-16 Width in 8-bit HEVC:
     - 8-bit HEVC with unaligned raster line width.
     - Severity: MEDIUM RISK.

Usage:
    python3 tools/audit_media_library.py                       # Audit Classic TV channels
    python3 tools/audit_media_library.py --all                 # Audit entire library
    python3 tools/audit_media_library.py --critical-only       # Only Tier 1 Fatal crashes
    python3 tools/audit_media_library.py --channel sitcoms     # Filter by channel
    python3 tools/audit_media_library.py --show "Cheers"       # Filter by show
    python3 tools/audit_media_library.py --generate-script fix.sh  # Generate ffmpeg batch re-encoder
    python3 tools/audit_media_library.py --json                # Export as JSON
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

DEFAULT_SERVER = os.environ.get("SERVER_HOST", "localhost")
DEFAULT_LOCAL_DB = os.path.expanduser("~/Library/Application Support/jellyfin/data/jellyfin.db")
DEFAULT_CHANNELS_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "channels", "channels-v4.json"
)

# Remote DB path on jfmac
REMOTE_DB_PATH = "$HOME/Library/Application Support/jellyfin/data/jellyfin.db"


def fetch_library_streams(server_host: Optional[str] = None, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Fetch all video stream info from jellyfin.db, either locally or over SSH.
    """
    sql_query = """
    SELECT 
        b.Id, b.SeriesName, b.Name, b.ParentIndexNumber, b.IndexNumber, b.Path, b.Type,
        m.Codec, m.Profile, m.PixelFormat, m.BitDepth, m.Width, m.Height, m.Level
    FROM MediaStreamInfos m
    JOIN BaseItems b ON m.ItemId = b.Id
    WHERE m.Codec IS NOT NULL AND m.StreamType = 1;
    """
    
    use_local = False
    if db_path and os.path.exists(db_path):
        use_local = True
    elif not server_host and os.path.exists(DEFAULT_LOCAL_DB):
        use_local = True
        db_path = DEFAULT_LOCAL_DB

    if use_local:
        import sqlite3
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        rows = cur.execute(sql_query).fetchall()
        conn.close()
    else:
        host = server_host or DEFAULT_SERVER
        # Run query remotely over SSH and return JSON
        remote_py = f"""
import sqlite3, json, os
db = sqlite3.connect(os.path.expanduser('{REMOTE_DB_PATH}'))
cur = db.cursor()
rows = cur.execute('''{sql_query}''').fetchall()
print(json.dumps(rows))
"""
        cmd = ["ssh", host, f"python3 -c \"{remote_py}\""]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to query jellyfin.db on {host}: {proc.stderr.strip()}")
        rows = json.loads(proc.stdout)

    items = []
    for r in rows:
        item_id, series, name, s_idx, ep_idx, path, item_type, codec, prof, pix_fmt, depth, w, h, lvl = r
        clean_id = (item_id or "").replace("-", "").lower()
        items.append({
            "id": clean_id,
            "raw_id": item_id,
            "series": series or (name if "Movie" in (item_type or "") else ""),
            "title": name,
            "season": s_idx,
            "episode": ep_idx,
            "path": path,
            "type": item_type,
            "codec": (codec or "").lower(),
            "profile": prof or "",
            "pixel_format": pix_fmt or "",
            "bit_depth": depth or 8,
            "width": w or 0,
            "height": h or 0,
            "level": lvl,
        })
    return items


def load_channels_mapping(channels_path: str) -> Dict[str, List[Dict[str, str]]]:
    """
    Map item_id -> list of {channel_id, channel_name, show_name}
    """
    if not os.path.exists(channels_path):
        return {}
    with open(channels_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    mapping = defaultdict(list)
    for ch in data.get("channels", []):
        ch_id = ch.get("id", "")
        ch_name = ch.get("name", ch_id)
        for blk in ch.get("blocks", []):
            for show in blk.get("shows", []):
                show_name = show.get("name", "")
                for jid in show.get("jellyfinIds", []):
                    mapping[jid.lower()].append({
                        "channel_id": ch_id,
                        "channel_name": ch_name,
                        "show_name": show_name,
                    })
    return mapping


def analyze_item(item: Dict[str, Any]) -> List[Dict[str, str]]:
    """
    Check an item against known Amlogic hardware crash vectors.
    Returns list of issue objects: [{"severity": "FATAL"|"HIGH"|"MEDIUM", "code": "...", "desc": "..."}]
    """
    issues = []
    codec = item["codec"]
    prof = item["profile"]
    pix_fmt = item["pixel_format"]
    depth = item["bit_depth"]
    w = item["width"]
    h = item["height"]
    is_10bit = ("10" in pix_fmt) or (depth > 8)

    if codec == "hevc":
        # Vector 1: Profile Main + 10-bit Contradiction (Judge Judy bug)
        if prof == "Main" and is_10bit:
            issues.append({
                "severity": "FATAL",
                "code": "HEVC_PROFILE_10BIT_CONFLICT",
                "desc": (
                    f"Profile 'Main' (strictly 8-bit) with 10-bit stream ({pix_fmt}, {depth}-bit). "
                    "Causes CMA buffer overflow in Amlogic vhevc driver and instant SoC lockup."
                ),
            })

        # Vector 2: 10-bit Width not mod-8 (Cheers bug)
        if is_10bit and w > 0 and (w % 8 != 0):
            byte_stride = w * 1.25
            issues.append({
                "severity": "FATAL",
                "code": "HEVC_10BIT_WIDTH_NOT_MOD8",
                "desc": (
                    f"10-bit HEVC width {w}px is not mod-8 ({w % 8} remainder). "
                    f"Calculated row stride {byte_stride:.1f} bytes causes unhandled AXI bus lockup in VPU."
                ),
            })

        # Vector 3: 10-bit Width mod-8 but not mod-16
        elif is_10bit and w > 0 and (w % 16 != 0):
            issues.append({
                "severity": "HIGH",
                "code": "HEVC_10BIT_WIDTH_NOT_MOD16",
                "desc": f"10-bit HEVC width {w}px is mod-8 but not mod-16 ({w % 16} remainder). Stride alignment risk.",
            })

        # Vector 4: 8-bit Width not mod-8
        elif not is_10bit and w > 0 and (w % 8 != 0):
            issues.append({
                "severity": "HIGH",
                "code": "HEVC_8BIT_WIDTH_NOT_MOD8",
                "desc": f"8-bit HEVC width {w}px is not mod-8 ({w % 8} remainder).",
            })

        # Vector 5: 8-bit Width mod-8 but not mod-16
        elif not is_10bit and w > 0 and (w % 16 != 0):
            issues.append({
                "severity": "MEDIUM",
                "code": "HEVC_8BIT_WIDTH_NOT_MOD16",
                "desc": f"8-bit HEVC width {w}px is not mod-16 ({w % 16} remainder).",
            })

        # Vector 6: Height not mod-8
        if h > 0 and (h % 8 != 0):
            issues.append({
                "severity": "MEDIUM",
                "code": "HEVC_HEIGHT_NOT_MOD8",
                "desc": f"HEVC height {h}px is not mod-8 ({h % 8} remainder).",
            })

    return issues


def generate_ffmpeg_command(item: Dict[str, Any]) -> str:
    """
    Generate safe ffmpeg command to re-encode the file to standard 8-bit H.264
    or compliant padded mod-16 container.
    """
    src_path = item["path"]
    if not src_path:
        return f"# Item {item['id']} has no path on disk"
    
    # Target dimensions: pad to mod-16 if needed
    w = item["width"]
    h = item["height"]
    pad_w = ((w + 15) // 16) * 16 if w > 0 else 1280
    pad_h = ((h + 15) // 16) * 16 if h > 0 else 720

    dirname = os.path.dirname(src_path)
    basename = os.path.basename(src_path)
    tmp_path = os.path.join(dirname, f".reencode_{basename}")
    orig_backup = os.path.join(dirname, f"{os.path.splitext(basename)[0]}.orig.mkv")

    # Re-encode to clean libx264 8-bit yuv420p with copy audio/subtitles
    cmd = (
        f'ffmpeg -hide_banner -loglevel warning -stats -i "{src_path}" '
        f'-c:v libx264 -crf 18 -preset medium -pix_fmt yuv420p '
        f'-vf "pad={pad_w}:{pad_h}:({pad_w}-iw)/2:({pad_h}-ih)/2" '
        f'-c:a copy -c:s copy -c:d copy '
        f'"{tmp_path}" && '
        f'mv "{src_path}" "{orig_backup}" && '
        f'mv "{tmp_path}" "{src_path}"'
    )
    return cmd


def main():
    parser = argparse.ArgumentParser(description="Audit Jellyfin library for Amlogic hardware crash vectors.")
    parser.add_argument("--server", help=f"SSH server host (default: {DEFAULT_SERVER})", default=None)
    parser.add_argument("--db", help="Local path to jellyfin.db (bypasses SSH)", default=None)
    parser.add_argument("--channels", help="Path to channels-v4.json", default=DEFAULT_CHANNELS_JSON)
    parser.add_argument("--all", action="store_true", help="Audit all library items (default: focus on Classic TV)")
    parser.add_argument("--critical-only", action="store_true", help="Show only FATAL crash issues")
    parser.add_argument("--channel", help="Filter by channel ID or name")
    parser.add_argument("--show", help="Filter by series/show name")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument("--generate-script", help="Write a bash re-encoding script for flagged files")
    args = parser.parse_args()

    # Load stream info
    try:
        items = fetch_library_streams(server_host=args.server, db_path=args.db)
    except Exception as e:
        sys.stderr.write(f"Error fetching library streams: {e}\n")
        sys.exit(1)

    # Load channels mapping
    channels_map = load_channels_mapping(args.channels)

    # Run analysis
    audited = []
    counts_by_severity = defaultdict(int)
    counts_by_code = defaultdict(int)
    series_affected = defaultdict(lambda: {"fatal": 0, "high": 0, "medium": 0, "total": 0})
    channels_affected = defaultdict(lambda: {"fatal": 0, "high": 0, "medium": 0, "total": 0})

    for item in items:
        # Check channel presence
        item_channels = channels_map.get(item["id"], [])
        item["channels"] = item_channels
        in_classictv = len(item_channels) > 0

        # Filtering
        if not args.all and not in_classictv:
            continue
        if args.channel:
            ch_match = False
            for ch in item_channels:
                if args.channel.lower() in ch["channel_id"].lower() or args.channel.lower() in ch["channel_name"].lower():
                    ch_match = True
                    break
            if not ch_match:
                continue
        if args.show and args.show.lower() not in (item["series"] or "").lower():
            continue

        issues = analyze_item(item)
        if not issues:
            continue

        max_severity = "MEDIUM"
        if any(iss["severity"] == "FATAL" for iss in issues):
            max_severity = "FATAL"
        elif any(iss["severity"] == "HIGH" for iss in issues):
            max_severity = "HIGH"

        if args.critical_only and max_severity != "FATAL":
            continue

        item["issues"] = issues
        item["max_severity"] = max_severity
        audited.append(item)

        counts_by_severity[max_severity] += 1
        for iss in issues:
            counts_by_code[iss["code"]] += 1

        s_name = item["series"] or "Unknown Show"
        series_affected[s_name]["total"] += 1
        series_affected[s_name][max_severity.lower()] += 1

        for ch in item_channels:
            c_name = ch["channel_name"]
            channels_affected[c_name]["total"] += 1
            channels_affected[c_name][max_severity.lower()] += 1

    # Output JSON if requested
    if args.json:
        out = {
            "summary": {
                "total_flagged": len(audited),
                "counts_by_severity": dict(counts_by_severity),
                "counts_by_code": dict(counts_by_code),
            },
            "channels_affected": dict(channels_affected),
            "series_affected": dict(series_affected),
            "items": audited,
        }
        print(json.dumps(out, indent=2))
        return

    # Generate bash re-encode script if requested
    if args.generate_script:
        with open(args.generate_script, "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\n")
            f.write("# Auto-generated batch re-encode script for Classic TV crash-prone media\n")
            f.write("set -euo pipefail\n\n")
            f.write(f"# Total files to re-encode: {len(audited)}\n\n")
            for item in audited:
                f.write(f"# [{item['max_severity']}] {item['series']} - S{item['season']}E{item['episode']} - {item['title']}\n")
                for iss in item["issues"]:
                    f.write(f"#   Issue: {iss['code']} -> {iss['desc']}\n")
                f.write(generate_ffmpeg_command(item) + "\n\n")
        print(f"Generated re-encoding script for {len(audited)} files: {args.generate_script}")
        return

    # Print summary
    scope_desc = "Entire Jellyfin Library" if args.all else "Active Classic TV Channels"
    print("=" * 80)
    print(f" CLASSIC TV HARDWARE CRASH AUDIT REPORT ({scope_desc})")
    print("=" * 80)
    print(f"Total Flagged Items:  {len(audited)}")
    print(f"  - FATAL (Instant SoC lockup / VPU bus error): {counts_by_severity['FATAL']}")
    print(f"  - HIGH RISK (Stride / Alignment hazard):       {counts_by_severity['HIGH']}")
    print(f"  - MEDIUM RISK (Non-standard dimensions):       {counts_by_severity['MEDIUM']}")
    print("-" * 80)
    print("CRASH VECTOR BREAKDOWN:")
    for code, count in sorted(counts_by_code.items(), key=lambda x: x[1], reverse=True):
        print(f"  {code:<32}: {count} files")
    print("-" * 80)

    if channels_affected:
        print("IMPACTED CLASSIC TV CHANNELS:")
        for ch_name, stats in sorted(channels_affected.items(), key=lambda x: x[1]["fatal"], reverse=True):
            print(f"  {ch_name:<24}: {stats['fatal']:3d} FATAL, {stats['high']:3d} HIGH, {stats['total']:3d} Total")
        print("-" * 80)

    print("TOP IMPACTED SHOWS:")
    for s_name, stats in sorted(series_affected.items(), key=lambda x: x[1]["fatal"], reverse=True)[:25]:
        print(f"  {s_name:<30}: {stats['fatal']:3d} FATAL, {stats['high']:3d} HIGH, {stats['total']:3d} Total")
    print("=" * 80)
    print("Run with --critical-only to see only fatal files.")
    print("Run with --generate-script fix_classictv.sh to generate safe re-encoding commands.")


if __name__ == "__main__":
    main()
