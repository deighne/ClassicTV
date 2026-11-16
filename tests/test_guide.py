"""
Unit tests for addon/resources/lib/guide.py.

Covers:
- Cache behavior (Requirement 16.4): first toggle fetches, second toggle
  within 30s uses cache, toggle after TTL refetches.
- Single-call Dialog.select() population (Requirement 16.5): all items are
  passed to select() in one call, count matches channel count.
- Fetch failure (Requirement 16.8): error notification shown, no channel
  change callback.
- Channel selection: correct channel_id forwarded to callback, -1 means no
  callback.
"""

import json
import sys
import time
import urllib.error
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Kodi stubs must be in sys.modules before any addon import.
# conftest.py is not auto-loaded in direct test runs via this import path,
# so we import it explicitly here. Pytest loads it automatically, making
# this import idempotent.
# ---------------------------------------------------------------------------
import os

# Ensure the tests directory is on sys.path so conftest can be imported.
_TESTS_DIR = os.path.dirname(__file__)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import conftest  # noqa: F401  — registers xbmc/xbmcgui/xbmcaddon/xbmcvfs stubs

# Ensure the addon lib directory is on sys.path.
_LIB_DIR = os.path.join(os.path.dirname(__file__), "..", "addon", "resources", "lib")
if _LIB_DIR not in sys.path:
    sys.path.insert(0, _LIB_DIR)

from guide import TVGuide  # noqa: E402

# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_GUIDE = {
    "channels": [
        {
            "id": "ch1",
            "number": 5,
            "name": "Nick",
            "now": {"title": "Hey Arnold", "type": "episode", "minutes_remaining": 10},
            "next": {"title": "Rugrats"},
        },
        {
            "id": "ch2",
            "number": 7,
            "name": "CN",
            "now": {"title": "Offline", "type": "offline", "minutes_remaining": 0},
            "next": None,
        },
    ]
}

