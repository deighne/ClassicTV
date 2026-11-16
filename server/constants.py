"""
Schedule_Service-side named constants for Classic TV 3.0.

Port numbers and service parameters are defined here. At runtime the service
reads environment variables to override these defaults (see .env.example).
No bare numeric literals for ports, timeouts, or intervals should appear
outside this module (Requirement 29.3).
"""

SCHEDULE_SERVICE_PORT = 8300
# Default HTTP port for the Schedule_Service on jfmac.
# Override at runtime via SERVICE_PORT environment variable.
# Documented in .env.example.

JELLYFIN_DEFAULT_PORT = 8096
# Default Jellyfin HTTP port. Used to construct JELLYFIN_BASE_URL when only
# a hostname is supplied. Override via JELLYFIN_BASE_URL environment variable
# (supply a full URL to use a non-default port or scheme).

RACHEL_ANCHOR_POLL_INTERVAL_SECONDS = 2
# How often the background Rachel poller thread queries the Jellyfin session
# API. Callers to GET /rachel-anchor between poll intervals receive the cached
# result from the most recent poll (Requirement 3.4).

RACHEL_ANCHOR_RESPONSE_TIMEOUT_SECONDS = 0.5
# Maximum time from TCP connection accepted to response body fully written for
# the /rachel-anchor endpoint (Requirement 3.5). Callers receive the cached
# result immediately; this budget covers serialization and socket write only.

CONFIG_HOT_RELOAD_DEBOUNCE_SECONDS = 5
# After a filesystem change event fires on the channels/ directory, wait this
# many seconds before reloading channel configuration. Debouncing prevents
# redundant reloads when editors write a file in multiple steps (e.g., save +
# format + save). The channel registry is atomically swapped under a Lock
# (Requirement 5.8).
