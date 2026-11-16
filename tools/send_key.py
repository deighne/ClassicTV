#!/usr/bin/env python3
"""
tools/send_key.py

Simulate a remote control button press on a local or remote Kodi instance
by sending JSONRPC.NotifyAll over the raw TCP JSON-RPC socket (port 9090).

This mirrors exactly what the physical IR remote does on the Sweet Potato:
  IR remote → ir-keytable → KEY_CHANNELUP → keyboard.xml →
  NotifyAll(classictv.service, ch_up) → ClassicTVMonitor.onNotification()

Usage:
    python3 tools/send_key.py ch_up
    python3 tools/send_key.py ch_down
    python3 tools/send_key.py guide
    python3 tools/send_key.py num_5
    python3 tools/send_key.py confirm
    python3 tools/send_key.py back

    # Target a remote device instead of localhost:
    python3 tools/send_key.py --host <kodi-ip> ch_up

Valid keys match the NotifyAll method names in keyboard.xml:
    ch_up, ch_down, guide
    num_0 … num_9, confirm, back
    up, down, left, right

The JSON-RPC socket (port 9090) is always available — no web server needed.
"""

import argparse
import json
import socket
import sys

VALID_KEYS = {
    "ch_up", "ch_down", "guide",
    "num_0", "num_1", "num_2", "num_3", "num_4",
    "num_5", "num_6", "num_7", "num_8", "num_9",
    "confirm", "back",
    "up", "down", "left", "right",
}

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9090


def send_notify(host: str, port: int, key: str) -> dict:
    msg = json.dumps({
        "jsonrpc": "2.0",
        "method": "JSONRPC.NotifyAll",
        "params": {
            "sender": "classictv.service",
            "message": key,
        },
        "id": 1,
    }) + "\n"

    with socket.create_connection((host, port), timeout=5) as s:
        s.sendall(msg.encode())
        response = s.recv(4096).decode()

    return json.loads(response)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send a simulated remote control keypress to Kodi.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "key",
        choices=sorted(VALID_KEYS),
        metavar="KEY",
        help=f"Button to send. One of: {', '.join(sorted(VALID_KEYS))}",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Kodi host (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Kodi JSON-RPC port (default: {DEFAULT_PORT})",
    )
    args = parser.parse_args()

    try:
        result = send_notify(args.host, args.port, args.key)
    except (ConnectionRefusedError, OSError) as exc:
        print(f"error: could not connect to {args.host}:{args.port} — {exc}", file=sys.stderr)
        print("       Is Kodi running? Is port 9090 open?", file=sys.stderr)
        sys.exit(1)

    if result.get("result") == "OK":
        print(f"sent: {args.key} → {args.host}:{args.port}")
    else:
        print(f"unexpected response: {result}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
