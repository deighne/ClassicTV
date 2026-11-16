# legacy/

This directory contains files from Classic TV 3.0 that are no longer active
but are kept for historical reference.

## com.classictv.schedule_service.plist

The launchd plist that ran the v3 Python schedule service on jfmac at `:8300`.

**Decommissioned:** September 19, 2026 (v4.0)

The v3 schedule service was replaced by the Jellyfin server plugin
(`Jellyfin.Plugin.ClassicTV`) which serves schedule data at
`http://jfmac:8096/ClassicTV/channel/{id}`. The v4 Kodi addon
(`plugin.video.classictv`) calls the Jellyfin plugin directly.

The service was stopped with:
```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.classictv.schedule_service.plist
```

The v3 Python source remains in `server/` for reference.
