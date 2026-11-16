"""
Classic TV 4.0 — Kodi addon entry point.

Startup sequence:
  1. Load settings (jellyfin_base_url, default_channel).
  2. Read Jellyfin server ID from the Jellyfin addon's data.json.
  3. Construct ScheduleClient, ChannelList, ModeState, ClassicTVPlayer.
  4. Wire collaborator references.
  5. Determine startup channel; enter TV_MODE.
  6. Start command worker thread (non-blocking channel changes).
  7. Register ClassicTVMonitor (xbmc.Monitor subclass).
  8. Startup playback with retry.
  9. IR logger thread (diagnostic).
 10. Main waitForAbort() loop.
 11. Shutdown: stop command worker, stop modes (watchdog), log exit.

All log lines prefixed [ClassicTV][service].
"""

from __future__ import annotations

import os
import queue
import struct
import sys
import threading

import xbmc
import xbmcaddon
import xbmcgui

# ---------------------------------------------------------------------------
# Path injection — must happen before any addon-local imports.
# ---------------------------------------------------------------------------

_ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB_DIR = os.path.join(_ADDON_DIR, "resources", "lib")

for _path in (_LIB_DIR, _ADDON_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from browse_controls import BROWSE_KEYS, BrowseControls
from channel_bug import ToastDisplay, create_channel_bug
from channels import ChannelList
from channel_entry import ChannelEntry
from jellyfin_server import get_server_id
from modes import ModeState
from player import ClassicTVPlayer
from schedule import ScheduleClient

_LOG_PREFIX = "[ClassicTV][service]"


# ---------------------------------------------------------------------------
# NotifyAll monitor
# ---------------------------------------------------------------------------


class ClassicTVMonitor(xbmc.Monitor):
    """Receives xbmc.Monitor.onNotification callbacks.

    Channel changes (ch_up, ch_down) are dispatched directly on the
    notification thread. They block briefly (decoder poll + schedule fetch)
    but this is acceptable — the previous command queue approach silently
    dropped rapid button presses which made channel surfing unreliable.

    Digit entry methods remain synchronous (fast timer operations, no I/O).

    Recognised sender: classictv.service (matches addon id in addon.xml)
    JSONRPC.NotifyAll prefixes method with "Other." — stripped on entry.
    """

    def __init__(self, player, channel_entry, command_queue: "queue.Queue", controls=None):
        super().__init__()
        self._controls = controls if controls is not None else BrowseControls()
        self._player = player
        self._channel_entry = channel_entry
        self._command_queue = command_queue  # kept for future use

    def onNotification(self, sender: str, method: str, data: str) -> None:  # noqa: N802
        if sender != "classictv.service":
            return

        method = method.removeprefix("Other.")

        self.dispatch(method)

    def dispatch(self, method: str) -> None:
        """Handle a Classic TV key/command by name (from NotifyAll or a dialog)."""
        xbmc.log(
            f"{_LOG_PREFIX} onNotification method={method!r}",
            xbmc.LOGDEBUG,
        )

        # Browse mode: OK and the d-pad drive pause / seek / OSD / subtitles for
        # whatever the viewer is watching, instead of Classic TV channel entry.
        if method in BROWSE_KEYS and self._player.is_user_browsing():
            if self._controls.handle(method):
                return

        # Channel changes — direct dispatch. The queue approach was dropped
        # because maxsize=1 silently dropped rapid ch+/ch- presses, making
        # channel surfing unreliable (half the presses did nothing).
        if method == "ch_up":
            self._player.handle_ch_up()
        elif method == "ch_down":
            self._player.handle_ch_down()

        # Digit entry is fast (timer manipulation only) — synchronous.
        elif method in (
            "num_0",
            "num_1",
            "num_2",
            "num_3",
            "num_4",
            "num_5",
            "num_6",
            "num_7",
            "num_8",
            "num_9",
        ):
            # Channel input only means something while Classic TV owns the player.
            if self._player.is_user_browsing():
                return
            self._channel_entry.handle_digit(int(method[-1]))
        elif method == "confirm":
            self._channel_entry.handle_confirm()
        elif method == "back":
            if self._channel_entry.is_accumulating():
                self._channel_entry.handle_back()
            else:
                xbmc.executebuiltin("Action(Back)")
        elif method == "guide":
            # EPG is the global mode toggle. While the viewer is browsing it
            # returns to Classic TV; while watching TV it stops playback (waiting
            # for the decoder to release) and steps aside to Home.
            if self._player.is_user_browsing():
                self._player.resume_from_browse()
            else:
                self._player.enter_browse()
        elif method in ("up", "down", "left", "right"):
            pass  # guide navigation — not implemented in 4.0
        else:
            if self._channel_entry.is_accumulating():
                self._channel_entry.handle_other(method)


# ---------------------------------------------------------------------------
# Command worker
# ---------------------------------------------------------------------------


def _start_command_worker(
    cmd_queue: "queue.Queue",
    stop_event: threading.Event,
) -> threading.Thread:
    """Start the command worker thread that drains cmd_queue.

    Channel changes and other blocking operations are enqueued by
    ClassicTVMonitor and executed here, keeping onNotification non-blocking.
    Stops when stop_event is set (addon shutdown).
    """

    def _loop() -> None:
        xbmc.log(f"{_LOG_PREFIX} command worker started", xbmc.LOGDEBUG)
        while not stop_event.is_set():
            try:
                cmd = cmd_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                cmd()
            except Exception as exc:
                xbmc.log(
                    f"{_LOG_PREFIX} command worker: unhandled exception: {exc}",
                    xbmc.LOGERROR,
                )
        xbmc.log(f"{_LOG_PREFIX} command worker stopped", xbmc.LOGDEBUG)

    t = threading.Thread(target=_loop, daemon=True, name="classictv-cmd-worker")
    t.start()
    return t


# ---------------------------------------------------------------------------
# IR logger (diagnostic)
# ---------------------------------------------------------------------------


def _start_ir_logger(stop_event: threading.Event) -> None:
    """Spawn a daemon thread that logs raw IR input events from /dev/input."""
    IR_DEVICE = "/dev/input/event2"
    INPUT_EVENT_FMT = "IIHHi"
    INPUT_EVENT_SIZE = struct.calcsize(INPUT_EVENT_FMT)
    EV_KEY = 1
    EV_MSC = 4

    def _loop() -> None:
        if not os.path.exists(IR_DEVICE):
            return
        try:
            with open(IR_DEVICE, "rb") as dev:
                while not stop_event.is_set():
                    raw = dev.read(INPUT_EVENT_SIZE)
                    if len(raw) < INPUT_EVENT_SIZE:
                        break
                    _, _, ev_type, code, value = struct.unpack(INPUT_EVENT_FMT, raw)
                    if ev_type == EV_KEY and value in (0, 1):
                        action = "press" if value == 1 else "release"
                        xbmc.log(
                            f"{_LOG_PREFIX} IR KEY type={ev_type}"
                            f" code={code:#06x} {action}",
                            xbmc.LOGDEBUG,
                        )
                    elif ev_type == EV_MSC:
                        xbmc.log(
                            f"{_LOG_PREFIX} IR MSC scancode={code:#010x}",
                            xbmc.LOGDEBUG,
                        )
        except OSError as exc:
            xbmc.log(f"{_LOG_PREFIX} IR logger stopped: {exc}", xbmc.LOGDEBUG)

    t = threading.Thread(target=_loop, daemon=True, name="classictv-ir-logger")
    t.start()


# ---------------------------------------------------------------------------
# Startup and main loop
# ---------------------------------------------------------------------------


def _select_startup_channel(
    all_channels,
    last_channel: str,
    default_channel: str,
) -> str:
    """Pick the startup channel: last opened, then configured default, then first."""
    available_ids = {ch.id for ch in all_channels}
    if last_channel and (not available_ids or last_channel in available_ids):
        return last_channel
    if default_channel and (not available_ids or default_channel in available_ids):
        return default_channel
    if all_channels:
        return all_channels[0].id
    return ""


def run() -> None:
    """Full addon startup sequence."""

    # ------------------------------------------------------------------
    # 1. Load settings
    # ------------------------------------------------------------------
    addon = xbmcaddon.Addon()
    jellyfin_base_url = (
        addon.getSetting("jellyfin_base_url") or "http://192.168.1.207:8096"
    )
    default_channel = addon.getSetting("default_channel") or ""
    last_channel = addon.getSetting("last_channel") or ""

    xbmc.log(
        f"{_LOG_PREFIX} starting"
        f" jellyfin_base_url={jellyfin_base_url!r}"
        f" default_channel={default_channel!r}",
        xbmc.LOGINFO,
    )

    # ------------------------------------------------------------------
    # 2. Read Jellyfin server ID
    # ------------------------------------------------------------------
    server_id = get_server_id()
    if not server_id:
        xbmc.log(
            f"{_LOG_PREFIX} could not read Jellyfin server ID"
            " — playback will use empty server parameter",
            xbmc.LOGWARNING,
        )

    # ------------------------------------------------------------------
    # 3. Construct modules
    # ------------------------------------------------------------------
    schedule_client = ScheduleClient(jellyfin_base_url)
    channel_list = ChannelList(schedule_client)
    player = ClassicTVPlayer()
    modes = ModeState()

    # ------------------------------------------------------------------
    # 4. Wire collaborators
    # ------------------------------------------------------------------
    player._base_url = jellyfin_base_url
    player._jellyfin_server_id = server_id
    player._schedule_client = schedule_client
    player._channel_list = channel_list
    player._modes = modes
    modes.player = player

    try:
        addon_path = xbmcaddon.Addon().getAddonInfo("path")
        player._channel_bug = create_channel_bug(addon_path)
    except Exception as exc:
        xbmc.log(f"{_LOG_PREFIX} ChannelBug setup failed: {exc}", xbmc.LOGWARNING)
        player._channel_bug = None

    channel_entry = ChannelEntry(
        channel_list,
        on_channel_change=lambda ch: player._change_channel(ch.id),
        display=player._channel_bug or ToastDisplay(),
    )

    # ------------------------------------------------------------------
    # 5. Determine startup channel and enter TV_MODE
    # ------------------------------------------------------------------
    all_channels = channel_list.all_channels()
    startup_channel_id = _select_startup_channel(
        all_channels, last_channel, default_channel
    )

    if startup_channel_id:
        modes.transition_to_tv(startup_channel_id)
        player._current_channel_id = startup_channel_id

    # ------------------------------------------------------------------
    # 6. Start command worker (non-blocking channel changes)
    # ------------------------------------------------------------------
    _cmd_queue: queue.Queue = queue.Queue(maxsize=1)
    _cmd_stop = threading.Event()
    _start_command_worker(_cmd_queue, _cmd_stop)

    # ------------------------------------------------------------------
    # 7. Register monitor
    # ------------------------------------------------------------------
    monitor = ClassicTVMonitor(player, channel_entry, _cmd_queue)
    if player._channel_bug is not None:
        player._channel_bug.on_key = monitor.dispatch

    # ------------------------------------------------------------------
    # 8. Startup playback with retry
    # ------------------------------------------------------------------
    if not startup_channel_id:
        xbmc.log(
            f"{_LOG_PREFIX} no channels available — skipping startup playback",
            xbmc.LOGWARNING,
        )
    else:
        started = player.startup_play(startup_channel_id)
        if not started:
            xbmcgui.Dialog().ok(
                "Classic TV",
                "Could not connect to the ClassicTV schedule service."
                " Please check the Jellyfin server and restart.",
            )
            modes.stop()
            return

    # ------------------------------------------------------------------
    # 10. IR logger thread
    # ------------------------------------------------------------------
    _ir_stop = threading.Event()
    _start_ir_logger(_ir_stop)

    # ------------------------------------------------------------------
    # 11. Main event loop
    # ------------------------------------------------------------------
    xbmc.log(f"{_LOG_PREFIX} entering main loop", xbmc.LOGINFO)
    while not monitor.waitForAbort(1):
        pass

    # ------------------------------------------------------------------
    # 12. Shutdown
    # ------------------------------------------------------------------
    xbmc.log(f"{_LOG_PREFIX} shutting down", xbmc.LOGINFO)
    _ir_stop.set()
    _cmd_stop.set()
    modes.stop()


if __name__ == "__main__":
    run()
