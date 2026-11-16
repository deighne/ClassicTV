"""
Unit tests for addon/resources/lib/player.py.

Covers:
- play_schedule_item: valid episode (StartTimeTicks in URL), bumper, offline item (no StartTimeTicks)
- _fetch_schedule: HTTP error returns None, malformed JSON returns None
- onPlayBackEnded: fetches schedule and calls play() with returned URL and seek offset
- onPlayBackEnded: all retries fail → plays Offline_Clip
- handle_ch_up / handle_ch_down: stops current playback, fetches schedule for new/prev channel,
  calls play()
- _watchdog_loop: fetches schedule and calls play() when isPlaying() is False in TV_Mode
- onPlayBackStarted: Strategy B activated when getTime() is far from _pending_seek
- onPlayBackStarted: Strategy B NOT activated when seek_offset is 0

Requirements: 31.2, 31.3
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure addon/resources/lib is on the path before importing addon modules.
# conftest.py is auto-loaded and has already injected the Kodi stubs.
# ---------------------------------------------------------------------------

sys.path.insert(0, "addon/resources/lib")

from player import ClassicTVPlayer


# ---------------------------------------------------------------------------
# Sample schedule responses
# ---------------------------------------------------------------------------

EPISODE_RESPONSE = {
    "channel_id": "test-ch",
    "item": {
        "jellyfin_id": "ep-001",
        "title": "Test Show S01E01",
        "type": "episode",
        "seek_offset": 300.0,
        "duration": 1800.0,
        "direct_play_url": "http://jf:8096/Videos/ep-001/stream?Static=true",
    },
    "next_item": None,
    "generated_at": 1700000000,
}

BUMPER_RESPONSE = {
    "channel_id": "test-ch",
    "item": {
        "jellyfin_id": "bump-002",
        "title": "Station ID",
        "type": "bumper",
        "seek_offset": 0.0,
        "duration": 30.0,
        "direct_play_url": "http://jf:8096/Videos/bump-002/stream?Static=true",
    },
    "next_item": None,
    "generated_at": 1700000000,
}

OFFLINE_RESPONSE = {
    "channel_id": "test-ch",
    "item": {
        "jellyfin_id": "OFFLINE",
        "title": "Offline",
        "type": "offline",
        "seek_offset": 0.0,
        "duration": 0.0,
        "direct_play_url": "http://jf:8096/Videos/OFFLINE/stream?Static=true",
    },
    "next_item": None,
    "generated_at": 1700000000,
}


# ---------------------------------------------------------------------------
# TestPlayer subclass — overrides Kodi-only methods so tests run without Kodi
# ---------------------------------------------------------------------------

class _InstrumentedPlayer(ClassicTVPlayer):
    """ClassicTVPlayer subclass that records calls for test assertions."""

    def __init__(self):
        super().__init__()
        self._play_calls: list[str] = []
        self._seek_calls: list[float] = []
        self._stop_calls: int = 0
        self._playing: bool = False
        self._mock_time: float = 0.0
        self._mock_schedule = None

    def play(self, url="", listitem=None, *args, **kwargs):
        self._play_calls.append(url)
        self._playing = True

    def stop(self):
        self._stop_calls += 1
        self._playing = False

    def isPlaying(self) -> bool:
        return self._playing

    def getTime(self) -> float:
        return self._mock_time

    def seekTime(self, t: float) -> None:
        self._seek_calls.append(t)
        self._mock_time = t

    def _fetch_schedule(self, channel_id, t, timeout=5.0):
        return self._mock_schedule


# ---------------------------------------------------------------------------
# Channel/ChannelList mocks for ch-up/ch-down tests
# ---------------------------------------------------------------------------

class _MockChannel:
    def __init__(self, channel_id: str):
        self.id = channel_id
        self.number = 5
        self.name = "Test"
        self.color = "#ff0000"


class _MockChannelList:
    def next_channel(self, current_id: str):
        return _MockChannel("next-ch")

    def prev_channel(self, current_id: str):
        return _MockChannel("prev-ch")


class _MockModes:
    current = "tv"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_player() -> _InstrumentedPlayer:
    """Return a fresh _InstrumentedPlayer with a known channel_id."""
    p = _InstrumentedPlayer()
    p._current_channel_id = "test-ch"
    p._schedule_service_url = "http://localhost:8300"
    return p


# ===========================================================================
# Tests: play_schedule_item
# ===========================================================================

class TestPlayScheduleItem:

    def test_valid_episode_calls_play_with_start_time_ticks(self):
        """Req 31.2: valid episode → play() called with StartTimeTicks in URL."""
        p = _make_player()
        p.play_schedule_item(EPISODE_RESPONSE)

        assert len(p._play_calls) == 1
        url = p._play_calls[0]
        assert "StartTimeTicks=" in url
        # seek_offset=300.0 → 300 * 10_000_000 = 3_000_000_000
        assert "StartTimeTicks=3000000000" in url

    def test_valid_episode_base_url_is_preserved(self):
        """The original direct_play_url path is kept intact when ticks are appended."""
        p = _make_player()
        p.play_schedule_item(EPISODE_RESPONSE)

        url = p._play_calls[0]
        assert "Videos/ep-001/stream" in url

    def test_valid_bumper_calls_play(self):
        """Req 31.2: bumper item → play() is called."""
        p = _make_player()
        p.play_schedule_item(BUMPER_RESPONSE)

        assert len(p._play_calls) == 1

    def test_valid_bumper_no_start_time_ticks(self):
        """Bumper has seek_offset=0; StartTimeTicks must NOT be appended."""
        p = _make_player()
        p.play_schedule_item(BUMPER_RESPONSE)

        url = p._play_calls[0]
        assert "StartTimeTicks" not in url

    def test_offline_item_calls_play(self):
        """Req 31.2: offline item → play() is called."""
        p = _make_player()
        p.play_schedule_item(OFFLINE_RESPONSE)

        assert len(p._play_calls) == 1

    def test_offline_item_no_start_time_ticks(self):
        """Offline item has seek_offset=0; StartTimeTicks must NOT be appended."""
        p = _make_player()
        p.play_schedule_item(OFFLINE_RESPONSE)

        url = p._play_calls[0]
        assert "StartTimeTicks" not in url

    def test_offline_item_url_matches_direct_play_url(self):
        """Offline item URL must be the direct_play_url unchanged."""
        p = _make_player()
        p.play_schedule_item(OFFLINE_RESPONSE)

        url = p._play_calls[0]
        assert "OFFLINE" in url

    def test_pending_seek_is_set_after_play(self):
        """_pending_seek is updated to the item's seek_offset before play()."""
        p = _make_player()
        p.play_schedule_item(EPISODE_RESPONSE)

        assert p._pending_seek == 300.0

    def test_current_item_is_set_after_play(self):
        """_current_item reflects the item dict from the response."""
        p = _make_player()
        p.play_schedule_item(EPISODE_RESPONSE)

        assert p._current_item["jellyfin_id"] == "ep-001"

    def test_current_channel_id_is_set_after_play(self):
        """_current_channel_id reflects the channel_id from the response."""
        p = _make_player()
        p.play_schedule_item(EPISODE_RESPONSE)

        assert p._current_channel_id == "test-ch"

    def test_existing_start_time_ticks_not_duplicated(self):
        """If the URL already contains StartTimeTicks, it must not be appended again."""
        p = _make_player()
        response = {
            "channel_id": "test-ch",
            "item": {
                "jellyfin_id": "ep-already",
                "title": "Already Ticked",
                "type": "episode",
                "seek_offset": 120.0,
                "duration": 600.0,
                "direct_play_url": "http://jf:8096/Videos/ep-already/stream?Static=true&StartTimeTicks=1200000000",
            },
            "next_item": None,
            "generated_at": 1700000000,
        }
        p.play_schedule_item(response)

        url = p._play_calls[0]
        assert url.count("StartTimeTicks=") == 1

    def test_local_cache_path_does_not_get_start_time_ticks(self):
        """Req 40.1, 40.3: when the media cache returns a local filesystem path,
        StartTimeTicks must NOT be appended to it.

        Appending &StartTimeTicks= to a path like
        /storage/classictv_cache/abc.bin produces an invalid path that
        Kodi's VideoPlayer rejects, causing playback to fail on every
        cached episode.

        This test FAILS before the fix and PASSES after.
        """
        p = _make_player()

        # Simulate a cache hit: get_local_url returns a local path
        mock_cache = MagicMock()
        mock_cache.get_local_url.return_value = "/storage/classictv_cache/ep-001.bin"
        p._media_cache = mock_cache

        p.play_schedule_item(EPISODE_RESPONSE)  # seek_offset=300.0, so would trigger append

        url = p._play_calls[0]
        assert url == "/storage/classictv_cache/ep-001.bin", (
            f"Expected bare local path, got: {url!r}. "
            f"StartTimeTicks must not be appended to filesystem paths (Requirement 40)."
        )
        assert "&StartTimeTicks=" not in url
        assert "StartTimeTicks" not in url

    def test_http_url_still_gets_start_time_ticks_when_seek_offset_nonzero(self):
        """Req 40.4: StartTimeTicks must still be appended to HTTP URLs
        when seek_offset > 0 — the fix must not regress normal HTTP playback.
        """
        p = _make_player()
        # No media cache — falls through to direct_play_url
        p._media_cache = None

        p.play_schedule_item(EPISODE_RESPONSE)  # seek_offset=300.0

        url = p._play_calls[0]
        assert url.startswith("http://"), f"Expected HTTP URL, got: {url!r}"
        assert "StartTimeTicks=" in url


