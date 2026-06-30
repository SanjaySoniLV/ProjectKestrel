"""macOS App Sandbox helpers for the Mac App Store build of Project Kestrel.

The App Store distribution of Kestrel runs inside Apple's App Sandbox, which
fundamentally changes how the app reaches the filesystem and other apps:

  * It can only touch files/folders the user explicitly grants through a
    system open-panel (powerbox). The path string alone carries no access on
    the next launch — access must be persisted as a *security-scoped
    bookmark* and re-acquired with ``startAccessingSecurityScopedResource``.
  * It cannot ``exec``/``Popen`` arbitrary executables (``/usr/bin/open``,
    third-party editors). Opening files in other apps / Finder must go through
    ``NSWorkspace``, which LaunchServices brokers on the app's behalf.

This module is the single place that talks to Cocoa for those concerns. It is
written to be import-safe everywhere: nothing here imports PyObjC at module
load time, and every public function degrades to a harmless no-op / ``None``
when run off macOS, outside the sandbox, or without PyObjC available. That lets
the rest of the codebase call into it unconditionally and keeps the Windows /
Linux / direct-download macOS builds behaving exactly as before.

Nothing in here can be exercised meaningfully without a *signed, entitled*
sandboxed build running on a real Mac — security-scoped bookmark creation
requires the ``com.apple.security.files.bookmarks.app-scope`` entitlement and
silently fails otherwise. The unsigned CI dev build only proves these imports
and code paths don't crash; real validation waits for device testing.
"""

from __future__ import annotations

import os
import sys
import base64
import threading
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Platform / sandbox detection (no PyObjC needed)
# ---------------------------------------------------------------------------

# macOS sets this env var for every process launched inside an App Sandbox
# container. It is the cheapest, most reliable "am I sandboxed?" signal and
# needs no Cocoa call. ``KESTREL_FORCE_SANDBOX`` lets us exercise the
# sandbox-only code paths in development / CI on a non-sandboxed binary.
_SANDBOX_ENV_KEYS = ("APP_SANDBOX_CONTAINER_ID",)

_sandbox_cache: Optional[bool] = None


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_sandboxed() -> bool:
    """True iff this process is running inside the macOS App Sandbox.

    Cached after first call. A forced override (``KESTREL_FORCE_SANDBOX=1``) is
    honoured for testing the sandbox code paths on an unsandboxed build, but
    note the Cocoa calls below will still fail without the real entitlements.
    """
    global _sandbox_cache
    if _sandbox_cache is not None:
        return _sandbox_cache
    if not is_macos():
        _sandbox_cache = False
        return False
    forced = os.environ.get("KESTREL_FORCE_SANDBOX", "").strip().lower()
    if forced in ("1", "true", "yes", "on"):
        _sandbox_cache = True
        return True
    _sandbox_cache = any(os.environ.get(k) for k in _SANDBOX_ENV_KEYS)
    return _sandbox_cache


# ---------------------------------------------------------------------------
# Lazy PyObjC loading
# ---------------------------------------------------------------------------

_objc_cache: dict = {}
_objc_lock = threading.Lock()


def _load_cocoa():
    """Import the Cocoa bits we need, caching the result.

    Returns a dict of the symbols used below, or ``None`` if PyObjC is not
    importable (off-mac, or a build that didn't bundle the frameworks).
    pywebview's Cocoa backend already depends on PyObjC for the macOS build,
    so these imports are expected to succeed there.
    """
    if "loaded" in _objc_cache:
        return _objc_cache.get("syms")
    with _objc_lock:
        if "loaded" in _objc_cache:
            return _objc_cache.get("syms")
        syms = None
        if is_macos():
            try:
                import Foundation  # type: ignore
                import AppKit  # type: ignore

                syms = {
                    "NSURL": Foundation.NSURL,
                    "NSData": Foundation.NSData,
                    "NSWorkspace": AppKit.NSWorkspace,
                    # Bookmark option masks (values are stable Apple constants;
                    # referenced by name when the constant is exported, with a
                    # literal fallback for older PyObjC that omits it).
                    "CREATE_SCOPE": getattr(
                        Foundation, "NSURLBookmarkCreationWithSecurityScope", 1 << 11
                    ),
                    "RESOLVE_SCOPE": getattr(
                        Foundation, "NSURLBookmarkResolutionWithSecurityScope", 1 << 10
                    ),
                    "OpenConfiguration": getattr(
                        AppKit, "NSWorkspaceOpenConfiguration", None
                    ),
                }
            except Exception:
                syms = None
        _objc_cache["loaded"] = True
        _objc_cache["syms"] = syms
        return syms


