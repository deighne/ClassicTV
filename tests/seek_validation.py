"""
seek_validation.py — Standalone seek strategy validation script.

Validates Requirement 11 (Mid-Episode Seek Strategy) against real
Kodi/Jellyfin hardware by measuring achieved seek accuracy for both
Strategy A (StartTimeTicks in URL) and Strategy B (post-start seek).

NOT a pytest test — run directly:

    JELLYFIN_API_KEY=xxx \\
    SEEK_TEST_ITEM_ID=abc123 \\
    KODI_URL=http://192.168.1.50:8080/jsonrpc \\
    python3 tests/seek_validation.py

Requirements validated: 11.1, 11.2, 11.3, 11.4, 11.6
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class Config:
    """All configuration in one place. Override via environment variables."""

    jellyfin_base_url: str = field(
        default_factory=lambda: os.environ.get(
            "JELLYFIN_BASE_URL", "http://192.168.1.207:8096"
        )
    )
    jellyfin_api_key: str = field(
        default_factory=lambda: os.environ.get("JELLYFIN_API_KEY", "")
    )
    kodi_url: str = field(
        default_factory=lambda: os.environ.get(
            "KODI_URL", "http://192.168.1.50:8080/jsonrpc"
        )
    )
    test_item_id: str = field(
        default_factory=lambda: os.environ.get("SEEK_TEST_ITEM_ID", "")
    )
    # Target seek offsets in seconds (Requirement 11.1: ±2s tolerance)
    target_offsets: list = field(default_factory=lambda: [0, 60, 300, 900])

    # Polling / timing constants
    playback_start_poll_interval_s: float = 0.5
    playback_start_timeout_s: float = 10.0
    post_seek_poll_interval_s: float = 0.5
    post_seek_max_polls: int = 5      # 5 × 500ms = 2.5s
    seek_tolerance_s: float = 2.0    # Requirement 11.1

    def validate(self) -> None:
        """Raise SystemExit with a clear message if required config is missing."""
        missing = []
        if not self.jellyfin_api_key:
            missing.append("JELLYFIN_API_KEY")
        if not self.test_item_id:
            missing.append("SEEK_TEST_ITEM_ID")
        if missing:
            _fatal(
                f"Missing required environment variable(s): {', '.join(missing)}\n"
                "Usage: JELLYFIN_API_KEY=xxx SEEK_TEST_ITEM_ID=abc123 "
                "KODI_URL=http://192.168.1.50:8080/jsonrpc python3 tests/seek_validation.py"
            )


# ---------------------------------------------------------------------------
# Dataclasses for results
# ---------------------------------------------------------------------------

@dataclass
class SeekResult:
    strategy: str
    target_offset: float
    achieved_position: float
    error: float
    elapsed_ms: int
    success: bool  # True if error <= tolerance


@dataclass
class StrategySummary:
    strategy: str
    results: list  # list[SeekResult]

    @property
    def mean_error(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.error for r in self.results) / len(self.results)

    @property
    def max_error(self) -> float:
        if not self.results:
            return 0.0
        return max(r.error for r in self.results)

    @property
    def mean_time_to_sync_ms(self) -> float:
        if not self.results:
            return 0.0
        return sum(r.elapsed_ms for r in self.results) / len(self.results)


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    """Print a timestamped log line to stdout."""
    ts = time.strftime("%H:%M:%S", time.localtime())
    print(f"[{ts}] {msg}", flush=True)


def _fatal(msg: str) -> None:
    """Print an error message and exit cleanly."""
    print(f"\nERROR: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Kodi JSON-RPC helper
# ---------------------------------------------------------------------------

def kodi_rpc(method: str, params: dict, kodi_url: str) -> dict:
    """Send a JSON-RPC call to Kodi and return the parsed response dict."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
    ).encode()
    req = urllib.request.Request(
        kodi_url,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _stop_kodi_playback(cfg: Config) -> None:
    """Stop any active Kodi player gracefully (ignores errors)."""
    try:
        active = kodi_rpc("Player.GetActivePlayers", {}, cfg.kodi_url)
        players = active.get("result", [])
        for p in players:
            kodi_rpc("Player.Stop", {"playerid": p["playerid"]}, cfg.kodi_url)
            time.sleep(0.5)
    except Exception:  # noqa: S110  # nothing playing or Kodi unreachable — that's fine
        pass


def _get_player_time_seconds(cfg: Config) -> Optional[float]:
    """
    Query Kodi for current playback position.
    Returns seconds as a float, or None if no active player.
    """
    try:
        active = kodi_rpc("Player.GetActivePlayers", {}, cfg.kodi_url)
        players = active.get("result", [])
        if not players:
            return None
        player_id = players[0]["playerid"]
        props = kodi_rpc(
            "Player.GetProperties",
            {"playerid": player_id, "properties": ["time"]},
            cfg.kodi_url,
        )
        t = props.get("result", {}).get("time", {})
        if not t:
            return None
        return t["hours"] * 3600 + t["minutes"] * 60 + t["seconds"]
    except Exception:
        return None


def _wait_for_playback_start(cfg: Config) -> Optional[float]:
    """
    Poll until Kodi reports a non-zero playback position, or timeout.
    Returns the first non-zero position in seconds, or None on timeout.
    """
    deadline = time.monotonic() + cfg.playback_start_timeout_s
    while time.monotonic() < deadline:
        pos = _get_player_time_seconds(cfg)
        if pos is not None and pos > 0:
            return pos
        time.sleep(cfg.playback_start_poll_interval_s)
    return None


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------

def _build_stream_url_with_ticks(cfg: Config, seek_offset_s: float) -> str:
    """
    Build a Jellyfin direct-play URL with StartTimeTicks embedded.
    StartTimeTicks = seek_offset_seconds * 10_000_000  (Requirement 11.3)
    """
    start_ticks = int(seek_offset_s * 10_000_000)
    return (
        f"{cfg.jellyfin_base_url}/Videos/{cfg.test_item_id}/stream"
        f"?Static=true"
        f"&api_key={cfg.jellyfin_api_key}"
        f"&StartTimeTicks={start_ticks}"
    )


def _build_stream_url_plain(cfg: Config) -> str:
    """Build a Jellyfin direct-play URL without any seek parameter."""
    return (
        f"{cfg.jellyfin_base_url}/Videos/{cfg.test_item_id}/stream"
        f"?Static=true"
        f"&api_key={cfg.jellyfin_api_key}"
    )


# ---------------------------------------------------------------------------
# Strategy A: StartTimeTicks
# ---------------------------------------------------------------------------

def _run_strategy_a(cfg: Config, target_offset: float) -> SeekResult:
    """
    Strategy A (Requirement 11.3): embed StartTimeTicks in the stream URL
    and measure where Kodi actually lands.
    """
    _log(f"  [Strategy A] target={target_offset}s — opening with StartTimeTicks")

    url = _build_stream_url_with_ticks(cfg, target_offset)
    _stop_kodi_playback(cfg)
    time.sleep(0.3)  # brief settle after stop

    t0 = time.monotonic()
    try:
        kodi_rpc("Player.Open", {"item": {"file": url}}, cfg.kodi_url)
    except urllib.error.URLError as exc:
        _fatal(f"Cannot reach Kodi at {cfg.kodi_url}: {exc}")

    achieved = _wait_for_playback_start(cfg)
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if achieved is None:
        _log(f"  [Strategy A] TIMEOUT — playback never started within {cfg.playback_start_timeout_s}s")
        return SeekResult(
            strategy="StartTimeTicks",
            target_offset=target_offset,
            achieved_position=-1.0,
            error=float("inf"),
            elapsed_ms=elapsed_ms,
            success=False,
        )

    error = abs(achieved - target_offset)
    success = error <= cfg.seek_tolerance_s
    _log(
        f"  [Strategy A] strategy=StartTimeTicks "
        f"target={target_offset} "
        f"achieved={achieved:.1f} "
        f"error={error:.1f}s "
        f"elapsed={elapsed_ms}ms"
        f"{'  OK' if success else '  FAIL (>{cfg.seek_tolerance_s}s)'}"
    )
    return SeekResult(
        strategy="StartTimeTicks",
        target_offset=target_offset,
        achieved_position=achieved,
        error=error,
        elapsed_ms=elapsed_ms,
        success=success,
    )


# ---------------------------------------------------------------------------
# Strategy B: Post-start seek
# ---------------------------------------------------------------------------

def _seek_decompose(target_s: float) -> dict:
    """Decompose a float seconds value into Kodi time dict {hours, minutes, seconds}."""
    total = round(target_s)
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    return {"hours": hours, "minutes": minutes, "seconds": seconds}


def _run_strategy_b(cfg: Config, target_offset: float) -> SeekResult:
    """
    Strategy B (Requirement 11.4): open without StartTimeTicks, wait for
    playback to start, then issue Player.Seek.  Poll up to
    post_seek_max_polls × post_seek_poll_interval_s for convergence.
    """
    _log(f"  [Strategy B] target={target_offset}s — post-start seek")

    url = _build_stream_url_plain(cfg)
    _stop_kodi_playback(cfg)
    time.sleep(0.3)

    t0 = time.monotonic()
    try:
        kodi_rpc("Player.Open", {"item": {"file": url}}, cfg.kodi_url)
    except urllib.error.URLError as exc:
        _fatal(f"Cannot reach Kodi at {cfg.kodi_url}: {exc}")

    # Wait for playback to start at all (any non-zero position)
    initial = _wait_for_playback_start(cfg)
    if initial is None:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        _log("  [Strategy B] TIMEOUT — playback never started")
        return SeekResult(
            strategy="PostStartSeek",
            target_offset=target_offset,
            achieved_position=-1.0,
            error=float("inf"),
            elapsed_ms=elapsed_ms,
            success=False,
        )

    # Issue the seek (Requirement 11.4)
    try:
        active = kodi_rpc("Player.GetActivePlayers", {}, cfg.kodi_url)
        players = active.get("result", [])
        if players:
            player_id = players[0]["playerid"]
            kodi_rpc(
                "Player.Seek",
                {"playerid": player_id, "value": {"time": _seek_decompose(target_offset)}},
                cfg.kodi_url,
            )
    except Exception as exc:
        _log(f"  [Strategy B] Seek call failed: {exc}")

    # Poll until within tolerance or exhausted (Requirement 11.4: up to 5×500ms)
    achieved = None
    for poll_num in range(cfg.post_seek_max_polls):
        time.sleep(cfg.post_seek_poll_interval_s)
        pos = _get_player_time_seconds(cfg)
        if pos is not None:
            achieved = pos
            if abs(pos - target_offset) <= cfg.seek_tolerance_s:
                break  # Converged
        _log(
            f"  [Strategy B] poll {poll_num + 1}/{cfg.post_seek_max_polls}: "
            f"pos={pos:.1f}s" if pos is not None else
            f"  [Strategy B] poll {poll_num + 1}/{cfg.post_seek_max_polls}: no position"
        )

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if achieved is None:
        achieved = -1.0

    error = abs(achieved - target_offset) if achieved >= 0 else float("inf")
    success = error <= cfg.seek_tolerance_s

    _log(
        f"  [Strategy B] strategy=PostStartSeek "
        f"target={target_offset} "
        f"achieved={achieved:.1f} "
        f"error={error:.1f}s "
        f"elapsed={elapsed_ms}ms"
        f"{'  OK' if success else '  FAIL (>{cfg.seek_tolerance_s}s)'}"
    )
    return SeekResult(
        strategy="PostStartSeek",
        target_offset=target_offset,
        achieved_position=achieved,
        error=error,
        elapsed_ms=elapsed_ms,
        success=success,
    )


# ---------------------------------------------------------------------------
# Connectivity pre-checks
# ---------------------------------------------------------------------------

def _check_jellyfin(cfg: Config) -> None:
    """Verify Jellyfin is reachable and the test item exists. Exit on failure."""
    _log(f"Checking Jellyfin at {cfg.jellyfin_base_url} ...")
    url = (
        f"{cfg.jellyfin_base_url}/Items/{cfg.test_item_id}"
        f"?api_key={cfg.jellyfin_api_key}"
    )
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        _log(f"  Jellyfin OK — item: {data.get('Name', '(unknown)')!r}")
    except urllib.error.HTTPError as exc:
        _fatal(
            f"Jellyfin returned HTTP {exc.code} for item {cfg.test_item_id}. "
            "Check SEEK_TEST_ITEM_ID."
        )
    except urllib.error.URLError as exc:
        _fatal(f"Cannot reach Jellyfin at {cfg.jellyfin_base_url}: {exc}")


def _check_kodi(cfg: Config) -> None:
    """Verify Kodi JSON-RPC is reachable. Exit on failure."""
    _log(f"Checking Kodi at {cfg.kodi_url} ...")
    try:
        resp = kodi_rpc("JSONRPC.Version", {}, cfg.kodi_url)
        version = resp.get("result", {}).get("version", {})
        _log(
            f"  Kodi OK — JSON-RPC {version.get('major', '?')}.{version.get('minor', '?')}"
        )
    except urllib.error.URLError as exc:
        _fatal(
            f"Cannot reach Kodi JSON-RPC at {cfg.kodi_url}: {exc}\n"
            "Ensure Kodi's web server is enabled (Settings > Services > Control > "
            "Allow remote control via HTTP) and the address/port are correct."
        )


# ---------------------------------------------------------------------------
# Summary table
# ---------------------------------------------------------------------------

def _print_summary(summaries: list) -> None:
    """Print the final summary table to stdout."""
    sep = "=" * 52
    print(f"\n{sep}")
    print("SEEK VALIDATION SUMMARY")
    print(sep)
    for s in summaries:
        print(f"Strategy: {s.strategy}")
        print(f"  mean_error: {s.mean_error:.1f}s  "
              f"max_error: {s.max_error:.1f}s  "
              f"mean_time_to_sync: {s.mean_time_to_sync_ms:.0f}ms")
        # Per-offset breakdown
        for r in s.results:
            status = "OK" if r.success else "FAIL"
            print(
                f"    offset={int(r.target_offset):>4}s  "
                f"achieved={r.achieved_position:>7.1f}s  "
                f"error={r.error:>5.1f}s  "
                f"elapsed={r.elapsed_ms:>5}ms  [{status}]"
            )
    print(sep)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = Config()
    cfg.validate()

    _log("=== Classic TV 3.0 — Seek Strategy Validation ===")
    _log(f"Jellyfin: {cfg.jellyfin_base_url}")
    _log(f"Kodi:     {cfg.kodi_url}")
    _log(f"Item ID:  {cfg.test_item_id}")
    _log(f"Offsets:  {cfg.target_offsets}s")
    _log(f"Tolerance: ±{cfg.seek_tolerance_s}s (Requirement 11.1)")
    print()

    # Pre-flight checks
    _check_jellyfin(cfg)
    _check_kodi(cfg)
    print()

    # -------------------------------------------------------------------
    # Phase 1: Strategy A — StartTimeTicks
    # -------------------------------------------------------------------
    _log("--- Phase 1: Strategy A (StartTimeTicks) ---")
    strategy_a_results = []
    for offset in cfg.target_offsets:
        result = _run_strategy_a(cfg, float(offset))
        strategy_a_results.append(result)
        # Brief pause between runs to let the decoder settle
        time.sleep(1.0)

    strategy_a_summary = StrategySummary(
        strategy="StartTimeTicks", results=strategy_a_results
    )

    # -------------------------------------------------------------------
    # Phase 2: Strategy B — only if any Strategy A result exceeded tolerance
    # (Requirement 11.4)
    # -------------------------------------------------------------------
    failing_offsets = [r.target_offset for r in strategy_a_results if not r.success]
    strategy_b_summary: Optional[StrategySummary] = None

    if failing_offsets:
        _log(
            f"\n--- Phase 2: Strategy B (Post-Start Seek) ---"
            f"  [{len(failing_offsets)} offset(s) exceeded {cfg.seek_tolerance_s}s tolerance]"
        )
        strategy_b_results = []
        for offset in cfg.target_offsets:
            result = _run_strategy_b(cfg, float(offset))
            strategy_b_results.append(result)
            time.sleep(1.0)
        strategy_b_summary = StrategySummary(
            strategy="PostStartSeek (if tested)", results=strategy_b_results
        )
    else:
        _log(
            "\nAll Strategy A results within tolerance. "
            "Skipping Strategy B. (Requirement 11.3 sufficient)"
        )

    # Stop playback when done
    _stop_kodi_playback(cfg)

    # -------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------
    summaries = [strategy_a_summary]
    if strategy_b_summary:
        summaries.append(strategy_b_summary)

    _print_summary(summaries)

    # Exit with non-zero if any result failed
    all_results = strategy_a_results + (
        strategy_b_summary.results if strategy_b_summary else []
    )
    if any(not r.success for r in all_results):
        # Strategy B results: failures here are notable (log ERROR per Req 11.5)
        for r in all_results:
            if not r.success:
                _log(
                    f"ERROR: seek target={r.target_offset}s strategy={r.strategy} "
                    f"achieved={r.achieved_position:.1f}s error={r.error:.1f}s "
                    f"(exceeds ±{cfg.seek_tolerance_s}s tolerance)"
                )
        sys.exit(1)
    else:
        _log("All seek targets within tolerance. Validation passed.")
        sys.exit(0)


if __name__ == "__main__":
    main()
