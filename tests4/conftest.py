"""
Kodi module stubs for the Classic TV 4.0 test suite.

Identical to tests/conftest.py with two differences:
1. The addon lib path points at addon4/resources/lib instead of addon/resources/lib.
2. The xbmcaddon stub defaults reflect 4.0 settings keys.
"""

import sys
import os
import types
from unittest.mock import MagicMock

# Inject addon4 lib path so test modules can import schedule, channels, etc.
_ADDON4_LIB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "addon4",
    "resources",
    "lib",
)
_ADDON4_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "addon4",
)
for _p in (_ADDON4_LIB, _ADDON4_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


# ---------------------------------------------------------------------------
# xbmc
# ---------------------------------------------------------------------------

xbmc_mod = _make_module("xbmc")
xbmc_mod.LOGDEBUG = 0
xbmc_mod.LOGINFO = 1
xbmc_mod.LOGWARNING = 2
xbmc_mod.LOGERROR = 3
xbmc_mod.LOGFATAL = 4

xbmc_mod.log = lambda msg, level=1: None
xbmc_mod.sleep = lambda ms: None
xbmc_mod.getCondVisibility = lambda cond: False
xbmc_mod.getInfoLabel = lambda label: ""
xbmc_mod.translatePath = lambda path: path
xbmc_mod.executebuiltin = MagicMock()
xbmc_mod.executeJSONRPC = lambda cmd: "{}"


class _Monitor:
    def waitForAbort(self, timeout: float = 0) -> bool:
        return False

    def abortRequested(self) -> bool:
        return False

    def onNotification(self, sender: str, method: str, data: str) -> None:
        pass


class _Player:
    def __init__(self) -> None:
        self._playing = False
        self._current_time = 0.0
        self.play_calls: list = []

    def play(
        self, item=None, listitem=None, windowed: bool = False, startpos: int = -1
    ) -> None:
        self.play_calls.append((item, listitem))

    def stop(self) -> None:
        self._playing = False

    def isPlaying(self) -> bool:
        return self._playing

    def isPlayingVideo(self) -> bool:
        return self._playing

    def getTime(self) -> float:
        return self._current_time

    def getTotalTime(self) -> float:
        return 0.0

    def seekTime(self, t: float) -> None:
        self._current_time = t

    def getPlayingFile(self) -> str:
        return ""


xbmc_mod.Monitor = _Monitor
xbmc_mod.Player = _Player


# ---------------------------------------------------------------------------
# xbmcgui
# ---------------------------------------------------------------------------

xbmcgui_mod = _make_module("xbmcgui")
xbmcgui_mod.NOTIFICATION_INFO = "info"
xbmcgui_mod.NOTIFICATION_WARNING = "warning"
xbmcgui_mod.NOTIFICATION_ERROR = "error"
xbmcgui_mod.DLG_YESNO_YES_BTN = 1
xbmcgui_mod.DLG_YESNO_NO_BTN = 0


class _VideoInfoTag:
    def __init__(self) -> None:
        self.title = ""
        self.media_type = ""
        self.resume_point: tuple[float, float] | None = None

    def setTitle(self, title: str) -> None:
        self.title = title

    def setMediaType(self, media_type: str) -> None:
        self.media_type = media_type

    def setResumePoint(self, time_s: float, total_s: float = 0.0) -> None:
        self.resume_point = (time_s, total_s)


class _ListItem:
    def __init__(self, label: str = "", path: str = "", **kwargs) -> None:
        self.label = label
        self.path = path
        self._props: dict = {}
        self._info: dict = {}
        self._video_tag = _VideoInfoTag()
        self.content_lookup: bool = True

    def setInfo(self, type: str, info_labels: dict) -> None:
        self._info[type] = dict(info_labels)

    def setProperty(self, key: str, value: str) -> None:
        self._props[key] = value

    def getProperty(self, key: str) -> str:
        return self._props.get(key, "")

    def setPath(self, path: str) -> None:
        self.path = path

    def setContentLookup(self, enable: bool) -> None:
        self.content_lookup = enable

    def getVideoInfoTag(self) -> _VideoInfoTag:
        return self._video_tag


class _Control:
    def __init__(self) -> None:
        self._label = ""

    def setLabel(self, text: str) -> None:
        self._label = text

    def getLabel(self) -> str:
        return self._label


class _Control:
    """Stub for a Kodi GUI control (label, image, etc.)."""

    def __init__(self) -> None:
        self._label = ""

    def setLabel(self, text: str) -> None:
        self._label = text

    def getLabel(self) -> str:
        return self._label


class _WindowXMLDialog:
    def __init__(self, *args, **kwargs) -> None:
        self._visible = False
        self._controls: dict = {}
        self._properties: dict = {}
        # Mirror Kodi's real WindowXMLDialog lifecycle: call onInit() after
        # construction so subclasses that initialise state there work correctly.
        self.onInit()

    def onInit(self) -> None:
        pass

    def show(self) -> None:
        self._visible = True

    def close(self) -> None:
        self._visible = False

    def isVisible(self) -> bool:
        return self._visible

    def onAction(self, action) -> None:
        pass

    def getControl(self, control_id: int) -> "_Control":
        return self._controls.setdefault(control_id, _Control())

    def setProperty(self, key: str, value: str) -> None:
        self._properties[key] = value

    def getProperty(self, key: str) -> str:
        return self._properties.get(key, "")


class _Dialog:
    def ok(self, heading: str, message: str) -> None:
        pass

    def notification(
        self,
        heading: str,
        message: str,
        icon: str = "",
        time: int = 5000,
        sound: bool = True,
    ) -> None:
        pass

    def select(self, heading: str, options) -> int:
        return -1


class _Window:
    _instances: dict = {}

    def __init__(self, window_id: int = 10000) -> None:
        self._window_id = window_id
        if window_id not in _Window._instances:
            _Window._instances[window_id] = {}

    def setProperty(self, key: str, val: str) -> None:
        _Window._instances[self._window_id][key] = val

    def getProperty(self, key: str) -> str:
        return _Window._instances.get(self._window_id, {}).get(key, "")

    @classmethod
    def _reset(cls) -> None:
        cls._instances.clear()


xbmcgui_mod.ListItem = _ListItem
xbmcgui_mod.WindowXMLDialog = _WindowXMLDialog
xbmcgui_mod.Dialog = _Dialog
xbmcgui_mod.Window = _Window


# ---------------------------------------------------------------------------
# xbmcaddon
# ---------------------------------------------------------------------------

xbmcaddon_mod = _make_module("xbmcaddon")


class _Addon:
    def __init__(self, addon_id: str = "plugin.video.classictv") -> None:
        self.addon_id = addon_id
        self._settings: dict = {
            "jellyfin_base_url": "http://localhost:8096",
            "default_channel": "sitcoms",
        }

    def getSetting(self, key: str) -> str:
        return self._settings.get(key, "")

    def setSetting(self, key: str, value: str) -> None:
        self._settings[key] = value

    def getAddonInfo(self, key: str) -> str:
        return {
            "id": self.addon_id,
            "name": "Classic TV",
            "version": "4.0.0",
            "path": "/storage/.kodi/addons/plugin.video.classictv",
            "profile": "/storage/.kodi/userdata/addon_data/plugin.video.classictv",
            "icon": "",
        }.get(key, "")


xbmcaddon_mod.Addon = _Addon


# ---------------------------------------------------------------------------
# xbmcvfs
# ---------------------------------------------------------------------------

xbmcvfs_mod = _make_module("xbmcvfs")
xbmcvfs_mod.translatePath = lambda path: path
xbmcvfs_mod.exists = lambda path: False
xbmcvfs_mod.mkdirs = lambda path: True
