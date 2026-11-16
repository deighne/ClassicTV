"""Unit tests for tools/classictv_worker.py"""

import json
import os
import tempfile
import pytest

from tools import classictv_worker, classictv_queue


def test_reset_orphaned_jobs_recovers_reencoding_items():
    with tempfile.TemporaryDirectory() as tmpdir:
        media_file = os.path.join(tmpdir, "Show - S01E01.mkv")
        stale_temp = os.path.join(tmpdir, ".reencode_Show - S01E01.mkv")
        with open(media_file, "w") as f:
            f.write("media")
        with open(stale_temp, "w") as f:
            f.write("stale")

        queue_path = os.path.join(tmpdir, "queue.json")
        queue_data = {
            "total_items": 2,
            "pending": 1,
            "reencoding": 1,
            "completed": 0,
            "failed": 0,
            "items": {
                "item-1": {
                    "id": "item-1",
                    "series": "Test Show",
                    "season": 1,
                    "episode": 1,
                    "status": "reencoding",
                    "file_path": media_file,
                    "started_at": "2026-11-09T00:00:00Z",
                },
                "item-2": {
                    "id": "item-2",
                    "series": "Other Show",
                    "season": 1,
                    "episode": 2,
                    "status": "pending",
                    "file_path": "/fake/other.mkv",
                },
            },
        }
        with open(queue_path, "w") as f:
            json.dump(queue_data, f)

        count = classictv_worker.reset_orphaned_jobs(queue_path)
        assert count == 1
        assert not os.path.exists(stale_temp)

        saved = classictv_queue.load_queue(queue_path)
        assert saved["items"]["item-1"]["status"] == "pending"
        assert "started_at" not in saved["items"]["item-1"]
        assert saved["pending"] == 2
        assert saved["reencoding"] == 0
