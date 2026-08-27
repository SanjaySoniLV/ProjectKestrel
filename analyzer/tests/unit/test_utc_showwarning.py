"""Regression tests for the utcnow / showwarning RecursionError (F11).

On Python 3.12+, ``datetime.utcnow()`` emits DeprecationWarning. The analysis
pipeline installs a ``warnings.showwarning`` hook that logs via
``log_warning`` → ``_utc_timestamp``. If that stamp still called ``utcnow``,
the warning re-entered the hook until RecursionError (seen on the CLI
``process_folder`` path, including ``TestPipelineOverrides``).
"""

from __future__ import annotations

import ast
import json
import sys
import threading
import warnings
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.logging_utils import (  # noqa: E402
    _file_timestamp,
    _utc_timestamp,
    log_warning,
    make_logged_showwarning,
    utc_now_naive,
)
from queue_manager import _utc_timestamp as queue_utc_timestamp  # noqa: E402


pytestmark = pytest.mark.unit

_ANALYZER_ROOT = Path(__file__).resolve().parents[2]


def _utcnow_call_linenos(source: str) -> list[int]:
    """Line numbers of real ``utcnow()`` / ``datetime.utcnow()`` calls."""
    tree = ast.parse(source)
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "utcnow":
            lines.append(getattr(node, "lineno", 0))
        elif isinstance(func, ast.Name) and func.id == "utcnow":
            lines.append(getattr(node, "lineno", 0))
    return lines


def test_utc_now_naive_is_naive_and_close_to_utc():
    dt = utc_now_naive()
    assert dt.tzinfo is None
    aware = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs((aware - dt).total_seconds()) < 2.0


def test_utc_timestamp_keeps_naive_zulu_suffix():
    ts = _utc_timestamp()
    assert ts.endswith("Z")
    assert "+00:00" not in ts
    parsed = datetime.fromisoformat(ts[:-1])
    assert parsed.tzinfo is None


def test_file_timestamp_format():
    stamp = _file_timestamp()
    datetime.strptime(stamp, "%Y%m%dT%H%M%SZ")


def test_utc_helpers_do_not_emit_utcnow_deprecation():
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        utc_now_naive()
        _utc_timestamp()
        _file_timestamp()
        queue_utc_timestamp()
    utcnow = [w for w in recorded if "utcnow" in str(w.message).lower()]
    assert utcnow == []


def test_log_warning_does_not_recurse_under_error_filter(tmp_path):
    """Treat DeprecationWarning as error: logging must not raise."""
    log_path = str(tmp_path / "kestrel.log.json")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        log_warning(log_path, "hello", category=UserWarning, stage="unit")
    entries = json.loads(Path(log_path).read_text(encoding="utf-8"))
    assert entries[-1]["message"] == "hello"
    assert entries[-1]["timestamp_utc"].endswith("Z")


def test_logged_showwarning_is_reentrant_when_logging_emits(tmp_path, monkeypatch):
    """If log_warning itself warns, the hook must not RecursionError."""
    import kestrel_analyzer.logging_utils as logging_utils

    log_path = str(tmp_path / "kestrel.log.json")
    stage_ctx = {"stage": "startup", "file": None}
    original = warnings.showwarning
    hook = make_logged_showwarning(
        log_path,
        stage_ctx,
        folder=str(tmp_path),
        original_showwarning=None,
    )

    real_log_warning = logging_utils.log_warning

    def noisy_log_warning(*args, **kwargs):
        warnings.warn("nested warning from logger", UserWarning)
        return real_log_warning(*args, **kwargs)

    monkeypatch.setattr(logging_utils, "log_warning", noisy_log_warning)
    warnings.showwarning = hook
    try:
        warnings.warn("outer warning", UserWarning)
    finally:
        warnings.showwarning = original

    entries = json.loads(Path(log_path).read_text(encoding="utf-8"))
    messages = [e["message"] for e in entries]
    assert any("outer warning" in m for m in messages)


def test_logged_showwarning_does_not_drop_concurrent_threads(tmp_path, monkeypatch):
    """A shared boolean guard would skip the second thread's warning."""
    import kestrel_analyzer.logging_utils as logging_utils

    log_path = str(tmp_path / "kestrel.log.json")
    hook = make_logged_showwarning(
        log_path,
        {"stage": "decode", "file": None},
        original_showwarning=None,
    )
    real_log_warning = logging_utils.log_warning
    entered = threading.Event()
    release = threading.Event()
    seen: list[str] = []
    seen_lock = threading.Lock()

    def blocking_log_warning(*args, **kwargs):
        message = str(args[1] if len(args) > 1 else kwargs.get("message"))
        with seen_lock:
            seen.append(message)
        if "from-thread-1" in message:
            entered.set()
            assert release.wait(timeout=2.0)
        return real_log_warning(*args, **kwargs)

    monkeypatch.setattr(logging_utils, "log_warning", blocking_log_warning)
    original = warnings.showwarning
    warnings.showwarning = hook
    try:
        t1 = threading.Thread(target=lambda: warnings.warn("from-thread-1", UserWarning))
        t1.start()
        assert entered.wait(timeout=2.0)
        warnings.warn("from-thread-2", UserWarning)
        release.set()
        t1.join(timeout=2.0)
    finally:
        release.set()
        warnings.showwarning = original

    assert any("from-thread-1" in m for m in seen)
    assert any("from-thread-2" in m for m in seen)


def test_concurrent_log_warning_keeps_valid_json(tmp_path):
    """Overlapping log_event writes must not tear or drop the JSON log."""
    log_path = str(tmp_path / "kestrel.log.json")
    n = 8
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        log_warning(log_path, f"msg-{i}", stage="unit")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entries = json.loads(Path(log_path).read_text(encoding="utf-8"))
    messages = {e["message"] for e in entries}
    assert {f"msg-{i}" for i in range(n)} <= messages


def test_logged_showwarning_records_category_and_stage(tmp_path):
    log_path = str(tmp_path / "kestrel.log.json")
    stage_ctx = {"stage": "list_files", "file": "IMG_001.CR3"}
    original = warnings.showwarning
    warnings.showwarning = make_logged_showwarning(
        log_path,
        stage_ctx,
        folder="/photos",
        original_showwarning=None,
    )
    try:
        warnings.warn("pipeline note", RuntimeWarning)
    finally:
        warnings.showwarning = original

    entry = json.loads(Path(log_path).read_text(encoding="utf-8"))[-1]
    assert entry["stage"] == "list_files"
    assert entry["category"] == "RuntimeWarning"
    assert entry["context"]["file"] == "IMG_001.CR3"
    assert entry["context"]["folder"] == "/photos"


def test_utcnow_call_detector_ignores_comments_and_finds_calls():
    source = """
# datetime.utcnow() in a comment must not count
x = datetime.utcnow()
y = utcnow()
z = datetime.utcnow ()
"""
    lines = _utcnow_call_linenos(source)
    assert lines == [3, 4, 5]


def test_analyzer_sources_do_not_call_datetime_utcnow():
    """Keep the RecursionError from coming back via a leftover utcnow() call."""
    offenders = []
    skip_parts = {".venv", "venv", "node_modules", "__pycache__", ".git"}
    for path in _ANALYZER_ROOT.rglob("*.py"):
        if any(part in skip_parts for part in path.parts):
            continue
        if "tests" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        try:
            lines = _utcnow_call_linenos(source)
        except SyntaxError:
            continue
        if lines:
            rel = path.relative_to(_ANALYZER_ROOT)
            offenders.append(f"{rel}:{lines}")
    assert offenders == []
