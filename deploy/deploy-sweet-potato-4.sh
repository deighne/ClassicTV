#!/usr/bin/env bash
# deploy/deploy-sweet-potato-4.sh
# Deploy Classic TV 4.0 Kodi addon to the Le Potato (Sweet Potato)
# Usage: ./deploy/deploy-sweet-potato-4.sh <kodi-ip>

set -euo pipefail

[ -f .env ] && { set -a; . ./.env; set +a; }

SWEET_POTATO_IP="${1:-${SWEET_POTATO_IP:-}}"
if [ -z "${SWEET_POTATO_IP}" ]; then
    echo "Usage: $0 <kodi-ip>  (or set SWEET_POTATO_IP in .env)"
    exit 1
fi
ADDON_DIR="/storage/.kodi/addons/plugin.video.classictv"
KEYMAP_DIR="/storage/.kodi/userdata/keymaps"
SSH_USER="root"

echo "=== Classic TV 4.0 — Deploy to Sweet Potato ==="
echo "Target: ${SSH_USER}@${SWEET_POTATO_IP}"
echo "Addon dir: ${ADDON_DIR}"
echo ""

# If the device is not reachable directly (some networks block client-to-client
# traffic), hop through jfmac, which always can reach it. Tried in order: jfmac's
# LAN address, then the Cloudflare tunnel alias from ~/.ssh/config.
SSH_OPTS=()
if ! ssh -o BatchMode=yes -o ConnectTimeout=5 "${SSH_USER}@${SWEET_POTATO_IP}" true 2>/dev/null; then
    for jump in ${SSH_JUMP_HOSTS:-}; do
        if ssh -o BatchMode=yes -o ConnectTimeout=10 -o "ProxyJump=${jump}" \
            "${SSH_USER}@${SWEET_POTATO_IP}" true 2>/dev/null; then
            SSH_OPTS=(-o "ProxyJump=${jump}")
            echo "Direct SSH unreachable; hopping through ${jump}"
            echo ""
            break
        fi
    done
fi

DEPLOY_VERSION="$(git describe --tags --always --dirty)"
echo "Version: ${DEPLOY_VERSION}"

# Refuse to deploy from a dirty working tree
if git status --porcelain | grep -q .; then
    echo "ERROR: working tree is dirty — commit or stash changes before deploying"
    exit 1
fi

# 1. Sync addon files
rsync -av --delete \
  -e "ssh ${SSH_OPTS[*]-}" \
  --exclude="*.pyc" \
  --exclude="__pycache__/" \
  --exclude="keymaps/" \
  addon4/ \
  "${SSH_USER}@${SWEET_POTATO_IP}:${ADDON_DIR}/"

echo "✓ Addon files synced"

# 2. Write VERSION file — matches git describe output on the device
echo "${DEPLOY_VERSION}" | ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "${SSH_USER}@${SWEET_POTATO_IP}" \
    "cat > ${ADDON_DIR}/VERSION"
echo "✓ VERSION written (${DEPLOY_VERSION})"

# 3. Deploy Kodi keymap to userdata/keymaps/ (NOT inside the addon dir —
#    Kodi only loads keymaps from special://userdata/keymaps/).
ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "${SSH_USER}@${SWEET_POTATO_IP}" "mkdir -p ${KEYMAP_DIR}"
scp ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} addon4/keymaps/keyboard.xml \
    "${SSH_USER}@${SWEET_POTATO_IP}:${KEYMAP_DIR}/classictv.xml"
echo "✓ Kodi keymap deployed to ${KEYMAP_DIR}/classictv.xml"

# 3. Deploy IR keymap
ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "${SSH_USER}@${SWEET_POTATO_IP}" "mkdir -p /storage/.config/rc_keymaps"
scp ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} ir-keymaps/sony-rmy180 \
    "${SSH_USER}@${SWEET_POTATO_IP}:/storage/.config/rc_keymaps/sony-rmy180"
ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "${SSH_USER}@${SWEET_POTATO_IP}" \
    "ir-keytable -p sony -w /storage/.config/rc_keymaps/sony-rmy180 \
     || echo 'WARN: ir-keytable not available'"
echo "✓ IR keymap deployed"

# Boot resilience — three new artefacts installed below.
JELLYFIN_BASE_URL="${JELLYFIN_BASE_URL:-http://localhost:8096}"
CLASSICTV_CONF_DIR="/storage/.cache/classictv"
CLASSICTV_CONF_FILE="${CLASSICTV_CONF_DIR}/classictv.conf"
SYSTEMD_DIR="/storage/.config/system.d"
UNIT_NAME="classictv-wait-for-jellyfin.service"

