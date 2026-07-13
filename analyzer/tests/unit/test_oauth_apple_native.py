"""Unit tests for the native "Sign in with Apple" flow + Clerk FAPI bridge.

These cover ``oauth_client.run_apple_native_flow`` (orchestration) and the two
bridge helpers ``_fapi_apple_sign_in`` / ``_authorize_with_session`` (parsing),
with no real network or PyObjC. The macOS-native credential capture lives in
``mac_apple_signin`` and is exercised only on-device (TestFlight); here we stub
it. The design contract under test: the Apple flow returns the SAME
``{"ok": bool, "bundle"|"error": ...}`` shape as the loopback/ASWeb transports,
so ``api_bridge``'s worker persists and notifies identically.
"""

import email.message
import io
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import oauth_client

pytestmark = pytest.mark.unit


class _FakeApple:
    """Stand-in for the mac_apple_signin module's ``authenticate``."""

    def __init__(self, cred):
        self._cred = cred

    def authenticate(self, *, timeout=None, cancel_event=None):
        return dict(self._cred)


def _token_ok(*_a, **_k):
    return {
        "access_token":  "AT",
        "refresh_token": "RT",
        "expires_in":    3600,
        "token_type":    "Bearer",
        "scope":         "openid email profile",
    }


def _echo_state_authorize(jar, authorize_url):
    """Fake ``_authorize_with_session`` that returns the code + the SAME state
    embedded in the authorize URL, so the flow's state check passes."""
    import urllib.parse
    st = urllib.parse.parse_qs(urllib.parse.urlparse(authorize_url).query).get("state", [None])[0]
    return {"code": "CODE", "state": st}


# ── run_apple_native_flow orchestration ──────────────────────────────────────

def test_apple_native_happy_path(monkeypatch):
    apple = _FakeApple({"identity_token": "idtok", "raw_nonce": "n"})
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in", lambda jar, tok: {"ok": True})
    monkeypatch.setattr(oauth_client, "_authorize_with_session", _echo_state_authorize)
    monkeypatch.setattr(oauth_client, "exchange_code", _token_ok)

    res = oauth_client.run_apple_native_flow(apple)

    assert res["ok"] is True
    assert res["bundle"]["access_token"] == "AT"
    assert res["bundle"]["refresh_token"] == "RT"


def test_apple_native_exchange_uses_custom_scheme_redirect(monkeypatch):
    """The token exchange must use the kestrel:// redirect that authorize echoed —
    matching what Clerk saw — or Clerk rejects with invalid_grant."""
    seen = {}
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in", lambda jar, tok: {"ok": True})
    monkeypatch.setattr(oauth_client, "_authorize_with_session", _echo_state_authorize)

    def _capture_exchange(code, verifier, redirect_uri):
        seen["redirect_uri"] = redirect_uri
        seen["code"] = code
        return _token_ok()

    monkeypatch.setattr(oauth_client, "exchange_code", _capture_exchange)
    res = oauth_client.run_apple_native_flow(_FakeApple({"identity_token": "idtok"}))

    assert res["ok"] is True
    assert seen["redirect_uri"] == oauth_client.MAC_REDIRECT_URI == "kestrel://callback"
    assert seen["code"] == "CODE"


def test_apple_native_credential_cancelled(monkeypatch):
    # Never touch the network if Apple itself reported cancellation.
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in",
                        lambda *a, **k: pytest.fail("must not call FAPI after cancel"))
    res = oauth_client.run_apple_native_flow(_FakeApple({"error": "cancelled"}))
    assert res["ok"] is False
    assert res["error"] == "cancelled"


def test_apple_native_missing_identity_token(monkeypatch):
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in",
                        lambda *a, **k: pytest.fail("must not call FAPI without a token"))
    res = oauth_client.run_apple_native_flow(_FakeApple({"identity_token": ""}))
    assert res["ok"] is False
    assert res["error"] == "apple_no_identity_token"


def test_apple_native_fapi_rejected(monkeypatch):
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in",
                        lambda jar, tok: {"error": "apple_signin_rejected", "error_description": "bad token"})
    monkeypatch.setattr(oauth_client, "_authorize_with_session",
                        lambda *a, **k: pytest.fail("must not authorize after a rejected sign-in"))
    res = oauth_client.run_apple_native_flow(_FakeApple({"identity_token": "idtok"}))
    assert res["ok"] is False
    assert res["error"] == "apple_signin_rejected"


def test_apple_native_authorize_no_code(monkeypatch):
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in", lambda jar, tok: {"ok": True})
    monkeypatch.setattr(oauth_client, "_authorize_with_session",
                        lambda jar, url: {"error": "apple_bridge_no_code", "error_description": "no code"})
    res = oauth_client.run_apple_native_flow(_FakeApple({"identity_token": "idtok"}))
    assert res["ok"] is False
    assert res["error"] == "apple_bridge_no_code"


