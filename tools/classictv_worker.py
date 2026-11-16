#!/usr/bin/env python3
"""
tools/classictv_worker.py

Background re-encoding worker for Classic TV media library.
Continuously processes files from the priority queue (highest priority first).

Key Features:
  - Schedule-aware: Re-checks schedule priorities so imminent broadcasts preempt lower priority items.
  - Safe zero-downtime swaps: Encodes to a hidden temporary file, backs up the original to .orig.mkv,
    and atomically moves the re-encoded file into place.
  - Video standards: Re-encodes using standard 8-bit H.264 (libx264, yuv420p, mod-16 padded),
    stream-copying all audio tracks and subtitles without transcoding.
  - Daemon mode: Can run as a persistent background daemon on jfmac.

Usage:
    python3 tools/classictv_worker.py --once             # Process single highest priority job and exit
    python3 tools/classictv_worker.py --count 5          # Process 5 jobs and exit
    python3 tools/classictv_worker.py --daemon           # Run continuously
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import classictv_queue

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("classictv-worker")

STOP_REQUESTED = False

def find_ffmpeg() -> str:
    override = os.environ.get("FFMPEG_BIN")
    if override and os.path.exists(override):
        return override
    for candidate in [
        "/Applications/Jellyfin.app/Contents/MacOS/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
    ]:
        if os.path.exists(candidate):
            return candidate
    which_bin = shutil.which("ffmpeg")
    if which_bin:
        return which_bin
    return "ffmpeg"

FFMPEG_BIN = find_ffmpeg()


def sig_handler(signum, frame):
    global STOP_REQUESTED
    logger.info(f"Signal {signum} received, stopping gracefully after current job...")
    STOP_REQUESTED = True


signal.signal(signal.SIGINT, sig_handler)
signal.signal(signal.SIGTERM, sig_handler)


class VolumeNotMountedError(Exception):
    """Raised when an external volume is not mounted."""
    pass


def reencode_file(job: Dict[str, Any]) -> bool:
    """
    Re-encode a single file to clean standard 8-bit H.264 with mod-16 padding.
    Returns True on success, False on failure.
    """
    src_path = job["path"]
    if src_path.startswith("/Volumes/"):
        parts = src_path.split("/")
        if len(parts) >= 3:
            vol_path = os.path.join("/Volumes", parts[2])
            if not (os.path.exists(vol_path) and os.path.ismount(vol_path)):
                logger.warning(f"Target volume is not mounted: {vol_path}. Skipping job without failing.")
                raise VolumeNotMountedError(f"Volume not mounted: {vol_path}")

    if not os.path.exists(src_path):
        logger.error(f"Source file not found: {src_path}")
        return False

    dirname = os.path.dirname(src_path)
    basename = os.path.basename(src_path)
    stem, ext = os.path.splitext(basename)

    tmp_path = os.path.join(dirname, f".reencode_{basename}")
    orig_backup = os.path.join(dirname, f"{stem}.orig.mkv")

    # Clean up any leftover temporary file from previous run
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    # Ensure mod-16 width and height
    w = job.get("width", 0)
    h = job.get("height", 0)
    pad_w = ((w + 15) // 16) * 16 if w > 0 else 1280
    pad_h = ((h + 15) // 16) * 16 if h > 0 else 720

    vf_filter = f"pad={pad_w}:{pad_h}:({pad_w}-iw)/2:({pad_h}-ih)/2"

    cmd = [
        FFMPEG_BIN,
        "-nostdin",
        "-hide_banner",
        "-loglevel", "warning",
        "-nostats",
        "-threads", "4",
        "-y",
        "-i", src_path,
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-vf", vf_filter,
        "-c:a", "copy",
        "-c:s", "copy",
        "-c:d", "copy",
        tmp_path,
    ]

    logger.info(f"Re-encoding: {job['series']} S{job['season']}E{job['episode']} ({basename})")
    logger.info(f"Filter: {vf_filter} -> {tmp_path}")
    start_t = time.time()

    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            logger.error(f"ffmpeg exited with return code {proc.returncode}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return False
    except Exception as e:
        logger.error(f"Failed to execute ffmpeg ({FFMPEG_BIN}): {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False

    elapsed = time.time() - start_t
    out_size_mb = os.path.getsize(tmp_path) / (1024 * 1024)
    logger.info(f"ffmpeg completed in {elapsed:.1f}s ({out_size_mb:.1f} MB)")

    # Atomic swap
    try:
        if not os.path.exists(orig_backup):
            try:
                os.rename(src_path, orig_backup)
            except OSError:
                subprocess.run(["mv", "-f", src_path, orig_backup], check=True)
        else:
            try:
                os.remove(src_path)
            except OSError:
                subprocess.run(["rm", "-f", src_path], check=True)
        try:
            os.rename(tmp_path, src_path)
        except OSError:
            subprocess.run(["mv", "-f", tmp_path, src_path], check=True)
        logger.info(f"Successfully swapped: {src_path} (backup at {orig_backup})")
        return True
    except Exception as e:
        logger.error(f"Failed to swap files into place: {e}")
        return False


def reset_orphaned_jobs(queue_path: str = classictv_queue.QUEUE_FILE) -> int:
    """Reset any jobs left in 'reencoding' status back to 'pending' on startup.

    Recovers items orphaned when a previous worker process was terminated mid-encode,
    and cleans up any partial .reencode_* temp files left on disk.
    """
    queue = classictv_queue.load_queue(queue_path)
    items = queue.get("items", {})
    reset_count = 0

    for item in items.values():
        if item.get("status") == "reencoding":
            name = f"{item.get('series', '')} S{item.get('season', '')}E{item.get('episode', '')}"
            logger.info(f"Resetting orphaned re-encoding job back to pending: {name}")
            item["status"] = "pending"
            item.pop("started_at", None)
            reset_count += 1

            src = item.get("path") or item.get("file_path")
            if src:
                dir_name = os.path.dirname(src)
                base_name = os.path.basename(src)
                tmp_path = os.path.join(dir_name, f".reencode_{base_name}")
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                        logger.info(f"Cleaned up stale temp file: {tmp_path}")
                    except Exception as e:
                        logger.warning(f"Could not remove stale temp file {tmp_path}: {e}")

    if reset_count > 0:
        classictv_queue.save_queue(queue, queue_path)
        logger.info(f"Reset {reset_count} orphaned job(s) in queue back to pending.")

    return reset_count


def run_worker_loop(once: bool = False, max_count: int = 0, queue_path: str = classictv_queue.QUEUE_FILE):
    processed = 0
    last_schedule_scan = 0.0

    logger.info(f"Classic TV Re-encode Worker started (queue: {queue_path})")

    # Clean up any jobs left hanging by a killed process or system reboot
    reset_orphaned_jobs(queue_path=queue_path)

    while not STOP_REQUESTED:
        now = time.time()

        # Rescan schedule every 15 minutes to reprioritise upcoming items
        if now - last_schedule_scan >= 900:
            logger.info("Rescanning broadcast schedule to refresh priorities...")
            try:
                res = classictv_queue.scan_schedule_and_boost_priorities(queue_path=queue_path)
                logger.info(f"Schedule scanned: {res.get('boosted_items', 0)} upcoming items prioritized.")
                last_schedule_scan = now
            except Exception as e:
                logger.warning(f"Schedule scan failed: {e}")

        # Pick highest priority pending job
        job = classictv_queue.get_next_job(queue_path=queue_path)
        if not job:
            logger.info("No pending jobs in queue.")
            if once or max_count > 0:
                break
            time.sleep(30)
            continue

        item_id = job["id"]
        queue = classictv_queue.load_queue(queue_path)
        if item_id in queue.get("items", {}):
            queue["items"][item_id]["status"] = "reencoding"
            queue["items"][item_id]["started_at"] = datetime.now(timezone.utc).isoformat()
            classictv_queue.save_queue(queue, queue_path)

        prio = job.get("effective_priority", 100)
        air_t = job.get("next_air_time")
        air_desc = f"airs in {(air_t - now)/3600.0:.1f}h on {job.get('next_air_channel')}" if air_t else "background"
        logger.info(f"Starting job [Priority {prio} - {air_desc}]: {job['series']} S{job['season']}E{job['episode']}")

        try:
            success = reencode_file(job)
        except VolumeNotMountedError:
            queue = classictv_queue.load_queue(queue_path)
            if item_id in queue.get("items", {}):
                queue["items"][item_id]["status"] = "pending"
                classictv_queue.save_queue(queue, queue_path)
            time.sleep(10)
            continue

        queue = classictv_queue.load_queue(queue_path)
        if item_id in queue.get("items", {}):
            it = queue["items"][item_id]
            if success:
                it["status"] = "completed"
                it["completed_at"] = datetime.now(timezone.utc).isoformat()
                logger.info(f"Completed job: {job['series']} S{job['season']}E{job['episode']}")
            else:
                it["attempts"] = it.get("attempts", 0) + 1
                if it["attempts"] >= 3:
                    it["status"] = "failed"
                    logger.error(f"Job failed 3 times, marked as failed: {job['series']}")
                else:
                    it["status"] = "pending"
                    logger.warning(f"Job failed (attempt {it['attempts']}/3), returning to queue")
            classictv_queue.save_queue(queue, queue_path)

        processed += 1
        if once or (max_count > 0 and processed >= max_count):
            break

        time.sleep(2)

    logger.info(f"Classic TV Worker shutting down. Processed {processed} jobs.")


def main():
    parser = argparse.ArgumentParser(description="Classic TV Re-encoding Background Worker")
    parser.add_argument("--once", action="store_true", help="Process one job and exit")
    parser.add_argument("--count", type=int, default=0, help="Process N jobs and exit")
    parser.add_argument("--daemon", action="store_true", help="Run continuously in background")
    parser.add_argument("--queue-file", default=classictv_queue.QUEUE_FILE, help="Path to reencode_queue.json")
    args = parser.parse_args()

    once = args.once or (not args.daemon and args.count == 0)
    run_worker_loop(once=once, max_count=args.count, queue_path=args.queue_file)


if __name__ == "__main__":
    main()
