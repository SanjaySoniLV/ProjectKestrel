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
import shutil
import subprocess
import sys
import time
import webbrowser

from settings_utils import load_persisted_settings, save_persisted_settings, debug, info, warn, error
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

# Cloud-compute backend poller cadence (seconds). One poller per active job
# keeps the per-job remote snapshot fresh; JS reads from cache so there is no
# N+1 query against the Worker per render. 5s gives near-realtime UI without
# burning Worker subrequests when several jobs run in parallel.
_CC_POLL_INTERVAL_SEC = 5

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
    """Write the auth JWT to OS keychain; fall back to plaintext file.

    Fallback file is locked down to owner-read/write (``0o600``) and lives
    in a ``0o700`` directory. Without that the default umask leaves the
    file world-readable on POSIX, which on a shared dev box / CI box is a
    direct JWT exfil path (audit Medium-13). On Windows, ``chmod`` is a
    weak ACL approximation; this is best-effort there — the keyring path
    is the only secure-by-default option on Windows.
    """
    try:
        import keyring
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_KEY, json.dumps(data))
    except Exception:
        path = _get_auth_fallback_path()
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        try:
            os.chmod(directory, 0o700)
        except OSError:
            pass
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _auth_normalize_expiry_seconds(expiry: float | int) -> float:
    """JWT exp is seconds since epoch. Some callers accidentally pass ms."""
    e = float(expiry)
    if e > 1e12:  # e.g. 1730000000000
        e = e / 1000.0
    return e


