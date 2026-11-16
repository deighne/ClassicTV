"""
Unit tests for ChannelBug (channel_bug.py) — task 7.1.

Requirements: 1.2, 1.4, 3.1, 4.1, 4.2, 4.3, 5.1, 5.2

Tests rely on the Kodi stubs in tests4/conftest.py. No real Kodi or network
connection is needed.
"""

import pytest
from unittest.mock import patch

import xbmcgui

from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st

from channel_bug import ChannelBug


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def bug():
    """Create a fresh ChannelBug and reset the Window property store before each test."""
    xbmcgui.Window._reset()
    b = ChannelBug("/fake/path")
    yield b
    # Teardown: cancel any live timer so it doesn't outlive the test.
    if b._timer is not None:
        b._timer.cancel()
        b._timer = None
    if getattr(b, "_static_timer", None) is not None:
        b._static_timer.cancel()
        b._static_timer = None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_show_for_publishes_number_and_name(bug):
    """show_for(5, "NBC") publishes "5" and "NBC" as window properties. (Req 1.2)

    The XML labels bind to these properties (rather than getControl().setLabel(),
    which is unreliable before the dialog window initialises).
    """
    bug.show_for(5, "NBC")
    w = xbmcgui.Window(10000)
    assert w.getProperty("ClassicTV.ChannelNumber") == "5"
    assert w.getProperty("ClassicTV.ChannelName") == "NBC"


def test_show_for_zero_displays_ch(bug):
    """show_for(0, …) publishes the string "CH", not "0". (Req 1.4)"""
    bug.show_for(0, "Test")
    assert xbmcgui.Window(10000).getProperty("ClassicTV.ChannelNumber") == "CH"


def test_window_properties_set_after_show(bug):
    """show_for() sets BugVisible="1" and ChannelName on Window(10000). (Req 4.1)"""
    bug.show_for(3, "CBS")
    w = xbmcgui.Window(10000)
    assert w.getProperty("ClassicTV.BugVisible") == "1"
    assert w.getProperty("ClassicTV.ChannelName") == "CBS"


def test_window_properties_cleared_after_timeout(bug):
    """_on_timeout() clears all three ClassicTV window properties. (Req 4.2)"""
    bug.show_for(3, "CBS")
    bug._on_timeout()
    w = xbmcgui.Window(10000)
    assert w.getProperty("ClassicTV.BugVisible") == ""
    assert w.getProperty("ClassicTV.ChannelName") == ""
    assert w.getProperty("ClassicTV.ChannelNumber") == ""


def test_show_failure_does_not_set_properties(bug):
    """If show() raises, _clear_window_properties() is called so BugVisible stays "". (Req 5.1, 4.3)"""
    with patch.object(bug, "show", side_effect=RuntimeError("Kodi show failed")):
        # Must not raise
        bug.show_for(3, "CBS")
    w = xbmcgui.Window(10000)
    assert w.getProperty("ClassicTV.BugVisible") == ""


def test_show_for_calls_show(bug):
    """show_for() invokes show() and arms the timer. (Req 2.1)"""
    with patch.object(bug, "show") as mock_show:
        bug.show_for(3, "CBS")
    mock_show.assert_called_once()
    assert bug._timer is not None


def test_onaction_delegates_to_super(bug):
    """onAction() forwards to the base class without consuming the event. (Req 3.1)"""
    initial_visible = bug._visible
    # Must not raise with None action
    bug.onAction(None)
    # Visibility must not have changed as a side effect
    assert bug._visible == initial_visible


# ---------------------------------------------------------------------------
# Property-based tests (Hypothesis)
# ---------------------------------------------------------------------------


@given(
    number=st.integers(min_value=1, max_value=999),
    name=st.text(min_size=1, max_size=50),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=50)
def test_show_for_sets_correct_labels(number, name):
    """Property 1: show_for(number, name) always sets the correct control labels.

    **Validates: Requirements 1.2, 1.3**
    """
    import xbmcgui

    xbmcgui.Window._reset()
    b = ChannelBug("/fake/path")
    try:
        b.show_for(number, name)
        w = xbmcgui.Window(10000)
        assert w.getProperty("ClassicTV.ChannelNumber") == str(number)
        assert w.getProperty("ClassicTV.ChannelName") == name
    finally:
        if b._timer is not None:
            b._timer.cancel()
            b._timer = None


# ---------------------------------------------------------------------------
# Property-based tests
# ---------------------------------------------------------------------------


