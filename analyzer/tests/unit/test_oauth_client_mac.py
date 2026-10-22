"""Unit tests for the macOS ASWebAuthenticationSession OAuth transport.

These exercise ``oauth_client.run_authorization_flow_mac`` and the platform
dispatch in ``run_authorization_flow`` WITHOUT any PyObjC / real Mac: a fake
``mac_oauth`` stub stands in for the native session, so the parsing, CSRF
state check, error passthrough, and — critically — the "the sandboxed build
must never fall back to the external browser" guarantee are all verified on any
OS. The real ASWebAuthenticationSession plumbing in mac_oauth.py can only be
validated on-device (TestFlight / the dev macOS build).
"""

from urllib.parse import urlparse, parse_qs

import oauth_client as oc


class _EchoMac:
    """Stub provider that echoes the authorize URL's state back, like Clerk."""

    def __init__(self, code="auth_code_123"):
        self._code = code
        self.calls = []

    def authenticate(self, url, scheme, *, timeout, cancel_event=None):
        self.calls.append({"url": url, "scheme": scheme, "timeout": timeout})
        state = parse_qs(urlparse(url).query)["state"][0]
        return {"callback_url": f"kestrel://callback?code={self._code}&state={state}"}


class _StaticMac:
    """Stub provider that returns a fixed result regardless of input."""

    def __init__(self, result):
        self._result = result

    def authenticate(self, url, scheme, *, timeout, cancel_event=None):
        return dict(self._result)


def _stub_exchange(monkeypatch, captured=None):
    def _fake(code, verifier, redirect_uri):
        if captured is not None:
            captured.update(code=code, verifier=verifier, redirect_uri=redirect_uri)
        return {
            "access_token": "AT",
            "refresh_token": "RT",
            "expires_in": 3600,
            "token_type": "Bearer",
            "scope": "openid email profile",
        }
    monkeypatch.setattr(oc, "exchange_code", _fake)


def test_mac_flow_happy_path_uses_custom_scheme(monkeypatch):
    captured = {}
    _stub_exchange(monkeypatch, captured)
    mac = _EchoMac()

    res = oc.run_authorization_flow_mac(mac)

    assert res["ok"] is True
    assert res["bundle"]["access_token"] == "AT"
    assert res["bundle"]["refresh_token"] == "RT"
    # The session was asked to intercept the kestrel scheme...
    assert mac.calls[0]["scheme"] == oc.MAC_CALLBACK_SCHEME == "kestrel"
    # ...the authorize URL points at Clerk with the custom-scheme redirect...
    sent_url = mac.calls[0]["url"]
    assert sent_url.startswith(oc.CLERK_AUTHORIZE_URL)
    assert parse_qs(urlparse(sent_url).query)["redirect_uri"][0] == "kestrel://callback"
    # ...and the token exchange used that same redirect (RFC 6749 §4.1.3).
    assert captured["redirect_uri"] == "kestrel://callback"
    assert captured["code"] == "auth_code_123"


def test_mac_flow_rejects_state_mismatch(monkeypatch):
    _stub_exchange(monkeypatch)
    mac = _StaticMac({"callback_url": "kestrel://callback?code=abc&state=WRONG"})

    res = oc.run_authorization_flow_mac(mac)

    assert res["ok"] is False
    assert res["error"] == "state_mismatch"


def test_mac_flow_passes_through_cancellation():
    mac = _StaticMac({"error": "cancelled"})
    res = oc.run_authorization_flow_mac(mac)
    assert res == {"ok": False, "error": "cancelled", "error_description": None}


def test_mac_flow_surfaces_provider_error_as_no_code():
    mac = _StaticMac(
        {"callback_url": "kestrel://callback?error=access_denied&error_description=denied"}
    )
    res = oc.run_authorization_flow_mac(mac)
    assert res["ok"] is False
    assert res["error"] == "no_code"
    assert res["error_description"] == "denied"


def test_mac_flow_blocks_unsafe_authorize_url(monkeypatch):
    # The url_validator gate must veto before the session is ever presented.
    mac = _EchoMac()
    res = oc.run_authorization_flow_mac(mac, url_validator=lambda _u: False)
    assert res["ok"] is False
    assert res["error"] == "unsafe_authorize_url"
    assert mac.calls == []  # never presented the sheet


def test_dispatch_delegates_to_mac_transport_on_darwin(monkeypatch):
    monkeypatch.setattr(oc.sys, "platform", "darwin")
    monkeypatch.setattr(oc, "_load_mac_oauth", lambda: object())
    seen = {}

    def _fake_mac_flow(mac, progress_cb, *, url_validator, cancel_event):
        seen["delegated"] = True
        return {"ok": True, "bundle": {"access_token": "AT"}}

    monkeypatch.setattr(oc, "run_authorization_flow_mac", _fake_mac_flow)

    res = oc.run_authorization_flow()
    assert seen.get("delegated") is True
    assert res["ok"] is True


def test_sandboxed_build_never_falls_back_to_browser(monkeypatch):
    """The App Store (sandboxed) build must error, not open the system browser,
    when ASWebAuthenticationSession is unavailable — that fallback is exactly
    the Guideline 4 violation Apple rejected."""
    monkeypatch.setattr(oc.sys, "platform", "darwin")
    monkeypatch.setattr(oc, "_load_mac_oauth", lambda: None)
    monkeypatch.setattr(oc, "_is_macos_sandboxed", lambda: True)

    # If the guard failed and it fell through to loopback, this would try to
    # bind a socket / open a browser. A sentinel makes that a hard failure.
    def _boom():
        raise AssertionError("must not reach the loopback/browser transport")

    monkeypatch.setattr(oc, "_bind_loopback", _boom)

    res = oc.run_authorization_flow()
    assert res["ok"] is False
    assert res["error"] == "aswebauth_unavailable"
