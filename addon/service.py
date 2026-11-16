"""
Classic TV 3.0 — Kodi addon entry point.

This module is the top-level service script that Kodi executes when the
addon starts. It owns the full startup sequence, wires all collaborators
together, registers the NotifyAll monitor, and runs the main event loop.

Responsibilities (Requirements 6.1–6.4, 15.1–15.4, 15.7):
  - Load settings from xbmcaddon.
  - Construct and wire all modules: ClassicTVPlayer, ModeState, ChannelList,
    TVGuide, ChannelEntry, MediaCache.
  - Enter TV_Mode and begin playback of the default channel with retry.
  - Dispatch xbmc.Monitor.onNotification callbacks to module handlers with
    no blocking I/O in the dispatch path.
  - Run waitForAbort() loop until Kodi signals shutdown.

All log lines prefixed [ClassicTV][service].
"""

import sys
import os
import time

import xbmc
import xbmcgui
import xbmcaddon

# ---------------------------------------------------------------------------
# Addon module path injection
# Must happen before importing addon-local modules so the interpreter can
# resolve them without Kodi's built-in path manipulation.
# ---------------------------------------------------------------------------

_ADDON_DIR = os.path.dirname(os.path.abspath(__file__))
_LIB_DIR = os.path.join(_ADDON_DIR, "resources", "lib")