@given(
    calls=st.lists(
        st.tuples(st.integers(0, 99), st.text(max_size=20)),
        min_size=1,
        max_size=5,
    )
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=50)
def test_timer_idempotency(calls):
    """Property 4: After any sequence of show_for() calls, exactly one timer is pending.

    **Validates: Requirements 2.2, 2.3**
    """
    import xbmcgui

    xbmcgui.Window._reset()
    b = ChannelBug("/fake/path")
    try:
        for number, name in calls:
            b.show_for(number, name)
        # After all calls, exactly one timer must be live
        assert b._timer is not None
        assert b._timer.is_alive()
    finally:
        if b._timer is not None:
            b._timer.cancel()
            b._timer = None


# ---------------------------------------------------------------------------
# Factory construction — regression guard
# ---------------------------------------------------------------------------


def test_create_channel_bug_passes_skin_and_res():
    """Regression: construction must pass ("default", "1080i").

    Omitting these made Kodi resolve resources/skins/Default and raise
    "XML File for Window is missing", so the overlay never appeared on device.
    """
    import channel_bug as cb

    with patch.object(cb, "ChannelBug") as mock_bug:
        cb.create_channel_bug("/addon/path")

    mock_bug.assert_called_once_with(
        "ChannelBug.xml", "/addon/path", "default", "1080i"
    )


def test_create_channel_bug_returns_none_on_construction_failure():
    """A bad/missing XML must disable the overlay, not raise into the service."""
    import channel_bug as cb

    with patch.object(cb, "ChannelBug", side_effect=RuntimeError("missing xml")):
        assert cb.create_channel_bug("/addon/path") is None


# ---------------------------------------------------------------------------
# show_text / hide (digit-entry display)
# ---------------------------------------------------------------------------


def test_show_text_sets_properties_and_arms_short_timer():
    import xbmcgui
    from channel_bug import ChannelBug

    bug = ChannelBug("ChannelBug.xml", "/addon", "default", "1080i")
    bug.show_text("12_", "It's 1997", 2.5, title="Press ENTER to tune", badge="TUNE")
    w = xbmcgui.Window(10000)
    assert w.getProperty("ClassicTV.ChannelNumber") == "12_"
    assert w.getProperty("ClassicTV.ChannelDisplay") == "CH 12_"
    assert w.getProperty("ClassicTV.ChannelName") == "It's 1997"
    assert w.getProperty("ClassicTV.NowPlaying") == "Press ENTER to tune"
    assert w.getProperty("ClassicTV.OSDBadge") == "TUNE"
    assert bug._timer is not None and bug._timer.interval == 2.5
    bug.hide()


def test_hide_clears_properties_and_cancels_timer():
    import xbmcgui
    from channel_bug import ChannelBug

    bug = ChannelBug("ChannelBug.xml", "/addon", "default", "1080i")
    bug.show_text("7_", "", 5)
    bug.hide()
    assert bug._timer is None
    assert xbmcgui.Window(10000).getProperty("ClassicTV.ChannelNumber") == ""
    assert xbmcgui.Window(10000).getProperty("ClassicTV.OSDBadge") == ""


class _Action:
    def __init__(self, action_id):
        self._id = action_id

    def getId(self):
        return self._id


def _bug_with_keys():
    from channel_bug import ChannelBug

    got = []
    bug = ChannelBug("ChannelBug.xml", "/addon", "default", "1080i")
    bug.on_key = got.append
    return bug, got


def test_dialog_forwards_digit_and_channel_actions():
    import time

    bug, got = _bug_with_keys()
    for aid in (59, 60, 5, 6):  # NUMBER_1, NUMBER_2, PAGE_UP, PAGE_DOWN
        bug.onAction(_Action(aid))
    time.sleep(0.2)
    assert sorted(got) == sorted(["num_1", "num_2", "ch_up", "ch_down"])


def test_dialog_forwards_back_and_arrow_actions():
    import time

    bug, got = _bug_with_keys()
    for aid in (10, 92, 110, 1, 2, 3, 4):
        bug.onAction(_Action(aid))
    time.sleep(0.2)
    assert sorted(got) == sorted(["back", "back", "back", "left", "right", "up", "down"])


def test_dialog_ignores_other_actions():
    import time

    bug, got = _bug_with_keys()
    bug.onAction(_Action(88))  # ACTION_VOLUME_UP — not mapped to ClassicTV method
    time.sleep(0.1)
    assert got == []


def test_dialog_without_on_key_is_safe():
    from channel_bug import ChannelBug

    ChannelBug("ChannelBug.xml", "/addon", "default", "1080i").onAction(_Action(59))