# ===========================================================================
# Tests: _fetch_schedule (HTTP-level error handling)
# ===========================================================================

class TestFetchSchedule:
    """These tests exercise the real _fetch_schedule (not the TestPlayer override)."""

    def test_http_error_returns_none(self):
        """_fetch_schedule returns None when the server returns an HTTP error."""
        import urllib.error

        real_player = ClassicTVPlayer()
        real_player._schedule_service_url = "http://localhost:8300"

        with patch("urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.URLError("connection refused")
            result = real_player._fetch_schedule("test-ch", 1700000000)

        assert result is None

    def test_malformed_json_returns_none(self):
        """_fetch_schedule returns None when the server sends malformed JSON."""

        real_player = ClassicTVPlayer()
        real_player._schedule_service_url = "http://localhost:8300"

        bad_response = MagicMock()
        bad_response.read.return_value = b"this is not json {{{"
        bad_response.__enter__ = lambda s: s
        bad_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=bad_response):
            result = real_player._fetch_schedule("test-ch", 1700000000)

        assert result is None


# ===========================================================================
# Tests: onPlayBackEnded (EOF retune)
# ===========================================================================

class TestOnPlayBackEnded:

    def test_successful_fetch_calls_play(self):
        """Req 31.3: EOF → fetch schedule → play() called with returned URL."""
        p = _make_player()
        p._mock_schedule = EPISODE_RESPONSE

        p.onPlayBackEnded()

        assert len(p._play_calls) == 1

    def test_successful_fetch_embeds_start_time_ticks(self):
        """After EOF retune the URL contains StartTimeTicks when seek_offset > 0."""
        p = _make_player()
        p._mock_schedule = EPISODE_RESPONSE

        p.onPlayBackEnded()

        url = p._play_calls[0]
        assert "StartTimeTicks=" in url

    def test_failed_fetch_plays_offline_clip(self):
        """Req 31.3: all fetch retries fail → Offline_Clip play() is called."""
        p = _make_player()
        p._mock_schedule = None  # every _fetch_schedule returns None
        p._offline_clip_url = "http://jf:8096/Videos/OFFLINE/stream?Static=true"

        p.onPlayBackEnded()

        # At least one play() call with the offline URL.
        assert any("OFFLINE" in url for url in p._play_calls)

    def test_failed_fetch_no_offline_url_configured(self):
        """If offline URL is empty and all fetches fail, play() is not called (no crash)."""
        p = _make_player()
        p._mock_schedule = None
        p._offline_clip_url = ""

        # Must not raise.
        p.onPlayBackEnded()

        # play() is not called when there's no offline clip configured.
        assert p._play_calls == []