for _path in (_LIB_DIR, _ADDON_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from channels import ChannelList
from channel_entry import ChannelEntry
from constants import STARTUP_RETRY_MAX, STARTUP_RETRY_INTERVAL_SECONDS
from guide import TVGuide
from media_cache import MediaCache
from modes import ModeState
from player import ClassicTVPlayer

_LOG_PREFIX = "[ClassicTV][service]"


# ---------------------------------------------------------------------------
# NotifyAll monitor
# ---------------------------------------------------------------------------

class ClassicTVMonitor(xbmc.Monitor):
    """Receives xbmc.Monitor.onNotification callbacks from the remote.

    All dispatch is done with a simple dict lookup + direct method call so
    the handler returns in well under 1ms (Requirements 15.1–15.4).  No
    blocking I/O lives in this path.

    Recognised senders:
        classictv.service — internal NotifyAll messages from keymaps.

    Recognised methods (Requirements 15.2, 15.3):
        ch_up, ch_down         — channel navigation
        guide                  — TV Guide toggle
        num_0 – num_9          — direct digit channel entry
        confirm                — confirm accumulated digit entry
        back                   — cancel accumulated digit entry
        up / down / left / right — guide navigation (logged, no-op for now)

    Any other method while channel_entry is accumulating is routed to
    channel_entry.handle_other() so the state machine can reset.
    """

    def __init__(self, player, modes, guide, channel_entry):
        super().__init__()
        self._player = player
        self._modes = modes
        self._guide = guide
        self._channel_entry = channel_entry

    def onNotification(self, sender: str, method: str, data: str) -> None:  # noqa: N802
        """Dispatch a NotifyAll message to the appropriate handler.

        Requirements 15.1–15.4: route within 1ms, no blocking I/O.
        """
        if sender != "classictv.service":
            return

        # Req 46: JSONRPC.NotifyAll prefixes method with "Other." (e.g. "Other.ch_up").
        # The keymap NotifyAll() builtin sends bare names (e.g. "ch_up").
        # Strip the prefix so both paths reach the same dispatch logic.
        method = method.removeprefix("Other.")

        xbmc.log(
            f"{_LOG_PREFIX} onNotification sender={sender!r}"
            f" method={method!r} data={data!r}",
            xbmc.LOGDEBUG,
        )

        # --- channel navigation ------------------------------------------
        if method == "ch_up":
            self._player.handle_ch_up()
            return

        if method == "ch_down":
            self._player.handle_ch_down()
            return

        # --- TV Guide ----------------------------------------------------
        if method == "guide":
            self._guide.toggle()
            return

        # --- direct digit entry ------------------------------------------
        if method in (
            "num_0", "num_1", "num_2", "num_3", "num_4",
            "num_5", "num_6", "num_7", "num_8", "num_9",
        ):
            digit = int(method[-1])
            self._channel_entry.handle_digit(digit)
            return

        if method == "confirm":
            self._channel_entry.handle_confirm()
            return

        if method == "back":
            self._channel_entry.handle_back()
            return

        # --- guide navigation (logged only) ------------------------------
        if method in ("up", "down", "left", "right"):
            xbmc.log(
                f"{_LOG_PREFIX} guide navigation: {method}",
                xbmc.LOGDEBUG,
            )
            return

        # --- anything else while accumulating ----------------------------
        if self._channel_entry.is_accumulating():
            xbmc.log(
                f"{_LOG_PREFIX} unrecognised method={method!r}"
                f" while accumulating — routing to channel_entry.handle_other",
                xbmc.LOGDEBUG,
            )
            self._channel_entry.handle_other(method)
            return

        xbmc.log(
            f"{_LOG_PREFIX} unhandled notification method={method!r}",
            xbmc.LOGDEBUG,
        )


# ---------------------------------------------------------------------------
# Startup and main loop
# ---------------------------------------------------------------------------

def run() -> None:
    """Full addon startup sequence (Requirements 6.1–6.4, 15.7).

    Steps:
      1. Load settings from xbmcaddon (Req 6.1).
      2. Construct all module instances (Req 6.2).
      3. Wire collaborator references (Req 6.2).
      4. Determine startup channel; enter TV_Mode (Req 6.3).
      5. Register ClassicTVMonitor.
      6. Fetch schedule with retry; begin playback (Req 6.4).
      7. Enter waitForAbort() main loop (Req 15.7).
    """

    # ------------------------------------------------------------------
    # Step 1: Load settings
    # ------------------------------------------------------------------
    addon = xbmcaddon.Addon()

    schedule_service_url = (
        addon.getSetting("schedule_service_url") or "http://192.168.1.207:8300"
    )
    default_channel = addon.getSetting("default_channel") or ""
    jellyfin_base_url = (
        addon.getSetting("jellyfin_base_url") or "http://192.168.1.207:8096"
    )
    offline_clip_url = addon.getSetting("offline_clip_url") or ""

    xbmc.log(
        f"{_LOG_PREFIX} starting"
        f" schedule_service_url={schedule_service_url!r}"
        f" default_channel={default_channel!r}"
        f" jellyfin_base_url={jellyfin_base_url!r}",
        xbmc.LOGINFO,
    )

    # Read deploy version from VERSION file written by deploy-sweet-potato.sh.
    # This is the first thing that appears in the log — grep for it to confirm
    # which deploy is running.
    _addon_version_file = os.path.join(
        xbmcaddon.Addon().getAddonInfo("path"), "VERSION"
    )
    try:
        with open(_addon_version_file) as _vf:
            _deploy_version = _vf.read().strip()
    except OSError:
        _deploy_version = "dev"
    xbmc.log(
        f"{_LOG_PREFIX} deploy version={_deploy_version!r}",
        xbmc.LOGINFO,
    )

    # ------------------------------------------------------------------
    # Step 2 & 3: Construct and wire modules
    # ------------------------------------------------------------------

    # Player
    player = ClassicTVPlayer()
    player._schedule_service_url = schedule_service_url
    player._offline_clip_url = offline_clip_url

    # Mode state machine
    modes = ModeState()
    modes.player = player
    modes._schedule_service_url = schedule_service_url
    modes._offline_clip_url = offline_clip_url
    modes._jellyfin_base_url = jellyfin_base_url

    # Channel list (fetched from /guide at construction)
    channel_list = ChannelList(schedule_service_url)
    player._channel_list = channel_list
    player._modes = modes

    # Media cache (prefetch SD card store)
    media_cache = MediaCache(player=player)
    player._media_cache = media_cache

    # TV Guide overlay
    guide = TVGuide(
        schedule_service_url,
        channel_change_callback=lambda cid: player._change_channel(cid),
    )

    # Direct digit channel entry state machine
    channel_entry = ChannelEntry(
        channel_list,
        on_channel_change=lambda ch: player._change_channel(ch.id),
    )

    # Wire the watchdog trigger so ModeState can restart playback
    modes._on_watchdog_trigger = lambda: _watchdog_restart(player)

    xbmc.log(f"{_LOG_PREFIX} all modules initialised", xbmc.LOGINFO)

    # ------------------------------------------------------------------
    # Start media cache prefetch thread
    # ------------------------------------------------------------------
    channel_ids = [ch.id for ch in channel_list.all_channels()]
    if channel_ids:
        media_cache.start_prefetch_thread(schedule_service_url, channel_ids)
        xbmc.log(
            f"{_LOG_PREFIX} prefetch thread started for {len(channel_ids)} channel(s)",
            xbmc.LOGINFO,
        )

    # ------------------------------------------------------------------
    # Step 4: Determine startup channel and enter TV_Mode
    # ------------------------------------------------------------------
    all_channels = channel_list.all_channels()
    if default_channel:
        startup_channel_id = default_channel
    elif all_channels:
        startup_channel_id = all_channels[0].id
    else:
        startup_channel_id = ""

    xbmc.log(
        f"{_LOG_PREFIX} entering TV_Mode channel={startup_channel_id!r}",
        xbmc.LOGINFO,
    )
    modes.transition_to_tv(startup_channel_id)

    # ------------------------------------------------------------------
    # Step 5: Register the NotifyAll monitor
    # ------------------------------------------------------------------
    monitor = ClassicTVMonitor(player, modes, guide, channel_entry)

    # ------------------------------------------------------------------
    # Step 6: Startup playback with retry (Req 6.4)
    # ------------------------------------------------------------------
    started = False

    if not startup_channel_id:
        xbmc.log(
            f"{_LOG_PREFIX} no startup channel available — skipping initial playback",
            xbmc.LOGWARNING,
        )
    else:
        for attempt in range(1, STARTUP_RETRY_MAX + 1):
            xbmc.log(
                f"{_LOG_PREFIX} startup schedule fetch"
                f" attempt {attempt}/{STARTUP_RETRY_MAX}"
                f" channel={startup_channel_id!r}",
                xbmc.LOGINFO,
            )
            t = int(time.time())
            response = player._fetch_schedule(startup_channel_id, t)
            if response is not None:
                xbmc.log(
                    f"{_LOG_PREFIX} startup schedule fetch succeeded"
                    f" (attempt {attempt}) — starting playback",
                    xbmc.LOGINFO,
                )
                player.play_schedule_item(response)
                started = True
                break

            if attempt < STARTUP_RETRY_MAX:
                xbmc.log(
                    f"{_LOG_PREFIX} startup fetch attempt {attempt} failed;"
                    f" retrying in {STARTUP_RETRY_INTERVAL_SECONDS}s",
                    xbmc.LOGWARNING,
                )
                xbmc.sleep(STARTUP_RETRY_INTERVAL_SECONDS * 1000)

        if not started:
            xbmc.log(
                f"{_LOG_PREFIX} all {STARTUP_RETRY_MAX} startup fetch attempts failed;"
                f" showing error dialog",
                xbmc.LOGERROR,
            )
            xbmcgui.Dialog().ok(
                "Classic TV",
                "Schedule_Service unreachable. Please restart.",
            )
            return

    # ------------------------------------------------------------------
    # Step 7: IR event logger (diagnostic — runs only on CoreELEC)
    #
    # Reads raw Linux input events from the meson-ir device and logs
    # every button press at DEBUG level. This eliminates the need for a
    # timed 'push buttons now' test window — every press appears in the
    # log permanently, regardless of when it happens.
    #
    # Event format (32-bit ARM): struct input_event
    #   tv_sec   (u32) + tv_usec  (u32)  — 8 bytes
    #   type     (u16)                    — 2 bytes
    #   code     (u16)                    — 2 bytes
    #   value    (s32)                    — 4 bytes
    #   total: 16 bytes
    #
    # type=EV_MSC(4) code=MSC_SCAN(4) carries the raw scancode.
    # type=EV_KEY(1) carries the translated KEY_* code and press/release.
    # ------------------------------------------------------------------
    import struct
    import threading

    IR_DEVICE = "/dev/input/event2"
    INPUT_EVENT_FMT = "IIHHi"   # u32 u32 u16 u16 s32
    INPUT_EVENT_SIZE = struct.calcsize(INPUT_EVENT_FMT)
    EV_KEY = 1
    EV_MSC = 4

    def _ir_logger_loop(stop_event: threading.Event) -> None:
        if not os.path.exists(IR_DEVICE):
            xbmc.log(
                f"{_LOG_PREFIX} IR logger: {IR_DEVICE} not found — skipping"
                f" (not CoreELEC?)",
                xbmc.LOGINFO,
            )
            return

        # Also check /dev/lirc0 for raw pulse data — this tells us whether
        # the hardware is receiving any IR signal at all, regardless of
        # whether the protocol decoder recognises it.
        lirc_device = "/dev/lirc0"
        has_lirc = os.path.exists(lirc_device)

        if has_lirc:
            # Start a second thread to watch raw LIRC pulses
            def _lirc_loop() -> None:
                try:
                    # LIRC mode2 format: each pulse/space is 4 bytes (u32)
                    # bit 24 set = pulse, clear = space; lower 24 bits = duration µs
                    import fcntl
                    LIRC_SET_REC_MODE = 0x40046911  # _IOW('i', 0x11, __u32)
                    LIRC_MODE_MODE2 = 0x00000004
                    with open(lirc_device, "rb") as lf:
                        try:
                            fcntl.ioctl(lf, LIRC_SET_REC_MODE, LIRC_MODE_MODE2)
                        except Exception:  # noqa: BLE001, S110
                            pass  # already in mode2 or not supported
                        xbmc.log(
                            f"{_LOG_PREFIX} IR logger: also listening on {lirc_device} (raw pulses)",
                            xbmc.LOGINFO,
                        )
                        while not stop_event.is_set():
                            raw = lf.read(4)
                            if len(raw) < 4:
                                break
                            val = struct.unpack("I", raw)[0]
                            is_pulse = bool(val & 0x01000000)
                            duration_us = val & 0x00FFFFFF
                            xbmc.log(
                                f"{_LOG_PREFIX} IR lirc:"
                                f" {'pulse' if is_pulse else 'space'}"
                                f" {duration_us}µs",
                                xbmc.LOGDEBUG,
                            )
                except Exception as exc:  # noqa: BLE001, S110
                    xbmc.log(
                        f"{_LOG_PREFIX} IR lirc logger error: {exc}",
                        xbmc.LOGWARNING,
                    )

            _lirc_thread = threading.Thread(
                target=_lirc_loop, daemon=True, name="ir-lirc-logger"
            )
            _lirc_thread.start()

        try:
            with open(IR_DEVICE, "rb") as f:
                xbmc.log(
                    f"{_LOG_PREFIX} IR logger: listening on {IR_DEVICE}",
                    xbmc.LOGINFO,
                )
                while not stop_event.is_set():
                    raw = f.read(INPUT_EVENT_SIZE)
                    if len(raw) < INPUT_EVENT_SIZE:
                        break
                    _tv_sec, _tv_usec, ev_type, code, value = struct.unpack(
                        INPUT_EVENT_FMT, raw
                    )
                    if ev_type == EV_MSC:
                        xbmc.log(
                            f"{_LOG_PREFIX} IR raw scancode=0x{code:04x}"
                            f" value=0x{value:08x}",
                            xbmc.LOGDEBUG,
                        )
                    elif ev_type == EV_KEY and value in (0, 1, 2):
                        action = {0: "release", 1: "press", 2: "repeat"}[value]
                        xbmc.log(
                            f"{_LOG_PREFIX} IR key code={code} ({action})",
                            xbmc.LOGDEBUG,
                        )
        except Exception as exc:  # noqa: BLE001, S110
            xbmc.log(
                f"{_LOG_PREFIX} IR logger error: {exc}",
                xbmc.LOGWARNING,
            )

    _ir_stop = threading.Event()
    _ir_thread = threading.Thread(
        target=_ir_logger_loop, args=(_ir_stop,), daemon=True, name="ir-logger"
    )
    _ir_thread.start()

    # ------------------------------------------------------------------
    # Step 8: Main event loop (Req 15.7)
    # Blocks here until Kodi signals shutdown (waitForAbort returns True).
    # ------------------------------------------------------------------
    xbmc.log(f"{_LOG_PREFIX} entering main event loop", xbmc.LOGINFO)

    while not monitor.waitForAbort(1):
        pass

    _ir_stop.set()
    xbmc.log(f"{_LOG_PREFIX} abort requested — shutting down", xbmc.LOGINFO)


# ---------------------------------------------------------------------------
# Watchdog restart helper
# ---------------------------------------------------------------------------

def _watchdog_restart(player: "ClassicTVPlayer") -> None:
    """Callback invoked by ModeState's watchdog when playback stops unexpectedly.

    Fetches a fresh schedule for the current channel and resumes playback.
    Runs on the watchdog's daemon thread, so it must not block indefinitely.
    """
    channel_id = player._current_channel_id
    if not channel_id:
        xbmc.log(
            f"{_LOG_PREFIX} watchdog_restart: no current channel — skipping",
            xbmc.LOGWARNING,
        )
        return

    xbmc.log(
        f"{_LOG_PREFIX} watchdog_restart: fetching schedule"
        f" channel={channel_id!r}",
        xbmc.LOGINFO,
    )
    t = int(time.time())
    response = player._fetch_schedule(channel_id, t)
    if response is not None:
        player.play_schedule_item(response)
    else:
        xbmc.log(
            f"{_LOG_PREFIX} watchdog_restart: schedule fetch failed"
            f" channel={channel_id!r}",
            xbmc.LOGERROR,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run()