def test_apple_native_state_mismatch(monkeypatch):
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in", lambda jar, tok: {"ok": True})
    monkeypatch.setattr(oauth_client, "_authorize_with_session",
                        lambda jar, url: {"code": "CODE", "state": "WRONG"})
    monkeypatch.setattr(oauth_client, "exchange_code",
                        lambda *a, **k: pytest.fail("must not exchange on state mismatch"))
    res = oauth_client.run_apple_native_flow(_FakeApple({"identity_token": "idtok"}))
    assert res["ok"] is False
    assert res["error"] == "state_mismatch"


def test_apple_native_url_validator_blocks(monkeypatch):
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in", lambda jar, tok: {"ok": True})
    res = oauth_client.run_apple_native_flow(
        _FakeApple({"identity_token": "idtok"}), url_validator=lambda u: False
    )
    assert res["ok"] is False
    assert res["error"] == "unsafe_authorize_url"


# ── _authorize_with_session: redirect / Location parsing ─────────────────────

class _FakeResp:
    def __init__(self, status=200, body=b""):
        self.status = status
        self._body = body

    def getcode(self):
        return self.status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeOpener:
    def __init__(self, on_open):
        self._on_open = on_open

    def open(self, req, timeout=None):
        return self._on_open(req)


def _http_error(location=None, code=302, body=None):
    hdrs = email.message.Message()
    if location is not None:
        hdrs["Location"] = location
    fp = io.BytesIO(body) if body is not None else None
    return urllib.error.HTTPError("https://clerk.projectkestrel.org/oauth/authorize",
                                  code, "Found", hdrs, fp)


def test_authorize_with_session_extracts_code(monkeypatch):
    loc = "kestrel://callback?code=ABC123&state=STATEXYZ"

    def _on_open(req):
        raise _http_error(location=loc)

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._authorize_with_session(object(), "https://clerk.projectkestrel.org/oauth/authorize?x=1")
    assert res == {"code": "ABC123", "state": "STATEXYZ"}


def test_authorize_with_session_rejects_non_custom_scheme(monkeypatch):
    # A redirect to https:// (not our kestrel:// scheme) must not be treated as a code.
    def _on_open(req):
        raise _http_error(location="https://evil.example/callback?code=X")

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._authorize_with_session(object(), "https://clerk.projectkestrel.org/oauth/authorize")
    assert res.get("error") == "apple_bridge_bad_redirect"


def test_authorize_with_session_no_redirect_on_200(monkeypatch):
    def _on_open(req):
        return _FakeResp(status=200, body=b"<html>consent</html>")

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._authorize_with_session(object(), "https://clerk.projectkestrel.org/oauth/authorize")
    assert res.get("error") == "apple_bridge_no_redirect"


def test_authorize_with_session_error_query(monkeypatch):
    def _on_open(req):
        raise _http_error(location="kestrel://callback?error=access_denied&error_description=nope")

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._authorize_with_session(object(), "https://clerk.projectkestrel.org/oauth/authorize")
    assert res.get("error") == "apple_bridge_no_code"


# ── _fapi_apple_sign_in: strategy + response parsing ─────────────────────────

def test_fapi_apple_sign_in_ok(monkeypatch):
    captured = {}

    def _on_open(req):
        captured["body"] = req.data.decode("ascii")
        captured["url"] = req.full_url
        return _FakeResp(status=200, body=b'{"response":{"status":"complete"},"client":{}}')

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._fapi_apple_sign_in(object(), "IDTOKEN")
    assert res == {"ok": True}
    # Correct strategy + token on the Frontend-API call.
    assert "strategy=oauth_token_apple" in captured["body"]
    assert "token=IDTOKEN" in captured["body"]
    assert captured["url"] == oauth_client.CLERK_FAPI_SIGN_INS_URL


def test_fapi_apple_sign_in_soft_error(monkeypatch):
    # 200 but with an errors[] payload is still a failure.
    def _on_open(req):
        return _FakeResp(status=200, body=b'{"errors":[{"long_message":"Identity token invalid"}]}')

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._fapi_apple_sign_in(object(), "IDTOKEN")
    assert res.get("error") == "apple_signin_rejected"
    assert "invalid" in (res.get("error_description") or "").lower()


def test_fapi_apple_sign_in_http_error(monkeypatch):
    def _on_open(req):
        raise _http_error(code=403, body=b'{"errors":[{"message":"Unauthorized request"}]}')

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._fapi_apple_sign_in(object(), "IDTOKEN")
    assert res.get("error") == "apple_signin_rejected"
