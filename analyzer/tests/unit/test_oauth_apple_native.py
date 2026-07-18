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


def _mock_session_established(monkeypatch, *, token="hdr.body.sig"):
    """Wire the activate+mint FAPI helpers so an Apple flow reaches a minted
    Clerk session token and returns a clerk_session bundle."""
    monkeypatch.setattr(oauth_client, "_fapi_touch_session", lambda jar, sid: {"ok": True})
    monkeypatch.setattr(oauth_client, "_fapi_get_session_token", lambda jar, sid: token)


# ── run_apple_native_flow orchestration ──────────────────────────────────────

def test_apple_native_happy_path(monkeypatch):
    # Existing user: sign-in completes with a session id; the flow mints a Clerk
    # session JWT and returns a clerk_session bundle — no /oauth/authorize.
    apple = _FakeApple({"identity_token": "idtok", "raw_nonce": "n"})
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in",
                        lambda jar, tok: {"ok": True, "session_id": "sess_1"})
    _mock_session_established(monkeypatch, token="hdr.body.sig")

    res = oauth_client.run_apple_native_flow(apple)

    assert res["ok"] is True
    assert res["bundle"]["kind"] == "clerk_session"
    assert res["bundle"]["access_token"] == "hdr.body.sig"
    assert res["bundle"]["clerk_session_id"] == "sess_1"


def test_apple_native_no_session_token_fails(monkeypatch):
    # Session established but the mint returns nothing -> clean error, no bundle
    # (never hand back an empty credential).
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in",
                        lambda jar, tok: {"ok": True, "session_id": "sess_1"})
    monkeypatch.setattr(oauth_client, "_fapi_touch_session", lambda jar, sid: {"ok": True})
    monkeypatch.setattr(oauth_client, "_fapi_get_session_token", lambda jar, sid: None)
    res = oauth_client.run_apple_native_flow(_FakeApple({"identity_token": "idtok"}))
    assert res["ok"] is False
    assert res["error"] == "apple_no_session_token"


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
    monkeypatch.setattr(oauth_client, "_fapi_get_session_token",
                        lambda *a, **k: pytest.fail("must not mint a token after a rejected sign-in"))
    res = oauth_client.run_apple_native_flow(_FakeApple({"identity_token": "idtok"}))
    assert res["ok"] is False
    assert res["error"] == "apple_signin_rejected"


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
        return _FakeResp(status=200, body=b'{"response":{"status":"complete","created_session_id":"sess_A"},"client":{}}')

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._fapi_apple_sign_in(object(), "IDTOKEN")
    assert res == {"ok": True, "session_id": "sess_A"}
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


def test_fapi_apple_sign_in_transferable(monkeypatch):
    # A first-time Apple identity: token verified, but no user to sign in yet, so
    # Clerk marks the first-factor verification 'transferable'. We signal the
    # caller to convert it to a sign-up rather than reporting a bogus success.
    def _on_open(req):
        return _FakeResp(status=200, body=(
            b'{"response":{"status":"needs_identifier",'
            b'"first_factor_verification":{"status":"transferable",'
            b'"strategy":"oauth_token_apple"}},"client":{}}'
        ))

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._fapi_apple_sign_in(object(), "IDTOKEN")
    assert res == {"transfer": True}


def test_fapi_apple_sign_in_incomplete_is_error(monkeypatch):
    # 200, no errors[], but the sign-in neither completed nor is transferable —
    # the old code returned {"ok": True} here and authorize then bounced to the
    # sign-in page. Now it's a diagnosable error carrying the real status.
    def _on_open(req):
        return _FakeResp(status=200, body=b'{"response":{"status":"needs_second_factor"},"client":{}}')

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._fapi_apple_sign_in(object(), "IDTOKEN")
    assert res.get("error") == "apple_signin_incomplete"
    assert "needs_second_factor" in (res.get("error_description") or "")


# ── _fapi_apple_sign_up_transfer: create-account-on-first-Apple-sign-in ───────

def test_fapi_apple_sign_up_transfer_ok(monkeypatch):
    captured = {}

    def _on_open(req):
        captured["body"] = req.data.decode("ascii")
        captured["url"] = req.full_url
        return _FakeResp(status=200, body=b'{"response":{"status":"complete","created_session_id":"sess_B"},"client":{}}')

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._fapi_apple_sign_up_transfer(object())
    assert res == {"ok": True, "session_id": "sess_B"}
    # transfer=true against the sign_ups endpoint (Clerk pulls the verified Apple
    # identity from the transferable sign-in; no other fields needed).
    assert "transfer=true" in captured["body"]
    assert captured["url"] == oauth_client.CLERK_FAPI_SIGN_UPS_URL


