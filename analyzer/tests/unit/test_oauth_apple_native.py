"""Unit tests for the native "Sign in with Apple" flow + Clerk FAPI bridge.

These cover ``oauth_client.run_apple_native_flow`` (orchestration) and the Clerk
Frontend-API helpers, with no real network or PyObjC. The macOS-native credential
capture lives in ``mac_apple_signin`` and is exercised only on-device (TestFlight);
here we stub it. The design contract under test: the Apple flow returns the SAME
``{"ok": bool, "bundle"|"error": ...}`` shape as the loopback/ASWeb transports, so
``api_bridge``'s worker persists and notifies identically.

Transport: Clerk **native mode** (``_is_native=1`` + Bearer client token). Every
FAPI request carries the ``_is_native=1`` flag and authenticates with an
``Authorization: Bearer <client_token>`` header (not the ``__client`` cookie);
each response returns the rotated client token in its ``Authorization`` header,
which the flow captures onto the ``_NativeSession`` and persists as the durable
re-mint credential.
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


def _sign_in_ok(session_id="sess_1", client_token="CLIENTTOK"):
    """A ``_fapi_apple_sign_in`` stub that mimics the real one's side effect of
    populating ``sess.client_token`` from the native-mode response header."""
    def _fn(sess, tok):
        sess.client_token = client_token
        return {"ok": True, "session_id": session_id}
    return _fn


def _mock_session_established(monkeypatch, *, token="hdr.body.sig"):
    """Wire the activate+mint FAPI helpers so an Apple flow reaches a minted
    Clerk session token and returns a clerk_session bundle."""
    monkeypatch.setattr(oauth_client, "_fapi_touch_session", lambda sess, sid: {"ok": True})
    monkeypatch.setattr(oauth_client, "_fapi_get_session_token", lambda sess, sid: token)


# ── run_apple_native_flow orchestration ──────────────────────────────────────

def test_apple_native_happy_path(monkeypatch):
    # Existing user: sign-in completes with a session id; the flow mints a Clerk
    # session JWT and returns a clerk_session bundle — no /oauth/authorize.
    apple = _FakeApple({"identity_token": "idtok", "raw_nonce": "n"})
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in",
                        _sign_in_ok(session_id="sess_1", client_token="CLIENTTOK"))
    _mock_session_established(monkeypatch, token="hdr.body.sig")

    res = oauth_client.run_apple_native_flow(apple)

    assert res["ok"] is True
    assert res["bundle"]["kind"] == "clerk_session"
    assert res["bundle"]["access_token"] == "hdr.body.sig"
    assert res["bundle"]["clerk_session_id"] == "sess_1"
    # The durable native-mode client token is persisted for on-demand re-mint.
    assert res["bundle"]["clerk_client"] == "CLIENTTOK"


def test_apple_native_no_session_token_fails(monkeypatch):
    # Session established but the mint returns nothing -> clean error, no bundle
    # (never hand back an empty credential).
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in", _sign_in_ok())
    monkeypatch.setattr(oauth_client, "_fapi_touch_session", lambda sess, sid: {"ok": True})
    monkeypatch.setattr(oauth_client, "_fapi_get_session_token", lambda sess, sid: None)
    res = oauth_client.run_apple_native_flow(_FakeApple({"identity_token": "idtok"}))
    assert res["ok"] is False
    assert res["error"] == "apple_no_session_token"


def test_apple_native_no_client_token_fails(monkeypatch):
    # Session + JWT minted, but no client token was ever returned by the FAPI ->
    # we couldn't re-mint the ~60s token later, so surface it instead of handing
    # back a bundle that will silently sign the user out.
    def _sign_in(sess, tok):  # never sets sess.client_token
        return {"ok": True, "session_id": "sess_1"}
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in", _sign_in)
    _mock_session_established(monkeypatch, token="hdr.body.sig")
    res = oauth_client.run_apple_native_flow(_FakeApple({"identity_token": "idtok"}))
    assert res["ok"] is False
    assert res["error"] == "apple_no_client_token"


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
                        lambda sess, tok: {"error": "apple_signin_rejected", "error_description": "bad token"})
    monkeypatch.setattr(oauth_client, "_fapi_get_session_token",
                        lambda *a, **k: pytest.fail("must not mint a token after a rejected sign-in"))
    res = oauth_client.run_apple_native_flow(_FakeApple({"identity_token": "idtok"}))
    assert res["ok"] is False
    assert res["error"] == "apple_signin_rejected"


# ── native-mode transport plumbing (_fapi_post) ──────────────────────────────

class _FakeResp:
    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self._body = body
        self.headers = _msg(headers or {})

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


def _msg(d):
    m = email.message.Message()
    for k, v in d.items():
        m[k] = v
    return m


def _http_error(location=None, code=302, body=None, headers=None):
    hdrs = _msg(headers or {})
    if location is not None:
        hdrs["Location"] = location
    fp = io.BytesIO(body) if body is not None else None
    return urllib.error.HTTPError("https://clerk.projectkestrel.org/v1/client/sign_ins",
                                  code, "Found", hdrs, fp)


def test_fapi_post_native_mode_sends_flag_and_bearer_captures_token(monkeypatch):
    captured = {}

    def _on_open(req):
        captured["url"] = req.full_url
        captured["auth"] = req.get_header("Authorization")
        # Clerk returns the (rotated) native client token in the Authorization
        # response header — with a Bearer prefix we must strip.
        return _FakeResp(status=200, body=b'{"response":{"status":"complete"}}',
                         headers={"Authorization": "Bearer ROTATED.TOKEN"})

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    sess = oauth_client._NativeSession("INITIAL.TOKEN")
    r = oauth_client._fapi_post(sess, oauth_client.CLERK_FAPI_SIGN_INS_URL, {"a": "b"})
    assert "data" in r
    assert "_is_native=1" in captured["url"]              # native mode flag on URL
    assert captured["auth"] == "Bearer INITIAL.TOKEN"     # sent the held token
    assert sess.client_token == "ROTATED.TOKEN"           # captured the rotated one


def test_fapi_post_first_call_has_no_bearer(monkeypatch):
    captured = {}

    def _on_open(req):
        captured["auth"] = req.get_header("Authorization")
        return _FakeResp(status=200, body=b'{"response":{}}',
                         headers={"Authorization": "FIRST.TOKEN"})

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    sess = oauth_client._NativeSession()  # no token yet
    oauth_client._fapi_post(sess, oauth_client.CLERK_FAPI_SIGN_INS_URL, {})
    assert captured["auth"] is None                       # no Authorization sent
    assert sess.client_token == "FIRST.TOKEN"             # established from response


def test_fapi_post_captures_token_on_error_response(monkeypatch):
    # The rotated client token can ride on an error response too — capture it so a
    # follow-up call isn't silently unauthenticated.
    def _on_open(req):
        raise _http_error(code=422, body=b'{"errors":[{"message":"x"}]}',
                          headers={"Authorization": "AFTER.ERROR.TOKEN"})

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    sess = oauth_client._NativeSession("OLD")
    r = oauth_client._fapi_post(sess, oauth_client.CLERK_FAPI_SIGN_INS_URL, {})
    assert r.get("error") == "apple_signin_rejected"
    assert sess.client_token == "AFTER.ERROR.TOKEN"


def test_native_url_appends_flag():
    assert oauth_client._native_url("https://x/y") == "https://x/y?_is_native=1"
    assert oauth_client._native_url("https://x/y?a=b") == "https://x/y?a=b&_is_native=1"


# ── _fapi_apple_sign_in: strategy + response parsing ─────────────────────────

def test_fapi_apple_sign_in_ok(monkeypatch):
    captured = {}

    def _on_open(req):
        captured["body"] = req.data.decode("ascii")
        captured["url"] = req.full_url
        return _FakeResp(status=200, body=b'{"response":{"status":"complete","created_session_id":"sess_A"},"client":{}}')

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._fapi_apple_sign_in(oauth_client._NativeSession(), "IDTOKEN")
    assert res == {"ok": True, "session_id": "sess_A"}
    # Correct strategy + token on the Frontend-API call.
    assert "strategy=oauth_token_apple" in captured["body"]
    assert "token=IDTOKEN" in captured["body"]
    assert captured["url"].startswith(oauth_client.CLERK_FAPI_SIGN_INS_URL)
    assert "_is_native=1" in captured["url"]


def test_fapi_apple_sign_in_soft_error(monkeypatch):
    # 200 but with an errors[] payload is still a failure.
    def _on_open(req):
        return _FakeResp(status=200, body=b'{"errors":[{"long_message":"Identity token invalid"}]}')

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._fapi_apple_sign_in(oauth_client._NativeSession(), "IDTOKEN")
    assert res.get("error") == "apple_signin_rejected"
    assert "invalid" in (res.get("error_description") or "").lower()


def test_fapi_apple_sign_in_http_error(monkeypatch):
    def _on_open(req):
        raise _http_error(code=403, body=b'{"errors":[{"message":"Unauthorized request"}]}')

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._fapi_apple_sign_in(oauth_client._NativeSession(), "IDTOKEN")
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
    res = oauth_client._fapi_apple_sign_in(oauth_client._NativeSession(), "IDTOKEN")
    assert res == {"transfer": True}


def test_fapi_apple_sign_in_incomplete_is_error(monkeypatch):
    # 200, no errors[], but the sign-in neither completed nor is transferable —
    # a diagnosable error carrying the real status.
    def _on_open(req):
        return _FakeResp(status=200, body=b'{"response":{"status":"needs_second_factor"},"client":{}}')

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._fapi_apple_sign_in(oauth_client._NativeSession(), "IDTOKEN")
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
    res = oauth_client._fapi_apple_sign_up_transfer(oauth_client._NativeSession())
    assert res == {"ok": True, "session_id": "sess_B"}
    # transfer=true against the sign_ups endpoint (Clerk pulls the verified Apple
    # identity from the transferable sign-in; no other fields needed).
    assert "transfer=true" in captured["body"]
    assert captured["url"].startswith(oauth_client.CLERK_FAPI_SIGN_UPS_URL)
    assert "_is_native=1" in captured["url"]


def test_fapi_apple_sign_up_transfer_missing_requirements(monkeypatch):
    # The instance demands a field Apple didn't provide — surface it, don't
    # silently proceed to a session-less token mint.
    def _on_open(req):
        return _FakeResp(status=200, body=b'{"response":{"status":"missing_requirements"},"client":{}}')

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    res = oauth_client._fapi_apple_sign_up_transfer(oauth_client._NativeSession())
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
    res = oauth_client._fapi_apple_sign_up_transfer(oauth_client._NativeSession())
    assert res.get("error") == "apple_signup_rejected"
    assert "oauth_token_invalid" in (res.get("error_description") or "")


# ── session activation (setActive / touch) + session id ──────────────────────

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
    res = oauth_client._fapi_touch_session(oauth_client._NativeSession(), "sess_A")
    assert res == {"ok": True}
    assert captured["url"].startswith(oauth_client.CLERK_FAPI_SESSIONS_URL + "/sess_A/touch")
    assert "_is_native=1" in captured["url"]


def test_apple_native_flow_activates_session(monkeypatch):
    # A completed sign-up returns a session id; the flow must touch (activate) it
    # AND mint the session token from it before returning the bundle.
    touched = {}

    def _touch(sess, sid):
        touched["sid"] = sid
        return {"ok": True}

    minted = {}

    def _mint(sess, sid):
        minted["sid"] = sid
        return "hdr.body.sig"

    def _sign_up(sess):
        sess.client_token = "CLIENTTOK"
        return {"ok": True, "session_id": "sess_live"}

    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in",
                        lambda sess, tok: {"transfer": True})
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_up_transfer", _sign_up)
    monkeypatch.setattr(oauth_client, "_fapi_touch_session", _touch)
    monkeypatch.setattr(oauth_client, "_fapi_get_session_token", _mint)

    res = oauth_client.run_apple_native_flow(_FakeApple({"identity_token": "idtok"}))

    assert res["ok"] is True
    assert res["bundle"]["kind"] == "clerk_session"
    assert touched.get("sid") == "sess_live"  # activated the created session
    assert minted.get("sid") == "sess_live"   # and minted the session token


def test_fapi_get_session_token_prefers_kestrel_api_template(monkeypatch):
    captured = {}

    def _on_open(req):
        captured["url"] = req.full_url
        return _FakeResp(status=200, body=b'{"object":"token","jwt":"HDR.BODY.SIG"}')

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    jwt = oauth_client._fapi_get_session_token(oauth_client._NativeSession(), "sess_A")
    assert jwt == "HDR.BODY.SIG"
    # Minted from the kestrel_api template, not the raw default token.
    assert captured["url"].startswith(oauth_client.CLERK_FAPI_SESSIONS_URL + "/sess_A/tokens/kestrel_api")
    assert "_is_native=1" in captured["url"]


def test_fapi_get_session_token_falls_back_to_default(monkeypatch):
    # If the template mint 404s (template unprovisioned), fall back to the
    # default session token — matching the other token-fetch sites.
    urls = []

    def _on_open(req):
        urls.append(req.full_url)
        if "/tokens/kestrel_api" in req.full_url:
            raise _http_error(code=404, body=b'{"errors":[{"message":"template not found"}]}')
        return _FakeResp(status=200, body=b'{"jwt":"DEFAULT.JWT"}')

    monkeypatch.setattr(oauth_client.urllib.request, "build_opener",
                        lambda *a, **k: _FakeOpener(_on_open))
    jwt = oauth_client._fapi_get_session_token(oauth_client._NativeSession(), "sess_A")
    assert jwt == "DEFAULT.JWT"
    assert "/tokens/kestrel_api" in urls[0]                   # tried the template first
    assert "/sess_A/tokens?" in urls[1]                       # then the default (+ native flag)


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

    def _sign_up(sess):
        calls.append("signup")
        sess.client_token = "CLIENTTOK"
        return {"ok": True, "session_id": "sess_new"}

    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in", lambda sess, tok: {"transfer": True})
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_up_transfer", _sign_up)
    _mock_session_established(monkeypatch, token="hdr.body.sig")

    res = oauth_client.run_apple_native_flow(_FakeApple({"identity_token": "idtok"}))

    assert res["ok"] is True
    assert res["bundle"]["kind"] == "clerk_session"
    assert res["bundle"]["clerk_session_id"] == "sess_new"
    assert calls == ["signup"]  # the transfer actually ran


def test_apple_native_flow_signup_transfer_failure_aborts(monkeypatch):
    # If account creation fails, surface it and never mint a session token.
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_in", lambda sess, tok: {"transfer": True})
    monkeypatch.setattr(oauth_client, "_fapi_apple_sign_up_transfer",
                        lambda sess: {"error": "apple_signup_incomplete", "error_description": "status=missing_requirements"})
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
    b = oauth_client.build_session_bundle(jwt, "CLIENTTOK", "sess_9", now=1000.0)
    assert b["kind"] == "clerk_session"
    assert b["access_token"] == jwt
    assert b["expires_at"] == float(exp)
    assert b["clerk_client"] == "CLIENTTOK"
    assert b["clerk_session_id"] == "sess_9"
    assert b["refresh_token"] == ""


def test_build_session_bundle_falls_back_when_no_exp():
    b = oauth_client.build_session_bundle("no.jwt.here", "c", "s", now=1000.0)
    # Unparseable exp -> a short forward window from `now`, never 0/None.
    assert b["expires_at"] > 1000.0


def test_remint_session_token_uses_client_token(monkeypatch):
    seen = {}

    def _mint(sess, sid):
        seen["client"] = sess.client_token
        seen["sid"] = sid
        return "NEW.SESSION.JWT"

    monkeypatch.setattr(oauth_client, "_fapi_get_session_token", _mint)
    out = oauth_client.remint_session_token("CLIENTTOK", "sess_9")
    assert out == "NEW.SESSION.JWT"
    assert seen["client"] == "CLIENTTOK"  # durable native client token used for auth
    assert seen["sid"] == "sess_9"


def test_remint_session_token_requires_inputs():
    assert oauth_client.remint_session_token("", "sess") is None
    assert oauth_client.remint_session_token("client", "") is None
