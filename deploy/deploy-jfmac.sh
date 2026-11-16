#!/usr/bin/env bash
# deploy/deploy-jfmac.sh
# Deploy web content and channel configs to jfmac (Classic TV 4.0)
#
# NOTE: The v3 Python schedule service (com.classictv.schedule_service, :8300)
# was decommissioned on 2026-09-19. This script no longer manages it.
# Schedule data is now served by the Jellyfin plugin at :8096/ClassicTV/...
#
# Usage: ./deploy/deploy-jfmac.sh [user@host]

set -euo pipefail

[ -f .env ] && { set -a; . ./.env; set +a; }

JFMAC_USER="${JFMAC_USER:-$(whoami)}"
JFMAC_HOST="${JFMAC_HOST:-192.168.1.10}"
JFMAC_TARGET="${1:-${JFMAC_USER}@${JFMAC_HOST}}"
REMOTE_DIR="${REMOTE_DIR:-~/projects/ClassicTV}"

echo "=== Classic TV 4.0 — Deploy web content to jfmac ==="
echo "Target: ${JFMAC_TARGET}"
echo ""

DEPLOY_VERSION="$(git describe --tags --always --dirty)"
echo "Version: ${DEPLOY_VERSION}"

# Confirm the v3 schedule service is NOT running
if ssh "${JFMAC_TARGET}" "curl -s --max-time 2 http://localhost:8300/health" > /dev/null 2>&1; then
    echo "ERROR: v3 schedule service is still running on :8300 — it should be decommissioned."
    echo "Stop it with: launchctl bootout gui/\$(id -u) ~/Library/LaunchAgents/com.classictv.schedule_service.plist"
    exit 1
fi

# 1. Sync web content and tools (serves tv.dane.photos via Caddy)
rsync -av \
  web/ \
  "${JFMAC_TARGET}:${REMOTE_DIR}/web/"

echo "✓ Web content synced"

rsync -av \
  tools/ \
  "${JFMAC_TARGET}:${REMOTE_DIR}/tools/"

echo "✓ Tools synced"

# 2. Write VERSION file
# shellcheck disable=SC2029
echo "${DEPLOY_VERSION}" | ssh "${JFMAC_TARGET}" "cat > ${REMOTE_DIR}/VERSION"
echo "✓ VERSION written (${DEPLOY_VERSION})"

# 3. Deploy and start Classic TV daemons (launchd)
scp deploy/com.classictv.status_monitor.plist \
  "${JFMAC_TARGET}:Library/LaunchAgents/com.classictv.status_monitor.plist"
scp deploy/com.classictv.guide_renderer.plist \
  "${JFMAC_TARGET}:Library/LaunchAgents/com.classictv.guide_renderer.plist"
scp deploy/com.classictv.reencoder.plist \
  "${JFMAC_TARGET}:Library/LaunchAgents/com.classictv.reencoder.plist"
scp deploy/com.classictv.tv_auth.plist \
  "${JFMAC_TARGET}:Library/LaunchAgents/com.classictv.tv_auth.plist"
ssh "${JFMAC_TARGET}" '
  UID_NUM=$(id -u)
  reload_agent() {
    local label="$1"
    local plist="$2"
    launchctl bootout "gui/${UID_NUM}/${label}" 2>/dev/null || true
    for _ in $(seq 1 30); do
      launchctl print "gui/${UID_NUM}/${label}" >/dev/null 2>&1 || break
      sleep 0.5
    done
    launchctl bootstrap "gui/${UID_NUM}" "$plist"
  }

  reload_agent "com.classictv.status_monitor" "$HOME/Library/LaunchAgents/com.classictv.status_monitor.plist"
  reload_agent "com.classictv.guide_renderer" "$HOME/Library/LaunchAgents/com.classictv.guide_renderer.plist"
  reload_agent "com.classictv.tv_auth" "$HOME/Library/LaunchAgents/com.classictv.tv_auth.plist"

  # Leave reencoder undisturbed if already running so an in-progress ffmpeg
  # encode is not interrupted (which previously raced bootout/bootstrap).
  if ! launchctl print "gui/${UID_NUM}/com.classictv.reencoder" >/dev/null 2>&1; then
    launchctl bootstrap "gui/${UID_NUM}" "$HOME/Library/LaunchAgents/com.classictv.reencoder.plist"
  fi
  echo "✓ Status monitor, guide renderer, and re-encoder daemons registered with launchd"
'

# 4. Verify Jellyfin plugin is responding
sleep 1
curl -sf "http://${JFMAC_HOST}:8096/ClassicTV/channels" > /dev/null \
    && echo "✓ Jellyfin ClassicTV plugin is online" \
    || echo "WARN: /ClassicTV/channels did not respond (Jellyfin may be starting)"

echo ""
echo "=== Deploy complete ==="
