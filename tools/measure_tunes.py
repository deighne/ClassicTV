#!/usr/bin/env python3
"""Measure channel-change latency on a live Kodi by simulating the remote.

For each trial it sends a Classic TV key (like the IR remote does) over JSON-RPC
and times how long until Kodi reports the new stream started (``Player.OnAVStart``),
so the number is key press to picture, with no log correlation. Run it a few
times and read the summary; compare before and after a change.

    python3 tools/measure_tunes.py --host <kodi-ip>
    python3 tools/measure_tunes.py --host 127.0.0.1 --port 19090 --count 10
    python3 tools/measure_tunes.py --keys ch_up,ch_up,ch_down --settle 15

If the device is only reachable through jfmac, forward the port first:

    ssh -N -L 19090:192.168.1.237:9090 dane@192.168.1.207 &

The TV changes channel during the run. Pick a time when nobody is watching.
"""

from __future__ import annotations

import argparse
import json
import socket
import statistics
import sys
import time


class Rpc:
    """Kodi's TCP JSON-RPC: concatenated JSON objects with no delimiter."""

    def __init__(self, host: str, port: int) -> None:
        self.sock = socket.create_connection((host, port), timeout=10)
        self.sock.settimeout(0.2)
        self.buf = ""
        self.dec = json.JSONDecoder()

    def send_notify(self, key: str) -> None:
        msg = {
            "jsonrpc": "2.0",
            "method": "JSONRPC.NotifyAll",
            "params": {"sender": "classictv.service", "message": key},
            "id": 1,
        }
        self.sock.sendall(json.dumps(msg).encode())

    def poll(self) -> list[tuple[float, dict]]:
        """Read whatever has arrived; return [(arrival time, message)]."""
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            data = b""
        now = time.monotonic()
        self.buf += data.decode("utf8", "ignore")
        out = []
        while self.buf.strip():
            self.buf = self.buf.lstrip()
            try:
                obj, end = self.dec.raw_decode(self.buf)
            except ValueError:
                break
            self.buf = self.buf[end:]
            out.append((now, obj))
        return out


def run_trial(rpc: Rpc, key: str, timeout: float) -> dict:
    rpc.poll()  # drop stale events
    t0 = time.monotonic()
    rpc.send_notify(key)
    marks: dict[str, float] = {}
    title = ""
    while time.monotonic() - t0 < timeout:
        for t, msg in rpc.poll():
            method = msg.get("method", "")
            if method in ("Player.OnStop", "Player.OnPlay", "Player.OnAVStart"):
                marks.setdefault(method, t - t0)
                if method == "Player.OnPlay":
                    item = msg.get("params", {}).get("data", {}).get("item", {})
                    title = item.get("title") or item.get("label") or title
        if "Player.OnAVStart" in marks:
            break
    return {"key": key, "marks": marks, "title": title, "ok": "Player.OnAVStart" in marks}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9090)
    ap.add_argument("--count", type=int, default=8, help="number of tunes")
    ap.add_argument("--keys", default="ch_up,ch_down",
                    help="comma list of keys, cycled (default alternates ch_up/ch_down)")
    ap.add_argument("--settle", type=float, default=12,
                    help="seconds to wait after each tune before the next")
    ap.add_argument("--timeout", type=float, default=90, help="give up on one tune after this")
    args = ap.parse_args()

    keys = [k for k in args.keys.split(",") if k]
    rpc = Rpc(args.host, args.port)
    results = []
    print(f"{'#':>2} {'key':8} {'stop':>6} {'play':>6} {'picture':>8}  channel/title")
    for i in range(args.count):
        r = run_trial(rpc, keys[i % len(keys)], args.timeout)
        results.append(r)
        m = r["marks"]
        f = lambda k: f"{m[k]:6.2f}" if k in m else "     -"
        pic = f"{m['Player.OnAVStart']:8.2f}" if r["ok"] else " TIMEOUT"
        print(f"{i + 1:>2} {r['key']:8} {f('Player.OnStop')} {f('Player.OnPlay')} {pic}  {r['title']}")
        sys.stdout.flush()
        time.sleep(args.settle)

    times = [r["marks"]["Player.OnAVStart"] for r in results if r["ok"]]
    if times:
        print(f"\nkey to picture, {len(times)}/{len(results)} tunes:"
              f" min {min(times):.2f}s  median {statistics.median(times):.2f}s"
              f"  mean {statistics.mean(times):.2f}s  max {max(times):.2f}s")
    return 0 if len(times) == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
