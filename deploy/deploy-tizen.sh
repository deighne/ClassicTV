#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TIZEN_DIR="$ROOT/tizen"
DIST_DIR="$ROOT/dist"
TV_IP="${1:-}"
CERT_PROFILE="${TIZEN_CERT_PROFILE:-}"

mkdir -p "$DIST_DIR"

if ! command -v tizen >/dev/null 2>&1; then
  echo "Tizen CLI ('tizen') not found in PATH."
  echo "Import $TIZEN_DIR into Tizen Studio, sign with a Samsung TV certificate,"
  echo "or install Tizen Studio CLI tools and re-run."
  exit 1
fi

BUILD_DIR="$DIST_DIR/tizen-build"
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

tizen build-web -out "$BUILD_DIR" -- "$TIZEN_DIR"

if [ -n "$CERT_PROFILE" ]; then
  tizen package -t wgt -s "$CERT_PROFILE" -o "$DIST_DIR" -- "$BUILD_DIR"
else
  tizen package -t wgt -o "$DIST_DIR" -- "$BUILD_DIR"
fi

WGT_PATH="$(find "$DIST_DIR" -maxdepth 1 -name '*.wgt' | head -n 1)"
WGT_NO_SPACE="$DIST_DIR/$(basename "$WGT_PATH" | tr -d ' ')"
if [ "$WGT_PATH" != "$WGT_NO_SPACE" ]; then
  mv -f "$WGT_PATH" "$WGT_NO_SPACE"
  WGT_PATH="$WGT_NO_SPACE"
fi
echo "Packaged: $WGT_PATH"

if [ -n "$TV_IP" ]; then
  if ! command -v sdb >/dev/null 2>&1; then
    echo "sdb not found; connect to $TV_IP and install $WGT_PATH from Tizen Studio."
    exit 1
  fi
  sdb connect "$TV_IP"
  TARGET_NAME="$(sdb devices | awk -v ip="$TV_IP" '$1 ~ ip && $2 == "device" {print $3; exit}')"
  TARGET_NAME="${TARGET_NAME:-$TV_IP}"
  tizen install -n "$(basename "$WGT_PATH")" -t "$TARGET_NAME" -- "$DIST_DIR"
fi
