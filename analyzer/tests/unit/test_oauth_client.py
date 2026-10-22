"""Unit tests for the OAuth loopback-bind fallback.

Regression guard for the WinError 10013 (WSAEACCES) sign-in failure: the
loopback redirect server used a single fixed port (53682) that sat inside
Windows' boot-randomized reserved dynamic-port range (49152-65535), so
``bind()`` intermittently failed with "access forbidden" and sign-in broke on
some boots but not others. CI never caught it because it seeds auth tokens and
never exercises the bind path.

The fix: try an ordered list of candidate ports, all in the IANA user range
(1024-49151, never carved up by WinNAT's dynamic reservations), all registered
with Clerk, and build the redirect URI from whichever one actually binds.
"""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import oauth_client

pytestmark = pytest.mark.unit


# IANA dynamic/ephemeral range that Windows (WinNAT/Hyper-V/WSL2) carves into
# boot-randomized reserved chunks. Binding anything in here can hit WSAEACCES.
_DYNAMIC_RANGE_START = 49152


class _FakeServer:
    """Stand-in for http.server.HTTPServer — only needs server_close()."""

    def __init__(self, port):
        self.port = port
        self.closed = False

    def server_close(self):
        self.closed = True


def _patch_bind(monkeypatch, behavior):
    """Patch HTTPServer construction. ``behavior`` maps port -> OSError to raise
    (a successful bind returns a _FakeServer when the port isn't in the map)."""

    def _factory(addr, handler):
        host, port = addr
        exc = behavior.get(port)
        if exc is not None:
            raise exc
        return _FakeServer(port)

    monkeypatch.setattr(oauth_client.http.server, "HTTPServer", _factory)


def _oserror(errno_code, winerror=None):
    e = OSError()
    e.errno = errno_code
    if winerror is not None:
        e.winerror = winerror
    return e


# ── The regression guard that would have caught the original bug ─────────────

def test_all_candidate_ports_are_below_the_dynamic_range():
    """Every candidate must sit in the user range so WinNAT's boot-randomized
    reservations (which only touch 49152-65535) can never make bind() fail."""
    assert oauth_client.LOOPBACK_PORTS, "must have at least one candidate port"
    for port in oauth_client.LOOPBACK_PORTS[:-1]:
        # All but the legacy trailing fallback must be safe.
        assert 1024 <= port < _DYNAMIC_RANGE_START, (
            f"candidate port {port} is in/below the reserved dynamic range"
        )


def test_redirect_uri_matches_bound_port():
    assert oauth_client._redirect_uri(17893) == "http://127.0.0.1:17893/callback"


def test_authorize_url_uses_supplied_redirect_uri():
    url = oauth_client._build_authorize_url("state123", "chal456",
                                            "http://127.0.0.1:27184/callback")
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A27184%2Fcallback" in url


# ── Bind fallback behaviour ──────────────────────────────────────────────────

def test_bind_skips_wsaeacces_port_and_uses_next(monkeypatch):
    """First candidate is reserved (WSAEACCES 10013) — bind must fall through
    to the next candidate rather than failing the whole flow."""
    ports = oauth_client.LOOPBACK_PORTS
    behavior = {ports[0]: _oserror(10013, winerror=10013)}
    _patch_bind(monkeypatch, behavior)

    result = oauth_client._bind_loopback()

    assert "error" not in result
    assert result["port"] == ports[1]
    assert isinstance(result["server"], _FakeServer)


def test_bind_skips_in_use_port_and_uses_next(monkeypatch):
    ports = oauth_client.LOOPBACK_PORTS
    behavior = {ports[0]: _oserror(10048, winerror=10048)}  # EADDRINUSE (win)
    _patch_bind(monkeypatch, behavior)

    result = oauth_client._bind_loopback()

    assert result.get("port") == ports[1]


def test_bind_all_reserved_returns_no_loopback_port(monkeypatch):
    behavior = {p: _oserror(10013, winerror=10013) for p in oauth_client.LOOPBACK_PORTS}
    _patch_bind(monkeypatch, behavior)

    result = oauth_client._bind_loopback()

    assert result.get("error") == "no_loopback_port"
    assert result.get("error_description")


def test_bind_all_in_use_returns_port_in_use(monkeypatch):
    behavior = {p: _oserror(98) for p in oauth_client.LOOPBACK_PORTS}  # EADDRINUSE posix
    _patch_bind(monkeypatch, behavior)

    result = oauth_client._bind_loopback()

    assert result.get("error") == "port_in_use"


def test_bind_first_port_succeeds(monkeypatch):
    _patch_bind(monkeypatch, {})
    result = oauth_client._bind_loopback()
    assert result["port"] == oauth_client.LOOPBACK_PORTS[0]


# ── Flow-level wiring: a bind failure aborts before opening the browser ───────

def test_flow_aborts_without_opening_browser_when_no_port(monkeypatch):
    # This guards the loopback (Windows/Linux) transport. On macOS the flow
    # diverts to ASWebAuthenticationSession before ever binding a port, so pin
    # the platform to a loopback OS to exercise the path this test is about —
    # otherwise it fails on the macOS CI runner with aswebauth_start_failed.
    monkeypatch.setattr(oauth_client.sys, "platform", "win32")
    behavior = {p: _oserror(10013, winerror=10013) for p in oauth_client.LOOPBACK_PORTS}
    _patch_bind(monkeypatch, behavior)

    opened = []
    monkeypatch.setattr(oauth_client.webbrowser, "open",
                        lambda *a, **k: opened.append(a))

    result = oauth_client.run_authorization_flow()

    assert result["ok"] is False
    assert result["error"] == "no_loopback_port"
    assert opened == [], "browser must not be opened when no port could bind"