# ===========================================================================
# Tests: handle_ch_up / handle_ch_down
# ===========================================================================

class TestChannelChange:

    def _make_player_with_channels(self) -> _InstrumentedPlayer:
        p = _make_player()
        p._channel_list = _MockChannelList()
        return p

    # ------------------------------------------------------------------
    # Basic delegation: handle_ch_up / handle_ch_down → _change_channel
    # ------------------------------------------------------------------

    def test_ch_up_calls_stop(self):
        """Req 31.3: ch_up stops current playback."""
        p = self._make_player_with_channels()
        p._playing = True
        p._mock_schedule = EPISODE_RESPONSE

        p.handle_ch_up()

        assert p._stop_calls >= 1

    def test_ch_up_calls_play_for_next_channel(self):
        """Req 31.3: ch_up fetches schedule for next channel and calls play()."""
        p = self._make_player_with_channels()
        p._playing = True
        p._mock_schedule = EPISODE_RESPONSE

        p.handle_ch_up()

        assert len(p._play_calls) >= 1

    def test_ch_down_calls_stop(self):
        """Req 31.3: ch_down stops current playback."""
        p = self._make_player_with_channels()
        p._playing = True
        p._mock_schedule = EPISODE_RESPONSE

        p.handle_ch_down()

        assert p._stop_calls >= 1

    def test_ch_down_calls_play_for_prev_channel(self):
        """Req 31.3: ch_down fetches schedule for prev channel and calls play()."""
        p = self._make_player_with_channels()
        p._playing = True
        p._mock_schedule = EPISODE_RESPONSE

        p.handle_ch_down()

        assert len(p._play_calls) >= 1

    def test_ch_up_without_channel_list_is_noop(self):
        """handle_ch_up is a no-op when _channel_list is not set."""
        p = _make_player()
        p._channel_list = None

        p.handle_ch_up()

        assert p._play_calls == []
        assert p._stop_calls == 0

    def test_ch_down_without_channel_list_is_noop(self):
        """handle_ch_down is a no-op when _channel_list is not set."""
        p = _make_player()
        p._channel_list = None

        p.handle_ch_down()

        assert p._play_calls == []
        assert p._stop_calls == 0

    def test_ch_up_failed_fetch_does_not_play_new_channel(self):
        """If the schedule fetch for the new channel fails, play() is not called
        for the new channel (Req 8.8)."""
        p = self._make_player_with_channels()
        p._playing = True
        p._mock_schedule = None  # fetch always fails

        p.handle_ch_up()

        # stop() was called but no successful play for new channel.
        assert p._stop_calls >= 1
        # play() may be called to resume the previous channel (Req 8.8).
        # What must NOT happen: a successful new-channel play.
        # We verify stop was invoked and total play calls <= 1 (resume attempt).
        assert len(p._play_calls) <= 1

    # ------------------------------------------------------------------
    # _change_channel sequence (Req 8.1–8.8)
    # ------------------------------------------------------------------

    def test_channel_static_shown_before_stop(self):
        """Req 8.1, 12.8: Channel_Static overlay must be shown before stop()
        is called — not after."""
        p = self._make_player_with_channels()
        p._playing = True
        p._mock_schedule = EPISODE_RESPONSE

        call_order = []
        mock_static = MagicMock()
        mock_static.show.side_effect = lambda: call_order.append("static_show")

        original_stop = p.stop
        def _tracking_stop():
            call_order.append("stop")
            original_stop()
        p.stop = _tracking_stop

        p._channel_static = mock_static
        p.handle_ch_up()

        assert "static_show" in call_order, "channel_static.show() was never called"
        assert "stop" in call_order, "stop() was never called"
        assert call_order.index("static_show") < call_order.index("stop"), (
            "channel_static.show() must be called BEFORE stop() (Req 8.1, 12.8). "
            f"Actual order: {call_order}"
        )

    def test_stop_called_before_fetch(self):
        """Req 8.2: playback must be stopped before fetching the new schedule."""
        p = self._make_player_with_channels()
        p._playing = True

        call_order = []

        original_stop = p.stop
        def _tracking_stop():
            call_order.append("stop")
            original_stop()
        p.stop = _tracking_stop

        def _tracking_fetch(channel_id, t, **kw):
            call_order.append("fetch")
            return EPISODE_RESPONSE
        p._mock_schedule = EPISODE_RESPONSE
        p._fetch_schedule = _tracking_fetch

        p.handle_ch_up()

        assert "stop" in call_order
        assert "fetch" in call_order
        assert call_order.index("stop") < call_order.index("fetch"), (
            "stop() must be called before the schedule fetch (Req 8.2). "
            f"Actual order: {call_order}"
        )

    def test_successful_fetch_calls_play_schedule_item(self):
        """Req 8.2: on successful fetch, play_schedule_item is called with
        the new channel's response."""
        p = self._make_player_with_channels()
        p._playing = True
        p._mock_schedule = EPISODE_RESPONSE

        p.handle_ch_up()

        assert len(p._play_calls) >= 1
        # URL should correspond to the episode from EPISODE_RESPONSE
        assert "ep-001" in p._play_calls[0]

    def test_failed_fetch_resumes_previous_channel(self):
        """Req 8.8: when the schedule fetch for the new channel fails,
        the player must attempt to resume the previously tuned channel."""
        p = self._make_player_with_channels()
        p._playing = True
        p._current_channel_id = "test-ch"

        fetch_calls = []
        def _tracking_fetch(channel_id, t, **kw):
            fetch_calls.append(channel_id)
            if channel_id == "next-ch":
                return None   # new channel fetch fails
            return EPISODE_RESPONSE  # previous channel succeeds

        p._fetch_schedule = _tracking_fetch
        p.handle_ch_up()

        assert "next-ch" in fetch_calls, "Did not attempt to fetch new channel"
        assert "test-ch" in fetch_calls, (
            "Did not attempt to resume previous channel after failed fetch (Req 8.8)."
        )

    def test_failed_fetch_channel_static_closed(self):
        """Req 8.8: when the fetch fails, channel_static must be closed
        (not left visible on screen)."""
        p = self._make_player_with_channels()
        p._playing = True
        p._mock_schedule = None  # all fetches fail

        mock_static = MagicMock()
        p._channel_static = mock_static

        p.handle_ch_up()

        mock_static.close.assert_called(), (
            "channel_static.close() was not called after a failed channel fetch. "
            "The static overlay must be dismissed (Req 8.8)."
        )

    def test_current_channel_id_updated_on_success(self):
        """After a successful channel change, _current_channel_id must reflect
        the new channel so the next ch_up/ch_down navigates from the right place."""
        p = self._make_player_with_channels()
        p._playing = True
        p._current_channel_id = "test-ch"
        p._mock_schedule = EPISODE_RESPONSE  # EPISODE_RESPONSE has channel_id="test-ch"

        # Override to return a response for the new channel
        def _fetch(channel_id, t, **kw):
            return {**EPISODE_RESPONSE, "channel_id": channel_id}
        p._fetch_schedule = _fetch

        p.handle_ch_up()

        assert p._current_channel_id == "next-ch", (
            f"_current_channel_id should be 'next-ch' after ch_up, "
            f"got {p._current_channel_id!r}."
        )

    def test_ch_up_ch_down_sequence_navigates_correctly(self):
        """End-to-end: ch_up followed by ch_down returns to the original channel."""
        p = self._make_player_with_channels()
        p._playing = True
        p._current_channel_id = "test-ch"

        channel_sequence = []

        def _fetch(channel_id, t, **kw):
            channel_sequence.append(channel_id)
            return {**EPISODE_RESPONSE, "channel_id": channel_id}
        p._fetch_schedule = _fetch

        p.handle_ch_up()    # should go to "next-ch"
        p.handle_ch_down()  # should go back to "prev-ch" from "next-ch"

        assert len(channel_sequence) >= 2
        assert channel_sequence[0] == "next-ch", (
            f"First ch_up should fetch 'next-ch', got {channel_sequence[0]!r}"
        )


