"""JavaScript API bridge for Project Kestrel visualizer.

Provides the Api class that exposes methods to the pywebview JavaScript layer
and serves as the bridge between the web UI and native OS operations.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import webbrowser

from settings_utils import load_persisted_settings, save_persisted_settings, debug, info, warn, error
# Several long-standing call sites use `log(...)` as a catch-all logger.
# Bind it to `error` so error-path handlers don't NameError mid-cleanup
# (which masks the real exception that triggered them).
log = error
from queue_manager import _queue_manager

try:
    from kestrel_analyzer.exposure_compensation import preserve_highlights_for_stops as _preserve_highlights_for_stops
except ImportError:
    try:
        from analyzer.kestrel_analyzer.exposure_compensation import preserve_highlights_for_stops as _preserve_highlights_for_stops
    except ImportError:
        def _preserve_highlights_for_stops(stops: float) -> float:
            if stops > 1.0:
                return 0.95
            if stops > 0.4:
                return 0.9
            if stops > 0.0:
                return 0.85
            return 0.0

try:
    from editor_launch import launch as _launch_editor
except ImportError:
    try:
        from analyzer.editor_launch import launch as _launch_editor
    except ImportError:
        _launch_editor = None

# macOS App Sandbox helpers. Import-safe everywhere (no-ops off-mac / off-sandbox);
# used only by the Mac App Store build to route folder access through powerbox +
# security-scoped bookmarks and to open files/Finder via NSWorkspace.
try:
    import mac_sandbox as _mac_sandbox
except ImportError:
    try:
        from analyzer import mac_sandbox as _mac_sandbox
    except ImportError:
        _mac_sandbox = None

# Distribution channel ('direct' website build vs 'appstore' sandboxed build),
# baked at build time. Import-safe everywhere; used by the frontend to branch
# What's New / cloud-compute CTAs on channel without cutting a new release.
try:
    import dist_channel as _dist_channel
except ImportError:
    try:
        from analyzer import dist_channel as _dist_channel
    except ImportError:
        _dist_channel = None

# Support-page URLs. /support-me carries the donate option; /support is the same
# page with no payment path at all. Which one the app opens is decided by
# get_support_url() — see the note there on why this is storefront-scoped.
SUPPORT_URL_FULL = 'https://projectkestrel.org/support-me'
SUPPORT_URL_NO_PAYMENT = 'https://projectkestrel.org/support'


def _load_storefront():
    """Return the ``mac_storefront`` module if StoreKit storefront lookup is
    usable on this build, else ``None``. Never raises."""
    if sys.platform != 'darwin':
        return None
    try:
        import mac_storefront  # type: ignore
    except ImportError:  # pragma: no cover - package-style import path
        try:
            from analyzer import mac_storefront  # type: ignore
        except Exception:
            return None
    except Exception:
        return None
    try:
        return mac_storefront if mac_storefront.is_available() else None
    except Exception:
        return None

from kestrel_analyzer.config import (
    JPEG_EXTENSIONS as _JPEG_EXTENSIONS,
    RAW_EXTENSIONS as _RAW_EXTENSIONS,
)

# Telemetry — failsafe import (never blocks startup)
try:
    import kestrel_telemetry as _telemetry
except ImportError:
    try:
        from analyzer import kestrel_telemetry as _telemetry
    except ImportError:
        _telemetry = None  # type: ignore[assignment]

# pywebview availability
WEBVIEW_IMPORT_SUCCESS = False
try:
    import webview  # type: ignore  # noqa: F401
    WEBVIEW_IMPORT_SUCCESS = True
except Exception:
    pass

# OAuth 2.0 + PKCE flow against Clerk OAuth Applications. Pure-stdlib module
# kept separate so it's unit-testable without pywebview / JS.
try:
    import oauth_client as _oauth  # type: ignore
except ImportError:
    try:
        from analyzer import oauth_client as _oauth  # type: ignore
    except ImportError:
        _oauth = None  # type: ignore[assignment]

# Cloud-compute backend poller cadence (seconds). One poller per active job
# keeps the per-job remote snapshot fresh; JS reads from cache so there is no
# N+1 query against the Worker per render. 5s gives near-realtime UI without
# burning Worker subrequests when several jobs run in parallel.
_CC_POLL_INTERVAL_SEC = 5

# Continuous-retrieval loop cadence: how long cloud_compute_retrieve_results
# sleeps between successive list_results+download passes for a still-running
# job. Packs arrive in batches of ~10, so 15s keeps the gallery fresh without
# hammering R2/list_results.
_CC_RETRIEVE_LOOP_SEC = 15

# B2 accept gate: Worker returns 503 {error:'cloud_busy'} when Modal GPU
# capacity is at the job-block threshold. Surfaced verbatim to JS on submit.
_CC_CLOUD_BUSY_USER_MESSAGE = (
    "Cloud Compute Servers are at max capacity, please try again in a few "
    "minutes, or contact support. Sorry for the inconvenience!"
)

# ── Account-auth helpers (Kestrel Auth Worker JWT) ───────────────────────────
_KEYRING_SERVICE = 'ProjectKestrel'
# Big-bang rename in the auth-migration: keychain slot changed from
# 'perch_auth' to 'kestrel_auth'. Existing installs see an empty slot and
# are prompted to sign in once. Acceptable pre-launch.
_KEYRING_KEY     = 'kestrel_auth'

def _get_auth_fallback_path() -> str:
    """Plaintext fallback path when no keyring backend is available."""
    from settings_utils import _get_user_data_dir
    return os.path.join(_get_user_data_dir(), 'auth.json')

def _keyring_load() -> dict | None:
    """Read the stored auth JWT from OS keychain; fall back to plaintext file.

    If the key is missing from the keychain (get_password returns None), we
    must still read the file fallback — otherwise a token stored only in
    ``auth.json`` (when keyring save failed) is never loaded after restart.
    """
    try:
        import keyring
        raw = keyring.get_password(_KEYRING_SERVICE, _KEYRING_KEY)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    try:
        with open(_get_auth_fallback_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _keyring_save(data: dict) -> None:
    """Write the auth bundle to OS keychain; fall back to plaintext file.

    Critical invariant: only ONE of keychain / fallback-file should hold
    canonical data at any moment. If keychain write succeeds we delete any
    stale fallback file (and vice versa) so ``_keyring_load`` — which checks
    keychain first — cannot return outdated data while fresh data sits
    invisibly in the file. OAuth bundles routinely exceed Windows
    Credential Manager's ~2560-byte per-credential limit, so the file
    fallback is hit in practice, not just in keyring-unavailable
    environments.

    Fallback file is locked down to owner-read/write (``0o600``) in a
    ``0o700`` directory. Without that the default umask leaves the file
    world-readable on POSIX, which on a shared dev / CI box is a direct
    JWT exfil path (audit Medium-13). On Windows, ``chmod`` is a weak ACL
    approximation; the keyring path is the secure-by-default option there.
    """
    serialized = json.dumps(data)
    fallback_path = _get_auth_fallback_path()

    keyring_ok = False
    keyring_err: Exception | None = None
    try:
        import keyring
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY, serialized)
        keyring_ok = True
    except Exception as e:
        keyring_err = e

    if keyring_ok:
        # Wipe any stale file fallback so the next _keyring_load doesn't
        # silently return outdated data if keychain later breaks.
        try:
            os.remove(fallback_path)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        return

    # Keychain failed (most often: bundle too large for the OS keystore).
    # Log it so this isn't silent, then ensure the keychain has no stale
    # entry that would shadow the about-to-be-written file.
    try:
        print(
            f"[Auth] keychain write failed ({type(keyring_err).__name__}: "
            f"{keyring_err}); falling back to plaintext file at {fallback_path}",
            flush=True,
        )
    except Exception:
        pass
    try:
        import keyring as _kr
        try:
            _kr.delete_password(_KEYRING_SERVICE, _KEYRING_KEY)
        except Exception:
            pass
    except ImportError:
        pass

    directory = os.path.dirname(fallback_path)
    os.makedirs(directory, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    with open(fallback_path, 'w', encoding='utf-8') as f:
        f.write(serialized)
    try:
        os.chmod(fallback_path, 0o600)
    except OSError:
        pass


def _auth_jwt_exp_unverified(token: str) -> float | None:
    """Return JWT `exp` (seconds since epoch) from the payload without verifying the signature."""
    t = str(token).strip()
    parts = t.split(".")
    if len(parts) < 2:
        return None
    try:
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg))
        e = payload.get("exp")
        if e is None:
            return None
        return float(e)
    except Exception:  # pragma: no cover
        return None


def _auth_jwt_seconds_until_exp(token: str) -> float | None:
    """Seconds from now until JWT exp (unverified), or None if not decodable / no exp."""
    exp = _auth_jwt_exp_unverified(token)
    if exp is None:
        return None
    return float(exp) - time.time()


def _auth_jwt_sub_unverified(token: str) -> str | None:
    """Return JWT `sub` (the stable Clerk user id) from the payload without
    verifying the signature. Used as an offline owner id to tag cloud jobs so
    history can be filtered to the account that submitted them — works even
    when the network is down or the job's folder is unavailable."""
    t = str(token).strip()
    parts = t.split(".")
    if len(parts) < 2:
        return None
    try:
        seg = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg))
        sub = payload.get("sub")
        return str(sub) if sub else None
    except Exception:  # pragma: no cover
        return None


# ──────────────────────────────────────────────────────────────────────────────

# Metadata writing utilities
try:
    from metadata_writer import write_xmp_metadata as _write_xmp_metadata
except ImportError:
    _write_xmp_metadata = None  # type: ignore[assignment]

HOST = '127.0.0.1'

_ALLOWED_ROOT = os.environ.get('KESTREL_ALLOWED_ROOT')
if _ALLOWED_ROOT:
    _ALLOWED_ROOT = os.path.abspath(os.path.expanduser(_ALLOWED_ROOT))

_ALLOWED_EDITORS = {
    'system', 'darktable', 'lightroom', 'photoshop', 'capture_one',
    'affinity', 'gimp', 'rawtherapee', 'luminar', 'dxo', 'on1',
    'acdsee', 'paintshop', 'faststone', 'xnview', 'irfanview', 'custom',
}

# Editor-launch allowlist tracks the analyzer's supported formats so any
# file Kestrel can analyze can also be opened in the configured editor.
_DEFAULT_EDITOR_EXTENSIONS = list(_RAW_EXTENSIONS) + list(_JPEG_EXTENSIONS)
_EXTERNAL_URL_SCHEME_ALLOWLIST = frozenset({'http', 'https', 'mailto'})


def _is_safe_external_url(url) -> bool:
    """Return True iff ``url`` is safe to hand to ``webbrowser.open``.

    Only plain ``http://``, ``https://``, and ``mailto:`` URLs are allowed.
    Everything else — ``file://``, ``javascript:``, ``data:``, Windows-specific
    custom schemes like ``ms-appdata:`` / ``search-ms:``, UNC paths (``\\\\host``
    or forward-slash ``//host``), and any URL containing control characters —
    is rejected.

    Rationale (FINDING-01): ``webbrowser.open`` ultimately calls
    ``ShellExecute`` on Windows, which happily launches local executables when
    given a ``file://`` URL or a custom URI scheme bound to an installed
    handler. Combined with the stored DOM-XSS formerly present in the scene
    renderer, that was a clean stored-XSS-to-RCE chain. The allowlist closes
    the browser side of that chain.
    """
    if not isinstance(url, str):
        return False
    u = url.strip()
    if not u:
        return False
    # Reject any ASCII control character (incl. newline, CR, NUL, DEL).
    for ch in u:
        o = ord(ch)
        if o < 0x20 or o == 0x7F:
            return False
    # Reject UNC paths and backslash injection (Windows ShellExecute
    # interprets these as local file references).
    if '\\' in u or u.startswith('//'):
        return False
    scheme, sep, _rest = u.partition(':')
    if not sep:
        return False
    return scheme.strip().lower() in _EXTERNAL_URL_SCHEME_ALLOWLIST


_ALLOWED_EDITOR_EXTENSIONS: set[str] = set()


def _normalize_extensions(exts):
    normalized = []
    seen = set()
    for ext in exts or []:
        e = str(ext or '').strip().lower()
        if not e:
            continue
        if not e.startswith('.'):
            e = f'.{e}'
        if e in seen:
            continue
        seen.add(e)
        normalized.append(e)
    return normalized


_ALLOWED_EDITOR_EXTENSIONS = set(
    _normalize_extensions(
        os.environ.get('KESTREL_ALLOWED_EXTENSIONS', ','.join(_DEFAULT_EDITOR_EXTENSIONS)).split(',')
    )
)


_CULLING_COMPANION_EXTENSIONS = tuple(
    _normalize_extensions(['.xmp', *(_JPEG_EXTENSIONS or [])])
)
_RAW_EXTENSION_SET = set(_normalize_extensions(_RAW_EXTENSIONS or []))
_CULLING_PRIMARY_IMAGE_EXTENSIONS = set(
    _normalize_extensions([*(_RAW_EXTENSIONS or []), *(_JPEG_EXTENSIONS or [])])
)


