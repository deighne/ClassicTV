"""
Boot resilience tests for the sweet-potato-boot-network-race feature.

Covers three independent layers of defence against the black-screen boot race:

  Layer 1 — Readiness gate (_wait_for_jellyfin_server)
    Polls /System/Info/Public before the schedule retry loop so startup_play()
    never runs against an unreachable Jellyfin instance.

  Layer 2 — Playback confirmation (_play_confirm_event)
    play_slot() arms the event; onPlayBackStarted() sets it. The watchdog waits
    for it after every play_slot() call and distinguishes a wedged Jellyfin addon
    (server reachable, event never fires) from a network outage (server also down).

  Layer 3 — Self-heal (_reload_jellyfin_addon / _consecutive_failures)
    After WEDGE_FAILURE_THRESHOLD consecutive unconfirmed plays on a healthy
    server, the watchdog disables and re-enables plugin.video.jellyfin via
    JSON-RPC. A 5-minute cooldown prevents reload storms.

Test functions are added in tasks 11.2–11.5.
"""

import json
import time
from unittest.mock import MagicMock


import xbmc  # provided by conftest.py stubs

from player import ClassicTVPlayer
from schedule import ScheduleSlot
from constants import (
    JELLYFIN_RELOAD_COOLDOWN_SECONDS,
    WEDGE_FAILURE_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SUCCESS_RPC_RESPONSE = json.dumps({"id": 1, "jsonrpc": "2.0", "result": "OK"})


def _make_slot(
    channel_id: str = "sitcoms",
    title: str = "Test Show",
    jellyfin_item_id: str = "abc123",
    start_offset: float = 120.0,
    duration: float = 1800.0,
) -> ScheduleSlot:
    """Return a minimal valid ScheduleSlot for testing."""
    return ScheduleSlot(
        channel_id=channel_id,
        title=title,
        jellyfin_item_id=jellyfin_item_id,
        start_offset=start_offset,
        duration=duration,
    )


def _make_player(abort_requested: bool = False) -> ClassicTVPlayer:
    """Create a ClassicTVPlayer wired for boot-resilience testing.

    Monkeypatches applied:
    - ``xbmc.executeJSONRPC`` — MagicMock returning a success JSON string so
      _reload_jellyfin_addon() sees a clean RPC result without contacting Kodi.
    - ``xbmc.Monitor`` — stub whose ``abortRequested()`` returns ``abort_requested``
      (default False), giving per-test control over the shutdown path.

    Instance state set:
    - ``_schedule_client`` — mock with ``check_server_health()`` returning True and
      ``get_slot()`` returning a valid slot; tests override these as needed.
    - ``_current_channel_id`` — "sitcoms"
    """
    # Patch xbmc.executeJSONRPC for this player's lifetime.
    xbmc.executeJSONRPC = MagicMock(return_value=_SUCCESS_RPC_RESPONSE)

    # Patch xbmc.Monitor so abort behaviour is test-controlled.
    _abort = abort_requested

    class _ControllableMonitor:
        def abortRequested(self) -> bool:  # noqa: N802
            return _abort

        def waitForAbort(self, timeout: float = 0) -> bool:  # noqa: N802
            return _abort

    xbmc.Monitor = _ControllableMonitor

    # Build the player.
    player = ClassicTVPlayer()
    player._current_channel_id = "sitcoms"

    # Attach a default mock schedule client.
    mock_client = MagicMock()
    mock_client.check_server_health.return_value = True
    mock_client.get_slot.return_value = _make_slot()
    mock_client.channel_exists.return_value = True
    player._schedule_client = mock_client

    return player


# ---------------------------------------------------------------------------
# Task 11.2 — Readiness gate unit tests
# ---------------------------------------------------------------------------


def test_readiness_gate_exits_on_first_success() -> None:
    """_wait_for_jellyfin_server returns immediately when the server is healthy.

    Requirements: 3.1, 3.2
    """
    player = _make_player()
    player._schedule_client.check_server_health.return_value = True

    start = time.monotonic()
    player._wait_for_jellyfin_server(10)
    elapsed = time.monotonic() - start

    # Should return well before the 10 s timeout (xbmc.sleep is a no-op in
    # tests, so even a full loop is fast — but health returns True immediately
    # and the method returns after the first successful check).
    assert elapsed < 5, f"readiness gate took too long ({elapsed:.3f}s)"
    player._schedule_client.check_server_health.assert_called()


def test_readiness_gate_exits_after_timeout() -> None:
    """_wait_for_jellyfin_server returns even if the server never becomes healthy.

    Requirements: 3.1, 3.4
    """
    player = _make_player()
    player._schedule_client.check_server_health.return_value = False

    # A tiny timeout ensures the loop exhausts quickly (xbmc.sleep is a no-op).
    player._wait_for_jellyfin_server(0.01)
    # Reaching here without hanging is the assertion.


def test_readiness_gate_exits_on_kodi_abort() -> None:
    """_wait_for_jellyfin_server returns immediately when Kodi signals an abort.

    The abort flag is checked at the top of each iteration, so the server
    health endpoint is either never called or called at most once before the
    guard fires.

    Requirements: 3.3, 3.7
    """
    player = _make_player(abort_requested=True)
    player._schedule_client.check_server_health.return_value = False

    start = time.monotonic()
    player._wait_for_jellyfin_server(90)
    elapsed = time.monotonic() - start

    # Must return well before the 90 s timeout.
    assert elapsed < 5, f"readiness gate did not honour abort ({elapsed:.3f}s)"
    # Health endpoint never needs to be called when abort is set on entry.
    assert player._schedule_client.check_server_health.call_count <= 1


def test_readiness_gate_shows_notification() -> None:
    """_wait_for_jellyfin_server shows at least one OSD notification while polling.

    Requirements: 3.2, 3.5
    """
    import xbmcgui

    player = _make_player()
    player._schedule_client.check_server_health.return_value = False

    # Replace xbmcgui.Dialog with a MagicMock so we can inspect .notification().
    mock_dialog_instance = MagicMock()
    mock_dialog_class = MagicMock(return_value=mock_dialog_instance)
    xbmcgui.Dialog = mock_dialog_class

    # Short timeout — xbmc.sleep is a no-op so the loop runs through rapidly.
    player._wait_for_jellyfin_server(0.05)

    # At least one "Waiting for media server..." notification must have been shown.
    assert mock_dialog_instance.notification.call_count >= 1


# ---------------------------------------------------------------------------
# Task 11.3 — Playback confirmation unit tests
# ---------------------------------------------------------------------------


def test_startup_play_calls_readiness_gate_first():
    """_wait_for_jellyfin_server must be called before any get_slot() call.

    Validates: Requirements 3.1, 4.1
    """
    player = _make_player()

    call_order = []

    def _mock_wait(timeout):
        call_order.append("wait")

    def _mock_get_slot(channel_id, **kwargs):
        call_order.append("get_slot")
        return _make_slot()

    player._wait_for_jellyfin_server = _mock_wait
    player._schedule_client.get_slot.side_effect = _mock_get_slot
    player._schedule_client.check_server_health.return_value = True

    player.startup_play("sitcoms")

    assert "wait" in call_order, "_wait_for_jellyfin_server was not called"
    assert "get_slot" in call_order, "get_slot was not called"
    assert call_order.index("wait") < call_order.index("get_slot"), (
        "_wait_for_jellyfin_server must be called before get_slot"
    )


def test_play_slot_arms_confirm_event():
    """play_slot() must clear _play_confirm_event after executebuiltin.

    Validates: Requirements 4.1, 4.6
    """
    player = _make_player()

    # Pre-set the event to confirm play_slot() clears it.
    player._play_confirm_event.set()
    assert player._play_confirm_event.is_set()

    player.play_slot(_make_slot())

    assert not player._play_confirm_event.is_set(), (
        "_play_confirm_event should be cleared (not set) after play_slot()"
    )


def test_onplaybackstarted_sets_confirm_event():
    """onPlayBackStarted() must set _play_confirm_event.

    Validates: Requirements 4.2
    """
    player = _make_player()

    assert not player._play_confirm_event.is_set(), (
        "_play_confirm_event should not be set before onPlayBackStarted()"
    )

    player.onPlayBackStarted()

    assert player._play_confirm_event.is_set(), (
        "_play_confirm_event should be set after onPlayBackStarted()"
    )


def test_onplaybackstarted_resets_consecutive_failures():
    """onPlayBackStarted() must reset _consecutive_failures to 0.

    Validates: Requirements 7.3
    """
    player = _make_player()
    player._consecutive_failures = 5

    player.onPlayBackStarted()

    assert player._consecutive_failures == 0, (
        "_consecutive_failures should be reset to 0 by onPlayBackStarted()"
    )


# ---------------------------------------------------------------------------
# Task 11.4 — Self-heal unit tests
# ---------------------------------------------------------------------------


def test_reload_addon_cooldown_prevents_rpc():
    """_reload_jellyfin_addon returns False without calling executeJSONRPC when within cooldown.

    Requirements: 5.6
    """
    player = _make_player()
    # Simulate a reload that just happened.
    player._last_reload_time = time.monotonic()

    result = player._reload_jellyfin_addon()

    assert result is False, (
        "_reload_jellyfin_addon should return False when within cooldown"
    )
    xbmc.executeJSONRPC.assert_not_called()


def test_reload_addon_issues_disable_then_enable():
    """_reload_jellyfin_addon calls executeJSONRPC twice: disable then enable.

    The first call must have enabled=false, the second enabled=true.

    Requirements: 5.1, 5.2
    """
    player = _make_player()
    # Pre-set the confirm event so the wait() returns True immediately.
    player._play_confirm_event.set()

    player._reload_jellyfin_addon()

    assert xbmc.executeJSONRPC.call_count == 2, (
        f"Expected 2 RPC calls, got {xbmc.executeJSONRPC.call_count}"
    )

    first_call_args = xbmc.executeJSONRPC.call_args_list[0][0][0]
    second_call_args = xbmc.executeJSONRPC.call_args_list[1][0][0]

    first_payload = json.loads(first_call_args)
    second_payload = json.loads(second_call_args)

    assert first_payload["params"]["enabled"] is False, (
        "First RPC call should disable the addon (enabled=false)"
    )
    assert second_payload["params"]["enabled"] is True, (
        "Second RPC call should re-enable the addon (enabled=true)"
    )


def test_reload_addon_sets_last_reload_time():
    """_reload_jellyfin_addon updates _last_reload_time after a successful reload.

    Requirements: 5.6
    """
    player = _make_player()
    assert player._last_reload_time == 0.0, "Expected _last_reload_time to start at 0.0"

    # Pre-set the event so wait() returns True immediately.
    player._play_confirm_event.set()

    before = time.monotonic()
    player._reload_jellyfin_addon()

    assert player._last_reload_time > 0.0, (
        "_last_reload_time should be updated after reload"
    )
    assert player._last_reload_time >= before, (
        "_last_reload_time should be >= the time before the reload"
    )


def test_reload_addon_falls_back_to_restartapp_on_rpc_error():
    """_reload_jellyfin_addon calls executebuiltin('RestartApp') when the RPC disable fails.

    Requirements: 5.4, 5.5
    """
    player = _make_player()
    error_response = json.dumps(
        {
            "id": 1,
            "jsonrpc": "2.0",
            "error": {"code": -32601, "message": "Method not found"},
        }
    )
    xbmc.executeJSONRPC = MagicMock(return_value=error_response)
    xbmc.executebuiltin = MagicMock()

    player._reload_jellyfin_addon()

    restart_calls = [
        call
        for call in xbmc.executebuiltin.call_args_list
        if call[0][0] == "RestartApp"
    ]
    assert len(restart_calls) >= 1, (
        "executebuiltin('RestartApp') should be called when the RPC disable step fails"
    )


def test_reload_addon_falls_back_to_restartapp_on_confirm_timeout():
    """_reload_jellyfin_addon calls executebuiltin('RestartApp') when playback is not confirmed.

    Requirements: 5.7, 5.8
    """
    player = _make_player()
    # Patch wait() to simulate instant timeout (returns False = not confirmed).
    player._play_confirm_event.wait = lambda timeout: False
    xbmc.executebuiltin = MagicMock()

    player._reload_jellyfin_addon()

    restart_calls = [
        call
        for call in xbmc.executebuiltin.call_args_list
        if call[0][0] == "RestartApp"
    ]
    assert len(restart_calls) >= 1, (
        "executebuiltin('RestartApp') should be called when onPlayBackStarted never fires"
    )


# ---------------------------------------------------------------------------
# Task 11.5 — Watchdog integration unit tests
# ---------------------------------------------------------------------------


def test_watchdog_increments_failure_on_unconfirmed_server_healthy() -> None:
    """watchdog_recover() increments _consecutive_failures when playback is
    not confirmed and the server is healthy.

    Validates: Requirements 7.1, 7.2
    """
    player = _make_player()
    player._schedule_client.check_server_health.return_value = True

    # Simulate instant timeout: wait() returns False (never confirmed).
    player._play_confirm_event.wait = lambda t: False

    player.watchdog_recover()

    assert player._consecutive_failures == 1, (
        "_consecutive_failures should be 1 after one unconfirmed recovery on a healthy server"
    )


def test_watchdog_no_increment_on_server_unreachable() -> None:
    """watchdog_recover() does NOT increment _consecutive_failures when the
    server is unreachable (network outage path, not a wedged addon).

    Validates: Requirements 7.2
    """
    player = _make_player()
    player._schedule_client.check_server_health.return_value = False

    # Simulate instant timeout: wait() returns False (never confirmed).
    player._play_confirm_event.wait = lambda t: False

    player.watchdog_recover()

    assert player._consecutive_failures == 0, (
        "_consecutive_failures should remain 0 when the server is unreachable"
    )


def test_watchdog_calls_reload_at_threshold() -> None:
    """watchdog_recover() calls _reload_jellyfin_addon() when
    _consecutive_failures reaches WEDGE_FAILURE_THRESHOLD.

    Validates: Requirements 7.1, 7.2
    """
    player = _make_player()
    player._schedule_client.check_server_health.return_value = True
    player._consecutive_failures = WEDGE_FAILURE_THRESHOLD - 1

    # Simulate instant timeout so the threshold branch is reached.
    player._play_confirm_event.wait = lambda t: False

    player._reload_jellyfin_addon = MagicMock(return_value=True)

    player.watchdog_recover()

    player._reload_jellyfin_addon.assert_called_once()


def test_watchdog_resets_failure_counter_after_reload() -> None:
    """watchdog_recover() resets _consecutive_failures to 0 after triggering
    a reload (so a single threshold breach does not immediately re-trigger).

    Validates: Requirements 7.1, 7.2
    """
    player = _make_player()
    player._schedule_client.check_server_health.return_value = True
    player._consecutive_failures = WEDGE_FAILURE_THRESHOLD - 1

    player._play_confirm_event.wait = lambda t: False
    player._reload_jellyfin_addon = MagicMock(return_value=True)

    player.watchdog_recover()

    assert player._consecutive_failures == 0, (
        "_consecutive_failures should be reset to 0 after _reload_jellyfin_addon is called"
    )


def test_watchdog_confirmed_playback_resets_failure_counter() -> None:
    """When onPlayBackStarted fires (simulated by calling it directly),
    _consecutive_failures is reset to 0 regardless of its prior value.

    The watchdog itself does not reset the counter on confirmed playback — it
    just returns. The reset is performed by onPlayBackStarted(). This test
    verifies the full confirmation path end-to-end.

    Validates: Requirements 7.3, 7.4
    """
    player = _make_player()
    player._consecutive_failures = 3

    # Simulate confirmed playback: wait() returns True immediately.
    player._play_confirm_event.wait = lambda t: True

    player.watchdog_recover()

    # The watchdog returned early (confirmed). Counter is still 3 — the
    # watchdog does not reset it; that is onPlayBackStarted's job.
    assert player._consecutive_failures == 3, (
        "watchdog_recover() should not reset _consecutive_failures on confirmed playback; "
        "that is onPlayBackStarted's responsibility"
    )

    # Now simulate the callback arriving.
    player.onPlayBackStarted()

    assert player._consecutive_failures == 0, (
        "_consecutive_failures should be 0 after onPlayBackStarted() fires"
    )


# ---------------------------------------------------------------------------
# Task 12 — Hypothesis property-based tests
# ---------------------------------------------------------------------------

from hypothesis import given, settings
from hypothesis import strategies as st


@given(health_sequence=st.lists(st.booleans(), min_size=0, max_size=60))
@settings(max_examples=200)
def test_readiness_gate_always_terminates(health_sequence):
    """
    Feature: sweet-potato-boot-network-race,
    Property 1: Readiness gate always terminates
    Validates: Requirements 3.1, 3.4
    """
    player = _make_player()
    sequence = list(health_sequence)

    def _health():
        if sequence:
            return sequence.pop(0)
        return False

    player._schedule_client.check_server_health.side_effect = _health
    # Must return without exception for any mix of True/False responses
    player._wait_for_jellyfin_server(0.001)  # tiny timeout for speed


@given(item_ids=st.lists(st.text(min_size=1, max_size=36), min_size=1, max_size=20))
@settings(max_examples=200)
def test_confirmation_event_single_record(item_ids):
    """
    Feature: sweet-potato-boot-network-race,
    Property 3: At most one pending confirmation record
    Validates: Requirements 4.1, 4.6
    """
    import threading as _threading

    player = _make_player()

    for item_id in item_ids:
        slot = _make_slot(jellyfin_item_id=item_id)
        player.play_slot(slot)
        # After each play_slot() call, the event should be cleared (not set)
        assert not player._play_confirm_event.is_set(), (
            f"_play_confirm_event should be cleared after play_slot() with item_id={item_id!r}"
        )

    # There is always exactly one threading.Event for confirmation
    assert isinstance(player._play_confirm_event, _threading.Event)


@given(
    elapsed=st.floats(
        min_value=0.0,
        max_value=JELLYFIN_RELOAD_COOLDOWN_SECONDS - 0.001,
        allow_nan=False,
        allow_infinity=False,
    )
)
@settings(max_examples=200)
def test_reload_cooldown_never_violated(elapsed):
    """
    Feature: sweet-potato-boot-network-race,
    Property 4: Reload cooldown is never violated
    Validates: Requirements 5.6
    """
    player = _make_player()
    # Set _last_reload_time so that `elapsed` seconds appear to have passed
    player._last_reload_time = time.monotonic() - elapsed

    # Reset the executeJSONRPC mock to track calls from this test only
    xbmc.executeJSONRPC = MagicMock(return_value=_SUCCESS_RPC_RESPONSE)

    result = player._reload_jellyfin_addon()

    assert result is False, (
        f"_reload_jellyfin_addon should return False when only {elapsed:.3f}s have elapsed "
        f"(cooldown is {JELLYFIN_RELOAD_COOLDOWN_SECONDS}s)"
    )
    xbmc.executeJSONRPC.assert_not_called()


@given(n=st.integers(min_value=1, max_value=WEDGE_FAILURE_THRESHOLD - 1))
@settings(max_examples=100)
def test_failure_counter_increments_monotonically(n):
    """
    Feature: sweet-potato-boot-network-race,
    Property 5: Consecutive-failure counter increments monotonically until reset
    Validates: Requirements 7.1
    """
    player = _make_player()
    player._schedule_client.check_server_health.return_value = True
    # Simulate instant timeout so each watchdog call increments the counter
    player._play_confirm_event.wait = lambda t: False

    for i in range(n):
        player.watchdog_recover()
        assert player._consecutive_failures == i + 1, (
            f"After {i + 1} calls, _consecutive_failures should be {i + 1}, "
            f"got {player._consecutive_failures}"
        )


@given(failures_before=st.integers(min_value=0, max_value=10))
@settings(max_examples=200)
def test_confirmed_playback_resets_counter(failures_before):
    """
    Feature: sweet-potato-boot-network-race,
    Property 7: Confirmed playback always resets the failure counter
    Validates: Requirements 7.3
    """
    player = _make_player()
    player._consecutive_failures = failures_before

    player.onPlayBackStarted()

    assert player._consecutive_failures == 0, (
        f"_consecutive_failures should be 0 after onPlayBackStarted(), "
        f"regardless of previous value ({failures_before})"
    )
