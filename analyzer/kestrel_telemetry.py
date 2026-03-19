"""
Project Kestrel — Telemetry Module

Handles all outbound communication with the Cloudflare Worker API:
  - Feedback / bug reports
  - Crash reports
  - Anonymous per-folder usage analytics

DESIGN RULES:
  1. Every public function is **failsafe** — never raises, never blocks the UI.
  2. All HTTP calls run in daemon threads with a 10-second timeout.
  3. No personally identifiable data is ever sent (no filenames, paths, image data).
  4. Analytics are only sent when the user has explicitly opted in.
"""

import hashlib
import json
import os
import platform
import sys
import threading
import traceback
import uuid
from typing import Any, Dict, List, Optional

# Attempt to import urllib — should always succeed (stdlib)
try:
    import urllib.request
    import urllib.error
except ImportError:
    urllib = None  # type: ignore[assignment]

import ssl
import certifi # ensure we have a CA bundle for HTTPS requests, even in frozen/packaged environments
# ---------------------------------------------------------------------------
# Configuration — the shared secret and endpoint URL
# ---------------------------------------------------------------------------
KESTREL_API_URL = "https://api.projectkestrel.org"  # production endpoint
#KESTREL_API_URL = "http://127.0.0.1:8787"  # local testing endpoint
KESTREL_SHARED_SECRET = "kestrel_secret_dev_shared"  # basic abuse-prevention

_TIMEOUT_SECONDS = 10
_MAX_LOG_ENTRIES = 50
_MAX_SCREENSHOT_BYTES = 2 * 1024 * 1024  # 2 MB cap for screenshot payloads


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_version() -> str:
    """Read the version string from VERSION.txt (failsafe)."""
    try:
        # Check relative to this file, then one level up (repo root)
        for candidate in [
            os.path.join(os.path.dirname(__file__), 'VERSION.txt'),
            os.path.join(os.path.dirname(__file__), '..', 'VERSION.txt'),
        ]:
            if os.path.isfile(candidate):
                with open(candidate, 'r', encoding='utf-8') as f:
                    for line in f:
                        if line.strip().lower().startswith('version:'):
                            return line.strip().split(':', 1)[1].strip()
        return 'unknown'
    except Exception:
        return 'unknown'


def _get_os_info() -> str:
    """Return a short OS description (e.g. 'Windows-10-x86_64')."""
    try:
        return f"{platform.system()}-{platform.release()}-{platform.machine()}"
    except Exception:
        return 'unknown'


def get_machine_id(settings: dict) -> str:
    """Return a stable, random machine identifier.

    If ``machine_id`` already exists in *settings*, return it.
    Otherwise generate a new UUID4, store it in *settings*, and return it.

    The caller is responsible for persisting the settings dict to disk.
    """
    try:
        mid = settings.get('machine_id')
        if mid and isinstance(mid, str) and len(mid) > 8:
            return mid
        mid = str(uuid.uuid4())
        settings['machine_id'] = mid
        return mid
    except Exception:
        return 'unknown'

