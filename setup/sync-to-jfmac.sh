#!/usr/bin/env bash
# setup/sync-to-jfmac.sh
# Initial full-repo sync from dev machine to jfmac.
# Run this once before provision-jfmac.sh when setting up fresh.
# For ongoing deploys (server code + channels only), use deploy/deploy-jfmac.sh instead.
#
# Usage: bash setup/sync-to-jfmac.sh [ssh-target]
# Default target: $SERVER_HOST or user@192.168.1.10
# Example: bash setup/sync-to-jfmac.sh user@192.168.1.10

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET="${1:-${SERVER_HOST:-user@192.168.1.10}}"
REMOTE_PATH="$HOME/projects/ClassicTV3.0"

echo "=== Classic TV 3.0 — Initial sync to jfmac ==="
echo "Source : ${REPO_DIR}/"
echo "Target : ${TARGET}:${REMOTE_PATH}/"
echo ""

# Ensure the remote projects directory exists
ssh "${TARGET}" "mkdir -p ~/projects"

rsync -av --progress \
  --exclude=".venv/" \
  --exclude="*.pyc" \
  --exclude="__pycache__/" \
  --exclude=".coverage" \
  --exclude=".hypothesis/" \
  --exclude=".pytest_cache/" \
  --exclude=".env" \
  "${REPO_DIR}/" \
  "${TARGET}:${REMOTE_PATH}/"

echo ""
echo "=== Sync complete ==="
echo "Next: ssh ${TARGET} and run: bash ~/projects/ClassicTV3.0/setup/provision-jfmac.sh"
