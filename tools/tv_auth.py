#!/usr/bin/env python3
"""
tools/tv_auth.py — Jellyfin Admin cookie auth service for Caddy forward_auth.

Protects interactive pages on tv.dane.photos behind a Jellyfin administrator
login. Listens on 127.0.0.1:8898 and exposes:
  GET  /auth/check   — Caddy forward_auth endpoint (200 if valid admin cookie,
                       302 -> /auth/login for HTML, 401 otherwise)
  GET  /auth/login   — Login form
  POST /auth/login   — Authenticates against Jellyfin /Users/AuthenticateByName
                       and requires User.Policy.IsAdministrator == True
  GET  /auth/logout  — Clears session cookie and redirects to /auth/login
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("tv_auth")

COOKIE_NAME = "ctv_admin"
USER_COOKIE_NAME = "ctv_user"
SESSION_TTL_SECONDS = 30 * 24 * 3600  # 30 days

LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Classic TV — Sign In</title>
  <style>
    :root {
      --bg: #050708;
      --card: rgba(14, 18, 20, 0.96);
      --border: rgba(255, 255, 255, 0.14);
      --text: #eef2f3;
      --muted: #8b989d;
      --phosphor: #46ff7a;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
      background: radial-gradient(circle at center, #0d1512 0%, #050708 75%);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }
    .card {
      width: 100%;
      max-width: 390px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.75rem;
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.65);
    }
    h1 {
      margin: 0 0 0.25rem;
      font-family: "Courier New", Courier, monospace;
      font-size: 1.45rem;
      letter-spacing: 0.08em;
      color: var(--phosphor);
    }
    p {
      margin: 0 0 1.25rem;
      font-size: 0.9rem;
      color: var(--muted);
    }
    .field {
      display: flex;
      flex-direction: column;
      gap: 0.35rem;
      margin-bottom: 0.95rem;
    }
    label { font-size: 0.8rem; color: var(--muted); }
    input {
      background: rgba(255, 255, 255, 0.06);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.6rem 0.75rem;
      color: var(--text);
      font-size: 0.95rem;
      outline: none;
    }
    input:focus { border-color: var(--phosphor); }
    button {
      width: 100%;
      padding: 0.7rem 1rem;
      border: none;
      border-radius: 6px;
      background: var(--phosphor);
      color: #031508;
      font-weight: 700;
      font-size: 0.95rem;
      cursor: pointer;
    }
    .error {
      margin-top: 0.8rem;
      color: #ff6b6b;
      font-size: 0.85rem;
      min-height: 1.1em;
    }
    .secondary-btn {
      margin-top: 0.65rem;
      background: rgba(255, 255, 255, 0.08);
      color: var(--text);
      border: 1px solid var(--border);
    }
    .secondary-btn:hover { background: rgba(255, 255, 255, 0.14); }
    .qc-box {
      margin-top: 0.85rem;
      padding: 0.85rem;
      border: 1px dashed rgba(70, 255, 122, 0.45);
      border-radius: 8px;
      background: rgba(70, 255, 122, 0.06);
      text-align: center;
      cursor: pointer;
    }
    .qc-box.hidden { display: none; }
    .qc-code {
      font-family: "Courier New", Courier, monospace;
      font-size: 1.7rem;
      font-weight: 700;
      letter-spacing: 0.2em;
      color: var(--phosphor);
    }
    .qc-hint {
      margin-top: 0.3rem;
      font-size: 0.72rem;
      color: var(--muted);
      letter-spacing: 0.04em;
    }
  </style>
</head>
<body>
  <form class="card" method="POST" action="/auth/login">
    <h1>CLASSIC TV</h1>
    <p>__SUBTITLE__</p>
    <input type="hidden" id="auth-next" name="next" value="__NEXT_URL__">
    <input type="hidden" id="auth-mode" name="mode" value="__MODE__">
    <div class="field">
      <label for="username">Username</label>
      <input id="username" name="username" type="text" required autocomplete="username" autofocus>
    </div>
    <div class="field">
      <label for="password">Password</label>
      <input id="password" name="password" type="password" autocomplete="current-password">
    </div>
    <button type="submit">Sign In</button>
    <button id="auth-qc-btn" type="button" class="secondary-btn">Sign In with Quick Connect</button>
    <div id="auth-qc-box" class="qc-box hidden" tabindex="0" role="button" aria-label="Quick Connect Code">
      <div id="auth-qc-code" class="qc-code">------</div>
      <div id="auth-qc-hint" class="qc-hint">ENTER CODE IN JELLYFIN → QUICK CONNECT (CLICK TO COPY)</div>
    </div>
    <div id="auth-error" class="error">__ERROR_MSG__</div>
  </form>
  <script src="/auth/jellyfin_login.js"></script>
  <script>
    (() => {
      if (!window.ClassicTVLogin) return;
      const nextUrl = document.getElementById('auth-next').value || '/';
      const mode = document.getElementById('auth-mode').value || 'admin';
      const errEl = document.getElementById('auth-error');
      window.ClassicTVLogin.bindQuickConnect({
        getServerUrl: () => '/auth/quick-connect/' + encodeURIComponent(mode),
        getAuthHeader: () => 'MediaBrowser Client="ClassicTV Auth", Device="Web", DeviceId="ctv-auth-gate", Version="1.0.0"',
        qcBtn: document.getElementById('auth-qc-btn'),
        qcBox: document.getElementById('auth-qc-box'),
        qcCodeEl: document.getElementById('auth-qc-code'),
        qcHintEl: document.getElementById('auth-qc-hint'),
        errEl,
        idleButtonText: 'Sign In with Quick Connect',
        idleHintText: 'ENTER CODE IN JELLYFIN → QUICK CONNECT (CLICK TO COPY)',
        onSuccess: () => {
          window.location.href = nextUrl;
        },
      });
    })();
  </script>
</body>
</html>
"""


