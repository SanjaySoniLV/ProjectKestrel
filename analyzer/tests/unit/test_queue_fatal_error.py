"""Regression test: a fatal pipeline error must not be reported as a done folder.

``AnalysisPipeline.process_folder`` catches a fatal error (e.g. a failed model
load or DB read), logs it, calls ``on_error('fatal', exc)`` and returns
normally rather than raising. The queue therefore saw no exception and used to
mark the folder ``done`` with 0 images processed -- a silent false success.
Wiring ``on_error`` lets the queue surface it as ``error`` instead.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import queue_manager

pytestmark = pytest.mark.unit


class _FatalPipeline:
    """Stub mirroring the real pipeline's swallow-fatal-and-return behavior."""

    detector_name = None
    use_gpu = False

    def process_folder(self, path, pause_event=None, cancel_event=None,
                        callbacks=None, **kwargs):
        cb = (callbacks or {}).get('on_error')
        if cb:
            cb('fatal', RuntimeError('simulated model load failure'))
        # Returns normally, exactly like process_folder after its outer except.


def test_fatal_pipeline_error_marks_folder_error(tmp_path):
    mgr = queue_manager.QueueManager()
    # Pre-seed the cached pipeline so _run reuses the stub instead of building
    # a real AnalysisPipeline.
    mgr._pipeline = _FatalPipeline()

    folder = str(tmp_path)
    mgr.enqueue([folder], use_gpu=False)
    assert mgr.join_worker(timeout=30) is True

    item = mgr._items[0]
    assert item.status == 'error', f"expected 'error', got {item.status!r}"
    assert item.error, "fatal error message should be surfaced, not empty"
