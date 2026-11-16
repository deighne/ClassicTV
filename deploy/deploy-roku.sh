#!/usr/bin/env bash
# deploy/deploy-roku.sh — Package and optionally sideload the Classic TV Roku channel.
#
# Usage:
#   bash deploy/deploy-roku.sh                      # build dist/classictv-roku.zip
#   bash deploy/deploy-roku.sh <roku-ip> <password> # build + sideload to Roku dev mode

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${ROOT_DIR}/dist"
ZIP_PATH="${OUT_DIR}/classictv-roku.zip"

mkdir -p "${OUT_DIR}"
rm -f "${ZIP_PATH}"

(
  cd "${ROOT_DIR}/roku"
  zip -qr "${ZIP_PATH}" manifest source components images
)

echo "✓ Built ${ZIP_PATH}"

if [ "${#}" -ge 1 ]; then
  ROKU_IP="$1"
  ROKU_PASS="${2:-${ROKU_DEV_PASS:-}}"
  if [ -z "${ROKU_PASS}" ]; then
    echo "Usage: $0 <roku-ip> <rokudev-password>"
    exit 1
  fi
  echo "Sideloading to http://${ROKU_IP}/plugin_install ..."
  curl -sf --digest -u "rokudev:${ROKU_PASS}" \
    -F "mysubmit=Install" \
    -F "archive=@${ZIP_PATH}" \
    "http://${ROKU_IP}/plugin_install" > /dev/null
  echo "✓ Installed on Roku (${ROKU_IP})"
fi
