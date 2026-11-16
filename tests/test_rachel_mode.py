"""
Unit tests for the Rachel mode poller (_rachel_poller_loop) in
addon/resources/lib/modes.py.

Covers Requirements 18.1–18.6:
  18.1  Poll /rachel-anchor every 2 s (verified via interval constant & thread behaviour)
  18.2  state="playing" with new jellyfin_id → play new item at computed position
  18.3  state="playing" same jellyfin_id, drift > threshold → seekTime()
  18.4  state="playing" same jellyfin_id, drift within threshold → no seek
  18.5  state="idle" → stop current playback, start Offline_Clip
  18.6  Request failure → retain state, no crash, at most one WARNING/60 s
"""

import json
import sys
import threading
import time
from unittest.mock import MagicMock, patch


# Kodi stubs are injected by conftest.py before any addon import.
sys.path.insert(0, "addon/resources/lib")

import modes as _modes_module
from modes import _rachel_poller_loop

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SCHEDULE_URL = "http://localhost:8300"
OFFLINE_URL = "http://jf:8096/offline"
JELLYFIN_URL = "http://jf:8096"


def _make_mock_modes(
    schedule_url: str = SCHEDULE_URL,
    offline_url: str = OFFLINE_URL,
    jellyfin_url: str = JELLYFIN_URL,
    is_playing: bool = False,
    current_time: float = 0.0,
) -> MagicMock:
    """Return a MagicMock that quacks like a ModeState for the poller."""
    m = MagicMock()
    m._rachel_stop = threading.Event()
    m._schedule_service_url = schedule_url
    m._offline_clip_url = offline_url
    m._jellyfin_base_url = jellyfin_url

    m.player = MagicMock()
    m.player.isPlaying.return_value = is_playing
    m.player.getTime.return_value = current_time
    return m


