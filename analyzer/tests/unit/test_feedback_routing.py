"""Unit tests for Api.send_feedback endpoint routing.

The feedback dialog carries a `send_as_user` opt-in (default OFF). The
backend must:

  * take the Support path (client.post_report) ONLY when the user opted in
    AND a signed-in support client can be built, sending the redacted log
    tail and the screenshot with it, and
  * take the anonymous analytics path (_telemetry.send_feedback) in every
    other case — opted out, signed out, or Support failure — still carrying
    the logs and screenshot.

These tests pin that routing so the opt-in can't silently regress back to
"always report as the signed-in account", and so the signed-in path can't
go back to dropping logs and screenshots.
"""

import base64
import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge
import log_redactor
import support_client


pytestmark = pytest.mark.unit

# A real (tiny) PNG header so prepare_screenshot accepts it.
_PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32).decode("ascii")

_LOG_WITH_PATH = "INFO analysing C:\\Users\\alice\\Pictures\\IMG_0001.CR2\n"


class _FakeSupportClient:
    """Records post_report calls so tests can assert the Support path was (or
    wasn't) taken. raise_on_post simulates a Support failure."""

    def __init__(self, raise_on_post=False, response=None):
        self.raise_on_post = raise_on_post
        self.response = response if response is not None else {"ok": True, "report_id": 7}
        self.calls = []

    def post_report(self, **kwargs):
        self.calls.append(kwargs)
        if self.raise_on_post:
            raise RuntimeError("simulated support failure")
        return self.response


class _FakeTelemetry:
    """Stand-in for the kestrel_telemetry module. Records send_feedback
    (the anonymous analytics path) and provides the metadata helpers
    send_feedback() reads."""

    def __init__(self, log_tail="log-tail"):
        self.feedback_calls = []
        self.log_tail = log_tail
        self.log_tail_requests = 0

    # --- metadata helpers used by Api.send_feedback ---
    def get_machine_id(self, settings):
        return "machine-123"

    def get_recent_log_tail(self, folder=None, runtime_log_files=3):
        self.log_tail_requests += 1
        return self.log_tail

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
def fake_user(monkeypatch):
    """Pin the OS username the redactor looks for to 'alice'."""
    def _expanduser(path):
        if path.startswith('~'):
            return r'C:\Users\alice' + path[1:]
        return path

    monkeypatch.setattr(log_redactor.os.path, 'expanduser', _expanduser)
    monkeypatch.setenv('USERNAME', 'alice')
    monkeypatch.setenv('USER', 'alice')
    log_redactor._cache_key = ('', '')
    log_redactor._cache_username_pattern = None
    log_redactor._cache_home_pattern = None
    yield
    log_redactor._cache_key = ('', '')


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


def _use_support(api, monkeypatch, client):
    monkeypatch.setattr(api, "_support_make_client", lambda: (client, None))
    return client


