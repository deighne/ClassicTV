"""
Unit tests for server/jellyfin.py.

Tests Requirements: 1.5, 1.6, 10.2, 10.3, 10.4

Coverage:
- validate_direct_play: valid/invalid containers, codecs, audio; network errors
- resolve_dead_id: success path, cache hit, not-found, network errors, cache clear
- get_stream_url: URL construction with and without seek_ticks; env override
"""

from __future__ import annotations

import urllib.error

import pytest

import server.jellyfin as jf


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_cache():
    """Wipe the dead-ID cache before and after every test."""
    jf.clear_dead_id_cache()
    yield
    jf.clear_dead_id_cache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_source(
    container: str,
    video_codec: str,
    audio_codec: str,
) -> dict:
    """Return a minimal MediaSource dict."""
    return {
        "Container": container,
        "MediaStreams": [
            {"Type": "Video", "Codec": video_codec},
            {"Type": "Audio", "Codec": audio_codec},
        ],
    }


def _valid_response(container: str = "mkv", video: str = "h264", audio: str = "aac") -> dict:
    # Jellyfin returns Items list for /Items?Ids= queries
    return {"Items": [{"Id": "abc", "MediaSources": [_make_source(container, video, audio)]}], "TotalRecordCount": 1}


# ---------------------------------------------------------------------------
# validate_direct_play
# ---------------------------------------------------------------------------


class TestValidateDirectPlay:
    """Tests 1-11: validate_direct_play."""

    def test_valid_h264_mkv_aac(self, monkeypatch):
        """Test 1: h264 / mkv / aac → True."""
        monkeypatch.setattr(jf, "_get_json", lambda url: _valid_response("mkv", "h264", "aac"))
        assert jf.validate_direct_play("abc") is True

    def test_transcoded_unsupported_video_codec(self, monkeypatch):
        """Test 2: unsupported video codec (mpeg2video) → False."""
        monkeypatch.setattr(
            jf, "_get_json", lambda url: _valid_response("mkv", "mpeg2video", "aac")
        )
        assert jf.validate_direct_play("abc") is False

    def test_unsupported_container(self, monkeypatch):
        """Test 3: container avi not in passthrough set → False."""
        monkeypatch.setattr(
            jf, "_get_json", lambda url: _valid_response("avi", "h264", "aac")
        )
        assert jf.validate_direct_play("abc") is False

    def test_no_media_sources(self, monkeypatch):
        """Test 4: MediaSources list is empty → False."""
        monkeypatch.setattr(jf, "_get_json", lambda url: {"Items": [{"Id": "abc", "MediaSources": []}], "TotalRecordCount": 1})
        assert jf.validate_direct_play("abc") is False

    def test_item_not_found(self, monkeypatch):
        """Test 4b: Items list is empty (ID not found in Jellyfin) → False."""
        monkeypatch.setattr(jf, "_get_json", lambda url: {"Items": [], "TotalRecordCount": 0})
        assert jf.validate_direct_play("abc") is False

    def test_empty_media_streams(self, monkeypatch):
        """Test 5: MediaStreams list is empty → False."""
        monkeypatch.setattr(
            jf,
            "_get_json",
            lambda url: {"Items": [{"Id": "abc", "MediaSources": [{"Container": "mkv", "MediaStreams": []}]}], "TotalRecordCount": 1},
        )
        assert jf.validate_direct_play("abc") is False

    def test_jellyfin_unreachable(self, monkeypatch):
        """Test 6: URLError → False."""
        def _raise(url):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(jf, "_get_json", _raise)
        assert jf.validate_direct_play("abc") is False

    def test_http_error_404(self, monkeypatch):
        """Test 7: HTTPError 404 → False."""
        def _raise(url):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        monkeypatch.setattr(jf, "_get_json", _raise)
        assert jf.validate_direct_play("abc") is False

    def test_invalid_json(self, monkeypatch):
        """Test 8: JSON decode error → False."""
        import json

        def _raise(url):
            raise json.JSONDecodeError("Expecting value", "", 0)

        monkeypatch.setattr(jf, "_get_json", _raise)
        assert jf.validate_direct_play("abc") is False

    def test_hevc_mp4_aac(self, monkeypatch):
        """Test 9: hevc video + aac audio + mp4 → True."""
        monkeypatch.setattr(
            jf, "_get_json", lambda url: _valid_response("mp4", "hevc", "aac")
        )
        assert jf.validate_direct_play("abc") is True

    def test_h264_mkv_ac3(self, monkeypatch):
        """Test 10: h264 video + ac3 audio + mkv → True."""
        monkeypatch.setattr(
            jf, "_get_json", lambda url: _valid_response("mkv", "h264", "ac3")
        )
        assert jf.validate_direct_play("abc") is True

    def test_h264_unsupported_audio(self, monkeypatch):
        """Test 11: h264 video + unsupported audio (dts) → False."""
        monkeypatch.setattr(
            jf, "_get_json", lambda url: _valid_response("mkv", "h264", "dts")
        )
        assert jf.validate_direct_play("abc") is False