def test_fapi_apple_sign_up_transfer_missing_requirements(monkeypatch):
    # The instance demands a field Apple didn't provide — surface it, don't
    # silently proceed to a session-less authorize.
    def _on_open(req):
        return _FakeResp(status=200, body=b'{"response":{"status":"missing_requirements"},"client":{}}')

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._fapi_apple_sign_up_transfer(object())
    assert res.get("error") == "apple_signup_incomplete"
    assert "missing_requirements" in (res.get("error_description") or "")


def test_fapi_apple_sign_up_transfer_rejection_is_retagged(monkeypatch):
    # Clerk *rejects* the transfer (errors[]), e.g. a consumed Apple nonce. The
    # shared _fapi_post tags every rejection apple_signin_rejected; the transfer
    # must re-tag it apple_signup_rejected and surface Clerk's machine code, so
    # the log localises the failure to the sign-up step, not the sign-in.
    def _on_open(req):
        return _FakeResp(status=200, body=(
            b'{"errors":[{"long_message":"failed security validations",'
            b'"code":"oauth_token_invalid"}]}'
        ))

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._fapi_apple_sign_up_transfer(object())
    assert res.get("error") == "apple_signup_rejected"
    assert "oauth_token_invalid" in (res.get("error_description") or "")


# ── session activation (setActive / touch) ───────────────────────────────────

def test_created_session_id_prefers_response_field():
    data = {"response": {"status": "complete", "created_session_id": "sess_X"}, "client": {}}
    assert oauth_client._created_session_id(data) == "sess_X"


def test_created_session_id_falls_back_to_client():
    data = {"response": {"status": "complete"},
            "client": {"last_active_session_id": "sess_Y", "sessions": [{"id": "sess_Z"}]}}
    assert oauth_client._created_session_id(data) == "sess_Y"


def test_created_session_id_absent_is_empty():
    assert oauth_client._created_session_id({"response": {"status": "complete"}, "client": {}}) == ""


def test_fapi_touch_session_hits_touch_endpoint(monkeypatch):
    captured = {}

    def _on_open(req):
        captured["url"] = req.full_url
        return _FakeResp(status=200, body=b'{"response":{"status":"active"}}')

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._fapi_touch_session(object(), "sess_A")
    assert res == {"ok": True}
    assert captured["url"] == oauth_client.CLERK_FAPI_SESSIONS_URL + "/sess_A/touch"


def test_apple_native_flow_activates_session(monkeypatch):
    # A completed sign-up returns a session id; the flow must touch (activate) it
    # AND mint the session token from it before returning the bundle.
    touched = {}

    def _touch(jar, sid):
        touched["sid"] = sid
        return {"ok": True}

    minted = {}

    def _mint(jar, sid):
        minted["sid"] = sid
        return "hdr.body.sig"

    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in",
                        lambda jar, tok: {"transfer": True})
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_up_transfer",
                        lambda jar: {"ok": True, "session_id": "sess_live"})
    monkeypatch.setattr(oauth_client, "_fapi_touch_session", _touch)
    monkeypatch.setattr(oauth_client, "_fapi_get_session_token", _mint)

    res = oauth_client.run_apple_native_flow(_FakeApple({"identity_token": "idtok"}))

    assert res["ok"] is True
    assert res["bundle"]["kind"] == "clerk_session"
    assert touched.get("sid") == "sess_live"  # activated the created session
    assert minted.get("sid") == "sess_live"   # and minted the session token


def test_fapi_get_session_token_parses_jwt(monkeypatch):
    captured = {}

    def _on_open(req):
        captured["url"] = req.full_url
        return _FakeResp(status=200, body=b'{"object":"token","jwt":"HDR.BODY.SIG"}')

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    jwt = oauth_client._fapi_get_session_token(object(), "sess_A")
    assert jwt == "HDR.BODY.SIG"
    assert captured["url"] == oauth_client.CLERK_FAPI_SESSIONS_URL + "/sess_A/tokens"


def test_set_session_cookie_installs_session(monkeypatch):
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    oauth_client._set_session_cookie(jar, "HDR.BODY.SIG")
    names = {c.name: c.value for c in jar}
    assert names.get("__session") == "HDR.BODY.SIG"


def test_set_session_cookie_matches_production_suffix():
    import http.cookiejar
    jar = http.cookiejar.CookieJar()
    # Seed a suffixed __client_uat like Clerk production sets, so the session
    # cookie is installed under the same suffix too.
    jar.set_cookie(http.cookiejar.Cookie(
        version=0, name="__client_uat_xccWTl", value="1",
        port=None, port_specified=False,
        domain="clerk.projectkestrel.org", domain_specified=True, domain_initial_dot=False,
        path="/", path_specified=True, secure=True, expires=None, discard=False,
        comment=None, comment_url=None, rest={}, rfc2109=False,
    ))
    oauth_client._set_session_cookie(jar, "JWT")
    names = {c.name for c in jar}
    assert "__session" in names
    assert "__session_xccWTl" in names


