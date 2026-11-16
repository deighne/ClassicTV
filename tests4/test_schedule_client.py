"""Tests for ScheduleClient — schedule.py"""

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

from schedule import ScheduleClient, ChannelInfo


# ---------------------------------------------------------------------------
# Minimal HTTP server fixture to serve test responses
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """Records last request path and serves responses from _routes dict."""

    def log_message(self, format, *args):
        pass  # suppress output during tests

    def do_GET(self):
        response_body = self.server._routes.get(self.path)
        if response_body is None:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(response_body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def fake_server():
    """Spin up a localhost HTTP server with configurable routes."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server._routes = {}
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()
    port = server.server_address[1]
    yield server, f"http://127.0.0.1:{port}"
    server.shutdown()


# ---------------------------------------------------------------------------
# get_channels
# ---------------------------------------------------------------------------


def test_get_channels_returns_channel_list(fake_server):
    server, base_url = fake_server
    server._routes["/ClassicTV/channels"] = {
        "Channels": [
            {"Id": "sitcoms", "Name": "Classic Sitcoms"},
            {"Id": "anime", "Name": "Anime"},
        ]
    }

    client = ScheduleClient(base_url)
    channels = client.get_channels()

    assert len(channels) == 2
    assert channels[0] == ChannelInfo(id="sitcoms", name="Classic Sitcoms")
    assert channels[1] == ChannelInfo(id="anime", name="Anime")


def test_get_channels_returns_empty_on_server_error(fake_server):
    _server, base_url = fake_server
    # No route registered → 404
    client = ScheduleClient(base_url)
    assert client.get_channels() == []


def test_get_channels_returns_empty_on_unreachable():
    client = ScheduleClient("http://127.0.0.1:1")  # nothing listening
    assert client.get_channels() == []


def test_get_channels_returns_empty_on_malformed_json(fake_server):
    _server, _base_url = fake_server

    class _BadHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"not-json{{{")

    bad_server = HTTPServer(("127.0.0.1", 0), _BadHandler)
    bt = Thread(target=bad_server.serve_forever, daemon=True)
    bt.start()
    bad_base = f"http://127.0.0.1:{bad_server.server_address[1]}"
    client = ScheduleClient(bad_base)
    assert client.get_channels() == []
    bad_server.shutdown()


# ---------------------------------------------------------------------------
# get_slot
# ---------------------------------------------------------------------------


def test_get_slot_returns_schedule_slot(fake_server):
    server, base_url = fake_server
    server._routes["/ClassicTV/channel/sitcoms"] = {
        "ChannelId": "sitcoms",
        "Title": "Cheers S01E01",
        "JellyfinItemId": "abc123",
        "StartOffset": 847.3,
        "Duration": 1295.0,
        "NextItem": {
            "JellyfinItemId": "def456",
            "Title": "Cheers S01E02",
            "StartOffset": 0.0,
            "Duration": 1380.0,
        },
        "GeneratedAt": 1700000000,
    }

    client = ScheduleClient(base_url)
    slot = client.get_slot("sitcoms")

    assert slot is not None
    assert slot.channel_id == "sitcoms"
    assert slot.title == "Cheers S01E01"
    assert slot.jellyfin_item_id == "abc123"
    assert abs(slot.start_offset - 847.3) < 0.01
    assert abs(slot.duration - 1295.0) < 0.01
    assert slot.next_item is not None
    assert slot.next_item.jellyfin_item_id == "def456"


def test_get_slot_returns_none_on_404(fake_server):
    _, base_url = fake_server
    client = ScheduleClient(base_url)
    assert client.get_slot("nonexistent") is None


def test_get_slot_returns_none_on_unreachable():
    client = ScheduleClient("http://127.0.0.1:1")
    assert client.get_slot("sitcoms") is None


def test_get_slot_next_item_is_none_when_missing(fake_server):
    server, base_url = fake_server
    server._routes["/ClassicTV/channel/sitcoms"] = {
        "ChannelId": "sitcoms",
        "Title": "Show",
        "JellyfinItemId": "abc",
        "StartOffset": 0.0,
        "Duration": 1800.0,
        "NextItem": None,
        "GeneratedAt": 0,
    }
    client = ScheduleClient(base_url)
    slot = client.get_slot("sitcoms")
    assert slot is not None
    assert slot.next_item is None


def test_get_slot_next_item_empty_id_is_none(fake_server):
    server, base_url = fake_server
    server._routes["/ClassicTV/channel/sitcoms"] = {
        "ChannelId": "sitcoms",
        "Title": "Show",
        "JellyfinItemId": "abc",
        "StartOffset": 0.0,
        "Duration": 1800.0,
        "NextItem": {
            "JellyfinItemId": "",
            "Title": "",
            "StartOffset": 0.0,
            "Duration": 0.0,
        },
        "GeneratedAt": 0,
    }
    client = ScheduleClient(base_url)
    slot = client.get_slot("sitcoms")
    assert slot is not None
    assert slot.next_item is None  # empty ID treated as no next item


def test_get_channels_returns_empty_on_json_array_response(fake_server):
    """Server returns a JSON array — must not raise AttributeError."""
    _, _base_url = fake_server

    class _ArrayHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b"[1, 2, 3]"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    from http.server import HTTPServer
    from threading import Thread

    bad = HTTPServer(("127.0.0.1", 0), _ArrayHandler)
    Thread(target=bad.serve_forever, daemon=True).start()
    bad_url = f"http://127.0.0.1:{bad.server_address[1]}"
    client = ScheduleClient(bad_url)
    assert client.get_channels() == []
    bad.shutdown()


def test_get_slot_returns_none_on_json_string_response(fake_server):
    """Server returns a JSON string — must not raise AttributeError."""
    _, _base_url = fake_server

    class _StringHandler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b'"ok"'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    from http.server import HTTPServer
    from threading import Thread

    bad = HTTPServer(("127.0.0.1", 0), _StringHandler)
    Thread(target=bad.serve_forever, daemon=True).start()
    bad_url = f"http://127.0.0.1:{bad.server_address[1]}"
    client = ScheduleClient(bad_url)
    assert client.get_slot("sitcoms") is None
    bad.shutdown()


# ---------------------------------------------------------------------------
# ChannelInfo.number parsing — task 10
# ---------------------------------------------------------------------------


def test_get_channels_parses_number_field(fake_server):
    """Number field present in JSON is parsed into ChannelInfo.number."""
    server, base_url = fake_server
    server._routes["/ClassicTV/channels"] = {
        "Channels": [
            {"Id": "sitcoms", "Name": "Classic Sitcoms", "Number": 5},
            {"Id": "anime", "Name": "Anime", "Number": 12},
        ]
    }
    client = ScheduleClient(base_url)
    channels = client.get_channels()
    assert channels[0].number == 5
    assert channels[1].number == 12


def test_get_channels_number_defaults_to_zero_when_absent(fake_server):
    """Number field absent from JSON defaults to 0."""
    server, base_url = fake_server
    server._routes["/ClassicTV/channels"] = {
        "Channels": [
            {"Id": "sitcoms", "Name": "Classic Sitcoms"},
        ]
    }
    client = ScheduleClient(base_url)
    channels = client.get_channels()
    assert channels[0].number == 0


def test_get_channels_mixed_number_presence(fake_server):
    """Some channels have Number, some don't — both parse correctly."""
    server, base_url = fake_server
    server._routes["/ClassicTV/channels"] = {
        "Channels": [
            {"Id": "news", "Name": "News", "Number": 3},
            {"Id": "sitcoms", "Name": "Sitcoms"},  # no Number key
            {"Id": "anime", "Name": "Anime", "Number": 0},
        ]
    }
    client = ScheduleClient(base_url)
    channels = client.get_channels()
    assert channels[0].number == 3
    assert channels[1].number == 0
    assert channels[2].number == 0


# ---------------------------------------------------------------------------
# channel_exists — task 11
# ---------------------------------------------------------------------------


def test_channel_exists_returns_true_when_present(fake_server):
    """channel_exists returns True when the channel is in the live list."""
    server, base_url = fake_server
    server._routes["/ClassicTV/channels"] = {
        "Channels": [
            {"Id": "sitcoms", "Name": "Classic Sitcoms"},
            {"Id": "anime", "Name": "Anime"},
        ]
    }
    client = ScheduleClient(base_url)
    assert client.channel_exists("sitcoms") is True
    assert client.channel_exists("anime") is True


def test_channel_exists_returns_false_when_absent(fake_server):
    """channel_exists returns False when the channel is not in the list
    (covers the disabled-channel case — disabled channels are filtered out
    by the server before returning the channel list)."""
    server, base_url = fake_server
    server._routes["/ClassicTV/channels"] = {
        "Channels": [
            {"Id": "anime", "Name": "Anime"},
        ]
    }
    client = ScheduleClient(base_url)
    assert client.channel_exists("sitcoms") is False


def test_channel_exists_returns_true_on_unreachable_server():
    """When the server is unreachable, channel_exists returns True (conservative
    fallback — treat transient connectivity failure as 'may still exist' to
    avoid an unwanted tune-away)."""
    client = ScheduleClient("http://127.0.0.1:1")
    assert client.channel_exists("sitcoms") is True


def test_channel_exists_returns_true_on_empty_channel_list(fake_server):
    """If get_channels returns an empty list (server error / empty config),
    channel_exists returns True to avoid a fallback tune on a transient issue."""
    _server, base_url = fake_server
    # No route for /ClassicTV/channels → 404 → get_channels returns []
    client = ScheduleClient(base_url)
    assert client.channel_exists("sitcoms") is True


# ---------------------------------------------------------------------------
# check_server_health — task 2.2
# ---------------------------------------------------------------------------

from unittest.mock import patch


def test_check_server_health_returns_true_on_200():
    """When _get_json returns a dict, check_server_health returns True."""
    client = ScheduleClient("http://127.0.0.1:1")
    with patch.object(client, "_get_json", return_value={"ServerName": "Jellyfin"}):
        assert client.check_server_health() is True


def test_check_server_health_returns_false_on_error():
    """When _get_json returns None (any error), check_server_health returns False."""
    client = ScheduleClient("http://127.0.0.1:1")
    with patch.object(client, "_get_json", return_value=None):
        assert client.check_server_health() is False


def test_check_server_health_uses_system_info_endpoint():
    """check_server_health must call _get_json with a URL ending in /System/Info/Public."""
    client = ScheduleClient("http://192.168.1.207:8096")
    with patch.object(client, "_get_json", return_value={}) as mock_get:
        client.check_server_health()
        called_url = mock_get.call_args[0][0]
        assert called_url.endswith("/System/Info/Public"), (
            f"Expected URL ending in /System/Info/Public, got: {called_url!r}"
        )


def test_stream_url_builds_direct_static_endpoint():
    """stream_url(item_id) returns the direct HTTP byte-range stream URL."""
    client = ScheduleClient("http://192.168.1.207:8096/")
    assert client.base_url == "http://192.168.1.207:8096"
    assert (
        client.stream_url("dbe859fdb18d0e3e8c309244954bc52e")
        == "http://192.168.1.207:8096/Videos/dbe859fdb18d0e3e8c309244954bc52e/stream?static=true"
    )