def _get_ssl_context():
    """Return an SSL context using certifi's CA bundle (required for frozen macOS apps)."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    return ctx

def _post_json(endpoint: str, payload: dict) -> None:
    """POST JSON to the Cloudflare Worker (fire-and-forget, failsafe)."""
    if urllib is None:
        print('[telemetry] urllib not available — skipping POST', flush=True)
        return
    url = f"{KESTREL_API_URL}{endpoint}"
    try:
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'X-Kestrel-Key': KESTREL_SHARED_SECRET,
                'User-Agent': 'Mozilla/5.0 (KestrelTelemetry/1.0)',
            },
            method='POST',
        )
        print(f'[telemetry] POST {url}  key={KESTREL_SHARED_SECRET[:8]}…', flush=True)
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS, context=_get_ssl_context()) as resp:
            body = resp.read().decode('utf-8', errors='replace')
            print(f'[telemetry] → {resp.status} {body[:200]}', flush=True)
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace') if hasattr(e, 'read') else ''
        print(f'[telemetry] HTTP {e.code} from {url}: {body[:300]}', flush=True)
    except urllib.error.URLError as e:
        print(f'[telemetry] URLError posting to {url}: {e.reason}', flush=True)
    except Exception as e:
        print(f'[telemetry] Error posting to {url}: {e}', flush=True)


def _post_json_async(endpoint: str, payload: dict) -> None:
    """Fire-and-forget POST in a daemon thread (never blocks the caller)."""
    try:
        t = threading.Thread(target=_post_json, args=(endpoint, payload), daemon=True)
        t.start()
    except Exception:
        pass


def _hash_folder_name(folder_path: str) -> str:
    """Return a one-way hash of the folder *name* (not full path) for analytics."""
    try:
        name = os.path.basename(folder_path.rstrip('/\\'))
        return hashlib.sha256(name.encode('utf-8')).hexdigest()[:12]
    except Exception:
        return ''


# ---------------------------------------------------------------------------
# Public API: Feedback
# ---------------------------------------------------------------------------

def send_feedback(
    report_type: str,
    description: str,
    contact: str = '',
    screenshot_b64: str = '',
    log_tail: str = '',
    machine_id: str = '',
    version: str = '',
) -> None:
    """Send a feedback / bug report to the Cloudflare Worker (async, failsafe).

    Parameters
    ----------
    report_type : str
        One of 'bug', 'suggestion', 'liked', 'general'.
    description : str
        User-supplied description text.
    contact : str
        Optional email / contact info.
    screenshot_b64 : str
        Optional base64-encoded screenshot PNG.
    log_tail : str
        Optional stringified recent log entries.
    machine_id, version : str
        Machine identifier and app version.
    """
    try:
        # Enforce a size cap on large payloads
        if len(screenshot_b64) > _MAX_SCREENSHOT_BYTES:
            screenshot_b64 = ''  # silently drop oversized screenshots

        payload = {
            'type': report_type or 'general',
            'description': description or '',
            'contact': contact or '',
            'screenshot_b64': screenshot_b64,
            'log_tail': log_tail,
            'machine_id': machine_id,
            'version': version or _read_version(),
            'os': _get_os_info(),
        }
        _post_json_async('/api/feedback', payload)
    except Exception:
        pass  # failsafe


# ---------------------------------------------------------------------------
# Public API: Crash Reports
# ---------------------------------------------------------------------------

def send_crash_report(
    exc: Optional[Exception] = None,
    tb_str: str = '',
    log_tail: str = '',
    session_analytics: Optional[dict] = None,
    machine_id: str = '',
    version: str = '',
) -> None:
    """Send a crash report to the Cloudflare Worker (async, failsafe).

    Parameters
    ----------
    exc : Exception, optional
        The exception object.
    tb_str : str
        Pre-formatted traceback string.
    log_tail : str
        Optional recent log entries.
    session_analytics : dict, optional
        Any analytics data collected so far in this session.
    machine_id, version : str
        Machine identifier and app version.
    """
    try:
        exc_type = type(exc).__name__ if exc else 'Unknown'
        exc_msg = str(exc) if exc else ''
        if not tb_str:
            try:
                tb_str = traceback.format_exc()
            except Exception:
                tb_str = ''
        payload = {
            'exception_type': exc_type,
            'exception_message': exc_msg,
            'traceback': tb_str,
            'log_tail': log_tail,
            'session_analytics': session_analytics,
            'machine_id': machine_id,
            'version': version or _read_version(),
            'os': _get_os_info(),
        }
        _post_json_async('/api/crash', payload)
    except Exception:
        pass  # failsafe


# ---------------------------------------------------------------------------
# Public API: Non-Optional Telemetry (Installation/Completion)
# ---------------------------------------------------------------------------

def send_installation_telemetry(machine_id: str, version: str = '') -> None:
    """Send a notification when the software is installed (failsafe)."""
    try:
        payload = {
            'machine_id': machine_id,
            'version': version or _read_version(),
            'os': _get_os_info(),
            'platform': platform.platform(),
        }
        _post_json_async('/api/install', payload)
    except Exception:
        pass


def send_analysis_completion_telemetry(
    files_analyzed: int,
    machine_id: str = '',
    version: str = '',
    avg_time_per_file_s: float = 0.0,
) -> None:
    """Send total photos analyzed upon completion (non-optional, failsafe)."""
    try:
        payload = {
            'machine_id': machine_id,
            'version': version or _read_version(),
            'os': _get_os_info(),
            'files_analyzed': files_analyzed,
            'avg_time_per_file_s': round(avg_time_per_file_s, 3),
        }
        _post_json_async('/api/completion', payload)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API: Analytics
# ---------------------------------------------------------------------------

def send_folder_analytics(
    folder_path: str,
    files_analyzed: int,
    total_files: int,
    active_compute_time_s: float,
    file_sizes_kb: List[float],
    file_formats: Dict[str, int],
    was_cancelled: bool = False,
    machine_id: str = '',
    version: str = '',
) -> None:
    """Send per-folder analytics after analysis completes (async, failsafe).

    Only called when the user has opted in.

    Parameters
    ----------
    folder_path : str
        Used only to create a one-way hash of the folder name.
    files_analyzed : int
        Number of NEW files analyzed in this session (not previously analyzed).
    total_files : int
        Total files in the folder.
    active_compute_time_s : float
        Wall-clock seconds of active analysis (excludes paused time).
    file_sizes_kb : list[float]
        Sizes of the analyzed files in KB.
    file_formats : dict[str, int]
        Extension -> count mapping (e.g. {'.CR3': 5, '.jpg': 3}).
    was_cancelled : bool
        Whether the analysis was cancelled before completion.
    machine_id, version : str
        Machine identifier and app version.
    """
    try:
        avg_size = sum(file_sizes_kb) / len(file_sizes_kb) if file_sizes_kb else 0
        avg_speed = (active_compute_time_s * 1000 / files_analyzed) if files_analyzed > 0 else 0

        payload = {
            'machine_id': machine_id,
            'version': version or _read_version(),
            'os': _get_os_info(),
            'folder_name_hash': _hash_folder_name(folder_path),
            'files_analyzed': files_analyzed,
            'avg_file_size_kb': round(avg_size, 1),
            'file_sizes_kb': file_sizes_kb,
            'avg_analysis_speed_ms': round(avg_speed, 1),
            'file_formats': file_formats,
            'active_compute_time_s': round(active_compute_time_s, 1),
            'was_cancelled': was_cancelled,
        }
        _post_json_async('/api/analytics', payload)
    except Exception:
        pass  # failsafe


# ---------------------------------------------------------------------------
# Public API: Log Tail
# ---------------------------------------------------------------------------

def get_recent_log_tail(folder: Optional[str] = None, max_entries: int = _MAX_LOG_ENTRIES) -> str:
    """Read the most recent pipeline log file and return a truncated string.

    Completely failsafe — returns an empty string if anything goes wrong
    (missing file, parse error, permission denied, etc.).

    Parameters
    ----------
    folder : str or None
        Folder that was being analyzed (log files live in ``<folder>/.kestrel/``).
        If None, tries the user home ``~/.kestrel/`` directory.
    max_entries : int
        Maximum number of log entries to include (most recent first).

    Returns
    -------
    str
        JSON-formatted string of the last N log entries, or ''.
    """
    try:
        from kestrel_analyzer.config import KESTREL_DIR_NAME, LOG_FILENAME_PREFIX, LOG_FILE_EXTENSION
    except ImportError:
        try:
            # Fallback: import from relative path
            from analyzer.kestrel_analyzer.config import KESTREL_DIR_NAME, LOG_FILENAME_PREFIX, LOG_FILE_EXTENSION
        except ImportError:
            # Cannot import config — use defaults
            KESTREL_DIR_NAME = '.kestrel'
            LOG_FILENAME_PREFIX = 'kestrel_log'
            LOG_FILE_EXTENSION = 'json'

    try:
        # Build list of candidate log directories
        candidates = []
        if folder:
            candidates.append(os.path.join(folder, KESTREL_DIR_NAME))
        candidates.append(os.path.join(os.path.expanduser('~'), KESTREL_DIR_NAME))

        # Find the most recent log file across candidates
        best_path = None
        best_mtime = 0

        for log_dir in candidates:
            if not os.path.isdir(log_dir):
                continue
            try:
                for fname in os.listdir(log_dir):
                    if fname.startswith(LOG_FILENAME_PREFIX) and fname.endswith(f'.{LOG_FILE_EXTENSION}'):
                        fp = os.path.join(log_dir, fname)
                        try:
                            mt = os.path.getmtime(fp)
                            if mt > best_mtime:
                                best_mtime = mt
                                best_path = fp
                        except OSError:
                            continue
            except OSError:
                continue

        if not best_path:
            return ''

        with open(best_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        if not isinstance(data, list):
            return ''

        # Take the last N entries
        tail = data[-max_entries:]
        return json.dumps(tail, indent=2, default=str)

    except Exception:
        return ''  # failsafe — never raise


# ---------------------------------------------------------------------------
# Public API: Collect Per-Folder Stats from _QueueItem
# ---------------------------------------------------------------------------

def collect_folder_stats(item_path: str, files_this_session: int, total_files: int) -> Dict[str, Any]:
    """Collect file sizes and extension counts for a completed folder.

    This scans the raw image files in the folder to gather statistics.
    Completely failsafe — returns empty/default values on any error.

    Parameters
    ----------
    item_path : str
        Absolute path to the analyzed folder.
    files_this_session : int
        Number of NEW files analyzed in this session.
    total_files : int
        Total number of image files in the folder.

    Returns
    -------
    dict with keys: file_sizes_kb, file_formats
    """
    print("[telemetry] collect_folder_stats: item_path:", item_path, "files_this_session:", files_this_session, "total_files:", total_files)
    try:
        # Import known extensions
        try:
            from kestrel_analyzer.config import RAW_EXTENSIONS, JPEG_EXTENSIONS
        except ImportError:
            print("Failed to import kestrel_analyzer.config")
            try:
                from analyzer.kestrel_analyzer.config import RAW_EXTENSIONS, JPEG_EXTENSIONS
            except ImportError:
                print("Failed to import analyzer.kestrel_analyzer.config — using hardcoded extensions")
                # Keep these lists in sync with kestrel_analyzer/config.py (the source of truth).
                RAW_EXTENSIONS = {'.cr2', '.cr3', '.nef', '.arw', '.dng', '.orf', '.rw2', '.raf', '.pef', '.sr2', '.x3f', '.srw'}
                JPEG_EXTENSIONS = {'.jpg', '.jpeg', '.jpe', '.jfif', '.png', '.tiff', '.tif', '.bmp', '.webp'}
        
        file_sizes_kb: List[float] = []
        file_formats: Dict[str, int] = {}
        # Ensure we can combine lists or sets without raising a TypeError
        all_exts = {str(e).lower() for e in set(RAW_EXTENSIONS) | set(JPEG_EXTENSIONS)}

        # Debug: surface the path being scanned and loaded extension sets
        try:
            print(f"[telemetry debug] collect_folder_stats: item_path={item_path!r} files_this_session={files_this_session} total_files={total_files}", flush=True)
            print(f"[telemetry debug] RAW_EXTENSIONS={sorted(list(RAW_EXTENSIONS))}", flush=True)
            print(f"[telemetry debug] JPEG_EXTENSIONS={sorted(list(JPEG_EXTENSIONS))}", flush=True)
            print(f"[telemetry debug] all_exts={sorted(list(all_exts))}", flush=True)
        except Exception:
            pass

        # Non-recursive: only inspect files at the top level of the
        # provided folder to avoid scanning internal folders (e.g. .kestrel).
        MAX_ENTRIES = 1000
        try:
            entries = os.listdir(item_path)
        except Exception:
            entries = []

        try:
            print(f"[telemetry debug] listing top-level of {item_path!r} entries_count={len(entries)} sample={entries[:10]}", flush=True)
        except Exception:
            pass

        for fname in entries:
            if len(file_sizes_kb) >= MAX_ENTRIES:
                break
            fpath = os.path.join(item_path, fname)
            if not os.path.isfile(fpath):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in all_exts:
                continue
            try:
                size_kb = os.path.getsize(fpath) / 1024.0
                file_sizes_kb.append(round(size_kb, 1))
            except OSError:
                continue
            file_formats[ext] = file_formats.get(ext, 0) + 1

        try:
            print(f"[telemetry debug] matched_count={len(file_sizes_kb)} file_formats={file_formats}", flush=True)
        except Exception:
            pass

        return {
            'file_sizes_kb': file_sizes_kb,
            'file_formats': file_formats,
        }
    except Exception as e:
        print(f"[telemetry error] collect_folder_stats failed: {e}")
        return {'file_sizes_kb': [], 'file_formats': {}}
