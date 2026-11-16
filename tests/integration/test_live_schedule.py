"""
Integration tests for the live Schedule_Service running on jfmac.

Requires: --run-integration flag and reachable jfmac hardware.
All tests depend on the session-scoped `schedule_service_url` fixture
from conftest.py, which skips the test if the service is unreachable.

Requirements: 32.1–32.5
"""

import json
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Module-scoped fixture: fetch all channels once per module
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def channels(schedule_service_url):
    """Fetch all channels from the guide endpoint.

    Returns the list of channel dicts from the ``channels`` key.
    Skips the module if the guide response is malformed.

    Requirements: 32.3
    """
    t = int(time.time())
    with urllib.request.urlopen(
        f"{schedule_service_url}/guide?t={t}", timeout=5
    ) as resp:
        data = json.loads(resp.read())
    assert "channels" in data, "Guide response missing 'channels' key"
    return data["channels"]


@pytest.fixture(scope="module")
def schedule_responses(schedule_service_url, channels):
    """Fetch /schedule/{id}?t=now for every channel.

    Returns a list of (channel_id, response_dict) tuples.

    Requirements: 32.3
    """
    t = int(time.time())
    results = []
    for ch in channels:
        channel_id = ch["id"]
        with urllib.request.urlopen(
            f"{schedule_service_url}/schedule/{channel_id}?t={t}", timeout=5
        ) as resp:
            data = json.loads(resp.read())
        results.append((channel_id, data))
    return results


# ---------------------------------------------------------------------------
# Test 1: Health endpoint
# ---------------------------------------------------------------------------


def test_health_endpoint(schedule_service_url):
    """GET /health returns 200 with status:ok and a version field.

    Requirements: 32.1
    """
    with urllib.request.urlopen(
        f"{schedule_service_url}/health", timeout=5
    ) as resp:
        assert resp.status == 200
        data = json.loads(resp.read())

    assert data.get("status") == "ok", f"Expected status:ok, got: {data}"
    assert "version" in data, f"Health response missing 'version' field: {data}"


# ---------------------------------------------------------------------------
# Test 2: Schedule endpoint — all channels return valid JSON with expected fields
# ---------------------------------------------------------------------------


def test_schedule_endpoint_all_channels(schedule_responses):
    """For each channel, /schedule/{id}?t=now returns valid JSON with all
    required top-level and nested fields.

    Requirements: 32.3, 1.2
    """
    required_top_level = {"channel_id", "item", "next_item", "generated_at"}
    required_item_fields = {
        "jellyfin_id",
        "title",
        "type",
        "seek_offset",
        "duration",
        "direct_play_url",
    }

    for channel_id, data in schedule_responses:
        assert required_top_level <= data.keys(), (
            f"Channel {channel_id}: missing top-level fields "
            f"{required_top_level - data.keys()}"
        )
        item = data["item"]
        assert required_item_fields <= item.keys(), (
            f"Channel {channel_id}: item missing fields "
            f"{required_item_fields - item.keys()}"
        )
        assert isinstance(data["generated_at"], int), (
            f"Channel {channel_id}: generated_at should be int, got "
            f"{type(data['generated_at'])}"
        )


# ---------------------------------------------------------------------------
# Test 3: seek_offset is in bounds for every schedule response
# ---------------------------------------------------------------------------


def test_seek_offset_in_bounds(schedule_responses):
    """For each schedule response, 0 <= seek_offset < duration.

    Requirements: 32.3, 1.8
    """
    for channel_id, data in schedule_responses:
        item = data["item"]
        seek_offset = item["seek_offset"]
        duration = item["duration"]

        assert isinstance(seek_offset, (int, float)), (
            f"Channel {channel_id}: seek_offset is not a number: {seek_offset!r}"
        )
        assert isinstance(duration, (int, float)), (
            f"Channel {channel_id}: duration is not a number: {duration!r}"
        )
        assert seek_offset >= 0.0, (
            f"Channel {channel_id}: seek_offset {seek_offset} is negative"
        )
        assert seek_offset < duration, (
            f"Channel {channel_id}: seek_offset {seek_offset} >= duration {duration}"
        )


# ---------------------------------------------------------------------------
# Test 4: Guide endpoint — all channels with required fields
# ---------------------------------------------------------------------------


