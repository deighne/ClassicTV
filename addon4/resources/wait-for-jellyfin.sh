#!/usr/bin/env sh
# wait-for-jellyfin.sh — Boot gate for Classic TV
#
# Polls the Jellyfin server's public health endpoint until it returns HTTP 200
# or 60 seconds elapse. Exits 0 always so the systemd unit never blocks Kodi
# permanently.
#
# Reads JELLYFIN_BASE_URL from /storage/.cache/classictv/classictv.conf if
# available; falls back to the hardcoded default when the file is missing.
#
# Requirements: 2.2, 2.3, 2.4, 2.5

set -eu

# Guard: if curl is not available, exit cleanly — nothing we can do.
command -v curl >/dev/null 2>&1 || exit 0

# Default Jellyfin server URL (overridden by classictv.conf if present).
JELLYFIN_BASE_URL="http://localhost:8096"

# Attempt to read the shared config file written by the deploy script.
CONF_FILE="/storage/.cache/classictv/classictv.conf"
if [ -f "$CONF_FILE" ]; then
    # shellcheck source=/dev/null
    . "$CONF_FILE"
fi

# Ensure we have a non-empty URL.
if [ -z "${JELLYFIN_BASE_URL:-}" ]; then
    JELLYFIN_BASE_URL="http://localhost:8096"
fi

ENDPOINT="${JELLYFIN_BASE_URL}/System/Info/Public"
# 30 iterations x 2s = 60s, comfortably under the unit's TimeoutStartSec=90.
MAX_ITERATIONS=30
POLL_INTERVAL=2
i=0

while [ "$i" -lt "$MAX_ITERATIONS" ]; do
    if curl -sf --max-time 3 "$ENDPOINT" >/dev/null 2>&1; then
        echo "wait-for-jellyfin: server ready after $((i * POLL_INTERVAL))s"
        exit 0
    fi
    i=$((i + 1))
    sleep "$POLL_INTERVAL"
done

echo "wait-for-jellyfin: timed out after $((MAX_ITERATIONS * POLL_INTERVAL))s — proceeding anyway"
exit 0