# ---------------------------------------------------------------------------
# resolve_dead_id
# ---------------------------------------------------------------------------


class TestResolveDeadId:
    """Tests 12-16: resolve_dead_id."""

    def test_success_returns_new_id_and_caches(self, monkeypatch):
        """Test 12: single item returned → new ID returned and cached."""
        call_count = 0

        def _fake_get(url):
            nonlocal call_count
            call_count += 1
            return {"Items": [{"Id": "new-uuid-123", "Name": "Episode Title"}]}

        monkeypatch.setattr(jf, "_get_json", _fake_get)

        result = jf.resolve_dead_id("old-id", "series-id", 1, 1)
        assert result == "new-uuid-123"
        assert call_count == 1

        # Verify the value is now in the cache (second call must not hit network)
        result2 = jf.resolve_dead_id("old-id", "series-id", 1, 1)
        assert result2 == "new-uuid-123"
        assert call_count == 1  # no additional HTTP call

    def test_cache_hit_skips_http(self, monkeypatch):
        """Test 13: cached entry is returned without a second HTTP call."""
        call_count = 0

        def _fake_get(url):
            nonlocal call_count
            call_count += 1
            return {"Items": [{"Id": "cached-uuid", "Name": "Ep"}]}

        monkeypatch.setattr(jf, "_get_json", _fake_get)

        jf.resolve_dead_id("stale-id", "series-id", 2, 3)
        assert call_count == 1

        jf.resolve_dead_id("stale-id", "series-id", 2, 3)
        assert call_count == 1  # still 1

    def test_not_found_empty_items(self, monkeypatch):
        """Test 14: empty Items list → None."""
        monkeypatch.setattr(jf, "_get_json", lambda url: {"Items": []})
        result = jf.resolve_dead_id("old-id", "series-id", 1, 1)
        assert result is None

    def test_jellyfin_unreachable(self, monkeypatch):
        """Test 15: URLError → None."""
        def _raise(url):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(jf, "_get_json", _raise)
        result = jf.resolve_dead_id("old-id", "series-id", 1, 1)
        assert result is None

    def test_clear_cache_invalidates_entries(self, monkeypatch):
        """Test 16: clear_dead_id_cache() causes next call to hit the network again."""
        call_count = 0

        def _fake_get(url):
            nonlocal call_count
            call_count += 1
            return {"Items": [{"Id": f"uuid-{call_count}"}]}

        monkeypatch.setattr(jf, "_get_json", _fake_get)

        first = jf.resolve_dead_id("stale-id", "series-id", 1, 1)
        assert first == "uuid-1"
        assert call_count == 1

        jf.clear_dead_id_cache()

        second = jf.resolve_dead_id("stale-id", "series-id", 1, 1)
        assert second == "uuid-2"
        assert call_count == 2  # cache was cleared, so network was called again


# ---------------------------------------------------------------------------
# get_stream_url
# ---------------------------------------------------------------------------


class TestGetStreamUrl:
    """Tests 17-19: get_stream_url."""

    def test_without_seek_ticks(self):
        """Test 17: no seek_ticks → URL ends with Static=true, no StartTimeTicks."""
        url = jf.get_stream_url("abc123")
        assert "Static=true" in url
        assert "StartTimeTicks" not in url

    def test_with_seek_ticks(self):
        """Test 18: seek_ticks provided → StartTimeTicks appended."""
        url = jf.get_stream_url("abc123", seek_ticks=7_423_000_000)
        assert "Static=true" in url
        assert "StartTimeTicks=7423000000" in url

    def test_base_url_from_environment(self, monkeypatch):
        """Test 19: JELLYFIN_BASE_URL env var controls base URL."""
        monkeypatch.setenv("JELLYFIN_BASE_URL", "http://jellyfin.local:8096")
        url = jf.get_stream_url("xyz789")
        assert url.startswith("http://jellyfin.local:8096/Videos/xyz789/stream")


