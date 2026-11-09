"""
Support client — desktop-side wrapper for support.projectkestrel.org.

Signed-in feedback ("Send as <account>" in the feedback dialog) is posted here
with the same Clerk JWT the other hosted add-ons use. Unlike the older account
feedback route, this endpoint stores the report's recent log tail and the
attached screenshot, so a signed-in report carries the same evidence an
anonymous one does.

Anonymous feedback and crash reports do not use this module; they keep going
through ``kestrel_telemetry``.

Route: ``POST /v1/me/reports``

    {
      "type": "bug" | "suggestion" | "feedback" | "crash",
      "product": "desktop",
      "message": str,
      "contact_email": str,      # optional
      "case_ref": "K-1042",      # optional
      "app_version": str, "platform": str, "os": str,
      "log_tail": str,           # optional, <= 256 KB (UTF-8)
      "screenshot_b64": str,     # optional, <= 10 MB decoded, PNG/JPEG/WebP
      "extra": {}
    }

Response: ``{"ok": true, "report_id": ..., "ref": "K-1042"?}``. ``ref`` is
present only when the report joined or created a support case.

The payload-preparation helpers below are pure functions so they can be unit
tested without a network.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import sys

# Dual import for root-vs-package sys.path (see auth_client.py).
try:
    from auth_client import AuthClient, AuthClientError, AuthClientNetworkError
except ImportError:  # pragma: no cover - package-style import path
    from analyzer.auth_client import (  # type: ignore[no-redef]
        AuthClient,
        AuthClientError,
        AuthClientNetworkError,
    )

try:
    from log_redactor import redact_user_paths
except ImportError:  # pragma: no cover - package-style import path
    from analyzer.log_redactor import redact_user_paths  # type: ignore[no-redef]

__all__ = [
    "AuthClientError",
    "AuthClientNetworkError",
    "MAX_LOG_TAIL_BYTES",
    "MAX_SCREENSHOT_BYTES",
    "SupportClient",
    "default_support_api_base",
    "normalize_case_ref",
    "platform_name",
    "prepare_log_tail",
    "prepare_screenshot",
    "report_type_for",
]

_DEFAULT_SUPPORT_API_BASE = "https://support.projectkestrel.org"

# Server-side limits for POST /v1/me/reports. The client enforces them too so
# an oversized payload is trimmed or dropped here rather than rejected there.
MAX_LOG_TAIL_BYTES = 256 * 1024
MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024  # decoded bytes

_LOG_TRUNCATED_MARKER = "[... earlier log lines truncated ...]\n"

# Dialog type values -> report types the endpoint accepts.
_REPORT_TYPE_MAP = {
    "bug": "bug",
    "suggestion": "suggestion",
    "liked": "feedback",
    "general": "feedback",
    "account": "feedback",
}

_CASE_REF_RE = re.compile(r"^K?-?\s*(\d{1,9})$", re.IGNORECASE)


def default_support_api_base() -> str:
    """Resolve the Support base URL — env override, then default."""
    return os.environ.get("KESTREL_SUPPORT_API_BASE", _DEFAULT_SUPPORT_API_BASE).rstrip("/")


def report_type_for(dialog_type: str) -> str:
    """Map a feedback-dialog type value to the endpoint's report type."""
    return _REPORT_TYPE_MAP.get(str(dialog_type or "").strip().lower(), "feedback")


def platform_name() -> str:
    """Short platform name: windows, macos or linux."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def normalize_case_ref(raw) -> str:
    """Return a case reference as ``K-<digits>``, or '' if *raw* isn't one.

    Accepts the forms a user is likely to type: ``K-1042``, ``k1042``,
    ``K 1042`` or just ``1042``.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    m = _CASE_REF_RE.match(text.replace(" ", ""))
    if not m:
        return ""
    return f"K-{int(m.group(1))}"


def prepare_log_tail(text, max_bytes: int | None = None) -> str:
    """Redact user paths from *text*, then cap it at *max_bytes* of UTF-8.

    Redaction runs first so the cap can't cut a path in half and leave a
    username fragment unredacted. When the text is too long, the most recent
    lines (the end) are kept and a marker line is put in front.
    """
    if not text:
        return ""
    if max_bytes is None:
        max_bytes = MAX_LOG_TAIL_BYTES
    redacted = redact_user_paths(str(text))
    encoded = redacted.encode("utf-8")
    if len(encoded) <= max_bytes:
        return redacted
    marker = _LOG_TRUNCATED_MARKER.encode("utf-8")
    keep = max(0, max_bytes - len(marker))
    tail = encoded[len(encoded) - keep:]
    # Start on a whole line where possible, and never mid-character.
    newline = tail.find(b"\n")
    if 0 <= newline < len(tail) - 1:
        tail = tail[newline + 1:]
    return (marker + tail).decode("utf-8", errors="ignore")


def _image_kind(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


def prepare_screenshot(b64, max_bytes: int | None = None) -> tuple[str, str]:
    """Validate a base64 screenshot for the endpoint.

    Returns ``(clean_b64, drop_reason)``. On success *drop_reason* is '' and
    *clean_b64* is plain base64 (any ``data:...;base64,`` prefix removed). On
    failure *clean_b64* is '' and *drop_reason* is one of ``too_large``,
    ``invalid`` or ``unsupported_type``. Empty input returns ``('', '')``.
    """
    text = str(b64 or "").strip()
    if not text:
        return "", ""
    if max_bytes is None:
        max_bytes = MAX_SCREENSHOT_BYTES
    if text.startswith("data:") and "," in text:
        text = text.split(",", 1)[1]
    text = "".join(text.split())
    # Cheap size check before decoding: 4 base64 chars carry 3 bytes.
    if (len(text) * 3) // 4 - text[-2:].count("=") > max_bytes:
        return "", "too_large"
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return "", "invalid"
    if len(raw) > max_bytes:
        return "", "too_large"
    if not _image_kind(raw):
        return "", "unsupported_type"
    return text, ""


class SupportClient(AuthClient):
    """Clerk-JWT client for the Support host. Reuses AuthClient's request and
    error handling; only the base URL, User-Agent and timeout differ."""

    USER_AGENT = "KestrelDesktop/Support/1.0"

    def __init__(self, api_base: str, jwt_token: str | None, dev_user: str | None = None,
                 timeout: int = 60) -> None:
        # A 10 MB screenshot is ~13 MB of JSON, so allow a longer timeout than
        # the account calls use.
        super().__init__(api_base, jwt_token, dev_user=dev_user, timeout=timeout)

    def post_report(
        self,
        report_type: str,
        message: str,
        app_version: str = "",
        os: str = "",
        platform: str = "",
        contact_email: str = "",
        case_ref: str = "",
        log_tail: str = "",
        screenshot_b64: str = "",
        extra: dict | None = None,
    ) -> dict:
        """POST /v1/me/reports. Optional fields are left out when empty.

        Callers pass *log_tail* and *screenshot_b64* already prepared with
        :func:`prepare_log_tail` / :func:`prepare_screenshot`.
        Returns the response dict; raises AuthClientError on failure.
        """
        body: dict = {
            "type": report_type,
            "product": "desktop",
            "message": message,
            "app_version": app_version,
            "platform": platform,
            "os": os,
        }
        if contact_email:
            body["contact_email"] = contact_email
        if case_ref:
            body["case_ref"] = case_ref
        if log_tail:
            body["log_tail"] = log_tail
        if screenshot_b64:
            body["screenshot_b64"] = screenshot_b64
        if extra:
            body["extra"] = extra
        return self._request("POST", "/v1/me/reports", body)