def load_or_create_secret(secret_path: Path) -> bytes:
    if secret_path.exists():
        raw = secret_path.read_text(encoding="utf-8").strip()
        if raw:
            return raw.encode("utf-8")
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_hex(32)
    secret_path.write_text(secret, encoding="utf-8")
    os.chmod(secret_path, 0o600)
    return secret.encode("utf-8")


def sign_token(payload: dict, secret: bytes) -> str:
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    sig = hmac.new(secret, body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def verify_token(token: str, secret: bytes) -> dict | None:
    if "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = hmac.new(secret, body.encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        padded = body + "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        if int(data.get("exp", 0)) < int(time.time()):
            return None
        return data
    except Exception:
        return None


def safe_next_url(raw: str | None) -> str:
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    if raw.startswith("/auth/"):
        return "/"
    return raw


def _user_result_from_auth(
    data: dict,
    fallback_username: str,
    require_admin: bool,
) -> tuple[bool, dict | str]:
    user = data.get("User") or {}
    policy = user.get("Policy") or {}
    is_admin = bool(policy.get("IsAdministrator"))
    if require_admin and not is_admin:
        return False, "Administrator privileges required."
    return True, {
        "userId": user.get("Id", ""),
        "username": user.get("Name", fallback_username),
        "isAdmin": is_admin,
    }


def authenticate_jellyfin_user(
    jellyfin_url: str,
    username: str,
    password: str,
    require_admin: bool = True,
) -> tuple[bool, dict | str]:
    url = f"{jellyfin_url.rstrip('/')}/Users/AuthenticateByName"
    body = json.dumps({"Username": username, "Pw": password}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": 'MediaBrowser Client="ClassicTV Auth", Device="Web", DeviceId="ctv-auth-gate", Version="1.0.0"',
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401):
            return False, "Invalid username or password."
        return False, f"Jellyfin returned HTTP {exc.code}."
    except Exception as exc:
        return False, f"Unable to reach Jellyfin: {exc}"

    return _user_result_from_auth(data, username, require_admin)


def proxy_jellyfin_json(
    jellyfin_url: str,
    path_and_query: str,
    method: str = "GET",
    body: bytes | None = None,
    auth_header: str = "",
) -> tuple[int, bytes]:
    url = f"{jellyfin_url.rstrip('/')}{path_and_query}"
    hdrs = {
        "Authorization": auth_header
        or 'MediaBrowser Client="ClassicTV Auth", Device="Web", DeviceId="ctv-auth-gate", Version="1.0.0"',
    }
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:
        return 502, json.dumps({"error": str(exc)}).encode("utf-8")


def authenticate_jellyfin_admin(jellyfin_url: str, username: str, password: str) -> tuple[bool, dict | str]:
    return authenticate_jellyfin_user(jellyfin_url, username, password, require_admin=True)


_TOKEN_CACHE: dict[str, tuple[float, dict]] = {}
_TOKEN_CACHE_TTL = 60.0


def verify_jellyfin_token(jellyfin_url: str, auth_header: str) -> dict | None:
    """Validate a Jellyfin Authorization / MediaBrowser Token header via /Users/Me."""
    if not auth_header or "Token=" not in auth_header:
        return None
    now = time.time()
    cached = _TOKEN_CACHE.get(auth_header)
    if cached and cached[0] > now:
        return cached[1]

    url = f"{jellyfin_url.rstrip('/')}/Users/Me"
    req = urllib.request.Request(url, method="GET", headers={"Authorization": auth_header})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            user = json.loads(resp.read().decode("utf-8"))
    except Exception:
        _TOKEN_CACHE.pop(auth_header, None)
        return None

    info = {"userId": user.get("Id", ""), "username": user.get("Name", "")}
    if not info["userId"]:
        return None
    _TOKEN_CACHE[auth_header] = (now + _TOKEN_CACHE_TTL, info)
    return info


class AuthHandler(BaseHTTPRequestHandler):
    secret: bytes = b""
    jellyfin_url: str = "http://127.0.0.1:8096"

    def log_message(self, fmt: str, *args) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def _is_https(self) -> bool:
        proto = self.headers.get("X-Forwarded-Proto", "").lower()
        return proto == "https"

    def _get_session(self, cookie_name: str = COOKIE_NAME) -> dict | None:
        raw_cookie = self.headers.get("Cookie", "")
        if not raw_cookie:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(raw_cookie)
        except Exception:
            return None
        morsel = cookie.get(cookie_name)
        if not morsel:
            return None
        return verify_token(morsel.value, self.secret)

    def _get_any_session(self) -> dict | None:
        return self._get_session(COOKIE_NAME) or self._get_session(USER_COOKIE_NAME)

    def _render_login(self, next_url: str, mode: str, error_msg: str = "") -> str:
        subtitle = (
            "Sign in with your Jellyfin account to watch Classic TV."
            if mode == "user"
            else "Sign in with your Jellyfin administrator account."
        )
        return (
            LOGIN_HTML.replace("__NEXT_URL__", html.escape(next_url, quote=True))
            .replace("__MODE__", html.escape(mode, quote=True))
            .replace("__SUBTITLE__", subtitle)
            .replace("__ERROR_MSG__", html.escape(error_msg))
        )

    def _send_html(self, status: int, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(
        self,
        location: str,
        status: int = 302,
        set_cookies: list[str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        for c in set_cookies or []:
            self.send_header("Set-Cookie", c)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_json(self, status: int, raw: bytes, set_cookies: list[str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for c in set_cookies or []:
            self.send_header("Set-Cookie", c)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _build_session_cookies(self, result: dict) -> list[str]:
        payload = {
            "userId": result["userId"],
            "username": result["username"],
            "exp": int(time.time()) + SESSION_TTL_SECONDS,
        }
        token = sign_token(payload, self.secret)
        secure = "; Secure" if self._is_https() else ""
        cookies = [
            f"{USER_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={SESSION_TTL_SECONDS}{secure}"
        ]
        if result.get("isAdmin"):
            cookies.append(
                f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; "
                f"Max-Age={SESSION_TTL_SECONDS}{secure}"
            )
        return cookies

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/auth/check-user":
            session = self._get_any_session()
            if session:
                self.send_response(200)
                self.send_header("X-ClassicTV-User", session.get("username", ""))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            auth_hdr = self.headers.get("Authorization", "")
            if not auth_hdr and self.headers.get("X-Emby-Token"):
                auth_hdr = f'MediaBrowser Token="{self.headers.get("X-Emby-Token")}"'
            user_info = verify_jellyfin_token(self.jellyfin_url, auth_hdr)
            if user_info:
                self.send_response(200)
                self.send_header("X-ClassicTV-User", user_info.get("username", ""))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            if (qs.get("redirect") or [""])[0] == "1" and "text/html" in self.headers.get("Accept", ""):
                fwd_uri = self.headers.get("X-Forwarded-Uri", "/watch/")
                target = "/auth/login?mode=user&next=" + urllib.parse.quote(safe_next_url(fwd_uri), safe="/")
                self._redirect(target, status=302)
                return

            self.send_response(401)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/auth/check":
            session = self._get_session(COOKIE_NAME)
            if session:
                self.send_response(200)
                self.send_header("X-ClassicTV-User", session.get("username", ""))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            accept = self.headers.get("Accept", "")
            fwd_uri = self.headers.get("X-Forwarded-Uri", "/")
            if "text/html" in accept:
                target = "/auth/login?next=" + urllib.parse.quote(safe_next_url(fwd_uri), safe="/")
                self._redirect(target, status=302)
            else:
                self.send_response(401)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
            return

        if path == "/auth/login":
            mode = "user" if (qs.get("mode") or [""])[0] == "user" else "admin"
            next_url = safe_next_url((qs.get("next") or ["/"])[0])
            session = self._get_any_session() if mode == "user" else self._get_session(COOKIE_NAME)
            if session:
                self._redirect(next_url, status=302)
                return
            self._send_html(200, self._render_login(next_url, mode))
            return

        if path == "/auth/jellyfin_login.js":
            js_path = Path(__file__).resolve().parent.parent / "web" / "shared" / "jellyfin_login.js"
            raw = js_path.read_bytes() if js_path.exists() else b""
            self.send_response(200 if raw else 404)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if raw:
                self.wfile.write(raw)
            return

        if path.startswith("/auth/quick-connect/") and path.endswith("/QuickConnect/Connect"):
            secret_q = (qs.get("secret") or [""])[0]
            status, raw = proxy_jellyfin_json(
                self.jellyfin_url,
                f"/QuickConnect/Connect?secret={urllib.parse.quote(secret_q)}",
                method="GET",
                auth_header=self.headers.get("Authorization", ""),
            )
            self._send_json(status, raw)
            return

        if path == "/auth/logout":
            secure = "; Secure" if self._is_https() else ""
            clear_cookies = [
                f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}",
                f"{USER_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0{secure}",
            ]
            self._redirect("/auth/login", status=302, set_cookies=clear_cookies)
            return

        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/auth/quick-connect/"):
            parts = [p for p in parsed.path.split("/") if p]
            # /auth/quick-connect/<mode>/QuickConnect/Initiate or .../Users/AuthenticateWithQuickConnect
            mode = parts[2] if len(parts) >= 3 and parts[2] == "user" else "admin"
            tail = "/" + "/".join(parts[3:]) if len(parts) >= 4 else ""
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw_body = self.rfile.read(min(length, 16384)) if length > 0 else None

            if tail == "/QuickConnect/Initiate":
                status, raw = proxy_jellyfin_json(
                    self.jellyfin_url,
                    "/QuickConnect/Initiate",
                    method="POST",
                    auth_header=self.headers.get("Authorization", ""),
                )
                self._send_json(status, raw)
                return

            if tail == "/Users/AuthenticateWithQuickConnect":
                status, raw = proxy_jellyfin_json(
                    self.jellyfin_url,
                    "/Users/AuthenticateWithQuickConnect",
                    method="POST",
                    body=raw_body or b"{}",
                    auth_header=self.headers.get("Authorization", ""),
                )
                if status != 200:
                    self._send_json(status, raw)
                    return
                try:
                    data = json.loads(raw.decode("utf-8"))
                except Exception:
                    self._send_json(502, b'{"error":"Invalid Jellyfin response"}')
                    return
                ok, result = _user_result_from_auth(data, "", require_admin=(mode != "user"))
                if not ok:
                    self._send_json(403, json.dumps({"error": str(result)}).encode("utf-8"))
                    return
                self._send_json(200, raw, set_cookies=self._build_session_cookies(result))
                return

            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if parsed.path != "/auth/login":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", "0") or 0)
        raw_body = self.rfile.read(min(length, 16384)).decode("utf-8", errors="replace")
        form = urllib.parse.parse_qs(raw_body, keep_blank_values=True)
        username = (form.get("username") or [""])[0].strip()
        password = (form.get("password") or [""])[0]
        next_url = safe_next_url((form.get("next") or ["/"])[0])
        mode = "user" if (form.get("mode") or [""])[0] == "user" else "admin"

        ok, result = authenticate_jellyfin_user(
            self.jellyfin_url,
            username,
            password,
            require_admin=(mode != "user"),
        )
        if not ok:
            status = 403 if "Administrator" in str(result) else 401
            self._send_html(status, self._render_login(next_url, mode, str(result)))
            return

        self._redirect(next_url, status=303, set_cookies=self._build_session_cookies(result))


def main() -> int:
    parser = argparse.ArgumentParser(description="Classic TV Admin Auth Service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8898)
    parser.add_argument("--jellyfin-url", default="http://127.0.0.1:8096")
    parser.add_argument(
        "--secret-file",
        default=os.path.expanduser("~/.config/classictv/auth_secret"),
    )
    args = parser.parse_args()

    AuthHandler.secret = load_or_create_secret(Path(args.secret_file))
    AuthHandler.jellyfin_url = args.jellyfin_url

    server = ThreadingHTTPServer((args.host, args.port), AuthHandler)
    logger.info("tv_auth listening on %s:%d (jellyfin=%s)", args.host, args.port, args.jellyfin_url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
