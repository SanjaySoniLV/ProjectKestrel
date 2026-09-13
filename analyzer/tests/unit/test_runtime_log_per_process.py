"""Regression tests: each process gets its own runtime log file.

``_enable_runtime_log_capture`` named the file from a UTC timestamp with
one-second resolution and opened it in append mode. Two instances launched in
the same second — a double-click, or a second copy started while the first is
open, which the recovery-dialog logic explicitly expects — therefore shared one
file and interleaved their output line by line.

That is exactly the session whose log a crash report most needs to be readable:
field reports from such a launch show every line duplicated and two different
pids announcing the same ``session_start``, so neither instance's history can be
followed. Adding the pid to the name keeps the two apart while preserving the
``kestrel_runtime_`` prefix and ``.log`` suffix that ``kestrel_telemetry`` uses
to select recent runtime logs for a report.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    import visualizer
except Exception as e:  # pragma: no cover - environment-dependent
    pytest.skip(f"visualizer module not importable in this env: {e}", allow_module_level=True)

import kestrel_analyzer.logging_utils as logging_utils

pytestmark = pytest.mark.unit


@pytest.fixture
def capture(tmp_path, monkeypatch):
    """Run ``_enable_runtime_log_capture`` against a temp log dir, then restore.

    The function replaces ``sys.stdout``/``sys.stderr`` process-wide and stores
    the open handle in a module global, so every call has to be undone or the
    rest of the session writes into the test's temp directory.
    """
    monkeypatch.setattr(logging_utils, "resolve_log_dir", lambda folder: str(tmp_path))
    opened = []

    def run(pid: int) -> str:
        monkeypatch.setattr(visualizer.os, "getpid", lambda: pid)
        saved = (sys.stdout, sys.stderr, visualizer._RUNTIME_LOG_HANDLE)
        handle = None
        try:
            path = visualizer._enable_runtime_log_capture()
            handle = visualizer._RUNTIME_LOG_HANDLE
        finally:
            sys.stdout, sys.stderr, visualizer._RUNTIME_LOG_HANDLE = saved
        if handle is not None and handle not in saved:
            handle.close()
        opened.append(path)
        return path

    yield run

    for path in opened:
        assert path, "log capture returned no path"


class TestPerProcessRuntimeLog:
    def test_two_instances_in_the_same_second_do_not_share_a_file(self, capture):
        first = capture(4242)
        second = capture(4343)

        assert first != second, (
            "two instances started in the same second opened the same runtime "
            "log and interleaved their output"
        )
        assert os.path.basename(first) != os.path.basename(second)

    def test_the_name_identifies_the_process(self, capture):
        path = capture(31337)

        assert "_p31337." in os.path.basename(path)

    def test_the_name_still_matches_what_telemetry_collects(self, capture):
        """kestrel_telemetry selects runtime logs by prefix and suffix."""
        name = os.path.basename(capture(4242))

        assert name.startswith("kestrel_runtime_")
        assert name.endswith(".log")

    def test_the_timestamp_stays_first_so_names_sort_by_start_time(self, capture):
        name = os.path.basename(capture(4242))

        assert re.match(r"^kestrel_runtime_\d{8}T\d{6}Z_p\d+\.log$", name), name

    def test_output_written_after_capture_lands_in_that_process_file(self, capture):
        path = capture(4242)

        with open(path, "a", encoding="utf-8") as f:
            f.write("hello from this process\n")

        assert "hello from this process" in Path(path).read_text(encoding="utf-8")
