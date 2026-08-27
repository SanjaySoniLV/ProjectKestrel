"""Regression tests for two queue robustness issues.

1. enqueue() decided whether to start the worker thread with an
   ``if not self.is_running:`` check performed OUTSIDE the lock, so two
   concurrent enqueue() calls (e.g. a double-clicked "Start") could both
   observe "not running" and start two worker threads on the same _items list.

2. The pipeline object is a manager-lifetime singleton reused across runs.
   _run() only refreshed ``detector_name`` on the cached pipeline, never
   ``use_gpu``, so toggling GPU/CPU between runs was silently ignored.
"""

import os
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import queue_manager

pytestmark = pytest.mark.unit


def test_concurrent_enqueue_starts_a_single_worker(tmp_path):
    mgr = queue_manager.QueueManager()
    release = threading.Event()
    started = threading.Event()

    class _BlockingPipeline:
        detector_name = None
        use_gpu = False

        def process_folder(self, path, **kwargs):
            started.set()
            release.wait(5)   # hold the worker inside process_folder

    mgr._pipeline = _BlockingPipeline()

    folders = []
    for i in range(8):
        f = tmp_path / f"f{i}"
        f.mkdir()
        folders.append(str(f))

    # Fire many enqueue() calls at once.
    threads = [threading.Thread(target=lambda p=p: mgr.enqueue([p], use_gpu=False))
               for p in folders]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Wait until the (single) worker is actually inside process_folder, then
    # count how many 'kestrel-queue' worker threads are alive.
    assert started.wait(5)
    workers = [t for t in threading.enumerate()
               if t.name == 'kestrel-queue' and t.is_alive()]
    n = len(workers)

    release.set()
    mgr.join_worker(timeout=10)

    assert n == 1, f"expected exactly one worker thread, got {n}"


def test_use_gpu_change_between_runs_reaches_cached_pipeline(tmp_path):
    mgr = queue_manager.QueueManager()

    class _StubPipeline:
        detector_name = None
        use_gpu = True   # first run constructed with GPU

        def process_folder(self, path, **kwargs):
            return  # empty folder: return immediately

    stub = _StubPipeline()
    mgr._pipeline = stub

    f1 = tmp_path / "run1"; f1.mkdir()
    mgr.enqueue([str(f1)], use_gpu=True)
    assert mgr.join_worker(timeout=15) is True
    assert stub.use_gpu is True

    # Second run with GPU turned OFF must propagate onto the reused pipeline.
    f2 = tmp_path / "run2"; f2.mkdir()
    mgr.enqueue([str(f2)], use_gpu=False)
    assert mgr.join_worker(timeout=15) is True
    assert stub.use_gpu is False, "use_gpu toggle was not propagated to the cached pipeline"