def have_pyobjc() -> bool:
    return _load_cocoa() is not None


# ---------------------------------------------------------------------------
# Security-scoped bookmarks
# ---------------------------------------------------------------------------
#
# Bookmarks are persisted as base64 text so they slot into settings.json
# cleanly. Resolved NSURLs that we are actively accessing are tracked by path
# so callers can stop access symmetrically.

_active_urls: dict = {}            # realpath -> NSURL (currently being accessed)
_active_lock = threading.Lock()


def create_bookmark(path: str) -> Optional[str]:
    """Create a security-scoped bookmark for ``path``; return base64 text.

    Returns ``None`` off-sandbox, without PyObjC, or on any Cocoa error. The
    caller is expected to have just received ``path`` from a powerbox open
    panel (only then does the kernel grant the right the bookmark captures).
    """
    syms = _load_cocoa()
    if syms is None:
        return None
    try:
        url = syms["NSURL"].fileURLWithPath_(path)
        data, err = url.bookmarkDataWithOptions_includingResourceValuesForKeys_relativeToURL_error_(
            syms["CREATE_SCOPE"], None, None, None
        )
        if data is None:
            return None
        raw = bytes(data)
        return base64.b64encode(raw).decode("ascii")
    except Exception:
        return None


def resolve_bookmark(b64: str) -> Tuple[Optional[str], bool]:
    """Resolve a base64 bookmark to ``(path, is_stale)``.

    ``path`` is ``None`` if resolution fails. ``is_stale`` True means the
    bookmark resolved but should be recreated (the file moved / was replaced)
    — callers should re-mint and persist a fresh bookmark when convenient.
    Note: this only resolves the URL; call :func:`start_access` to actually
    gain read/write rights.
    """
    syms = _load_cocoa()
    if syms is None:
        return None, False
    try:
        raw = base64.b64decode(b64.encode("ascii"))
        data = syms["NSData"].dataWithBytes_length_(raw, len(raw))
        url, stale, err = syms[
            "NSURL"
        ].URLByResolvingBookmarkData_options_relativeToURL_bookmarkDataIsStale_error_(
            data, syms["RESOLVE_SCOPE"], None, None, None
        )
        if url is None:
            return None, False
        return str(url.path()), bool(stale)
    except Exception:
        return None, False


def start_access(b64: str) -> Optional[str]:
    """Resolve ``b64`` and begin security-scoped access; return the path.

    The resolved NSURL is retained internally keyed by its real path so a later
    :func:`stop_access` can balance the call. Returns ``None`` on failure.
    """
    syms = _load_cocoa()
    if syms is None:
        return None
    try:
        raw = base64.b64decode(b64.encode("ascii"))
        data = syms["NSData"].dataWithBytes_length_(raw, len(raw))
        url, _stale, _err = syms[
            "NSURL"
        ].URLByResolvingBookmarkData_options_relativeToURL_bookmarkDataIsStale_error_(
            data, syms["RESOLVE_SCOPE"], None, None, None
        )
        if url is None:
            return None
        ok = url.startAccessingSecurityScopedResource()
        path = str(url.path())
        if ok:
            with _active_lock:
                _active_urls[os.path.realpath(path)] = url
        return path
    except Exception:
        return None


def stop_access(path: str) -> None:
    """Balance a prior :func:`start_access` for ``path`` (best effort)."""
    try:
        key = os.path.realpath(path)
    except Exception:
        key = path
    with _active_lock:
        url = _active_urls.pop(key, None)
    if url is not None:
        try:
            url.stopAccessingSecurityScopedResource()
        except Exception:
            pass