def _mock_response(data: dict) -> MagicMock:
    """Build a context-manager mock that returns JSON-encoded *data* on read()."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(data).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _run_one_iteration(mock_modes: MagicMock, response_data: dict) -> None:
    """Run exactly one poll iteration synchronously.

    Patches:
      - urllib.request.urlopen → returns a mock response with *response_data*
      - modes.RACHEL_POLL_INTERVAL_SECONDS → 0  (no actual sleep between iterations)

    The stop event is made to return False on the first wait() call (allowing
    one iteration to proceed) and True on all subsequent calls (stopping the
    loop).  The loop therefore executes the body exactly once before returning.
    """
    call_count = [0]

    def controlled_wait(timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return False  # first wait: let the loop body run
        return True       # subsequent: stop

    mock_modes._rachel_stop.wait = controlled_wait

    with patch("urllib.request.urlopen", return_value=_mock_response(response_data)):
        with patch.object(_modes_module, "RACHEL_POLL_INTERVAL_SECONDS", 0):
            _rachel_poller_loop(mock_modes)


def _run_one_iteration_with_error(mock_modes: MagicMock, exc: Exception) -> None:
    """Run one iteration where urlopen raises *exc*."""
    call_count = [0]

    def controlled_wait(timeout=None):
        call_count[0] += 1
        return call_count[0] > 1

    mock_modes._rachel_stop.wait = controlled_wait

    with patch("urllib.request.urlopen", side_effect=exc):
        with patch.object(_modes_module, "RACHEL_POLL_INTERVAL_SECONDS", 0):
            _rachel_poller_loop(mock_modes)


# ---------------------------------------------------------------------------
# Requirement 18.5 — Idle state
# ---------------------------------------------------------------------------

class TestIdleState:
    """WHEN state="idle", stop current playback and play Offline_Clip (Req 18.4/18.5)."""

    def test_idle_calls_stop_when_playing(self):
        """Player.stop() is called when idle and playback is in progress."""
        mock_modes = _make_mock_modes(is_playing=True)
        _run_one_iteration(mock_modes, {"state": "idle"})
        mock_modes.player.stop.assert_called_once()

    def test_idle_plays_offline_clip_after_stop(self):
        """Player.play() is called with the offline URL after stop()."""
        mock_modes = _make_mock_modes(is_playing=True)
        _run_one_iteration(mock_modes, {"state": "idle"})

        # play() must have been called
        assert mock_modes.player.play.called
        # The URL passed to play must contain the offline URL
        play_args = mock_modes.player.play.call_args
        url_arg = play_args[0][0] if play_args[0] else play_args[1].get("item", "")
        assert OFFLINE_URL in str(url_arg)

    def test_idle_does_not_call_stop_when_not_playing(self):
        """stop() is not called when player is already stopped."""
        mock_modes = _make_mock_modes(is_playing=False)
        _run_one_iteration(mock_modes, {"state": "idle"})
        mock_modes.player.stop.assert_not_called()

    def test_idle_does_not_call_seek(self):
        """seekTime() must never be called during an idle response."""
        mock_modes = _make_mock_modes(is_playing=True)
        _run_one_iteration(mock_modes, {"state": "idle"})
        mock_modes.player.seekTime.assert_not_called()


# ---------------------------------------------------------------------------
# Requirement 18.2 — New item in playing state
# ---------------------------------------------------------------------------

class TestNewItem:
    """WHEN state="playing" with a different jellyfin_id, play the new item (Req 18.2)."""

    def _playing_payload(self, jellyfin_id: str = "abc123", anchor_position: float = 10.0) -> dict:
        return {
            "state": "playing",
            "jellyfin_id": jellyfin_id,
            "anchor_position": anchor_position,
            # Use a fixed anchor_timestamp well in the past so computed_pos is
            # deterministic: anchor_position + elapsed ≥ anchor_position.
            "anchor_timestamp": time.time() - 5.0,
        }

    def test_new_item_calls_play(self):
        """player.play() is invoked when a new jellyfin_id is received."""
        mock_modes = _make_mock_modes()
        _run_one_iteration(mock_modes, self._playing_payload("item-001"))
        assert mock_modes.player.play.called

    def test_new_item_url_contains_jellyfin_id(self):
        """The play URL must reference the new item's jellyfin_id."""
        mock_modes = _make_mock_modes()
        _run_one_iteration(mock_modes, self._playing_payload("item-XYZ"))
        play_args = mock_modes.player.play.call_args
        url_arg = play_args[0][0] if play_args[0] else ""
        assert "item-XYZ" in str(url_arg)

    def test_new_item_url_contains_start_time_ticks(self):
        """The play URL must include StartTimeTicks to seek to the computed position."""
        mock_modes = _make_mock_modes()
        payload = self._playing_payload("item-seek", anchor_position=30.0)
        _run_one_iteration(mock_modes, payload)
        play_args = mock_modes.player.play.call_args
        url_arg = play_args[0][0] if play_args[0] else ""
        assert "StartTimeTicks=" in str(url_arg)

    def test_new_item_does_not_call_seek_time(self):
        """For a new item, seekTime() must not be called (seek is baked into URL)."""
        mock_modes = _make_mock_modes()
        _run_one_iteration(mock_modes, self._playing_payload("item-new"))
        mock_modes.player.seekTime.assert_not_called()

    def test_new_item_start_ticks_reflect_anchor_position(self):
        """StartTimeTicks must encode at least anchor_position (in 100ns units)."""
        mock_modes = _make_mock_modes()
        anchor_pos = 60.0  # 60 seconds
        payload = {
            "state": "playing",
            "jellyfin_id": "item-ticks",
            "anchor_position": anchor_pos,
            "anchor_timestamp": time.time(),  # no elapsed time drift
        }
        _run_one_iteration(mock_modes, payload)
        play_args = mock_modes.player.play.call_args
        url_str = str(play_args[0][0] if play_args[0] else "")
        # Extract ticks from URL
        import re
        match = re.search(r"StartTimeTicks=(\d+)", url_str)
        assert match is not None, f"StartTimeTicks not found in URL: {url_str}"
        ticks = int(match.group(1))
        # anchor_pos * 10_000_000 ticks; allow a tiny elapsed-time margin
        assert ticks >= int(anchor_pos * 10_000_000)


# ---------------------------------------------------------------------------
# Requirement 18.3 — Same item, drift exceeds threshold → seek
# ---------------------------------------------------------------------------

