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

# certifi-backed TLS context — bare urlopen() fails CERTIFICATE_VERIFY_FAILED
# in a frozen macOS .app. See net_tls. Dual import for root-vs-package sys.path.
try:
    from net_tls import ssl_context as _ssl_context
except ImportError:  # pragma: no cover - package-style import path
    from analyzer.net_tls import ssl_context as _ssl_context

# ── Configuration ────────────────────────────────────────────────────────────

CLERK_AUTHORIZE_URL = "https://clerk.projectkestrel.org/oauth/authorize"
CLERK_TOKEN_URL     = "https://clerk.projectkestrel.org/oauth/token"
CLERK_REVOKE_URL    = "https://clerk.projectkestrel.org/oauth/revoke"

# Public OAuth client — no secret. PKCE replaces what a confidential client's
# secret would do. The client_id below is registered in the Clerk dashboard,
# and EVERY port in LOOPBACK_PORTS below must be present in that OAuth
# application's allowed redirect URLs (one ``http://127.0.0.1:<port>/callback``
# entry each) or Clerk will reject the authorize/token calls as a redirect_uri
# mismatch.
CLERK_CLIENT_ID     = "fiYWTMVayj2jbKvj"

# OpenID Connect base scopes — ``email`` and ``profile`` populate the
# corresponding claims on the access token's payload so the Workers (which
# only consume ``sub`` today) and the Auth Worker's ``/v1/me`` enrichment
# don't have to make an extra Clerk userinfo round-trip.
CLERK_SCOPES        = "openid email profile"

# 127.0.0.1 specifically — NOT "localhost". DNS edge cases and IPv6 ``::1``
# resolution surprises make literal-IPv4 the safer choice per RFC 8252 §7.3.
LOOPBACK_HOST       = "127.0.0.1"

# Ordered list of candidate loopback ports. We bind the first one that's
# available and build the redirect URI from it; ALL of them are registered as
# redirect URLs in the Clerk OAuth app (see CLERK_CLIENT_ID note above).
#
# Why a list and not a single fixed port: binding a *single* fixed port fails
# with WinError 10013 (WSAEACCES, "access forbidden") whenever that port lands
# inside one of Windows' reserved dynamic-port ranges. Hyper-V / WSL2 / Docker /
# WinNAT carve the IANA *dynamic/ephemeral* range (49152–65535) into reserved
# chunks, and — critically — those chunks are RE-RANDOMIZED on every boot
# (`netsh interface ipv4 show excludedportrange protocol=tcp`). The original
# single port 53682 lived in that range, so sign-in worked or failed depending
# purely on which ports the OS happened to reserve that boot — intermittent,
# machine-dependent, and invisible to CI (which seeds tokens and never binds).
#
# These candidates all sit in the IANA *user/registered* range (1024–49151),
# which WinNAT's boot-time dynamic reservations never touch, so they're immune
# to that failure. They're spread out (not consecutive) so an unrelated app
# squatting on one is unlikely to take its neighbours too. 53682 is kept LAST
# as a best-effort fallback: it's already registered with Clerk and is free on
# most boots, so it costs nothing to keep and only ever helps.
LOOPBACK_PORTS      = (17893, 27184, 37265, 47632, 53682)


def _redirect_uri(port: int) -> str:
    """The loopback redirect URI for ``port``. Must match a Clerk redirect URL."""
    return f"http://{LOOPBACK_HOST}:{port}/callback"

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