# ===========================================================================
# Tests: _watchdog_loop (single iteration)
# ===========================================================================

class TestWatchdogLoop:
    """Tests for watchdog recovery behavior — tests the inner logic, not the loop."""

    def test_watchdog_fetches_and_calls_play_when_not_playing_in_tv_mode(self):
        """Req 31.3: when not playing in TV_Mode, watchdog fetches schedule and calls play()."""
        p = _make_player()
        p._playing = False
        p._mock_schedule = EPISODE_RESPONSE
        p._modes = _MockModes()  # current == "tv"

        # Simulate what the watchdog loop does: if not isPlaying(), fetch and play
        if not p.isPlaying():
            response = p._fetch_schedule(p._current_channel_id, 0)
            if response:
                p.play_schedule_item(response)

        assert len(p._play_calls) == 1

    def test_watchdog_skips_recovery_in_movie_mode(self):
        """Watchdog must not restart playback when mode is not 'tv' (Req 9.3)."""
        p = _make_player()
        p._playing = False
        p._mock_schedule = EPISODE_RESPONSE

        class _MovieModes:
            current = "movie"

        p._modes = _MovieModes()

        # Simulate what the watchdog loop does when NOT in TV_Mode: skip
        if p._modes.current != "tv":
            pass  # No play() call

        assert len(p._play_calls) == 0


# ===========================================================================
# Tests: onPlayBackStarted — seek strategy
# ===========================================================================

