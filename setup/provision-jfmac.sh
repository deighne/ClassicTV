#!/usr/bin/env bash
# setup/provision-jfmac.sh
# One-time setup for jfmac (Mac Mini media server)
# Run from the ClassicTV3.0 repo root on jfmac itself.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== Classic TV 3.0 — Provision jfmac ==="
echo "Repo: ${REPO_DIR}"
echo ""

# 1. Python venv and dependencies
if [ ! -d "${REPO_DIR}/.venv" ]; then
  python3 -m venv "${REPO_DIR}/.venv"
  echo "✓ Virtual environment created"
fi

"${REPO_DIR}/.venv/bin/pip" install --quiet --upgrade pip
"${REPO_DIR}/.venv/bin/pip" install --quiet pytz pyyaml watchdog hypothesis pytest pytest-cov

echo "✓ Python dependencies installed"

# 2. .env file
if [ ! -f "${REPO_DIR}/.env" ]; then
  cp "${REPO_DIR}/.env.example" "${REPO_DIR}/.env"
  echo "✓ .env created from .env.example"
  echo "  IMPORTANT: Edit ${REPO_DIR}/.env and set JELLYFIN_API_KEY and OFFLINE_CLIP_JELLYFIN_ID"
else
  echo "✓ .env already exists"
fi

# 3. Install launchd plist
SERVICE_LABEL="com.classictv.schedule_service"
PLIST_SRC="${REPO_DIR}/launchd/${SERVICE_LABEL}.plist"
PLIST_DST="${HOME}/Library/LaunchAgents/${SERVICE_LABEL}.plist"

mkdir -p "${HOME}/Library/LaunchAgents"
cp "${PLIST_SRC}" "${PLIST_DST}"
echo "✓ launchd plist installed at ${PLIST_DST}"
echo "  Load it with: launchctl bootstrap gui/$(id -u) ${PLIST_DST}"

# 4. Create log directory
mkdir -p "${HOME}/Library/Logs/ClassicTV"
echo "✓ Log directory ready: ${HOME}/Library/Logs/ClassicTV"

echo ""
echo "=== Provisioning complete ==="
echo "Next steps:"
echo "  1. Edit .env: JELLYFIN_API_KEY and OFFLINE_CLIP_JELLYFIN_ID"
echo "  2. Edit channels/*.yaml with real Jellyfin IDs"
echo "  3. launchctl bootstrap gui/\$(id -u) ${PLIST_DST}"
echo "  4. curl http://localhost:8300/health"
