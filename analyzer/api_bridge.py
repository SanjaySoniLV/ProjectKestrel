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

    def choose_directories(self):
        """Open native folder picker dialog with multi-select support.

        Returns: list[str] of selected folder paths (empty list if cancelled).

        Platform-specific:
        - macOS: osascript's "choose folder ... with multiple selections allowed"
          is native and reliable.
        - Windows/Linux: tkinter's askdirectory is single-select only with no
          plural equivalent. We try pywebview's create_file_dialog with
          allow_multiple=True first (in-process, no subprocess). If pywebview
          isn't accessible we fall back to a single-pick loop — call
          choose_directory once and return [path].
        """
        try:
            if sys.platform == 'darwin':
                # AppleScript returns a list of alias references separated by ", "
                # when multiple selection is enabled. POSIX path of {…} converts
                # each to a slash-path string.
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

            # Try pywebview's native multi-select dialog (Windows / Linux).
            try:
                import webview  # noqa: F401  (only used to access the windows registry)
                wins = getattr(webview, 'windows', None)
                if wins:
                    win = wins[0]
                    result = win.create_file_dialog(
                        webview.FOLDER_DIALOG,
                        allow_multiple=True,
                    )
                    if not result:
                        info('[API] choose_directories -> cancelled')
                        return []
                    # create_file_dialog returns a tuple of paths
                    paths = [str(p) for p in result if p]
                    info(f'[API] choose_directories (pywebview) -> {len(paths)} path(s)')
                    return paths
            except Exception as e:
                # Fall through to single-pick fallback if pywebview path fails.
                info(f'[API] choose_directories pywebview path failed ({e!r}); falling back to single-pick')

            # Fallback: single-pick via tkinter. User can repeat the action if they
            # want multiple folders — better UX than nothing.
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
