"""
Kodi module stubs for the Classic TV 3.0 test suite.

Kodi ships its Python bindings as compiled C extensions inside the Kodi
process. They do not exist as installable packages — running the addon
outside Kodi immediately raises ImportError for xbmc, xbmcgui, xbmcaddon,
and xbmcvfs. This conftest injects minimal pure-Python stubs into
sys.modules *before* any addon import so that every test module can import
addon code without Kodi being installed (Requirement 31.1).

Stub design principles:
- Classes and methods that addon modules reference must exist and have
  compatible signatures, but they do not need real implementations.
- Where a method is called in production code and its return value is
  inspected (e.g. isPlaying, getTime), the stub returns a sensible default
  so tests can override it with monkeypatch or subclassing.
- Side-effect methods (play, stop, seekTime, etc.) are no-ops by default.
- Constants used by addon code (ACTION_*, KEY_*, LOG_*) are defined here.
"""

import sys
import types
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_module(name: str) -> types.ModuleType:
    """Create an empty module and register it in sys.modules."""
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


# ---------------------------------------------------------------------------
# xbmc
# ---------------------------------------------------------------------------

xbmc_mod = _make_module("xbmc")

# Log level constants
xbmc_mod.LOGDEBUG = 0
xbmc_mod.LOGINFO = 1
xbmc_mod.LOGWARNING = 2
xbmc_mod.LOGERROR = 3
xbmc_mod.LOGFATAL = 4

# Action constants used by onAction handlers
xbmc_mod.ACTION_MOVE_UP = 3
xbmc_mod.ACTION_MOVE_DOWN = 4
xbmc_mod.ACTION_MOVE_LEFT = 1
xbmc_mod.ACTION_MOVE_RIGHT = 2
xbmc_mod.ACTION_SELECT_ITEM = 7
xbmc_mod.ACTION_PREVIOUS_MENU = 10
xbmc_mod.ACTION_NAV_BACK = 92

# Condition identifiers (used by getCondVisibility)
xbmc_mod.PLAYER_PLAYING = "Player.Playing"


def _log(message: str, level: int = 1) -> None:
    """No-op log stub. Tests that need to inspect log output should
    monkeypatch this or use capfd/caplog."""
    pass


def _sleep(ms: int) -> None:
    """Stub for xbmc.sleep(ms). No-op — tests should not rely on real delays."""
    pass


def _get_cond_visibility(condition: str) -> bool:
    return False


def _get_info_label(label: str) -> str:
    return ""


class _Monitor:
    """Stub for xbmc.Monitor."""

    def waitForAbort(self, timeout: float = 0) -> bool:
        """Returns False (abort not requested). Override in tests as needed."""
        return False

    def abortRequested(self) -> bool:
        return False

    def onNotification(self, sender: str, method: str, data: str) -> None:
        pass

    def onSettingsChanged(self) -> None:
        pass


class _Player:
    """Stub for xbmc.Player."""

    def __init__(self) -> None:
        self._playing = False
        self._current_time = 0.0
        self._total_time = 0.0

    def play(self, item=None, listitem=None, windowed: bool = False, startpos: int = -1) -> None:
        self._playing = True

    def stop(self) -> None:
        self._playing = False

    def pause(self) -> None:
        pass

    def isPlaying(self) -> bool:
        return self._playing

    def isPlayingVideo(self) -> bool:
        return self._playing

    def isPlayingAudio(self) -> bool:
        return False

    def getTime(self) -> float:
        return self._current_time

    def getTotalTime(self) -> float:
        return self._total_time

    def seekTime(self, seek_time: float) -> None:
        self._current_time = seek_time

    def getPlayingFile(self) -> str:
        return ""

    def onPlayBackStarted(self) -> None:
        pass

    def onPlayBackEnded(self) -> None:
        pass

    def onPlayBackStopped(self) -> None:
        pass

    def onPlayBackError(self) -> None:
        pass

    def onPlayBackPaused(self) -> None:
        pass

    def onPlayBackResumed(self) -> None:
        pass