# 4. Write shared config for boot gate shell script.
ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "${SSH_USER}@${SWEET_POTATO_IP}" "mkdir -p ${CLASSICTV_CONF_DIR}"
ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "${SSH_USER}@${SWEET_POTATO_IP}" \
    "echo 'JELLYFIN_BASE_URL=\"${JELLYFIN_BASE_URL}\"' > ${CLASSICTV_CONF_FILE}"
# Verify exact content.
ACTUAL=$(ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "${SSH_USER}@${SWEET_POTATO_IP}" "cat ${CLASSICTV_CONF_FILE}")
EXPECTED="JELLYFIN_BASE_URL=\"${JELLYFIN_BASE_URL}\""
if [ "${ACTUAL}" != "${EXPECTED}" ]; then
    echo "ERROR: ${CLASSICTV_CONF_FILE} content mismatch"
    echo "  expected: ${EXPECTED}"
    echo "  actual:   ${ACTUAL}"
    exit 1
fi
echo "✓ classictv.conf written and verified"

# 5. Write network_wait file.
#    This is consumed by kodi-waitonnetwork.service as a systemd EnvironmentFile,
#    which requires one KEY=VALUE assignment per line (multiple on one line are
#    NOT parsed).
NETWORK_WAIT_FILE="/storage/.cache/coreelec/network_wait"
NETWORK_WAIT_CONTENT='WAIT_NETWORK="true"
WAIT_NETWORK_TIME="30"
WAIT_NETWORK_DEFAULT="true"'
printf '%s\n' "${NETWORK_WAIT_CONTENT}" | ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "${SSH_USER}@${SWEET_POTATO_IP}" \
    "cat > ${NETWORK_WAIT_FILE}" \
    || { echo "ERROR: could not write ${NETWORK_WAIT_FILE}"; exit 1; }
ACTUAL_NW=$(ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "${SSH_USER}@${SWEET_POTATO_IP}" "cat ${NETWORK_WAIT_FILE}")
if [ "${ACTUAL_NW}" != "${NETWORK_WAIT_CONTENT}" ]; then
    echo "ERROR: ${NETWORK_WAIT_FILE} content mismatch"
    echo "  expected: ${NETWORK_WAIT_CONTENT}"
    echo "  actual:   ${ACTUAL_NW}"
    exit 1
fi
echo "✓ network_wait written and verified"

# 6. Deploy boot gate shell script (already synced by rsync step above).
ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "${SSH_USER}@${SWEET_POTATO_IP}" \
    "chmod +x ${ADDON_DIR}/resources/wait-for-jellyfin.sh"
echo "✓ wait-for-jellyfin.sh made executable"

# 7. Install systemd unit.
ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "${SSH_USER}@${SWEET_POTATO_IP}" "mkdir -p ${SYSTEMD_DIR}"
scp ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "addon4/resources/${UNIT_NAME}" \
    "${SSH_USER}@${SWEET_POTATO_IP}:${SYSTEMD_DIR}/${UNIT_NAME}"
echo "✓ systemd unit installed"

# 8. Enable the unit.
ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "${SSH_USER}@${SWEET_POTATO_IP}" \
    "systemctl daemon-reload && systemctl enable ${UNIT_NAME}"
echo "✓ systemd unit enabled"

# 9. Verify enablement.
ENABLED=$(ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "${SSH_USER}@${SWEET_POTATO_IP}" \
    "systemctl is-enabled ${UNIT_NAME} 2>/dev/null || echo 'not-found'")
if [ "${ENABLED}" != "enabled" ]; then
    echo "ERROR: ${UNIT_NAME} is not enabled (got: ${ENABLED})"
    exit 1
fi
echo "✓ ${UNIT_NAME} confirmed enabled"

# 10. Lock display mode to 1080p @ 59.94 Hz (disable refresh rate / resolution switching on content).
ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "${SSH_USER}@${SWEET_POTATO_IP}" \
    "sed -i 's/<setting id=\"videoplayer.adjustrefreshrate\"[^>]*>[^<]*<\/setting>/<setting id=\"videoplayer.adjustrefreshrate\">0<\/setting>/' /storage/.kodi/userdata/guisettings.xml"
ssh ${SSH_OPTS[@]+"${SSH_OPTS[@]}"} "${SSH_USER}@${SWEET_POTATO_IP}" \
    "cat << 'EOF' > /storage/.config/autostart.sh
#!/bin/sh
# Lock HDMI output strictly to 1080p @ 59.94 Hz
echo 1 > /sys/class/amhdmitx/amhdmitx0/frac_rate_policy
echo 1080p60hz > /sys/class/display/mode
EOF
chmod +x /storage/.config/autostart.sh"
echo "✓ Display locked to 1080p @ 59.94 Hz (videoplayer.adjustrefreshrate=0, autostart.sh installed)"

echo ""
echo "=== Deploy complete ==="
echo "Kodi will reload the keymap automatically."
echo "If the addon needs restarting: power cycle or restart Kodi manually."