SAMPLE_GUIDE_SINGLE = {
    "channels": [
        {
            "id": "abc",
            "number": 3,
            "name": "ABC",
            "now": {"title": "News at 6", "type": "episode", "minutes_remaining": 25},
            "next": {"title": "Wheel of Fortune"},
        }
    ]
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_urlopen(payload: dict = SAMPLE_GUIDE):
    """Return a side_effect function that simulates a successful urlopen."""

    def _handler(url, timeout):
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    return _handler


def _make_guide(callback=None):
    """Construct a fresh TVGuide pointing at a local test URL."""
    cb = callback or MagicMock()
    guide = TVGuide("http://localhost:8300", cb)
    return guide, cb


# ===========================================================================
# Cache behavior — Requirement 16.4
# ===========================================================================


class TestCacheBehavior:
    """First open fetches; re-open within TTL uses cache; expired TTL refetches."""

    def test_first_toggle_fetches_from_network(self):
        """First toggle() must make exactly one network request."""
        guide, _ = _make_guide()
        mock_dialog = MagicMock()
        mock_dialog.select.return_value = -1  # no selection

        with patch("guide.xbmcgui.Dialog", return_value=mock_dialog):
            with patch("urllib.request.urlopen", side_effect=_mock_urlopen()) as mock_open:
                guide.toggle()

        mock_open.assert_called_once()

    def test_second_toggle_within_ttl_uses_cache(self):
        """Second toggle() within 30 s must not make a network request."""
        guide, _ = _make_guide()
        mock_dialog = MagicMock()
        mock_dialog.select.return_value = -1

        with patch("guide.xbmcgui.Dialog", return_value=mock_dialog):
            with patch("urllib.request.urlopen", side_effect=_mock_urlopen()) as mock_open:
                guide.toggle()  # first — fetches
                guide.toggle()  # second — should use cache
                assert mock_open.call_count == 1, (
                    "urlopen should be called only once when the second toggle is within the TTL"
                )

    def test_toggle_after_ttl_refetches(self):
        """toggle() after the 30-second TTL expires must make a new network request."""
        guide, _ = _make_guide()
        mock_dialog = MagicMock()
        mock_dialog.select.return_value = -1

        with patch("guide.xbmcgui.Dialog", return_value=mock_dialog):
            with patch("urllib.request.urlopen", side_effect=_mock_urlopen()) as mock_open:
                guide.toggle()  # first fetch
                assert mock_open.call_count == 1

                # Expire the cache manually by backdating the cache timestamp.
                guide._cache_time = time.time() - 31  # 31 s ago — past the 30 s TTL

                guide.toggle()  # should refetch
                assert mock_open.call_count == 2, (
                    "urlopen should be called again after the cache TTL expires"
                )


# ===========================================================================
# Single-call Dialog.select() population — Requirement 16.5
# ===========================================================================


class TestSingleCallPopulation:
    """Dialog.select() must receive all items in a single call."""

    def test_select_called_once_with_full_list(self):
        """Dialog().select() must be invoked exactly once per guide open."""
        guide, _ = _make_guide()
        mock_dialog = MagicMock()
        mock_dialog.select.return_value = -1

        with patch("guide.xbmcgui.Dialog", return_value=mock_dialog):
            with patch("urllib.request.urlopen", side_effect=_mock_urlopen()):
                guide.toggle()

        mock_dialog.select.assert_called_once()

    def test_item_count_matches_channel_count(self):
        """Number of items passed to select() must equal the channel count in the response."""
        guide, _ = _make_guide()
        mock_dialog = MagicMock()
        mock_dialog.select.return_value = -1

        with patch("guide.xbmcgui.Dialog", return_value=mock_dialog):
            with patch("urllib.request.urlopen", side_effect=_mock_urlopen()):
                guide.toggle()

        # select() signature: select(heading, list_of_items)
        _, args, _ = mock_dialog.select.mock_calls[0]
        _heading, items = args
        expected_count = len(SAMPLE_GUIDE["channels"])
        assert len(items) == expected_count, (
            f"Expected {expected_count} items in select(), got {len(items)}"
        )

    def test_single_channel_item_count(self):
        """Works correctly for a single-channel response."""
        guide, _ = _make_guide()
        mock_dialog = MagicMock()
        mock_dialog.select.return_value = -1

        with patch("guide.xbmcgui.Dialog", return_value=mock_dialog):
            with patch("urllib.request.urlopen", side_effect=_mock_urlopen(SAMPLE_GUIDE_SINGLE)):
                guide.toggle()

        _, args, _ = mock_dialog.select.mock_calls[0]
        _, items = args
        assert len(items) == 1


# ===========================================================================
# Fetch failure — Requirement 16.8
# ===========================================================================


class TestFetchFailure:
    """On any fetch error: show error notification; no channel-change callback."""

    def _run_with_error(self, error):
        """Helper: run toggle() with urlopen raising `error`; return (guide, callback, dialog)."""
        guide, cb = _make_guide()
        mock_dialog = MagicMock()
        mock_dialog.select.return_value = -1

        with patch("guide.xbmcgui.Dialog", return_value=mock_dialog):
            with patch("urllib.request.urlopen", side_effect=error):
                guide.toggle()

        return guide, cb, mock_dialog

    def test_url_error_shows_notification(self):
        """URLError during fetch must trigger a notification dialog."""
        _, _, mock_dialog = self._run_with_error(
            urllib.error.URLError("connection refused")
        )
        mock_dialog.notification.assert_called_once()

    def test_url_error_no_channel_change_callback(self):
        """URLError must not invoke the channel-change callback."""
        _, cb, _ = self._run_with_error(urllib.error.URLError("connection refused"))
        cb.assert_not_called()

    def test_os_error_shows_notification(self):
        """OSError (e.g. socket.timeout) during fetch must trigger a notification."""
        _, _, mock_dialog = self._run_with_error(OSError("timed out"))
        mock_dialog.notification.assert_called_once()

    def test_os_error_no_channel_change_callback(self):
        """OSError must not invoke the channel-change callback."""
        _, cb, _ = self._run_with_error(OSError("timed out"))
        cb.assert_not_called()

    def test_malformed_json_shows_notification(self):
        """A response with invalid JSON must trigger a notification."""
        guide, cb = _make_guide()
        mock_dialog = MagicMock()
        mock_dialog.select.return_value = -1

        def _bad_json(url, timeout):
            resp = MagicMock()
            resp.read.return_value = b"not json {"
            resp.__enter__ = lambda s: s
            resp.__exit__ = MagicMock(return_value=False)
            return resp

        with patch("guide.xbmcgui.Dialog", return_value=mock_dialog):
            with patch("urllib.request.urlopen", side_effect=_bad_json):
                guide.toggle()

        mock_dialog.notification.assert_called_once()
        cb.assert_not_called()

    def test_notification_is_warning_icon(self):
        """Notification must use the WARNING icon (Requirement 16.8 style)."""
        _, _, mock_dialog = self._run_with_error(urllib.error.URLError("refused"))
        call_args = mock_dialog.notification.call_args
        # Third positional arg is the icon; xbmcgui.NOTIFICATION_WARNING == "warning"
        assert call_args.args[2] == "warning", (
            "Error notification should use NOTIFICATION_WARNING icon"
        )

    def test_notification_2s_duration(self):
        """Notification must specify 2 000 ms display duration (Requirement 16.8)."""
        _, _, mock_dialog = self._run_with_error(urllib.error.URLError("refused"))
        call_args = mock_dialog.notification.call_args
        assert call_args.args[3] == 2000, (
            "Error notification should be shown for 2 000 ms"
        )


# ===========================================================================
# Channel selection behavior
# ===========================================================================


class TestChannelSelection:
    """Selecting a channel invokes callback; dismissing does not."""

    def test_selected_channel_triggers_callback_with_correct_id(self):
        """Selecting index 0 must call callback with ch1's id."""
        guide, cb = _make_guide()
        mock_dialog = MagicMock()
        mock_dialog.select.return_value = 0  # first channel selected

        with patch("guide.xbmcgui.Dialog", return_value=mock_dialog):
            with patch("urllib.request.urlopen", side_effect=_mock_urlopen()):
                guide.toggle()

        cb.assert_called_once_with("ch1")

    def test_second_channel_selection_triggers_correct_id(self):
        """Selecting index 1 must call callback with ch2's id."""
        guide, cb = _make_guide()
        mock_dialog = MagicMock()
        mock_dialog.select.return_value = 1  # second channel selected

        with patch("guide.xbmcgui.Dialog", return_value=mock_dialog):
            with patch("urllib.request.urlopen", side_effect=_mock_urlopen()):
                guide.toggle()

        cb.assert_called_once_with("ch2")

    def test_no_selection_returns_minus_one_no_callback(self):
        """Dialog returning -1 (dismissed) must not invoke the callback."""
        guide, cb = _make_guide()
        mock_dialog = MagicMock()
        mock_dialog.select.return_value = -1

        with patch("guide.xbmcgui.Dialog", return_value=mock_dialog):
            with patch("urllib.request.urlopen", side_effect=_mock_urlopen()):
                guide.toggle()

        cb.assert_not_called()

    def test_guide_not_visible_after_selection(self):
        """After a selection is made the guide should mark itself not visible."""
        guide, _ = _make_guide()
        mock_dialog = MagicMock()
        mock_dialog.select.return_value = 0

        with patch("guide.xbmcgui.Dialog", return_value=mock_dialog):
            with patch("urllib.request.urlopen", side_effect=_mock_urlopen()):
                guide.toggle()

        assert guide._is_visible is False

    def test_guide_not_visible_after_dismiss(self):
        """After the user dismisses (select=-1) the guide is not visible."""
        guide, _ = _make_guide()
        mock_dialog = MagicMock()
        mock_dialog.select.return_value = -1

        with patch("guide.xbmcgui.Dialog", return_value=mock_dialog):
            with patch("urllib.request.urlopen", side_effect=_mock_urlopen()):
                guide.toggle()

        assert guide._is_visible is False


# ===========================================================================
# Display formatting
# ===========================================================================


class TestDisplayFormatting:
    """Spot-checks on the display lines built for Dialog.select()."""

    def _get_display_lines(self, payload=SAMPLE_GUIDE, select_return=-1):
        guide, _ = _make_guide()
        mock_dialog = MagicMock()
        mock_dialog.select.return_value = select_return
        captured = {}

        def capture_select(heading, items):
            captured["items"] = items
            return select_return

        mock_dialog.select.side_effect = capture_select

        with patch("guide.xbmcgui.Dialog", return_value=mock_dialog):
            with patch("urllib.request.urlopen", side_effect=_mock_urlopen(payload)):
                guide.toggle()

        return captured.get("items", [])

    def test_offline_channel_shows_offline(self):
        """A channel with type='offline' should display 'Offline' as the program."""
        lines = self._get_display_lines()
        # ch2 (CN) has type="offline"
        cn_line = lines[1]
        assert "Offline" in cn_line

    def test_active_channel_shows_title(self):
        """A channel with a real program should include its title."""
        lines = self._get_display_lines()
        # ch1 (Nick) has title="Hey Arnold"
        nick_line = lines[0]
        assert "Hey Arnold" in nick_line

    def test_next_title_included_when_present(self):
        """Next program title should appear in the display line when provided."""
        lines = self._get_display_lines()
        nick_line = lines[0]
        assert "Rugrats" in nick_line

    def test_channel_number_and_name_present(self):
        """Channel number and name must appear in every line."""
        lines = self._get_display_lines()
        assert "5" in lines[0]
        assert "Nick" in lines[0]
        assert "7" in lines[1]
        assert "CN" in lines[1]