def _auth_debug_jwt_enabled() -> bool:
    return os.environ.get("AUTH_DEBUG_JWT", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _auth_debug_log_token(where: str, token: str | None) -> None:
    """Log non-secret JWT metadata (iss, aud, exp) and a short token fingerprint."""
    if not _auth_debug_jwt_enabled():
        return
    if not token:
        log(f"[Auth debug] {where}: (no token)")
        return
    t = str(token).strip()
    parts = t.split(".")
    payload: dict = {}
    if len(parts) >= 2:
        try:
            seg = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(seg))
        except Exception as ex:  # pragma: no cover
            payload = {"_decode_error": str(ex)}
    head = t[:24] if len(t) > 24 else t
    tail = t[-16:] if len(t) > 16 else ""
    log(
        f"[Auth debug] {where}: len={len(t)} fingerprint={head!r}…{tail!r} "
        f"iss={payload.get('iss')!r} aud={payload.get('aud')!r} "
        f"exp={payload.get('exp')} sub={payload.get('sub')!r}"
    )


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
        # pause_event, thread, result}). Cloud-compute reuses the Perch JWT
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
            path_real = os.path.realpath(path)
            root_real = os.path.realpath(root)
            common = os.path.commonpath([path_real, root_real])
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
    
    def choose_directory(self):
        """Open native folder picker dialog.
        Returns: absolute path to selected folder, or None if cancelled.
        """
        try:
            if sys.platform == 'darwin':
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
                        'kestrel_version': kestrel_version,
                        'children': children,
                    })
                return result

            tree = _scan(root_path, max_depth)
            root_has_kestrel = os.path.isfile(os.path.join(root_path, '.kestrel', 'kestrel_database.csv'))
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
            webbrowser.open(url)
            return {'success': True}
        except Exception as e:
            error(f'[API] open_url({url!r}) error: {e}')
            return {'success': False, 'error': str(e)}

    # ------------------------------------------------------------------ #
    #  Telemetry / Feedback API                                            #
    # ------------------------------------------------------------------ #

    def send_feedback(self, data):
        """Send feedback / bug report (async, failsafe). Called from JS."""
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
            _telemetry.send_feedback(
                report_type=data.get('type', 'general'),
                description=data.get('description', ''),
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
                    db_dst       = os.path.join(kestrel_dir, 'kestrel_database.csv')
                    readonly_exists = os.path.isfile(readonly_src)
                    debug_info.append(f'[api]     readonly_src: {readonly_src} exists={readonly_exists}')
                    
                    if readonly_exists:
                        try:
                            shutil.copy2(readonly_src, db_dst)
                            debug_info.append(f'[api]     Restored sample DB: {db_dst}')
                        except Exception as e:
                            debug_info.append(f'[api]     Failed to restore DB: {e}')
                    else:
                        debug_info.append(f'[api]     No readonly DB found at {readonly_src}')
                    
                    paths.append(full)
                    debug_info.append(f'[api]     Added path: {full}')
            
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

    def start_analysis_queue(self, paths, use_gpu=True, wildlife_enabled=True, retry_errored=False, species_detection_enabled=True):
        """Enqueue folders for analysis. ``paths`` may be a JSON string or list.

        ``retry_errored`` (bool): when True, drop rows previously marked
        ``species == "Error"`` from each folder's CSV before reprocessing, so
        those images get re-analyzed instead of being skipped as already-done.

        ``species_detection_enabled`` (bool): when False, the bird species
        classifier is skipped and species/family fields are recorded as
        ``Unknown``. Detection, quality scoring, and culling still run.
        """
        try:
            if isinstance(paths, str):
                paths = json.loads(paths)
            if not isinstance(paths, list):
                return {'success': False, 'error': 'paths must be a list'}

            validated_paths = []
            invalid_paths = []
            for raw in paths:
                if not raw:
                    continue
                root_real, err = self._validate_root_dir(raw, context='start_analysis_queue', require_exists=True)
                if err:
                    invalid_paths.append(str(raw))
                    continue
                if root_real not in validated_paths:
                    validated_paths.append(root_real)

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
                                          retry_errored=bool(retry_errored))
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

    def restore_analysis_queue(self):
        """Restore a previous queue snapshot persisted in user settings."""
        return _queue_manager.restore_from_persisted_state()

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
    _sign_in_window = None
    _server_port = None

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

    def get_auth_token(self):
        """Return stored Perch JWT if present and not near expiry, else token=None."""
        try:
            data = _keyring_load()
            if not data:
                _auth_debug_log_token("get_auth_token: keyring empty", None)
                return {"success": True, "token": None}
            token = data.get("token")
            if not token:
                return {"success": True, "token": None}
            ttl = _auth_jwt_seconds_until_exp(str(token))
            if ttl is not None:
                if ttl < 300:  # 5-min buffer (match prior keyring behaviour)
                    _auth_debug_log_token(
                        "get_auth_token: JWT exp within 5min (rejected)", token
                    )
                    if _auth_debug_jwt_enabled():
                        log(f"[Auth debug] get_auth_token: ttl_sec={ttl:.0f}")
                    return {"success": True, "token": None}
            else:
                expiry_raw = data.get("expiry", 0)
                try:
                    exp = _auth_normalize_expiry_seconds(expiry_raw) if expiry_raw else 0.0
                except (TypeError, ValueError):
                    exp = 0.0
                if time.time() > (exp - 300):
                    _auth_debug_log_token(
                        "get_auth_token: could not decode JWT; stored exp past buffer",
                        token,
                    )
                    return {"success": True, "token": None}
            exp_out = _auth_jwt_exp_unverified(str(token))
            if exp_out is None:
                try:
                    exp_out = _auth_normalize_expiry_seconds(data.get("expiry", 0))
                except (TypeError, ValueError):
                    exp_out = 0.0
            _auth_debug_log_token("get_auth_token: returning token", token)
            return {"success": True, "token": token, "expiry": exp_out}
        except Exception as e:
            print(f"[API] get_auth_token() -> Error: {e}", flush=True)
            return {"success": True, "token": None}

    def store_auth_token(self, token, expiry):
        """Persist Perch JWT to OS keychain. Called from desktop-signin.html via pywebview."""
        try:
            try:
                exp = _auth_normalize_expiry_seconds(
                    float(expiry) if expiry is not None else 0.0
                )
            except (TypeError, ValueError):
                exp = 0.0
            _keyring_save({"token": str(token), "expiry": exp})
            _auth_debug_log_token("store_auth_token: saved", str(token) if token else None)
            if _auth_debug_jwt_enabled():
                log(f"[Auth debug] store_auth_token: exp_stored={exp!r} (from arg {expiry!r})")
            # Invalidate per-instance caches — they're keyed on the previous
            # (possibly expired) token and would otherwise pin the UI to a
            # stale "not signed in" state for up to 5 minutes after re-auth.
            self._perch_account_cache = None
            self._perch_account_cache_at = 0.0
            self._perch_usage_cache = None
            self._perch_usage_cache_at = 0.0
            # Notify main window
            if self._main_window:
                safe_token = json.dumps(str(token))
                self._main_window.evaluate_js(
                    f'window.onAuthSignIn && window.onAuthSignIn({safe_token})'
                )
            # Close sign-in window
            if self._sign_in_window:
                self._sign_in_window.destroy()
                self._sign_in_window = None
            return {'success': True}
        except Exception as e:
            print(f'[API] store_auth_token() -> Error: {e}', flush=True)
            return {'success': False, 'error': str(e)}

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
        """
        data = _keyring_load()
        token = (data or {}).get("token")
        dev_user = os.environ.get("PERCH_DEV_USER_ID")
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
        data = _keyring_load()
        token = (data or {}).get("token")
        dev_user = os.environ.get("PERCH_DEV_USER_ID")
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

    # ─── Cloud Compute — submit / poll / cancel ───────────────────────────
    # Reuses the Perch JWT (same Clerk identity). The cloud-compute Worker
    # validates the JWT and calls Perch internally for entitlement + usage
    # accrual; the desktop app does not need to know about that handshake.

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
            )
        except ValueError as e:
            return None, {"ok": False, "error": str(e)}
        return client, None

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
        # Match the local pipeline / folder_inspector RAW-priority rule: if the
        # folder has any RAWs we ONLY analyze RAWs (their JPEG sidecars are
        # ignored). Only when there are zero RAWs do we fall back to JPEGs.
        # Without this, cloud jobs upload both RAW and JPG, doubling bandwidth
        # and producing duplicate scene rows on merge. See folder_inspector.py
        # `_list_images_in_folder` (line 20-34) — keep the two filters in sync.
        try:
            from kestrel_analyzer.config import RAW_EXTENSIONS, JPEG_EXTENSIONS
        except ImportError:
            from analyzer.kestrel_analyzer.config import RAW_EXTENSIONS, JPEG_EXTENSIONS  # type: ignore[no-redef]
        raw_set = {ext.lower() for ext in RAW_EXTENSIONS}
        jpeg_set = {ext.lower() for ext in JPEG_EXTENSIONS}
        candidates = [p for p in folder.iterdir() if p.is_file()]
        raws = sorted(p for p in candidates if p.suffix.lower() in raw_set)
        all_files = raws if raws else sorted(p for p in candidates if p.suffix.lower() in jpeg_set)
        if not all_files:
            return [], None, frozenset(), 0, 0

        db_path = folder / ".kestrel" / "kestrel_database.csv"
        analyzed: set = set()
        errored: set = set()
        if db_path.is_file():
            try:
                import csv as _csv
                with db_path.open("r", encoding="utf-8", newline="") as f:
                    reader = _csv.DictReader(f)
                    for row in reader:
                        name = (row.get("filename") or "").strip()
                        if not name:
                            continue
                        analyzed.add(name)
                        # Error marker matches the local pipeline's predicate
                        # at pipeline.py: species=="Error" (no separate boolean).
                        if (row.get("species") or "").strip() == "Error":
                            errored.add(name)
            except Exception:
                analyzed = set()
                errored = set()

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
        "uploadPauseRequested": False,
        "stopRequested": False,
        "controlFlags": {},
        "remoteStatus": None,
        "updatedAtMs": 0,
        "failureCount": 0,
        "lastError": None,
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
            "downloadedCount", "pack_count", "uploadPauseRequested",
            "stopRequested", "controlFlags",
        ):
            if key in remote and remote[key] is not None:
                snapshot[key] = remote[key]
        rs = remote.get("status")
        if isinstance(rs, str) and rs:
            snapshot["remoteStatus"] = rs
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
             the next bootstrap reconciliation will retry — see
             cloud_compute_list_pending_jobs's stale-R2-pack cleanup pass.

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
        a no-op if a poller is already running for this job_id. Exits when the
        local status becomes terminal (``done|failed|cancelled``) or
        ``cancel_event`` fires."""
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
                    if state.get("status") in ("done", "failed", "cancelled"):
                        return
                    cancel_ev = state.get("cancel_event")
                if cancel_ev is not None and cancel_ev.is_set():
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
                        # Phase 2 auto-resume: when the Worker observes that
                        # the client is heartbeating again after a previous
                        # auto-pause (e.g. desktop was offline), it returns
                        # autoPausedCleared=true on this single response.
                        # Release the local pause_event so the upload thread
                        # un-blocks without needing the user to click Resume.
                        if isinstance(remote, dict) and remote.get("autoPausedCleared"):
                            with self._ensure_cc_lock():
                                st = self._cc_jobs.get(job_id) or {}
                                pe = st.get("pause_event")
                            if pe is not None and not pe.is_set():
                                pe.set()
                                warn(
                                    f"[cloud-compute] {job_id}: Worker auto-cleared upload pause; "
                                    "resuming local upload thread."
                                )
                except Exception as e:
                    self._cc_record_remote_failure(job_id, str(e))
                    # Log every 5th consecutive failure so the journal doesn't
                    # drown but the user can still find the original cause.
                    with self._ensure_cc_lock():
                        st = self._cc_jobs.get(job_id) or {}
                        fc = int(((st.get("remote") or {}).get("failureCount")) or 0)
                    if fc == 1 or fc % 5 == 0:
                        warn(f"[cloud-compute] poller {job_id}: failure #{fc}: {e}")
                _time.sleep(_CC_POLL_INTERVAL_SEC)

        thread = _t.Thread(target=_poller, name=f"cc-poll-{job_id}", daemon=True)
        with self._ensure_cc_lock():
            self._cc_poll_threads[job_id] = thread
        thread.start()

    def cloud_compute_submit_job(self, root_path: str) -> dict:
        """Kick off a cloud-compute job for a folder of CR3s. Non-blocking.

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
                return {"ok": False, "error": "No CR3/JPEG files found in folder"}
            return {
                "ok": False,
                "error": (
                    f"All {already_analyzed} of {total_in_folder} image(s) in "
                    "this folder are already analyzed — nothing to send to "
                    "cloud compute."
                ),
                "nothingToDo": True,
            }

        # Submit synchronously (cheap call). We need the jobId before we can
        # return it to the caller; the heavy upload+poll runs on a thread.
        try:
            submit = client.submit_job(files, analysis_settings=analysis_settings)
        except ccc.JobInProgressError as e:
            # Stage 6 concurrency gate: a Cloud Compute job is already in
            # flight for this user. Not a fault — surface to JS with a
            # MyAccount deep-link instead of an error toast.
            return {
                "ok": False,
                "error": "job_in_progress",
                "activeJobId": e.active_job_id,
                "myAccountUrl": "https://myaccount.projectkestrel.org/cloud-compute",
                "message": str(e) or "You have a Cloud Compute job running.",
            }
        except ccc.CloudComputeError as e:
            return {
                "ok": False,
                "error": e.message,
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
        # pause_event is "set" when uploads are running; cleared to pause.
        # Starts set so the upload thread does not block out of the gate.
        pause_event = _t.Event()
        pause_event.set()

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
                    pause_event=pause_event,
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
            terminal = "done" if result.get("ok") else "failed"
            with self._ensure_cc_lock():
                state = self._cc_jobs.get(job_id)
                if state is not None:
                    # Don't clobber a cancellation that landed during the
                    # final stretch (race between cancel + run_full_job's
                    # natural completion).
                    if state.get("status") != "cancelled":
                        state["status"] = terminal
                        state["result"] = result
            try:
                if terminal == "done":
                    self._cc_jobs_store().update_job(job_id, status="done")
                else:
                    self._cc_jobs_store().update_job(job_id, status="failed")
            except Exception:
                pass

        with self._ensure_cc_lock():
            self._cc_jobs[job_id] = {
                "jobId": job_id,
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
                "pause_event": pause_event,
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
            # Set by the bootstrap orphan reaper (e.g. "upload_interrupted")
            # so the panel can explain why a non-obvious failure happened.
            "failureReason": state.get("failureReason") or "",
            "progress": dict(state.get("progress") or {}),
            # Cached remote counters (zeros until first poll lands).
            "uploadedCount": remote.get("uploadedCount", 0),
            "analyzedCount": remote.get("analyzedCount", 0),
            "dispatchedCount": remote.get("dispatchedCount", 0),
            "pendingCount": remote.get("pendingCount", 0),
            "downloadedCount": remote.get("downloadedCount", 0),
            "pack_count": remote.get("pack_count", 0),
            "uploadPauseRequested": remote.get("uploadPauseRequested", False),
            "stopRequested": remote.get("stopRequested", False),
            "controlFlags": remote.get("controlFlags", {}),
            "remoteStatus": remote.get("remoteStatus"),
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
        with self._ensure_cc_lock():
            jobs = [
                self._cc_serialise_job(jid, state)
                for jid, state in self._cc_jobs.items()
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

    def cloud_compute_clear_done(self) -> dict:
        """Remove every terminal job (done|cancelled|failed) from both the
        persistent ledger and the in-memory map. Returns the IDs removed."""
        try:
            removed = self._cc_jobs_store().remove_terminal_jobs()
        except Exception as e:
            return {"ok": False, "error": f"store: {e}"}
        with self._ensure_cc_lock():
            for jid in removed:
                self._cc_jobs.pop(jid, None)
        return {"ok": True, "removed": removed}

    def cloud_compute_pause_job(self, job_id: str) -> dict:
        """Pause uploads for a job. Modal keeps draining the already-uploaded
        backlog — pause is upload-side only. Worker tracks the pause in
        ``jobs.upload_pause_requested`` so cross-orchestrator decisions (Stage
        2 multi-modal) can respect it later. Idempotent."""
        with self._ensure_cc_lock():
            state = self._cc_jobs.get(job_id)
            if state is None:
                return {"ok": False, "error": "unknown jobId"}
            pause_ev = state.get("pause_event")
        # Tell the Worker first so it survives a desktop crash.
        client, client_err = self._cc_make_client()
        if client is None:
            return client_err or {"ok": False, "error": "no client"}
        try:
            client.pause_job(job_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if pause_ev is not None:
            pause_ev.clear()
        with self._ensure_cc_lock():
            if job_id in self._cc_jobs:
                self._cc_jobs[job_id]["status"] = "upload_paused"
        try:
            self._cc_jobs_store().update_job(job_id, status="upload_paused")
        except Exception:
            # Store write failed — revert in-memory state so local + remote agree.
            if pause_ev is not None:
                pause_ev.set()
            with self._ensure_cc_lock():
                if job_id in self._cc_jobs:
                    self._cc_jobs[job_id]["status"] = "uploading"
        return {"ok": True, "uploadPauseRequested": True}

    def cloud_compute_resume_job(self, job_id: str) -> dict:
        """Inverse of pause_job. Idempotent."""
        with self._ensure_cc_lock():
            state = self._cc_jobs.get(job_id)
            if state is None:
                return {"ok": False, "error": "unknown jobId"}
            pause_ev = state.get("pause_event")
        client, client_err = self._cc_make_client()
        if client is None:
            return client_err or {"ok": False, "error": "no client"}
        try:
            client.resume_job(job_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if pause_ev is not None:
            pause_ev.set()
        with self._ensure_cc_lock():
            if job_id in self._cc_jobs:
                self._cc_jobs[job_id]["status"] = "uploading"
        try:
            self._cc_jobs_store().update_job(job_id, status="uploading")
        except Exception:
            # Store write failed — revert in-memory state so local + remote agree.
            if pause_ev is not None:
                pause_ev.clear()
            with self._ensure_cc_lock():
                if job_id in self._cc_jobs:
                    self._cc_jobs[job_id]["status"] = "upload_paused"
        return {"ok": True, "uploadPauseRequested": False}

    def cloud_compute_cancel_job(self, job_id: str) -> dict:
        """Terminal cancel. Tells the Worker to stop the job (Modal exits on
        next pending-images poll, staging objects are async-deleted) AND
        signals the local upload/poll thread to exit. Marks the job
        ``cancelled`` in the persistent ledger.
        """
        with self._ensure_cc_lock():
            state = self._cc_jobs.get(job_id)
            if state is None:
                return {"ok": False, "error": "unknown jobId"}
            cancel_ev = state.get("cancel_event")
            pause_ev = state.get("pause_event")
        # Remote cancel first. If the desktop dies right after, the Worker
        # has already started staging-cleanup so we don't leak storage.
        client, _err = self._cc_make_client()
        remote_err: str | None = None
        if client is not None:
            try:
                client.cancel_job_remote(job_id)
            except Exception as e:
                remote_err = str(e)
        # Local cleanup: release any pause so the upload thread can see the
        # cancel and exit, then set cancel_event.
        if pause_ev is not None:
            pause_ev.set()
        if cancel_ev is not None:
            cancel_ev.set()
        with self._ensure_cc_lock():
            if job_id in self._cc_jobs:
                self._cc_jobs[job_id]["status"] = "cancelled"
        try:
            self._cc_jobs_store().update_job(job_id, status="cancelled")
        except Exception:
            pass
        return {"ok": True, "remoteError": remote_err}

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
            return {"ok": False, "error": e.message, "status": e.status}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        result["ok"] = True
        return result

    def cloud_compute_list_pending_jobs(self) -> dict:
        """Return the set of jobs whose status is not terminal locally OR whose
        result packs have not all been downloaded yet. Used by the startup
        resume flow: when the user reopens the app, JS calls this; if it
        returns non-empty, the resume dialog prompts the user to download.

        Each entry: ``{jobId, folderPath, status, imageCount, downloadedPacks,
        remoteStatus, availablePacks}``. ``remoteStatus`` / ``availablePacks``
        are best-effort — set to ``None`` on transient Worker failures.
        """
        try:
            store = self._cc_jobs_store()
            jobs = store.load_jobs()
        except Exception as e:
            return {"ok": False, "error": f"store load failed: {e}", "jobs": []}

        if not jobs:
            return {"ok": True, "jobs": []}

        client, _ = self._cc_make_client()

        # Bootstrap orphan reaper. Any local job stuck in 'uploading' or
        # 'upload_paused' across a process restart has no upload thread to
        # resume — the previous process took it to the grave. Mark such jobs
        # failed locally with a clear reason so the UI can explain it, and
        # fire-and-forget a Worker cancel to clean up R2 + D1 (idempotent;
        # no-op if the Worker already moved to terminal). Only reaches here
        # the first time bootstrap runs after a crash because subsequent
        # passes find them in 'failed' state and skip.
        orphan_statuses = {"uploading", "upload_paused"}
        orphans = [j for j in jobs if j["status"] in orphan_statuses
                   and j["jobId"] not in self._cc_jobs]
        if orphans:
            import threading as _t
            for j in orphans:
                try:
                    store.update_job(
                        j["jobId"],
                        status="failed",
                        failureReason="upload_interrupted",
                    )
                    j["status"] = "failed"
                    j["failureReason"] = "upload_interrupted"
                except Exception:
                    pass
                # Register in the in-memory map so the cloud queue panel
                # surfaces the failed orphan with a clear reason. Without this
                # the user would have no signal that the job is dead — the
                # panel only renders _cc_jobs entries, not the persistent
                # ledger.
                with self._ensure_cc_lock():
                    if j["jobId"] not in self._cc_jobs:
                        self._cc_jobs[j["jobId"]] = {
                            "jobId": j["jobId"],
                            "rootPath": j["folderPath"],
                            "imageCount": int(j.get("imageCount") or 0),
                            "newImageCount": int(j.get("imageCount") or 0),
                            "anchorFilename": (j.get("anchorFilename") or "") or None,
                            "totalInFolder": None,
                            "alreadyAnalyzed": None,
                            "status": "failed",
                            "failureReason": "upload_interrupted",
                            "progress": {"event": "orphan_reaped"},
                            "cancel_event": None,
                            "pause_event": None,
                            "remote": dict(self._CC_REMOTE_DEFAULTS),
                        }
                if client is not None:
                    jid = j["jobId"]
                    def _cancel(jid=jid):
                        try:
                            # `origin=orphan` so the Worker audit log can
                            # distinguish a desktop-crash reap from a user
                            # click. Was previously calling the wrong method
                            # (`cancel_job` doesn't exist; only
                            # `cancel_job_remote` does) — the bare except
                            # silently swallowed the AttributeError so orphan
                            # cleanup on the Worker side was never firing.
                            client.cancel_job_remote(jid, origin="orphan")
                        except Exception as e:
                            warn(f"[cloud-compute] orphan reap {jid}: cancel_job_remote failed: {e}")
                    _t.Thread(target=_cancel, name=f"cc-reap-{jid}", daemon=True).start()

        # Locally-terminal jobs (done/cancelled/failed) skip Worker I/O entirely
        # at bootstrap. They still appear in the returned list so the panel can
        # render them (and Clear Done can target them), but we don't burn
        # /api/jobs/* + /api/jobs/*/results requests on jobs the desktop has
        # already finalised. Avoids the audit's HIGH-2 case where a cancelled
        # job whose Worker race-condition'd to 'complete' still showed up as
        # resumable.
        from cloud_jobs_store import _TERMINAL_STATUSES as _CC_TERMINAL_STATUSES
        try:
            from cloud_folder_state import list_merged_packs as _fs_list_merged
        except Exception:
            _fs_list_merged = None  # noqa: N816
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
            }
            is_terminal = j["status"] in _CC_TERMINAL_STATUSES
            if client is not None and not is_terminal:
                try:
                    remote = client.get_status(j["jobId"])
                    entry["remoteStatus"] = remote.get("status")
                    entry["analyzedCount"] = remote.get("analyzedCount")
                    files = client.list_results(j["jobId"])
                    available = [
                        str(f.get("filename") or "")
                        for f in files
                        if str(f.get("filename") or "").endswith(".zip")
                    ]
                    # Proactive stale-R2 cleanup: any pack that's already
                    # been merged locally is dead weight in R2. Best-effort
                    # batch delete; on failure, log and let the next
                    # bootstrap (or a Worker-side cleanup cron, when we
                    # build it) catch it.
                    if folder_available:
                        merged_set = set(merged_union)
                        stale = [n for n in available if n in merged_set]
                        if stale:
                            try:
                                client.delete_packs(j["jobId"], stale)
                                available = [n for n in available if n not in merged_set]
                            except Exception as e:
                                warn(
                                    f"[cloud-compute] {j['jobId']}: bootstrap stale-R2 "
                                    f"cleanup ({len(stale)} packs) failed: {e}"
                                )
                    entry["availablePacks"] = available
                except Exception:
                    pass
            out_jobs.append(entry)
        return {"ok": True, "jobs": out_jobs}

    def cloud_compute_resume_download(self, job_id: str) -> dict:
        """Resume pack download + merge for an existing persisted job.

        Registers the job in the in-memory ``_cc_jobs`` map (so the cloud
        queue panel can render it), starts the standard background remote
        poller, and spawns a one-off worker that downloads + merges any
        packs not already present locally. Status is only marked ``done``
        when the Worker confirms ``status==complete`` AND every available
        pack has been downloaded — otherwise the live poller keeps tracking
        and the UI reflects real state."""
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

        import threading as _t

        # Register in-memory state so the cloud queue panel renders this job
        # even though it predates the current process. The remote poller will
        # populate the cached counters within one tick.
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
        _retry_errored = bool(isinstance(_snapshot, dict) and _snapshot.get("retry_errored"))
        with self._ensure_cc_lock():
            if job_id not in self._cc_jobs:
                self._cc_jobs[job_id] = {
                    "jobId": job_id,
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
                    "pause_event": None,
                    "remote": dict(self._CC_REMOTE_DEFAULTS),
                }
        # Start the live remote poller; safe to call again if already running.
        self._cc_start_remote_poller(job_id)

        def _worker() -> None:
            pack_dir = folder / ".kestrel" / "cloud-packs"
            pack_dir.mkdir(parents=True, exist_ok=True)
            # Merged-set truth = union(folder-local cloud_folder_state,
            # legacy desktop-store downloadedPacks). Folder-local is the
            # post-fix authoritative source; the legacy field stays so old
            # jobs that pre-date the folder-state file still dedup correctly.
            try:
                from cloud_folder_state import list_merged_packs as _list_merged
                merged_in_folder = set(_list_merged(folder, job_id))
            except Exception:
                merged_in_folder = set()
            already = set(target.get("downloadedPacks") or []) | merged_in_folder
            try:
                files = client.list_results(job_id)
            except Exception as e:
                with self._ensure_cc_lock():
                    self._cc_pack_events.append({
                        "jobId": job_id, "folderPath": str(folder),
                        "packName": None, "error": str(e),
                    })
                return
            available_pack_names = [
                str(meta.get("filename") or "") for meta in files
                if str(meta.get("filename") or "").endswith(".zip")
            ]
            # Stale-R2 cleanup: any pack still in R2 that we've already merged
            # is dead weight (probably from a pre-folder-state job, or a prior
            # delete-packs call that lost network). Tell the Worker to drop it
            # so the next list_results stops returning them.
            stale = [n for n in available_pack_names if n in already]
            if stale and client is not None:
                try:
                    client.delete_packs(job_id, stale)
                    available_pack_names = [n for n in available_pack_names if n not in already]
                except Exception as e:
                    warn(f"[cloud-compute] {job_id}: stale-R2 cleanup ({len(stale)} packs) failed: {e}")
            for fname in available_pack_names:
                if fname in already:
                    continue
                dest = pack_dir / fname
                # Filesystem fallback dedup: if the pack zip is already on disk
                # but missing from `downloadedPacks`, we previously re-downloaded
                # it. That happens when the JSON ledger was killed mid-write
                # (atomic-replace race) or `add_downloaded_pack` swallowed an
                # exception. Re-merging is safe (the database merge is
                # last-wins by filename) and skipping the network call is the
                # whole point — repair the JSON so the next launch isn't
                # confused either.
                if dest.exists() and dest.stat().st_size > 0:
                    try:
                        ccc.merge_pack_into_kestrel(
                            dest, folder,
                            protected_filenames=set(anchor_filenames) if anchor_filenames else None,
                            overwrite_errors=_retry_errored,
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
                        overwrite_errors=_retry_errored,
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

            # Only mark `done` when Worker confirms terminal-complete state
            # AND every available pack has been pulled locally. Otherwise
            # leave the persisted status untouched — the live poller (and
            # subsequent app launches) will keep observing reality.
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

        _t.Thread(target=_worker, name=f"cc-resume-{job_id}", daemon=True).start()
        return {"ok": True, "jobId": job_id}

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
        existing_perch_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict:
        """Kick off an async upload. Returns immediately with `{job_id}`.

        JS polls `get_share_progress(job_id)` for live state. The browser is NOT
        opened automatically — the user clicks an "Open in browser" button on
        the success card so the auto-redirect-during-work pattern is gone.

        ``skip_rejected``: when True (default), CSV rows marked culled are
        omitted from the upload. The dialog defaults this to True; the user
        can uncheck "Skip rejected photos" in the dialog to override.

        ``existing_perch_id`` / ``idempotency_key`` (Phase 2 resume): when set,
        the uploader reuses the given perch instead of creating a new one and
        skips R2 PUTs for assets the server already reports as committed. The
        client passes both from a stored ``perch_upload_manifest.json``.
        """
        try:
            from perch_uploader import PerchKestrelUploader
        except ImportError:
            try:
                from analyzer.perch_uploader import PerchKestrelUploader
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
                    existing_perch_id=(str(existing_perch_id) if existing_perch_id else None),
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
            # Definite delete — clear local link AND any pending upload manifest.
            try:
                link_path.unlink()
            except OSError:
                pass
            try:
                self._perch_manifest_path(str(root_real)).unlink()
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

    # ─── Resumable upload helpers (Phase 2) ────────────────────────────────

    @staticmethod
    def _perch_manifest_path(folder_path: str) -> "Path":
        from pathlib import Path as _P
        return _P(folder_path) / ".kestrel" / "perch_upload_manifest.json"

    def detect_resumable_upload(self, folder_path: str) -> dict:
        """Inspect ``.kestrel/perch_upload_manifest.json`` and reconcile against
        the server's authoritative upload state.

        Returns one of:
          {"present": False}                                  — no manifest
          {"present": True, "status": "deleted"}              — server 404; manifest cleared
          {"present": True, "status": "unauthorized"}         — 401; left alone
          {"present": True, "status": "forbidden"}            — 403; left alone
          {"present": True, "status": "unreachable", "error"} — network error
          {"present": True, "status": "complete", ...}        — everything committed
          {"present": True, "status": "resumable", "perch_id", "perch_url",
           "idempotency_key", "title", "total", "committed", "pending"}
        """
        root_real, err = self._validate_root_dir(
            folder_path, context="detect_resumable_upload", require_exists=True
        )
        if err:
            return {"present": False, "error": err}
        manifest_path = self._perch_manifest_path(str(root_real))
        if not manifest_path.is_file():
            return {"present": False}

        import json as _json
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = _json.load(f)
        except Exception as e:
            return {"present": False, "error": f"manifest unreadable: {e}"}

        perch_id = str((manifest or {}).get("perch_id") or "").strip()
        if not perch_id:
            return {"present": False, "error": "manifest has no perch_id"}

        token, dev_user, terr = self._check_auth_token()
        if terr:
            return {"present": True, "status": "unauthorized", "manifest": manifest}

        try:
            import requests as _req
            headers: dict = {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            if dev_user:
                headers["x-dev-user-id"] = str(dev_user)
            r = _req.get(
                f"{self.get_perch_api_base()}/v1/perches/{perch_id}/upload-state",
                headers=headers,
                timeout=15,
            )
        except Exception as e:
            return {"present": True, "status": "unreachable", "error": str(e), "manifest": manifest}

        if r.status_code == 404:
            try:
                manifest_path.unlink()
            except OSError:
                pass
            return {"present": True, "status": "deleted", "perch_id": perch_id}
        if r.status_code == 401:
            return {"present": True, "status": "unauthorized", "manifest": manifest}
        if r.status_code == 403:
            return {"present": True, "status": "forbidden", "manifest": manifest}
        if not r.ok:
            return {
                "present": True, "status": "unreachable",
                "error": f"HTTP {r.status_code}", "manifest": manifest,
            }

        body = r.json()
        server_assets = body.get("assets") or []
        committed = sum(1 for a in server_assets if a.get("uploadState") == "committed")
        pending = sum(1 for a in server_assets if a.get("uploadState") == "pending")
        total = committed + pending

        if total > 0 and pending == 0:
            return {
                "present": True,
                "status": "complete",
                "perch_id": perch_id,
                "perch_url": str(manifest.get("perch_url") or ""),
                "total": total,
                "committed": committed,
                "pending": 0,
            }
        return {
            "present": True,
            "status": "resumable",
            "perch_id": perch_id,
            "perch_url": str(manifest.get("perch_url") or ""),
            "idempotency_key": str(manifest.get("idempotency_key") or ""),
            "title": str(manifest.get("title") or ""),
            "total": total,
            "committed": committed,
            "pending": pending,
        }

    def discard_resumable_upload(self, folder_path: str) -> dict:
        """Delete the local manifest AND ``DELETE`` the partial perch on the
        server — used by the dialog's "Start over" button. Best-effort: if the
        Worker call fails the manifest is still removed so the user isn't stuck."""
        root_real, err = self._validate_root_dir(
            folder_path, context="discard_resumable_upload", require_exists=True
        )
        if err:
            return {"success": False, "error": err}
        manifest_path = self._perch_manifest_path(str(root_real))
        if not manifest_path.is_file():
            return {"success": True, "removed": False}
        import json as _json
        perch_id = ""
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = _json.load(f)
            perch_id = str((manifest or {}).get("perch_id") or "").strip()
        except Exception:
            pass

        worker_error: Optional[str] = None
        if perch_id:
            token, dev_user, terr = self._check_auth_token()
            if terr is None:
                try:
                    import requests as _req
                    headers: dict = {}
                    if token:
                        headers["Authorization"] = f"Bearer {token}"
                    if dev_user:
                        headers["x-dev-user-id"] = str(dev_user)
                    dr = _req.delete(
                        f"{self.get_perch_api_base()}/v1/perches/{perch_id}",
                        headers=headers,
                        timeout=15,
                    )
                    if not dr.ok and dr.status_code != 404:
                        worker_error = f"HTTP {dr.status_code}"
                except Exception as e:
                    worker_error = str(e)

        try:
            manifest_path.unlink()
        except OSError as e:
            return {"success": False, "error": f"manifest unlink failed: {e}", "worker_error": worker_error}

        return {"success": True, "removed": True, "perch_id": perch_id, "worker_error": worker_error}

    # ─── Sync (Phase 3) ────────────────────────────────────────────────

    def compute_state_hash(self, folder_path: str) -> dict:
        """Hash kestrel_database.csv + kestrel_scenedata.json. JS uses this to
        decide whether the Sync button should be greyed out (clean) or not."""
        root_real, err = self._validate_root_dir(folder_path, context="compute_state_hash", require_exists=True)
        if err:
            return {"success": False, "error": err}
        h = self._hash_kestrel_state(str(root_real))
        return {"success": True, "hash": h}

    def compute_sync_diff(self, folder_path: str) -> dict:
        """Read-only preview for the Sync modal. Returns the diff between the
        local folder and the server perch without applying anything.

        Errors surface with the same shape as ``share_with_perch`` failures
        so the JS layer can branch on `error: 'perch_deleted'` etc.
        """
        try:
            from perch_uploader import PerchKestrelUploader, _PerchDeleted
        except ImportError:
            try:
                from analyzer.perch_uploader import PerchKestrelUploader, _PerchDeleted
            except ImportError as e:
                return {"success": False, "error": f"uploader import failed: {e}"}

        token, dev_user, terr = self._check_auth_token()
        if terr:
            return terr

        root_real, verr = self._validate_root_dir(
            folder_path, context="compute_sync_diff", require_exists=True
        )
        if verr:
            return {"success": False, "error": verr}

        try:
            uploader = PerchKestrelUploader(
                self.get_perch_api_base(),
                str(token) if token else None,
                dev_user=dev_user,
            )
            diff = uploader.compute_sync_diff(str(root_real))
        except _PerchDeleted as e:
            # Server says the perch is gone — clear local state so the folder
            # card flips back to its un-published affordance.
            try:
                self._perch_link_path(str(root_real)).unlink()
            except OSError:
                pass
            try:
                self._perch_manifest_path(str(root_real)).unlink()
            except OSError:
                pass
            return {"success": False, "error": "perch_deleted", "perch_id": e.perch_id}
        except FileNotFoundError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            log(f"compute_sync_diff: {e}")
            return {"success": False, "error": str(e)}

        return {"success": True, "diff": diff}

    def sync_to_perch(self, folder_path: str) -> dict:
        """Kick off an async sync job. Returns immediately with `{job_id}`.
        JS polls via the same ``get_share_progress`` infrastructure used for
        uploads, so the in-flight Perch-uploads card pattern is reused.
        """
        try:
            from perch_uploader import PerchKestrelUploader, _PerchDeleted
        except ImportError:
            try:
                from analyzer.perch_uploader import PerchKestrelUploader, _PerchDeleted
            except ImportError as e:
                return {"success": False, "error": f"uploader import failed: {e}"}

        token, dev_user, terr = self._check_auth_token()
        if terr:
            return terr

        root_real, verr = self._validate_root_dir(
            folder_path, context="sync_to_perch", require_exists=True
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
            self._share_jobs[job_id] = {
                "progress": {"phase": "starting", "kind": "sync"},
                "cancel_event": cancel_event,
                "thread": None,
            }
            self._active_share_job = job_id

        def _on_progress(payload: dict) -> None:
            with lock:
                if job_id in self._share_jobs:
                    p = dict(payload)
                    p["kind"] = "sync"  # tag the job kind so JS can branch
                    self._share_jobs[job_id]["progress"] = p

        def _runner() -> None:
            try:
                uploader = PerchKestrelUploader(
                    self.get_perch_api_base(),
                    str(token) if token else None,
                    dev_user=dev_user,
                )
                result = uploader.sync_to_perch(
                    str(root_real),
                    progress_callback=_on_progress,
                    cancel_event=cancel_event,
                )
                # On a successful (full or partial) sync, refresh the link's
                # state hash so the Sync button greys out "Up to date" until
                # the next local edit.
                if result and result.get("ok") and not result.get("canceled"):
                    try:
                        link_path = self._perch_link_path(str(root_real))
                        if link_path.is_file():
                            with open(link_path, "r", encoding="utf-8") as f:
                                import json as _json
                                link = _json.load(f)
                            link["state_hash_at_upload"] = self._hash_kestrel_state(str(root_real))
                            link["last_synced_at_ms"] = int(time.time() * 1000)
                            tmp = link_path.with_suffix(".json.tmp")
                            with open(tmp, "w", encoding="utf-8") as f:
                                _json.dump(link, f, indent=2)
                            os.replace(tmp, link_path)
                    except Exception as up_err:
                        log(f"sync_to_perch: link update failed: {up_err}")
            except _PerchDeleted as e:
                try:
                    self._perch_link_path(str(root_real)).unlink()
                except OSError:
                    pass
                try:
                    self._perch_manifest_path(str(root_real)).unlink()
                except OSError:
                    pass
                _on_progress({"phase": "error", "message": "perch_deleted", "perch_id": e.perch_id})
            except Exception as e:
                log(f"sync_to_perch: {e}")
                import traceback as _tb
                log(_tb.format_exc())
                _on_progress({"phase": "error", "message": str(e)})
            finally:
                with lock:
                    if self._active_share_job == job_id:
                        self._active_share_job = None
                # Sync may have changed asset metadata — invalidate caches.
                self._perch_usage_cache = None
                self._perch_usage_cache_at = 0.0

        import threading as _t
        thread = _t.Thread(target=_runner, name=f"PerchSync-{job_id[:8]}", daemon=True)
        with lock:
            self._share_jobs[job_id]["thread"] = thread
        thread.start()

        return {"success": True, "job_id": job_id}

    def open_auth_sign_in(self, url):
        """Open a pywebview window for desktop Perch sign-in."""
        try:
            if not WEBVIEW_IMPORT_SUCCESS:
                return {'success': False, 'error': 'pywebview not available'}
            import webview as _wv
            if self._sign_in_window:
                try:
                    self._sign_in_window.destroy()
                except Exception:
                    pass
                self._sign_in_window = None
            win = _wv.create_window(
                'Sign In to Project Kestrel',
                url,
                js_api=self,   # same Api instance — store_auth_token is accessible
                width=520,
                height=700,
                resizable=False,
            )
            self._sign_in_window = win
            return {'success': True}
        except Exception as e:
            print(f'[API] open_auth_sign_in() -> Error: {e}', flush=True)
            return {'success': False, 'error': str(e)}

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