class scoped_access:
    """Context manager: hold security-scoped access to a bookmark's path.

    ``with scoped_access(b64) as path:`` yields the resolved path (or ``None``
    if it couldn't be acquired) and releases access on exit. No-op friendly:
    off-sandbox it simply yields ``None`` and the caller proceeds with the
    plain path it already had.
    """

    def __init__(self, b64: Optional[str]):
        self._b64 = b64
        self._path: Optional[str] = None

    def __enter__(self) -> Optional[str]:
        if self._b64:
            self._path = start_access(self._b64)
        return self._path

    def __exit__(self, *_exc):
        if self._path:
            stop_access(self._path)
        return False


# ---------------------------------------------------------------------------
# Bookmark persistence (settings-backed)
# ---------------------------------------------------------------------------
#
# Bookmarks live in settings.json under a single dict keyed by the *realpath*
# the user granted. This is the one spot that couples to settings_utils, done
# lazily to keep the module import-safe.

_BOOKMARK_SETTINGS_KEY = "mac_folder_bookmarks"


def _settings_mod():
    try:
        import settings_utils  # type: ignore

        return settings_utils
    except Exception:
        try:
            from analyzer import settings_utils  # type: ignore

            return settings_utils
        except Exception:
            return None


def _bookmark_key(path: str) -> str:
    try:
        return os.path.realpath(path)
    except Exception:
        return path


def remember_folder(path: str) -> bool:
    """Mint + persist a security-scoped bookmark for a just-granted folder.

    Safe to call unconditionally: returns ``False`` (and does nothing) when not
    sandboxed, off-mac, without PyObjC, or on failure.
    """
    if not is_sandboxed():
        return False
    b64 = create_bookmark(path)
    if not b64:
        return False
    sm = _settings_mod()
    if sm is None:
        return False
    try:
        settings = sm.load_persisted_settings()
        marks = settings.get(_BOOKMARK_SETTINGS_KEY)
        if not isinstance(marks, dict):
            marks = {}
        marks[_bookmark_key(path)] = b64
        settings[_BOOKMARK_SETTINGS_KEY] = marks
        sm.save_persisted_settings(settings)
        return True
    except Exception:
        return False


def lookup_bookmark(path: str) -> Optional[str]:
    """Return the stored base64 bookmark covering ``path``, if any.

    Matches an exact realpath first, then the nearest stored ancestor folder
    (so a bookmark on a parent grants access to children within it).
    """
    sm = _settings_mod()
    if sm is None:
        return None
    try:
        settings = sm.load_persisted_settings()
    except Exception:
        return None
    marks = settings.get(_BOOKMARK_SETTINGS_KEY)
    if not isinstance(marks, dict) or not marks:
        return None
    key = _bookmark_key(path)
    if key in marks:
        return marks[key]
    # Nearest-ancestor match.
    best = None
    best_len = -1
    for stored, b64 in marks.items():
        try:
            anc = stored if stored.endswith(os.sep) else stored + os.sep
            if key == stored or key.startswith(anc):
                if len(stored) > best_len:
                    best, best_len = b64, len(stored)
        except Exception:
            continue
    return best


def activate_all_bookmarks() -> int:
    """Resolve every stored bookmark and hold access for the whole session.

    Called once at startup (sandboxed builds only). This is the primary
    mechanism by which previously-chosen photo folders remain readable/writable
    across launches without re-prompting: each bookmark is resolved and
    ``startAccessingSecurityScopedResource`` is held until the process exits.
    Folders chosen *during* this session already carry a live powerbox grant.

    Returns the number of bookmarks successfully activated. No-op (returns 0)
    off-sandbox / without PyObjC. Stale bookmarks are re-minted in place.
    """
    if not is_sandboxed():
        return 0
    sm = _settings_mod()
    if sm is None:
        return 0
    try:
        settings = sm.load_persisted_settings()
    except Exception:
        return 0
    marks = settings.get(_BOOKMARK_SETTINGS_KEY)
    if not isinstance(marks, dict) or not marks:
        return 0
    activated = 0
    refreshed = False
    for stored_path, b64 in list(marks.items()):
        path = start_access(b64)
        if path:
            activated += 1
            # Opportunistically refresh a stale bookmark now that we hold access.
            _resolved, stale = resolve_bookmark(b64)
            if stale:
                fresh = create_bookmark(path)
                if fresh:
                    marks[stored_path] = fresh
                    refreshed = True
    if refreshed:
        try:
            settings[_BOOKMARK_SETTINGS_KEY] = marks
            sm.save_persisted_settings(settings)
        except Exception:
            pass
    return activated


