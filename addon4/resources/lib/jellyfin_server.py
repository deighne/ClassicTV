"""
Jellyfin server ID discovery for Classic TV 4.0.

Reads the server UUID from the Jellyfin Kodi addon's data.json.
This is the same file the Jellyfin addon uses internally — reading it
is safe and consistent with how the addon itself resolves its credentials.

The server ID is passed as the `server` parameter in the plugin:// URL:
  plugin://plugin.video.jellyfin/?mode=play&id=...&server=SERVER_ID&timestamp=...
"""

from __future__ import annotations

import json

import xbmc
import xbmcvfs

_LOG_PREFIX = "[ClassicTV][jellyfin_server]"

_DATA_JSON_PATH = "special://profile/addon_data/plugin.video.jellyfin/data.json"


def get_server_id() -> str:
    """Return the Jellyfin server UUID, or "" on any error.

    Reads Servers[0].Id from the Jellyfin addon's data.json.
    Logs a warning if the file is missing or malformed.
    """
    try:
        real_path = xbmcvfs.translatePath(_DATA_JSON_PATH)
        with open(real_path, encoding="utf-8") as f:
            data = json.load(f)
        server_id: str = data["Servers"][0]["Id"]
        xbmc.log(
            f"{_LOG_PREFIX} server ID resolved: {server_id[:8]}...",
            xbmc.LOGINFO,
        )
        return server_id
    except FileNotFoundError:
        xbmc.log(
            f"{_LOG_PREFIX} data.json not found at {_DATA_JSON_PATH}"
            " — is the Jellyfin addon installed and configured?",
            xbmc.LOGWARNING,
        )
    except (json.JSONDecodeError, KeyError, IndexError) as exc:
        xbmc.log(
            f"{_LOG_PREFIX} failed to parse data.json: {exc}",
            xbmc.LOGWARNING,
        )
    except Exception as exc:
        xbmc.log(
            f"{_LOG_PREFIX} unexpected error reading server ID: {exc}",
            xbmc.LOGWARNING,
        )
    return ""