class TestSendFeedbackRouting:

    def test_opted_in_and_signed_in_uses_support(self, api, fake_telemetry, monkeypatch):
        """send_as_user=True + a buildable client → Support path, and the
        anonymous analytics path is NOT used."""
        client = _use_support(api, monkeypatch, _FakeSupportClient())

        result = api.send_feedback(_base_data(send_as_user=True))

        assert result == {"success": True}
        assert len(client.calls) == 1, "expected exactly one Support post"
        call = client.calls[0]
        assert call["report_type"] == "bug"
        assert call["message"] == "something broke"
        assert call["contact_email"] == "user@example.com"
        assert call["app_version"] == "v(Test)"
        assert call["os"] == "TestOS"
        assert call["platform"] in ("windows", "macos", "linux")
        assert call["log_tail"] == ""
        assert call["extra"] == {"logs_requested": False}
        assert fake_telemetry.feedback_calls == [], "analytics path must not run when Support succeeds"

    def test_never_uses_auth_worker(self, api, fake_telemetry, monkeypatch):
        """Signed-in feedback no longer goes to the account service."""
        _use_support(api, monkeypatch, _FakeSupportClient())
        monkeypatch.setattr(
            api, "_auth_make_client",
            lambda: pytest.fail("auth client must not be built for feedback"),
        )
        assert api.send_feedback(_base_data(send_as_user=True)) == {"success": True}

    def test_opted_out_never_builds_support_client(self, api, fake_telemetry, monkeypatch):
        """send_as_user=False → the client is never even built (so a signed-in
        user reporting anonymously stays anonymous), and the analytics path
        handles the report."""
        calls = {"support": 0}

        def _spy_make_client():
            calls["support"] += 1
            return (_FakeSupportClient(), None)

        monkeypatch.setattr(api, "_support_make_client", _spy_make_client)

        result = api.send_feedback(_base_data(send_as_user=False))

        assert result == {"success": True}
        assert calls["support"] == 0, "support client must not be built when opted out"
        assert len(fake_telemetry.feedback_calls) == 1, "expected the anonymous analytics path"

    def test_default_payload_is_anonymous(self, api, fake_telemetry, monkeypatch):
        """A payload with no send_as_user key at all defaults to anonymous."""
        calls = {"support": 0}
        monkeypatch.setattr(
            api, "_support_make_client",
            lambda: (calls.__setitem__("support", calls["support"] + 1), (_FakeSupportClient(), None))[1],
        )
        data = _base_data()
        del data["send_as_user"]

        result = api.send_feedback(data)

        assert result == {"success": True}
        assert calls["support"] == 0
        assert len(fake_telemetry.feedback_calls) == 1

    def test_opted_in_but_signed_out_falls_back_to_analytics(self, api, fake_telemetry, monkeypatch):
        """send_as_user=True but no client (signed out) → analytics path."""
        monkeypatch.setattr(
            api, "_support_make_client",
            lambda: (None, {"ok": False, "error": "not_signed_in"}),
        )

        result = api.send_feedback(_base_data(send_as_user=True))

        assert result == {"success": True, "sent_anonymously": True}
        assert len(fake_telemetry.feedback_calls) == 1, "should fall back to anonymous when signed out"

    def test_opted_in_support_failure_falls_back_to_analytics(self, api, fake_telemetry, monkeypatch):
        """send_as_user=True, client builds but post_report throws → the
        report still lands via the anonymous analytics path, logs and
        screenshot included."""
        client = _use_support(api, monkeypatch, _FakeSupportClient(raise_on_post=True))

        result = api.send_feedback(_base_data(
            send_as_user=True, include_logs=True, screenshot_b64=_PNG_B64,
        ))

        assert result == {"success": True, "sent_anonymously": True}
        assert len(client.calls) == 1, "support post was attempted"
        assert len(fake_telemetry.feedback_calls) == 1, "and then fell back to analytics"
        anon = fake_telemetry.feedback_calls[0]
        assert anon["log_tail"] == "log-tail"
        assert anon["screenshot_b64"] == _PNG_B64

    def test_analytics_path_carries_screenshot(self, api, fake_telemetry, monkeypatch):
        """An opted-out report with a screenshot keeps it."""
        _use_support(api, monkeypatch, _FakeSupportClient())

        api.send_feedback(_base_data(send_as_user=False, screenshot_b64="QUJD"))

        assert len(fake_telemetry.feedback_calls) == 1
        assert fake_telemetry.feedback_calls[0]["screenshot_b64"] == "QUJD"