def access_for(path: str) -> "scoped_access":
    """Context manager granting sandbox access to ``path`` via a stored bookmark.

    ``with access_for(folder):`` is a no-op off-sandbox (yields ``None``), and
    on the sandbox resolves+holds the nearest stored bookmark for the duration.
    """
    if not is_sandboxed():
        return scoped_access(None)
    return scoped_access(lookup_bookmark(path))


# ---------------------------------------------------------------------------
# NSWorkspace: open / reveal (sandbox-legal alternatives to `open`)
# ---------------------------------------------------------------------------


def reveal_in_finder(path: str) -> bool:
    """Reveal ``path`` in Finder via NSWorkspace. Returns success."""
    syms = _load_cocoa()
    if syms is None:
        return False
    try:
        url = syms["NSURL"].fileURLWithPath_(path)
        syms["NSWorkspace"].sharedWorkspace().activateFileViewerSelectingURLs_([url])
        return True
    except Exception:
        return False


def open_default(path: str) -> bool:
    """Open ``path`` in its default handler app via NSWorkspace. Returns success."""
    syms = _load_cocoa()
    if syms is None:
        return False
    try:
        url = syms["NSURL"].fileURLWithPath_(path)
        return bool(syms["NSWorkspace"].sharedWorkspace().openURL_(url))
    except Exception:
        return False


def _open_with_app_url(file_path: str, app_url) -> bool:
    syms = _load_cocoa()
    if syms is None or app_url is None:
        return False
    ws = syms["NSWorkspace"].sharedWorkspace()
    file_url = syms["NSURL"].fileURLWithPath_(file_path)
    cfg_cls = syms.get("OpenConfiguration")
    try:
        if cfg_cls is not None:
            cfg = cfg_cls.configuration()
            # Async API (10.15+). Fire-and-forget: a None completion handler is
            # accepted by PyObjC and we don't need the launch result inline.
            ws.openURLs_withApplicationAtURL_configuration_completionHandler_(
                [file_url], app_url, cfg, None
            )
            return True
    except Exception:
        pass
    # Fallback to the deprecated-but-present synchronous selector.
    try:
        return bool(
            ws.openURLs_withApplicationAtURL_options_configuration_error_(
                [file_url], app_url, 0, {}, None
            )
        )
    except Exception:
        return False


def open_in_app_at_path(file_path: str, app_path: str) -> bool:
    """Open ``file_path`` using the app bundle at ``app_path`` (a ``.app``)."""
    syms = _load_cocoa()
    if syms is None:
        return False
    try:
        app_url = syms["NSURL"].fileURLWithPath_(app_path)
        return _open_with_app_url(file_path, app_url)
    except Exception:
        return False


def open_in_app_named(file_path: str, app_display_name: str) -> bool:
    """Best-effort: open ``file_path`` in an app located by display name.

    Looks for ``<name>.app`` in the standard Applications folders (system and
    user). Returns False if not found, so the caller can fall back to the
    default-handler open. Sandboxed apps may read ``/Applications`` listings to
    resolve an app URL; the launch itself is brokered by LaunchServices.
    """
    if not app_display_name:
        return False
    candidates = [
        os.path.join("/Applications", f"{app_display_name}.app"),
        os.path.join("/Applications/Utilities", f"{app_display_name}.app"),
        os.path.join(os.path.expanduser("~/Applications"), f"{app_display_name}.app"),
    ]
    for app_path in candidates:
        if os.path.isdir(app_path):
            if open_in_app_at_path(file_path, app_path):
                return True
    return False