def test_toast_display_uses_notification_not_a_dialog(monkeypatch):
    import channel_bug

    calls = []

    class _FakeDialog:
        def notification(self, *args):
            calls.append(args)

    monkeypatch.setattr(channel_bug.xbmcgui, "Dialog", _FakeDialog)
    channel_bug.ToastDisplay().show_text("12_", "", 4.5)
    assert calls and calls[0][1] == "12_"
    assert calls[0][3] <= 2000


# ---------------------------------------------------------------------------
# CRT Static snow (show_static / hide_static)
# ---------------------------------------------------------------------------


def test_show_static_sets_static_and_channel_properties(bug):
    import xbmcgui

    bug.show_static(4, "Cartoon Network", max_seconds=1.0)
    w = xbmcgui.Window(10000)
    assert w.getProperty("ClassicTV.StaticVisible") == "1"
    assert w.getProperty("ClassicTV.StaticFrame") in ("0", "1", "2", "3")
    assert w.getProperty("ClassicTV.ChannelNumber") == "4"
    assert w.getProperty("ClassicTV.ChannelName") == "Cartoon Network"
    bug.hide_static()
    assert w.getProperty("ClassicTV.StaticVisible") == ""
    assert w.getProperty("ClassicTV.StaticFrame") == ""


def test_show_static_cycles_frames_and_auto_clears(bug):
    import time
    import xbmcgui

    bug.show_static(2, "Anime", max_seconds=0.15)
    w = xbmcgui.Window(10000)
    assert w.getProperty("ClassicTV.StaticVisible") == "1"
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline and w.getProperty("ClassicTV.StaticVisible"):
        time.sleep(0.02)
    assert w.getProperty("ClassicTV.StaticVisible") == ""
    assert w.getProperty("ClassicTV.StaticFrame") == ""


def test_dialog_forwards_confirm_and_guide_actions():
    import time

    bug, got = _bug_with_keys()
    for aid in (7, 18):  # ACTION_SELECT_ITEM, ACTION_SHOW_GUI
        bug.onAction(_Action(aid))
    time.sleep(0.1)
    assert sorted(got) == sorted(["confirm", "guide"])


def test_show_for_formats_trinitron_channel_display_and_persists_active_channel(bug):
    import xbmcgui

    bug.show_for(4, "Nickelodeon Cartoons", title="Rugrats")
    w = xbmcgui.Window(10000)
    assert w.getProperty("ClassicTV.ChannelNumber") == "4"
    assert w.getProperty("ClassicTV.ChannelDisplay") == "CH 04"
    assert w.getProperty("ClassicTV.ChannelName") == "Nickelodeon Cartoons"
    assert w.getProperty("ClassicTV.NowPlaying") == "Rugrats"
    assert w.getProperty("ClassicTV.ActiveChannelNumber") == "4"
    assert w.getProperty("ClassicTV.ActiveChannelName") == "Nickelodeon Cartoons"

    # When the 10s bug times out, transient properties clear while ActiveChannel* persist.
    bug._on_timeout()
    assert w.getProperty("ClassicTV.BugVisible") == ""
    assert w.getProperty("ClassicTV.ChannelNumber") == ""
    assert w.getProperty("ClassicTV.ChannelDisplay") == ""
    assert w.getProperty("ClassicTV.NowPlaying") == ""
    assert w.getProperty("ClassicTV.ActiveChannelNumber") == "4"
    assert w.getProperty("ClassicTV.ActiveChannelName") == "Nickelodeon Cartoons"


def test_hide_static_holds_until_post_av_and_min_seconds_elapse(bug):
    import time
    import xbmcgui

    bug.show_static(
        3,
        "Disney Cartoons",
        max_seconds=2.0,
        min_seconds=0.12,
        post_av_seconds=0.10,
    )
    w = xbmcgui.Window(10000)
    assert w.getProperty("ClassicTV.StaticVisible") == "1"

    # Calling hide_static() immediately (as onAVStarted does) must hold static
    # for the post-AV / min-seconds settle window before clearing StaticVisible.
    bug.hide_static()
    assert w.getProperty("ClassicTV.StaticVisible") == "1"

    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline and w.getProperty("ClassicTV.StaticVisible"):
        time.sleep(0.02)
    assert w.getProperty("ClassicTV.StaticVisible") == ""


def test_dialog_forwards_jump_sms_digits_2_through_9():
    import time

    bug, got = _bug_with_keys()
    # Global remote.xml maps 2..9 to JumpSMS2..9 (action IDs 142..149) when
    # ChannelBug is open outside FullscreenVideo.
    for aid in range(142, 150):
        bug.onAction(_Action(aid))
    time.sleep(0.1)
    assert sorted(got) == [f"num_{n}" for n in range(2, 10)]


