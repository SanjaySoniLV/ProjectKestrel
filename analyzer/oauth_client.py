"""OAuth 2.0 Authorization Code + PKCE flow against Clerk OAuth Applications.

Self-contained — no pywebview, no JS, only Python stdlib. The desktop ``Api``
class in :mod:`api_bridge` delegates to ``run_authorization_flow`` and
``refresh_access_token`` here.

Why an external module: keeping the transport layer (PKCE generation, loopback
server, token exchange) out of ``api_bridge.py`` lets it be unit-tested
without spinning up pywebview and without the global JS-bridge state. Tests
under ``analyzer/tests/unit/`` can import ``oauth_client`` directly.

References:
- RFC 8252 (OAuth 2.0 for Native Apps)  — loopback redirect, no embedded WebView.
- RFC 7636 (PKCE)                       — S256 challenge method only.
- Clerk OAuth Applications              — JWT access tokens signed by the same
                                          JWKS as session tokens, so the
                                          downstream Workers validate them
                                          without any source change.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import json
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Callable, Optional

# ── Configuration ────────────────────────────────────────────────────────────

CLERK_AUTHORIZE_URL = "https://clerk.projectkestrel.org/oauth/authorize"
CLERK_TOKEN_URL     = "https://clerk.projectkestrel.org/oauth/token"
CLERK_REVOKE_URL    = "https://clerk.projectkestrel.org/oauth/revoke"

# Public OAuth client — no secret. PKCE replaces what a confidential client's
# secret would do. The client_id below is registered in the Clerk dashboard
# with redirect URI hardcoded to LOOPBACK_HOST:LOOPBACK_PORT/callback.
CLERK_CLIENT_ID     = "fiYWTMVayj2jbKvj"

# OpenID Connect base scopes — ``email`` and ``profile`` populate the
# corresponding claims on the access token's payload so the Workers (which
# only consume ``sub`` today) and the Auth Worker's ``/v1/me`` enrichment
# don't have to make an extra Clerk userinfo round-trip.
CLERK_SCOPES        = "openid email profile"

# 127.0.0.1 specifically — NOT "localhost". DNS edge cases and IPv6 ``::1``
# resolution surprises make literal-IPv4 the safer choice per RFC 8252 §7.3.
LOOPBACK_HOST       = "127.0.0.1"
LOOPBACK_PORT       = 53682
REDIRECT_URI        = f"http://{LOOPBACK_HOST}:{LOOPBACK_PORT}/callback"

# Hard wall on how long we wait for the user to complete sign-in in their
# browser. If they get distracted past this, the flow aborts cleanly and the
# lock in the bridge is released so the next click starts fresh.
FLOW_TIMEOUT_SEC    = 300

# Matches the existing 300-second pre-expiry buffer at api_bridge.py:1994.
# Any code path consuming an access token within this window of expiry
# triggers a synchronous refresh first.
REFRESH_BUFFER_SEC  = 300


# ── Internal helpers (no UI / no I/O beyond what's specified) ────────────────

def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _gen_pkce() -> tuple[str, str]:
    """Return ``(verifier, challenge)``. S256-only, 96-char verifier.

    Within RFC 7636's [43, 128] range. We use 64 random bytes → base64url ≈ 86
    chars, well within bounds and well above the 256-bit security floor.
    """
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def _gen_state() -> str:
    """CSRF nonce; matched on callback with constant-time compare."""
    return _b64url(secrets.token_bytes(32))


def _build_authorize_url(state: str, challenge: str) -> str:
    params = {
        "client_id":             CLERK_CLIENT_ID,
        "redirect_uri":          REDIRECT_URI,
        "response_type":         "code",
        "code_challenge":        challenge,
        "code_challenge_method": "S256",
        "state":                 state,
        "scope":                 CLERK_SCOPES,
    }
    return CLERK_AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


_CALLBACK_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    "<title>Project Kestrel — Sign-in complete</title>"
    "<style>body{font-family:system-ui,sans-serif;background:#0e1116;color:#e6e8ee;"
    "display:flex;align-items:center;justify-content:center;height:100vh;margin:0}"
    "main{max-width:520px;padding:32px;text-align:center}"
    "h1{font-size:22px;margin:0 0 8px}p{margin:0;opacity:.8}</style></head>"
    "<body><main><h1>Sign-in complete</h1>"
    "<p>You can close this tab and return to Project Kestrel.</p></main></body></html>"
)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """One-shot HTTP handler that captures the OAuth redirect.

    Only ``GET /callback?...`` is honored. Anything else gets 404. After a
    successful capture the server's ``result`` attribute is populated and the
    associated stop_event is set; subsequent requests get 410 Gone, though in
    practice the server is torn down before any second request arrives.
    """

    # Silence the default BaseHTTPRequestHandler stderr logger so the auth
    # code never ends up in stdout / log files via the request line.
    def log_message(self, format, *args):  # noqa: A002 — matches base class
        return

    def do_GET(self):
        server = self.server  # type: ignore[assignment]
        if getattr(server, "result", None) is not None:
            self.send_response(410)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Already handled")
            return

        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.rstrip("/") != "/callback":
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not found")
            return

        q = urllib.parse.parse_qs(parsed.query)
        result = {
            "code":              (q.get("code") or [None])[0],
            "state":             (q.get("state") or [None])[0],
            "error":             (q.get("error") or [None])[0],
            "error_description": (q.get("error_description") or [None])[0],
        }
        server.result = result  # type: ignore[attr-defined]

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(_CALLBACK_HTML.encode("utf-8"))

        # Tell run_authorization_flow it can stop waiting.
        event = getattr(server, "stop_event", None)
        if event is not None:
            event.set()


def _run_callback_server(stop_event: threading.Event) -> dict:
    """Bind, serve until ``stop_event`` set or timeout, return captured params.

    Returns ``{"error": "port_in_use"}`` if ``LOOPBACK_PORT`` is already taken
    — we deliberately don't probe alternates because every alternate would
    need to be pre-registered with Clerk for the redirect URI to match.
    """
    try:
        server = http.server.HTTPServer((LOOPBACK_HOST, LOOPBACK_PORT), _CallbackHandler)
    except OSError as e:
        if e.errno in (98, 10048, 48):  # EADDRINUSE on linux / win / mac
            return {"error": "port_in_use"}
        return {"error": "bind_failed", "error_description": str(e)}

    server.result = None       # type: ignore[attr-defined]
    server.stop_event = stop_event  # type: ignore[attr-defined]

    def _serve():
        try:
            while not stop_event.is_set():
                server.handle_request()
                if getattr(server, "result", None) is not None:
                    break
        finally:
            try:
                server.server_close()
            except Exception:
                pass

    server.socket.settimeout(0.5)
    t = threading.Thread(target=_serve, name="oauth-callback", daemon=True)
    t.start()

    stop_event.wait(FLOW_TIMEOUT_SEC)

    if not stop_event.is_set():
        # Timeout — wake the server thread by poking ourselves at the port.
        try:
            with socket.create_connection((LOOPBACK_HOST, LOOPBACK_PORT), timeout=0.5) as s:
                s.sendall(b"GET /timeout HTTP/1.0\r\n\r\n")
        except Exception:
            pass
        stop_event.set()
        t.join(timeout=2.0)
        return {"error": "timeout"}

    t.join(timeout=2.0)
    return getattr(server, "result", None) or {"error": "no_result"}


def _token_request(url: str, form: dict, timeout: float = 15.0) -> dict:
    """POST form-encoded body to a Clerk OAuth endpoint, return parsed JSON.

    Returns the response dict on 2xx, or ``{"error", "error_description"?}`` on
    4xx/5xx, or ``{"error": "network", ...}`` on transport failure. Never
    raises — callers branch on ``"error" in result``.
    """
    if not url.startswith("https://"):
        return {"error": "insecure_url", "error_description": url}

    body = urllib.parse.urlencode({k: v for k, v in form.items() if v is not None}).encode("ascii")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type":    "application/x-www-form-urlencoded",
            "Accept":          "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            # Cloudflare's default Bot Fight Mode blocks the stdlib UA
            # ("Python-urllib/3.x"). Identify ourselves honestly with a
            # named-client UA — Clerk's logs will attribute requests to
            # us, and the CF rule that triggers on "python" / "urllib" /
            # "curl" substrings doesn't fire on this string.
            "User-Agent":      "ProjectKestrel-Desktop/oauth (+https://projectkestrel.org)",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        # Capture Clerk's actual response body — without this, str(e) is just
        # "HTTP Error 403: Forbidden" and we have no idea WHY Clerk rejected.
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        parsed: dict = {}
        if err_body.lstrip().startswith("{"):
            try:
                parsed = json.loads(err_body)
            except Exception:
                pass
        # Always include the raw body in the description (truncated) so a 403
        # with a non-JSON / non-RFC6749 body still surfaces something useful.
        body_snippet = err_body.strip()[:400] if err_body else ""
        description = (
            parsed.get("error_description")
            or parsed.get("message")
            or body_snippet
            or str(e)
        )
        # Echo to stderr for the desktop log — _auth_debug_log_token paths only
        # see access tokens, not error bodies, so this is our only trace.
        try:
            print(
                f"[oauth] token endpoint error: HTTP {e.code} url={url} "
                f"body={body_snippet!r}",
                flush=True,
            )
        except Exception:
            pass
        return {
            "error":             parsed.get("error") or f"http_{e.code}",
            "error_description": description,
        }
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as e:
        return {"error": "network", "error_description": str(e)}

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        return {"error": "bad_response", "error_description": str(e)}


# ── Public API ───────────────────────────────────────────────────────────────

def exchange_code(code: str, verifier: str) -> dict:
    """Exchange the authorization ``code`` + PKCE ``verifier`` for a token bundle."""
    return _token_request(CLERK_TOKEN_URL, {
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  REDIRECT_URI,
        "client_id":     CLERK_CLIENT_ID,
        "code_verifier": verifier,
    })


def refresh_access_token(refresh_token: str) -> dict:
    """Mint a new access token from a refresh token. Public-client refresh."""
    return _token_request(CLERK_TOKEN_URL, {
        "grant_type":    "refresh_token",
        "refresh_token": refresh_token,
        "client_id":     CLERK_CLIENT_ID,
    })


def revoke_token(token: str) -> dict:
    """Best-effort revocation. 3-second timeout, failures ignored by callers."""
    return _token_request(CLERK_REVOKE_URL, {
        "token":     token,
        "client_id": CLERK_CLIENT_ID,
    }, timeout=3.0)


def build_bundle(token_response: dict, *, now: Optional[float] = None) -> dict:
    """Translate Clerk's token response into our keychain schema.

    Deliberately drops ``id_token``: nothing in the desktop app reads it, and
    the extra ~700 bytes pushes the bundle past Windows Credential Manager's
    2560-byte UTF-16 cap, forcing the plaintext file fallback. Dropping it
    keeps the durable secret (refresh_token) and the bearer (access_token)
    in the OS keystore on most installs.
    """
    t = now if now is not None else time.time()
    expires_in = token_response.get("expires_in") or 0
    try:
        expires_in = float(expires_in)
    except (TypeError, ValueError):
        expires_in = 0.0
    return {
        "access_token":  token_response.get("access_token") or "",
        "refresh_token": token_response.get("refresh_token") or "",
        "expires_at":    t + expires_in,
        "token_type":    token_response.get("token_type") or "Bearer",
        "scope":         token_response.get("scope") or CLERK_SCOPES,
        "obtained_at":   t,
    }


def run_authorization_flow(
    progress_cb: Optional[Callable[[str], None]] = None,
    *,
    url_validator: Optional[Callable[[str], bool]] = None,
) -> dict:
    """Run the full PKCE flow. Returns ``{"ok": bool, "bundle"|"error": ...}``.

    The ``url_validator`` parameter lets the bridge plug in its existing
    ``_is_safe_external_url`` allowlist as a belt-and-suspenders check on a
    URL we constructed ourselves. Defense in depth — the URL is always our
    hardcoded HTTPS authorize endpoint plus encoded params, but the gate is
    cheap and catches future regressions if the constants are ever changed.
    """
    def _progress(label: str) -> None:
        if progress_cb is not None:
            try:
                progress_cb(label)
            except Exception:
                pass

    _progress("starting")
    verifier, challenge = _gen_pkce()
    state = _gen_state()
    url = _build_authorize_url(state, challenge)

    if url_validator is not None and not url_validator(url):
        return {"ok": False, "error": "unsafe_authorize_url"}

    # Open in the system default browser — explicitly NOT pywebview. The
    # whole point of RFC 8252 is that the user signs in with their daily
    # browser, where their Clerk session may already be alive, and where
    # the desktop binary can't observe the password.
    try:
        webbrowser.open(url, new=2, autoraise=True)
    except Exception as e:
        return {"ok": False, "error": "browser_open_failed", "error_description": str(e)}

    _progress("awaiting_callback")
    stop_event = threading.Event()
    cb = _run_callback_server(stop_event)
    if cb.get("error"):
        return {"ok": False, "error": cb["error"], "error_description": cb.get("error_description")}

    if cb.get("error") is None and cb.get("code") is None:
        # Clerk returned ?error=...&state=... — surface it as-is.
        if cb.get("error_description"):
            return {"ok": False, "error": "no_code", "error_description": cb["error_description"]}
        return {"ok": False, "error": "no_code"}

    returned_state = cb.get("state") or ""
    if not hmac.compare_digest(returned_state, state):
        return {"ok": False, "error": "state_mismatch"}

    _progress("exchanging")
    tok = exchange_code(cb["code"], verifier)
    if tok.get("error"):
        return {
            "ok": False,
            "error":             tok["error"],
            "error_description": tok.get("error_description"),
        }

    bundle = build_bundle(tok)
    if not bundle.get("access_token"):
        return {"ok": False, "error": "missing_access_token"}

    _progress("done")
    return {"ok": True, "bundle": bundle}