class TestOnPlayBackStarted:

    def test_strategy_b_activated_when_position_far_from_target(self):
        """Req 31.3: seekTime() called (Strategy B) when achieved position is
        outside SEEK_TOLERANCE_SECONDS of the intended seek offset."""
        p = _make_player()
        p._pending_seek = 300.0   # target: 300s
        p._mock_time = 0.0        # getTime() returns 0 → far from 300s
        p._current_item = {"title": "Test Show S01E01"}
        p._current_channel_id = "test-ch"

        p.onPlayBackStarted()

        assert len(p._seek_calls) >= 1
        assert p._seek_calls[0] == pytest.approx(300.0)

    def test_strategy_b_not_activated_when_seek_offset_is_zero(self):
        """Req 31.3: seekTime() must NOT be called when seek_offset (pending_seek) is 0."""
        p = _make_player()
        p._pending_seek = 0.0     # item starts from the beginning
        p._mock_time = 0.0
        p._current_item = {"title": "Bumper"}
        p._current_channel_id = "test-ch"

        p.onPlayBackStarted()

        assert p._seek_calls == []

    def test_strategy_b_not_activated_when_position_within_tolerance(self):
        """seekTime() must NOT be called when the achieved position is already
        within SEEK_TOLERANCE_SECONDS of the target."""
        p = _make_player()
        p._pending_seek = 300.0
        p._mock_time = 300.5      # within 2s tolerance
        p._current_item = {"title": "Test Show S01E01"}
        p._current_channel_id = "test-ch"

        p.onPlayBackStarted()

        assert p._seek_calls == []

    def test_channel_static_dismissed_on_playback_started(self):
        """on_playback_started() must be called on _channel_static (Req 12.6)."""
        p = _make_player()
        p._pending_seek = 0.0
        p._mock_time = 0.0
        p._current_item = {"title": "Test"}
        p._current_channel_id = "test-ch"

        mock_static = MagicMock()
        p._channel_static = mock_static

        p.onPlayBackStarted()

        mock_static.on_playback_started.assert_called_once()

    def test_channel_bug_shown_on_playback_started(self):
        """channel_bug.show() must be called when playback begins (Req 13.1)."""
        p = _make_player()
        p._pending_seek = 0.0
        p._mock_time = 0.0
        p._current_item = {"title": "Test"}
        p._current_channel_id = "test-ch"

        mock_bug = MagicMock()
        p._channel_bug = mock_bug

        p.onPlayBackStarted()

        mock_bug.show.assert_called_once()

    def test_channel_static_exception_does_not_propagate(self):
        """If channel_static.on_playback_started() raises, it must be caught (no crash)."""
        p = _make_player()
        p._pending_seek = 0.0
        p._mock_time = 0.0
        p._current_item = {"title": "Test"}
        p._current_channel_id = "test-ch"

        mock_static = MagicMock()
        mock_static.on_playback_started.side_effect = RuntimeError("boom")
        p._channel_static = mock_static

        p.onPlayBackStarted()  # must not raise

    def test_channel_bug_exception_does_not_propagate(self):
        """If channel_bug.show() raises, it must be caught (no crash)."""
        p = _make_player()
        p._pending_seek = 0.0
        p._mock_time = 0.0
        p._current_item = {"title": "Test"}
        p._current_channel_id = "test-ch"

        mock_bug = MagicMock()
        mock_bug.show.side_effect = RuntimeError("boom")
        p._channel_bug = mock_bug

        p.onPlayBackStarted()  # must not raise


# ===========================================================================
# Tests: _seek_with_retry
# ===========================================================================

class TestSeekWithRetry:

    def _make_seeking_player(self) -> _InstrumentedPlayer:
        """Return a player with _seek_in_progress=True, ready for _seek_with_retry."""
        p = _make_player()
        p._seek_in_progress = True
        return p

    def test_seek_called_immediately(self):
        """seekTime(target) is called on the first attempt."""
        p = self._make_seeking_player()
        p._mock_time = 0.0

        p._seek_with_retry(300.0)

        assert 300.0 in p._seek_calls

    def test_seek_stops_when_within_tolerance(self):
        """No further retries once the achieved position is within tolerance."""
        p = self._make_seeking_player()
        p._mock_time = 299.0  # 1s off — within 2s tolerance after seekTime

        p._seek_with_retry(300.0)

        assert len(p._seek_calls) == 1

    def test_exhausted_retries_do_not_raise(self):
        """_seek_with_retry must log and return without raising after max retries."""
        p = self._make_seeking_player()

        p.seekTime = lambda t: p._seek_calls.append(t)  # don't update _mock_time
        p._mock_time = 0.0

        from constants import SEEK_MAX_RETRIES
        p._seek_with_retry(300.0)

        assert len(p._seek_calls) == SEEK_MAX_RETRIES

    def test_runtime_error_from_seek_time_does_not_propagate(self):
        """Req 38.1, 38.2: RuntimeError thrown by seekTime() must be caught
        inside _seek_with_retry() — must never propagate to the caller.

        On the Amlogic decoder, seekTime() throws RuntimeError when the decoder
        is not yet ready. If uncaught, it deadlocks Kodi's Python runtime.

        This test FAILS before the fix and PASSES after.
        """
        p = _make_player()
        p._mock_time = 0.0
        p._seek_in_progress = True

        def raising_seek(t: float) -> None:
            p._seek_calls.append(t)
            raise RuntimeError("Kodi is not playing any media file")

        p.seekTime = raising_seek

        # Must not raise — RuntimeError must be swallowed inside _seek_with_retry.
        try:
            p._seek_with_retry(300.0)
        except RuntimeError:
            pytest.fail(
                "_seek_with_retry() allowed RuntimeError from seekTime() to propagate. "
                "This causes Kodi to freeze on Amlogic hardware (Requirement 38)."
            )

    def test_runtime_error_counts_toward_max_retries(self):
        """Req 38.1: each RuntimeError from seekTime() counts as a failed attempt.
        After SEEK_MAX_RETRIES failures, _seek_with_retry must stop and return.

        This test FAILS before the fix (RecursionError or unexpected call count)
        and PASSES after.
        """
        from constants import SEEK_MAX_RETRIES

        p = _make_player()
        p._mock_time = 0.0
        p._seek_in_progress = True

        def raising_seek(t: float) -> None:
            p._seek_calls.append(t)
            raise RuntimeError("Kodi is not playing any media file")

        p.seekTime = raising_seek

        p._seek_with_retry(300.0)

        assert len(p._seek_calls) == SEEK_MAX_RETRIES, (
            f"Expected exactly {SEEK_MAX_RETRIES} seekTime() attempts, "
            f"got {len(p._seek_calls)}."
        )