def _build_authorize_url(state: str, challenge: str, redirect_uri: str) -> str:
    params = {
        "client_id":             CLERK_CLIENT_ID,
        "redirect_uri":          redirect_uri,
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


# bind() failure codes that mean "this particular port is unusable, try the
# next candidate" rather than "the whole machine can't do loopback":
#   EADDRINUSE — something is already listening (98 linux / 48 mac / 10048 win)
#   EACCES     — permission denied; on Windows this is WSAEACCES / WinError
#                10013, raised when the port is inside a reserved dynamic range
#                (13 posix / 10013 win)
_PORT_RETRY_ERRNOS = frozenset({98, 48, 10048, 13, 10013})


def _bind_loopback() -> dict:
    """Bind the first available port in ``LOOPBACK_PORTS``.

    Returns ``{"server", "port"}`` on success, or ``{"error": ...}`` if no
    candidate could be bound:
      - ``port_in_use``     — at least one candidate was in use and none bound
                              (kept for backwards-compatible messaging).
      - ``no_loopback_port``— every candidate was reserved/forbidden/in-use.

    Each candidate is registered with Clerk, so whichever one binds yields a
    redirect URI Clerk will accept. See ``LOOPBACK_PORTS`` for why a single
    fixed port is insufficient on Windows.
    """
    last_error: Optional[OSError] = None
    saw_in_use = False
    for port in LOOPBACK_PORTS:
        try:
            server = http.server.HTTPServer((LOOPBACK_HOST, port), _CallbackHandler)
        except OSError as e:
            last_error = e
            code = e.errno
            win = getattr(e, "winerror", None)
            if code in (98, 48, 10048) or win in (10048,):
                saw_in_use = True
            if code in _PORT_RETRY_ERRNOS or win in (10048, 10013):
                continue  # this port is unusable — try the next candidate
            continue      # any other bind error: still try the remaining ports
        return {"server": server, "port": port}

    desc = (
        f"none of the {len(LOOPBACK_PORTS)} loopback ports could be bound "
        f"({', '.join(str(p) for p in LOOPBACK_PORTS)})"
        + (f"; last error: {last_error}" if last_error is not None else "")
    )
    if saw_in_use:
        return {"error": "port_in_use", "error_description": desc}
    return {"error": "no_loopback_port", "error_description": desc}


def _serve_callback(
    server: "http.server.HTTPServer",
    port: int,
    stop_event: threading.Event,
    cancel_event: Optional[threading.Event] = None,
) -> dict:
    """Serve the already-bound ``server`` until callback, cancel, or timeout.

    Always closes ``server`` before returning.

    ``cancel_event`` lets the caller abandon a flow the user never completed
    (e.g. they closed the browser tab and clicked "Sign In" again): when set,
    the loopback server is torn down promptly and ``{"error": "cancelled"}`` is
    returned, freeing the port for a fresh attempt instead of holding it for
    the full ``FLOW_TIMEOUT_SEC``.
    """
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

    # Wait for a callback (stop_event), an explicit cancel, or the timeout.
    # Poll in short slices so a cancel is honored promptly rather than only
    # after the full FLOW_TIMEOUT_SEC.
    deadline = time.monotonic() + FLOW_TIMEOUT_SEC
    cancelled = False
    while not stop_event.wait(0.25):
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            break
        if time.monotonic() >= deadline:
            break

    if not stop_event.is_set():
        # Cancelled or timed out — wake the server thread by poking ourselves
        # at the port, then tear it down so the port is free again.
        try:
            with socket.create_connection((LOOPBACK_HOST, port), timeout=0.5) as s:
                s.sendall(b"GET /timeout HTTP/1.0\r\n\r\n")
        except Exception:
            pass
        stop_event.set()
        t.join(timeout=2.0)
        return {"error": "cancelled"} if cancelled else {"error": "timeout"}

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
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_context()) as resp:
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
        # Echo to stderr for the desktop log — this error path doesn't carry
        # token contents, only the failure body, so it's safe to print.
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

def exchange_code(code: str, verifier: str, redirect_uri: str) -> dict:
    """Exchange the authorization ``code`` + PKCE ``verifier`` for a token bundle.

    ``redirect_uri`` MUST be byte-identical to the one sent on the authorize
    request (RFC 6749 §4.1.3) — i.e. derived from the same loopback port that
    actually bound — or Clerk rejects the exchange with ``invalid_grant``.
    """
    return _token_request(CLERK_TOKEN_URL, {
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  redirect_uri,
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
    cancel_event: Optional[threading.Event] = None,
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

    # Bind the loopback server FIRST — before building the authorize URL or
    # opening the browser — because the redirect URI (and thus the URL we send
    # the user to) depends on which candidate port actually binds. Binding
    # first also means a "no usable port" failure is reported immediately
    # instead of after the user has already been bounced to their browser.
    bound = _bind_loopback()
    if bound.get("error"):
        return {"ok": False, "error": bound["error"], "error_description": bound.get("error_description")}
    server = bound["server"]
    port = bound["port"]
    redirect_uri = _redirect_uri(port)

    # From here on the server holds an OS socket; close it on every early exit.
    try:
        url = _build_authorize_url(state, challenge, redirect_uri)

        if url_validator is not None and not url_validator(url):
            server.server_close()
            return {"ok": False, "error": "unsafe_authorize_url"}

        # Open in the system default browser — explicitly NOT pywebview. The
        # whole point of RFC 8252 is that the user signs in with their daily
        # browser, where their Clerk session may already be alive, and where
        # the desktop binary can't observe the password.
        try:
            webbrowser.open(url, new=2, autoraise=True)
        except Exception as e:
            server.server_close()
            return {"ok": False, "error": "browser_open_failed", "error_description": str(e)}

        if cancel_event is not None and cancel_event.is_set():
            server.server_close()
            return {"ok": False, "error": "cancelled"}

        _progress("awaiting_callback")
        stop_event = threading.Event()
        # _serve_callback owns the server from here and always closes it.
        cb = _serve_callback(server, port, stop_event, cancel_event)
    except BaseException:
        # Defensive: never leak the bound socket on an unexpected error before
        # _serve_callback took ownership.
        try:
            server.server_close()
        except Exception:
            pass
        raise

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
    tok = exchange_code(cb["code"], verifier, redirect_uri)
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