class Api:
    """JavaScript API exposed to webview for native file/folder operations."""

    # Extension → MIME type map used by read_image_file (avoids mimetypes.guess_type overhead)
    _MIME_MAP: dict = {
        '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
        '.png': 'image/png',  '.gif': 'image/gif',
        '.webp': 'image/webp',
        '.tif': 'image/tiff', '.tiff': 'image/tiff',
    }

    def __init__(self):
        # Cache os.path.realpath(root_path) — root_path is constant for the session
        # but realpath() does a GetFinalPathNameByHandle syscall on Windows each time.
        self._realpath_cache: dict = {}
        self._exposure_mode_cache: dict = {}
        self._has_unsaved_changes: bool = False
        self._cache_cleanup_roots: set[str] = set()
        self._culling_companion_extensions: tuple[str, ...] = _CULLING_COMPANION_EXTENSIONS
        # Set externally by visualizer.main() after window/server come up.
        self._main_window = None
        self._culling_window = None
        self._server_port: int | None = None
        # Async share-with-perch state (job_id -> {progress, cancel_event, thread})
        self._share_jobs: dict = {}
        self._share_jobs_lock = None
        self._active_share_job: str | None = None
        self._perch_account_cache: dict | None = None
        self._perch_account_cache_at: float = 0.0
        self._perch_usage_cache: dict | None = None
        self._perch_usage_cache_at: float = 0.0
        # Async cloud-compute job state (job_id -> {progress, cancel_event,
        # thread, result}). Cloud-compute reuses the Perch JWT
        # (same Clerk identity) — see _check_auth_token() and
        # analyzer/cloud_compute_client.py.
        self._cc_jobs: dict = {}
        self._cc_jobs_lock = None
        # Per-job remote-status poller threads (job_id -> Thread). One thread
        # per job, started at submit/resume, exits when local status becomes
        # terminal or `cancel_event` fires. Centralised polling lets JS render
        # from one bridge call (cloud_compute_list_jobs) without the N+1 query
        # pattern that previously called get_status per job per render.
        self._cc_poll_threads: dict = {}
        # Continuous-retrieval worker threads (job_id -> Thread). Started by
        # cloud_compute_retrieve_results, which loops _cc_drain_packs_once
        # until the job is terminal. Guarded so re-clicking "Retrieve Results"
        # doesn't spawn duplicate download loops (mirrors _cc_poll_threads).
        self._cc_retrieve_threads: dict = {}
        # Short-poll event queue for pack-merged notifications from the
        # background download thread. JS drains via
        # ``cloud_compute_get_pack_events()`` ~every poll tick and triggers a
        # folder rescan so the gallery refreshes as packs land — same UX as
        # local-analysis live updates. Drained-and-cleared each poll.
        self._cc_pack_events: list = []
        # 5-minute TTL cache for /api/usage so the Cloud destination card in
        # the analyze dialog doesn't hit the Worker on every keystroke.
        self._cc_usage_cache: dict | None = None
        self._cc_usage_cache_at: float = 0.0

        # macOS App Sandbox: re-acquire access to every previously-chosen photo
        # folder via its stored security-scoped bookmark, held for the whole
        # session. No-op on every other build (Windows/Linux/Developer-ID mac).
        if _mac_sandbox is not None:
            try:
                n = _mac_sandbox.activate_all_bookmarks()
                if n:
                    info(f'[sandbox] Re-activated {n} folder bookmark(s) for this session.')
            except Exception as e:
                warn(f'[sandbox] bookmark activation failed: {e}')

        # App Store build only: open StoreKit's connection at launch so the
        # storefront (which decides the Support-link destination) is resolved
        # well before the user clicks Support. SKPaymentQueue.storefront is nil
        # until an observer is attached and StoreKit connects asynchronously —
        # priming now avoids a nil-and-fail-closed on the first click. Best
        # effort, backgrounded so it never blocks startup; get_support_url also
        # primes lazily as a fallback. See mac_storefront.prime().
        self._maybe_prime_storefront()

    def _maybe_prime_storefront(self) -> None:
        """Kick off StoreKit priming in the background on the App Store build."""
        if sys.platform != 'darwin':
            return
        try:
            channel = _dist_channel.get_channel() if _dist_channel is not None else 'direct'
        except Exception:
            channel = 'direct'
        if channel != 'appstore':
            return

        def _prime():
            try:
                sf = _load_storefront()
                if sf is None:
                    print('[API] storefront prime: StoreKit unavailable in this bundle', flush=True)
                    return
                sf.prime()
                # Warm the cache so the first Support click is instant.
                country = sf.get_storefront_country()
                print(f'[API] storefront prime: country={country!r}', flush=True)
            except Exception as e:
                print(f'[API] storefront prime error: {e}', flush=True)

        try:
            import threading as _t
            _t.Thread(target=_prime, name='storefront-prime', daemon=True).start()
        except Exception:
            pass

    def _invalidate_account_caches(self) -> None:
        """Drop every identity-scoped cache (Perch account, Perch usage, and
        Cloud Compute usage) so a sign-out / account-switch / token rotation
        can't surface the previous user's numbers. The CC usage cache in
        particular was previously left untouched on sign-out, so the account
        panel kept showing the prior account's "images analyzed this period"."""
        self._perch_account_cache = None
        self._perch_account_cache_at = 0.0
        self._perch_usage_cache = None
        self._perch_usage_cache_at = 0.0
        self._cc_usage_cache = None
        self._cc_usage_cache_at = 0.0

    def notify_dirty(self, is_dirty: bool) -> dict:
        """Called from JS whenever the dirty flag changes."""
        self._has_unsaved_changes = bool(is_dirty)
        return {'success': True}

    def report_js_error(self, error_data: dict) -> dict:
        """Receive an unhandled JS exception or promise rejection and write it
        to the runtime log so it appears in crash reports even without DevTools.
        """
        try:
            err_type = str(error_data.get('type', 'js_error'))
            msg = str(error_data.get('msg', ''))[:500]
            stack = str(error_data.get('stack', ''))[:1500]
            source = str(error_data.get('source', ''))
            line = error_data.get('line', '')
            warn(f'[JS {err_type}] {msg}' + (f' @ {source}:{line}' if source else ''))
            if stack:
                warn(f'[JS {err_type} stack]\n{stack}')
        except Exception:
            pass
        return {'success': True}

    def _root_realpath(self, root_path: str) -> str:
        """Return os.path.realpath(root_path), cached for the lifetime of this Api."""
        if root_path not in self._realpath_cache:
            self._realpath_cache[root_path] = os.path.realpath(root_path)
        return self._realpath_cache[root_path]

    def _track_cache_root(self, root_path: str) -> None:
        """Record a folder root whose RAW preview cache should be cleaned on app close."""
        try:
            rp = str(root_path or '').strip().rstrip('/\\')
            if not rp:
                return
            self._cache_cleanup_roots.add(os.path.abspath(rp))
        except Exception:
            pass

    def _get_exposure_render_mode(self, root_path_real: str) -> str:
        """Return the exposure render mode for a folder, defaulting to legacy behavior."""
        root_key = os.path.abspath(str(root_path_real or '').strip())
        if not root_key:
            return 'legacy_auto_bright_v1'
        cached = self._exposure_mode_cache.get(root_key)
        if cached:
            return cached

        mode = 'legacy_auto_bright_v1'
        meta_path = os.path.join(root_key, '.kestrel', 'kestrel_metadata.json')
        try:
            if os.path.isfile(meta_path):
                with open(meta_path, 'r', encoding='utf-8') as mf:
                    metadata = json.load(mf)
                mode_raw = str(metadata.get('exposure_render_mode', '') or '').strip().lower()
                if mode_raw in {'legacy_auto_bright_v1', 'no_auto_bright_metered_v1'}:
                    mode = mode_raw
                elif mode_raw == 'mixed_per_row_v1':
                    # Without row-level mode, mixed folders should safely fall back to legacy.
                    mode = 'legacy_auto_bright_v1'
                elif str(metadata.get('exposure_pipeline_version', '')).strip() in {'2', '2.0'}:
                    mode = 'no_auto_bright_metered_v1'
        except Exception:
            mode = 'legacy_auto_bright_v1'

        self._exposure_mode_cache[root_key] = mode
        return mode

    def _resolve_editor_target(self, root_path: str, relative_path: str) -> tuple[str, str]:
        """Resolve an editor target from root+relative with boundary-safe normalization."""
        base_root = str(_ALLOWED_ROOT or root_path or '').strip()
        rel = str(relative_path or '').strip()
        if not base_root or not rel:
            return '', ''

        if (base_root.startswith('"') and base_root.endswith('"')) or (base_root.startswith("'") and base_root.endswith("'")):
            base_root = base_root[1:-1]
        if (rel.startswith('"') and rel.endswith('"')) or (rel.startswith("'") and rel.endswith("'")):
            rel = rel[1:-1]

        base_root = os.path.abspath(os.path.expanduser(base_root))
        rel = rel.replace('\\', '/')
        if os.path.isabs(rel):
            return '', base_root

        target = os.path.abspath(os.path.join(base_root, rel))
        return target, base_root

    def _is_within_root(self, path: str, root: str) -> bool:
        if not path or not root:
            return False
        try:
            root_real = os.path.realpath(root)
            # On Windows, ``os.path.realpath`` of a non-existent path under a
            # mapped network drive returns the drive-letter form (``Y:\...``),
            # while ``realpath`` of an existing UNC root returns the canonical
            # UNC form (``\\?\UNC\server\share\...``). Comparing those two
            # spellings mistakenly rejects valid reads — e.g. ``read_image_file``
            # asking for ``Y:\folder\.kestrel\export\foo.jpg`` (a file the user
            # has not yet exported) when the analyzed root is the UNC share.
            # Walk up ``path`` to the deepest existing ancestor, resolve that,
            # then re-append the non-existent tail so the comparison happens on
            # spellings that agree.
            candidate = path
            tail_parts: list[str] = []
            while not os.path.exists(candidate):
                parent = os.path.dirname(candidate)
                if not parent or parent == candidate:
                    break
                tail_parts.append(os.path.basename(candidate))
                candidate = parent
            ancestor_real = os.path.realpath(candidate)
            path_real = (
                os.path.join(ancestor_real, *reversed(tail_parts))
                if tail_parts
                else ancestor_real
            )
            try:
                common = os.path.commonpath([path_real, root_real])
            except ValueError:
                return False
            return common == root_real
        except Exception:
            return False

    def _editor_extension_allowed(self, path: str) -> bool:
        _, ext = os.path.splitext(path)
        return ext.lower() in _ALLOWED_EDITOR_EXTENSIONS

    def _strip_wrapping_quotes(self, value: str) -> str:
        s = str(value or '').strip()
        if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
            s = s[1:-1].strip()
        return s

    def _log_security_reject(self, context: str, reason: str, **details) -> None:
        try:
            parts = []
            for key, val in details.items():
                if val is None:
                    continue
                txt = str(val)
                if len(txt) > 300:
                    txt = txt[:300] + '...'
                parts.append(f'{key}={txt!r}')
            suffix = f' ({", ".join(parts)})' if parts else ''
            warn(f'[security] Reject {context}: {reason}{suffix}')
        except Exception:
            pass

    def _normalize_input_path(self, value: str) -> str:
        s = self._strip_wrapping_quotes(value)
        if not s:
            return ''
        try:
            s = os.path.expanduser(s)
            return os.path.abspath(os.path.normpath(s))
        except Exception:
            return ''

    def _validate_root_dir(self, root_path: str, context: str, require_exists: bool = True) -> tuple[str, str]:
        root_norm = self._normalize_input_path(root_path)
        if not root_norm:
            self._log_security_reject(context, 'Invalid root path', root=root_path)
            return '', 'Invalid root path'

        root_real = os.path.realpath(root_norm)
        if _ALLOWED_ROOT and not self._is_within_root(root_real, _ALLOWED_ROOT):
            self._log_security_reject(context, 'Path outside allowed root', root=root_real, allowed_root=_ALLOWED_ROOT)
            return '', 'Path outside allowed root'

        if require_exists and not os.path.isdir(root_real):
            self._log_security_reject(context, 'Root path is not a directory', root=root_real)
            return '', 'Invalid root path'

        return root_real, ''

    def _resolve_folder_root_and_kestrel(
        self,
        folder_path: str,
        context: str,
        require_root_exists: bool = True,
    ) -> tuple[str, str, str, str]:
        folder_norm = self._normalize_input_path(folder_path)
        if not folder_norm:
            self._log_security_reject(context, 'Invalid folder path', folder_path=folder_path)
            return '', '', '', 'Invalid folder path'

        is_kestrel_folder = os.path.basename(folder_norm).lower() == '.kestrel'
        root_candidate = os.path.dirname(folder_norm) if is_kestrel_folder else folder_norm
        root_real, err = self._validate_root_dir(root_candidate, context=context, require_exists=require_root_exists)
        if err:
            return '', '', '', err

        kestrel_candidate = folder_norm if is_kestrel_folder else os.path.join(root_real, '.kestrel')
        kestrel_real = os.path.realpath(os.path.abspath(kestrel_candidate))
        expected_kestrel = os.path.realpath(os.path.join(root_real, '.kestrel'))
        if kestrel_real != expected_kestrel:
            self._log_security_reject(
                context,
                'Resolved .kestrel path mismatch',
                folder_path=folder_path,
                kestrel_path=kestrel_real,
                expected=expected_kestrel,
            )
            return '', '', '', 'Invalid folder path'

        return root_real, kestrel_real, folder_norm, ''

    def _resolve_path_in_root(
        self,
        root_path: str,
        requested_path: str,
        context: str,
        allow_absolute: bool = True,
    ) -> tuple[str, str, str]:
        root_real, err = self._validate_root_dir(root_path, context=context, require_exists=True)
        if err:
            return '', '', err

        raw = self._strip_wrapping_quotes(requested_path)
        if not raw:
            self._log_security_reject(context, 'Empty path value', requested_path=requested_path)
            return '', '', 'Invalid path'

        raw = raw.replace('\\', '/')
        if os.path.isabs(raw):
            if not allow_absolute:
                self._log_security_reject(context, 'Absolute path not allowed', requested_path=requested_path)
                return '', '', 'Invalid path'
            target_abs = self._normalize_input_path(raw)
        else:
            rel = raw.lstrip('/\\')
            if not rel:
                self._log_security_reject(context, 'Relative path is empty after normalization', requested_path=requested_path)
                return '', '', 'Invalid path'
            target_abs = os.path.abspath(os.path.join(root_real, rel))

        target_real = os.path.realpath(target_abs)
        if not self._is_within_root(target_real, root_real):
            self._log_security_reject(
                context,
                'Path escapes root directory',
                root=root_real,
                requested_path=requested_path,
                resolved_path=target_real,
            )
            return '', '', 'Path escapes root directory'

        return root_real, target_real, ''

    def _sanitize_plain_filename(self, filename: str, context: str) -> str:
        name = self._strip_wrapping_quotes(filename).replace('\\', '/').strip().lstrip('/\\')
        if not name or name in {'.', '..'}:
            self._log_security_reject(context, 'Invalid filename', filename=filename)
            return ''
        if '/' in name or ':' in name:
            self._log_security_reject(context, 'Filename must not contain path separators', filename=filename)
            return ''
        return name

    def _fetch_remote_legal_payload(self) -> dict:
        """Internal helper: fetch https://projectkestrel.org/legal.json.

        Returns a dict with keys ``effective_date``, ``terms_url``,
        ``privacy_url`` on success, or an empty dict if the fetch fails.
        Never raises.
        """
        try:
            import urllib.request
            import ssl
            import certifi

            url = "https://projectkestrel.org/legal.json"
            ctx = ssl.create_default_context(cafile=certifi.where())
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'ProjectKestrel/1.0'},
                method='GET',
            )
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                if not isinstance(data, dict):
                    return {}
                return {
                    'effective_date': str(data.get('effective_date', '') or '').strip(),
                    'terms_url': str(data.get('terms_url', '') or '').strip(),
                    'privacy_url': str(data.get('privacy_url', '') or '').strip(),
                }
        except Exception as e:
            warn(f'[legal] fetch_remote_legal failed: {e}')
            return {}

    def fetch_remote_legal(self):
        """Fetch legal.json from projectkestrel.org to bypass CORS in JS."""
        data = self._fetch_remote_legal_payload()
        if not data:
            return {'success': False, 'error': 'Failed to fetch legal.json'}
        return {'success': True, 'data': data}

    def get_legal_status(self) -> dict:
        """Report legal-agreement state to the UI.

        Fetches ``legal.json`` and compares ``effective_date`` to the stored
        ``legal_agreed_date``. Returns a dict with:

        - ``agreed``: True if the user has accepted terms at least as recent
          as the remote effective date.
        - ``reason``: None, ``'new_user'``, or ``'terms_updated'`` when
          ``agreed`` is False.
        - ``effective_date``, ``terms_url``, ``privacy_url``: remote legal
          metadata (empty strings if the fetch failed).
        - ``install_sent``: whether install telemetry was sent once.

        If the network fetch fails, falls back to the legacy behaviour of
        treating any non-empty ``legal_agreed_version`` as agreement, so
        offline users are never blocked.
        """
        settings = load_persisted_settings()
        stored_date = str(settings.get('legal_agreed_date', '') or '').strip()
        legacy_agreed = str(settings.get('legal_agreed_version', '') or '').strip() != ''
        install_sent = settings.get('installed_telemetry_sent', False)

        remote = self._fetch_remote_legal_payload()
        effective_date = remote.get('effective_date', '')
        terms_url = remote.get('terms_url', '') or 'https://projectkestrel.org/terms-of-use'
        privacy_url = remote.get('privacy_url', '') or 'https://projectkestrel.org/privacy-policy'

        if not effective_date:
            agreed = legacy_agreed
            reason = None if agreed else 'new_user'
            info(f'[legal] get_legal_status (offline fallback): agreed={agreed}')
            return {
                'agreed': agreed,
                'reason': reason,
                'effective_date': '',
                'terms_url': terms_url,
                'privacy_url': privacy_url,
                'install_sent': install_sent,
            }

        if stored_date and stored_date >= effective_date:
            agreed = True
            reason = None
        elif legacy_agreed or stored_date:
            agreed = False
            reason = 'terms_updated'
        else:
            agreed = False
            reason = 'new_user'

        info(
            f'[legal] get_legal_status: agreed={agreed}, reason={reason}, '
            f'stored_date={stored_date!r}, effective_date={effective_date!r}'
        )
        return {
            'agreed': agreed,
            'reason': reason,
            'effective_date': effective_date,
            'terms_url': terms_url,
            'privacy_url': privacy_url,
            'install_sent': install_sent,
        }

    def agree_to_legal(self, effective_date: str = ''):
        """Mark legal agreement as accepted and trigger installation telemetry if needed.

        Parameters
        ----------
        effective_date : str
            The ``effective_date`` from ``legal.json`` that the UI showed to
            the user. Stored in ``legal_agreed_date`` and used for future
            re-acceptance comparisons. If empty, only the legacy
            ``legal_agreed_version`` marker is written.
        """
        settings = load_persisted_settings()
        version = _telemetry._read_version() if _telemetry else 'unknown'
        settings['legal_agreed_version'] = version
        date_str = str(effective_date or '').strip()
        if date_str:
            settings['legal_agreed_date'] = date_str
        info(f'[legal] User agreed to terms (version {version}, effective_date={date_str!r})')

        if not settings.get('installed_telemetry_sent', False):
            if _telemetry:
                mid = _telemetry.get_machine_id(settings)
                _telemetry.send_installation_telemetry(mid, version=version)
                settings['installed_telemetry_sent'] = True
                info('[legal] Initial installation telemetry triggered.')

        save_persisted_settings(settings)
        return {'success': True}
    
    @staticmethod
    def _pywebview_folder_dialog(allow_multiple: bool):
        """Open pywebview's native folder picker; return list[str] of paths.

        On macOS this routes through NSOpenPanel/powerbox, which is the only
        sandbox-legal folder picker (osascript needs Apple Events, tkinter is
        unavailable). Used by the sandboxed App Store build, and as the
        Windows/Linux multi-select picker. Returns [] on cancel/unavailable.
        """
        import webview
        wins = getattr(webview, 'windows', None)
        if not wins:
            return []
        win = wins[0]
        dialog_kind = None
        file_dialog = getattr(webview, 'FileDialog', None)
        if file_dialog is not None and hasattr(file_dialog, 'FOLDER'):
            dialog_kind = file_dialog.FOLDER
        elif hasattr(webview, 'FOLDER_DIALOG'):
            dialog_kind = webview.FOLDER_DIALOG
        if dialog_kind is None:
            return []
        result = win.create_file_dialog(dialog_kind, allow_multiple=allow_multiple)
        if not result:
            return []
        return [str(p) for p in result if p]

    def choose_directory(self):
        """Open native folder picker dialog.
        Returns: absolute path to selected folder, or None if cancelled.
        """
        try:
            sandboxed = (
                sys.platform == 'darwin'
                and _mac_sandbox is not None
                and _mac_sandbox.is_sandboxed()
            )
            if sandboxed:
                # Sandbox: powerbox folder panel + persist a security-scoped
                # bookmark so the grant survives across launches.
                paths = self._pywebview_folder_dialog(allow_multiple=False)
                folder = paths[0] if paths else ''
                if folder:
                    _mac_sandbox.remember_folder(folder)
            elif sys.platform == 'darwin':
                script = 'POSIX path of (choose folder with prompt "Select folder containing analyzed photos")'
                result = subprocess.run(
                    ['osascript', '-e', script],
                    capture_output=True,
                    text=True,
                    timeout=120
                )
                folder = result.stdout.strip() if result.returncode == 0 else ''
            else:
                # tkinter filedialog works on both Windows and Linux
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes('-topmost', True)
                folder = filedialog.askdirectory(title="Select folder containing analyzed photos")
                root.destroy()

            info(f'[API] choose_directory -> {folder!r}' if folder else '[API] choose_directory -> cancelled')
            return folder or None
        except Exception as e:
            error(f'[API] choose_directory error: {e}')
            return None

    def choose_directories(self):
        """Open native folder picker dialog with multi-select support.

        Returns: list[str] of selected folder paths (empty list if cancelled).

        Platform-specific:
        - macOS: osascript's "choose folder ... with multiple selections allowed"
          is native, reliable, and avoids any pywebview involvement.
        - Windows: pywebview's create_file_dialog uses WebView2's native
          IFileOpenDialog with FOS_ALLOWMULTISELECT. Falls back to a single
          tkinter pick if pywebview is unavailable (rare in a desktop build)
          or throws — the user can click "+ Load Folders…" again for each
          additional folder in the worst case.
        - Linux: pywebview's GTK/Qt backend; same fallback as Windows.
        """
        try:
            if (
                sys.platform == 'darwin'
                and _mac_sandbox is not None
                and _mac_sandbox.is_sandboxed()
            ):
                # Sandbox: powerbox multi-folder panel + persist a
                # security-scoped bookmark per chosen folder.
                paths = self._pywebview_folder_dialog(allow_multiple=True)
                if not paths:
                    info('[API] choose_directories -> cancelled')
                    return []
                for p in paths:
                    _mac_sandbox.remember_folder(p)
                info(f'[API] choose_directories (sandbox) -> {len(paths)} path(s)')
                return paths

            if sys.platform == 'darwin':
                # AppleScript returns one alias per line when "with multiple
                # selections allowed" is used. POSIX path of {…} converts each
                # alias to a slash-path string.
                script = (
                    'set chosen to choose folder with prompt '
                    '"Select folders containing analyzed photos" with multiple selections allowed\n'
                    'set out to ""\n'
                    'repeat with f in chosen\n'
                    '    set out to out & POSIX path of f & "\\n"\n'
                    'end repeat\n'
                    'return out'
                )
                result = subprocess.run(
                    ['osascript', '-e', script],
                    capture_output=True,
                    text=True,
                    timeout=120
                )
                if result.returncode != 0:
                    info('[API] choose_directories -> cancelled')
                    return []
                paths = [p.strip() for p in result.stdout.splitlines() if p.strip()]
                info(f'[API] choose_directories (macOS) -> {len(paths)} path(s)')
                return paths

            # Windows/Linux: try pywebview's native multi-select dialog.
            # Prefer the modern FileDialog.FOLDER enum (pywebview >= 5) and
            # fall back to the deprecated FOLDER_DIALOG constant on older
            # pywebview builds. If neither is available or the call throws,
            # fall through to the single-pick tkinter fallback.
            try:
                import webview
                wins = getattr(webview, 'windows', None)
                if wins:
                    win = wins[0]
                    dialog_kind = None
                    file_dialog = getattr(webview, 'FileDialog', None)
                    if file_dialog is not None and hasattr(file_dialog, 'FOLDER'):
                        dialog_kind = file_dialog.FOLDER
                    elif hasattr(webview, 'FOLDER_DIALOG'):
                        dialog_kind = webview.FOLDER_DIALOG
                    if dialog_kind is not None:
                        result = win.create_file_dialog(
                            dialog_kind,
                            allow_multiple=True,
                        )
                        if not result:
                            info('[API] choose_directories -> cancelled')
                            return []
                        # create_file_dialog returns a tuple of path strings
                        paths = [str(p) for p in result if p]
                        info(f'[API] choose_directories (pywebview) -> {len(paths)} path(s)')
                        return paths
            except Exception as e:
                # Fall through to tkinter single-pick if the pywebview path
                # fails (logged for diagnostics, not user-visible).
                info(f'[API] choose_directories pywebview path failed ({e!r}); falling back to single-pick')

            # Tkinter has no native multi-folder picker, so single-pick is the
            # safe fallback. User can click "+ Load Folders…" again for more.
            single = self.choose_directory()
            return [single] if single else []
        except Exception as e:
            error(f'[API] choose_directories error: {e}')
            return []

    def open_file_explorer(self, folder_path):
        """Open a folder in the native file explorer."""
        root_real, err = self._validate_root_dir(folder_path, context='open_file_explorer', require_exists=True)
        if err:
            return {'success': False, 'error': err}

        try:
            if sys.platform.startswith('win'):
                if hasattr(os, 'startfile'):
                    os.startfile(root_real)
                else:
                    # Fallback for Windows if startfile is somehow missing (e.g. specialized python builds)
                    subprocess.run(['explorer', root_real], check=False)
            elif sys.platform == 'darwin':
                # Sandbox can't Popen /usr/bin/open; route through NSWorkspace.
                if (
                    _mac_sandbox is not None
                    and _mac_sandbox.is_sandboxed()
                    and _mac_sandbox.open_default(root_real)
                ):
                    pass
                else:
                    subprocess.run(['open', root_real], check=False)
            else:
                subprocess.run(['xdg-open', root_real], check=False)
            return {'success': True, 'path': root_real}
        except Exception as e:
            error(f'[API] open_file_explorer error: {e}')
            return {'success': False, 'error': str(e)}

    def choose_application(self):
        """Open native file picker for choosing an application executable.
        Returns: absolute path to selected file, or None if cancelled.
        """
        try:
            if (
                sys.platform == 'darwin'
                and _mac_sandbox is not None
                and _mac_sandbox.is_sandboxed()
            ):
                # Sandbox: powerbox open panel (NSOpenPanel treats .app bundles
                # as selectable files). Persist a security-scoped bookmark so we
                # can re-open files with this editor on later launches.
                import webview
                wins = getattr(webview, 'windows', None)
                if not wins:
                    return None
                win = wins[0]
                file_dialog = getattr(webview, 'FileDialog', None)
                open_kind = (
                    file_dialog.OPEN if (file_dialog is not None and hasattr(file_dialog, 'OPEN'))
                    else getattr(webview, 'OPEN_DIALOG', None)
                )
                if open_kind is None:
                    return None
                result = win.create_file_dialog(
                    open_kind,
                    allow_multiple=False,
                    file_types=('Applications (*.app)', 'All files (*.*)'),
                )
                chosen = (str(result[0]) if result else '') or ''
                if chosen:
                    _mac_sandbox.remember_folder(chosen)
                return chosen or None
            if sys.platform == 'darwin':
                import subprocess as _sp
                script = 'POSIX path of (choose file of type {"app","APPL"} with prompt "Select an application")'
                result = _sp.run(['osascript', '-e', script], capture_output=True, text=True, timeout=120)
                if result.returncode == 0 and result.stdout.strip():
                    return result.stdout.strip()
                return None
            else:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes('-topmost', True)
                if sys.platform.startswith('win'):
                    filetypes = [('Executables', '*.exe'), ('All Files', '*.*')]
                else:
                    filetypes = [('All Files', '*.*')]
                filepath = filedialog.askopenfilename(
                    title="Select application executable",
                    filetypes=filetypes
                )
                root.destroy()
                return filepath if filepath else None
        except Exception as e:
            error(f'[API] choose_application error: {e}')
            return None

    def read_kestrel_csv(self, folder_path):
        """Read the kestrel_database.csv from the given folder path.
        
        Args:
            folder_path: Absolute path to folder (may be parent folder or .kestrel folder itself)
            
        Returns:
            dict with 'success': bool, 'data': str (CSV content), 'error': str, 'path': str, 'root': str
        """
        
        try:
            parent_folder, kestrel_dir, _, err = self._resolve_folder_root_and_kestrel(
                folder_path,
                context='read_kestrel_csv',
                require_root_exists=True,
            )
            if err:
                return {
                    'success': False,
                    'error': err,
                    'path': '',
                    'data': ''
                }

            csv_path = os.path.join(kestrel_dir, 'kestrel_database.csv')
            if not os.path.exists(csv_path):
                
                return {
                    'success': False,
                    'error': f'Could not find kestrel_database.csv at: {csv_path}',
                    'path': csv_path,
                    'data': ''
                }
            
            with open(csv_path, 'r', encoding='utf-8') as f:
                data = f.read()

            self._track_cache_root(parent_folder)
            
            
            return {
                'success': True,
                'data': data,
                'error': '',
                'path': csv_path,
                'root': parent_folder
            }
        except Exception as e:
            error(f'[API] read_kestrel_csv error: {e}')
            return {
                'success': False,
                'error': str(e),
                'path': '',
                'data': ''
            }

    def read_kestrel_metadata(self, folder_path: str):
        """Read kestrel_metadata.json from a folder's .kestrel directory."""
        try:
            _, kestrel_dir, _, err = self._resolve_folder_root_and_kestrel(
                folder_path,
                context='read_kestrel_metadata',
                require_root_exists=True,
            )
            if err:
                return {'success': False, 'error': err}

            meta_path = os.path.join(kestrel_dir, 'kestrel_metadata.json')
            if not os.path.isfile(meta_path):
                return {'success': False, 'error': 'Metadata file not found'}
            with open(meta_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return {'success': True, 'metadata': data}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def clear_kestrel_data(self, folder_path: str):
        """Delete the contents of the .kestrel folder within the given folder."""
        try:
            _, kestrel_dir, _, err = self._resolve_folder_root_and_kestrel(
                folder_path,
                context='clear_kestrel_data',
                require_root_exists=True,
            )
            if err:
                return {'success': False, 'error': err}

            if not os.path.isdir(kestrel_dir):
                return {'success': True, 'message': 'No .kestrel folder found'}

            shutil.rmtree(kestrel_dir)
            info(f'[API] clear_kestrel_data: removed {kestrel_dir}')
            return {'success': True, 'message': 'Kestrel analysis data cleared'}
        except Exception as e:
            error(f'[API] clear_kestrel_data error: {e}')
            return {'success': False, 'error': str(e)}

    def is_frozen_app(self):
        """Return whether the application is running as a frozen (PyInstaller) build."""
        return {'frozen': getattr(sys, 'frozen', False)}

    def get_app_version(self):
        """Return the current application version from config."""
        try:
            from kestrel_analyzer.config import VERSION
            return {'success': True, 'version': VERSION}
        except Exception:
            try:
                from analyzer.kestrel_analyzer.config import VERSION
                return {'success': True, 'version': VERSION}
            except Exception:
                return {'success': True, 'version': 'unknown'}

    def report_bridge_ready(self):
        """Diagnostic endpoint for --api-probe mode.

        Called from JS on the ``pywebviewready`` event to prove the JS-Python
        bridge round-trips. Safe to call at any time; side-effect-free unless a
        probe is listening (when ``self._probe_ready_event`` is set, this stores
        the payload on ``self._probe_ready_payload`` and signals the event).
        """
        from datetime import datetime, timezone
        try:
            from kestrel_analyzer.config import VERSION
        except Exception:
            try:
                from analyzer.kestrel_analyzer.config import VERSION
            except Exception:
                VERSION = 'unknown'
        payload = {
            'ok': True,
            'version': VERSION,
            'frozen': bool(getattr(sys, 'frozen', False)),
            'timestamp': datetime.now(timezone.utc).isoformat(),
        }
        evt = getattr(self, '_probe_ready_event', None)
        if evt is not None:
            self._probe_ready_payload = payload
            try:
                evt.set()
            except Exception:
                pass
        return payload

    def get_species_family_map(self):
        """Return a {species_display_name: family_display_name} mapping for the
        bird species classifier's North American taxonomy.

        Joins ``labels_scispecies.csv`` (Species → Scientific Family) with
        ``scispecies_dispname.csv`` (Scientific Family → Display Name) and
        caches the result on the bridge instance. Used by the frontend to
        auto-link species/family chips and populate species autocomplete.
        """
        cached = getattr(self, '_species_family_map_cache', None)
        if cached is not None:
            return cached
        try:
            import csv
            try:
                from kestrel_analyzer.config import MODELS_DIR as _models_dir
            except ImportError:
                from analyzer.kestrel_analyzer.config import MODELS_DIR as _models_dir
            base = str(_models_dir)
            species_to_scifam: dict[str, str] = {}
            with open(os.path.join(base, 'labels_scispecies.csv'), 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sp = (row.get('Species') or '').strip()
                    fam = (row.get('Scientific Family') or '').strip()
                    if sp and fam:
                        species_to_scifam[sp] = fam
            scifam_to_display: dict[str, str] = {}
            with open(os.path.join(base, 'scispecies_dispname.csv'), 'r', encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sci = (row.get('Scientific Family') or '').strip()
                    disp = (row.get('Display Name') or '').strip()
                    if sci and disp:
                        scifam_to_display[sci] = disp
            mapping: dict[str, str] = {}
            for sp, sci in species_to_scifam.items():
                disp = scifam_to_display.get(sci, sci)
                mapping[sp] = disp
            result = {'success': True, 'map': mapping}
        except Exception as e:
            error(f'[API] get_species_family_map error: {e}')
            result = {'success': False, 'error': str(e), 'map': {}}
        self._species_family_map_cache = result
        return result

    # ------------------------------------------------------------------ #
    #  Global bird catalog (regional + fuzzy combobox)                    #
    # ------------------------------------------------------------------ #

    def _get_bird_catalog(self):
        """Process-wide catalog singleton, cached on the bridge instance.

        Returns ``None`` on load failure so callers can degrade gracefully
        rather than 500-ing the entire combobox.
        """
        cached = getattr(self, '_bird_catalog_cache', None)
        if cached is not None:
            return cached
        try:
            try:
                from kestrel_analyzer.bird_catalog import get_catalog
            except ImportError:
                from analyzer.kestrel_analyzer.bird_catalog import get_catalog
            cached = get_catalog()
        except Exception as e:
            error(f'[API] bird catalog load error: {e}')
            cached = None
        self._bird_catalog_cache = cached
        return cached

    def get_bird_catalog_meta(self):
        """Return UI metadata: region labels + default selection + catalog size.

        Used by the frontend to populate the region picker in settings without
        downloading the full catalog. Cached separately from the records.
        """
        try:
            try:
                from kestrel_analyzer.bird_catalog import (
                    ALLOWED_REGION_CODES, REGION_LABELS, DEFAULT_REGION_SELECTION,
                )
            except ImportError:
                from analyzer.kestrel_analyzer.bird_catalog import (
                    ALLOWED_REGION_CODES, REGION_LABELS, DEFAULT_REGION_SELECTION,
                )
            cat = self._get_bird_catalog()
            return {
                'success': True,
                'regions': [
                    {'code': code, 'label': REGION_LABELS.get(code, code)}
                    for code in ALLOWED_REGION_CODES
                ],
                'default_regions': list(DEFAULT_REGION_SELECTION),
                'total_species': len(cat) if cat is not None else 0,
            }
        except Exception as e:
            error(f'[API] get_bird_catalog_meta error: {e}')
            return {'success': False, 'error': str(e),
                    'regions': [], 'default_regions': ['NA'], 'total_species': 0}

    def search_birds(self, query='', regions=None, limit=20):
        """Region-filtered fuzzy search across the global bird catalog.

        Parameters mirror the JS combobox: ``query`` is the text the user has
        typed (may be empty to seed the dropdown), ``regions`` is the list of
        biogeographic codes currently selected in Settings, and ``limit`` caps
        the number of returned records.
        """
        try:
            try:
                from kestrel_analyzer.bird_catalog import record_to_dict
            except ImportError:
                from analyzer.kestrel_analyzer.bird_catalog import record_to_dict
            cat = self._get_bird_catalog()
            if cat is None:
                return {'success': False, 'error': 'catalog unavailable', 'results': []}
            q = '' if query is None else str(query)
            sel = regions if isinstance(regions, (list, tuple)) else ['NA']
            try:
                n = int(limit)
            except (TypeError, ValueError):
                n = 20
            n = max(1, min(100, n))
            results = cat.search(q, sel, limit=n)
            return {'success': True, 'results': [record_to_dict(r) for r in results]}
        except Exception as e:
            error(f'[API] search_birds error: {e}')
            return {'success': False, 'error': str(e), 'results': []}

    def search_families(self, query='', regions=None, limit=20):
        """Region-filtered fuzzy search across unique bird families."""
        try:
            try:
                from kestrel_analyzer.bird_catalog import family_entry_to_dict
            except ImportError:
                from analyzer.kestrel_analyzer.bird_catalog import family_entry_to_dict
            cat = self._get_bird_catalog()
            if cat is None:
                return {'success': False, 'error': 'catalog unavailable', 'results': []}
            q = '' if query is None else str(query)
            sel = regions if isinstance(regions, (list, tuple)) else ['NA']
            try:
                n = int(limit)
            except (TypeError, ValueError):
                n = 20
            n = max(1, min(100, n))
            results = cat.search_families(q, sel, limit=n)
            return {'success': True, 'results': [family_entry_to_dict(f) for f in results]}
        except Exception as e:
            error(f'[API] search_families error: {e}')
            return {'success': False, 'error': str(e), 'results': []}

    def lookup_birds(self, names=None):
        """Resolve a list of canonical names (already-applied pills) to records.

        Returns a ``{ canonical_name: record_dict }`` map so the frontend can
        render the scientific-name subtext for pills it inherited from a
        previous session (where the catalog wasn't yet loaded into memory).
        Unknown names are simply omitted from the response.
        """
        try:
            try:
                from kestrel_analyzer.bird_catalog import record_to_dict
            except ImportError:
                from analyzer.kestrel_analyzer.bird_catalog import record_to_dict
            cat = self._get_bird_catalog()
            if cat is None:
                return {'success': False, 'error': 'catalog unavailable', 'map': {}}
            out: dict = {}
            if isinstance(names, (list, tuple)):
                for raw in names:
                    if not isinstance(raw, str):
                        continue
                    rec = cat.lookup(raw)
                    if rec is not None:
                        out[rec.canonical_common_name] = record_to_dict(rec)
            return {'success': True, 'map': out}
        except Exception as e:
            error(f'[API] lookup_birds error: {e}')
            return {'success': False, 'error': str(e), 'map': {}}

    def get_family_sci_map(self):
        """Return the catalog's full ``family_common -> family_sci`` map.

        Hydrated once on the JS side at startup so family-tier pills can
        resolve their italicised scientific-family subtext directly,
        without depending on a sibling species record being present in
        the same scene.
        """
        try:
            cat = self._get_bird_catalog()
            if cat is None:
                return {'success': False, 'error': 'catalog unavailable', 'map': {}}
            return {'success': True, 'map': cat.family_sci_map}
        except Exception as e:
            error(f'[API] get_family_sci_map error: {e}')
            return {'success': False, 'error': str(e), 'map': {}}

    def fetch_remote_version(self):
        """Fetch version.json from projectkestrel.org to bypass CORS in JS."""
        try:
            import urllib.request
            import urllib.error
            import json
            import ssl
            import certifi
            
            url = "https://projectkestrel.org/version.json"
            ctx = ssl.create_default_context(cafile=certifi.where())
            
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'ProjectKestrel/1.0'},
                method='GET'
            )
            
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                return {'success': True, 'data': data}
        except Exception as e:
            error(f'[API] fetch_remote_version error: {e}')
            return {'success': False, 'error': str(e)}

    def get_platform_info(self):
        """Return platform information (windows, macos, linux)."""
        import sys
        if sys.platform == 'darwin':
            return {'success': True, 'platform': 'macos'}
        elif sys.platform == 'win32':
            return {'success': True, 'platform': 'windows'}
        else:
            return {'success': True, 'platform': 'linux'}

    def get_dist_channel(self):
        """Return this build's distribution channel.

        ``'direct'``  -> website download (notarized DMG / Windows installer / Store).
        ``'appstore'``-> the sandboxed Mac App Store build.

        The frontend uses this to branch What's New copy and cloud-compute CTAs
        (e.g. App-Store-safe language) without needing a new release. Independent
        of sandbox detection — see dist_channel.py. Defaults to 'direct' if the
        helper is unavailable for any reason.
        """
        try:
            if _dist_channel is not None:
                return {'success': True, 'channel': _dist_channel.get_channel()}
        except Exception:
            pass
        return {'success': True, 'channel': 'direct'}

    def get_support_url(self):
        """Return the URL the "Support Project Kestrel" CTAs should open.

        ``/support-me`` includes the donate option; ``/support`` is the same page
        with no payment path at all. ``donate`` reports which one you got.

        Apple's anti-steering rule is **storefront-scoped, not geographic**. The
        US storefront is explicitly carved out of it (Guideline 3.1.1(a), and the
        3.1.3 preamble); every other storefront is not. So the App Store build
        may link out to Stripe for US-storefront customers only, and asks
        StoreKit which it has. Non-App-Store builds short-circuit on channel and
        never touch StoreKit — that also keeps the DMG/Windows paths free of any
        new dependency.

        Deliberately NOT IP geolocation: the rule keys on the App Store account's
        storefront, not where the machine is. See mac_storefront.py.

        Fails closed — any unresolved state yields the no-payment page.
        """
        channel = 'direct'
        try:
            if _dist_channel is not None:
                channel = _dist_channel.get_channel()
        except Exception:
            pass

        if channel != 'appstore':
            return {'success': True, 'url': SUPPORT_URL_FULL, 'donate': True}

        storefront = _load_storefront()
        if storefront is None:
            # StoreKit didn't load in this bundle — fail closed, and say so
            # visibly (mac_storefront's own logging isn't wired to stdout).
            print('[API] support_url: channel=appstore storekit=unavailable -> /support', flush=True)
            return {'success': True, 'url': SUPPORT_URL_NO_PAYMENT, 'donate': False}
        try:
            country = storefront.get_storefront_country()
        except Exception as e:
            country = None
            print(f'[API] support_url: storefront lookup error: {e}', flush=True)

        is_us = country == storefront.US_STOREFRONT_CODE
        url = SUPPORT_URL_FULL if is_us else SUPPORT_URL_NO_PAYMENT
        # One visible line to disambiguate the outcome on-device: country=None
        # means StoreKit loaded but the storefront never resolved (priming);
        # a real code like 'GBR' means a genuine non-US storefront.
        print(f'[API] support_url: channel=appstore country={country!r} -> '
              f"{'/support-me' if is_us else '/support'}", flush=True)
        return {'success': True, 'url': url, 'donate': is_us}

    def is_windows_store_app(self):
        """Check if running as a Windows Store app."""
        try:
            import sys
            if sys.platform != 'win32':
                return {'success': True, 'is_store': False}
            # Check if running from Program Files\WindowsApps (typical Store app location)
            import os
            app_path = os.path.dirname(sys.executable)
            is_store = 'WindowsApps' in app_path or os.environ.get('APPX_PACKAGE_ROOT') is not None
            return {'success': True, 'is_store': is_store}
        except Exception:
            return {'success': True, 'is_store': False}

    def inspect_folder(self, folder_path: str):
        """Return lightweight folder summary (total images, processed count)."""
        try:
            folder_real, err = self._validate_root_dir(folder_path, context='inspect_folder', require_exists=True)
            if err:
                return {'success': False, 'error': err}

            import importlib
            inspector = None
            try:
                inspector = importlib.import_module('analyzer.folder_inspector')
            except Exception:
                try:
                    inspector = importlib.import_module('folder_inspector')
                except Exception:
                    inspector = None
            if inspector is None or not hasattr(inspector, 'inspect_folder'):
                return {'success': False, 'error': 'Inspector unavailable'}
            info = inspector.inspect_folder(folder_real)
            return {'success': True, 'info': info}
        except Exception as e:
            error(f'[API] inspect_folder error: {e}')
            return {'success': False, 'error': str(e)}

    def inspect_folders(self, paths):
        """Batch-inspect multiple folders. Expects a list of absolute paths."""
        try:
            import importlib
            inspector = None
            try:
                inspector = importlib.import_module('analyzer.folder_inspector')
            except Exception:
                try:
                    inspector = importlib.import_module('folder_inspector')
                except Exception:
                    inspector = None
            if inspector is None or not hasattr(inspector, 'inspect_folders'):
                return {'success': False, 'error': 'Inspector unavailable', 'results': {}}
            if isinstance(paths, str):
                try:
                    paths = json.loads(paths)
                except Exception:
                    paths = [paths]

            if not isinstance(paths, list):
                return {'success': False, 'error': 'paths must be a list', 'results': {}}

            validated_paths = []
            invalid_paths = []
            for raw in paths:
                root_real, err = self._validate_root_dir(raw, context='inspect_folders', require_exists=True)
                if err:
                    invalid_paths.append(str(raw))
                    continue
                validated_paths.append(root_real)

            if invalid_paths:
                self._log_security_reject('inspect_folders', 'One or more invalid folder paths', invalid_count=len(invalid_paths))
                return {
                    'success': False,
                    'error': 'Invalid folder path in request',
                    'invalid_paths': invalid_paths,
                    'results': {},
                }

            results = inspector.inspect_folders(validated_paths)
            return {'success': True, 'results': results}
        except Exception as e:
            error(f'[API] inspect_folders error: {e}')
            return {'success': False, 'error': str(e), 'results': {}}
    
    def read_image_file(self, relative_path, root_path):
        """Read an image file and return it as base64-encoded data.
        
        Args:
            relative_path: Path relative to root (e.g., ".kestrel/export/photo.jpg") 
                          OR absolute path (for backward compatibility with old databases)
            root_path: Absolute path to root folder
            
        Returns:
            dict with 'success': bool, 'data': str (base64), 'mime': str, 'error': str
        """
        try:
            _, full_path, err = self._resolve_path_in_root(
                root_path,
                relative_path,
                context='read_image_file',
                allow_absolute=True,
            )
            if err:
                return {'success': False, 'error': err, 'data': '', 'mime': ''}

            # Read — let open() raise FileNotFoundError rather than a separate stat call
            try:
                with open(full_path, 'rb') as f:
                    data = f.read()
            except FileNotFoundError:
                return {'success': False, 'error': f'File not found: {full_path}', 'data': '', 'mime': ''}

            ext = os.path.splitext(full_path)[1].lower()
            mime_type = self._MIME_MAP.get(ext, 'image/jpeg')

            return {
                'success': True,
                'data': base64.b64encode(data).decode('ascii'),
                'mime': mime_type,
                'error': ''
            }
        except Exception as e:
            error(f'[API] read_image_file error: {e}')
            return {'success': False, 'error': str(e), 'data': '', 'mime': ''}

    def list_subfolders(self, root_path: str, max_depth: int = 3):
        """Recursively list subfolders under root_path, flagging those with .kestrel.

        Args:
            root_path: Absolute path to the root folder to scan.
            max_depth:  How many directory levels to descend (1 = direct children only).

        Returns:
            dict with 'success': bool, 'tree': list[node], 'error': str
            Each node: {name, path, has_kestrel, children: [...]}
        """
        try:
            root_path, err = self._validate_root_dir(root_path, context='list_subfolders', require_exists=True)
            if err:
                return {'success': False, 'tree': [], 'error': err}

            # Safety caps
            max_depth = max(1, min(int(max_depth), 6))
            try:
                MAX_NODES = max(100, int(os.environ.get('KESTREL_TREE_NODE_LIMIT', '2000')))
            except Exception:
                MAX_NODES = 2000
            node_count = [0]
            limit_reached = [False]

            def _scan(dir_path: str, depth: int) -> list:
                if depth < 1 or node_count[0] >= MAX_NODES:
                    return []
                result = []
                try:
                    entries = sorted(os.scandir(dir_path), key=lambda e: e.name.lower())
                except PermissionError:
                    return []
                for entry in entries:
                    if node_count[0] >= MAX_NODES:
                        limit_reached[0] = True
                        break
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    name = entry.name
                    if name.startswith('.') or name in ('__pycache__', '$RECYCLE.BIN', 'System Volume Information'):
                        continue
                    node_count[0] += 1
                    full = entry.path
                    has_kestrel = os.path.isfile(os.path.join(full, '.kestrel', 'kestrel_database.csv'))
                    kestrel_version = ''
                    has_perch_link = has_kestrel and os.path.isfile(
                        os.path.join(full, '.kestrel', 'perch_link.json')
                    )
                    if has_kestrel:
                        try:
                            meta_path = os.path.join(full, '.kestrel', 'kestrel_metadata.json')
                            if os.path.isfile(meta_path):
                                with open(meta_path, 'r', encoding='utf-8') as mf:
                                    kestrel_version = json.load(mf).get('version', '')
                        except Exception:
                            pass
                    children = _scan(full, depth - 1)
                    result.append({
                        'name': name,
                        'path': full,
                        'has_kestrel': has_kestrel,
                        'has_perch_link': has_perch_link,
                        'kestrel_version': kestrel_version,
                        'children': children,
                    })
                return result

            tree = _scan(root_path, max_depth)
            root_has_kestrel = os.path.isfile(os.path.join(root_path, '.kestrel', 'kestrel_database.csv'))
            root_has_perch_link = root_has_kestrel and os.path.isfile(
                os.path.join(root_path, '.kestrel', 'perch_link.json')
            )
            root_kestrel_version = ''
            if root_has_kestrel:
                try:
                    meta_path = os.path.join(root_path, '.kestrel', 'kestrel_metadata.json')
                    if os.path.isfile(meta_path):
                        with open(meta_path, 'r', encoding='utf-8') as mf:
                            root_kestrel_version = json.load(mf).get('version', '')
                except Exception:
                    pass
            return {
                'success': True,
                'tree': tree,
                'root_has_kestrel': root_has_kestrel,
                'root_has_perch_link': root_has_perch_link,
                'root_kestrel_version': root_kestrel_version,
                'error': '',
                'nodes': node_count[0],
                'truncated': bool(limit_reached[0]),
            }
        except Exception as e:
            error(f'[API] list_subfolders error: {e}')
            return {'success': False, 'tree': [], 'error': str(e)}

    def write_kestrel_csv(self, folder_path: str, csv_content: str):
        """Write CSV content back to .kestrel/kestrel_database.csv for the given folder."""
        try:
            _, kestrel_dir, _, err = self._resolve_folder_root_and_kestrel(
                folder_path,
                context='write_kestrel_csv',
                require_root_exists=True,
            )
            if err:
                return {'success': False, 'error': err}

            csv_path = os.path.join(kestrel_dir, 'kestrel_database.csv')
            if not os.path.exists(csv_path):
                return {'success': False, 'error': f'CSV not found: {csv_path}'}
            with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
                f.write(csv_content)
            return {'success': True, 'path': csv_path}
        except Exception as e:
            error(f'[API] write_kestrel_csv({folder_path!r}) error: {e}')
            return {'success': False, 'error': str(e)}

    def apply_normalization(self, folder_path: str, mode: str = None) -> dict:
        """Compute star ratings for all rows in a folder's database using the active rating profile.

        Reads the ``rating_profile`` setting, looks up its quality-score thresholds, and maps
        each image's raw quality score to a 1–5 star rating without any rank-based normalization.
        Returns the computed map WITHOUT writing to the CSV file.

        Also caches the folder's quality distribution in kestrel_metadata.json for potential
        future use (e.g. histogram display).

        The ``mode`` parameter is accepted for API compatibility but is ignored; profile
        thresholds always apply.

        Returns:
            {
              'success': bool,
              'normalized_ratings': {filename: int, ...},  # 0-5 for every row
              'mode_used': str,  # the active profile name
              'error': str
            }
        """
        try:
            import pandas as pd

            try:
                from kestrel_analyzer.ratings import (
                    get_profile_thresholds,
                    quality_to_rating,
                )
            except ImportError:
                from analyzer.kestrel_analyzer.ratings import (
                    get_profile_thresholds,
                    quality_to_rating,
                )

            folder_path, kestrel_dir, _, err = self._resolve_folder_root_and_kestrel(
                folder_path,
                context='apply_normalization',
                require_root_exists=True,
            )
            if err:
                return {'success': False, 'error': err, 'normalized_ratings': {}, 'mode_used': ''}

            csv_path = os.path.join(kestrel_dir, 'kestrel_database.csv')

            if not os.path.exists(csv_path):
                return {'success': False, 'error': 'No database found', 'normalized_ratings': {}, 'mode_used': ''}

            settings = load_persisted_settings()
            profile = settings.get('rating_profile', 'balanced')
            thresholds = get_profile_thresholds(profile)

            df = pd.read_csv(csv_path)
            if df.empty:
                return {'success': True, 'normalized_ratings': {}, 'mode_used': profile, 'error': ''}

            # --- Map quality scores to star ratings (in memory only — no CSV write) ---
            if 'filename' not in df.columns or 'quality' not in df.columns:
                return {'success': True, 'normalized_ratings': {}, 'mode_used': profile, 'error': ''}

            def _get_rating(q_val):
                try:
                    return quality_to_rating(float(q_val), thresholds)
                except (TypeError, ValueError):
                    return 0

            normalized_map = {
                str(row['filename']): _get_rating(row['quality'])
                for _, row in df.iterrows()
            }
            
            return {
                'success': True,
                'normalized_ratings': normalized_map,
                'mode_used': profile,
                'error': '',
            }
        except Exception as e:
            error(f'[API] apply_normalization error: {e}')
            return {'success': False, 'error': str(e), 'normalized_ratings': {}, 'mode_used': ''}

    def read_kestrel_scenedata(self, folder_path: str) -> dict:
        """Read kestrel_scenedata.json from a folder's .kestrel directory.

        Returns:
            {'success': bool, 'data': dict, 'error': str}
        """
        try:
            root_path, kestrel_dir, _, err = self._resolve_folder_root_and_kestrel(
                folder_path,
                context='read_kestrel_scenedata',
                require_root_exists=True,
            )
            if err:
                return {'success': False, 'data': {}, 'error': err}

            self._track_cache_root(root_path)
            scenedata_path = os.path.join(kestrel_dir, 'kestrel_scenedata.json')

            if not os.path.exists(scenedata_path):
                # Return an empty-but-valid structure; the UI will fall back to scene_count grouping
                
                return {'success': True, 'data': {'version': '2.0', 'image_ratings': {}, 'scenes': {}}, 'error': ''}

            with open(scenedata_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            # Ensure expected keys
            data.setdefault('version', '2.0')
            data.setdefault('image_ratings', {})
            data.setdefault('scenes', {})
            
            return {'success': True, 'data': data, 'error': ''}
        except Exception as e:
            error(f'[API] read_kestrel_scenedata({folder_path!r}) error: {e}')
            return {'success': False, 'data': {}, 'error': str(e)}

    def write_kestrel_scenedata(self, folder_path: str, scenedata: dict) -> dict:
        """Write kestrel_scenedata.json to a folder's .kestrel directory.

        Args:
            folder_path: Absolute path to folder (parent or .kestrel itself).
            scenedata: The scenedata dict (version, image_ratings, scenes).

        Returns:
            {'success': bool, 'path': str, 'error': str}
        """
        try:
            _, kestrel_dir, _, err = self._resolve_folder_root_and_kestrel(
                folder_path,
                context='write_kestrel_scenedata',
                require_root_exists=True,
            )
            if err:
                return {'success': False, 'error': err, 'path': ''}

            if not os.path.isdir(kestrel_dir):
                return {'success': False, 'error': f'.kestrel directory not found at: {kestrel_dir}', 'path': ''}

            scenedata_path = os.path.join(kestrel_dir, 'kestrel_scenedata.json')
            if not isinstance(scenedata, dict):
                return {'success': False, 'error': 'scenedata must be a dict', 'path': ''}

            with open(scenedata_path, 'w', encoding='utf-8') as f:
                json.dump(scenedata, f, indent=2)
            return {'success': True, 'path': scenedata_path, 'error': ''}
        except Exception as e:
            error(f'[API] write_kestrel_scenedata({folder_path!r}) error: {e}')
            return {'success': False, 'error': str(e), 'path': ''}

    def open_folder(self, path: str):
        """Open a folder in the system file browser (pywebview desktop mode)."""
        try:
            path, err = self._validate_root_dir(path, context='open_folder', require_exists=True)
            if err:
                return {'success': False, 'error': err}

            import platform as _platform
            p = _platform.system()
            if p == 'Windows':
                subprocess.Popen(['explorer', os.path.normpath(path)])
            elif p == 'Darwin':
                # Sandbox can't Popen /usr/bin/open; route through NSWorkspace.
                if (
                    _mac_sandbox is not None
                    and _mac_sandbox.is_sandboxed()
                    and _mac_sandbox.open_default(path)
                ):
                    pass
                else:
                    subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])
            return {'success': True}
        except Exception as e:
            error(f'[API] open_folder({path!r}) error: {e}')
            return {'success': False, 'error': str(e)}

    def open_in_editor(self, root: str, relative: str, editor: str = 'system'):
        """Open a photo in the configured editor via pywebview (desktop-only path)."""
        try:
            if _launch_editor is None:
                return {'success': False, 'error': 'Editor launcher unavailable'}

            target, resolved_root = self._resolve_editor_target(root, relative)
            if not target:
                return {'success': False, 'error': 'Invalid path'}
            if not self._is_within_root(target, resolved_root):
                return {'success': False, 'error': 'Path escapes allowed root'}
            if not os.path.exists(target):
                return {'success': False, 'error': 'File not found', 'path': target}
            if not self._editor_extension_allowed(target):
                return {
                    'success': False,
                    'error': 'Extension not allowed',
                    'path': target,
                    'allowed': sorted(_ALLOWED_EDITOR_EXTENSIONS),
                }

            editor_name = str(editor or 'system').strip().lower()
            if editor_name not in _ALLOWED_EDITORS:
                editor_name = 'system'

            # Under the App Sandbox, hold security-scoped access to the folder
            # while LaunchServices hands the file to the editor (no-op off
            # sandbox; editor_launch itself routes through NSWorkspace there).
            if _mac_sandbox is not None and _mac_sandbox.is_sandboxed():
                with _mac_sandbox.access_for(resolved_root):
                    _launch_editor(target, editor_name)
            else:
                _launch_editor(target, editor_name)
            return {'success': True, 'path': target}
        except Exception as e:
            error(f'[API] open_in_editor error: {e}')
            return {'success': False, 'error': str(e)}

    def open_url(self, url: str):
        """Open an external URL in the system default browser.

        Gated by ``_is_safe_external_url``: only plain ``http``, ``https``,
        and ``mailto`` schemes are passed through. Everything else (``file``,
        ``javascript``, ``data``, custom URI handlers, UNC paths, control
        characters) is rejected. See FINDING-01.
        """
        try:
            if not _is_safe_external_url(url):
                warn(f'[security] open_url refused unsafe URL: {url!r}')
                return {'success': False, 'error': 'URL scheme not allowed'}
            # In the macOS App Sandbox, webbrowser.open() shells out to
            # /usr/bin/open, which the sandbox blocks — external links would
            # silently do nothing. Route through NSWorkspace (LaunchServices
            # brokers it, no entitlement needed). Fall back to webbrowser only
            # if that path is unavailable.
            if (
                sys.platform == 'darwin'
                and _mac_sandbox is not None
                and _mac_sandbox.is_sandboxed()
            ):
                if _mac_sandbox.open_external_url(url):
                    return {'success': True}
                warn('[API] open_url: NSWorkspace open failed; trying webbrowser')
            webbrowser.open(url)
            return {'success': True}
        except Exception as e:
            error(f'[API] open_url({url!r}) error: {e}')
            return {'success': False, 'error': str(e)}

    # ------------------------------------------------------------------ #
    #  Telemetry / Feedback API                                            #
    # ------------------------------------------------------------------ #

    # Map dialog type values to Auth Worker report_type enum.
    # 'liked' has no direct equivalent; fold into 'general'.
    _FEEDBACK_TYPE_MAP: dict[str, str] = {
        'bug':        'bug',
        'suggestion': 'suggestion',
        'liked':      'general',
        'general':    'general',
        'account':    'account',
    }

    def send_feedback(self, data):
        """Send feedback / bug report (async, failsafe). Called from JS.

        When the user is signed in, routes to the Auth Worker
        (POST /v1/me/feedback) so feedback lands in the unified store.
        Falls back to the analytics-worker path when signed out or if the
        Auth Worker call fails.  Screenshots stay on the analytics path.
        """
        try:
            if _telemetry is None:
                warn('[API] send_feedback: telemetry unavailable')
                return {'success': False, 'error': 'Telemetry module not available'}
            if not isinstance(data, dict):
                return {'success': False, 'error': 'Invalid data'}
            settings = load_persisted_settings()
            machine_id = _telemetry.get_machine_id(settings)
            log_tail = ''
            if data.get('include_logs', False):
                active_folder = str(settings.get('active_analysis_path', '') or '').strip()
                log_tail = _telemetry.get_recent_log_tail(folder=active_folder or None, runtime_log_files=3)

            raw_type = data.get('type', 'general')
            report_type = self._FEEDBACK_TYPE_MAP.get(str(raw_type).lower(), 'general')
            description = data.get('description', '')

            # --- Auth Worker path (signed-in users who opted to send as self) ---
            # Gated on the explicit `send_as_user` opt-in from the dialog: even
            # when signed in, an unchecked box means the report is anonymous and
            # must take the analytics path below.
            # Screenshots are out of scope for the Auth path; they stay on the
            # analytics path only.  Any failure falls through to analytics.
            try:
                send_as_user = bool(data.get('send_as_user', False))
                client, _err = (self._auth_make_client() if send_as_user else (None, None))
                if client is not None:
                    version = _telemetry._read_version() if _telemetry else ''
                    os_info = _telemetry._get_os_info() if _telemetry else ''  # module-level fn
                    client.post_feedback(
                        report_type=report_type,
                        message=description,
                        version=version,
                        os=os_info,
                        contact=str(data.get('contact', '') or '').strip(),
                    )
                    return {'success': True}
            except Exception:
                pass  # fall through to analytics path

            # --- Analytics-worker path (signed-out or Auth call failed) ---
            _telemetry.send_feedback(
                report_type=raw_type,
                description=description,
                contact=data.get('contact', ''),
                screenshot_b64=data.get('screenshot_b64', ''),
                log_tail=log_tail,
                machine_id=machine_id,
                version=_telemetry._read_version(),
            )
            return {'success': True}
        except Exception as e:
            error(f'[API] send_feedback error: {e}')
            return {'success': False, 'error': str(e)}

    def get_settings(self):
        """Return persisted settings, ensuring machine_id and version exist."""
        try:
            settings = load_persisted_settings()
            if _telemetry is not None:
                _telemetry.get_machine_id(settings)
            if _telemetry is not None:
                settings['version'] = _telemetry._read_version()
            save_persisted_settings(settings)
            return {'success': True, 'settings': settings}
        except Exception as e:
            error(f'[API] get_settings error: {e}')
            return {'success': False, 'error': str(e), 'settings': {}}

    def save_settings_data(self, settings_dict):
        """Persist settings from JavaScript (wraps save_persisted_settings)."""
        try:
            if not isinstance(settings_dict, dict):
                return {'success': False, 'error': 'Invalid settings'}
            # Merge into existing persisted settings so stale/minimal frontend
            # payloads cannot drop unrelated keys (for example legal consent flags).
            existing = load_persisted_settings()
            if not isinstance(existing, dict):
                existing = {}
            merged = {**existing, **settings_dict}

            # Keep cumulative impact counters monotonic so stale UI payloads cannot
            # accidentally reset totals to a lower value.
            def _coerce_number(v):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            prev_files = _coerce_number(existing.get('kestrel_impact_total_files'))
            new_files = _coerce_number(merged.get('kestrel_impact_total_files'))
            if prev_files is not None and (new_files is None or new_files < prev_files):
                merged['kestrel_impact_total_files'] = int(prev_files)

            prev_secs = _coerce_number(existing.get('kestrel_impact_total_seconds'))
            new_secs = _coerce_number(merged.get('kestrel_impact_total_seconds'))
            if prev_secs is not None and (new_secs is None or new_secs < prev_secs):
                merged['kestrel_impact_total_seconds'] = prev_secs

            save_persisted_settings(merged)
            return {'success': True}
        except Exception as e:
            error(f'[API] save_settings_data error: {e}')
            return {'success': False, 'error': str(e)}

    # ------------------------------------------------------------------ #
    #  Sample Sets API                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _sample_sets_temp_root() -> str:
        """Return the temp root that holds this session's sample-set mirrors.

        Lives under the OS temp dir so it is writable on every install layout
        — including read-only bundles (MSIX / ``Program Files\\WindowsApps``,
        macOS Gatekeeper App Translocation) — and is the natural home for
        transient data that is regenerated each session.
        """
        return os.path.join(tempfile.gettempdir(), 'ProjectKestrel', 'sample_sets')

    @classmethod
    def cleanup_sample_set_mirrors(cls) -> None:
        """Best-effort removal of the temp sample-set mirror tree.

        Called on clean shutdown. Non-critical: each launch wipes and rebuilds
        the mirror before use (see :meth:`_mirror_sample_set_to_temp`), so a
        crash that skips this leaves only stale data the next session
        overwrites anyway.
        """
        try:
            root = cls._sample_sets_temp_root()
            if os.path.isdir(root):
                shutil.rmtree(root, ignore_errors=True)
        except Exception:
            pass

    @classmethod
    def _mirror_sample_set_to_temp(cls, bundled_path: str, debug_info: list) -> str | None:
        """Mirror a bundled sample set into a writable per-session temp dir.

        Runs on **every** platform, by default. Two reasons:

        * The bundled tree may be read-only (MSIX / ``WindowsApps``, macOS App
          Translocation), where writing back to
          ``<set>/.kestrel/kestrel_database.csv`` raises ``PermissionError``
          (errno 13) or ``OSError`` EROFS (errno 30). A writable mirror sidesteps
          both without special-casing the errno.
        * Even on writable installs, a fresh per-session mirror gives the
          tutorial a predictable clean slate each run and auto-refreshes if the
          bundled set ever changes.

        The prior mirror is wiped first (wipe-on-load) so edits from an earlier
        tutorial session never leak into a new one. Returns the mirror path, or
        ``None`` if mirroring failed — the caller then falls back to the bundled
        path with an in-place restore.
        """
        try:
            set_name = os.path.basename(os.path.normpath(bundled_path))
            mirror_root = cls._sample_sets_temp_root()
            mirror = os.path.join(mirror_root, set_name)
            # Wipe-on-load: always start from a clean copy of the bundle.
            if os.path.isdir(mirror):
                shutil.rmtree(mirror, ignore_errors=True)
            os.makedirs(mirror_root, exist_ok=True)
            # dirs_exist_ok=True keeps the refresh robust even if the wipe above
            # could not fully remove a locked file from a prior session.
            shutil.copytree(bundled_path, mirror, dirs_exist_ok=True)
            debug_info.append(f'[mirror] copied {bundled_path} -> {mirror}')
            # Reset the live DB from the readonly source inside the mirror so the
            # sample state is pristine for this session.
            mirror_readonly = os.path.join(mirror, '.kestrel', 'kestrel_database_readonly.csv')
            mirror_db = os.path.join(mirror, '.kestrel', 'kestrel_database.csv')
            if os.path.isfile(mirror_readonly):
                try:
                    shutil.copy2(mirror_readonly, mirror_db)
                    debug_info.append(f'[mirror] restored sample DB at mirror: {mirror_db}')
                except OSError as e:
                    debug_info.append(f'[mirror] mirror DB restore failed: {e}')
            return mirror
        except Exception as e:
            debug_info.append(f'[mirror] failed to mirror {bundled_path}: {e}')
            return None

    def get_sample_sets_paths(self):
        """Return absolute paths to bundled sample bird-photo sets.

        Works both during development (sample_sets/ next to the repo root)
        and in PyInstaller frozen builds (bundled via _MEIPASS).
        """
        try:
            candidates = []
            debug_info = []
            
            is_frozen = getattr(sys, 'frozen', False)
            debug_info.append(f'[init] sys.frozen={is_frozen}')
            
            if is_frozen:
                debug_info.append('[frozen] Checking frozen build paths...')
                meipass = getattr(sys, '_MEIPASS', None)
                exe_dir = os.path.dirname(sys.executable) if hasattr(sys, 'executable') else None
                debug_info.append(f'[frozen] sys._MEIPASS={meipass}')
                debug_info.append(f'[frozen] sys.executable={sys.executable}')
                debug_info.append(f'[frozen] exe_dir={exe_dir}')
                
                candidates_checked = []
                bases = []
                
                if meipass:
                    bases.append(meipass)
                    bases.append(os.path.join(meipass, '_internal'))
                if exe_dir:
                    bases.append(exe_dir)
                    bases.append(os.path.join(exe_dir, '_internal'))
                    parent_exe = os.path.dirname(exe_dir)
                    if parent_exe and parent_exe != exe_dir:
                        bases.append(parent_exe)
                        bases.append(os.path.join(parent_exe, '_internal'))
                
                sources_internal = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '_internal'))
                bases.append(sources_internal)
                
                debug_info.append(f'[frozen] Will check {len(bases)} base paths')
                for base in bases:
                    if not base or base in candidates_checked:
                        continue
                    candidates_checked.append(base)
                    d = os.path.join(base, 'sample_sets')
                    exists = os.path.isdir(d)
                    debug_info.append(f'[frozen] Checking {d}: exists={exists}')
                    if exists:
                        debug_info.append(f'[frozen] Found sample_sets at: {d}')
                        candidates.append(d)
                        break
                
                if not candidates and exe_dir:
                    debug_info.append(f'[frozen-fallback] Exhaustive search starting from {exe_dir}')
                    try:
                        start_dir = os.path.abspath(os.path.join(exe_dir, '..', '..'))
                        if not os.path.isdir(start_dir):
                            start_dir = exe_dir
                        for root, dirs, files in os.walk(start_dir):
                            depth = root[len(exe_dir):].count(os.sep)
                            if depth > 5:
                                del dirs[:]
                                continue
                            if 'sample_sets' in dirs:
                                found = os.path.join(root, 'sample_sets')
                                debug_info.append(f'[frozen-fallback] Found sample_sets at: {found}')
                                candidates.append(found)
                                break
                    except Exception as e:
                        debug_info.append(f'[frozen-fallback] Exhaustive search failed: {e}')
            else:
                debug_info.append('[dev] Not a frozen build')
            
            cwd_candidate = os.path.join(os.getcwd(), 'sample_sets')
            cwd_exists = os.path.isdir(cwd_candidate)
            debug_info.append(f'[dev-cwd] {cwd_candidate}: exists={cwd_exists}')
            if cwd_exists and cwd_candidate not in candidates:
                candidates.append(cwd_candidate)
            
            file_candidate = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'sample_sets')
            file_candidate = os.path.normpath(file_candidate)
            file_exists = os.path.isdir(file_candidate)
            debug_info.append(f'[dev-file] {file_candidate}: exists={file_exists}')
            if file_exists and file_candidate not in candidates:
                candidates.append(file_candidate)
            
            if not candidates and sys.platform.startswith('win'):
                debug_info.append('[fallback] Starting Program Files search...')
                pf_paths = [
                    os.environ.get('ProgramFiles'),
                    os.environ.get('ProgramFiles(x86)'),
                    'C:\\Program Files',
                    'C:\\Program Files (x86)',
                ]
                for pf_base in pf_paths:
                    if not pf_base or not os.path.isdir(pf_base):
                        continue
                    for dirname in os.listdir(pf_base):
                        if 'kestrel' in dirname.lower():
                            kestrel_dir = os.path.join(pf_base, dirname)
                            direct = os.path.join(kestrel_dir, 'sample_sets')
                            if os.path.isdir(direct):
                                debug_info.append(f'[fallback] Found sample_sets at: {direct}')
                                candidates.append(direct)
                                break
                            internal = os.path.join(kestrel_dir, '_internal', 'sample_sets')
                            if os.path.isdir(internal):
                                debug_info.append(f'[fallback] Found sample_sets at: {internal}')
                                candidates.append(internal)
                                break
                    if candidates:
                        break

            debug_info.append(f'[collect] Found {len(candidates)} candidate roots')
            for idx, cand in enumerate(candidates):
                debug_info.append(f'[collect]   [{idx}] {cand}')

            if not candidates:
                error_msg = 'sample_sets folder not found'
                # Dump the full path-search trace on failure so users can diagnose.
                for line in debug_info:
                    warn(line)
                error(f'[API] get_sample_sets_paths: {error_msg}')
                return {'success': False, 'error': error_msg, 'paths': []}

            sample_root = candidates[0]
            debug_info.append(f'[api] Using root: {sample_root}')
            
            try:
                items = os.listdir(sample_root)
                debug_info.append(f'[api] Root contains {len(items)} items: {items}')
            except Exception as e:
                debug_info.append(f'[api] Failed to list {sample_root}: {e}')
                items = []
            
            paths = []
            for name in sorted(items):
                full = os.path.join(sample_root, name)
                is_dir = os.path.isdir(full)
                kestrel_dir = os.path.join(full, '.kestrel')
                kestrel_exists = os.path.isdir(kestrel_dir)
                debug_info.append(f'[api]   Item "{name}": is_dir={is_dir}, has .kestrel={kestrel_exists}')

                if is_dir and kestrel_exists:
                    readonly_src = os.path.join(kestrel_dir, 'kestrel_database_readonly.csv')
                    readonly_exists = os.path.isfile(readonly_src)
                    debug_info.append(f'[api]     readonly_src: {readonly_src} exists={readonly_exists}')

                    # Default path (all platforms): mirror the bundled set into a
                    # writable per-session temp dir and hand the UI that copy. The
                    # mirror resets its own live DB from the readonly source, so it
                    # is a clean-slate sample set for this run.
                    mirror = self._mirror_sample_set_to_temp(full, debug_info)
                    if mirror is not None:
                        paths.append(mirror)
                        debug_info.append(f'[api]     Added mirror path: {mirror}')
                    else:
                        # Fallback: temp mirror unavailable. Use the bundled set
                        # directly and reset its DB in place, as before. Any
                        # residual OSError (e.g. a read-only bundle) is left to
                        # surface in the trace for future triage rather than
                        # silently swallowed.
                        db_dst = os.path.join(kestrel_dir, 'kestrel_database.csv')
                        if readonly_exists:
                            try:
                                shutil.copy2(readonly_src, db_dst)
                                debug_info.append(f'[api]     Restored sample DB in place: {db_dst}')
                            except OSError as e:
                                debug_info.append(
                                    f'[api]     In-place DB restore failed (mirror unavailable): {e}'
                                )
                        else:
                            debug_info.append(f'[api]     No readonly DB found at {readonly_src}')
                        paths.append(full)
                        debug_info.append(f'[api]     Added bundled path: {full}')
            
            # Success path: one-line summary at INFO. Full trace only at DEBUG.
            for line in debug_info:
                debug(line)
            info(f'[API] get_sample_sets_paths: {len(paths)} sets from {sample_root}')
            return {'success': True, 'paths': paths}
        except Exception as e:
            import traceback
            error(f'[API] get_sample_sets_paths error: {e}')
            error(f'[API] Traceback: {traceback.format_exc()}')
            return {'success': False, 'error': str(e), 'paths': []}

    # ------------------------------------------------------------------ #
    #  Analysis Queue API (called from JavaScript in pywebview mode)       #
    # ------------------------------------------------------------------ #

    def start_analysis_queue(self, paths, use_gpu=True, wildlife_enabled=True, retry_errored=False, species_detection_enabled=True, per_item_options=None):
        """Enqueue folders for analysis. ``paths`` may be a JSON string or list.

        ``retry_errored`` (bool): when True, drop rows previously marked
        ``species == "Error"`` from each folder's CSV before reprocessing, so
        those images get re-analyzed instead of being skipped as already-done.

        ``species_detection_enabled`` (bool): when False, the bird species
        classifier is skipped and species/family fields are recorded as
        ``Unknown``. Detection, quality scoring, and culling still run.

        ``per_item_options`` (dict | str | None): Phase 3 per-path option map.
        Keys are paths (raw or post-validation; we normalize on lookup).
        Values are dicts with optional bool flags:
          - ``delete_kestrel_on_start``: worker wipes the folder's .kestrel
            JUST BEFORE that folder's analysis starts (NOT at queue-build
            time). Used by the dialog when the user explicitly unlocks
            re-analysis of fully-analyzed or outdated folders.
          - ``skip_if_already_done``: worker re-inspects the folder when its
            turn comes up and silently marks it ``done`` (no pipeline call,
            no deletion) if it's still fully analyzed with no errors. Used
            so a user's accidental check on an already-done folder is a
            no-op rather than destructive.
        """
        try:
            if isinstance(paths, str):
                paths = json.loads(paths)
            if not isinstance(paths, list):
                return {'success': False, 'error': 'paths must be a list'}

            # Coerce per_item_options if it arrived as a JSON string.
            if isinstance(per_item_options, str):
                try:
                    per_item_options = json.loads(per_item_options)
                except Exception:
                    per_item_options = None
            if per_item_options is not None and not isinstance(per_item_options, dict):
                per_item_options = None

            validated_paths = []
            invalid_paths = []
            # Map raw->validated so per_item_options keyed by the raw frontend
            # path still resolves to the canonical realpath the worker uses.
            raw_to_validated = {}
            for raw in paths:
                if not raw:
                    continue
                root_real, err = self._validate_root_dir(raw, context='start_analysis_queue', require_exists=True)
                if err:
                    invalid_paths.append(str(raw))
                    continue
                if root_real not in validated_paths:
                    validated_paths.append(root_real)
                raw_to_validated[str(raw)] = root_real

            if invalid_paths:
                self._log_security_reject(
                    'start_analysis_queue',
                    'One or more queue paths are invalid',
                    invalid_count=len(invalid_paths),
                )
                return {
                    'success': False,
                    'error': 'Invalid folder path in queue request',
                    'invalid_paths': invalid_paths,
                }
            if not validated_paths:
                return {'success': False, 'error': 'No valid paths provided'}

            # Re-key per_item_options against the validated paths so the
            # queue manager can look up options by the same path it stores.
            validated_per_item_options = None
            if per_item_options:
                validated_per_item_options = {}
                for raw_key, opts in per_item_options.items():
                    if not isinstance(opts, dict):
                        continue
                    real = raw_to_validated.get(str(raw_key))
                    if real is None:
                        # Maybe the frontend already sent us the realpath.
                        if str(raw_key) in validated_paths:
                            real = str(raw_key)
                    if real is not None:
                        validated_per_item_options[real] = {
                            'delete_kestrel_on_start': bool(opts.get('delete_kestrel_on_start')),
                            'skip_if_already_done': bool(opts.get('skip_if_already_done')),
                        }

            sett = load_persisted_settings()
            detection_threshold = float(sett.get('detection_threshold', 0.25))
            detection_threshold = max(0.1, min(0.99, detection_threshold))
            scene_time_threshold = float(sett.get('scene_time_threshold', 1.0))
            scene_time_threshold = max(0.0, scene_time_threshold)
            detector_name = 'mdv5a'
            mode_raw = str(sett.get('wildlife_model_mode', '') or '').strip().lower()
            if mode_raw == 'accurate':
                detector_name = 'mdv5a'
            elif mode_raw == 'fast':
                detector_name = 'mdv1000-cedar'
            else:
                # Belt-and-braces: settings_utils._migrate_legacy_detector_name
                # has already remapped 'mdv6-e' on load, but if a raw stored value
                # still gets here we accept it and migrate again.
                from settings_utils import _migrate_legacy_detector_name
                legacy_detector = _migrate_legacy_detector_name(
                    str(sett.get('detector_name', '') or '').strip().lower()
                )
                if legacy_detector in {'mdv5a', 'mdv1000-cedar'}:
                    detector_name = legacy_detector
            mask_threshold = float(sett.get('mask_threshold', 0.5))
            mask_threshold = max(0.5, min(0.95, mask_threshold))
            try:
                max_bird_crops = int(float(sett.get('max_bird_crops', 10)))
            except (TypeError, ValueError):
                max_bird_crops = 10
            max_bird_crops = max(1, min(20, max_bird_crops))
            try:
                parallel_prefetch = int(float(sett.get('parallel_prefetch', 3)))
            except (TypeError, ValueError):
                parallel_prefetch = 3
            parallel_prefetch = max(1, min(5, parallel_prefetch))
            return _queue_manager.enqueue(validated_paths, use_gpu=bool(use_gpu),
                                          wildlife_enabled=bool(wildlife_enabled),
                                          species_detection_enabled=bool(species_detection_enabled),
                                          detection_threshold=detection_threshold,
                                          scene_time_threshold=scene_time_threshold,
                                          mask_threshold=mask_threshold,
                                          max_bird_crops=max_bird_crops,
                                          parallel_prefetch=parallel_prefetch,
                                          detector_name=detector_name,
                                          retry_errored=bool(retry_errored),
                                          per_item_options=validated_per_item_options)
        except Exception as e:
            error(f'[API] start_analysis_queue error: {e}')
            return {'success': False, 'error': str(e)}

    def pause_analysis_queue(self):
        """Pause the running analysis queue."""
        return _queue_manager.pause()

    def resume_analysis_queue(self):
        """Resume a paused analysis queue."""
        return _queue_manager.resume()

    def cancel_analysis_queue(self):
        """Cancel the analysis queue (marks pending items as cancelled)."""
        return _queue_manager.cancel()

    def get_queue_status(self):
        """Return the current state of the analysis queue."""
        return _queue_manager.get_status()

    def clear_queue_done(self):
        """Remove finished/errored/cancelled items from the queue list."""
        return _queue_manager.clear_done()

    def remove_queue_item(self, path: str):
        """Remove a single pending item from the queue by path."""
        try:
            return _queue_manager.remove_pending_item(str(path))
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def reorder_queue(self, ordered_paths):
        """Reorder pending queue items. ordered_paths is a JSON string or list of paths."""
        try:
            if isinstance(ordered_paths, str):
                ordered_paths = json.loads(ordered_paths)
            if not isinstance(ordered_paths, list):
                return {'success': False, 'error': 'ordered_paths must be a list'}
            return _queue_manager.reorder_pending(ordered_paths)
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def is_analysis_running(self):
        """Return True if the analysis queue is actively running."""
        return {'running': _queue_manager.is_running}

    def get_recovery_status(self):
        """Return persisted queue-recovery and unclean-shutdown state.

        ``exit_reason`` is the classified outcome of the previous session
        (``'clean' | 'os_shutdown' | 'crash' | 'unknown'``). The frontend
        uses it to pick dialog wording — alarming for ``'crash'``, soft
        for ``'unknown'``, no dialog at all for the other two. See
        ``visualizer._classify_prior_session``.
        """
        try:
            settings = load_persisted_settings()
            queue_state = _queue_manager.get_persisted_recovery_state()
            unclean_utc = str(settings.get('last_unclean_shutdown_utc', '') or '').strip()
            exit_reason = str(settings.get('last_exit_reason', '') or '').strip().lower()
            if exit_reason not in ('clean', 'os_shutdown', 'crash', 'unknown'):
                exit_reason = 'unknown' if unclean_utc else 'clean'
            return {
                'success': True,
                'unclean_shutdown': bool(unclean_utc),
                'unclean_shutdown_utc': unclean_utc,
                'exit_reason': exit_reason,
                'queue_recovery': queue_state,
            }
        except Exception as e:
            return {'success': False, 'error': str(e)}

    # Phase 3: restore_analysis_queue removed — feature replaced by the
    # analyze dialog's analyze_recents chip row.

    def clear_recovery_state(self, clear_queue_state: bool = True):
        """Clear persisted unclean-shutdown flag and optionally queue recovery snapshot."""
        try:
            settings = load_persisted_settings()
            settings.pop('last_unclean_shutdown_utc', None)
            if bool(clear_queue_state):
                settings.pop('queue_recovery_state', None)
            save_persisted_settings(settings)
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def send_recovery_crash_report(self):
        """Send a crash report generated from persisted recovery state and recent logs."""
        try:
            if _telemetry is None:
                return {'success': False, 'error': 'Telemetry module not available'}
            settings = load_persisted_settings()
            machine_id = _telemetry.get_machine_id(settings)
            active_folder = str(settings.get('active_analysis_path', '') or '').strip()
            log_tail = _telemetry.get_recent_log_tail(folder=active_folder or None, runtime_log_files=3)
            exit_reason = str(settings.get('last_exit_reason', '') or '').strip().lower() or 'unknown'
            _telemetry.send_crash_report(
                exc=None,
                tb_str='Recovered unclean shutdown report requested by user.',
                log_tail=log_tail,
                session_analytics={
                    'recovery_report': True,
                    'active_analysis_path': active_folder,
                    'exit_reason': exit_reason,
                },
                machine_id=machine_id,
                version=_telemetry._read_version(),
                exit_reason=exit_reason,
            )
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    # ------------------------------------------------------------------ #
    #  Culling Assistant API                                               #
    # ------------------------------------------------------------------ #

    _main_window = None
    _culling_window = None
    _server_port = None
    # OAuth flow state (per-instance, defaults safe for fresh sessions).
    _oauth_lock = None              # lazy-init threading.Lock in _get_oauth_lock
    _oauth_in_flight = False
    _oauth_status = "idle"
    _oauth_cancel_event = None      # threading.Event for the active flow
    _oauth_thread = None            # the active oauth-flow worker thread

    def open_culling_window(self, root_path: str):
        """Open a new pywebview window for the Culling Assistant."""
        try:
            if not WEBVIEW_IMPORT_SUCCESS:
                return {'success': False, 'error': 'pywebview not available'}

            root_real, err = self._validate_root_dir(root_path, context='open_culling_window', require_exists=True)
            if err:
                return {'success': False, 'error': err}

            import webview as _wv
            folder_name = os.path.basename(root_real) if root_real else 'Unknown'
            port = self._server_port or 8765
            from urllib.parse import quote
            culling_url = f'http://{HOST}:{port}/culling.html?root={quote(root_real, safe="")}'

            win = _wv.create_window(
                f'Culling Assistant \u2014 {folder_name}',
                culling_url,
                js_api=self,
                width=1400,
                height=900,
            )
            self._culling_window = win
            return {'success': True}
        except Exception as e:
            error(f'[API] open_culling_window error: {e}')
            import traceback
            error(f'[culling] Traceback: {traceback.format_exc()}')
            return {'success': False, 'error': str(e)}

    def _get_oauth_lock(self):
        """Lazy-init the OAuth flow lock; mirrors the _share_jobs_lock pattern."""
        if self._oauth_lock is None:
            import threading as _t
            self._oauth_lock = _t.Lock()
        return self._oauth_lock

    def _load_token_bundle(self) -> dict | None:
        """Read the OAuth bundle from keychain. Returns None if missing or stale-schema."""
        data = _keyring_load()
        if not data or not isinstance(data, dict):
            return None
        if not data.get("access_token"):
            return None
        return data

    def _clear_keychain_auth(self) -> None:
        """Best-effort: remove the keyring slot and the plaintext fallback file."""
        try:
            import keyring as _kr
            try:
                _kr.delete_password(_KEYRING_SERVICE, _KEYRING_KEY)
            except Exception:
                pass
        except ImportError:
            pass
        try:
            os.remove(_get_auth_fallback_path())
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def _refresh_if_needed(self, bundle: dict) -> dict | None:
        """Refresh the access token if within REFRESH_BUFFER_SEC of expiry.

        Returns the (possibly updated) bundle. Returns None only if refresh
        definitively failed with ``invalid_grant`` and the keychain was
        cleared. On transient failures (network), returns the unchanged
        bundle so callers can still attempt to use the existing token until
        it truly expires.
        """
        if _oauth is None:
            return bundle  # OAuth module unavailable — nothing we can do
        # Native Apple bundles carry a Clerk session JWT, refreshed by re-minting
        # from the durable __client credential — not the OAuth refresh grant.
        if bundle.get("kind") == getattr(_oauth, "CLERK_SESSION_BUNDLE_KIND", "clerk_session"):
            return self._refresh_clerk_session(bundle)
        try:
            expires_at = float(bundle.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0.0
        ttl = expires_at - time.time()
        if ttl >= _oauth.REFRESH_BUFFER_SEC:
            return bundle
        refresh_token = bundle.get("refresh_token") or ""
        if not refresh_token:
            return bundle  # no refresh available; let upstream stale-check handle it

        lock = self._get_oauth_lock()
        if not lock.acquire(timeout=10.0):
            return bundle
        try:
            # Another caller may have refreshed while we waited for the lock.
            current = _keyring_load() or {}
            if (current.get("access_token")
                    and current.get("access_token") != bundle.get("access_token")):
                return current
            try:
                ttl_now = float(current.get("expires_at") or 0) - time.time()
            except (TypeError, ValueError):
                ttl_now = 0.0
            if ttl_now >= _oauth.REFRESH_BUFFER_SEC:
                return current

            resp = _oauth.refresh_access_token(refresh_token)
            if resp.get("error"):
                # invalid_grant => refresh token revoked / aged out / rotated past.
                if resp.get("error") == "invalid_grant":
                    self._clear_keychain_auth()
                    self._invalidate_account_caches()
                    return None
                # Network / 5xx — keep old bundle, downstream will surface stale.
                return current or bundle

            new_bundle = _oauth.build_bundle(resp)
            # Clerk may omit refresh_token if it isn't rotating; preserve ours.
            if not new_bundle.get("refresh_token"):
                new_bundle["refresh_token"] = refresh_token
            if not new_bundle.get("access_token"):
                return current or bundle
            _keyring_save(new_bundle)
            # Caches were keyed on the now-rotated access token.
            self._invalidate_account_caches()
            return new_bundle
        finally:
            lock.release()

    def _refresh_clerk_session(self, bundle: dict) -> dict | None:
        """Re-mint the Clerk session JWT for a ``clerk_session`` (native Apple)
        bundle when it's within the re-mint buffer of expiry.

        Session JWTs live ~60s; we mint a fresh one from the durable ``__client``
        credential on demand. Returns the (possibly updated) bundle, or the
        unchanged bundle on a transient failure — ``get_auth_token``'s staleness
        floor surfaces signed-out once the token has actually expired. Mirrors
        the locking/double-check pattern of ``_refresh_if_needed``.
        """
        buffer = getattr(_oauth, "CLERK_SESSION_REMINT_BUFFER_SEC", 15)
        try:
            expires_at = float(bundle.get("expires_at") or 0)
        except (TypeError, ValueError):
            expires_at = 0.0
        if expires_at - time.time() >= buffer:
            return bundle
        client = bundle.get("clerk_client") or ""
        sid = bundle.get("clerk_session_id") or ""
        if not client or not sid:
            return bundle

        lock = self._get_oauth_lock()
        if not lock.acquire(timeout=10.0):
            return bundle
        try:
            # Another caller may have re-minted while we waited for the lock.
            current = _keyring_load() or {}
            if (current.get("access_token")
                    and current.get("access_token") != bundle.get("access_token")):
                try:
                    if float(current.get("expires_at") or 0) - time.time() >= buffer:
                        return current
                except (TypeError, ValueError):
                    pass
            token = _oauth.remint_session_token(client, sid)
            if not token:
                # Transient (network) or the session ended. Keep the old bundle;
                # the staleness floor will surface signed-out once it expires.
                return current or bundle
            new_bundle = _oauth.build_session_bundle(token, client, sid)
            _keyring_save(new_bundle)
            self._invalidate_account_caches()
            return new_bundle
        finally:
            lock.release()

    def get_auth_token(self):
        """Return current OAuth access token; trigger lazy refresh near expiry.

        Return shape preserved for backward compatibility with JS callers:
        ``{success, token, expiry}``. ``token`` is None when the user is signed
        out or the token is past its post-refresh staleness floor.
        """
        try:
            bundle = self._load_token_bundle()
            if not bundle:
                return {"success": True, "token": None}
            bundle = self._refresh_if_needed(bundle)
            if bundle is None:
                return {"success": True, "token": None}
            access_token = bundle.get("access_token") or ""
            if not access_token:
                return {"success": True, "token": None}
            ttl = _auth_jwt_seconds_until_exp(str(access_token))
            # Clerk session JWTs (native Apple) are ~60s and re-minted on demand,
            # so a fresh one legitimately has < 60s left — use a small floor for
            # that kind. OAuth access tokens keep the original 60s staleness floor.
            floor = 5 if bundle.get("kind") == "clerk_session" else 60
            if ttl is None or ttl < floor:
                # Token is past its useful life and refresh didn't (or couldn't)
                # extend it — surface signed-out so the UI prompts re-auth.
                return {"success": True, "token": None}
            exp_out = _auth_jwt_exp_unverified(str(access_token))
            if exp_out is None:
                try:
                    exp_out = float(bundle.get("expires_at") or 0)
                except (TypeError, ValueError):
                    exp_out = 0.0
            return {"success": True, "token": access_token, "expiry": exp_out}
        except Exception as e:
            print(f"[API] get_auth_token() -> Error: {e}", flush=True)
            return {"success": True, "token": None}

    def get_perch_api_base(self) -> str:
        """Base URL of the Perch API Worker (no trailing slash)."""
        return os.environ.get(
            "PERCH_API_BASE", "https://perchapi.projectkestrel.org"
        ).rstrip("/")

    # ─── Perch upload — preflight, async share, progress, cancel ─────────
    # Per-instance share-job state lives on `self._share_jobs`, initialized in
    # __init__. Access is guarded by a lazy-allocated lock since pywebview
    # method handlers run on a thread distinct from the upload worker pool.

    def _ensure_share_lock(self) -> "threading.Lock":
        import threading as _t
        if self._share_jobs_lock is None:
            self._share_jobs_lock = _t.Lock()
        return self._share_jobs_lock

    def _check_auth_token(self) -> tuple[str | None, str | None, dict | None]:
        """Return (token, dev_user, error_dict-if-not-signed-in-or-stale).

        On a usable token: error_dict is None.
        On no token: error_dict has `needSignIn: True`.

        Triggers a lazy OAuth refresh when the access token is within the
        300s pre-expiry buffer, so a long-running call doesn't 401 if the
        token rolled over mid-flight.
        """
        dev_user = os.environ.get("PERCH_DEV_USER_ID")
        bundle = self._load_token_bundle()
        token = None
        if bundle is not None:
            bundle = self._refresh_if_needed(bundle)
            if bundle is not None:
                token = bundle.get("access_token") or None
        if not token and not dev_user:
            return None, None, {"success": False, "error": "not_signed_in", "needSignIn": True}
        if token and not dev_user:
            ttl = _auth_jwt_seconds_until_exp(str(token))
            if ttl is None or ttl < 90:
                return None, None, {
                    "success": False,
                    "error": "auth_token_expired",
                    "needSignIn": True,
                }
        return (str(token) if token else None), dev_user, None

    def preflight_perch_upload(self, root_path: str, skip_rejected: bool = True) -> dict:
        """Compute scene/photo/byte counts for a folder before uploading.

        Local-only (no auth needed). Returns aggregate totals plus a per-scene
        breakdown so the JS layer can render a checkbox-per-scene selector.
        Also reports `signedIn` so the dialog can fork between the explainer
        body and the upload-preview body.

        ``skip_rejected``: when True (default), CSV rows with ``culled``
        truthy are dropped from preflight totals. The number dropped is
        returned as ``rejectedSkipped`` so the dialog can show the count.
        """
        try:
            from perch_uploader import PerchKestrelUploader
        except ImportError:  # pragma: no cover
            try:
                from analyzer.perch_uploader import PerchKestrelUploader
            except ImportError as e:
                return {"ok": False, "error": f"uploader import failed: {e}"}

        root_real, err = self._validate_root_dir(
            root_path, context="preflight_perch_upload", require_exists=True
        )
        if err:
            return {"ok": False, "error": err}

        # Token check is non-fatal here — preflight runs even when signed out.
        dev_user = os.environ.get("PERCH_DEV_USER_ID")
        bundle = self._load_token_bundle()
        if bundle is not None:
            bundle = self._refresh_if_needed(bundle)
        token = (bundle or {}).get("access_token") if bundle else None
        signed_in = bool(dev_user)
        token_stale = False
        if not signed_in and token:
            ttl = _auth_jwt_seconds_until_exp(str(token))
            if ttl is None or ttl < 90:
                token_stale = True
            else:
                signed_in = True

        try:
            uploader = PerchKestrelUploader(
                self.get_perch_api_base(),
                str(token) if token else None,
                dev_user=dev_user,
            )
        except ValueError:
            # No usable auth at all — preflight still works (no network call),
            # so we pass a placeholder dev_user just to satisfy the constructor.
            # This placeholder never reaches the worker because preflight() is
            # local-only.
            try:
                uploader = PerchKestrelUploader(
                    self.get_perch_api_base(), None, dev_user="preflight-no-auth"
                )
            except Exception as e:
                return {"ok": False, "error": str(e)}
        try:
            pre = uploader.preflight(root_real, skip_rejected=bool(skip_rejected))
        except Exception as e:
            log(f"preflight_perch_upload: {e}")
            return {"ok": False, "error": str(e)}

        return {
            "ok": True,
            "signedIn": signed_in,
            "tokenStale": token_stale,
            "sceneCount": pre.scene_count,
            "imageCount": pre.image_count,
            "exportCount": pre.export_count,
            "cropCount": pre.crop_count,
            "totalBytes": pre.total_bytes,
            "fileCount": pre.file_count,
            "rejectedSkipped": pre.rejected_skipped,
            "skipRejectedUsed": bool(skip_rejected),
            "scenes": [
                {
                    "sceneId": s.scene_id,
                    "title": s.title,
                    "captureTimeMs": s.capture_time_ms,
                    "imageCount": s.image_count,
                    "exportCount": s.export_count,
                    "cropCount": s.crop_count,
                    "totalBytes": s.total_bytes,
                    "topQuality": s.top_quality,
                    "thumbnailPath": s.thumbnail_rel,
                    "reviewed": bool(s.reviewed),
                    "rejectedSkipped": int(s.rejected_skipped),
                    "species": list(s.species),
                    "families": list(s.families),
                }
                for s in pre.scenes
            ],
        }

    def get_perch_account(self) -> dict:
        """GET /v1/me — caller's Clerk profile. 5-min in-process cache.

        Only successful responses are cached — failures are NOT cached, so a
        recoverable error (transient network blip, token-just-refreshed) is
        retried on the next call instead of getting stuck for 5 minutes.
        """
        now = time.time()
        if (
            self._perch_account_cache is not None
            and self._perch_account_cache.get("success")
            and (now - self._perch_account_cache_at) < 300
        ):
            return self._perch_account_cache
        token, dev_user, err = self._check_auth_token()
        if err:
            return err
        try:
            import requests as _req
            headers: dict = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            if dev_user:
                headers["x-dev-user-id"] = str(dev_user)
            r = _req.get(
                f"{self.get_perch_api_base()}/v1/me",
                headers=headers,
                timeout=15,
            )
            if not r.ok:
                return {"success": False, "error": f"HTTP {r.status_code}"}
            body = r.json()
            out = {"success": True, "account": body}
            self._perch_account_cache = out
            self._perch_account_cache_at = now
            return out
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_perch_usage(self) -> dict:
        """GET /v1/me/usage — totalImages, totalAssets, totalBytes. 5-min cache."""
        now = time.time()
        if (
            self._perch_usage_cache is not None
            and (now - self._perch_usage_cache_at) < 300
        ):
            return self._perch_usage_cache
        token, dev_user, err = self._check_auth_token()
        if err:
            return err
        try:
            import requests as _req
            headers: dict = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            if dev_user:
                headers["x-dev-user-id"] = str(dev_user)
            r = _req.get(
                f"{self.get_perch_api_base()}/v1/me/usage",
                headers=headers,
                timeout=15,
            )
            if not r.ok:
                return {"success": False, "error": f"HTTP {r.status_code}"}
            body = r.json()
            out = {"success": True, "usage": body}
            self._perch_usage_cache = out
            self._perch_usage_cache_at = now
            return out
        except Exception as e:
            return {"success": False, "error": str(e)}

    def get_perch_list(self, limit: int = 200) -> dict:
        """GET /v1/me/perches — lightweight perch list for the Account panel."""
        token, dev_user, err = self._check_auth_token()
        if err:
            return err
        lim = max(1, min(int(limit or 200), 200))
        try:
            import requests as _req
            headers: dict = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            if dev_user:
                headers["x-dev-user-id"] = str(dev_user)
            r = _req.get(
                f"{self.get_perch_api_base()}/v1/me/perches",
                headers=headers,
                params={"limit": lim},
                timeout=15,
            )
            if not r.ok:
                return {"success": False, "error": f"HTTP {r.status_code}"}
            body = r.json()
            perches = body.get("perches") if isinstance(body, dict) else None
            if not isinstance(perches, list):
                perches = body if isinstance(body, list) else []
            return {"success": True, "perches": perches}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ─── Cloud Compute — submit / poll / cancel ───────────────────────────
    # Reuses the Perch JWT (same Clerk identity). The cloud-compute Worker
    # validates the JWT and calls Perch internally for entitlement + usage
    # accrual; the desktop app does not need to know about that handshake.

    @staticmethod
    def _sanitize_cloud_error_message(msg: str) -> str:
        """Strip credentials from Worker error bodies before surfacing to JS.

        Worker error responses are forwarded verbatim into the analyzer UI; a
        misbehaving upstream (or a future logging-the-request-body bug) could
        echo back the user's Bearer token / JWT. Redact those patterns and
        cap the payload so a flood of HTML / stack trace can't crowd out the
        actionable error.
        """
        if not msg:
            return ""
        text = str(msg)
        text = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer [REDACTED]", text)
        text = re.sub(
            r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b",
            "[REDACTED_JWT]",
            text,
        )
        if len(text) > 300:
            text = text[:300] + "…"
        return text

    @staticmethod
    def _cc_submit_error_response(exc: "object") -> dict | None:
        """Map known Worker submit failures to desktop-friendly error dicts.

        Returns a full ``{ok: False, ...}`` payload for typed cases, or
        ``None`` so the caller can fall through to the generic sanitizer.
        """
        try:
            from cloud_compute_client import CloudComputeError as _CCE
        except ImportError:
            return None
        if not isinstance(exc, _CCE):
            return None
        if exc.status != 503:
            return None
        try:
            parsed = json.loads(exc.message)
        except (ValueError, TypeError):
            parsed = None
        if not isinstance(parsed, dict) or parsed.get("error") != "cloud_busy":
            return None
        return {
            "ok": False,
            "error": _CC_CLOUD_BUSY_USER_MESSAGE,
            "status": 503,
            "errorCode": "cloud_busy",
        }

    def _ensure_cc_lock(self) -> "threading.Lock":
        import threading as _t
        if self._cc_jobs_lock is None:
            self._cc_jobs_lock = _t.Lock()
        return self._cc_jobs_lock

    def cloud_compute_get_api_base(self) -> str:
        """Settings-aware cloud-compute Worker base URL (no trailing slash)."""
        try:
            from cloud_compute_client import default_api_base
        except ImportError:
            try:
                from analyzer.cloud_compute_client import default_api_base
            except ImportError:
                return "https://cloudcompute.projectkestrel.org"

        # Settings override > env override > default. settings_utils stores the
        # value as a string; empty string = unset.
        try:
            settings = self.get_settings()
            if isinstance(settings, dict):
                cfg = settings.get("settings") if "settings" in settings else settings
                if isinstance(cfg, dict):
                    s_val = str(cfg.get("cloud_compute_api_base") or "").strip()
                    if s_val:
                        return s_val.rstrip("/")
        except Exception:
            pass
        return default_api_base()

    def _cc_import(self):
        """Lazy import of cloud_compute_client. Returns the module or raises."""
        try:
            import cloud_compute_client as ccc
            return ccc
        except ImportError:
            from analyzer import cloud_compute_client as ccc  # type: ignore[no-redef]
            return ccc

    def _cc_jobs_store(self):
        """Lazy import of cloud_jobs_store."""
        try:
            import cloud_jobs_store as cjs
            return cjs
        except ImportError:
            from analyzer import cloud_jobs_store as cjs  # type: ignore[no-redef]
            return cjs

    def _cc_owner_id(self) -> str:
        """Stable id of the currently signed-in account (JWT `sub`), or "" when
        signed out / undecodable. Used to tag cloud jobs at submit and to filter
        job history so switching accounts never surfaces another user's jobs.
        Read straight off the stored access token — no network call — so it
        works offline and regardless of folder availability."""
        try:
            bundle = self._load_token_bundle()
            token = (bundle or {}).get("access_token") if bundle else None
            if not token:
                return ""
            return _auth_jwt_sub_unverified(str(token)) or ""
        except Exception:
            return ""

    def _cc_fresh_token(self) -> str | None:
        """Token provider handed to CloudComputeClient. Called by the client on
        a 401 to obtain a FRESH JWT and retry — so a session that expires
        mid-job (e.g. after laptop sleep) self-heals instead of failing the
        job. _check_auth_token() triggers a lazy OAuth refresh when the access
        token is near/at expiry, which is exactly the post-sleep case. Returns
        None when re-auth genuinely can't be obtained (signed out / refresh
        token revoked); the client then raises CloudComputeAuthError."""
        try:
            token, _dev_user, _err = self._check_auth_token()
        except Exception:
            return None
        return token

    def _cc_make_client(self):
        """Build an authenticated CloudComputeClient. Returns (client, error_dict)."""
        token, dev_user, token_err = self._check_auth_token()
        if token_err:
            return None, token_err
        try:
            ccc = self._cc_import()
        except ImportError as e:
            return None, {"ok": False, "error": f"cloud_compute_client import failed: {e}"}
        try:
            client = ccc.CloudComputeClient(
                self.cloud_compute_get_api_base(),
                token,
                dev_user=dev_user,
                # On a 401, let the client refresh + retry instead of failing
                # the job. See _cc_fresh_token.
                token_provider=self._cc_fresh_token,
            )
        except ValueError as e:
            return None, {"ok": False, "error": str(e)}
        return client, None

    def auth_get_api_base(self) -> str:
        """Settings-aware Auth Worker base URL (no trailing slash). Mirrors
        cloud_compute_get_api_base — the JWT bridge talks to a different
        domain from the CC Worker, so we resolve it independently."""
        try:
            from auth_client import default_auth_api_base
        except ImportError:
            try:
                from analyzer.auth_client import default_auth_api_base
            except ImportError:
                return "https://auth.projectkestrel.org"
        try:
            settings = self.get_settings()
            if isinstance(settings, dict):
                cfg = settings.get("settings") if "settings" in settings else settings
                if isinstance(cfg, dict):
                    s_val = str(cfg.get("auth_api_base") or "").strip()
                    if s_val:
                        return s_val.rstrip("/")
        except Exception:
            pass
        return default_auth_api_base()

    def _auth_import(self):
        """Lazy import of auth_client. Returns the module or raises."""
        try:
            import auth_client as ac
            return ac
        except ImportError:
            from analyzer import auth_client as ac  # type: ignore[no-redef]
            return ac

    def _auth_make_client(self):
        """Build an authenticated AuthClient. Returns (client, error_dict)."""
        token, dev_user, token_err = self._check_auth_token()
        if token_err:
            return None, token_err
        try:
            ac = self._auth_import()
        except ImportError as e:
            return None, {"ok": False, "error": f"auth_client import failed: {e}"}
        try:
            client = ac.AuthClient(
                self.auth_get_api_base(),
                token,
                dev_user=dev_user,
            )
        except ValueError as e:
            return None, {"ok": False, "error": str(e)}
        return client, None

    # ── Notifications (H6) — proxy the central Auth-hosted store ────────────
    # The bell UI lives in the desktop app but the store lives on the Auth
    # Worker; we proxy through Python so the Clerk JWT (held in the OS keychain)
    # never has to be handed to the webview. All best-effort: a failure returns
    # {success: False, error} and the bell degrades quietly.
    def get_notifications(self) -> dict:
        """GET /v1/me/notifications → {success, notifications, unreadCount}."""
        client, err = self._auth_make_client()
        if err:
            return {"success": False, "error": err.get("error", "not signed in")}
        try:
            data = client.get_notifications(30)
            return {
                "success": True,
                "notifications": data.get("notifications", []),
                "unreadCount": data.get("unreadCount", 0),
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def mark_notification_read(self, notif_id: str) -> dict:
        """POST /v1/me/notifications/{id}/read."""
        client, err = self._auth_make_client()
        if err:
            return {"success": False, "error": err.get("error", "not signed in")}
        try:
            client.mark_notification_read(notif_id)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def mark_all_notifications_read(self) -> dict:
        """POST /v1/me/notifications/read-all."""
        client, err = self._auth_make_client()
        if err:
            return {"success": False, "error": err.get("error", "not signed in")}
        try:
            client.mark_all_notifications_read()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def hide_notification(self, notif_id: str) -> dict:
        """DELETE /v1/me/notifications/{id} — soft hide."""
        client, err = self._auth_make_client()
        if err:
            return {"success": False, "error": err.get("error", "not signed in")}
        try:
            client.hide_notification(notif_id)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _cc_load_analyzed_filenames(self, folder) -> tuple[set, set]:
        """Read analyzed + errored filenames from the folder's kestrel database,
        running schema migration as needed.

        Wraps ``kestrel_analyzer.database.load_database`` so legacy CSVs (with
        ``rating`` / ``scene_name`` columns and no ``kestrel_scenedata.json``)
        get the same OLD_..._csv backup + scenedata.json extraction as on a
        local enqueue. Without this, the cloud path would read a legacy CSV
        directly and the pack-merge would silently lose the user's pre-migration
        ratings / scene names.

        Returns ``(analyzed_filenames, errored_filenames)``. Both empty when
        no database exists yet.
        """
        from pathlib import Path as _Path
        folder = _Path(folder)
        kestrel_dir = folder / ".kestrel"
        if not (kestrel_dir / "kestrel_database.csv").is_file():
            return set(), set()
        try:
            from kestrel_analyzer.database import load_database
        except ImportError:
            from analyzer.kestrel_analyzer.database import load_database  # type: ignore[no-redef]
        try:
            database, _db_path = load_database(
                str(kestrel_dir), analyzer_name="cloud-compute-select"
            )
        except Exception as e:
            warn(f"[cloud-compute] load_database failed for {folder}: {e}")
            return set(), set()
        if database.empty or "filename" not in database.columns:
            return set(), set()
        analyzed = {
            str(f).strip() for f in database["filename"].values if str(f).strip()
        }
        errored: set = set()
        if "species" in database.columns:
            mask = database["species"].astype(str) == "Error"
            errored = {
                str(f).strip()
                for f in database.loc[mask, "filename"].values
                if str(f).strip()
            }
        return analyzed, errored

    def _cc_select_upload_files(self, folder, retry_errored: bool = False) -> tuple:
        """Resume-aware file-selection for cloud upload.

        Mirrors the local pipeline's "pick up where Kestrel left off" behavior:
        reads ``<folder>/.kestrel/kestrel_database.csv`` to discover which
        images have already been analyzed, then returns only the unprocessed
        ones — **prepending the last alphabetically-analyzed file as a
        scene-merger anchor** so the cloud pipeline's per-image similarity
        check has a real previous_image to compare against. Without the
        anchor, the first new image would have no previous_image and could be
        wrongly split into a new scene.

        When ``retry_errored=True``, rows with ``species == "Error"`` are
        treated as un-analyzed (re-uploaded + expected to be overwritten by
        the cloud-result merge), and the file immediately preceding each
        errored file (by sort order) is added to the protected-anchor set
        so the cloud pipeline has a real previous_image for scene continuity
        at the errored file's position. The pack-merge respects the protected
        set by passing it to ``merge_pack_into_kestrel(..., protected_filenames=...)``.

        Returns ``(upload_files, anchor_filename, anchor_filenames,
        total_in_folder, already_analyzed_count)`` where:
          - ``anchor_filename``: the primary (last-alphabetical) anchor, used
            for display/log messages. May be ``None``.
          - ``anchor_filenames``: frozenset of ALL filenames we re-upload
            purely for scene continuity (the primary anchor plus any
            per-errored-predecessor anchors). Caller MUST pass this to
            ``merge_pack_into_kestrel`` as ``protected_filenames`` so the
            cloud pipeline's re-analysis of these anchor frames doesn't
            clobber the user's already-good local rows.

        Returns an empty ``upload_files`` list when there is nothing new to
        analyze — the caller should treat that as a no-op.
        """
        from pathlib import Path as _Path
        folder = _Path(folder)
        # Discover analyzable images via the SAME code path the local pipeline
        # uses — folder_inspector.list_images_in_folder → select_camera_images —
        # so a cloud job analyzes exactly the files local analysis would. That
        # shared helper applies RAW-priority correctly: a JPEG is dropped only
        # when a same-stem RAW exists (an in-camera sidecar); an ORPHAN JPEG (a
        # lone JPG-only frame in an otherwise-RAW shoot) is KEPT, and hidden /
        # AppleDouble files are filtered. This function previously re-implemented
        # its own `raws if raws else jpegs` filter, which dropped orphan JPEGs and
        # silently truncated such folders by one image vs. local (off-by-one). Do
        # NOT fork the discovery rule again — share folder_inspector.
        import importlib
        try:
            _inspector = importlib.import_module('analyzer.folder_inspector')
        except Exception:
            _inspector = importlib.import_module('folder_inspector')
        # Returns sorted image NAMES; map back to Paths in this folder. Order is
        # lexical, matching the canonical processing order submit_job re-asserts.
        all_files = [folder / name for name in _inspector.list_images_in_folder(str(folder))]
        if not all_files:
            return [], None, frozenset(), 0, 0

        # Read analyzed/errored filenames via the shared helper so legacy CSVs
        # get migrated (load_database → _needs_upgrade → _perform_db_upgrade)
        # the same way the local enqueue path does. Without this, a folder
        # whose CSV still has the pre-migration rating/scene_name columns
        # would have its user data silently dropped on first cloud pack-merge.
        analyzed, errored = self._cc_load_analyzed_filenames(folder)

        # When retry_errored is on, errored filenames are NOT considered
        # "analyzed" for the skip filter, so they get re-uploaded. They are,
        # however, expected to be overwritten by the cloud result-merge —
        # they're NOT added to the protected anchor set.
        skip = analyzed - errored if retry_errored else analyzed
        new_files = [p for p in all_files if p.name not in skip]

        # Build the protected-anchor set. The primary anchor is the last
        # alphabetical analyzed-and-not-errored file (same as before). When
        # retry_errored is on, we ALSO need a scene-continuity anchor for each
        # errored gap: the immediately-preceding file in the sorted folder
        # listing. That predecessor is a healthy already-analyzed row whose
        # local data we MUST keep, hence membership in the protected set.
        protected: set = set()
        anchor_filename = None
        if analyzed and new_files:
            # Healthy-analyzed = analyzed minus errored. Errored rows being
            # re-uploaded shouldn't double as scene anchors (their species
            # value is "Error", not a real classification).
            healthy = analyzed - errored
            healthy_in_folder = [p for p in all_files if p.name in healthy]
            if healthy_in_folder:
                primary = healthy_in_folder[-1]
                anchor_filename = primary.name
                protected.add(primary.name)
                if primary not in new_files:
                    new_files = [primary] + new_files

        if retry_errored and errored:
            errored_in_folder = [p for p in all_files if p.name in errored]
            # Build index map once so predecessor lookup is O(1) per errored file.
            index_by_path = {p: i for i, p in enumerate(all_files)}
            for ep in errored_in_folder:
                idx = index_by_path.get(ep)
                if idx is None or idx == 0:
                    continue  # first file in folder has no predecessor
                pred = all_files[idx - 1]
                # Skip a predecessor that's itself errored or un-analyzed —
                # neither provides a clean scene-continuity baseline.
                if pred.name in errored or pred.name not in analyzed:
                    continue
                protected.add(pred.name)
                if pred not in new_files:
                    new_files = [pred] + new_files

        return (
            new_files,
            anchor_filename,
            frozenset(protected),
            len(all_files),
            len(analyzed),
        )

    def _cc_analysis_settings_snapshot(self) -> dict | None:
        """Project the user's local advanced-analysis settings into the
        cloud-compute wire format.

        The wire allowlist (``cloud_compute_client.ANALYSIS_SETTINGS_ALLOWLIST``)
        is intentionally narrow — only ``detector_name``, ``confidence_threshold``
        and a handful of feature toggles cross to Modal today. We pull each from
        the same ``settings.json`` keys the local queue reads at enqueue time,
        so picking ``Cloud`` from the destination toggle uses the same advanced
        settings as ``Local`` would. ``filter_analysis_settings`` (called by
        ``CloudComputeClient.submit_job``) will then drop anything the wire
        doesn't accept, so this can safely include keys that aren't yet wired
        up on the Modal side (forward-compatible).
        """
        try:
            settings = self.get_settings()
            if not isinstance(settings, dict):
                return None
            cfg = settings.get("settings") if "settings" in settings else settings
            if not isinstance(cfg, dict):
                return None
        except Exception:
            return None
        # Mirrors the local queue's advanced-settings keys (visualizer.js
        # ~line 8285-8318). Cloud takes whatever subset it can use; the rest
        # are dropped at the filter step.
        candidate: dict = {}
        det = cfg.get("detector_name")
        if isinstance(det, str) and det:
            candidate["detector_name"] = det
        thr = cfg.get("detection_threshold")
        if isinstance(thr, (int, float)) and 0.10 <= float(thr) <= 0.99:
            candidate["confidence_threshold"] = float(thr)
        # Boolean feature toggles. Project from the same flag names the local
        # pipeline checks. Missing → omit (Modal uses its built-in default).
        for src_key, wire_key in (
            ("species_detection_enabled", "species_detection_enabled"),
            ("wildlife_enabled",          "wildlife_enabled"),
            ("scene_grouping_enabled",    "scene_grouping_enabled"),
            ("crop_generation_enabled",   "crop_generation_enabled"),
            ("quality_model_enabled",     "quality_model_enabled"),
            ("retry_errored",             "retry_errored"),
        ):
            v = cfg.get(src_key)
            if isinstance(v, bool):
                candidate[wire_key] = v
        # Advanced numeric/enum settings. Range guards mirror the CLI's
        # documented ranges (cli.py) so we don't ship out-of-range values that
        # Modal would just clamp anyway.
        mbc = cfg.get("max_bird_crops")
        if isinstance(mbc, int) and not isinstance(mbc, bool) and 1 <= mbc <= 20:
            candidate["max_bird_crops"] = mbc
        eq = cfg.get("exposure_quality")
        if isinstance(eq, str) and eq in ("lenient", "balanced", "aggressive"):
            candidate["exposure_quality"] = eq
        stt = cfg.get("scene_time_threshold")
        if isinstance(stt, (int, float)) and not isinstance(stt, bool) and 0.0 <= float(stt) <= 60.0:
            candidate["scene_time_threshold"] = float(stt)
        tmw = cfg.get("thumbnail_max_width")
        if isinstance(tmw, int) and not isinstance(tmw, bool) and 400 <= tmw <= 2400:
            candidate["thumbnail_max_width"] = tmw
        tjc = cfg.get("thumbnail_jpeg_compression")
        if isinstance(tjc, (int, float)) and not isinstance(tjc, bool) and 0.50 <= float(tjc) <= 1.00:
            candidate["thumbnail_jpeg_compression"] = float(tjc)
        return candidate or None

    # Default cached remote counters — keeps the JS render code simple by
    # guaranteeing every numeric counter is a number, never `None`.
    _CC_REMOTE_DEFAULTS: dict = {  # type: ignore[var-annotated]
        "uploadedCount": 0,
        "analyzedCount": 0,
        "dispatchedCount": 0,
        "pendingCount": 0,
        "downloadedCount": 0,
        "pack_count": 0,
        "stopRequested": False,
        "remoteStatus": None,
        # Worker's upload_complete flag (POST /api/jobs/:id/complete sets this).
        # JS layer uses the false→true flip as one of two triggers for
        # maybeStartNextCloudJob (the other is remoteStatus → terminal). On
        # free-tier (limit=1) this is a no-op since uploadComplete doesn't
        # free a slot; on paid-tier (limit>=2) it lets the next folder's
        # upload start as soon as the previous folder's upload finishes.
        "uploadComplete": False,
        "updatedAtMs": 0,
        "failureCount": 0,
        "lastError": None,
        # §0 UX-rework additions (no worker change — these already ship in
        # GET /api/jobs/{id}):
        #  - retrievedCount: images whose result pack the client has pulled
        #    from the worker (results_status='results_retrieved'); drives the
        #    third "results retrieved" progress bar.
        #  - activeContainerCount: live Modal containers (jobs.active_container_count);
        #    drives the per-job "Additional information" container tally.
        #  - terminalReason: jobs.terminal_reason once terminal
        #    (complete | client_disconnected | modal_retries_exhausted |
        #    runaway_dispatch | stalled_no_container | user_cancel | orphan_reaped …);
        #    drives the friendly per-state explanation banner.
        "retrievedCount": 0,
        "activeContainerCount": 0,
        "terminalReason": None,
        # Server-authoritative status booleans (the desktop renders buttons off
        # these instead of inferring from status + counts). Defaults match a
        # freshly-submitted job (non-terminal, cancellable, accepting uploads).
        "terminal": False,
        "cancellable": True,
        "resultsAvailable": False,
        "acceptingUploads": True,
    }

    def _cc_apply_remote_snapshot(self, job_id: str, remote: dict) -> None:
        """Merge a fresh remote snapshot from the Worker into the per-job cache.

        Idempotent. Holds the cc lock briefly to swap the dict; the JS render
        path reads from here under the same lock so partial updates can't be
        observed."""
        import time as _t
        snapshot = dict(self._CC_REMOTE_DEFAULTS)
        for key in (
            "uploadedCount", "analyzedCount", "dispatchedCount", "pendingCount",
            "downloadedCount", "pack_count", "retrievedCount",
            "stopRequested", "uploadComplete",
            # Server-authoritative status booleans (camelCase on the wire).
            "terminal", "cancellable", "resultsAvailable", "acceptingUploads",
        ):
            if key in remote and remote[key] is not None:
                snapshot[key] = remote[key]
        rs = remote.get("status")
        if isinstance(rs, str) and rs:
            snapshot["remoteStatus"] = rs
        # active_container_count + terminal_reason ride along on the raw jobs
        # row (worker spreads `...job` into GET /api/jobs/{id}). snake_case on
        # the wire; normalise to the camelCase keys the JS layer reads.
        acc = remote.get("active_container_count")
        if acc is not None:
            try:
                snapshot["activeContainerCount"] = int(acc)
            except (TypeError, ValueError):
                snapshot["activeContainerCount"] = 0
        tr = remote.get("terminal_reason")
        if isinstance(tr, str) and tr:
            snapshot["terminalReason"] = tr
        snapshot["updatedAtMs"] = int(_t.time() * 1000)
        snapshot["failureCount"] = 0
        snapshot["lastError"] = None
        with self._ensure_cc_lock():
            state = self._cc_jobs.get(job_id)
            if state is not None:
                state["remote"] = snapshot

    def _cc_finalize_pack_merge(
        self,
        folder,
        job_id: str,
        pack_name: str,
        dest_zip,
        client,
    ) -> None:
        """Per-pack post-merge cleanup. Called from both the live job path
        (`_on_pack_merged` callback in submit_job) and the resume-download
        worker.

        Order matters — durability-first:
          1. Folder-local truth gets the merged-pack mark first. After this,
             the next bootstrap will treat the pack as merged regardless of
             whether the local zip still exists or the R2 delete fired.
          2. Best-effort local zip delete (we don't need the bytes anymore).
          3. Best-effort Worker delete-packs call. Failures are absorbed;
             Worker cron reaps stale R2 when results are fully retrieved.

        Each step is independent: a failure at step 2 doesn't block step 3,
        and vice versa.
        """
        try:
            from cloud_folder_state import mark_pack_merged as _mark
            _mark(folder, job_id, pack_name)
        except Exception as e:
            warn(f"[cloud-compute] {job_id}: mark_pack_merged({pack_name}) failed: {e}")
        try:
            if dest_zip is not None and dest_zip.exists():
                dest_zip.unlink()
        except Exception as e:
            warn(f"[cloud-compute] {job_id}: local zip cleanup ({pack_name}) failed: {e}")
        if client is not None:
            try:
                client.delete_packs(job_id, [pack_name])
            except Exception as e:
                warn(f"[cloud-compute] {job_id}: R2 delete_packs({pack_name}) failed (will retry on next bootstrap): {e}")

    def _cc_record_remote_failure(self, job_id: str, err: str) -> None:
        """Bump the per-job remote-failure counter and stash the latest error.
        Does NOT zero out the cached counters — JS keeps rendering the
        last-known good values + a 'syncing…' badge driven by ``updatedAtMs``."""
        with self._ensure_cc_lock():
            state = self._cc_jobs.get(job_id)
            if state is None:
                return
            cur = state.get("remote") or dict(self._CC_REMOTE_DEFAULTS)
            cur["failureCount"] = int(cur.get("failureCount") or 0) + 1
            cur["lastError"] = str(err)[:240]
            state["remote"] = cur

    def _cc_start_remote_poller(self, job_id: str) -> None:
        """Start a single background poller thread that refreshes the per-job
        cached remote snapshot every ``_CC_POLL_INTERVAL_SEC``. Idempotent —
        a no-op if a poller is already running for this job_id.

        The poller's lifetime is tied to the LOCAL job status, NOT to
        ``cancel_event``: ``run_full_job`` sets ``cancel_event`` on the first
        'incomplete'/'cancelled'/'failed' remote tick to STOP UPLOADS, but the
        local status only flips to its terminal mapping after run_full_job
        finishes draining downloads. If the poller exited on cancel_event it
        would stop refreshing the cached `remote` snapshot during the entire
        incomplete/cancelled drain and the UI's analysis counters would freeze.
        So we keep refreshing until the local status is terminal
        (``done|failed|cancelled|incomplete``). We still short-circuit on a USER
        cancellation (local status already flipped to 'cancelled' by
        cloud_compute_cancel_job) via the terminal-status check below."""
        import threading as _t
        with self._ensure_cc_lock():
            existing = self._cc_poll_threads.get(job_id)
            if existing is not None and existing.is_alive():
                return

        def _poller() -> None:
            import time as _time
            while True:
                with self._ensure_cc_lock():
                    state = self._cc_jobs.get(job_id)
                    if state is None:
                        return
                try:
                    client, client_err = self._cc_make_client()
                    if client is None:
                        # Auth gone (e.g. JWT expired). Record + back off.
                        self._cc_record_remote_failure(
                            job_id,
                            (client_err or {}).get("error") or "no client",
                        )
                    else:
                        remote = client.get_status(job_id)
                        self._cc_apply_remote_snapshot(job_id, remote)
                        # Client-side pause was removed — the only client-driven
                        # job control left is cancel. The poller just refreshes
                        # the remote snapshot.
                except Exception as e:
                    self._cc_record_remote_failure(job_id, str(e))
                    # Log every 5th consecutive failure so the journal doesn't
                    # drown but the user can still find the original cause.
                    with self._ensure_cc_lock():
                        st = self._cc_jobs.get(job_id) or {}
                        fc = int(((st.get("remote") or {}).get("failureCount")) or 0)
                    if fc == 1 or fc % 5 == 0:
                        warn(f"[cloud-compute] poller {job_id}: failure #{fc}: {e}")
                # Terminal-status check AFTER the refresh, not before. The tick
                # that observes done/failed/cancelled/incomplete then also
                # captures any final remote bump (e.g. Modal's /progress lands
                # within the same poll window as the local 'done' flip from the
                # download worker, but on the prior arrangement we'd exit before
                # the refresh and freeze analyzedCount one tick stale).
                # 'incomplete' is in the exit set: by the time the LOCAL status
                # is 'incomplete', run_full_job has finished draining (active
                # containers hit 0), so there's nothing left to refresh.
                with self._ensure_cc_lock():
                    state = self._cc_jobs.get(job_id)
                    if state is None:
                        return
                    if state.get("status") in (
                        "done", "failed", "cancelled", "incomplete"
                    ):
                        return
                _time.sleep(_CC_POLL_INTERVAL_SEC)

        thread = _t.Thread(target=_poller, name=f"cc-poll-{job_id}", daemon=True)
        with self._ensure_cc_lock():
            self._cc_poll_threads[job_id] = thread
        thread.start()

    def cloud_compute_submit_job(self, root_path: str) -> dict:
        """Kick off a cloud-compute job for a folder of images. Non-blocking.

        Snapshots the cloud-compute analysis-settings overrides at submit time
        (matches the local-queue pattern) and forwards them to the Worker so
        Modal can splice them into the analyzer subprocess. Returns
        immediately with ``{ok, jobId, imageCount}`` (or an error dict); a
        background thread handles the upload + poll + merge. Track with
        ``cloud_compute_get_status(jobId)`` and ``cloud_compute_list_jobs()``.
        """
        try:
            ccc = self._cc_import()
        except ImportError as e:
            return {"ok": False, "error": f"cloud_compute_client import failed: {e}"}

        root_real, err = self._validate_root_dir(
            root_path, context="cloud_compute_submit_job", require_exists=True
        )
        if err:
            return {"ok": False, "error": err}

        client, client_err = self._cc_make_client()
        if client_err is not None:
            return client_err

        from pathlib import Path as _Path
        root = _Path(root_real)
        # Resume-aware selection: skip files the local pipeline has already
        # analyzed (folder_inspector-style discovery), but RE-include the last
        # already-analyzed file as a scene-merger anchor so the cloud
        # pipeline's previous_image is real, not None. With retry_errored on,
        # also include errored rows + the file before each errored row.
        analysis_settings = self._cc_analysis_settings_snapshot()
        _retry_errored = bool((analysis_settings or {}).get("retry_errored"))
        files, anchor_filename, anchor_filenames, total_in_folder, already_analyzed = (
            self._cc_select_upload_files(root, retry_errored=_retry_errored)
        )
        if not files:
            if total_in_folder == 0:
                return {"ok": False, "error": "No supported image files found in folder"}
            return {
                "ok": False,
                "error": (
                    f"All {already_analyzed} of {total_in_folder} image(s) in "
                    "this folder are already analyzed — nothing to send to "
                    "cloud compute."
                ),
                "nothingToDo": True,
            }

        # The Worker stamps a dense image_index in the EXACT order we send
        # fileNames, and that index is now the canonical processing order for
        # the whole cloud pipeline (manifests, container ranges, scaling, scene
        # continuity). Sort by name so the index order is the natural lexical
        # order regardless of how _cc_select_upload_files arranged anchors /
        # errored-predecessors (it prepends them). The same sorted list backs
        # both submit_job (presigned-URL order) and run_full_job (upload order),
        # so indices, URLs, and uploads stay aligned. (Time-domain/capture-time
        # ordering is a deferred future change; lexical matches today's order.)
        files = sorted(files, key=lambda p: p.name)

        # Log file-selection details so the user can see if files are being reused
        # or re-analyzed (mirrors the local pipeline's "Picking up where Kestrel
        # left off" message via the queue manager logs).
        new_count = len(files) - len(anchor_filenames)
        if already_analyzed > 0:
            info(
                f"[cloud-compute] Picking up where Kestrel left off: "
                f"{already_analyzed} analyzed, sending {new_count} new + {len(anchor_filenames)} anchor(s)"
            )
        else:
            info(f"[cloud-compute] No prior analysis found, sending all {len(files)} file(s) to cloud")

        # Submit synchronously (cheap call). We need the jobId before we can
        # return it to the caller; the heavy upload+poll runs on a thread.
        try:
            submit = client.submit_job(files, analysis_settings=analysis_settings)
        except ccc.JobInProgressError as e:
            # Stage 6 concurrency gate: a Cloud Compute job is already in
            # flight for this user. Not a fault — surface to JS with a
            # MyAccount deep-link instead of an error toast. ``activeJobIds`` /
            # ``current`` / ``limit`` are passed through from the Worker's
            # 403 body so the desktop's auto-drain queue can decide between
            # "wait" and "warn about orphan" without hitting the Auth Worker.
            return {
                "ok": False,
                "error": "job_in_progress",
                "activeJobId": e.active_job_id,
                "activeJobIds": list(e.active_job_ids) if e.active_job_ids else [],
                "current": e.current,
                "limit": e.limit,
                "myAccountUrl": "https://myaccount.projectkestrel.org/cloud-compute",
                "message": str(e) or "You have a Cloud Compute job running.",
            }
        except ccc.LegalAcceptanceRequiredError as e:
            # Launch item #13: updated ToS / Privacy Policy. Open the accept
            # page in the system browser so the user can review and agree
            # there. Best-effort — if webbrowser fails, the URL is still
            # surfaced to JS for an in-app link.
            try:
                webbrowser.open(e.accept_url, new=2)
            except Exception as _e:
                warn(f"[cloud-compute] failed to launch browser for legal accept: {_e}")
            return {
                "ok": False,
                "error": "legal_acceptance_required",
                "acceptUrl": e.accept_url,
                "currentEffectiveDate": e.current_effective_date,
                "message": (
                    str(e)
                    or "Project Kestrel's Terms of Service or Privacy Policy "
                       "have been updated. Please review and accept in your browser."
                ),
            }
        except ccc.CloudComputeError as e:
            mapped = self._cc_submit_error_response(e)
            if mapped is not None:
                return mapped
            return {
                "ok": False,
                "error": self._sanitize_cloud_error_message(e.message),
                "status": e.status,
                "needSignIn": e.status == 401,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

        job_id = str(submit.get("jobId") or "")
        if not job_id:
            return {"ok": False, "error": "Worker returned no jobId"}

        import threading as _t
        cancel_event = _t.Event()

        def _on_progress(payload: dict) -> None:
            with self._ensure_cc_lock():
                state = self._cc_jobs.get(job_id)
                if state is not None:
                    state["progress"] = dict(payload)

        def _on_pack_merged(pack_name: str) -> None:
            # Record both in cloud_jobs_store (persistent legacy cache) and
            # the in-memory event queue (drained by the JS poll for live
            # folder refreshes).
            try:
                store = self._cc_jobs_store()
                store.add_downloaded_pack(job_id, pack_name)
            except Exception:
                pass
            # Folder-local truth + bounded R2 storage: mark merged, drop
            # the local zip, ask the Worker to delete the R2 pack. See
            # _cc_finalize_pack_merge for the durability ordering.
            try:
                from pathlib import Path as _P
                pack_dir = _P(root) / ".kestrel" / "cloud-packs"
                self._cc_finalize_pack_merge(
                    _P(root), job_id, pack_name, pack_dir / pack_name, client,
                )
            except Exception as e:
                warn(f"[cloud-compute] {job_id}: pack-merge finalize failed: {e}")
            with self._ensure_cc_lock():
                self._cc_pack_events.append({
                    "jobId": job_id,
                    "folderPath": str(root),
                    "packName": pack_name,
                })

        def _worker() -> None:
            try:
                result = client.run_full_job(
                    root,
                    file_paths=files,
                    analysis_settings=analysis_settings,
                    on_progress=_on_progress,
                    on_pack_merged=_on_pack_merged,
                    cancel_event=cancel_event,
                    protected_filenames=set(anchor_filenames) if anchor_filenames else None,
                    overwrite_errors=_retry_errored,
                    # Pass the pre-submitted job ID and presigned URLs so
                    # run_full_job skips its internal submit_job call.
                    # Without this, two Worker jobs are created: the poller
                    # watches the first; uploads go to the second — counters
                    # never advance in the UI.
                    job_id=job_id,
                    presigned_urls=submit.get("presignedUrls", []),
                )
            except ccc.JobCancelled:
                # User clicked Cancel; cloud_compute_cancel_job has already
                # set status='cancelled' in both the in-memory map and the
                # persistent ledger. Don't overwrite that with 'failed'.
                return
            except ccc.CloudComputeAuthError:
                # Session expired (401) and the in-client token refresh couldn't
                # recover within its budget — e.g. laptop slept long enough that
                # the refresh token also aged out, or the user is signed out.
                # This is TRANSIENT: the job is fine server-side and result packs
                # stay downloadable. Do NOT mark 'failed'. Keep the current
                # non-terminal local status and surface a friendly reconnect
                # message; the next app launch / sign-in resumes the download.
                with self._ensure_cc_lock():
                    state = self._cc_jobs.get(job_id)
                    if state is not None and state.get("status") not in (
                        "done", "failed", "cancelled", "incomplete"
                    ):
                        state["error"] = "Session expired — reconnecting…"
                return
            except Exception as e:
                with self._ensure_cc_lock():
                    state = self._cc_jobs.get(job_id)
                    if state is not None:
                        state["status"] = "failed"
                        state["error"] = str(e)
                try:
                    self._cc_jobs_store().update_job(job_id, status="failed")
                except Exception:
                    pass
                return
            # Map the remote terminal status to a local one. 'incomplete' is NOT
            # 'failed' — the client merely disconnected >10min with uploads
            # unfinished; the in-session poller already drained what it could,
            # and restart-resume of incomplete jobs is deferred. Surface it as a
            # distinct 'incomplete' badge instead of a scary failure.
            status_str = str(result.get("status") or "")
            if result.get("ok"):
                terminal = "done"
            elif status_str == "incomplete":
                terminal = "incomplete"
            else:
                terminal = "failed"
            # Capture the worker's terminal_reason (cached on the remote
            # snapshot by the poller) so the §4 history panel can show a
            # specific "why it ended" message in future sessions without a
            # Worker round-trip. None for a clean completion.
            terminal_reason = None
            with self._ensure_cc_lock():
                state = self._cc_jobs.get(job_id)
                if state is not None:
                    rsnap = state.get("remote") or {}
                    tr = rsnap.get("terminalReason")
                    if isinstance(tr, str) and tr:
                        terminal_reason = tr
                    # Don't clobber a cancellation that landed during the
                    # final stretch (race between cancel + run_full_job's
                    # natural completion).
                    if state.get("status") != "cancelled":
                        state["status"] = terminal
                        state["result"] = result
            try:
                _upd = {"status": terminal}
                if terminal_reason:
                    _upd["terminalReason"] = terminal_reason
                self._cc_jobs_store().update_job(job_id, **_upd)
            except Exception:
                pass

        owner_id = self._cc_owner_id()
        with self._ensure_cc_lock():
            self._cc_jobs[job_id] = {
                "jobId": job_id,
                "ownerId": owner_id,
                "rootPath": str(root),
                "imageCount": len(files),
                "newImageCount": len(files) - len(anchor_filenames),
                "anchorFilename": anchor_filename,
                "anchorFilenames": sorted(anchor_filenames),
                "totalInFolder": total_in_folder,
                "alreadyAnalyzed": already_analyzed,
                "status": "uploading",
                "progress": {"event": "submitted"},
                "cancel_event": cancel_event,
                "presignedUrls": submit.get("presignedUrls", []),  # for completeness
                # Cached remote-counters snapshot. The background poller
                # refreshes this; JS reads it via cloud_compute_list_jobs.
                # Defaults are zeros so JS never sees `undefined → 0` flicker
                # before the first poll lands.
                "remote": dict(self._CC_REMOTE_DEFAULTS),
            }

        # Persist to cloud_jobs_store so a startup poll can discover this job
        # after a restart. settingsSnapshot is the same allowlisted dict the
        # Worker received so the audit trail matches what Modal actually ran.
        try:
            store = self._cc_jobs_store()
            store.upsert_job({
                "jobId": job_id,
                "ownerId": owner_id,
                "folderPath": str(root),
                "createdAtUtc": store.utc_now_iso(),
                "status": "uploading",
                "imageCount": len(files),
                "anchorFilename": anchor_filename or "",
                "anchorFilenames": sorted(anchor_filenames),
                "settingsSnapshot": analysis_settings or {},
                "downloadedPacks": [],
            })
        except Exception:
            pass

        thread = _t.Thread(target=_worker, name=f"cc-job-{job_id}", daemon=True)
        thread.start()
        with self._ensure_cc_lock():
            self._cc_jobs[job_id]["thread"] = thread
        # Start the per-job remote-status poller. Background-thread that
        # refreshes the cached `remote` snapshot every _CC_POLL_INTERVAL_SEC.
        # JS renders from the cache so the UI never depends on the JS-tick
        # cadence aligning with a successful Worker fetch.
        self._cc_start_remote_poller(job_id)

        return {
            "ok": True,
            "jobId": job_id,
            "imageCount": len(files),
            "newImageCount": len(files) - len(anchor_filenames),
            "anchorFilename": anchor_filename,
            "anchorFilenames": sorted(anchor_filenames),
            "totalInFolder": total_in_folder,
            "alreadyAnalyzed": already_analyzed,
        }

    def _cc_serialise_job(self, job_id: str, state: dict) -> dict:
        """Build the wire-shape descriptor for one job. Reads the cached
        ``remote`` snapshot maintained by the background poller; never
        triggers a Worker call so this is safe to call on every render tick.
        Caller MUST hold the cc lock."""
        remote = dict(state.get("remote") or self._CC_REMOTE_DEFAULTS)
        out = {
            "jobId": job_id,
            "rootPath": state.get("rootPath"),
            "imageCount": state.get("imageCount"),
            "newImageCount": state.get("newImageCount"),
            "anchorFilename": state.get("anchorFilename"),
            "totalInFolder": state.get("totalInFolder"),
            "alreadyAnalyzed": state.get("alreadyAnalyzed"),
            "status": state.get("status", "running"),
            # Optional local tag explaining a non-obvious terminal status.
            "failureReason": state.get("failureReason") or "",
            "progress": dict(state.get("progress") or {}),
            # Cached remote counters (zeros until first poll lands).
            "uploadedCount": remote.get("uploadedCount", 0),
            "analyzedCount": remote.get("analyzedCount", 0),
            "dispatchedCount": remote.get("dispatchedCount", 0),
            "pendingCount": remote.get("pendingCount", 0),
            "downloadedCount": remote.get("downloadedCount", 0),
            "pack_count": remote.get("pack_count", 0),
            # §1: images whose result pack the client has retrieved from the
            # worker — numerator of the third "results retrieved" bar.
            "retrievedCount": remote.get("retrievedCount", 0),
            # §2: live Modal container count for the "Additional information"
            # disclosure (0 once the job is terminal).
            "activeContainerCount": remote.get("activeContainerCount", 0),
            # §3: server-side terminal reason for the friendly "why it ended"
            # banner. None until the job reaches a terminal state.
            "terminalReason": remote.get("terminalReason"),
            "stopRequested": remote.get("stopRequested", False),
            # True once the desktop has called /api/jobs/:id/complete and the
            # Worker has recorded upload_complete=1. JS uses the false→true
            # flip to trip the auto-drain queue (relevant on paid tiers with
            # maxConcurrentJobs>=2).
            "uploadComplete": bool(remote.get("uploadComplete", False)),
            "remoteStatus": remote.get("remoteStatus"),
            # Server-authoritative status booleans — the JS layer renders buttons
            # straight off these (no status/count inference). See _CC_REMOTE_DEFAULTS.
            "terminal": bool(remote.get("terminal", False)),
            "cancellable": bool(remote.get("cancellable", True)),
            "resultsAvailable": bool(remote.get("resultsAvailable", False)),
            "acceptingUploads": bool(remote.get("acceptingUploads", True)),
            # Staleness signals for the UI: updatedAtMs is wall-clock of last
            # successful poll (0 means "never"); failureCount is consecutive
            # failures since the last success; lastError is the most recent
            # network/HTTP error string (truncated). The JS layer renders a
            # 'syncing…' badge when staleness > threshold.
            "remoteUpdatedAtMs": remote.get("updatedAtMs", 0),
            "remoteFailureCount": remote.get("failureCount", 0),
            "remoteLastError": remote.get("lastError"),
        }
        if "result" in state:
            out["result"] = state["result"]
        if "error" in state:
            out["error"] = state["error"]
        return out

    def cloud_compute_get_status(self, job_id: str) -> dict:
        """Single-job descriptor read from the in-process cache. Cheap — does
        no Worker I/O. The cached counters are kept fresh by the per-job
        background poller started in ``cloud_compute_submit_job`` and
        ``cloud_compute_resume_download``."""
        with self._ensure_cc_lock():
            state = self._cc_jobs.get(job_id)
            if state is None:
                return {"ok": False, "error": "unknown jobId"}
            descriptor = self._cc_serialise_job(job_id, state)
        descriptor["ok"] = True
        return descriptor

    def cloud_compute_list_jobs(self) -> dict:
        """Return rich descriptors (with cached remote counters) for every job
        submitted this session. JS renders the cloud queue panel from this
        single bridge call — no per-job follow-up needed."""
        # Filter to the current account so an in-session account switch (sign
        # out → sign in without restart) doesn't show the previous user's live
        # jobs. When the owner can't be resolved (signed out, or an undecodable
        # token) we fall back to showing all rather than hiding the user's own
        # in-flight jobs; un-owned (legacy) in-memory states are always shown.
        owner = self._cc_owner_id()
        with self._ensure_cc_lock():
            jobs = [
                self._cc_serialise_job(jid, state)
                for jid, state in self._cc_jobs.items()
                if (not owner)
                or (not (state.get("ownerId") or ""))
                or (state.get("ownerId") == owner)
            ]
        return {"ok": True, "jobs": jobs}

    # ─── Stage 5E — dashboard-feeding bridge methods ────────────────────
    # Thin proxies over the Worker's user-facing endpoints. The primary
    # consumer is the external online dashboard; the desktop UI keeps a
    # single "View cloud usage online →" link in Settings rather than
    # mirroring the full history table.

    def cloud_compute_list_history(self, filters: dict | None = None) -> dict:
        """Proxy GET /api/jobs (Stage 5C). ``filters`` is a dict matching the
        query params: ``status`` (str/csv, supports `'running'`), ``from`` /
        ``to`` (ISO datetimes), ``limit`` (int), ``cursor`` (opaque)."""
        client, client_err = self._cc_make_client()
        if client is None:
            return client_err or {"ok": False, "error": "no client"}
        f = filters or {}
        try:
            body = client.list_jobs(
                status=f.get("status"),
                from_iso=f.get("from"),
                to_iso=f.get("to"),
                limit=f.get("limit"),
                cursor=f.get("cursor"),
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        body["ok"] = True
        return body

    def cloud_compute_get_job_events(self, job_id: str, order: str = "desc") -> dict:
        """Proxy GET /api/jobs/:jobId/events (Stage 5C)."""
        client, client_err = self._cc_make_client()
        if client is None:
            return client_err or {"ok": False, "error": "no client"}
        try:
            body = client.get_job_events(job_id, order=order)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        body["ok"] = True
        return body

    def cloud_compute_get_job_timing_stats(self, job_id: str) -> dict:
        """Proxy GET /api/jobs/:jobId/timing-stats (Stage 5C)."""
        client, client_err = self._cc_make_client()
        if client is None:
            return client_err or {"ok": False, "error": "no client"}
        try:
            body = client.get_job_timing_stats(job_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        body["ok"] = True
        return body

    def cloud_compute_get_usage_summary(self, period: str = "monthly") -> dict:
        """Proxy GET /api/usage (Stage 5D). Returns aggregate totals for the
        current month or all-time. Used by the panel badge / Settings link."""
        client, client_err = self._cc_make_client()
        if client is None:
            return client_err or {"ok": False, "error": "no client"}
        try:
            body = client.get_usage(period=period)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        body["ok"] = True
        return body

    def cloud_compute_get_entitlements(self) -> dict:
        """Proxy GET /v1/me/entitlements on the Auth Worker. Returns the
        user's tier, plan limits, current-period usage, and active-job slots
        held — same payload MyAccount's Cloud Compute dashboard renders.

        Used by the analyze dialog's cloud queue logic to decide whether the
        next folder can submit now (``activeJobs.length <
        limits.maxConcurrentJobs``) or should wait for a slot. Failure is
        non-fatal — JS treats absent / errored response as "unknown, try
        anyway and let the Worker's 403 decide."""
        client, client_err = self._auth_make_client()
        if client is None:
            return client_err or {"ok": False, "error": "no client"}
        try:
            body = client.get_my_entitlements()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if not isinstance(body, dict):
            return {"ok": False, "error": "auth worker returned non-object"}
        body["ok"] = True
        return body

    def cloud_compute_clear_done(self) -> dict:
        """Legacy bridge hook — does **not** touch the persistent job ledger.

        The desktop's "Clear done" control only hides finished rows in the
        cloud-analysis pill (``cloud-compute.js`` client-side filter). Job
        history for the Account panel and startup resume still read the full
        on-disk manifest via ``cloud_jobs_store``.
        """
        return {"ok": True, "removed": []}

    def cloud_compute_cancel_job(self, job_id: str) -> dict:
        """Terminal cancel. Tells the Worker to stop the job, then — ONLY if the
        remote cancel actually LANDED — signals the local upload/poll thread to
        exit and marks the job ``cancelled`` in the persistent ledger.

        If the remote cancel can't be confirmed (network down / expired session),
        we DO NOT touch local state: the job is still running server-side, so a
        false local ``cancelled`` would desync the desktop. We return
        ``{ok:False, transient:True}`` so the UI keeps Cancel live to retry. A
        404 means the job is already gone server-side → safe to finalize.
        """
        with self._ensure_cc_lock():
            state = self._cc_jobs.get(job_id)
            if state is None:
                return {"ok": False, "error": "unknown jobId"}
            cancel_ev = state.get("cancel_event")

        _transient = (
            "Couldn't reach the server — the job is still running. Try again."
        )

        ccc = self._cc_import()
        client, client_err = self._cc_make_client()
        if client is None:
            # No client (e.g. signed out / token gone) — we never reached the
            # server, so the job may still be running. Transient.
            return {
                "ok": False, "transient": True, "error": _transient,
                "remoteError": (client_err or {}).get("error") or "no client",
            }

        landed = False
        try:
            client.cancel_job_remote(job_id)
            landed = True  # 200 — the Worker accepted the cancel (idempotent).
        except ccc.CloudComputeAuthError as e:
            # 401 after refresh failed — session expired. Transient.
            return {"ok": False, "transient": True, "error": _transient, "remoteError": str(e)}
        except ccc.CloudComputeNetworkError as e:
            # Transport-level failure (status 0). Transient.
            return {"ok": False, "transient": True, "error": _transient, "remoteError": str(e)}
        except ccc.CloudComputeError as e:
            # Other non-2xx. 404 = job already gone server-side → nothing left
            # running, safe to finalize. Anything else = server refused.
            if getattr(e, "status", None) == 404:
                landed = True
            else:
                return {
                    "ok": False,
                    "error": f"Server refused the cancel ({getattr(e, 'status', '?')}); job not cancelled.",
                    "remoteError": str(e),
                }
        except Exception as e:
            # Unknown error talking to the server — be conservative, treat as transient.
            return {"ok": False, "transient": True, "error": _transient, "remoteError": str(e)}

        if not landed:
            return {"ok": False, "transient": True, "error": _transient}

        # Remote cancel landed (200) or the job is already gone (404): finalize
        # locally. Signal the upload/poll thread to exit (the upload worker checks
        # _check_cancel(); there is no pause to release anymore), then mark cancelled.
        if cancel_ev is not None:
            cancel_ev.set()
        with self._ensure_cc_lock():
            if job_id in self._cc_jobs:
                self._cc_jobs[job_id]["status"] = "cancelled"
        try:
            self._cc_jobs_store().update_job(job_id, status="cancelled")
        except Exception:
            pass
        return {"ok": True}

    def stop_cloud_uploads_for_shutdown(self) -> int:
        """Signal every in-flight cloud-compute job's ``cancel_event`` so its
        upload worker pool stops pulling queued files. Called from the app's
        shutdown path (``visualizer.main``'s finally block).

        LOCAL-ONLY — we deliberately do NOT tell the Worker to cancel. The job
        keeps running server-side and its result packs resume downloading on the
        next launch. The sole purpose is to release the upload
        ``ThreadPoolExecutor``: its ``atexit`` handler joins mid-flight worker
        threads on interpreter shutdown, so without this the pool keeps
        uploading every remaining image (and the process hangs) long after the
        window has closed and the static server has stopped.

        Idempotent and failsafe. Returns the number of jobs signalled.
        """
        signalled = 0
        try:
            with self._ensure_cc_lock():
                events = [
                    state.get("cancel_event")
                    for state in self._cc_jobs.values()
                    if state is not None
                ]
        except Exception:
            return 0
        for ev in events:
            if ev is None:
                continue
            try:
                if not ev.is_set():
                    ev.set()
                    signalled += 1
            except Exception:
                pass
        return signalled

    def cloud_compute_upload_test(
        self,
        folder_path: str,
        sample_count: int = 10,
    ) -> dict:
        """Run a real-image upload-throughput probe against the staging bucket.

        Returns ``{ok, mbps, samples_uploaded, total_bytes, elapsed_ms,
        errors}``. Errors surface as ``{ok: False, error}``; the Worker's
        ``file_too_large`` rejection is propagated verbatim so the dialog can
        explain the 200 MB cap.
        """
        root_real, err = self._validate_root_dir(
            folder_path, context="cloud_compute_upload_test", require_exists=True
        )
        if err:
            return {"ok": False, "error": err}
        client, client_err = self._cc_make_client()
        if client is None:
            return client_err or {"ok": False, "error": "no client"}
        try:
            ccc = self._cc_import()
        except ImportError as e:
            return {"ok": False, "error": f"cloud_compute_client import failed: {e}"}
        from pathlib import Path as _Path
        try:
            result = client.upload_test(_Path(root_real), sample_count=sample_count)
        except ccc.CloudComputeError as e:
            return {
                "ok": False,
                "error": self._sanitize_cloud_error_message(e.message),
                "status": e.status,
            }
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        result["ok"] = True
        return result

    # §4 history: when the account panel asks for the full picture, also probe
    # the Worker for salvageable result packs on TERMINAL failed/cancelled/
    # incomplete jobs (the Worker keeps the RESULTS bucket on terminal-failure +
    # user-cancel paths — see the CC worker invariants — and reaps it ~24h
    # after terminal). Bounded so a power-user with a long ledger can't trigger
    # a request storm: we query at most the N most-recent eligible terminal
    # jobs. 'done' jobs are excluded (fully merged by definition) and
    # 'upload_interrupted' orphans are excluded (their staging was reaped and
    # they never produced server-side results).
    _CC_HISTORY_TERMINAL_QUERY_CAP = 60

    def cloud_compute_list_pending_jobs(self, include_terminal: bool = False) -> dict:
        """Enumerate persisted cloud jobs for display (read-only).

        Per job, best-effort Worker reconcile for counters and result packs.
        No local status mutation, no remote cancel, no R2 deletes.

        Each entry: ``{jobId, folderPath, status, imageCount, downloadedPacks,
        remoteStatus, availablePacks, …}``. ``remoteStatus`` / ``availablePacks``
        are ``None`` on transient Worker failures or when the job is skipped.

        ``include_terminal`` (account panel): when True, ALSO query salvageable
        packs on terminal failed/cancelled/incomplete jobs (bounded cap).
        """
        try:
            store = self._cc_jobs_store()
            # Filter to the signed-in account (claims legacy un-owned rows). A
            # signed-out caller resolves to "" → no jobs, so history never leaks
            # the previous account's jobs after a sign-out / account switch.
            owner_id = self._cc_owner_id()
            if owner_id:
                jobs = store.jobs_for_owner(owner_id)
            else:
                jobs = []
        except Exception as e:
            return {"ok": False, "error": f"store load failed: {e}", "jobs": []}

        if not jobs:
            return {"ok": True, "jobs": []}

        client, _ = self._cc_make_client()

        # Locally-terminal jobs (done/cancelled/failed) skip Worker I/O entirely
        # at bootstrap. They still appear in the returned list so the panel can
        # render them (and Clear Done can target them), but we don't burn
        # /api/jobs/* + /api/jobs/*/results requests on jobs the desktop has
        # already finalised. Avoids the audit's HIGH-2 case where a cancelled
        # job whose Worker race-condition'd to 'complete' still showed up as
        # resumable.
        #
        # EXCEPTION: 'incomplete' jobs are terminal-for-upload but their result
        # packs may still be sitting in R2 (analysis continued server-side after
        # the client disconnected). Those are DOWNLOAD-resumable, so we DO query
        # the Worker for their availablePacks — see _DOWNLOAD_RESUMABLE_STATUSES.
        from cloud_jobs_store import (
            _TERMINAL_STATUSES as _CC_TERMINAL_STATUSES,
            _DOWNLOAD_RESUMABLE_STATUSES as _CC_DL_RESUMABLE,
        )
        try:
            from cloud_folder_state import list_merged_packs as _fs_list_merged
        except Exception:
            _fs_list_merged = None  # noqa: N816

        # §4 history: precompute the bounded set of TERMINAL jobs to also probe
        # for salvageable packs (only when include_terminal). Eligible =
        # failed/cancelled/incomplete (NOT done — fully merged), excluding
        # upload_interrupted orphans (no server-side results). Most-recent-first,
        # capped, so the request count stays bounded regardless of ledger size.
        terminal_query_ids: set[str] = set()
        if include_terminal:
            eligible = [
                j for j in jobs
                if j["status"] in {"failed", "cancelled", "incomplete"}
                and (j.get("failureReason") or "") != "upload_interrupted"
            ]
            eligible.sort(key=lambda j: j.get("createdAtUtc") or "", reverse=True)
            terminal_query_ids = {
                j["jobId"] for j in eligible[:self._CC_HISTORY_TERMINAL_QUERY_CAP]
            }

        out_jobs: list[dict] = []
        for j in jobs:
            folder_available = bool(j.get("folderPath")) and os.path.isdir(j["folderPath"])
            # Folder-local merged truth wins over the legacy global cache —
            # but we union with `downloadedPacks` so jobs that pre-date the
            # folder-state file still dedup correctly. JS sees a single
            # `downloadedPacks` field and doesn't need to know about the
            # split source.
            legacy_downloaded = list(j.get("downloadedPacks") or [])
            folder_merged: list[str] = []
            if folder_available and _fs_list_merged is not None:
                try:
                    folder_merged = _fs_list_merged(j["folderPath"], j["jobId"])
                except Exception:
                    folder_merged = []
            merged_union = list(dict.fromkeys(legacy_downloaded + folder_merged))
            entry: dict = {
                "jobId": j["jobId"],
                "folderPath": j["folderPath"],
                "status": j["status"],
                "failureReason": j.get("failureReason") or "",
                "imageCount": j["imageCount"],
                "downloadedPacks": merged_union,
                "createdAtUtc": j.get("createdAtUtc"),
                "settingsSnapshot": j.get("settingsSnapshot") or {},
                # True if the folder is currently mounted/readable. JS uses
                # this to gate auto-resume: an unavailable folder (external
                # drive ejected, network share offline) is silently deferred
                # rather than throwing — the periodic recheck timer auto-
                # resumes once the folder reappears.
                "folderAvailable": folder_available,
                "remoteStatus": None,
                "availablePacks": None,
                # §4c history extras. terminalReason comes from the local
                # ledger by default (persisted when the job went terminal) and
                # is overwritten with the live worker value when we query below.
                "terminalReason": j.get("terminalReason") or None,
                "retrievedCount": None,
                "activeContainerCount": None,
            }
            # Query the Worker for non-terminal jobs AND for download-resumable
            # 'incomplete' jobs (only when the folder is mounted — no point
            # pulling a pack list for an ejected drive we can't merge into).
            # §4 history additionally probes the bounded terminal set regardless
            # of folder availability, so a missing-folder job still reveals that
            # recoverable packs EXIST (driving the "Locate folder…" affordance).
            is_terminal = j["status"] in _CC_TERMINAL_STATUSES
            is_dl_resumable = j["status"] in _CC_DL_RESUMABLE and folder_available
            query_terminal_history = j["jobId"] in terminal_query_ids
            if client is not None and (not is_terminal or is_dl_resumable or query_terminal_history):
                try:
                    remote = client.get_status(j["jobId"])
                    entry["remoteStatus"] = remote.get("status")
                    entry["analyzedCount"] = remote.get("analyzedCount")
                    # §1/§3 history extras (free on the same GET).
                    entry["retrievedCount"] = remote.get("retrievedCount")
                    entry["activeContainerCount"] = remote.get("active_container_count")
                    _live_tr = remote.get("terminal_reason")
                    if isinstance(_live_tr, str) and _live_tr:
                        entry["terminalReason"] = _live_tr
                    files = client.list_results(j["jobId"])
                    available = [
                        str(f.get("filename") or "")
                        for f in files
                        if str(f.get("filename") or "").endswith(".zip")
                    ]
                    entry["availablePacks"] = available
                except Exception:
                    pass
            # A 'done' job has, by construction, pulled every result pack
            # (status only flips to done once remote==complete AND all packs
            # merged — see cloud_compute_resume_download). We deliberately
            # don't spend a Worker call on done jobs, which left
            # retrievedCount=None and rendered "0 / N retrieved" in the account
            # panel. Backfill from imageCount — all results are in.
            if entry["retrievedCount"] is None and j["status"] == "done":
                entry["retrievedCount"] = j["imageCount"]
            out_jobs.append(entry)
        return {"ok": True, "jobs": out_jobs}

    def _cc_prepare_resume(self, job_id: str) -> dict:
        """Shared setup for ``cloud_compute_resume_download`` /
        ``cloud_compute_retrieve_results``: load the persisted job, verify the
        folder is mounted, make the Worker client, register the job into the
        in-memory ``_cc_jobs`` map (so the cloud queue pill renders it even
        though it predates this process), and start the remote poller.

        Returns either an error dict (``ok==False`` — caller returns it
        verbatim to JS, including the ``folder_unavailable`` soft-fail) or a
        success context dict (``ok==True``) carrying ``target``, ``folder``,
        ``client``, ``ccc``, ``store``, ``anchor_filenames`` and
        ``retry_errored`` for the download worker."""
        try:
            store = self._cc_jobs_store()
            jobs = store.load_jobs()
        except Exception as e:
            return {"ok": False, "error": f"store load failed: {e}"}
        target = next((j for j in jobs if j["jobId"] == job_id), None)
        if target is None:
            return {"ok": False, "error": "unknown jobId"}
        from pathlib import Path as _Path
        folder = _Path(target["folderPath"])
        if not folder.is_dir():
            # Soft-fail: external-drive eject / network-share unmount is
            # transient. Returning a structured `reason` lets JS show a
            # helpful "Folder not currently mounted" caption and start the
            # periodic recheck instead of dropping a noisy error toast.
            return {
                "ok": False,
                "reason": "folder_unavailable",
                "folderPath": str(folder),
                "error": f"folder not currently accessible: {folder}",
            }
        client, client_err = self._cc_make_client()
        if client is None:
            return client_err or {"ok": False, "error": "no client"}
        try:
            ccc = self._cc_import()
        except ImportError as e:
            return {"ok": False, "error": str(e)}

        anchor_filename = (target.get("anchorFilename") or "") or None
        # anchorFilenames is the post-retry_errored protected-anchor set
        # persisted by cloud_compute_submit_job. Older jobs (pre-this-change)
        # only have anchorFilename, so fall back to the singleton.
        _persisted_anchors = target.get("anchorFilenames")
        if isinstance(_persisted_anchors, (list, tuple)) and _persisted_anchors:
            anchor_filenames = frozenset(
                str(x) for x in _persisted_anchors if isinstance(x, str) and x
            )
        elif anchor_filename:
            anchor_filenames = frozenset({anchor_filename})
        else:
            anchor_filenames = frozenset()
        # Retry-errored: persisted in settingsSnapshot at submit time. We
        # don't re-read settings.json here because the user may have toggled
        # the flag off after submission; the job-time snapshot is authoritative.
        _snapshot = target.get("settingsSnapshot") or {}
        retry_errored = bool(isinstance(_snapshot, dict) and _snapshot.get("retry_errored"))
        with self._ensure_cc_lock():
            if job_id not in self._cc_jobs:
                self._cc_jobs[job_id] = {
                    "jobId": job_id,
                    # Carry the owner so a resumed job in the live pill is scoped
                    # to the current account (matches the submit path).
                    "ownerId": target.get("ownerId") or self._cc_owner_id(),
                    "rootPath": str(folder),
                    "imageCount": int(target.get("imageCount") or 0),
                    "newImageCount": int(target.get("imageCount") or 0),
                    "anchorFilename": anchor_filename,
                    "anchorFilenames": sorted(anchor_filenames),
                    "totalInFolder": None,
                    "alreadyAnalyzed": None,
                    "status": str(target.get("status") or "downloading"),
                    "progress": {"event": "resume"},
                    "cancel_event": None,
                    "remote": dict(self._CC_REMOTE_DEFAULTS),
                }
        # Start the live remote poller; safe to call again if already running.
        self._cc_start_remote_poller(job_id)
        return {
            "ok": True,
            "target": target,
            "folder": folder,
            "client": client,
            "ccc": ccc,
            "store": store,
            "anchor_filenames": anchor_filenames,
            "retry_errored": retry_errored,
        }

    def _cc_seed_already(self, target, folder, job_id) -> set:
        """Seed the merged-pack truth = union(folder-local cloud_folder_state,
        legacy desktop-store downloadedPacks). Folder-local is the post-fix
        authoritative source; the legacy field stays so old jobs that pre-date
        the folder-state file still dedup correctly."""
        try:
            from cloud_folder_state import list_merged_packs as _list_merged
            merged_in_folder = set(_list_merged(folder, job_id))
        except Exception:
            merged_in_folder = set()
        return set(target.get("downloadedPacks") or []) | merged_in_folder

    def cloud_compute_resume_download(self, job_id: str) -> dict:
        """Resume pack download + merge for an existing persisted job — a
        one-shot pull of whatever packs are available right now.

        Registers the job in the in-memory ``_cc_jobs`` map (so the cloud
        queue panel can render it), starts the standard background remote
        poller, and spawns a one-off worker that downloads + merges any
        packs not already present locally. Status is only marked ``done``
        when the Worker confirms ``status==complete`` AND every available
        pack has been downloaded — otherwise the live poller keeps tracking
        and the UI reflects real state. For a still-running job whose packs
        keep arriving, use ``cloud_compute_retrieve_results`` (continuous)."""
        ctx = self._cc_prepare_resume(job_id)
        if not ctx.get("ok"):
            return ctx
        folder = ctx["folder"]; client = ctx["client"]; ccc = ctx["ccc"]
        store = ctx["store"]; target = ctx["target"]
        anchor_filenames = ctx["anchor_filenames"]; retry_errored = ctx["retry_errored"]

        import threading as _t

        def _worker() -> None:
            already = self._cc_seed_already(target, folder, job_id)
            drained = self._cc_drain_packs_once(
                job_id, folder, client, ccc, store,
                anchor_filenames, retry_errored, already,
            )
            if drained is None:
                return  # list_results failed; event already recorded
            already, available_pack_names = drained
            self._cc_maybe_mark_done(job_id, client, store, already, available_pack_names)

        _t.Thread(target=_worker, name=f"cc-resume-{job_id}", daemon=True).start()
        return {"ok": True, "jobId": job_id}

    def cloud_compute_retrieve_results(self, job_id: str) -> dict:
        """Restart the continuous 'download packs as they arrive' loop for an
        existing persisted job — the resumable equivalent of the live download
        path that runs during a fresh submit. Registers the job into the
        in-memory map (so the cloud queue pill repopulates), starts the remote
        poller, then loops ``_cc_drain_packs_once`` every
        ``_CC_RETRIEVE_LOOP_SEC`` until the Worker reports a terminal state,
        marking ``done`` once complete + all packs pulled.

        Idempotent: a second call while a loop is already running for this job
        is a no-op (guarded by ``_cc_retrieve_threads``), so the JS button can
        fire freely without double-downloading."""
        import threading as _t
        with self._ensure_cc_lock():
            existing = self._cc_retrieve_threads.get(job_id)
            if existing is not None and existing.is_alive():
                return {"ok": True, "jobId": job_id, "alreadyRunning": True}
        ctx = self._cc_prepare_resume(job_id)
        if not ctx.get("ok"):
            return ctx
        folder = ctx["folder"]; ccc = ctx["ccc"]; store = ctx["store"]
        target = ctx["target"]
        anchor_filenames = ctx["anchor_filenames"]; retry_errored = ctx["retry_errored"]

        def _loop() -> None:
            import time as _time
            already = self._cc_seed_already(target, folder, job_id)
            # Worker statuses that mean "no more packs will arrive". 'complete'
            # is handled separately: we keep pulling until every listed pack is
            # local (then _cc_maybe_mark_done flips us to 'done'), with a small
            # retry bound so a permanently-failing pack can't spin forever.
            _NO_MORE = {"failed", "cancelled", "incomplete", "error"}
            complete_retries = 0
            while True:
                # Stop promptly if the user cancelled or the job went terminal
                # locally (cloud_compute_cancel_job flips the in-memory status).
                with self._ensure_cc_lock():
                    st = self._cc_jobs.get(job_id)
                    local_status = (st or {}).get("status")
                if local_status in ("done", "failed", "cancelled", "incomplete"):
                    break
                # Re-make the client each pass so a mid-loop JWT refresh doesn't
                # wedge the whole retrieval (the poller does the same).
                client, _client_err = self._cc_make_client()
                if client is None:
                    _time.sleep(_CC_RETRIEVE_LOOP_SEC)
                    continue
                drained = self._cc_drain_packs_once(
                    job_id, folder, client, ccc, store,
                    anchor_filenames, retry_errored, already,
                )
                if drained is not None:
                    already, available_pack_names = drained
                    remote_status = self._cc_maybe_mark_done(
                        job_id, client, store, already, available_pack_names,
                    )
                    if remote_status == "complete":
                        if all(p in already for p in available_pack_names):
                            break  # all in; _cc_maybe_mark_done flipped to done
                        complete_retries += 1
                        if complete_retries >= 3:
                            # Stuck pack(s) after completion — stop spinning;
                            # the account panel's one-shot Download is the
                            # recovery path for the straggler(s).
                            break
                    elif remote_status in _NO_MORE:
                        break
                _time.sleep(_CC_RETRIEVE_LOOP_SEC)

        thread = _t.Thread(target=_loop, name=f"cc-retrieve-{job_id}", daemon=True)
        with self._ensure_cc_lock():
            self._cc_retrieve_threads[job_id] = thread
        thread.start()
        return {"ok": True, "jobId": job_id}

    def _cc_drain_packs_once(
        self, job_id, folder, client, ccc, store,
        anchor_filenames, retry_errored, already,
    ):
        """Download + merge every result pack on the Worker we haven't merged
        yet. Returns ``(already_updated, available_pack_names)`` or ``None`` if
        the initial ``list_results`` call failed (a pack event recording the
        error is appended in that case). Idempotent and safe to call repeatedly
        — used one-shot by ``cloud_compute_resume_download`` and in a loop by
        ``cloud_compute_retrieve_results``."""
        pack_dir = folder / ".kestrel" / "cloud-packs"
        pack_dir.mkdir(parents=True, exist_ok=True)
        try:
            files = client.list_results(job_id)
        except Exception as e:
            with self._ensure_cc_lock():
                self._cc_pack_events.append({
                    "jobId": job_id, "folderPath": str(folder),
                    "packName": None, "error": str(e),
                })
            return None
        # Sanitize Worker-supplied pack names to trusted basenames before any of
        # them is joined to pack_dir. A traversal name (e.g. ../../evil.zip) must
        # never reach the dest.exists()/merge or download_pack paths below — a
        # malicious/compromised Worker could otherwise read or write arbitrary
        # files. _safe_pack_filename rejects separators/parent-refs/drive-colon.
        # The sanitizer is a pure utility of the cloud_compute_client module
        # (not part of the injected behavior tests fake), so fall back to the
        # real module when ``ccc`` is a partial test double.
        safe_name_fn = getattr(ccc, "_safe_pack_filename", None) or \
            self._cc_import()._safe_pack_filename
        available_pack_names = []
        for meta in files:
            raw = str(meta.get("filename") or "")
            if not raw.endswith(".zip"):
                continue
            safe = safe_name_fn(raw)
            if safe is None:
                with self._ensure_cc_lock():
                    self._cc_pack_events.append({
                        "jobId": job_id, "folderPath": str(folder),
                        "packName": None,
                        "error": f"Rejected unsafe pack filename: {raw!r}",
                    })
                continue
            available_pack_names.append(safe)
        # Stale-R2 reconciliation: a pack still in R2 that we've ALREADY merged
        # locally means its server-side results_retrieved flip never landed —
        # e.g. a download GET whose (best-effort) flip was lost when the app was
        # closed mid-download. Do NOT just delete it: the Worker's delete-packs
        # path clears still-`results_available` rows to the terminal,
        # non-billable `results_nuked` state, so the user is never billed for
        # results they actually received and merged. Instead RE-DOWNLOAD it — the
        # GET re-fires the Worker's `results_available → results_retrieved` flip
        # (restoring billing), and the idempotent re-merge (last-wins by filename)
        # guarantees every asset/row made it into the folder. Only THEN does
        # _cc_finalize_pack_merge delete it, safely, now that the rows are
        # retrieved. A pack already gone from R2 won't appear in
        # available_pack_names, so there is nothing left to reconcile.
        stale = [n for n in available_pack_names if n in already]
        if stale and client is not None:
            for fname in stale:
                dest = pack_dir / fname
                try:
                    client.download_pack(job_id, fname, dest)
                    ccc.merge_pack_into_kestrel(
                        dest, folder,
                        protected_filenames=set(anchor_filenames) if anchor_filenames else None,
                        overwrite_errors=retry_errored,
                    )
                except Exception as e:
                    with self._ensure_cc_lock():
                        self._cc_pack_events.append({
                            "jobId": job_id, "folderPath": str(folder),
                            "packName": fname, "error": str(e),
                        })
                    continue
                self._cc_finalize_pack_merge(folder, job_id, fname, dest, client)
            available_pack_names = [n for n in available_pack_names if n not in already]
        for fname in available_pack_names:
            if fname in already:
                continue
            dest = pack_dir / fname
            # Filesystem fallback dedup: if the pack zip is already on disk but
            # missing from `downloadedPacks`, we previously re-downloaded it.
            # That happens when the JSON ledger was killed mid-write
            # (atomic-replace race) or `add_downloaded_pack` swallowed an
            # exception. Re-merging is safe (the database merge is last-wins by
            # filename) and skipping the network call is the whole point —
            # repair the JSON so the next launch isn't confused either.
            if dest.exists() and dest.stat().st_size > 0:
                try:
                    ccc.merge_pack_into_kestrel(
                        dest, folder,
                        protected_filenames=set(anchor_filenames) if anchor_filenames else None,
                        overwrite_errors=retry_errored,
                    )
                except Exception as e:
                    with self._ensure_cc_lock():
                        self._cc_pack_events.append({
                            "jobId": job_id, "folderPath": str(folder),
                            "packName": fname, "error": str(e),
                        })
                    continue
                already.add(fname)
                try:
                    store.add_downloaded_pack(job_id, fname)
                except Exception:
                    pass
                self._cc_finalize_pack_merge(folder, job_id, fname, dest, client)
                with self._ensure_cc_lock():
                    self._cc_pack_events.append({
                        "jobId": job_id, "folderPath": str(folder),
                        "packName": fname,
                    })
                continue
            try:
                client.download_pack(job_id, fname, dest)
                ccc.merge_pack_into_kestrel(
                    dest, folder,
                    protected_filenames=set(anchor_filenames) if anchor_filenames else None,
                    overwrite_errors=retry_errored,
                )
            except Exception as e:
                with self._ensure_cc_lock():
                    self._cc_pack_events.append({
                        "jobId": job_id, "folderPath": str(folder),
                        "packName": fname, "error": str(e),
                    })
                continue
            already.add(fname)
            try:
                store.add_downloaded_pack(job_id, fname)
            except Exception:
                pass
            self._cc_finalize_pack_merge(folder, job_id, fname, dest, client)
            with self._ensure_cc_lock():
                self._cc_pack_events.append({
                    "jobId": job_id, "folderPath": str(folder),
                    "packName": fname,
                })
        return already, available_pack_names

    def _cc_maybe_mark_done(self, job_id, client, store, already, available_pack_names):
        """Flip the job to ``done`` only when the Worker confirms
        terminal-complete state AND every available pack has been pulled
        locally. Otherwise leave the persisted status untouched — the live
        poller (and subsequent app launches) keep observing reality. Returns
        the Worker's reported status string (``""`` on query failure)."""
        try:
            remote = client.get_status(job_id)
            remote_status = str(remote.get("status") or "")
        except Exception:
            remote_status = ""
        try:
            if (
                remote_status == "complete"
                and all(p in already for p in available_pack_names)
            ):
                store.update_job(job_id, status="done")
                with self._ensure_cc_lock():
                    st = self._cc_jobs.get(job_id)
                    if st is not None:
                        st["status"] = "done"
        except Exception:
            pass
        return remote_status

    def cloud_compute_relocate_job(self, job_id: str, new_folder_path: str) -> dict:
        """§4d — re-point a persisted job's ``folderPath`` to a new location.

        Used by the account panel's "Locate folder…" recovery path when the
        original analyzed folder has moved (drive-letter change, folder
        renamed/moved, external drive remounted elsewhere). After relocation
        ``folderAvailable`` flips true on the next ``list_pending_jobs`` and the
        "Download results" button re-enables; ``resume_download`` then writes
        into the new location's ``.kestrel/``.

        Validation: the chosen dir must pass ``_validate_root_dir`` (normalised,
        inside the allowed root if one is configured, and an existing
        directory). We deliberately DON'T hard-require that the folder "looks
        like" the original — the pack merge is filename-keyed and idempotent, so
        a wrong pick simply merges into the wrong folder (recoverable) rather
        than corrupting anything. The picker UX already nudges the user to the
        right place.
        """
        job_id = (job_id or "").strip()
        if not job_id:
            return {"ok": False, "error": "missing jobId"}
        new_root, err = self._validate_root_dir(
            new_folder_path, "cloud_compute_relocate_job", require_exists=True
        )
        if err:
            return {"ok": False, "error": err}
        try:
            store = self._cc_jobs_store()
            target = next((j for j in store.load_jobs() if j["jobId"] == job_id), None)
        except Exception as e:
            return {"ok": False, "error": f"store load failed: {e}"}
        if target is None:
            return {"ok": False, "error": "unknown jobId"}
        try:
            updated = store.update_job(job_id, folderPath=new_root)
        except Exception as e:
            return {"ok": False, "error": f"store update failed: {e}"}
        if updated is None:
            return {"ok": False, "error": "update failed"}
        # Keep an already-registered in-memory entry (e.g. a failed/incomplete
        # job surfaced in _cc_jobs) pointed at the new location too, so a
        # subsequent resume_download in this same session targets the new path.
        with self._ensure_cc_lock():
            st = self._cc_jobs.get(job_id)
            if st is not None:
                st["rootPath"] = new_root
        return {"ok": True, "jobId": job_id, "folderPath": new_root}

    def cloud_compute_get_pack_events(self) -> dict:
        """Drain pack-merged events accumulated since the last call. JS calls
        this on its cloud-queue poll tick and triggers a folder rescan +
        gallery refresh for any ``folderPath`` mentioned."""
        with self._ensure_cc_lock():
            events = self._cc_pack_events
            self._cc_pack_events = []
        return {"ok": True, "events": events}

    def cloud_compute_get_usage(self) -> dict:
        """Cached fetch of ``/api/usage``. 5-minute TTL. Used by the Cloud
        destination card to display ``Remaining cloud analysis images: N``.
        Stub-shaped today (Stage 3 fleshes it out)."""
        import time as _t
        now = _t.time()
        if self._cc_usage_cache is not None and (now - self._cc_usage_cache_at) < 300:
            return {"ok": True, "usage": self._cc_usage_cache, "cached": True}
        client, client_err = self._cc_make_client()
        if client is None:
            return client_err or {"ok": False, "error": "no client"}
        try:
            usage = client.get_usage()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        self._cc_usage_cache = usage
        self._cc_usage_cache_at = now
        return {"ok": True, "usage": usage, "cached": False}

    def share_with_perch(
        self,
        root_path: str,
        excluded_scene_ids=None,
        skip_rejected: bool = True,
        idempotency_key: str | None = None,
    ) -> dict:
        """Kick off an async upload. Returns immediately with `{job_id}`.

        JS polls `get_share_progress(job_id)` for live state. The browser is NOT
        opened automatically — the user clicks an "Open in browser" button on
        the success card so the auto-redirect-during-work pattern is gone.

        ``skip_rejected``: when True (default), CSV rows marked culled are
        omitted from the upload. The dialog defaults this to True; the user
        can uncheck "Skip rejected photos" in the dialog to override.

        ``idempotency_key``: optional — if a previous call crashed between
        receiving the perch id and the success response, the JS layer can
        replay with the same key to dedupe. Server matches on
        ``(owner_id, idempotency_key)`` and returns the same perch row.
        """
        try:
            from perch_uploader import (
                PerchKestrelUploader,
                PerchLegalAcceptanceRequired,
                PerchPlanLimitExceeded,
            )
        except ImportError:
            try:
                from analyzer.perch_uploader import (
                    PerchKestrelUploader,
                    PerchLegalAcceptanceRequired,
                    PerchPlanLimitExceeded,
                )
            except ImportError as e:
                return {"success": False, "error": f"uploader import failed: {e}"}

        token, dev_user, err = self._check_auth_token()
        if err:
            return err

        root_real, verr = self._validate_root_dir(
            root_path, context="share_with_perch", require_exists=True
        )
        if verr:
            return {"success": False, "error": verr}

        lock = self._ensure_share_lock()
        with lock:
            if self._active_share_job is not None:
                return {
                    "success": False,
                    "error": "already_running",
                    "active_job_id": self._active_share_job,
                }
            import threading as _t
            import uuid as _uuid
            job_id = str(_uuid.uuid4())
            cancel_event = _t.Event()
            job_state = {
                "progress": {"phase": "starting"},
                "cancel_event": cancel_event,
                "thread": None,
            }
            self._share_jobs[job_id] = job_state
            self._active_share_job = job_id

        excluded = list(excluded_scene_ids or [])

        def _on_progress(payload: dict) -> None:
            with lock:
                if job_id in self._share_jobs:
                    self._share_jobs[job_id]["progress"] = dict(payload)

        def _runner() -> None:
            try:
                uploader = PerchKestrelUploader(
                    self.get_perch_api_base(),
                    str(token) if token else None,
                    dev_user=dev_user,
                )
                result = uploader.run(
                    str(root_real),
                    excluded_scene_ids=excluded,
                    progress_callback=_on_progress,
                    cancel_event=cancel_event,
                    skip_rejected=bool(skip_rejected),
                    idempotency_key=(str(idempotency_key) if idempotency_key else None),
                )
                # Persist `.kestrel/perch_link.json` only on a fully-successful
                # upload. On cancel, the partial perch lives on the server and
                # the user must clear it via the canceled-state UI; we don't
                # want a stale "Published" badge claiming success.
                if result and not result.get("canceled"):
                    try:
                        self._write_perch_link(
                            str(root_real),
                            result,
                            skip_rejected=bool(skip_rejected),
                            preflight=getattr(uploader, "_cached_preflight", None),
                        )
                    except Exception as link_err:
                        log(f"share_with_perch: perch_link.json write failed: {link_err}")
            except PerchLegalAcceptanceRequired as e:
                # Launch item #13: open the browser for ToS / Privacy re-acceptance
                # and surface a structured progress payload so the JS side can
                # render a "Review updated terms" card with a fallback link
                # if the browser launch failed.
                try:
                    webbrowser.open(e.accept_url, new=2)
                except Exception as _e:
                    warn(f"share_with_perch: failed to open legal accept URL: {_e}")
                _on_progress({
                    "phase": "error",
                    "message": "legal_acceptance_required",
                    "acceptUrl": e.accept_url,
                    "currentEffectiveDate": e.current_effective_date,
                })
            except PerchPlanLimitExceeded as e:
                # Stage 7: plan-tier cap denial. Surface a typed error so JS
                # renders an upgrade card with a clickable "Upgrade" button
                # to myaccount.projectkestrel.org/perch. The presigning
                # spinner unwinds because we end the job here.
                _on_progress({
                    "phase": "error",
                    "message": "plan_limit_exceeded",
                    "errorCode": e.error_code,
                    "status": e.status,
                    "tier": e.tier,
                    "current": e.current,
                    "limit": e.limit,
                    "filename": e.filename,
                    "upgradeUrl": e.upgrade_url,
                    "friendlyMessage": str(e),
                })
            except Exception as e:
                log(f"share_with_perch: {e}")
                import traceback as _tb
                log(_tb.format_exc())
                _on_progress({"phase": "error", "message": str(e)})
            finally:
                with lock:
                    if self._active_share_job == job_id:
                        self._active_share_job = None
                # Invalidate usage cache so the next dialog open shows fresh numbers.
                self._perch_usage_cache = None
                self._perch_usage_cache_at = 0.0

        import threading as _t
        thread = _t.Thread(target=_runner, name=f"PerchUpload-{job_id[:8]}", daemon=True)
        with lock:
            self._share_jobs[job_id]["thread"] = thread
        thread.start()

        return {"success": True, "job_id": job_id}

    def get_share_progress(self, job_id: str) -> dict:
        """Return the latest progress event for an in-flight or recent share job."""
        lock = self._ensure_share_lock()
        with lock:
            entry = self._share_jobs.get(str(job_id))
            if entry is None:
                return {"success": False, "error": "not_found"}
            return {"success": True, "progress": dict(entry.get("progress") or {})}

    def cancel_share(self, job_id: str) -> dict:
        """Request cancellation of an in-flight share job. Idempotent."""
        lock = self._ensure_share_lock()
        with lock:
            entry = self._share_jobs.get(str(job_id))
            if entry is None:
                return {"success": False, "error": "not_found"}
            ev = entry.get("cancel_event")
        if ev is not None:
            try:
                ev.set()
            except Exception:
                pass
        return {"success": True}

    def open_perch_url(self, url: str) -> dict:
        """Open an arbitrary URL in the user's default browser."""
        try:
            if not isinstance(url, str) or not url.strip():
                return {"success": False, "error": "missing url"}
            u = url.strip()
            if not (u.startswith("http://") or u.startswith("https://")):
                return {"success": False, "error": "invalid url scheme"}
            webbrowser.open(u, new=2, autoraise=True)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ─── Perch link persistence (Phase 1: per-folder perch_link.json) ──

    @staticmethod
    def _perch_link_path(folder_path: str) -> "Path":
        from pathlib import Path as _P
        return _P(folder_path) / ".kestrel" / "perch_link.json"

    @staticmethod
    def _hash_kestrel_state(folder_path: str) -> str | None:
        """SHA-256 over kestrel_database.csv + kestrel_scenedata.json contents.

        Returned as ``"sha256:<hex>"`` or None if neither file is present. Used
        as a "did anything change since upload?" gate by Phase 3 sync — covers
        both row-level edits and scene-renames.
        """
        import hashlib
        from pathlib import Path as _P
        kestrel_dir = _P(folder_path) / ".kestrel"
        h = hashlib.sha256()
        any_read = False
        for name in ("kestrel_database.csv", "kestrel_scenedata.json"):
            fp = kestrel_dir / name
            if fp.is_file():
                try:
                    with open(fp, "rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            h.update(chunk)
                    any_read = True
                except OSError:
                    pass
        return ("sha256:" + h.hexdigest()) if any_read else None

    def _write_perch_link(
        self,
        folder_path: str,
        run_result: dict,
        skip_rejected: bool,
        preflight=None,
    ) -> None:
        """Persist `.kestrel/perch_link.json` after a successful upload."""
        from pathlib import Path as _P
        import json as _json
        import time as _time
        link_path = self._perch_link_path(folder_path)
        link_path.parent.mkdir(parents=True, exist_ok=True)
        title = _P(folder_path).name or ""
        payload = {
            "version": 1,
            "perch_id": str(run_result.get("perch_id") or ""),
            "perch_url": str(run_result.get("url") or ""),
            "title": title,
            "uploaded_at_ms": int(_time.time() * 1000),
            "scene_count": int(run_result.get("scene_count") or 0),
            "asset_count": int(getattr(preflight, "file_count", 0) or 0),
            "image_count": int(getattr(preflight, "image_count", 0) or 0),
            "skip_rejected_used": bool(skip_rejected),
            "state_hash_at_upload": self._hash_kestrel_state(folder_path),
        }
        tmp = link_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(payload, f, indent=2)
        os.replace(tmp, link_path)

    def read_perch_link(self, folder_path: str) -> dict:
        """Read .kestrel/perch_link.json. Returns {present, link} or {present: False}."""
        root_real, err = self._validate_root_dir(folder_path, context="read_perch_link", require_exists=True)
        if err:
            return {"present": False, "error": err}
        link_path = self._perch_link_path(str(root_real))
        if not link_path.is_file():
            return {"present": False}
        try:
            import json as _json
            with open(link_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            return {"present": True, "link": data}
        except Exception as e:
            return {"present": False, "error": str(e)}

    def relink_perch(self, folder_path: str, perch_id: str) -> dict:
        """Re-associate a local folder with an EXISTING perch by writing
        `.kestrel/perch_link.json` — no re-upload.

        For folders that were shared previously but lost their local link (the
        `.kestrel` folder was deleted, the folder was moved/copied to another
        machine, etc.). Confirms the perch exists and is owned by the caller via
        GET /v1/perches/{id} (through get_perch_status), then persists a link
        payload shaped like the post-upload one so the linked view + "On Perch"
        button light up exactly as a fresh upload would.

        Returns {"success": True, "link": <payload>} or {"success": False,
        "error": ...} (error mirrors get_perch_status: not_found / unauthorized /
        forbidden / no_auth / unreachable …)."""
        root_real, err = self._validate_root_dir(
            folder_path, context="relink_perch", require_exists=True
        )
        if err:
            return {"success": False, "error": err}
        pid = str(perch_id or "").strip()
        if not pid:
            return {"success": False, "error": "missing_perch_id"}

        status = self.get_perch_status(pid)
        if not status.get("ok"):
            return {"success": False, "error": status.get("error") or "lookup_failed"}
        st = status.get("status") or {}

        from pathlib import Path as _P
        import json as _json
        import time as _time

        created = st.get("createdAt")
        uploaded_ms = (
            int(float(created) * 1000)
            if isinstance(created, (int, float)) and created
            else int(_time.time() * 1000)
        )
        payload = {
            "version": 1,
            "perch_id": pid,
            "perch_url": str(st.get("publicUrl") or ""),
            "title": str(st.get("title") or _P(str(root_real)).name or ""),
            "uploaded_at_ms": uploaded_ms,
            # scene_count isn't exposed by the perch detail endpoint; left 0.
            # The linked view tolerates 0 (it shows asset/image counts).
            "scene_count": 0,
            "asset_count": int(st.get("assetCount") or 0),
            "image_count": int(st.get("imageCount") or 0),
            "skip_rejected_used": False,
            "state_hash_at_upload": self._hash_kestrel_state(str(root_real)),
            # Marker so a later reader can tell this link was reconstructed
            # rather than written by a fresh upload (e.g. to soften a tag-sync
            # state-hash mismatch warning, which is expected after a re-link).
            "relinked": True,
        }
        link_path = self._perch_link_path(str(root_real))
        try:
            link_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = link_path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(payload, f, indent=2)
            os.replace(tmp, link_path)
        except Exception as e:
            return {"success": False, "error": str(e)}
        return {"success": True, "link": payload}

    def delete_perch_link(self, folder_path: str) -> dict:
        """Delete .kestrel/perch_link.json (local only; does not touch Worker)."""
        root_real, err = self._validate_root_dir(folder_path, context="delete_perch_link", require_exists=True)
        if err:
            return {"success": False, "error": err}
        link_path = self._perch_link_path(str(root_real))
        if not link_path.is_file():
            return {"success": True, "removed": False}
        try:
            link_path.unlink()
            return {"success": True, "removed": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def verify_perch_link(self, folder_path: str) -> dict:
        """Check whether the perch a folder is linked to still exists on the server.

        Returns one of:
          {"status": "missing"}          — no perch_link.json present
          {"status": "alive", ...}       — server returned 200, link is valid
          {"status": "deleted", ...}     — server returned 404, local file removed
          {"status": "unauthorized"}     — 401, user signed out (link untouched)
          {"status": "forbidden"}        — 403, link owned by another account (untouched)
          {"status": "unreachable", ...} — network error (link untouched)

        Only a definite 404 clears local state. 401/403/network errors are
        treated as transient and never cause a destructive cleanup.
        """
        root_real, err = self._validate_root_dir(
            folder_path, context="verify_perch_link", require_exists=True
        )
        if err:
            return {"status": "missing", "error": err}
        link_path = self._perch_link_path(str(root_real))
        if not link_path.is_file():
            return {"status": "missing"}
        try:
            import json as _json
            with open(link_path, "r", encoding="utf-8") as f:
                link = _json.load(f)
        except Exception as e:
            return {"status": "missing", "error": f"link unreadable: {e}"}
        perch_id = str((link or {}).get("perch_id") or "").strip()
        if not perch_id:
            return {"status": "missing", "error": "link has no perch_id"}

        token, dev_user, terr = self._check_auth_token()
        if terr:
            # No usable auth — treat as unauthorized; do NOT clear the link.
            return {"status": "unauthorized", "perch_id": perch_id, "link": link}
        try:
            import requests as _req
            headers: dict = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            if dev_user:
                headers["x-dev-user-id"] = str(dev_user)
            r = _req.get(
                f"{self.get_perch_api_base()}/v1/perches/{perch_id}",
                headers=headers,
                timeout=15,
            )
        except Exception as e:
            return {"status": "unreachable", "error": str(e), "perch_id": perch_id, "link": link}

        if r.status_code == 200:
            return {"status": "alive", "perch_id": perch_id, "link": link}
        if r.status_code == 404:
            # Definite delete — clear local link.
            try:
                link_path.unlink()
            except OSError:
                pass
            return {"status": "deleted", "perch_id": perch_id, "cleared_local": True}
        if r.status_code == 401:
            return {"status": "unauthorized", "perch_id": perch_id, "link": link}
        if r.status_code == 403:
            return {"status": "forbidden", "perch_id": perch_id, "link": link}
        # Anything else (5xx, etc.) — transient; leave local state alone.
        return {
            "status": "unreachable",
            "error": f"HTTP {r.status_code}",
            "perch_id": perch_id,
            "link": link,
        }

    def get_perch_status(self, perch_id: str) -> dict:
        """Fetch live status of one perch from the Perch Worker.

        Hits two endpoints sequentially with the same auth header:
          - GET /v1/perches/{id} — visibility, publicSlug, commentsPermission,
            publishedAt, owner. Treat 404 as "perch deleted on server".
          - GET /v1/me/perches — lightweight list. Find entry where id matches
            to pick up actualBytes + assetCount + imageCount.

        Returns:
          {"ok": True, "status": {
              "title", "visibility", "status" ("draft"|"published"),
              "publicUrl", "commentsPermission",
              "publishedAt" (unix seconds or None), "createdAt" (unix seconds),
              "actualBytes", "imageCount", "assetCount", "uploadState",
          }}
        On failure:
          {"ok": False, "error": "not_found" | "unauthorized" | "forbidden" |
                                  "unreachable" | "no_auth"}

        Callers treat this as advisory — the local perch_link.json is still
        authoritative for whether the dialog is in linked state. On
        "not_found" the caller is expected to clear the link locally.
        """
        pid = str(perch_id or "").strip()
        if not pid:
            return {"ok": False, "error": "missing_id"}

        token, dev_user, terr = self._check_auth_token()
        if terr:
            return {"ok": False, "error": "no_auth"}

        try:
            import requests as _req
        except Exception as e:
            return {"ok": False, "error": f"requests_unavailable: {e}"}

        headers: dict = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if dev_user:
            headers["x-dev-user-id"] = str(dev_user)
        base = self.get_perch_api_base()

        try:
            r_detail = _req.get(f"{base}/v1/perches/{pid}", headers=headers, timeout=15)
        except Exception as e:
            return {"ok": False, "error": "unreachable", "detail": str(e)}
        if r_detail.status_code == 404:
            return {"ok": False, "error": "not_found"}
        if r_detail.status_code == 401:
            return {"ok": False, "error": "unauthorized"}
        if r_detail.status_code == 403:
            return {"ok": False, "error": "forbidden"}
        if r_detail.status_code != 200:
            return {"ok": False, "error": f"http_{r_detail.status_code}"}
        try:
            detail = r_detail.json()
        except Exception as e:
            return {"ok": False, "error": f"bad_json: {e}"}

        perch_obj = detail.get("perch") if isinstance(detail, dict) else None
        if not isinstance(perch_obj, dict):
            # Shape mismatch — surface as unreachable so UI shows cached info.
            return {"ok": False, "error": "bad_shape"}

        # Second call: lightweight list for byte counts. Don't fail the whole
        # response if this 500s or 401s — we still got the detail; show what
        # we have and let bytes show "—".
        actual_bytes = None
        asset_count = None
        image_count = None
        upload_state = None
        try:
            r_list = _req.get(f"{base}/v1/me/perches", headers=headers, timeout=15)
            if r_list.status_code == 200:
                list_payload = r_list.json()
                items = list_payload.get("perches") if isinstance(list_payload, dict) else None
                if not isinstance(items, list):
                    items = list_payload if isinstance(list_payload, list) else []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    if str(it.get("id") or "") == pid:
                        actual_bytes = it.get("actualBytes")
                        asset_count = it.get("assetCount")
                        image_count = it.get("imageCount")
                        upload_state = it.get("uploadState")
                        break
        except Exception:
            pass  # advisory only

        return {
            "ok": True,
            "status": {
                "title": perch_obj.get("title"),
                "visibility": perch_obj.get("visibility"),
                "status": perch_obj.get("status"),
                "publicSlug": perch_obj.get("publicSlug"),
                "publicUrl": perch_obj.get("publicUrl"),
                "commentsPermission": perch_obj.get("commentsPermission"),
                "publishedAt": perch_obj.get("publishedAt"),
                "createdAt": perch_obj.get("createdAt"),
                "actualBytes": actual_bytes,
                "imageCount": image_count,
                "assetCount": asset_count,
                "uploadState": upload_state,
            },
        }

    # ─── Tag sync (H7): push species/family corrections to a linked perch ──
    #
    # When the user fixes species/family names on a *linked* folder, push just
    # those scene-level corrections to the existing perch via the batch route
    # PATCH /v1/perches/{id}/scenes — no file re-upload (tags are manifest
    # metadata). Scenes are matched by `kestrelSceneId` (the desktop scene id,
    # which is the CSV `scene_count` string, persisted into the manifest at
    # upload time). V1 is metadata-only: no added/deleted scenes or files.

    @staticmethod
    def _read_link_dict(folder_path: str) -> "dict | None":
        """Read .kestrel/perch_link.json as a dict, or None if absent/unreadable."""
        from pathlib import Path as _P
        link_path = _P(folder_path) / ".kestrel" / "perch_link.json"
        if not link_path.is_file():
            return None
        try:
            import json as _json
            with open(link_path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    @staticmethod
    def _read_scenedata_dict(folder_path: str) -> dict:
        """Read .kestrel/kestrel_scenedata.json as a dict (empty dict on absence/error)."""
        from pathlib import Path as _P
        sp = _P(folder_path) / ".kestrel" / "kestrel_scenedata.json"
        if not sp.is_file():
            return {}
        try:
            import json as _json
            with open(sp, "r", encoding="utf-8") as f:
                data = _json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _norm_tag_list(v) -> list:
        """Normalize a tag list the same way the uploader/worker do: stringify,
        strip, drop empties, preserve order. Makes a freshly-synced scene
        re-diff to 'no change' against the manifest."""
        if not isinstance(v, list):
            return []
        return [str(x).strip() for x in v if str(x).strip()]

    def _perch_tag_changeset(self, folder_path: str, perch_id: str):
        """Diff local user_tags against the live perch manifest, matched by
        kestrelSceneId. Returns (changes, None) on success or (None, error_code).

        Each change: {kestrelSceneId, title, species, family, remoteSpecies,
        remoteFamily} where species/family are the *local* (desired) lists.
        Only scenes present in BOTH the local scenedata and the remote manifest
        are considered (V1 is metadata-only — no add/delete of scenes).
        """
        token, dev_user, terr = self._check_auth_token()
        if terr:
            return None, "no_auth"
        try:
            import requests as _req
        except Exception as e:
            return None, f"requests_unavailable: {e}"

        headers: dict = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if dev_user:
            headers["x-dev-user-id"] = str(dev_user)
        try:
            r = _req.get(
                f"{self.get_perch_api_base()}/v1/perches/{perch_id}",
                headers=headers,
                timeout=15,
            )
        except Exception as e:
            return None, "unreachable"
        if r.status_code == 404:
            return None, "not_found"
        if r.status_code == 401:
            return None, "unauthorized"
        if r.status_code == 403:
            return None, "forbidden"
        if r.status_code != 200:
            return None, f"http_{r.status_code}"
        try:
            detail = r.json()
        except Exception:
            return None, "bad_json"
        remote_scenes = detail.get("scenes") if isinstance(detail, dict) else None
        if not isinstance(remote_scenes, list):
            return None, "bad_shape"

        scenedata = self._read_scenedata_dict(folder_path)
        local_scenes = scenedata.get("scenes") if isinstance(scenedata, dict) else {}
        local_scenes = local_scenes if isinstance(local_scenes, dict) else {}
        return self._diff_perch_tags(remote_scenes, local_scenes), None

    @classmethod
    def _diff_perch_tags(cls, remote_scenes, local_scenes) -> list:
        """Pure changeset: remote manifest scenes vs local scenedata, matched by
        kestrelSceneId. Returns scenes whose local species/family differ from
        remote. Only scenes present in BOTH sides are considered (V1 is
        metadata-only — no add/delete). Extracted from the network path so it's
        unit-testable.
        """
        local_scenes = local_scenes if isinstance(local_scenes, dict) else {}
        changes: list = []
        for rs in remote_scenes if isinstance(remote_scenes, list) else []:
            if not isinstance(rs, dict):
                continue
            ksid_raw = rs.get("kestrelSceneId")
            if ksid_raw is None:
                continue
            ksid = str(ksid_raw)
            local = local_scenes.get(ksid)
            if not isinstance(local, dict):
                continue  # scene not in local scenedata — skip (no add/delete in V1)
            ut = local.get("user_tags")
            ut = ut if isinstance(ut, dict) else {}
            local_species = cls._norm_tag_list(ut.get("species"))
            local_family = cls._norm_tag_list(ut.get("families"))
            remote_species = cls._norm_tag_list(rs.get("speciesList"))
            remote_family = cls._norm_tag_list(rs.get("familyList"))
            if local_species == remote_species and local_family == remote_family:
                continue
            title = ""
            nm = local.get("name")
            if isinstance(nm, str) and nm.strip():
                title = nm.strip()
            if not title:
                title = f"Scene {ksid}"
            changes.append({
                "kestrelSceneId": ksid,
                "title": title,
                "species": local_species,
                "family": local_family,
                "remoteSpecies": remote_species,
                "remoteFamily": remote_family,
            })
        return changes

    def _update_link_state_hash(self, folder_path: str) -> None:
        """Re-stamp perch_link.json's state_hash_at_upload to the current state,
        so the cheap 'anything changed?' gate reports in-sync after a sync."""
        import json as _json
        link_path = self._perch_link_path(folder_path)
        if not link_path.is_file():
            return
        with open(link_path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        if not isinstance(data, dict):
            return
        data["state_hash_at_upload"] = self._hash_kestrel_state(folder_path)
        tmp = link_path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(data, f, indent=2)
        os.replace(tmp, link_path)

    def compute_perch_tag_diff(self, folder_path: str) -> dict:
        """Compute the species/family changeset for a linked folder vs its perch.

        Returns:
          {"ok": True, "perch_id", "count", "changes": [...], "unchanged"?: bool}
          {"ok": False, "error": "not_linked"|"no_auth"|"unauthorized"|
                                  "forbidden"|"not_found"|"unreachable"|...}

        Uses the coarse perch_link.json `state_hash_at_upload` as a cheap gate:
        if nothing on disk changed since upload, there can be no tag diff, so it
        returns count 0 without a network round-trip. Otherwise it does the
        precise per-scene manifest comparison.
        """
        root_real, err = self._validate_root_dir(
            folder_path, context="compute_perch_tag_diff", require_exists=True
        )
        if err:
            return {"ok": False, "error": err}
        link = self._read_link_dict(str(root_real))
        if not link:
            return {"ok": False, "error": "not_linked"}
        perch_id = str(link.get("perch_id") or "").strip()
        if not perch_id:
            return {"ok": False, "error": "not_linked"}

        prev_hash = str(link.get("state_hash_at_upload") or "")
        cur_hash = self._hash_kestrel_state(str(root_real)) or ""
        if prev_hash and cur_hash and prev_hash == cur_hash:
            return {"ok": True, "perch_id": perch_id, "count": 0, "changes": [], "unchanged": True}

        changes, derr = self._perch_tag_changeset(str(root_real), perch_id)
        if derr is not None:
            return {"ok": False, "error": derr, "perch_id": perch_id}
        return {"ok": True, "perch_id": perch_id, "count": len(changes), "changes": changes}

    def sync_perch_tags(self, folder_path: str) -> dict:
        """Push the linked folder's species/family corrections to its perch.

        Recomputes the changeset internally (never trusts a client-supplied one,
        avoiding a TOCTOU on stale data), PATCHes the batch route, and on success
        re-stamps perch_link.json's state hash.

        Returns:
          {"ok": True, "perch_id", "updated": int, "skipped": [...], "count": int,
           "nothing_to_sync"?: bool}
          {"ok": False, "error": ...}
        """
        root_real, err = self._validate_root_dir(
            folder_path, context="sync_perch_tags", require_exists=True
        )
        if err:
            return {"ok": False, "error": err}
        link = self._read_link_dict(str(root_real))
        if not link:
            return {"ok": False, "error": "not_linked"}
        perch_id = str(link.get("perch_id") or "").strip()
        if not perch_id:
            return {"ok": False, "error": "not_linked"}

        changes, derr = self._perch_tag_changeset(str(root_real), perch_id)
        if derr is not None:
            return {"ok": False, "error": derr, "perch_id": perch_id}
        if not changes:
            # Already in sync — still re-stamp the hash so the next probe is cheap.
            try:
                self._update_link_state_hash(str(root_real))
            except Exception:
                pass
            return {
                "ok": True, "perch_id": perch_id, "updated": 0,
                "skipped": [], "count": 0, "nothing_to_sync": True,
            }

        token, dev_user, terr = self._check_auth_token()
        if terr:
            return {"ok": False, "error": "no_auth", "perch_id": perch_id}
        try:
            import requests as _req
        except Exception as e:
            return {"ok": False, "error": f"requests_unavailable: {e}"}

        headers: dict = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if dev_user:
            headers["x-dev-user-id"] = str(dev_user)
        payload = {
            "scenes": [
                {
                    "kestrelSceneId": c["kestrelSceneId"],
                    "species": c["species"],
                    "family": c["family"],
                }
                for c in changes
            ]
        }
        try:
            r = _req.patch(
                f"{self.get_perch_api_base()}/v1/perches/{perch_id}/scenes",
                json=payload,
                headers=headers,
                timeout=30,
            )
        except Exception as e:
            return {"ok": False, "error": "unreachable", "detail": str(e), "perch_id": perch_id}
        if r.status_code == 404:
            return {"ok": False, "error": "not_found", "perch_id": perch_id}
        if r.status_code == 401:
            return {"ok": False, "error": "unauthorized", "perch_id": perch_id}
        if r.status_code == 403:
            return {"ok": False, "error": "forbidden", "perch_id": perch_id}
        if r.status_code != 200:
            msg = ""
            try:
                body = r.json()
                if isinstance(body, dict):
                    msg = str(body.get("error") or "")
            except Exception:
                pass
            return {"ok": False, "error": msg or f"http_{r.status_code}", "perch_id": perch_id}
        try:
            body = r.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        updated = int(body.get("updated") or 0)
        skipped = body.get("skipped") if isinstance(body.get("skipped"), list) else []

        # Re-stamp the state hash so a subsequent compute_perch_tag_diff short-
        # circuits to "in sync" via the cheap gate.
        try:
            self._update_link_state_hash(str(root_real))
        except Exception as e:
            log(f"sync_perch_tags: state hash update failed: {e}")

        return {
            "ok": True, "perch_id": perch_id, "updated": updated,
            "skipped": skipped, "count": len(changes),
        }

    # ── OAuth 2.0 + PKCE sign-in / refresh / sign-out ────────────────────

    def start_oauth_sign_in(self):
        """Start a Clerk OAuth Authorization Code + PKCE flow in a background thread.

        Returns immediately. On success the worker thread persists the OAuth
        bundle to the keychain and calls ``window.onAuthSignIn(token)``; on
        failure it calls ``window.onAuthSignInFailed({error, description})``.
        Refuses to start a second concurrent flow. This is the loopback (Win/Linux)
        / ASWebAuthenticationSession (macOS) transport used for Google + email.
        """
        return self._begin_sign_in(None)

    def start_apple_native_sign_in(self):
        """Start native "Sign in with Apple" (ASAuthorizationController).

        macOS App Store build only. Same success/failure contract as
        ``start_oauth_sign_in`` — persists the same OAuth token bundle and calls
        ``window.onAuthSignIn`` / ``window.onAuthSignInFailed`` — but the
        credential is captured in Apple's native system sheet (Guideline 4),
        then bridged to a Clerk session and the standard OAuth token bundle.
        """
        if _oauth is None:
            return {"success": False, "error": "oauth_module_unavailable"}
        try:
            apple = _oauth._load_apple_signin()
        except Exception:
            apple = None
        if apple is None:
            return {"success": False, "error": "apple_unavailable"}
        return self._begin_sign_in(self._apple_flow)

    def _apple_flow(self, cancel_event):
        """Worker flow-callable: native Apple credential → Clerk → token bundle.

        Returns the same ``{"ok": bool, "bundle"|"error": ...}`` contract as
        ``oauth_client.run_authorization_flow`` so ``_oauth_worker`` handles it
        identically.
        """
        apple = _oauth._load_apple_signin()
        if apple is None:
            return {
                "ok": False,
                "error": "apple_unavailable",
                "error_description": "Native Sign in with Apple is unavailable on this build.",
            }
        return _oauth.run_apple_native_flow(
            apple,
            progress_cb=self._oauth_progress_cb,
            url_validator=_is_safe_external_url,
            cancel_event=cancel_event,
        )

    def _begin_sign_in(self, flow_fn=None):
        """Shared launcher for the sign-in transports.

        ``flow_fn`` is ``None`` for the standard OAuth/loopback/ASWeb flow, or a
        ``(cancel_event) -> result`` callable (native Apple). Refuses to start a
        second concurrent flow; a stale in-flight flow is cancelled and reaped so
        this click can start fresh.
        """
        if _oauth is None:
            return {"success": False, "error": "oauth_module_unavailable"}
        try:
            import threading as _t
            lock = self._get_oauth_lock()
            if not lock.acquire(timeout=5.0):
                return {"success": False, "error": "flow_in_progress"}
            try:
                # An in-flight flow here almost always means a previous attempt
                # was abandoned (the user closed the browser tab without
                # finishing). Rather than dead-ending until the 5-minute
                # callback timeout (or an app restart), cancel the stale flow
                # and reclaim the slot so this click can start fresh.
                prior_cancel = self._oauth_cancel_event if self._oauth_in_flight else None
                prior_thread = self._oauth_thread if self._oauth_in_flight else None
            finally:
                lock.release()

            # Tear down the stale flow OUTSIDE the lock — joining can take a
            # moment while its loopback callback server unwinds and frees the
            # port we are about to rebind.
            if prior_cancel is not None:
                prior_cancel.set()
            if prior_thread is not None and prior_thread.is_alive():
                prior_thread.join(timeout=8.0)
                if prior_thread.is_alive():
                    # Couldn't reclaim the port in time — report rather than
                    # corrupt state by starting a second flow on the same port.
                    return {"success": False, "error": "flow_in_progress"}

            if not lock.acquire(timeout=5.0):
                return {"success": False, "error": "flow_in_progress"}
            try:
                # The reaped worker's finally clears these; clear defensively in
                # case it lost the timed join above.
                cancel_event = _t.Event()
                self._oauth_cancel_event = cancel_event
                self._oauth_in_flight = True
                self._oauth_status = "starting"
                thread = _t.Thread(
                    target=self._oauth_worker, args=(cancel_event,),
                    kwargs={"flow_fn": flow_fn},
                    name="oauth-flow", daemon=True,
                )
                self._oauth_thread = thread
            finally:
                lock.release()

            thread.start()
            return {"success": True, "started": True}
        except Exception as e:
            self._oauth_in_flight = False
            self._oauth_status = "idle"
            self._oauth_cancel_event = None
            self._oauth_thread = None
            print(f"[API] _begin_sign_in() -> Error: {e}", flush=True)
            return {"success": False, "error": str(e)}

    def _oauth_progress_cb(self, label: str) -> None:
        self._oauth_status = str(label)

    def _oauth_worker(self, cancel_event=None, flow_fn=None) -> None:
        """Background thread that drives a sign-in flow to completion.

        ``flow_fn`` is ``None`` for the standard OAuth transport
        (``oauth_client.run_authorization_flow``) or a ``(cancel_event) -> result``
        callable (native Apple). Either way the result is the same
        ``{"ok": ...}`` contract. Persists the result on success and notifies JS
        either way. Always clears ``_oauth_in_flight`` in ``finally`` so a botched
        flow doesn't permanently block re-attempts. ``cancel_event`` lets a newer
        sign-in attempt supersede this one (the user closed the OAuth tab and
        clicked "Sign In" again).
        """
        try:
            if flow_fn is not None:
                result = flow_fn(cancel_event)
            else:
                result = _oauth.run_authorization_flow(
                    progress_cb=self._oauth_progress_cb,
                    url_validator=_is_safe_external_url,
                    cancel_event=cancel_event,
                )
            if not result.get("ok"):
                err = str(result.get("error") or "unknown")
                desc = str(result.get("error_description") or "")
                # A cancelled flow was superseded by a newer attempt (or a
                # deliberate abort) — stay quiet so we don't flash a spurious
                # failure over the fresh flow the user just started.
                if err == "cancelled":
                    print("[API] OAuth flow cancelled (superseded).", flush=True)
                    return
                print(f"[API] OAuth flow failed: {err} ({desc})", flush=True)
                self._notify_js_sign_in_failed(err, desc)
                return

            bundle = result["bundle"]
            _keyring_save(bundle)
            # Caches were keyed on the previous (now-stale) identity.
            self._invalidate_account_caches()
            self._notify_js_sign_in(bundle.get("access_token") or "")
        except Exception as e:
            print(f"[API] _oauth_worker() -> Error: {e}", flush=True)
            try:
                self._notify_js_sign_in_failed("unexpected", str(e))
            except Exception:
                pass
        finally:
            # Only clear shared flow state if THIS worker is still the active
            # one. A superseded worker must not stomp the flag/event/thread of
            # the newer flow that replaced it.
            try:
                lock = self._get_oauth_lock()
            except Exception:
                lock = None
            acquired = lock.acquire(timeout=5.0) if lock is not None else False
            try:
                if cancel_event is None or self._oauth_cancel_event is cancel_event:
                    self._oauth_in_flight = False
                    self._oauth_status = "idle"
                    self._oauth_cancel_event = None
                    self._oauth_thread = None
            finally:
                if acquired:
                    lock.release()

    def _notify_js_sign_in(self, token: str) -> None:
        if self._main_window is None:
            return
        try:
            safe = json.dumps(str(token))
            self._main_window.evaluate_js(
                f"window.onAuthSignIn && window.onAuthSignIn({safe})"
            )
        except Exception as e:
            print(f"[API] _notify_js_sign_in() -> Error: {e}", flush=True)

    def _notify_js_sign_in_failed(self, error: str, description: str = "") -> None:
        if self._main_window is None:
            return
        try:
            payload = json.dumps({"error": str(error), "description": str(description)})
            self._main_window.evaluate_js(
                f"window.onAuthSignInFailed && window.onAuthSignInFailed({payload})"
            )
        except Exception as e:
            print(f"[API] _notify_js_sign_in_failed() -> Error: {e}", flush=True)

    def _notify_js_sign_out(self) -> None:
        if self._main_window is None:
            return
        try:
            self._main_window.evaluate_js(
                "window.onAuthSignOut && window.onAuthSignOut()"
            )
        except Exception as e:
            print(f"[API] _notify_js_sign_out() -> Error: {e}", flush=True)

    def get_oauth_flow_status(self) -> dict:
        """Diagnostic — JS-callable. Mostly useful for surfacing 'stuck' flows."""
        return {
            "success": True,
            "in_flight": bool(self._oauth_in_flight),
            "status": str(self._oauth_status or "idle"),
        }

    def sign_out(self) -> dict:
        """Clear keychain credentials, best-effort revoke at Clerk, notify JS."""
        try:
            bundle = self._load_token_bundle()
            refresh_token = (bundle or {}).get("refresh_token") if bundle else None
            self._clear_keychain_auth()
            self._invalidate_account_caches()
            if refresh_token and _oauth is not None:
                try:
                    _oauth.revoke_token(str(refresh_token))
                except Exception:
                    pass  # best-effort
            self._notify_js_sign_out()
            return {"success": True}
        except Exception as e:
            print(f"[API] sign_out() -> Error: {e}", flush=True)
            return {"success": False, "error": str(e)}

    def _find_sidecar_file(self, root_path: str, filename: str, ext: str = '.xmp'):
        """Find sidecar file with given extension for an image file.
        
        Checks multiple naming conventions:
        - filename + ext (e.g., IMG_001.CR3.xmp)
        - name_without_ext + ext (e.g., IMG_001.xmp for IMG_001.CR3)
        
        Returns the filename (not path) if found, None otherwise.
        Searches in the same directory as the image.
        """
        # Check primary naming: filename + ext (e.g., IMG_001.CR3.xmp)
        sidecar_path = os.path.join(root_path, filename + ext)
        if os.path.exists(sidecar_path):
            return filename + ext
        
        # Check secondary naming: name_without_ext + ext (e.g., IMG_001.xmp)
        if '.' in filename:
            base_name = filename.rsplit('.', 1)[0]
            alt_sidecar_path = os.path.join(root_path, base_name + ext)
            if os.path.exists(alt_sidecar_path):
                return base_name + ext
        
        return None

    def _find_companion_files(self, root_path: str, filename: str) -> list[str]:
        """Find configured companion files (XMP + JPEG variants) for an image."""
        companions: list[str] = []
        seen: set[str] = set()
        filename_key = str(filename or '').lower()

        for ext in self._culling_companion_extensions:
            companion = self._find_sidecar_file(root_path, filename, ext)
            if not companion:
                continue
            key = companion.lower()
            if key == filename_key or key in seen:
                continue
            seen.add(key)
            companions.append(companion)

        return companions

    def _move_file_with_sidecars(self, root_path: str, filename: str, reject_dir: str):
        """Move a file and its configured companion files to reject directory.
        
        Returns (success: bool, moved_files: list[str])
        """
        moved_files = []

        # Move main file
        src = os.path.join(root_path, filename)
        dst = os.path.join(reject_dir, filename)
        try:
            if os.path.exists(src):
                shutil.move(src, dst)
                moved_files.append(filename)
            else:
                return False, moved_files
        except Exception:
            return False, moved_files

        companion_files = self._find_companion_files(root_path, filename)
        if companion_files:
            for companion in companion_files:
                companion_src = os.path.join(root_path, companion)
                companion_dst = os.path.join(reject_dir, companion)
                try:
                    if os.path.exists(companion_src):
                        shutil.move(companion_src, companion_dst)
                        moved_files.append(companion)
                    else:
                        warn(f'[reject] companion detected but not found at: {companion_src}')
                except Exception as e:
                    # Log warning but don't fail the main move if a companion fails
                    warn(f'[reject] Failed to move {companion}: {e}')
        else:
            debug(f'[reject] No companion sidecars found for: {filename}')

        return True, moved_files

    def move_rejects_to_folder(self, root_path: str, filenames):
        """Move original photo files and sidecars into _KESTREL_Rejects subfolder."""
        try:
            root_real, err = self._validate_root_dir(root_path, context='move_rejects_to_folder', require_exists=True)
            if err:
                return {'success': False, 'error': err}

            reject_dir = os.path.join(root_real, '_KESTREL_Rejects')
            reject_real = os.path.realpath(reject_dir)
            if not self._is_within_root(reject_real, root_real):
                self._log_security_reject('move_rejects_to_folder', 'Reject folder escapes root', root=root_real, reject=reject_real)
                return {'success': False, 'error': 'Invalid reject folder path'}

            os.makedirs(reject_dir, exist_ok=True)
            moved = []
            errors = []

            if isinstance(filenames, list):
                raw_filenames = filenames
            elif isinstance(filenames, (tuple, set)):
                raw_filenames = list(filenames)
            elif filenames:
                raw_filenames = [filenames]
            else:
                raw_filenames = []
            sanitized_filenames = []
            for raw in raw_filenames:
                clean = self._sanitize_plain_filename(raw, context='move_rejects_to_folder')
                if clean:
                    sanitized_filenames.append(clean)
                else:
                    errors.append(f'{raw}: invalid filename')

            for fn in sanitized_filenames:
                success, moved_files = self._move_file_with_sidecars(root_real, fn, reject_dir)
                if success:
                    moved.extend(moved_files)
                else:
                    errors.append(f'{fn}: move failed')
            info(f'[reject] moved {len(moved)} file(s) (including sidecars), errors {len(errors)}')
            return {'success': True, 'moved': len(moved), 'errors': errors, 'reject_folder': reject_real}
        except Exception as e:
            error(f'[API] move_rejects_to_folder error: {e}')
            return {'success': False, 'error': str(e)}

    def write_xmp_metadata(
        self,
        root_path: str,
        image_data,
        overwrite_external: bool = False,
        use_auto_labels: bool = False,
        fields=None,
    ):
        """Write XMP sidecar files for each image, embedding star rating and culling label.

        ``fields`` is an optional dict selecting which sections to write
        (``rating``, ``label``, ``species``, ``family``, ``quality``).
        Omitting it writes everything, preserving legacy behaviour.
        """
        if _write_xmp_metadata is None:
            return {'success': False, 'error': 'metadata_writer module not available'}
        root_real, err = self._validate_root_dir(root_path, context='write_xmp_metadata', require_exists=True)
        if err:
            return {'success': False, 'error': err}
        return _write_xmp_metadata(
            root_real,
            image_data,
            overwrite_external,
            use_auto_labels,
            fields=fields if isinstance(fields, dict) else None,
        )

    def _restore_file_with_sidecars(self, reject_dir: str, root_path: str, filename: str):
        """Restore a file and its configured companion files from reject directory.

        Returns (success: bool, restored_files: list[str])
        """
        restored_files = []

        # Restore main file
        src = os.path.join(reject_dir, filename)
        dst = os.path.join(root_path, filename)
        try:
            if os.path.exists(src):
                shutil.move(src, dst)
                restored_files.append(filename)
            else:
                return False, restored_files
        except Exception:
            return False, restored_files

        companion_files = self._find_companion_files(reject_dir, filename)
        if companion_files:
            for companion in companion_files:
                companion_src = os.path.join(reject_dir, companion)
                companion_dst = os.path.join(root_path, companion)
                try:
                    shutil.move(companion_src, companion_dst)
                    restored_files.append(companion)
                except Exception as e:
                    # Log warning but don't fail if companion restore fails
                    warn(f'[reject-undo] Failed to restore {companion}: {e}')
        else:
            debug(f'[reject-undo] No companion sidecars found for: {filename}')

        return True, restored_files

    def undo_reject_move(self, root_path: str, filenames):
        """Move files and their sidecars back from _KESTREL_Rejects to the root folder."""
        try:
            root_real, err = self._validate_root_dir(root_path, context='undo_reject_move', require_exists=True)
            if err:
                return {'success': False, 'error': err}

            reject_dir = os.path.join(root_real, "_KESTREL_Rejects")
            if not os.path.isdir(reject_dir):
                return {"success": False, "error": "_KESTREL_Rejects folder not found"}

            reject_real = os.path.realpath(reject_dir)
            if not self._is_within_root(reject_real, root_real):
                self._log_security_reject('undo_reject_move', 'Reject folder escapes root', root=root_real, reject=reject_real)
                return {'success': False, 'error': 'Invalid reject folder path'}

            restored = []
            errors = []

            if isinstance(filenames, list):
                raw_filenames = filenames
            elif isinstance(filenames, (tuple, set)):
                raw_filenames = list(filenames)
            elif filenames:
                raw_filenames = [filenames]
            else:
                raw_filenames = []
            sanitized_filenames = []
            for raw in raw_filenames:
                clean = self._sanitize_plain_filename(raw, context='undo_reject_move')
                if clean:
                    sanitized_filenames.append(clean)
                else:
                    errors.append(f'{raw}: invalid filename')

            for fn in sanitized_filenames:
                success, restored_files = self._restore_file_with_sidecars(reject_dir, root_real, fn)
                if success:
                    restored.extend(restored_files)
                else:
                    errors.append(f"{fn}: not found in rejects")
            info(f"[reject-undo] restored {len(restored)} file(s) (including sidecars), errors {len(errors)}")
            return {"success": True, "restored": len(restored), "errors": errors}
        except Exception as e:
            error(f"[API] undo_reject_move error: {e}")
            return {"success": False, "error": str(e)}

    def get_reject_restore_state(self, root_path: str):
        """Inspect on-disk traces from prior moves to determine if Undo should be offered."""
        try:
            root_path, err = self._validate_root_dir(root_path, context='get_reject_restore_state', require_exists=True)
            if err:
                return {'success': False, 'error': err}

            reject_dir = os.path.join(root_path, '_KESTREL_Rejects')
            kestrel_dir = os.path.join(root_path, '.kestrel')
            csv_backup = os.path.join(kestrel_dir, 'kestrel_database_old.csv')
            scenedata_backup = os.path.join(kestrel_dir, 'kestrel_scenedata_old.json')

            has_reject_folder = os.path.isdir(reject_dir)
            has_csv_backup = os.path.isfile(csv_backup)
            has_scenedata_backup = os.path.isfile(scenedata_backup)

            if not has_reject_folder:
                return {
                    'success': True,
                    'can_restore': False,
                    'reject_folder_exists': False,
                    'reject_count': 0,
                    'reject_filenames': [],
                    'has_csv_backup': has_csv_backup,
                    'has_scenedata_backup': has_scenedata_backup,
                }

            files = []
            for name in os.listdir(reject_dir):
                full = os.path.join(reject_dir, name)
                if os.path.isfile(full):
                    files.append(name)

            candidates = []
            for name in files:
                ext = os.path.splitext(name)[1].lower()
                if ext in _CULLING_PRIMARY_IMAGE_EXTENSIONS:
                    candidates.append(name)

            # Prefer RAW files as primaries so RAW+JPG pairs restore in one operation.
            candidates.sort(key=lambda n: (0 if os.path.splitext(n)[1].lower() in _RAW_EXTENSION_SET else 1, n.lower()))

            reject_filenames = []
            excluded = set()
            for name in candidates:
                key = name.lower()
                if key in excluded:
                    continue
                reject_filenames.append(name)
                companions = self._find_companion_files(reject_dir, name)
                for comp in companions:
                    excluded.add(comp.lower())

            return {
                'success': True,
                'can_restore': len(reject_filenames) > 0,
                'reject_folder_exists': True,
                'reject_count': len(reject_filenames),
                'reject_filenames': reject_filenames,
                'has_csv_backup': has_csv_backup,
                'has_scenedata_backup': has_scenedata_backup,
            }
        except Exception as e:
            error(f'[API] get_reject_restore_state error: {e}')
            return {'success': False, 'error': str(e)}

    def backup_kestrel_db(self, root_path: str):
        """Backup both kestrel_database.csv and kestrel_scenedata.json before major operations.

        Creates:
        - .kestrel/kestrel_database_old.csv (from kestrel_database.csv)
        - .kestrel/kestrel_scenedata_old.json (from kestrel_scenedata.json)

        Returns:
            {"success": bool, "backup_csv": str, "backup_scenedata": str, "error": str}
        """
        try:
            root_path, err = self._validate_root_dir(root_path, context='backup_kestrel_db', require_exists=True)
            if err:
                return {'success': False, 'error': err, 'backup_csv': '', 'backup_scenedata': ''}

            kestrel_dir = os.path.join(root_path, ".kestrel")
            kestrel_real = os.path.realpath(kestrel_dir)
            if not self._is_within_root(kestrel_real, root_path):
                self._log_security_reject('backup_kestrel_db', 'Resolved .kestrel path escapes root', root=root_path, kestrel=kestrel_real)
                return {'success': False, 'error': 'Invalid .kestrel path', 'backup_csv': '', 'backup_scenedata': ''}

            csv_path = os.path.join(kestrel_dir, "kestrel_database.csv")
            scenedata_path = os.path.join(kestrel_dir, "kestrel_scenedata.json")
            csv_backup = os.path.join(kestrel_dir, "kestrel_database_old.csv")
            scenedata_backup = os.path.join(kestrel_dir, "kestrel_scenedata_old.json")

            if not os.path.exists(csv_path):
                return {"success": False, "error": "kestrel_database.csv not found", "backup_csv": "", "backup_scenedata": ""}

            # Backup CSV
            shutil.copy2(csv_path, csv_backup)
            info(f"[backup] CSV backed up to {csv_backup}")

            # Backup scenedata if it exists
            scenedata_backed = False
            if os.path.exists(scenedata_path):
                shutil.copy2(scenedata_path, scenedata_backup)
                scenedata_backed = True
                info(f"[backup] Scenedata backed up to {scenedata_backup}")

            return {
                "success": True,
                "backup_csv": csv_backup,
                "backup_scenedata": scenedata_backup if scenedata_backed else "",
                "error": ""
            }
        except Exception as e:
            error(f"[API] backup_kestrel_db error: {e}")
            return {"success": False, "error": str(e), "backup_csv": "", "backup_scenedata": ""}

    def restore_kestrel_db_backup(self, root_path: str):
        """Restore both kestrel_database.csv and kestrel_scenedata.json from backups.

        Restores from:
        - .kestrel/kestrel_database_old.csv (to kestrel_database.csv)
        - .kestrel/kestrel_scenedata_old.json (to kestrel_scenedata.json, if backup exists)

        Returns:
            {"success": bool, "error": str}
        """
        try:
            root_path, err = self._validate_root_dir(root_path, context='restore_kestrel_db_backup', require_exists=True)
            if err:
                return {'success': False, 'error': err}

            kestrel_dir = os.path.join(root_path, ".kestrel")
            kestrel_real = os.path.realpath(kestrel_dir)
            if not self._is_within_root(kestrel_real, root_path):
                self._log_security_reject('restore_kestrel_db_backup', 'Resolved .kestrel path escapes root', root=root_path, kestrel=kestrel_real)
                return {'success': False, 'error': 'Invalid .kestrel path'}

            csv_path = os.path.join(kestrel_dir, "kestrel_database.csv")
            csv_backup = os.path.join(kestrel_dir, "kestrel_database_old.csv")
            scenedata_path = os.path.join(kestrel_dir, "kestrel_scenedata.json")
            scenedata_backup = os.path.join(kestrel_dir, "kestrel_scenedata_old.json")

            if not os.path.exists(csv_backup):
                return {"success": False, "error": "kestrel_database_old.csv not found"}

            # Restore CSV
            shutil.copy2(csv_backup, csv_path)
            info(f"[backup] CSV restored from {csv_backup}")

            # Restore scenedata if backup exists
            if os.path.exists(scenedata_backup):
                shutil.copy2(scenedata_backup, scenedata_path)
                info(f"[backup] Scenedata restored from {scenedata_backup}")

            return {"success": True, "error": ""}
        except Exception as e:
            error(f"[API] restore_kestrel_db_backup error: {e}")
            return {"success": False, "error": str(e)}

    def open_reject_folder(self, root_path: str):
        """Open the _KESTREL_Rejects folder in the system file browser."""
        root_path, err = self._validate_root_dir(root_path, context='open_reject_folder', require_exists=True)
        if err:
            return {'success': False, 'error': err}

        reject_dir = os.path.join(root_path, '_KESTREL_Rejects')
        reject_real = os.path.realpath(reject_dir)
        if not self._is_within_root(reject_real, root_path):
            self._log_security_reject('open_reject_folder', 'Reject folder escapes root', root=root_path, reject=reject_real)
            return {'success': False, 'error': 'Invalid reject folder path'}

        if os.path.isdir(reject_dir):
            return self.open_folder(reject_dir)
        return {'success': False, 'error': '_KESTREL_Rejects folder not found'}

    def notify_main_window_refresh(self):
        """Tell the main visualizer window to reload its data."""
        try:
            if not WEBVIEW_IMPORT_SUCCESS:
                return {'success': False, 'error': 'pywebview not available'}
            import webview as _wv
            if _wv.windows and len(_wv.windows) > 0:
                main_win = _wv.windows[0]
                main_win.evaluate_js('if(window.reloadCurrentFolders) window.reloadCurrentFolders();')
                return {'success': True}
            return {'success': False, 'error': 'No main window found'}
        except Exception as e:
            error(f'[API] notify_main_window_refresh error: {e}')
            return {'success': False, 'error': str(e)}

    def read_raw_full(
        self,
        filename: str,
        root_path: str,
        exp_correction: float = 0.0,
        exposure_mode: str = '',
        exposure_meter_scale: float = 1.0,
    ):
        """Process a RAW file and return full-resolution JPEG as base64.
        Results are cached in {root}/.kestrel/culling_TMP/ for fast subsequent loads.
        Falls back to read_image_file for non-RAW formats.

        exp_correction: exposure offset in stops applied during postprocessing.
            0.0 (default) = no correction, matches standard display preview.
            Positive = brighten, negative = darken.  Clamped to [-2.0, +3.0].

        exposure_mode: optional per-row render mode from CSV. When omitted,
            mode falls back to folder metadata.

        exposure_meter_scale: optional per-row global metering scale. When
            mode is no_auto_bright_metered_v1 and exp_correction is ~0, this
            value is used as a fallback correction (log2 scale) so no-detection
            rows still receive baseline metering in RAW preview.
        """
        from io import BytesIO

        try:
            # Normalize separators from CSV/JS so macOS/Linux don't treat '\\' as a literal char.
            filename = str(filename or '').replace('\\', '/')
            root_path_real, full_path_real, err = self._resolve_path_in_root(
                root_path,
                filename,
                context='read_raw_full',
                allow_absolute=True,
            )
            if err:
                return {'success': False, 'error': err}

            full_path = full_path_real
            self._track_cache_root(root_path_real)
            if not os.path.exists(full_path):
                return {'success': False, 'error': f'File not found: {filename}'}

            ext = os.path.splitext(filename)[1].lower()

            if ext not in _RAW_EXTENSION_SET:
                return self.read_image_file(filename, root_path_real)

            # Clamp exposure correction to the same limits as the pipeline
            try:
                exp_correction = float(exp_correction)
            except (TypeError, ValueError):
                exp_correction = 0.0
            requested_exp_correction = max(-2.0, min(3.0, exp_correction))

            try:
                exposure_meter_scale = float(exposure_meter_scale)
            except (TypeError, ValueError):
                exposure_meter_scale = 1.0
            if not math.isfinite(exposure_meter_scale) or exposure_meter_scale <= 0.0:
                exposure_meter_scale = 1.0
            exposure_meter_scale = max(0.25, min(8.0, exposure_meter_scale))

            mode_override = str(exposure_mode or '').strip().lower()
            if mode_override in {'legacy_auto_bright_v1', 'no_auto_bright_metered_v1'}:
                render_mode = mode_override
            else:
                render_mode = self._get_exposure_render_mode(root_path_real)
            use_no_auto_bright = render_mode == 'no_auto_bright_metered_v1'

            exp_correction = requested_exp_correction
            used_meter_fallback = False
            if use_no_auto_bright and abs(exp_correction) <= 1e-4:
                # No-bird rows typically carry zero EV but still have a global
                # metering scale. Recover that baseline correction for RAW preview.
                meter_stops = math.log2(exposure_meter_scale)
                if abs(meter_stops) > 1e-3:
                    exp_correction = meter_stops
                    used_meter_fallback = True
            exp_correction = max(-2.0, min(3.0, exp_correction))

            settings = load_persisted_settings()
            use_cache = bool(settings.get('raw_preview_cache_enabled', True))
            debug_logging_enabled = bool(settings.get('raw_preview_debug_logging_enabled', True))

            cache_dir = os.path.join(root_path_real, '.kestrel', 'culling_TMP')
            # Cache key includes relative path + extension + file identity,
            # and exposure/mode so previews cannot be reused across EV variants
            # or different exposure-render pipelines.
            file_stat = os.stat(full_path)
            rel_for_key = os.path.normpath(os.path.relpath(full_path_real, root_path_real)).replace('\\', '/')
            key_material = (
                f'{rel_for_key}|{ext}|{int(file_stat.st_mtime_ns)}|{int(file_stat.st_size)}'
                f'|ev={exp_correction:+.4f}|mode={render_mode}'
            )
            cache_token = hashlib.sha1(key_material.encode('utf-8')).hexdigest()[:16]
            base = os.path.splitext(os.path.basename(filename))[0]
            cache_name = f'{base}_{cache_token}_preview.jpg'
            cache_path = os.path.join(cache_dir, cache_name)

            debug_meta = {
                'filename': filename,
                'full_path': full_path,
                'platform': sys.platform,
                'exp_correction_requested': round(float(requested_exp_correction), 4),
                'exp_correction_effective': round(float(exp_correction), 4),
                'exposure_meter_scale': round(float(exposure_meter_scale), 6),
                'used_meter_fallback': bool(used_meter_fallback),
                'requested_mode': mode_override,
                'render_mode': render_mode,
                'use_no_auto_bright': bool(use_no_auto_bright),
                'use_cache': bool(use_cache),
                'cache_dir': cache_dir,
                'cache_name': cache_name,
                'cache_path': cache_path,
                'key_material': key_material,
                'cache_token': cache_token,
            }

            if use_cache and os.path.exists(cache_path):
                debug(
                    f'[raw-preview] cache hit for {filename} '
                    f'(exp={exp_correction:+.3f}, mode={render_mode})'
                )
                with open(cache_path, 'rb') as f:
                    cache_bytes = f.read()
                cache_stat = os.stat(cache_path)
                debug_meta.update({
                    'cache_hit': True,
                    'cache_file_bytes': int(len(cache_bytes)),
                    'cache_file_mtime_ns': int(cache_stat.st_mtime_ns),
                    'storage_preview_path': cache_path,
                })
                if debug_logging_enabled:
                    debug(f'[raw-preview] debug: {json.dumps(debug_meta, sort_keys=True)}')
                b64 = base64.b64encode(cache_bytes).decode('ascii')
                return {'success': True, 'data': b64, 'mime': 'image/jpeg', 'debug': debug_meta}

            import rawpy
            from PIL import Image

            debug(
                f'[raw-preview] Processing RAW file {filename} '
                f'(exp={exp_correction:+.3f}, mode={render_mode}, cache={use_cache})'
            )
            with rawpy.imread(full_path) as raw:
                try:
                    sizes = raw.sizes
                    raw_sizes = {
                        'width': int(getattr(sizes, 'width', 0) or 0),
                        'height': int(getattr(sizes, 'height', 0) or 0),
                        'raw_width': int(getattr(sizes, 'raw_width', 0) or 0),
                        'raw_height': int(getattr(sizes, 'raw_height', 0) or 0),
                        'iwidth': int(getattr(sizes, 'iwidth', 0) or 0),
                        'iheight': int(getattr(sizes, 'iheight', 0) or 0),
                        'flip': int(getattr(sizes, 'flip', 0) or 0),
                    }
                except Exception:
                    raw_sizes = {}

                linear_scale = float(max(0.25, min(8.0, 2.0 ** exp_correction)))
                if use_no_auto_bright:
                    rgb = raw.postprocess(
                        no_auto_bright=True,
                        exp_shift=linear_scale,
                        exp_preserve_highlights=_preserve_highlights_for_stops(exp_correction),
                    )
                else:
                    if exp_correction != 0.0:
                        rgb = raw.postprocess(
                            exp_shift=linear_scale,
                            exp_preserve_highlights=_preserve_highlights_for_stops(exp_correction),
                        )
                    else:
                        rgb = raw.postprocess()

            img = Image.fromarray(rgb)

            buf = BytesIO()
            img.save(buf, format='JPEG', quality=90, subsampling=0, optimize=False, progressive=False)
            jpg_bytes = buf.getvalue()
            wrote_cache = False
            if use_cache:
                os.makedirs(cache_dir, exist_ok=True)
                with open(cache_path, 'wb') as f:
                    f.write(jpg_bytes)
                wrote_cache = True

            storage_preview_path = cache_path
            if not wrote_cache:
                # Even when cache is disabled, persist one debug copy for inspection.
                os.makedirs(cache_dir, exist_ok=True)
                debug_name = f'{base}_{cache_token}_preview_debug.jpg'
                storage_preview_path = os.path.join(cache_dir, debug_name)
                with open(storage_preview_path, 'wb') as f:
                    f.write(jpg_bytes)

            b64 = base64.b64encode(jpg_bytes).decode('ascii')
            debug_meta.update({
                'cache_hit': False,
                'cache_written': bool(wrote_cache),
                'storage_preview_path': storage_preview_path,
                'raw_sizes': raw_sizes,
                'postprocess_rgb_shape': list(rgb.shape) if hasattr(rgb, 'shape') else [],
                'postprocess_rgb_dtype': str(getattr(rgb, 'dtype', '')),
                'jpeg_bytes': int(len(jpg_bytes)),
                'jpeg_kb': round(len(jpg_bytes) / 1024.0, 2),
                'jpeg_dimensions': {'width': int(img.width), 'height': int(img.height)},
            })
            if debug_logging_enabled:
                debug(f'[raw-preview] debug: {json.dumps(debug_meta, sort_keys=True)}')
            if use_cache:
                debug(f'[raw-preview] Done, {len(jpg_bytes)//1024}KB JPEG ({img.width}x{img.height}), cached as {cache_name}')
            else:
                debug(f'[raw-preview] Done, {len(jpg_bytes)//1024}KB JPEG ({img.width}x{img.height}), cache disabled')
            return {'success': True, 'data': b64, 'mime': 'image/jpeg', 'debug': debug_meta}
        except Exception as e:
            error(f'[API] read_raw_full error: {e} (filename={filename}, root_path={root_path_real if "root_path_real" in locals() else root_path})')
            return {'success': False, 'error': str(e)}

    def cleanup_culling_cache(self, root_path: str):
        """Remove the .kestrel/culling_TMP folder to free up space."""
        try:
            root_real, err = self._validate_root_dir(root_path, context='cleanup_culling_cache', require_exists=False)
            if err:
                return {'success': False, 'error': err}

            if not os.path.isdir(root_real):
                return {'success': True}

            cache_dir = os.path.join(root_real, '.kestrel', 'culling_TMP')
            cache_real = os.path.realpath(cache_dir)
            if not self._is_within_root(cache_real, root_real):
                self._log_security_reject('cleanup_culling_cache', 'Cache path escapes root', root=root_real, cache=cache_real)
                return {'success': False, 'error': 'Invalid cache path'}

            if os.path.exists(cache_dir):
                shutil.rmtree(cache_dir)
                info(f'[cache] cleanup_culling_cache: removed {cache_dir}')
                return {'success': True}
            return {'success': True}
        except Exception as e:
            error(f'[API] cleanup_culling_cache error: {e}')
            return {'success': False, 'error': str(e)}

    def cleanup_tracked_culling_caches(self):
        """Clear RAW preview caches for all roots touched in this app session."""
        try:
            roots = sorted(self._cache_cleanup_roots)
            if not roots:
                return {'success': True, 'cleared': 0, 'failed': []}

            failed = []
            cleared = 0
            for root in roots:
                res = self.cleanup_culling_cache(root)
                if res.get('success'):
                    cleared += 1
                else:
                    failed.append({'root': root, 'error': res.get('error', 'Unknown error')})

            # Always clear the tracking set; future sessions can re-populate it.
            self._cache_cleanup_roots.clear()
            return {'success': len(failed) == 0, 'cleared': cleared, 'failed': failed}
        except Exception as e:
            error(f'[API] cleanup_tracked_culling_caches error: {e}')
            return {'success': False, 'cleared': 0, 'failed': [{'root': '', 'error': str(e)}]}