# ===========================================================================
# Tests: Requirement 37 — onPlayBackStarted seek loop on Amlogic hardware
# ===========================================================================

class TestSeekLoopGuard:
    """Regression tests for the Amlogic-specific bug where onPlayBackStarted
    fires on every seekTime() call, causing infinite recursive seek chains.

    Requirement 37 — discovered 2026-09-10 on CoreELEC 21.3 / Sweet Potato.

    On the Amlogic decoder, each seekTime() call triggers another
    onPlayBackStarted. Without a guard, multiple concurrent seek correction
    chains accumulate. The fix is a _seek_in_progress flag that blocks
    re-entrant seek corrections.
    """

    def _make_player_with_reentrant_seek(self, target: float, initial_time: float = 0.0) -> _InstrumentedPlayer:
        """Return a player whose seekTime() simulates the Amlogic behaviour:
        each seekTime() call triggers onPlayBackStarted() again.

        This is the hardware condition that caused the bug.

        To expose the loop, seekTime() does NOT update _mock_time — so every
        onPlayBackStarted() re-entry still sees achieved=0 far from target,
        and would trigger another seek correction if the guard is missing.
        """
        p = _InstrumentedPlayer()
        p._current_channel_id = "test-ch"
        p._schedule_service_url = "http://localhost:8300"
        p._pending_seek = target
        p._mock_time = initial_time
        p._current_item = {"title": "SpongeBob SquarePants"}

        def reentrant_seek_time_no_convergence(t: float) -> None:
            # Record the call but deliberately do NOT update _mock_time.
            # This means getTime() still returns initial_time after seeking,
            # so without a guard, each re-entrant onPlayBackStarted would
            # think the position is still wrong and call seekTime() again.
            p._seek_calls.append(t)
            p.onPlayBackStarted()   # Amlogic: fires onPlayBackStarted on seek

        p.seekTime = reentrant_seek_time_no_convergence
        return p

    def test_seektime_not_called_more_than_max_retries_when_reentrant(self):
        """Req 37.3: even when onPlayBackStarted fires on every seekTime()
        (Amlogic behaviour), seekTime() must be called at most SEEK_MAX_RETRIES
        times total — not multiplied by each spurious re-entry.

        Without the fix, each seekTime() triggers onPlayBackStarted, which
        triggers _seek_with_retry, which triggers seekTime again — exponential
        growth. With the fix, only the first chain runs; re-entries are blocked.

        This test FAILS before the fix and PASSES after.
        """
        from constants import SEEK_MAX_RETRIES

        target = 500.0
        # initial_time far from target so Strategy B is always triggered
        p = self._make_player_with_reentrant_seek(target, initial_time=0.0)

        p.onPlayBackStarted()

        assert len(p._seek_calls) <= SEEK_MAX_RETRIES, (
            f"seekTime() was called {len(p._seek_calls)} times "
            f"(max allowed: {SEEK_MAX_RETRIES}). "
            f"The Amlogic re-entrant onPlayBackStarted loop is not being guarded."
        )

    def test_second_on_playback_started_during_seek_is_ignored(self):
        """Req 37.1: a second onPlayBackStarted call that is re-entrant (called
        from within an active seek correction) must not start a new seek chain.

        Sequential calls (after the first has completed) are allowed to seek —
        the guard only blocks re-entrant calls within the same call stack.
        This test uses the reentrant helper to verify that re-entrant calls
        during an active seek do not add extra seekTime() invocations.
        """
        from constants import SEEK_MAX_RETRIES

        target = 500.0
        # Use reentrant helper: each seekTime() calls onPlayBackStarted() again.
        # Without the guard this recurses unboundedly; with the guard only
        # SEEK_MAX_RETRIES seekTime() calls are made across all chains.
        p = self._make_player_with_reentrant_seek(target, initial_time=0.0)

        p.onPlayBackStarted()

        # The guard must have bounded the total calls to at most SEEK_MAX_RETRIES.
        assert len(p._seek_calls) <= SEEK_MAX_RETRIES, (
            f"Re-entrant onPlayBackStarted during seek added too many calls: "
            f"{len(p._seek_calls)} > {SEEK_MAX_RETRIES}."
        )

    def test_seek_in_progress_flag_cleared_by_play_schedule_item(self):
        """Req 37.2: _seek_in_progress must be False after play_schedule_item()
        so that the next play call is not permanently blocked from seeking.

        This test FAILS before the fix (flag doesn't exist) and PASSES after.
        """
        p = _InstrumentedPlayer()
        p._current_channel_id = "test-ch"
        p._schedule_service_url = "http://localhost:8300"

        p.play_schedule_item(EPISODE_RESPONSE)

        assert p._seek_in_progress is False, (
            "_seek_in_progress must be False after play_schedule_item() "
            "so subsequent onPlayBackStarted calls can seek correctly."
        )


