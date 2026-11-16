"""Unit tests for automatic incident recording in tools/classictv-status.py."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

STATUS_PATH = Path(__file__).resolve().parent.parent / "tools" / "classictv-status.py"
SPEC = importlib.util.spec_from_file_location("classictv_status", STATUS_PATH)
classictv_status = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(classictv_status)


def test_record_incident_prepends_and_deduplicates(tmp_path, monkeypatch):
    inc_path = tmp_path / "incidents.json"
    monkeypatch.setattr(classictv_status, "INCIDENTS_JSON_PATH", inc_path)

    prev_sp = {
        "channel_number": 22,
        "channel_name": "Movies",
        "playing_item": {
            "title": "The Lego Movie",
            "showtitle": "",
            "season": -1,
            "episode": -1,
            "file": "/media/movies/lego.mkv",
        },
    }

    incidents = classictv_status.record_incident(
        [],
        incident_type="UNEXPECTED_REBOOT",
        severity="CRITICAL",
        summary="Sweet Potato rebooted unexpectedly",
        timestamp="2026-11-14T20:00:00+00:00",
        prev_sp=prev_sp,
        evidence=["Uptime reset from 2h 0m to 1m"],
        resolved=True,
    )
    assert len(incidents) == 1
    assert incidents[0]["channel"] == {"number": 22, "name": "Movies"}
    assert incidents[0]["item"]["file_path"] == "/media/movies/lego.mkv"
    assert json.loads(inc_path.read_text())[0]["type"] == "UNEXPECTED_REBOOT"

    # Duplicate within 15 minutes is ignored
    incidents2 = classictv_status.record_incident(
        incidents,
        incident_type="UNEXPECTED_REBOOT",
        severity="CRITICAL",
        summary="Duplicate",
        timestamp="2026-11-14T20:05:00+00:00",
        prev_sp=prev_sp,
        evidence=[],
        resolved=True,
    )
    assert len(incidents2) == 1


def test_record_incident_enriches_ffprobe_and_kodi_log(tmp_path, monkeypatch):
    inc_path = tmp_path / "incidents.json"
    monkeypatch.setattr(classictv_status, "INCIDENTS_JSON_PATH", inc_path)
    monkeypatch.setattr(classictv_status.os.path, "exists", lambda p: p == "/media/hevc10.mkv")

    def fake_check_output(cmd, **kwargs):
        if cmd[0] == "ffprobe":
            return json.dumps({
                "streams": [{
                    "codec_name": "hevc",
                    "profile": "Main 10",
                    "width": 1920,
                    "height": 1080,
                    "pix_fmt": "yuv420p10le",
                    "level": 150,
                }]
            })
        if cmd[0] == "ssh":
            return "ERROR: OutputPicture - timeout waiting for buffer\n[ClassicTV] onAVStarted"
        raise AssertionError(f"Unexpected cmd: {cmd}")

    monkeypatch.setattr(classictv_status.subprocess, "check_output", fake_check_output)

    incidents = classictv_status.record_incident(
        [],
        incident_type="UNEXPECTED_REBOOT",
        severity="CRITICAL",
        summary="Crash",
        timestamp="2026-11-15T10:00:00+00:00",
        prev_sp={"channel_number": 2, "channel_name": "Anime", "playing_item": {"title": "Ep1", "file": "/media/hevc10.mkv"}},
        evidence=["Uptime reset"],
        resolved=True,
        sweet_host="192.168.1.237",
    )
    fa = incidents[0]["file_analysis"]
    assert fa["codec"] == "hevc"
    assert fa["dimensions"] == "1920x1080"
    assert len(fa["warnings"]) == 2
    assert any("OutputPicture" in line for line in incidents[0]["evidence"])
