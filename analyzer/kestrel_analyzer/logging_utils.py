import json
import os
import threading
import traceback
from datetime import datetime, timezone
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


_log_write_lock = threading.RLock()


def utc_now_naive() -> datetime:
    """UTC now as a naive datetime.

    The deprecated naive-UTC constructor emits DeprecationWarning on
    Python 3.12+. If a ``warnings.showwarning`` hook logs via
    :func:`log_warning` (which stamps the entry with this helper), that
    warning would re-enter the hook and raise RecursionError.
    ``datetime.now(timezone.utc)`` is silent.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_timestamp() -> str:
    return utc_now_naive().isoformat() + "Z"


def _file_timestamp() -> str:
    return utc_now_naive().strftime("%Y%m%dT%H%M%SZ")


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


def parse_log_text(text: str) -> list:
    """Parse analysis-log text in either on-disk format.

    Two formats exist in the wild and both must stay readable:

    * **JSONL** (current) — one JSON object per line. An unparseable line is
      skipped rather than failing the file, so a torn final line from a hard
      process death costs one entry instead of the whole log.
    * **JSON array** (written before the JSONL switch) — a single
      pretty-printed list. Installed builds still hold these, and crash
      reports uploaded from them must keep parsing.

    The formats are told apart by the first non-whitespace character: a
    legacy file starts with ``[``. Objects in a legacy file are indented
    across several lines, so line-wise parsing cannot recover a *truncated*
    array; that case still yields ``[]``, exactly as it did before. Avoiding
    that unrecoverable shape is the reason for the change.
    """
    stripped = text.lstrip()
    if not stripped:
        return []

    if stripped[0] == "[":
        try:
            data = json.loads(stripped)
        except Exception:
            return []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            # A partially-written final line, or foreign content. Skip it and
            # keep every entry that did land.
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    return entries


def read_log_entries(log_path: str) -> list:
    """Read an analysis log into a list of entries, oldest first.

    Tolerates both on-disk formats (see :func:`parse_log_text`) and returns
    ``[]`` for a missing or unreadable file rather than raising — callers are
    diagnostics paths that must not fail because a log is absent.
    """
    if not os.path.exists(log_path):
        return []
    try:
        with open(log_path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except Exception:
        return []
    return parse_log_text(text)


# Retained under the old private name: it was the only reader before the JSONL
# switch, and keeping the alias means any out-of-tree caller keeps working.
_read_log_entries = read_log_entries


def log_event(log_path: str, entry: Dict[str, Any]) -> None:
    """Append one entry to the analysis log.

    The log is JSONL — one JSON object per line, opened append-only — so a
    write is O(1) in the number of entries already recorded.

    It was previously a single JSON array that had to be read, re-serialised
    and rewritten on *every* call, making a run O(n^2) in the number of
    entries. That cost is why only the four once-per-run setup stages were
    ever persisted: marking the per-image stages would have made analysis
    quadratic in file count.

    Serialisation uses ``default=str`` so an object pandas or numpy handed us
    degrades to its string form instead of raising and discarding the entry —
    these records exist to explain failures, and losing one to a type error
    defeats the point. IO errors still propagate: callers on the crash path
    (``_mark_stage``, ``_log_resolved_providers``) swallow them deliberately,
    and silently absorbing them here would hide a misconfigured log directory
    from every caller.
    """
    entry_with_time = {"timestamp_utc": _utc_timestamp(), **entry}
    line = json.dumps(entry_with_time, default=str)
    # ``json.dumps`` escapes any newline inside a value, so one entry is always
    # exactly one physical line. Worker threads can reach the warning hook
    # concurrently; the lock keeps their lines from interleaving mid-write.
    with _log_write_lock:
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


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


def make_logged_showwarning(
    log_path: str,
    stage_ctx: Dict[str, Any],
    folder: Optional[str] = None,
    original_showwarning=None,
):
    """Build a ``warnings.showwarning`` hook that writes to the JSON log.

    The hook is re-entrancy-safe: if logging itself emits a warning (the
    historical ``datetime.utcnow`` DeprecationWarning, or any other), the
    nested call skips JSON logging instead of stacking until RecursionError.
    Nested warnings are still forwarded to ``original_showwarning`` so they
    are not silently dropped.

    The guard is thread-local: ``warnings.showwarning`` is process-global, and
    decoder/worker threads can warn concurrently. A plain closure boolean
    would drop or interleave those unrelated warnings.
    """
    state = threading.local()

    def _showwarning(message, category, filename, lineno, file=None, line=None):
        if getattr(state, "in_handler", False):
            if original_showwarning is not None:
                original_showwarning(
                    message, category, filename, lineno, file=file, line=line
                )
            return
        state.in_handler = True
        try:
            log_warning(
                log_path,
                message,
                category=category,
                filename=filename,
                lineno=lineno,
                stage=stage_ctx.get("stage"),
                context={"file": stage_ctx.get("file"), "folder": folder},
            )
            if original_showwarning:
                original_showwarning(
                    message, category, filename, lineno, file=file, line=line
                )
        finally:
            state.in_handler = False

    return _showwarning


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