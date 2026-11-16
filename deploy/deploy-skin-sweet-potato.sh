#!/usr/bin/env bash
set -euo pipefail

# Deploy the Classic TV (Trinitron CRT) Kodi skin to the Sweet Potato and
# activate it via Kodi JSON-RPC / guisettings.
#
# Usage:
#   bash deploy/deploy-skin-sweet-potato.sh [SWEET_POTATO_IP]

[ -f .env ] && { set -a; . ./.env; set +a; }
SP_HOST="${1:-${SWEET_POTATO_IP:-}}"
if [ -z "${SP_HOST}" ]; then
    echo "Usage: $0 <kodi-ip>  (or set SWEET_POTATO_IP in .env)"
    exit 1
fi
SP_TARGET="root@${SP_HOST}"
SKIN_DIR="/storage/.kodi/addons/skin.classictv"

echo "=== Classic TV — Deploy skin.classictv to Sweet Potato ==="
echo "Target: ${SP_TARGET}"
echo "Skin dir: ${SKIN_DIR}"
echo ""

ssh "${SP_TARGET}" "mkdir -p ${SKIN_DIR}"
rsync -av --delete skin.classictv/ "${SP_TARGET}:${SKIN_DIR}/"
echo "✓ Skin files synced"

# Enable and activate skin.classictv via Kodi TCP JSON-RPC (:9090)
python3 - "${SP_HOST}" <<'PY'
import json, socket, sys
host = sys.argv[1]
try:
    with socket.create_connection((host, 9090), timeout=5) as s:
        for req_id, method, params in (
            (1, "Addons.SetAddonEnabled", {"addonid": "skin.classictv", "enabled": True}),
            (2, "Settings.SetSettingValue", {"setting": "lookandfeel.skin", "value": "skin.classictv"}),
        ):
            s.sendall((json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n").encode())
            s.recv(4096)
    print("✓ Skin enabled and activated (skin.classictv)")
except Exception as exc:
    print(f"WARN: Could not activate via JSON-RPC ({exc}); enable/select 'Classic TV (Trinitron CRT)' in Add-ons -> Look and feel -> Skin")
PY
