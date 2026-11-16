#!/usr/bin/env bash
# deploy/deploy-sweet-potato.sh
# Deploy Classic TV 3.0 Kodi addon to the Le Potato (Sweet Potato)
# Usage: ./deploy/deploy-sweet-potato.sh [sweet-potato-ip]
# Default IP: 192.168.1.50 (set SWEET_POTATO_IP env var or pass as $1)

set -euo pipefail

SWEET_POTATO_IP="${1:-${SWEET_POTATO_IP:-192.168.1.50}}"
KODI_ADDON_DIR="/storage/.kodi/addons/script.classictv.service"
SSH_USER="root"

echo "=== Classic TV 3.0 — Deploy to Sweet Potato ==="
echo "Target: ${SSH_USER}@${SWEET_POTATO_IP}"
echo "Addon dir: ${KODI_ADDON_DIR}"
echo ""

# Compute version string using git describe — the standard open-source approach.
# Produces e.g. "v3.0.0", "v3.0.0-14-gabcdef1", or "abcdef1" if no tags exist.
# Appends "-dirty" if the working tree has uncommitted changes.
DEPLOY_VERSION="$(git describe --tags --always --dirty)"
echo "Version: ${DEPLOY_VERSION}"

# 1. Sync addon files
rsync -av --delete \
  --exclude="*.pyc" \
  --exclude="__pycache__/" \
  addon/ \
  "${SSH_USER}@${SWEET_POTATO_IP}:${KODI_ADDON_DIR}/"

echo "✓ Addon files synced"

# 2. Write VERSION file — read by the addon at startup and logged.
#    Grep for "deploy version=" in kodi.log to find this deploy's entries.
# shellcheck disable=SC2029  # KODI_ADDON_DIR intentionally expands client-side
echo "${DEPLOY_VERSION}" | ssh "${SSH_USER}@${SWEET_POTATO_IP}" "cat > ${KODI_ADDON_DIR}/VERSION"
echo "✓ VERSION written (${DEPLOY_VERSION})"

# 3. Deploy IR keymap
ssh "${SSH_USER}@${SWEET_POTATO_IP}" "mkdir -p /storage/.config/rc_keymaps"
scp ir-keymaps/sony-rmy180 "${SSH_USER}@${SWEET_POTATO_IP}:/storage/.config/rc_keymaps/sony-rmy180"

# 4. Apply IR keymap
ssh "${SSH_USER}@${SWEET_POTATO_IP}" "ir-keytable -p sony -w /storage/.config/rc_keymaps/sony-rmy180 || echo 'WARN: ir-keytable not available or device not found'"

echo "✓ IR keymap deployed"

# 5. Restart Kodi addon (via JSON-RPC)
KODI_URL="http://${SWEET_POTATO_IP}:8080/jsonrpc"
curl -s -f \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","method":"Addons.ExecuteAddon","params":{"addonid":"script.classictv.service"},"id":1}' \
  "${KODI_URL}" > /dev/null && echo "✓ Addon reloaded via JSON-RPC" || echo "WARN: Could not reload via JSON-RPC (Kodi may need manual restart)"

echo ""
echo "=== Deploy complete ==="