# ---------------------------------------------------------------------------
# get_episode_durations  (Requirement 41)
# ---------------------------------------------------------------------------


class TestGetEpisodeDurations:
    """Tests for get_episode_durations() — Requirement 41.

    This function does not yet exist. These tests FAIL before the fix
    and PASS after.
    """

    def test_returns_duration_in_seconds_from_run_time_ticks(self, monkeypatch):
        """Req 41.1, 41.6: RunTimeTicks is divided by 10_000_000 to get seconds."""
        def _fake_get(url):
            return {
                "Items": [
                    {"Id": "ep-001", "RunTimeTicks": 6637290000},  # 663.729s
                    {"Id": "ep-002", "RunTimeTicks": 13500000000},  # 1350.0s
                ],
                "TotalRecordCount": 2,
            }

        monkeypatch.setattr(jf, "_get_json", _fake_get)
        result = jf.get_episode_durations(["ep-001", "ep-002"])

        assert result == pytest.approx({"ep-001": 663.729, "ep-002": 1350.0})

    def test_omits_items_with_zero_run_time_ticks(self, monkeypatch):
        """Req 41.1: items with zero or missing RunTimeTicks are omitted."""
        def _fake_get(url):
            return {
                "Items": [
                    {"Id": "ep-001", "RunTimeTicks": 6637290000},
                    {"Id": "ep-002", "RunTimeTicks": 0},
                    {"Id": "ep-003"},  # missing RunTimeTicks
                ],
                "TotalRecordCount": 3,
            }

        monkeypatch.setattr(jf, "_get_json", _fake_get)
        result = jf.get_episode_durations(["ep-001", "ep-002", "ep-003"])

        assert "ep-001" in result
        assert "ep-002" not in result
        assert "ep-003" not in result

    def test_returns_empty_dict_when_jellyfin_unreachable(self, monkeypatch):
        """Req 41.3: returns empty dict on any error — scheduler falls back gracefully."""
        def _raise(url):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(jf, "_get_json", _raise)
        result = jf.get_episode_durations(["ep-001"])

        assert result == {}

    def test_caches_results_avoids_second_request(self, monkeypatch):
        """Req 41.4: subsequent calls for the same ID use the cache, not the network."""
        call_count = 0

        def _fake_get(url):
            nonlocal call_count
            call_count += 1
            return {
                "Items": [{"Id": "ep-001", "RunTimeTicks": 6637290000}],
                "TotalRecordCount": 1,
            }

        monkeypatch.setattr(jf, "_get_json", _fake_get)

        first = jf.get_episode_durations(["ep-001"])
        second = jf.get_episode_durations(["ep-001"])

        assert first == second
        assert call_count == 1  # second call hit the cache

    def test_cache_cleared_by_clear_dead_id_cache(self, monkeypatch):
        """Req 41.4: clear_dead_id_cache() also clears the duration cache
        so hot-reloaded channels get fresh durations."""
        call_count = 0

        def _fake_get(url):
            nonlocal call_count
            call_count += 1
            return {
                "Items": [{"Id": "ep-001", "RunTimeTicks": 6637290000}],
                "TotalRecordCount": 1,
            }

        monkeypatch.setattr(jf, "_get_json", _fake_get)

        jf.get_episode_durations(["ep-001"])
        assert call_count == 1

        jf.clear_dead_id_cache()  # hot-reload invalidates duration cache too

        jf.get_episode_durations(["ep-001"])
        assert call_count == 2  # cache was cleared, network was called again

    def test_empty_id_list_returns_empty_dict(self, monkeypatch):
        """Calling with an empty list must return immediately without a network call."""
        call_count = 0

        def _fake_get(url):
            nonlocal call_count
            call_count += 1
            return {"Items": [], "TotalRecordCount": 0}

        monkeypatch.setattr(jf, "_get_json", _fake_get)
        result = jf.get_episode_durations([])

        assert result == {}
        assert call_count == 0  # no network call for empty input