xbmc_mod.log = _log
xbmc_mod.sleep = _sleep
xbmc_mod.getCondVisibility = _get_cond_visibility
xbmc_mod.getInfoLabel = _get_info_label
xbmc_mod.Monitor = _Monitor
xbmc_mod.Player = _Player

# translatePath: return the path unchanged (no Kodi special dirs in tests)
xbmc_mod.translatePath = lambda path: path

# executebuiltin: no-op
xbmc_mod.executebuiltin = lambda cmd: None
xbmc_mod.executeJSONRPC = lambda cmd: "{}"


# ---------------------------------------------------------------------------
# xbmcgui
# ---------------------------------------------------------------------------

xbmcgui_mod = _make_module("xbmcgui")

# Dialog result constants
xbmcgui_mod.DLG_YESNO_YES_BTN = 1
xbmcgui_mod.DLG_YESNO_NO_BTN = 0


class _Action:
    """Stub for xbmcgui.Action passed to onAction handlers."""

    def __init__(self, action_id: int = 0) -> None:
        self._id = action_id

    def getId(self) -> int:
        return self._id

    def getButtonCode(self) -> int:
        return 0

    def getAmount1(self) -> float:
        return 0.0

    def getAmount2(self) -> float:
        return 0.0


class _ListItem:
    """Stub for xbmcgui.ListItem."""

    def __init__(self, label: str = "", label2: str = "", path: str = "", offscreen: bool = False) -> None:
        self.label = label
        self.label2 = label2
        self.path = path
        self._info: dict = {}
        self._art: dict = {}
        self._properties: dict = {}

    def setLabel(self, label: str) -> None:
        self.label = label

    def setLabel2(self, label: str) -> None:
        self.label2 = label

    def setInfo(self, type: str, info_labels: dict) -> None:
        self._info.update(info_labels)

    def setArt(self, values: dict) -> None:
        self._art.update(values)

    def setProperty(self, key: str, value: str) -> None:
        self._properties[key] = value

    def getProperty(self, key: str) -> str:
        return self._properties.get(key, "")

    def setPath(self, path: str) -> None:
        self.path = path

    def getPath(self) -> str:
        return self.path


class _Window:
    """Stub for xbmcgui.Window."""

    def __init__(self, existing_window_id: int = 0) -> None:
        self._visible = False
        self._controls: dict = {}

    def show(self) -> None:
        self._visible = True

    def close(self) -> None:
        self._visible = False

    def isVisible(self) -> bool:
        return self._visible

    def getControl(self, control_id: int):
        return self._controls.get(control_id, MagicMock())

    def setFocus(self, control) -> None:
        pass

    def getFocusId(self) -> int:
        return 0

    def onAction(self, action) -> None:
        pass

    def onControl(self, control) -> None:
        pass


class _WindowDialog(_Window):
    """Stub for xbmcgui.WindowDialog."""
    pass


class _WindowXML(_Window):
    """Stub for xbmcgui.WindowXML."""

    def __init__(self, xml_filename: str = "", addon_path: str = "", default_skin: str = "default", default_res: str = "720p") -> None:
        super().__init__()


class _WindowXMLDialog(_WindowXML):
    """Stub for xbmcgui.WindowXMLDialog.

    Channel_Bug, Channel_Static, Channel_Entry, and TVGuide all inherit from
    this class. The stub supports show() / close() and tracks visibility state
    so tests can assert whether an overlay is open.
    """
    pass


class _Dialog:
    """Stub for xbmcgui.Dialog (notification/confirmation dialogs)."""

    def ok(self, heading: str, message: str) -> None:
        pass

    def yesno(self, heading: str, message: str, nolabel: str = "", yeslabel: str = "") -> bool:
        return False

    def notification(self, heading: str, message: str, icon: str = "", time: int = 5000, sound: bool = True) -> None:
        pass

    def textviewer(self, heading: str, text: str) -> None:
        pass

    def browse(self, type: int, heading: str, shares: str, mask: str = "", useThumbs: bool = False,
               treatAsFolder: bool = False, default: str = "", enableMultiple: bool = False):
        return ""