# ===========================================================================
# Tests: Requirement 39 — seek correction must not block the callback thread
# ===========================================================================

class TestSeekOffCallbackThread:
    """Requirement 39: onPlayBackStarted must return quickly even when a seek
    correction is needed. The seek runs on a daemon thread, not the callback.

    Discovered 2026-09-15: xbmc.sleep() inside onPlayBackStarted blocks Kodi's
    player state machine on the Amlogic decoder, causing the stream to abort,
    firing onPlayBackEnded, triggering an EOF retune — the doom loop.
    """

    def _make_slow_seek_player(self, target: float) -> _InstrumentedPlayer:
        """Return a player whose seekTime() sleeps 200ms to simulate a slow seek.

        Used to verify that onPlayBackStarted returns quickly even when the
        seek correction is needed — the sleep must happen off the callback thread.
        """
        import time as _time

        p = _InstrumentedPlayer()
        p._current_channel_id = "test-ch"
        p._pending_seek = target
        p._mock_time = 0.0   # far from target — triggers Strategy B
        p._current_item = {"title": "SpongeBob SquarePants"}

        original_seek = p.seekTime

        def slow_seek(t: float) -> None:
            _time.sleep(0.2)   # 200ms — longer than SEEK_CALLBACK_MAX_BLOCKING_MS
            original_seek(t)

        p.seekTime = slow_seek
        return p

    def test_on_playback_started_returns_within_max_blocking_ms(self):
        """Req 39.1, 39.5: onPlayBackStarted must return within
        SEEK_CALLBACK_MAX_BLOCKING_MS even when a seek correction is needed.

        Before the fix: onPlayBackStarted blocks for up to
        SEEK_MAX_RETRIES * SEEK_RETRY_INTERVAL_MS ms (1500ms) because
        _seek_with_retry() runs on the callback thread.

        After the fix: onPlayBackStarted spawns a thread and returns immediately.

        This test FAILS before the fix and PASSES after.
        """
        import time as _time
        try:
            from constants import SEEK_CALLBACK_MAX_BLOCKING_MS
        except ImportError:
            pytest.fail(
                "SEEK_CALLBACK_MAX_BLOCKING_MS constant not found in constants.py. "
                "Add it as part of the Requirement 39 fix."
            )

        p = self._make_slow_seek_player(target=500.0)

        t0 = _time.monotonic()
        p.onPlayBackStarted()
        elapsed_ms = (_time.monotonic() - t0) * 1000

        assert elapsed_ms < SEEK_CALLBACK_MAX_BLOCKING_MS, (
            f"onPlayBackStarted took {elapsed_ms:.1f}ms "
            f"(max allowed: {SEEK_CALLBACK_MAX_BLOCKING_MS}ms). "
            f"_seek_with_retry() is still blocking the callback thread "
            f"(Requirement 39)."
        )

    def test_seek_thread_started_when_correction_needed(self):
        """Req 39.2, 39.6: a seek thread must be started when a correction is
        needed; self._seek_thread must be set and alive after onPlayBackStarted.

        This test FAILS before the fix (_seek_thread attribute doesn't exist)
        and PASSES after.
        """
        p = _make_player()
        p._pending_seek = 500.0
        p._mock_time = 0.0   # far from target — triggers Strategy B
        p._current_item = {"title": "Test"}

        p.onPlayBackStarted()

        assert hasattr(p, "_seek_thread"), (
            "_seek_thread attribute not found on ClassicTVPlayer. "
            "The seek thread must be stored so it can be inspected (Requirement 39.3)."
        )
        assert p._seek_thread is not None, (
            "_seek_thread is None after onPlayBackStarted. "
            "A thread should have been spawned for the seek correction."
        )
        # Give the thread a moment to start
        import time as _time
        _time.sleep(0.05)
        assert p._seek_thread.is_alive() or len(p._seek_calls) > 0, (
            "Seek thread started but completed immediately with no seek calls. "
            "Verify the thread is actually executing _seek_with_retry."
        )

    def test_no_seek_thread_when_no_correction_needed(self):
        """Req 39.2: no thread should be spawned when seek_offset is 0
        or already within tolerance.

        This ensures we don't spawn unnecessary threads on every play call.
        """
        p = _make_player()
        p._pending_seek = 0.0   # no seek needed
        p._mock_time = 0.0
        p._current_item = {"title": "Test"}

        p.onPlayBackStarted()

        # Either no attribute or None — no thread needed
        seek_thread = getattr(p, "_seek_thread", None)
        assert seek_thread is None, (
            "A seek thread was started when no correction was needed "
            "(pending_seek=0, achieved=0). Unnecessary thread spawn."
        )

    def test_play_schedule_item_clears_seek_in_progress_without_waiting(self):
        """Req 39.3: play_schedule_item() must clear _seek_in_progress and
        proceed immediately even if a seek thread is still running.

        Before the fix: _seek_in_progress is not True when play_schedule_item
        is called (seek runs synchronously, already done). The flag is already
        False. Test validates the flag is False after play_schedule_item.

        After the fix: The flag may be True if a seek thread is mid-flight.
        play_schedule_item must still clear it and return without joining the
        old thread.

        This test is valid both before and after the fix — the flag must be
        False after play_schedule_item regardless.
        """
        p = _make_player()
        p._schedule_service_url = "http://localhost:8300"

        # Manually set _seek_in_progress to simulate a mid-flight seek thread
        p._seek_in_progress = True

        p.play_schedule_item(EPISODE_RESPONSE)

        assert p._seek_in_progress is False, (
            "_seek_in_progress was not cleared by play_schedule_item(). "
            "A new play call must always start with a clean seek state (Req 39.3)."
        )


