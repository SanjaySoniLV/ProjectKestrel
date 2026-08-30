"""Unit tests for Api.send_feedback endpoint routing.

The feedback dialog carries a `send_as_user` opt-in (default OFF). The
backend must:

  * take the Auth Worker path (client.post_feedback) ONLY when the user
    opted in AND a signed-in auth client can be built, and
  * take the anonymous analytics path (_telemetry.send_feedback) in every
    other case — opted out, signed out, or Auth Worker failure.

These tests pin that routing so the opt-in can't silently regress back to
"always report as the signed-in account."
"""

import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge


pytestmark = pytest.mark.unit


class _FakeAuthClient:
    """Records post_feedback calls so tests can assert the Auth Worker path
    was (or wasn't) taken. raise_on_post simulates a transient Auth failure."""

    def __init__(self, raise_on_post=False):
        self.raise_on_post = raise_on_post
        self.calls = []

    def post_feedback(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_on_post:
            raise RuntimeError("simulated auth worker failure")
        return {"ok": True}


class _FakeTelemetry:
    """Stand-in for the kestrel_telemetry module. Records send_feedback
    (the anonymous analytics path) and provides the metadata helpers
    send_feedback() reads."""

    def __init__(self):
        self.feedback_calls = []

    # --- metadata helpers used by Api.send_feedback ---
    def get_machine_id(self, settings):
        return "machine-123"

    def get_recent_log_tail(self, folder=None, runtime_log_files=3):
        return "log-tail"

    def _read_version(self):
        return "v(Test)"

    def _get_os_info(self):
        return "TestOS"

    # --- anonymous analytics path ---
    def send_feedback(self, **kwargs):
        self.feedback_calls.append(kwargs)
        return {"ok": True}


@pytest.fixture
def fake_telemetry(monkeypatch):
    tel = _FakeTelemetry()
    monkeypatch.setattr(api_bridge, "_telemetry", tel)
    # Keep settings deterministic and filesystem-free.
    monkeypatch.setattr(api_bridge, "load_persisted_settings", lambda: {})
    return tel


@pytest.fixture
def api():
    return api_bridge.Api()


def _base_data(**overrides):
    data = {
        "type": "bug",
        "description": "something broke",
        "contact": "user@example.com",
        "include_logs": False,
        "screenshot_b64": "",
        "send_as_user": False,
    }
    data.update(overrides)
    return data


class TestSendFeedbackRouting:

    def test_opted_in_and_signed_in_uses_auth_worker(self, api, fake_telemetry, monkeypatch):
        """send_as_user=True + a buildable auth client → Auth Worker path,
        and the anonymous analytics path is NOT used."""
        client = _FakeAuthClient()
        monkeypatch.setattr(api, "_auth_make_client", lambda: (client, None))

        result = api.send_feedback(_base_data(send_as_user=True))

        assert result == {"success": True}
        assert len(client.calls) == 1, "expected exactly one Auth Worker post"
        assert client.calls[0]["report_type"] == "bug"
        assert client.calls[0]["message"] == "something broke"
        assert client.calls[0]["contact"] == "user@example.com"
        assert fake_telemetry.feedback_calls == [], "analytics path must not run when auth path succeeds"

    def test_opted_out_never_builds_auth_client(self, api, fake_telemetry, monkeypatch):
        """send_as_user=False → the auth client is never even built (so a
        signed-in user reporting anonymously stays anonymous), and the
        analytics path handles the report."""
        calls = {"auth": 0}

        def _spy_make_client():
            calls["auth"] += 1
            return (_FakeAuthClient(), None)

        monkeypatch.setattr(api, "_auth_make_client", _spy_make_client)

        result = api.send_feedback(_base_data(send_as_user=False))

        assert result == {"success": True}
        assert calls["auth"] == 0, "auth client must not be built when opted out"
        assert len(fake_telemetry.feedback_calls) == 1, "expected the anonymous analytics path"

    def test_default_payload_is_anonymous(self, api, fake_telemetry, monkeypatch):
        """A payload with no send_as_user key at all defaults to anonymous."""
        calls = {"auth": 0}
        monkeypatch.setattr(
            api, "_auth_make_client",
            lambda: (calls.__setitem__("auth", calls["auth"] + 1), (_FakeAuthClient(), None))[1],
        )
        data = _base_data()
        del data["send_as_user"]

        result = api.send_feedback(data)

        assert result == {"success": True}
        assert calls["auth"] == 0
        assert len(fake_telemetry.feedback_calls) == 1

    def test_opted_in_but_signed_out_falls_back_to_analytics(self, api, fake_telemetry, monkeypatch):
        """send_as_user=True but no auth client (signed out) → analytics path."""
        monkeypatch.setattr(
            api, "_auth_make_client",
            lambda: (None, {"ok": False, "error": "not_signed_in"}),
        )

        result = api.send_feedback(_base_data(send_as_user=True))

        assert result == {"success": True}
        assert len(fake_telemetry.feedback_calls) == 1, "should fall back to anonymous when signed out"

    def test_opted_in_auth_failure_falls_back_to_analytics(self, api, fake_telemetry, monkeypatch):
        """send_as_user=True, auth client builds but post_feedback throws →
        the report still lands via the anonymous analytics path."""
        client = _FakeAuthClient(raise_on_post=True)
        monkeypatch.setattr(api, "_auth_make_client", lambda: (client, None))

        result = api.send_feedback(_base_data(send_as_user=True))

        assert result == {"success": True}
        assert len(client.calls) == 1, "auth post was attempted"
        assert len(fake_telemetry.feedback_calls) == 1, "and then fell back to analytics"

    def test_analytics_path_carries_screenshot(self, api, fake_telemetry, monkeypatch):
        """The anonymous path forwards the screenshot (the Auth path drops it),
        so an opted-out report with a screenshot keeps it."""
        monkeypatch.setattr(api, "_auth_make_client", lambda: (_FakeAuthClient(), None))

        api.send_feedback(_base_data(send_as_user=False, screenshot_b64="QUJD"))

        assert len(fake_telemetry.feedback_calls) == 1
        assert fake_telemetry.feedback_calls[0]["screenshot_b64"] == "QUJD"