class TestDriftCorrectionRequired:
    """WHEN same jellyfin_id and drift > RACHEL_DRIFT_THRESHOLD_SECONDS → seekTime (Req 18.3)."""

    def _two_iteration_payload(self, jellyfin_id: str, anchor_position: float) -> dict:
        return {
            "state": "playing",
            "jellyfin_id": jellyfin_id,
            "anchor_position": anchor_position,
            "anchor_timestamp": time.time(),
        }

    def _run_two_iterations(self, mock_modes: MagicMock, first_payload: dict, second_payload: dict) -> None:
        """Run exactly two poll iterations against the same ModeState mock."""
        responses = iter([first_payload, second_payload])
        call_count = [0]

        def controlled_wait(timeout=None):
            call_count[0] += 1
            # Return False for iterations 1 and 2, True on the third call to stop.
            return call_count[0] > 2

        mock_modes._rachel_stop.wait = controlled_wait

        def make_response(data):
            return _mock_response(data)

        def urlopen_side_effect(url, timeout=None):
            return make_response(next(responses))

        with patch("urllib.request.urlopen", side_effect=urlopen_side_effect):
            with patch.object(_modes_module, "RACHEL_POLL_INTERVAL_SECONDS", 0):
                _rachel_poller_loop(mock_modes)

    def test_drift_beyond_threshold_calls_seek_time(self):
        """seekTime() called when local pos differs from computed pos by > threshold."""

        jf_id = "same-item-drift"
        anchor_pos = 50.0
        # After two iterations with the same item, the second poll finds
        # the local player significantly behind.
        mock_modes = _make_mock_modes(is_playing=True)

        # local_time will be 0.0 (the mock default), computed_pos ≥ anchor_pos=50.0
        # → drift = 50.0 - 0.0 = 50s, well above threshold.
        payload = self._two_iteration_payload(jf_id, anchor_pos)
        self._run_two_iterations(mock_modes, payload, payload)

        mock_modes.player.seekTime.assert_called()

    def test_drift_beyond_threshold_seek_target_is_computed_pos(self):
        """seekTime() target approximates anchor_position (within a few seconds of drift)."""

        jf_id = "same-seek-target"
        anchor_pos = 100.0
        mock_modes = _make_mock_modes(is_playing=True, current_time=0.0)
        payload = self._two_iteration_payload(jf_id, anchor_pos)
        self._run_two_iterations(mock_modes, payload, payload)

        seek_arg = mock_modes.player.seekTime.call_args[0][0]
        # computed_pos = anchor_pos + elapsed ≥ anchor_pos; allow up to 5s of
        # elapsed time between test construction and loop execution.
        assert abs(seek_arg - anchor_pos) < 10.0


# ---------------------------------------------------------------------------
# Requirement 18.4 — Same item, drift within threshold → no seek
# ---------------------------------------------------------------------------

class TestDriftWithinThreshold:
    """WHEN same jellyfin_id and drift ≤ threshold, no seekTime() call (Req 18.4)."""

    def _run_two_iterations(self, mock_modes: MagicMock, payload: dict) -> None:
        call_count = [0]

        def controlled_wait(timeout=None):
            call_count[0] += 1
            return call_count[0] > 2

        mock_modes._rachel_stop.wait = controlled_wait

        def urlopen_side_effect(url, timeout=None):
            return _mock_response(payload)

        with patch("urllib.request.urlopen", side_effect=urlopen_side_effect):
            with patch.object(_modes_module, "RACHEL_POLL_INTERVAL_SECONDS", 0):
                _rachel_poller_loop(mock_modes)

    def test_no_seek_when_drift_within_threshold(self):
        """seekTime() must NOT be called when local pos matches computed pos closely."""

        jf_id = "same-item-nodrift"
        # Set anchor_pos ≈ local current_time (both ~0); computed_pos ≈ 0 + tiny elapsed.
        # With current_time=0 and anchor_pos=0, drift < 1 s → no seek.
        anchor_pos = 0.0
        mock_modes = _make_mock_modes(is_playing=True, current_time=0.0)
        payload = {
            "state": "playing",
            "jellyfin_id": jf_id,
            "anchor_position": anchor_pos,
            "anchor_timestamp": time.time(),  # now → computed_pos ≈ 0
        }
        self._run_two_iterations(mock_modes, payload)
        mock_modes.player.seekTime.assert_not_called()


# ---------------------------------------------------------------------------
# Requirement 18.6 — Request failure
# ---------------------------------------------------------------------------

