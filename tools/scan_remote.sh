#!/usr/bin/env bash
# tools/scan_remote.sh
#
# Interactive IR remote scancode capture for CoreELEC / meson-ir.
#
# Prompts you to press each button one at a time, waits indefinitely,
# captures the scancode, confirms receipt, then moves to the next button.
# Outputs a ready-to-use ir-keytable keymap file at the end.
#
# Usage (run on the Sweet Potato):
#   systemctl stop kodi eventlircd
#   bash /var/media/xtra/classictv-bootstrap/scan_remote.sh
#   systemctl start eventlircd kodi
#
# Requirements: python3, ir-keytable
#
# The input device node (/dev/input/eventN) is discovered at runtime via
# ir-keytable so it works regardless of which eventN the kernel assigns.
# If the device becomes unreadable mid-session (e.g. due to a transient
# error) the script re-discovers it before each prompt.

set -euo pipefail

OUTPUT="/tmp/remote_scancodes.txt"
EVENT_SIZE=16   # input_event: tv_sec(u32) tv_usec(u32) type(u16) code(u16) value(s32)
EV_MSC=4

# Buttons to capture in order: "label:KEY_NAME"
BUTTONS=(
    "UP:KEY_UP"
    "DOWN:KEY_DOWN"
    "LEFT:KEY_LEFT"
    "RIGHT:KEY_RIGHT"
    "OK/Enter:KEY_OK"
    "Channel Up:KEY_CHANNELUP"
    "Channel Down:KEY_CHANNELDOWN"
    "Guide/EPG:KEY_EPG"
    "Back/Exit:KEY_EXIT"
    "0:KEY_NUMERIC_0"
    "1:KEY_NUMERIC_1"
    "2:KEY_NUMERIC_2"
    "3:KEY_NUMERIC_3"
    "4:KEY_NUMERIC_4"
    "5:KEY_NUMERIC_5"
    "6:KEY_NUMERIC_6"
    "7:KEY_NUMERIC_7"
    "8:KEY_NUMERIC_8"
    "9:KEY_NUMERIC_9"
)

# "transport" mode captures only the playback buttons that are not in the
# keymap yet. Press Ctrl-C to stop; buttons captured so far are already saved.
# Merge the lines it prints into addon-side ir-keymaps/sony-rmy180.
if [ "${1:-}" = "transport" ]; then
    BUTTONS=(
        "Play:KEY_PLAY"
        "Pause:KEY_PAUSE"
        "Stop:KEY_STOP"
        "Fast Forward:KEY_FASTFORWARD"
        "Rewind:KEY_REWIND"
        "Subtitle/CC:KEY_SUBTITLE"
    )
fi

# Discover the current IR input device by querying ir-keytable.
# Re-runs each time in case the event node has changed.
find_device() {
    local dev
    dev="$(ir-keytable 2>&1 | awk '/Input device/{print $NF}')"
    if [ -z "$dev" ] || [ ! -r "$dev" ]; then
        echo "ERROR: IR input device not found or not readable." >&2
        echo "       Make sure kodi and eventlircd are stopped:" >&2
        echo "         systemctl stop kodi eventlircd" >&2
        exit 1
    fi
    echo "$dev"
}

# Ensure sony (and other common) protocols are enabled so the kernel
# decoder can match whatever the remote transmits.
enable_protocols() {
    ir-keytable -p NEC,RC-5,RC-6,JVC,SONY > /dev/null 2>&1 || true
}

read_next_scancode() {
    local device="$1"
    # Read raw input_event structs until we see an EV_MSC event carrying
    # the scancode. Drains a few extra events to swallow key-repeat noise,
    # then returns the scancode as a hex string.
    python3 - "$device" "$EV_MSC" "$EVENT_SIZE" <<'PYEOF'
import sys, struct

device = sys.argv[1]
ev_msc = int(sys.argv[2])
size   = int(sys.argv[3])
fmt    = "IIHHi"   # u32 u32 u16 u16 s32

try:
    with open(device, "rb") as f:
        while True:
            raw = f.read(size)
            if len(raw) < size:
                break
            _sec, _usec, ev_type, _code, value = struct.unpack(fmt, raw)
            if ev_type == ev_msc:
                sc = f"0x{value & 0xFFFFFFFF:08x}"
                print(sc)
                sys.stdout.flush()
                # Drain repeats
                for _ in range(15):
                    f.read(size)
                break
except OSError as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
}

# ---- Main ----------------------------------------------------------------

echo "=== Classic TV 3.0 — IR Remote Scancode Capture ==="
echo ""

enable_protocols
DEVICE="$(find_device)"

echo "Device : ${DEVICE}"
echo "Output : ${OUTPUT}"
echo ""

true > "$OUTPUT"

for entry in "${BUTTONS[@]}"; do
    label="${entry%%:*}"
    keyname="${entry##*:}"

    while true; do
        # Re-discover device before each prompt — handles rare node changes
        DEVICE="$(find_device)"

        printf "Press  %-20s  ... " "${label}"

        scancode="$(read_next_scancode "$DEVICE")"

        if [ -n "$scancode" ]; then
            printf "%-14s  ✓ got it! Next in 2s...\n" "${scancode}"
            printf "%s %s  # %s\n" "${scancode}" "${keyname}" "${label}" >> "$OUTPUT"
            sleep 2
            break
        fi
    done
done

echo ""
echo "=== Capture complete ==="
echo ""
echo "Scancodes written to ${OUTPUT}:"
echo ""
cat "$OUTPUT"
echo ""
echo "Next steps:"
echo "  1. Copy the scancodes into ir-keymaps/sony-rmy180 in the repo"
echo "  2. Run: bash deploy/deploy-sweet-potato.sh"
