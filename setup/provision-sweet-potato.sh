#!/usr/bin/env bash
# setup/provision-sweet-potato.sh
# One-time provisioning for the Le Potato (Sweet Potato / CoreELEC)
# Run once after initial CoreELEC installation.
# Usage: ./setup/provision-sweet-potato.sh [sweet-potato-ip]

set -euo pipefail

SWEET_POTATO_IP="${1:-${SWEET_POTATO_IP:-192.168.1.50}}"
SSH_USER="root"

echo "=== Classic TV 3.0 — Provision Sweet Potato ==="

# 1. Create required directories
ssh "${SSH_USER}@${SWEET_POTATO_IP}" "
  mkdir -p /storage/.kodi/addons/script.classictv.service
  mkdir -p /storage/.config/rc_keymaps
  mkdir -p /storage/classictv_cache
  echo '✓ Directories created'
"

# 2. Enable Kodi web server (required for JSON-RPC remote control)
ssh "${SSH_USER}@${SWEET_POTATO_IP}" "
  SETTINGS_DIR=/storage/.kodi/userdata
  mkdir -p \${SETTINGS_DIR}
  echo '✓ Kodi settings dir ready'
  echo 'Enable Kodi web server manually: Settings > Services > Control > Allow remote control via HTTP'
  echo 'Port: 8080, No credentials required on LAN'
"

# 3. Check ir-keytable availability
ssh "${SSH_USER}@${SWEET_POTATO_IP}" "
  which ir-keytable && echo '✓ ir-keytable available' || echo 'WARN: ir-keytable not found — IR remote will not work'
"

# 4. Deploy IR keymap
scp ir-keymaps/sony-rmy180 "${SSH_USER}@${SWEET_POTATO_IP}:/storage/.config/rc_keymaps/sony-rmy180"
ssh "${SSH_USER}@${SWEET_POTATO_IP}" "ir-keytable -p sony -w /storage/.config/rc_keymaps/sony-rmy180 2>/dev/null && echo '✓ IR keymap applied' || echo 'WARN: Could not apply IR keymap'"

# 5. Persist the keymap across reboots via rc_maps.cfg
ssh "${SSH_USER}@${SWEET_POTATO_IP}" "
  RC_MAPS=/storage/.config/rc_maps.cfg
  if ! grep -q 'sony-rmy180' \${RC_MAPS} 2>/dev/null; then
    echo 'driver *' >> \${RC_MAPS}
    echo 'map /storage/.config/rc_keymaps/sony-rmy180' >> \${RC_MAPS}
    echo '✓ rc_maps.cfg updated'
  else
    echo '✓ rc_maps.cfg already configured'
  fi
"

echo ""
echo "=== Provisioning complete. Run deploy-sweet-potato.sh to deploy the addon. ==="