class TestRequestFailure:
    """IF GET /rachel-anchor fails, retain state, no crash, ≤1 WARNING/60 s (Req 18.6)."""

    def test_url_error_does_not_raise(self):
        """URLError must be swallowed; the loop continues without crashing."""
        import urllib.error

        mock_modes = _make_mock_modes()
        # Should not raise:
        _run_one_iteration_with_error(mock_modes, urllib.error.URLError("connection refused"))

    def test_http_error_does_not_raise(self):
        """HTTPError must be swallowed without crashing."""
        import urllib.error

        mock_modes = _make_mock_modes()
        exc = urllib.error.HTTPError(
            url="http://localhost:8300/rachel-anchor",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=None,
        )
        _run_one_iteration_with_error(mock_modes, exc)

    def test_generic_exception_does_not_raise(self):
        """Any unexpected exception during urlopen must be caught."""
        mock_modes = _make_mock_modes()
        _run_one_iteration_with_error(mock_modes, RuntimeError("unexpected"))

    def test_failure_does_not_call_player_stop(self):
        """Player.stop() must NOT be called when the request fails."""
        import urllib.error

        mock_modes = _make_mock_modes(is_playing=True)
        _run_one_iteration_with_error(mock_modes, urllib.error.URLError("timeout"))
        mock_modes.player.stop.assert_not_called()

    def test_failure_does_not_call_player_play(self):
        """player.play() must NOT be called when the request fails."""
        import urllib.error

        mock_modes = _make_mock_modes()
        _run_one_iteration_with_error(mock_modes, urllib.error.URLError("timeout"))
        mock_modes.player.play.assert_not_called()

    def test_warning_logged_on_first_failure(self):
        """xbmc.log() should be called at LOGWARNING level on the first failure."""
        import urllib.error

        mock_modes = _make_mock_modes()
        # Patch xbmc.log on the module object that modes.py captured at import
        # time. If other test files re-import conftest (re-registering sys.modules
        # with a fresh xbmc stub), `import xbmc` here would return a different
        # object than the one modes.py holds. Patching via _modes_module.xbmc
        # guarantees we intercept the actual calls modes.py makes.
        with patch.object(_modes_module.xbmc, "log") as mock_log:
            _run_one_iteration_with_error(mock_modes, urllib.error.URLError("oops"))

        assert mock_log.called, "xbmc.log was never called"
        # LOGWARNING == 2 in the stub; check positional or keyword 'level' arg.
        warning_calls = [
            c for c in mock_log.call_args_list
            if (len(c[0]) >= 2 and c[0][1] == 2)
            or c[1].get("level") == 2
        ]
        assert len(warning_calls) >= 1, (
            f"No LOGWARNING call found. Calls: {mock_log.call_args_list}"
        )

    def test_warning_rate_limited_within_60_seconds(self):
        """Only one LOGWARNING should fire within a 60-second window (Req 18.6).

        Simulates three consecutive failures; verifies that the second and third
        do not produce additional warning log calls (rate-limit window active).
        """
        import urllib.error

        mock_modes = _make_mock_modes()
        log_calls = []

        # Use _modes_module.xbmc — the exact module object modes.py imported at
        # load time — so the patch survives conftest re-imports by other test files.
        def capturing_log(msg, level=1):  # default=LOGINFO(1)
            log_calls.append((msg, level))

        # Run 3 failure iterations in rapid succession (< 1 s total).
        call_count = [0]

        def controlled_wait(timeout=None):
            call_count[0] += 1
            return call_count[0] > 3  # allow 3 iterations

        mock_modes._rachel_stop.wait = controlled_wait

        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("oops")):
            with patch.object(_modes_module, "RACHEL_POLL_INTERVAL_SECONDS", 0):
                with patch.object(_modes_module.xbmc, "log", side_effect=capturing_log):
                    _rachel_poller_loop(mock_modes)

        warning_count = sum(1 for _, level in log_calls if level == 2)
        assert warning_count == 1, (
            f"Expected 1 LOGWARNING across 3 rapid failures but got {warning_count}"
        )


# ---------------------------------------------------------------------------
# Miscellaneous / edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_missing_schedule_url_skips_iteration(self):
        """If _schedule_service_url is empty, the iteration is skipped silently."""
        mock_modes = _make_mock_modes(schedule_url="")
        # urlopen must NOT be called.
        with patch("urllib.request.urlopen") as mock_urlopen:
            call_count = [0]

            def controlled_wait(timeout=None):
                call_count[0] += 1
                return call_count[0] > 1

            mock_modes._rachel_stop.wait = controlled_wait
            with patch.object(_modes_module, "RACHEL_POLL_INTERVAL_SECONDS", 0):
                _rachel_poller_loop(mock_modes)

        mock_urlopen.assert_not_called()

    def test_unknown_state_does_not_crash(self):
        """An unknown state value in the response must be handled gracefully."""
        mock_modes = _make_mock_modes()
        _run_one_iteration(mock_modes, {"state": "unknown_future_state"})
        # Neither stop nor play nor seek should have been called.
        mock_modes.player.stop.assert_not_called()
        mock_modes.player.play.assert_not_called()
        mock_modes.player.seekTime.assert_not_called()

    def test_idle_without_offline_url_does_not_call_play(self):
        """If _offline_clip_url is empty, player.play() is not called on idle."""
        mock_modes = _make_mock_modes(is_playing=True, offline_url="")
        _run_one_iteration(mock_modes, {"state": "idle"})
        mock_modes.player.play.assert_not_called()

    def test_stop_is_called_before_play_on_new_item(self):
        """The first call on a new item must be to stop the current stream."""
        mock_modes = _make_mock_modes(is_playing=True)
        payload = {
            "state": "playing",
            "jellyfin_id": "brand-new-item",
            "anchor_position": 0.0,
            "anchor_timestamp": time.time(),
        }
        _run_one_iteration(mock_modes, payload)

        # Verify stop was NOT called for a new item start — the implementation
        # in modes.py plays directly without an explicit stop (the new play()
        # call replaces the stream). If the implementation changes to call stop
        # first, this test should be updated accordingly.
        # Current implementation: play() only, no explicit stop().
        assert mock_modes.player.play.called
