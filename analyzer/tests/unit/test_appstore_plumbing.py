"""Tests for the App Store plumbing added alongside the sandbox work:

  * dist_channel.py — the baked 'direct' vs 'appstore' distribution channel.
  * settings_utils._sanitize_mac_folder_bookmarks — the explicit sanitizer for
    security-scoped bookmarks, which must NOT fall through to the passthrough
    handler (whose per-string cap would silently truncate a large blob).
"""

import base64

import pytest

import dist_channel
import settings_utils


# --------------------------------------------------------------------------- #
# dist_channel
# --------------------------------------------------------------------------- #

def _no_baked(monkeypatch):
    """Force _read_baked() to miss so we exercise the env/default path."""
    monkeypatch.setattr(dist_channel, "_cached", None, raising=False)
    monkeypatch.setattr(dist_channel, "_read_baked", lambda: None)


def test_channel_defaults_direct(monkeypatch):
    _no_baked(monkeypatch)
    monkeypatch.delenv("KESTREL_DIST_CHANNEL", raising=False)
    assert dist_channel.get_channel() == "direct"
    assert dist_channel.is_direct() is True
    assert dist_channel.is_appstore() is False


def test_channel_env_appstore(monkeypatch):
    _no_baked(monkeypatch)
    monkeypatch.setenv("KESTREL_DIST_CHANNEL", "appstore")
    assert dist_channel.get_channel() == "appstore"
    assert dist_channel.is_appstore() is True


def test_channel_invalid_env_falls_back_to_direct(monkeypatch):
    _no_baked(monkeypatch)
    monkeypatch.setenv("KESTREL_DIST_CHANNEL", "not-a-channel")
    assert dist_channel.get_channel() == "direct"


def test_channel_baked_wins_over_env(monkeypatch):
    monkeypatch.setattr(dist_channel, "_cached", None, raising=False)
    monkeypatch.setattr(dist_channel, "_read_baked", lambda: "appstore")
    monkeypatch.setenv("KESTREL_DIST_CHANNEL", "direct")
    assert dist_channel.get_channel() == "appstore"


def test_channel_result_is_cached(monkeypatch):
    monkeypatch.setattr(dist_channel, "_cached", None, raising=False)
    monkeypatch.setattr(dist_channel, "_read_baked", lambda: "appstore")
    assert dist_channel.get_channel() == "appstore"
    # Flip the underlying source; cached value must persist.
    monkeypatch.setattr(dist_channel, "_read_baked", lambda: "direct")
    assert dist_channel.get_channel() == "appstore"


# --------------------------------------------------------------------------- #
# mac_folder_bookmarks sanitizer
# --------------------------------------------------------------------------- #

def _b64(nbytes: int) -> str:
    return base64.b64encode(b"\x00" * nbytes).decode("ascii")


def test_bookmarks_valid_preserved():
    good = _b64(2000)
    marks = settings_utils._sanitize_mac_folder_bookmarks(
        {"/Users/x/Photos": good, "/Volumes/Card": good}
    )
    assert marks == {"/Users/x/Photos": good, "/Volumes/Card": good}


def test_bookmarks_oversized_blob_dropped_not_truncated():
    huge = _b64(60000)  # base64 ~80000 chars > _MAX_BOOKMARK_B64_CHARS
    assert len(huge) > settings_utils._MAX_BOOKMARK_B64_CHARS
    marks = settings_utils._sanitize_mac_folder_bookmarks({"/p": huge})
    # Must be dropped entirely, never truncated to a corrupt blob.
    assert "/p" not in marks


def test_bookmarks_non_base64_dropped():
    marks = settings_utils._sanitize_mac_folder_bookmarks(
        {"/ok": _b64(100), "/bad": "not base64!! @#$"}
    )
    assert "/ok" in marks
    assert "/bad" not in marks


def test_bookmarks_non_string_and_empty_key_dropped():
    marks = settings_utils._sanitize_mac_folder_bookmarks(
        {"": _b64(50), "/good": _b64(50), "/nonstr": 123}
    )
    assert marks == {"/good": _b64(50)}


def test_bookmarks_non_dict_returns_empty():
    assert settings_utils._sanitize_mac_folder_bookmarks("garbage") == {}
    assert settings_utils._sanitize_mac_folder_bookmarks(None) == {}
    assert settings_utils._sanitize_mac_folder_bookmarks(["a", "b"]) == {}


def test_bookmarks_entry_cap():
    good = _b64(20)
    many = {f"/p/{i}": good for i in range(settings_utils._MAX_BOOKMARK_ENTRIES + 50)}
    marks = settings_utils._sanitize_mac_folder_bookmarks(many)
    assert len(marks) == settings_utils._MAX_BOOKMARK_ENTRIES


# --------------------------------------------------------------------------- #
# End-to-end through the real sanitizer entry point
# --------------------------------------------------------------------------- #

def test_payload_preserves_valid_bookmarks():
    good = _b64(1500)
    out = settings_utils._sanitize_settings_payload(
        {"mac_folder_bookmarks": {"/Users/x/Photos": good}}
    )
    assert out["mac_folder_bookmarks"] == {"/Users/x/Photos": good}


def test_payload_garbage_bookmarks_become_empty_not_passthrough():
    # A garbage (non-dict) value for the key must be coerced to {} by the explicit
    # handler, NOT truncated/kept as a string by the passthrough handler.
    out = settings_utils._sanitize_settings_payload(
        {"mac_folder_bookmarks": "x" * 100000}
    )
    assert out["mac_folder_bookmarks"] == {}