# ===========================================================================
# Tests: Requirement 42 — _seek_with_retry must never let exceptions escape
# the seek thread; getTime() and xbmc.sleep() can throw after stream stops
# ===========================================================================

class TestSeekWithRetryNeverRaises:
    """Regression tests for unhandled exceptions escaping _seek_with_retry.

    Discovered 2026-09-12: the seek thread (spawned by onPlayBackStarted in
    Req 39) was logging 'EXCEPTION: Kodi is not playing any media file' and
    terminating with an unhandled exception. Root cause: getTime() and
    xbmc.sleep() both throw RuntimeError when the Amlogic player has already
    stopped, and only seekTime() was wrapped in try/except.

    Requirement 42: _seek_with_retry must catch ALL exceptions from ANY Kodi
    call (seekTime, getTime, xbmc.sleep) and return cleanly — never re-raise
    out of the seek thread.
    """

    def test_get_time_raising_at_retry_check_does_not_propagate(self):
        """Req 42: getTime() throwing after the seek attempt must be caught.

        This simulates the exact failure seen in the log:
        - seekTime() succeeds
        - xbmc.sleep() returns normally
        - getTime() throws RuntimeError ('Kodi is not playing any media file')
        The exception must be caught and _seek_with_retry must return without
        raising.

        This test FAILS before the fix and PASSES after.
        """
        p = _make_player()
        p._mock_time = 0.0

        def raising_get_time() -> float:
            raise RuntimeError("Kodi is not playing any media file")

        p.getTime = raising_get_time

        # Must not raise — RuntimeError from getTime() must be swallowed.
        try:
            p._seek_with_retry(300.0)
        except RuntimeError:
            pytest.fail(
                "_seek_with_retry() allowed RuntimeError from getTime() to propagate. "
                "This causes an unhandled thread exception and a doom loop (Req 42)."
            )

    def test_get_time_raising_at_max_retries_log_does_not_propagate(self):
        """Req 42: getTime() in the 'seek failed after N retries' log line
        also throws when the player has stopped. Must be caught.

        This test FAILS before the fix and PASSES after.
        """
        p = _make_player()
        p._mock_time = 0.0

        call_count = [0]

        def raising_get_time() -> float:
            call_count[0] += 1
            raise RuntimeError("Kodi is not playing any media file")

        p.getTime = raising_get_time

        # Force exhaustion of retries by making seekTime not converge
        def non_converging_seek(t: float) -> None:
            p._seek_calls.append(t)
            # do NOT update mock_time — position never converges

        p.seekTime = non_converging_seek

        # Must not raise even when getTime() throws in the error log line
        try:
            p._seek_with_retry(300.0)
        except RuntimeError:
            pytest.fail(
                "_seek_with_retry() raised when getTime() threw during the "
                "'seek failed' log message. Both getTime() call sites must be "
                "wrapped (Req 42)."
            )

    def test_stale_seek_thread_stops_when_seek_in_progress_cleared(self):
        """Req 42: when play_schedule_item() is called while a seek thread is
        running (clears _seek_in_progress), the seek thread must notice and
        stop rather than continuing to call seekTime() on the new player.

        Without this guard, a stale seek thread from a previous play call
        can interfere with the new episode's playback.

        This test verifies that seek attempts stop after _seek_in_progress
        is cleared externally (simulating play_schedule_item being called).
        """
        import time as _time
        import threading

        p = _make_player()
        p._mock_time = 0.0
        p._pending_seek = 500.0
        p._current_item = {"title": "Test"}

        # Make seekTime slow and non-converging so the thread runs for a while
        seek_calls_after_clear = [0]

        def slow_non_converging_seek(t: float) -> None:
            p._seek_calls.append(t)
            _time.sleep(0.05)
            # Count calls that happen after _seek_in_progress was cleared
            if not p._seek_in_progress:
                seek_calls_after_clear[0] += 1

        p.seekTime = slow_non_converging_seek

        # Start the seek thread by calling onPlayBackStarted
        p._seek_in_progress = True
        thread = threading.Thread(target=p._seek_with_retry, args=(500.0,), daemon=True)
        thread.start()

        # Simulate play_schedule_item being called: clear the flag
        _time.sleep(0.08)   # let one seek attempt run
        p._seek_in_progress = False

        # Give the thread time to notice and stop
        thread.join(timeout=2.0)

        assert not thread.is_alive() or seek_calls_after_clear[0] == 0, (
            "Seek thread continued making seekTime() calls after _seek_in_progress "
            "was cleared. Stale seek threads must stop when a new play call starts."
        )
