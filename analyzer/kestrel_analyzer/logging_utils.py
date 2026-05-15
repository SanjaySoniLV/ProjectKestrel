import json
import os
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .config import KESTREL_DIR_NAME, LOG_FILENAME_PREFIX, LOG_FILE_EXTENSION

# Re-export the leveled console logger from ``settings_utils`` so any module
# inside the kestrel_analyzer subpackage (including ml/) can do
# ``from ..logging_utils import debug, info, warn, error`` without dealing
# with the package-depth or fallback-import dance themselves. The structured
# JSON channel (``log_event`` / ``log_warning`` / ``log_exception`` below) is
# unrelated and lives alongside.
try:
    from ..settings_utils import debug, info, warn, error  # type: ignore  # noqa: F401
except (ImportError, ValueError):
    # cli.py imports kestrel_analyzer as a top-level package, so the relative
    # ``..settings_utils`` walks above the package root and raises. The bare
    # import works because ``analyzer/`` is on sys.path in that case.
    try:
        from settings_utils import debug, info, warn, error  # type: ignore  # noqa: F401
    except ImportError:
        def debug(*_a, **_kw): pass  # type: ignore[no-redef]
        def info(*_a, **_kw): pass   # type: ignore[no-redef]
        def warn(*_a, **_kw): pass   # type: ignore[no-redef]
        def error(*_a, **_kw): pass  # type: ignore[no-redef]


def _utc_timestamp() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _file_timestamp() -> str:
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def resolve_log_dir(folder: Optional[str]) -> str:
    candidates = []
    if folder:
        candidates.append(Path(folder) / KESTREL_DIR_NAME)
    candidates.append(Path.home() / KESTREL_DIR_NAME)

    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return str(candidate)
        except Exception:
            continue

    return str(Path.cwd())


def get_log_path(folder: Optional[str], session_id: Optional[str] = None) -> str:
    log_dir = resolve_log_dir(folder)
    session_id = session_id or _file_timestamp()
    filename = f"{LOG_FILENAME_PREFIX}_{session_id}.{LOG_FILE_EXTENSION}"
    return os.path.join(log_dir, filename)


def _read_log_entries(log_path: str) -> list:
    if not os.path.exists(log_path):
        return []
    try:
        with open(log_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def log_event(log_path: str, entry: Dict[str, Any]) -> None:
    entry_with_time = {"timestamp_utc": _utc_timestamp(), **entry}
    entries = _read_log_entries(log_path)
    entries.append(entry_with_time)
    with open(log_path, "w", encoding="utf-8") as handle:
        json.dump(entries, handle, indent=2)


def log_exception(
    log_path: str,
    exc: Exception,
    stage: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
    level: str = "error",
) -> None:
    log_event(
        log_path,
        {
            "level": level,
            "stage": stage,
            "context": context or {},
            "exception_type": type(exc).__name__,
            "exception_message": str(exc),
            "traceback": traceback.format_exc(),
        },
    )


def log_warning(
    log_path: str,
    message: Any,
    category: Optional[type] = None,
    filename: Optional[str] = None,
    lineno: Optional[int] = None,
    stage: Optional[str] = None,
    context: Optional[Dict[str, Any]] = None,
) -> None:
    log_event(
        log_path,
        {
            "level": "warning",
            "stage": stage,
            "context": context or {},
            "message": str(message),
            "category": category.__name__ if category else None,
            "filename": filename,
            "lineno": lineno,
        },
    )