class TestIncludeLogs:

    def test_signed_in_sends_redacted_log_tail(self, api, fake_telemetry, fake_user, monkeypatch):
        """include_logs=True on the signed-in path: the log tail is collected,
        redacted, and sent to Support (it used to be dropped)."""
        fake_telemetry.log_tail = _LOG_WITH_PATH
        client = _use_support(api, monkeypatch, _FakeSupportClient())

        api.send_feedback(_base_data(send_as_user=True, include_logs=True))

        assert fake_telemetry.log_tail_requests == 1
        sent = client.calls[0]["log_tail"]
        assert "alice" not in sent
        assert r"C:\Users\[REDACTED]\Pictures\IMG_0001.CR2" in sent
        assert client.calls[0]["extra"] == {"logs_requested": True}
        assert fake_telemetry.feedback_calls == []

    def test_signed_in_log_tail_is_capped(self, api, fake_telemetry, monkeypatch):
        """A log tail over the Support limit is trimmed to it, keeping the end."""
        big = "".join(f"line {i:07d}\n" for i in range(40_000)) + "LAST LINE\n"
        assert len(big.encode("utf-8")) > support_client.MAX_LOG_TAIL_BYTES
        fake_telemetry.log_tail = big
        client = _use_support(api, monkeypatch, _FakeSupportClient())

        api.send_feedback(_base_data(send_as_user=True, include_logs=True))

        sent = client.calls[0]["log_tail"]
        assert len(sent.encode("utf-8")) <= support_client.MAX_LOG_TAIL_BYTES
        assert sent.endswith("LAST LINE\n")

    def test_anonymous_passes_log_tail_to_telemetry(self, api, fake_telemetry, monkeypatch):
        """include_logs=True on the anonymous path: the log tail goes to the
        analytics send function (which does its own redaction), unchanged."""
        fake_telemetry.log_tail = _LOG_WITH_PATH
        client = _use_support(api, monkeypatch, _FakeSupportClient())

        api.send_feedback(_base_data(send_as_user=False, include_logs=True))

        assert client.calls == []
        assert fake_telemetry.feedback_calls[0]["log_tail"] == _LOG_WITH_PATH

    def test_logs_not_collected_when_unticked(self, api, fake_telemetry, monkeypatch):
        _use_support(api, monkeypatch, _FakeSupportClient())

        api.send_feedback(_base_data(send_as_user=True, include_logs=False))
        api.send_feedback(_base_data(send_as_user=False, include_logs=False))

        assert fake_telemetry.log_tail_requests == 0
        assert fake_telemetry.feedback_calls[0]["log_tail"] == ""


class TestScreenshotAndCaseRef:

    def test_signed_in_sends_screenshot(self, api, fake_telemetry, monkeypatch):
        client = _use_support(api, monkeypatch, _FakeSupportClient())

        result = api.send_feedback(_base_data(send_as_user=True, screenshot_b64=_PNG_B64))

        assert result == {"success": True}
        assert client.calls[0]["screenshot_b64"] == _PNG_B64

    def test_oversized_screenshot_dropped_and_reported(self, api, fake_telemetry, monkeypatch):
        """A screenshot over the decoded-size cap is not sent, the report still
        goes through, and the caller is told why."""
        monkeypatch.setattr(support_client, "MAX_SCREENSHOT_BYTES", 16)
        client = _use_support(api, monkeypatch, _FakeSupportClient())

        result = api.send_feedback(_base_data(send_as_user=True, screenshot_b64=_PNG_B64))

        assert result == {"success": True, "screenshot_dropped": "too_large"}
        assert client.calls[0]["screenshot_b64"] == ""
        assert client.calls[0]["extra"]["screenshot_dropped"] == "too_large"

    def test_case_ref_normalised_and_ref_returned(self, api, fake_telemetry, monkeypatch):
        client = _use_support(
            api, monkeypatch,
            _FakeSupportClient(response={"ok": True, "report_id": 9, "ref": "K-1042"}),
        )

        result = api.send_feedback(_base_data(send_as_user=True, case_ref="k 1042"))

        assert client.calls[0]["case_ref"] == "K-1042"
        assert result == {"success": True, "ref": "K-1042"}

    def test_invalid_case_ref_not_sent(self, api, fake_telemetry, monkeypatch):
        client = _use_support(api, monkeypatch, _FakeSupportClient())

        api.send_feedback(_base_data(send_as_user=True, case_ref="not a ref"))

        assert client.calls[0]["case_ref"] == ""

    def test_anonymous_case_ref_kept_in_description(self, api, fake_telemetry, monkeypatch):
        """The analytics payload has no case_ref field, so the reference is
        put at the top of the description instead of being lost."""
        _use_support(api, monkeypatch, _FakeSupportClient())

        api.send_feedback(_base_data(send_as_user=False, case_ref="K-77"))

        desc = fake_telemetry.feedback_calls[0]["description"]
        assert desc.startswith("[Case reference: K-77]\n")
        assert desc.endswith("something broke")