class _DialogProgress:
    """Stub for xbmcgui.DialogProgress."""

    def create(self, heading: str, message: str = "") -> None:
        pass

    def update(self, percent: int, message: str = "") -> None:
        pass

    def close(self) -> None:
        pass

    def iscanceled(self) -> bool:
        return False


xbmcgui_mod.Action = _Action
xbmcgui_mod.ListItem = _ListItem
xbmcgui_mod.Window = _Window
xbmcgui_mod.WindowDialog = _WindowDialog
xbmcgui_mod.WindowXML = _WindowXML
xbmcgui_mod.WindowXMLDialog = _WindowXMLDialog
xbmcgui_mod.Dialog = _Dialog
xbmcgui_mod.DialogProgress = _DialogProgress

# Notification icon constants
xbmcgui_mod.NOTIFICATION_INFO = "info"
xbmcgui_mod.NOTIFICATION_WARNING = "warning"
xbmcgui_mod.NOTIFICATION_ERROR = "error"


# ---------------------------------------------------------------------------
# xbmcaddon
# ---------------------------------------------------------------------------

xbmcaddon_mod = _make_module("xbmcaddon")


class _Addon:
    """Stub for xbmcaddon.Addon.

    Stores settings in a plain dict so tests can pre-populate values via
    instance._settings before exercising code that reads addon settings.
    """

    def __init__(self, addon_id: str = "script.classictv.service") -> None:
        self.addon_id = addon_id
        self._settings: dict = {
            "schedule_service_url": "http://localhost:8300",
            "default_channel": "nickelodeon",
        }

    def getSetting(self, setting_id: str) -> str:
        return self._settings.get(setting_id, "")

    def setSetting(self, setting_id: str, value: str) -> None:
        self._settings[setting_id] = value

    def getSettingBool(self, setting_id: str) -> bool:
        return self._settings.get(setting_id, False)

    def getSettingInt(self, setting_id: str) -> int:
        try:
            return int(self._settings.get(setting_id, 0))
        except (ValueError, TypeError):
            return 0

    def getAddonInfo(self, key: str) -> str:
        info = {
            "id": self.addon_id,
            "name": "Classic TV",
            "version": "3.0.0",
            "path": "/storage/.kodi/addons/script.classictv.service",
            "profile": "/storage/.kodi/userdata/addon_data/script.classictv.service",
            "icon": "",
        }
        return info.get(key, "")

    def openSettings(self) -> None:
        pass


xbmcaddon_mod.Addon = _Addon


# ---------------------------------------------------------------------------
# xbmcvfs
# ---------------------------------------------------------------------------

xbmcvfs_mod = _make_module("xbmcvfs")


class _File:
    """Stub for xbmcvfs.File (virtual filesystem file handle)."""

    def __init__(self, path: str, mode: str = "r") -> None:
        self.path = path
        self.mode = mode
        self._data = b""
        self._pos = 0

    def read(self, num_bytes: int = -1) -> bytes:
        if num_bytes == -1:
            chunk = self._data[self._pos:]
            self._pos = len(self._data)
        else:
            chunk = self._data[self._pos:self._pos + num_bytes]
            self._pos += len(chunk)
        return chunk

    def write(self, data: bytes) -> bool:
        self._data += data
        return True

    def close(self) -> None:
        pass

    def size(self) -> int:
        return len(self._data)


xbmcvfs_mod.File = _File
xbmcvfs_mod.exists = lambda path: False
xbmcvfs_mod.mkdir = lambda path: True
xbmcvfs_mod.mkdirs = lambda path: True
xbmcvfs_mod.rmdir = lambda path: True
xbmcvfs_mod.delete = lambda path: True
xbmcvfs_mod.copy = lambda src, dst: True
xbmcvfs_mod.rename = lambda src, dst: True
xbmcvfs_mod.listdir = lambda path: ([], [])
xbmcvfs_mod.translatePath = lambda path: path
