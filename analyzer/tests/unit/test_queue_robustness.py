"""Regression tests for two queue robustness issues.

1. enqueue() decided whether to start the worker thread with an
   ``if not self.is_running:`` check performed OUTSIDE the lock, so two
   concurrent enqueue() calls (e.g. a double-clicked "Start") could both
   observe "not running" and start two worker threads on the same _items list.

2. The pipeline object is a manager-lifetime singleton reused across runs.
   _run() only refreshed ``detector_name`` on the cached pipeline, never
   ``use_gpu``, so toggling GPU/CPU between runs was silently ignored.

3. After the start-under-lock fix, a worker that had already seen an empty
   queue could still look alive, so enqueue() skipped starting a successor
   and left the new items unprocessed. Idle-exit now releases
   ``_worker_claimed`` under the same lock enqueue() uses.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import queue_manager

pytestmark = pytest.mark.unit


def _alive_queue_workers():
    return [
        t for t in threading.enumerate()
        if t.name == "kestrel-queue" and t.is_alive()
    ]


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

    try:
        assert started.wait(5)
        # Poll briefly so a duplicate worker that starts a tick late still
        # shows up, instead of sampling once at a lucky instant.
        max_n = 0
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            max_n = max(max_n, len(_alive_queue_workers()))
            time.sleep(0.02)
    finally:
        release.set()
        assert mgr.join_worker(timeout=10) is True

    assert max_n == 1, f"expected exactly one worker thread, got {max_n}"


def test_enqueue_during_idle_exit_starts_successor(tmp_path):
    """Worker has released its claim but is still alive; enqueue must start a successor."""
    mgr = queue_manager.QueueManager()
    processed: list[str] = []

    class _StubPipeline:
        detector_name = None
        use_gpu = False

        def process_folder(self, path, **kwargs):
            processed.append(path)

    mgr._pipeline = _StubPipeline()

    idle = threading.Event()
    gate = threading.Event()
    real_take = mgr._take_next_pending

    def gated_take():
        item = real_take()
        if item is None:
            idle.set()
            gate.wait(5)
        return item

    mgr._take_next_pending = gated_take

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    mgr.enqueue([str(first)], use_gpu=False)
    assert idle.wait(5), "worker never reached idle-exit"
    assert mgr._worker_claimed is False
    assert mgr._thread is not None and mgr._thread.is_alive()

    res = mgr.enqueue([str(second)], use_gpu=False)
    assert res["success"] is True
    gate.set()
    assert mgr.join_worker(timeout=15) is True

    assert str(first) in processed
    assert str(second) in processed, "successor worker never processed the post-idle enqueue"


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
