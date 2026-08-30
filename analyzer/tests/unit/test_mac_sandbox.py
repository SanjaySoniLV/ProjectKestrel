"""Tests for the macOS App Sandbox helper (mac_sandbox.py).

Two jobs:
  * Off macOS (e.g. the Windows dev box, Linux CI): every public function must
    degrade to a harmless no-op so the rest of the app can call into it
    unconditionally.
  * On macOS CI (unsigned, so the security-scoped *entitlement* is absent): we
    can't exercise real bookmark grants, but we CAN verify that the Cocoa
    objects respond to exactly the selectors mac_sandbox calls. A typo in a
    selector name is otherwise swallowed by the module's broad excepts and
    would only surface on a real signed device — this catches it in CI.
"""

import sys

import pytest

import mac_sandbox


def _reset_sandbox_cache():
    mac_sandbox._sandbox_cache = None


def test_is_sandboxed_forced_override(monkeypatch):
    monkeypatch.setattr(mac_sandbox, "_sandbox_cache", None, raising=False)
    monkeypatch.setattr(mac_sandbox.sys, "platform", "darwin", raising=False)
    monkeypatch.setenv("KESTREL_FORCE_SANDBOX", "1")
    assert mac_sandbox.is_sandboxed() is True
    # cache cleanup for other tests
    _reset_sandbox_cache()


def test_is_sandboxed_false_without_env(monkeypatch):
    monkeypatch.setattr(mac_sandbox, "_sandbox_cache", None, raising=False)
    monkeypatch.delenv("KESTREL_FORCE_SANDBOX", raising=False)
    monkeypatch.delenv("APP_SANDBOX_CONTAINER_ID", raising=False)
    assert mac_sandbox.is_sandboxed() is False
    _reset_sandbox_cache()


@pytest.mark.skipif(sys.platform == "darwin", reason="off-macOS no-op behavior")
def test_noops_off_macos():
    assert mac_sandbox.is_macos() is False
    assert mac_sandbox.is_sandboxed() is False
    assert mac_sandbox.have_pyobjc() is False
    assert mac_sandbox.create_bookmark("/tmp") is None
    assert mac_sandbox.resolve_bookmark("Zm9v") == (None, False)
    assert mac_sandbox.start_access("Zm9v") is None
    assert mac_sandbox.remember_folder("/tmp") is False
    assert mac_sandbox.lookup_bookmark("/tmp") is None
    assert mac_sandbox.activate_all_bookmarks() == 0
    assert mac_sandbox.reveal_in_finder("/tmp") is False
    assert mac_sandbox.open_default("/tmp") is False
    assert mac_sandbox.open_in_app_at_path("/tmp/x", "/Applications/None.app") is False
    assert mac_sandbox.open_in_app_named("/tmp/x", "DefinitelyNotInstalled") is False
    # Context managers must be safe no-ops too.
    with mac_sandbox.scoped_access(None) as p:
        assert p is None
    with mac_sandbox.access_for("/tmp") as p:
        assert p is None


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Cocoa selectors")
def test_pyobjc_selectors_exist_on_macos():
    """The Cocoa objects must respond to the exact selectors we invoke."""
    assert mac_sandbox.have_pyobjc() is True
    syms = mac_sandbox._load_cocoa()
    assert syms is not None

    url = syms["NSURL"].fileURLWithPath_("/tmp")
    # Bookmark create/resolve selectors.
    assert url.respondsToSelector_(
        "bookmarkDataWithOptions:includingResourceValuesForKeys:relativeToURL:error:"
    )
    assert syms["NSURL"].respondsToSelector_(
        "URLByResolvingBookmarkData:options:relativeToURL:bookmarkDataIsStale:error:"
    )
    assert url.respondsToSelector_("startAccessingSecurityScopedResource")
    assert url.respondsToSelector_("stopAccessingSecurityScopedResource")

    # NSWorkspace open/reveal selectors.
    ws = syms["NSWorkspace"].sharedWorkspace()
    assert ws.respondsToSelector_("activateFileViewerSelectingURLs:")
    assert ws.respondsToSelector_("openURL:")
    assert ws.respondsToSelector_(
        "openURLs:withApplicationAtURL:configuration:completionHandler:"
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Cocoa round-trip")
def test_plain_bookmark_roundtrip_no_crash(tmp_path):
    """Creating a (non-scoped) bookmark path shouldn't raise.

    Without the security-scope entitlement, create_bookmark may return None on
    an unsigned binary — that's acceptable. The point is the selector call path
    executes cleanly rather than throwing, which a wrong signature would.
    """
    result = mac_sandbox.create_bookmark(str(tmp_path))
    # Either a base64 string or None, but never an exception.
    assert result is None or isinstance(result, str)
