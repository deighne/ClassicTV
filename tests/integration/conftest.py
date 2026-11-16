"""
Integration test fixtures for Classic TV 3.0.

These fixtures require live hardware:
  - jfmac (Jellyfin + Schedule_Service at 192.168.1.207)
  - Sweet Potato / Le Potato running Kodi (at 192.168.1.50)

All integration tests must be decorated with @pytest.mark.integration
and are skipped by default.  Pass --run-integration on the command line
to opt in.

Requirements: 32.1–32.5
"""

import os
import urllib.request

import pytest


# ---------------------------------------------------------------------------
# CLI option registration
# ---------------------------------------------------------------------------


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests against live hardware (jfmac + Sweet_Potato + Kodi)",
    )


# ---------------------------------------------------------------------------
# Guard fixture — all integration fixtures depend on this
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def run_integration(request):
    """Skip the entire session unless --run-integration was passed.

    All other integration fixtures declare `run_integration` as a parameter
    so that the skip fires before any connectivity check is attempted.

    Requirements: 32.1, 32.2
    """
    if not request.config.getoption("--run-integration"):
        pytest.skip("Pass --run-integration to run integration tests")


# ---------------------------------------------------------------------------
# Service URL fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def schedule_service_url(run_integration):
    """Return the base URL of the running Schedule_Service.

    Reads SCHEDULE_SERVICE_URL from the environment; defaults to
    http://192.168.1.207:8300.  Skips with a clear message if the
    /health endpoint is unreachable within 3 seconds.

    Requirements: 32.3
    """
    url = os.environ.get("SCHEDULE_SERVICE_URL", "http://192.168.1.207:8300")
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=3) as resp:
            assert resp.status == 200
    except Exception as exc:
        pytest.skip(f"Schedule_Service unreachable at {url}: {exc}")
    return url


@pytest.fixture(scope="session")
def jellyfin_url(run_integration):
    """Return the base URL of the Jellyfin instance.

    Reads JELLYFIN_BASE_URL from the environment; defaults to
    http://192.168.1.207:8096.  Skips if the host is unreachable.

    Requirements: 32.4
    """
    url = os.environ.get("JELLYFIN_BASE_URL", "http://192.168.1.207:8096")
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            # Any HTTP response (including redirects rendered as 200) means
            # the service is up; we just need to confirm connectivity.
            assert resp.status in range(200, 400)
    except Exception as exc:
        pytest.skip(f"Jellyfin unreachable at {url}: {exc}")
    return url


@pytest.fixture(scope="session")
def kodi_url(run_integration):
    """Return the JSON-RPC endpoint URL for the Kodi instance.

    Reads KODI_URL from the environment; defaults to
    http://192.168.1.50:8080/jsonrpc.  Skips if the endpoint is
    unreachable.

    Requirements: 32.5
    """
    url = os.environ.get("KODI_URL", "http://192.168.1.50:8080/jsonrpc")
    try:
        # POST a minimal JSONRPC ping to verify Kodi is alive.
        payload = b'{"jsonrpc":"2.0","method":"JSONRPC.Ping","id":1}'
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            assert resp.status == 200
    except Exception as exc:
        pytest.skip(f"Kodi JSON-RPC unreachable at {url}: {exc}")
    return url
