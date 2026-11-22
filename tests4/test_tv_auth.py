"""Unit tests for tools/tv_auth.py route-guarding and session cookies."""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import tv_auth  # noqa: E402


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@pytest.fixture()
def auth_env():
    """Start a mock Jellyfin server and a live tv_auth server."""
    tv_auth._TOKEN_CACHE.clear()

    class JellyfinMock(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def do_POST(self):
            if self.path == "/Users/AuthenticateByName":
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                u = body.get("Username")
                if u == "admin" and body.get("Pw") == "pw":
                    resp = {"User": {"Id": "u-admin", "Name": "admin", "Policy": {"IsAdministrator": True}}}
                elif u == "rachel" and body.get("Pw") == "pw":
                    resp = {"User": {"Id": "u-rachel", "Name": "rachel", "Policy": {"IsAdministrator": False}}}
                else:
                    self.send_response(401)
                    self.end_headers()
                    return
                raw = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if self.path == "/QuickConnect/Initiate":
                raw = json.dumps({"Secret": "sec-1", "Code": "654321"}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if self.path == "/Users/AuthenticateWithQuickConnect":
                length = int(self.headers.get("Content-Length", "0") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                if body.get("Secret") == "sec-user":
                    resp = {"User": {"Id": "u-rachel", "Name": "rachel", "Policy": {"IsAdministrator": False}}}
                elif body.get("Secret") == "sec-admin":
                    resp = {"User": {"Id": "u-admin", "Name": "admin", "Policy": {"IsAdministrator": True}}}
                else:
                    self.send_response(401)
                    self.end_headers()
                    return
                raw = json.dumps(resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            self.send_response(404)
            self.end_headers()

        def do_GET(self):
            if self.path.startswith("/QuickConnect/Connect"):
                raw = json.dumps({"Authenticated": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if self.path == "/Users/Me":
                auth = self.headers.get("Authorization", "")
                if "valid-token" in auth:
                    raw = json.dumps({"Id": "u-rachel", "Name": "rachel"}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

    jf_server = ThreadingHTTPServer(("127.0.0.1", 0), JellyfinMock)
    jf_port = jf_server.server_address[1]
    jf_thread = threading.Thread(target=jf_server.serve_forever, daemon=True)
    jf_thread.start()

    tv_auth.AuthHandler.secret = b"test-secret-key"
    tv_auth.AuthHandler.jellyfin_url = f"http://127.0.0.1:{jf_port}"
    auth_server = ThreadingHTTPServer(("127.0.0.1", 0), tv_auth.AuthHandler)
    auth_port = auth_server.server_address[1]
    auth_thread = threading.Thread(target=auth_server.serve_forever, daemon=True)
    auth_thread.start()

    opener = urllib.request.build_opener(_NoRedirect())
    try:
        yield f"http://127.0.0.1:{auth_port}", opener
    finally:
        auth_server.shutdown()
        auth_server.server_close()
        jf_server.shutdown()
        jf_server.server_close()


def _request(opener, url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        resp = opener.open(req, timeout=5)
        return resp.status, resp.headers, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read().decode("utf-8")


def _extract_cookies(headers) -> str:
    parts = []
    for val in headers.get_all("Set-Cookie") or []:
        parts.append(val.split(";", 1)[0])
    return "; ".join(parts)


def test_unauthenticated_checks(auth_env):
    base, opener = auth_env
    status, _, _ = _request(opener, f"{base}/auth/check")
    assert status == 401

    status, _, _ = _request(opener, f"{base}/auth/check-user")
    assert status == 401

    status, hdrs, _ = _request(
        opener,
        f"{base}/auth/check-user?redirect=1",
        headers={"Accept": "text/html", "X-Forwarded-Uri": "/watch/"},
    )
    assert status == 302
    assert hdrs["Location"] == "/auth/login?mode=user&next=/watch/"


def test_non_admin_rejected_from_admin_login_allowed_in_user_mode(auth_env):
    base, opener = auth_env

    # Admin login rejects non-admin
    body = urllib.parse.urlencode({"username": "rachel", "password": "pw", "next": "/manage/"}).encode()
    status, _, _ = _request(opener, f"{base}/auth/login", method="POST", data=body)
    assert status == 403

    # User login accepts non-admin and sets only ctv_user
    body = urllib.parse.urlencode({"username": "rachel", "password": "pw", "next": "/watch/", "mode": "user"}).encode()
    status, hdrs, _ = _request(opener, f"{base}/auth/login", method="POST", data=body)
    assert status == 303
    assert hdrs["Location"] == "/watch/"
    cookie_hdr = _extract_cookies(hdrs)
    assert "ctv_user=" in cookie_hdr
    assert "ctv_admin=" not in cookie_hdr

    # ctv_user passes /auth/check-user...
    status, hdrs, _ = _request(opener, f"{base}/auth/check-user?redirect=1", headers={"Cookie": cookie_hdr})
    assert status == 200
    assert hdrs["X-ClassicTV-User"] == "rachel"

    # ...but is still blocked from admin /auth/check
    status, _, _ = _request(opener, f"{base}/auth/check", headers={"Cookie": cookie_hdr})
    assert status == 401


def test_admin_login_sets_both_cookies_and_passes_all_checks(auth_env):
    base, opener = auth_env
    body = urllib.parse.urlencode({"username": "admin", "password": "pw", "next": "/"}).encode()
    status, hdrs, _ = _request(opener, f"{base}/auth/login", method="POST", data=body)
    assert status == 303
    cookie_hdr = _extract_cookies(hdrs)
    assert "ctv_admin=" in cookie_hdr
    assert "ctv_user=" in cookie_hdr

    status, _, _ = _request(opener, f"{base}/auth/check", headers={"Cookie": cookie_hdr})
    assert status == 200
    status, _, _ = _request(opener, f"{base}/auth/check-user", headers={"Cookie": cookie_hdr})
    assert status == 200


def test_jellyfin_bearer_token_check_user(auth_env):
    base, opener = auth_env
    status, hdrs, _ = _request(
        opener,
        f"{base}/auth/check-user",
        headers={"Authorization": 'MediaBrowser Client="Test", Token="valid-token"'},
    )
    assert status == 200
    assert hdrs["X-ClassicTV-User"] == "rachel"

    status, _, _ = _request(
        opener,
        f"{base}/auth/check-user",
        headers={"Authorization": 'MediaBrowser Client="Test", Token="bad-token"'},
    )
    assert status == 401


def test_quick_connect_proxy_and_user_cookie(auth_env):
    base, opener = auth_env
    # Login page embeds Quick Connect UI + helper script
    status, _, body = _request(opener, f"{base}/auth/login?mode=user&next=/watch")
    assert status == 200
    assert "auth-qc-btn" in body and "/auth/jellyfin_login.js" in body

    status, _, js_body = _request(opener, f"{base}/auth/jellyfin_login.js")
    assert status == 200 and "ClassicTVLogin" in js_body

    status, _, init_body = _request(
        opener, f"{base}/auth/quick-connect/user/QuickConnect/Initiate", method="POST", data=b""
    )
    assert status == 200 and json.loads(init_body)["Code"] == "654321"

    status, _, chk_body = _request(
        opener, f"{base}/auth/quick-connect/user/QuickConnect/Connect?secret=sec-1"
    )
    assert status == 200 and json.loads(chk_body)["Authenticated"] is True

    # Non-admin rejected in admin mode, accepted in user mode
    status, _, _ = _request(
        opener,
        f"{base}/auth/quick-connect/admin/Users/AuthenticateWithQuickConnect",
        method="POST",
        data=json.dumps({"Secret": "sec-user"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert status == 403

    status, hdrs, _ = _request(
        opener,
        f"{base}/auth/quick-connect/user/Users/AuthenticateWithQuickConnect",
        method="POST",
        data=json.dumps({"Secret": "sec-user"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert status == 200
    assert f"{tv_auth.USER_COOKIE_NAME}=" in hdrs.get("Set-Cookie", "")