def test_guide_endpoint(schedule_service_url, channels):
    """GET /guide?t=now returns all channels with required display fields.

    Requirements: 32.3, 2.2, 2.3
    """
    required_channel_fields = {"id", "name", "number", "color", "now", "next"}
    required_now_fields = {"title", "type", "minutes_remaining"}

    assert len(channels) > 0, "Guide returned zero channels"

    for ch in channels:
        assert required_channel_fields <= ch.keys(), (
            f"Channel {ch.get('id', '?')}: missing guide fields "
            f"{required_channel_fields - ch.keys()}"
        )
        now = ch["now"]
        assert required_now_fields <= now.keys(), (
            f"Channel {ch['id']}: 'now' missing fields "
            f"{required_now_fields - now.keys()}"
        )
        assert isinstance(ch["number"], int), (
            f"Channel {ch['id']}: 'number' should be int, got {type(ch['number'])}"
        )
        assert isinstance(now["minutes_remaining"], int), (
            f"Channel {ch['id']}: minutes_remaining should be int, "
            f"got {type(now['minutes_remaining'])}"
        )


# ---------------------------------------------------------------------------
# Test 5: Guide response contains no sensitive data
# ---------------------------------------------------------------------------


def test_guide_no_sensitive_data(channels):
    """Guide response contains no jellyfin_ids, direct_play_urls, or seek_offsets.

    Requirements: 32.3, 2.6
    """
    forbidden_keys = {"jellyfin_id", "direct_play_url", "seek_offset"}

    def _check_dict(d: dict, path: str) -> None:
        for key in forbidden_keys:
            assert key not in d, (
                f"Sensitive key '{key}' found in guide response at {path}"
            )
        for k, v in d.items():
            if isinstance(v, dict):
                _check_dict(v, f"{path}.{k}")
            elif isinstance(v, list):
                for i, item in enumerate(v):
                    if isinstance(item, dict):
                        _check_dict(item, f"{path}.{k}[{i}]")

    for ch in channels:
        _check_dict(ch, f"channels[{ch.get('id', '?')}]")


# ---------------------------------------------------------------------------
# Test 6: Rachel anchor endpoint — valid idle or playing response
# ---------------------------------------------------------------------------


def test_rachel_anchor_endpoint(schedule_service_url):
    """GET /rachel-anchor returns a valid response in either idle or playing state.

    Requirements: 32.3, 3.2, 3.3
    """
    with urllib.request.urlopen(
        f"{schedule_service_url}/rachel-anchor", timeout=5
    ) as resp:
        assert resp.status == 200
        data = json.loads(resp.read())

    assert "state" in data, f"/rachel-anchor response missing 'state': {data}"
    assert data["state"] in ("idle", "playing"), (
        f"Unexpected state value: {data['state']!r}"
    )

    if data["state"] == "playing":
        playing_fields = {
            "jellyfin_id",
            "anchor_position",
            "anchor_timestamp",
            "total_duration",
        }
        assert playing_fields <= data.keys(), (
            f"Playing state missing fields: {playing_fields - data.keys()}"
        )
        assert isinstance(data["anchor_position"], (int, float)), (
            f"anchor_position is not a number: {data['anchor_position']!r}"
        )
        assert isinstance(data["anchor_timestamp"], (int, float)), (
            f"anchor_timestamp is not a number: {data['anchor_timestamp']!r}"
        )
        assert isinstance(data["total_duration"], (int, float)), (
            f"total_duration is not a number: {data['total_duration']!r}"
        )
        assert data["anchor_position"] >= 0.0, (
            f"anchor_position is negative: {data['anchor_position']}"
        )
        assert data["total_duration"] > 0.0, (
            f"total_duration is not positive: {data['total_duration']}"
        )


# ---------------------------------------------------------------------------
# Test 7: Direct play URLs are reachable on Jellyfin
# ---------------------------------------------------------------------------


def test_direct_play_urls_are_reachable(
    schedule_service_url, jellyfin_url, schedule_responses
):
    """For each channel, the direct_play_url from /schedule returns a non-404
    HTTP response from Jellyfin (HEAD request).

    Requirements: 32.4, 1.5
    """
    failures = []

    for channel_id, data in schedule_responses:
        direct_play_url = data["item"].get("direct_play_url", "")
        if not direct_play_url:
            failures.append(
                f"Channel {channel_id}: direct_play_url is empty"
            )
            continue

        try:
            req = urllib.request.Request(direct_play_url, method="HEAD")
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
        except urllib.error.HTTPError as exc:
            status = exc.code

        if status == 404:
            failures.append(
                f"Channel {channel_id}: direct_play_url returned 404 "
                f"({direct_play_url!r})"
            )

    assert not failures, (
        "The following channels have unreachable direct_play_urls:\n"
        + "\n".join(f"  - {f}" for f in failures)
    )
