"""Unit tests for support_client.py — payload preparation and the request body."""

import base64
import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import support_client
from support_client import (
    MAX_LOG_TAIL_BYTES,
    SupportClient,
    normalize_case_ref,
    prepare_log_tail,
    prepare_screenshot,
    report_type_for,
)


pytestmark = pytest.mark.unit


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 8
GIF = b"GIF89a" + b"\x00" * 16


class TestNormalizeCaseRef:

    @pytest.mark.parametrize("raw", ["K-1042", "k-1042", "K1042", "k 1042", " 1042 ", "K-01042"])
    def test_accepted_forms(self, raw):
        assert normalize_case_ref(raw) == "K-1042"

    @pytest.mark.parametrize("raw", ["", None, "   "])
    def test_empty(self, raw):
        assert normalize_case_ref(raw) == ""

    @pytest.mark.parametrize("raw", ["abc", "K-", "K-12a", "X-1042", "K-1234567890", "1042; drop"])
    def test_rejected(self, raw):
        assert normalize_case_ref(raw) == ""


class TestReportType:

    @pytest.mark.parametrize("dialog,expected", [
        ("bug", "bug"), ("suggestion", "suggestion"), ("liked", "feedback"),
        ("general", "feedback"), ("account", "feedback"), ("BUG", "bug"),
        ("unknown", "feedback"), ("", "feedback"),
    ])
    def test_mapping(self, dialog, expected):
        assert report_type_for(dialog) == expected


class TestPrepareLogTail:

    def test_empty(self):
        assert prepare_log_tail("") == ""
        assert prepare_log_tail(None) == ""

    def test_redaction_applied(self, monkeypatch):
        seen = []

        def _fake_redact(text):
            seen.append(text)
            return text.replace("secretuser", "[REDACTED]")

        monkeypatch.setattr(support_client, "redact_user_paths", _fake_redact)
        out = prepare_log_tail("C:\\Users\\secretuser\\x.log\n")
        assert seen == ["C:\\Users\\secretuser\\x.log\n"]
        assert "secretuser" not in out

    def test_redaction_runs_before_cap(self, monkeypatch):
        """The cap is applied to the redacted text, so a username sitting at the
        cut point is never left half-redacted."""
        calls = []
        monkeypatch.setattr(
            support_client, "redact_user_paths",
            lambda t: (calls.append(len(t)), t.replace("bob", "[REDACTED]"))[1],
        )
        text = "x" * 200 + "bob\n" + "y" * 50 + "\n"
        out = prepare_log_tail(text, max_bytes=100)
        assert calls == [len(text)], "redaction must see the full, uncapped text"
        assert "bob" not in out

    def test_small_text_unchanged(self, monkeypatch):
        monkeypatch.setattr(support_client, "redact_user_paths", lambda t: t)
        assert prepare_log_tail("a\nb\n") == "a\nb\n"

    def test_cap_keeps_the_end(self, monkeypatch):
        monkeypatch.setattr(support_client, "redact_user_paths", lambda t: t)
        text = "".join(f"line {i}\n" for i in range(100_000))
        out = prepare_log_tail(text)
        assert len(out.encode("utf-8")) <= MAX_LOG_TAIL_BYTES
        assert out.startswith("[... earlier log lines truncated ...]\n")
        assert out.endswith("line 99999\n")
        # Starts on a whole line after the marker.
        assert out.splitlines()[1].startswith("line ")

    def test_cap_counts_utf8_bytes_and_never_splits_a_character(self, monkeypatch):
        monkeypatch.setattr(support_client, "redact_user_paths", lambda t: t)
        text = "é" * 1000  # 2 bytes each, no newlines
        out = prepare_log_tail(text, max_bytes=301)
        assert len(out.encode("utf-8")) <= 301
        body = out.split("\n", 1)[1]
        assert set(body) == {"é"}


class TestPrepareScreenshot:

    @pytest.mark.parametrize("data", [PNG, JPEG, WEBP])
    def test_accepted_types(self, data):
        b64 = _b64(data)
        assert prepare_screenshot(b64) == (b64, "")

    def test_empty(self):
        assert prepare_screenshot("") == ("", "")
        assert prepare_screenshot(None) == ("", "")

    def test_data_url_prefix_removed(self):
        b64 = _b64(PNG)
        assert prepare_screenshot("data:image/png;base64," + b64) == (b64, "")

    def test_unsupported_type(self):
        assert prepare_screenshot(_b64(GIF)) == ("", "unsupported_type")

    def test_invalid_base64(self):
        assert prepare_screenshot("not*base64!") == ("", "invalid")

    def test_too_large(self):
        data = PNG + b"\x00" * 100
        assert prepare_screenshot(_b64(data), max_bytes=len(data) - 1) == ("", "too_large")

    def test_exactly_at_limit_accepted(self):
        data = PNG + b"\x00" * 100
        b64 = _b64(data)
        assert prepare_screenshot(b64, max_bytes=len(data)) == (b64, "")

    def test_default_limit_is_10_mb(self):
        assert support_client.MAX_SCREENSHOT_BYTES == 10 * 1024 * 1024


class TestPostReport:

    def _client(self, monkeypatch):
        client = SupportClient("https://support.example.test/", "jwt-token")
        sent = {}

        def _fake_request(method, path, body=None):
            sent.update(method=method, path=path, body=body)
            return {"ok": True, "report_id": 1}

        monkeypatch.setattr(client, "_request", _fake_request)
        return client, sent

    def test_minimal_body(self, monkeypatch):
        client, sent = self._client(monkeypatch)
        client.post_report(report_type="bug", message="hi", app_version="v1", os="Win", platform="windows")
        assert sent["method"] == "POST"
        assert sent["path"] == "/v1/me/reports"
        assert sent["body"] == {
            "type": "bug", "product": "desktop", "message": "hi",
            "app_version": "v1", "platform": "windows", "os": "Win",
        }

    def test_optional_fields_included(self, monkeypatch):
        client, sent = self._client(monkeypatch)
        client.post_report(
            report_type="feedback", message="m", contact_email="a@b.test", case_ref="K-5",
            log_tail="log", screenshot_b64="QUJD", extra={"logs_requested": True},
        )
        body = sent["body"]
        assert body["contact_email"] == "a@b.test"
        assert body["case_ref"] == "K-5"
        assert body["log_tail"] == "log"
        assert body["screenshot_b64"] == "QUJD"
        assert body["extra"] == {"logs_requested": True}

    def test_headers_and_base(self):
        client = SupportClient("https://support.example.test/", "jwt-token")
        assert client.api_base == "https://support.example.test"
        assert client._auth_headers["Authorization"] == "Bearer jwt-token"
        assert client.USER_AGENT == "KestrelDesktop/Support/1.0"
        assert client.timeout == 60

    def test_default_base(self, monkeypatch):
        monkeypatch.delenv("KESTREL_SUPPORT_API_BASE", raising=False)
        assert support_client.default_support_api_base() == "https://support.projectkestrel.org"
        monkeypatch.setenv("KESTREL_SUPPORT_API_BASE", "http://localhost:8787/")
        assert support_client.default_support_api_base() == "http://localhost:8787"