# ── _decode_jwt_claims: diagnostic-only unverified decode ─────────────────────

def test_decode_jwt_claims_reads_payload():
    import base64 as _b64
    import json as _json

    payload = {"aud": "org.projectkestrel.desktop", "iss": "https://appleid.apple.com", "nonce": "abc"}
    seg = _b64.urlsafe_b64encode(_json.dumps(payload).encode()).rstrip(b"=").decode()
    token = "hdr." + seg + ".sig"
    claims = oauth_client._decode_jwt_claims(token)
    assert claims["aud"] == "org.projectkestrel.desktop"
    assert claims["nonce"] == "abc"


def test_decode_jwt_claims_malformed_is_empty():
    assert oauth_client._decode_jwt_claims("not-a-jwt") == {}
    assert oauth_client._decode_jwt_claims("") == {}


# ── run_apple_native_flow: sign-up transfer branch ───────────────────────────

def test_apple_native_flow_transfers_new_identity(monkeypatch):
    # sign-in says 'transferable' → the flow runs the sign-up transfer, then mints
    # a session token from the created session and returns a clerk_session bundle.
    calls = []
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in", lambda jar, tok: {"transfer": True})
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_up_transfer",
                        lambda jar: calls.append("signup") or {"ok": True, "session_id": "sess_new"})
    _mock_session_established(monkeypatch, token="hdr.body.sig")

    res = oauth_client.run_apple_native_flow(_FakeApple({"identity_token": "idtok"}))

    assert res["ok"] is True
    assert res["bundle"]["kind"] == "clerk_session"
    assert res["bundle"]["clerk_session_id"] == "sess_new"
    assert calls == ["signup"]  # the transfer actually ran


def test_apple_native_flow_signup_transfer_failure_aborts(monkeypatch):
    # If account creation fails, surface it and never mint a session token.
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in", lambda jar, tok: {"transfer": True})
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_up_transfer",
                        lambda jar: {"error": "apple_signup_incomplete", "error_description": "status=missing_requirements"})
    monkeypatch.setattr(oauth_client, "_fapi_get_session_token",
                        lambda *a, **k: pytest.fail("must not mint a token after a failed sign-up transfer"))
    res = oauth_client.run_apple_native_flow(_FakeApple({"identity_token": "idtok"}))
    assert res["ok"] is False
    assert res["error"] == "apple_signup_incomplete"


# ── clerk_session bundle + re-mint (native Apple credential) ──────────────────

def test_build_session_bundle_reads_exp_from_jwt():
    import base64 as _b64
    import json as _json
    exp = 1893456000  # fixed far-future timestamp
    seg = _b64.urlsafe_b64encode(_json.dumps({"exp": exp, "sub": "user_1"}).encode()).rstrip(b"=").decode()
    jwt = "hdr." + seg + ".sig"
    b = oauth_client.build_session_bundle(jwt, "CLIENTCOOKIE", "sess_9", now=1000.0)
    assert b["kind"] == "clerk_session"
    assert b["access_token"] == jwt
    assert b["expires_at"] == float(exp)
    assert b["clerk_client"] == "CLIENTCOOKIE"
    assert b["clerk_session_id"] == "sess_9"
    assert b["refresh_token"] == ""


def test_build_session_bundle_falls_back_when_no_exp():
    b = oauth_client.build_session_bundle("no.jwt.here", "c", "s", now=1000.0)
    # Unparseable exp -> a short forward window from `now`, never 0/None.
    assert b["expires_at"] > 1000.0


def test_remint_session_token_seeds_client_cookie(monkeypatch):
    seen = {}

    def _mint(jar, sid):
        seen["client"] = oauth_client._jar_cookie_value(jar, "__client")
        seen["sid"] = sid
        return "NEW.SESSION.JWT"

    monkeypatch.setattr(oauth_client, "_fapi_get_session_token", _mint)
    out = oauth_client.remint_session_token("CLIENTCOOKIE", "sess_9")
    assert out == "NEW.SESSION.JWT"
    assert seen["client"] == "CLIENTCOOKIE"  # durable credential seeded for auth
    assert seen["sid"] == "sess_9"


def test_remint_session_token_requires_inputs():
    assert oauth_client.remint_session_token("", "sess") is None
    assert oauth_client.remint_session_token("client", "") is None
