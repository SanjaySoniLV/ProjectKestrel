"""Regression test: the decode generator must not leak threads on early close.

``AnalysisPipeline._iter_decoded`` runs a producer thread that owns a
ThreadPoolExecutor and is bounded by a semaphore the consumer releases after
each yield. If the consumer abandons the generator early (an outer cancel or a
fatal error makes ``process_folder`` return, raising GeneratorExit at the
yield), the release never happened, so the producer stayed parked on
``semaphore.acquire()`` forever and its decode-worker threads leaked for the
process lifetime.
"""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.pipeline import AnalysisPipeline

pytestmark = pytest.mark.unit


def _decode_threads():
    return [
        t.name for t in threading.enumerate()
        if '_submit_all' in t.name or 'ThreadPoolExecutor' in t.name
    ]


def test_decode_generator_releases_threads_on_early_close(tmp_path):
    # A handful of files so the threaded path (max_workers > 1) is exercised.
    names = []
    for i in range(6):
        p = tmp_path / f"img_{i}.jpg"
        # Not a real JPEG: _decode_image catches the failure and returns an
        # error dict, which is still yielded -- enough to drive the generator.
        p.write_bytes(b"\xff\xd8\xff\xe0not-a-real-jpeg")
        names.append(p.name)

    pipeline = AnalysisPipeline(use_gpu=False)

    assert _decode_threads() == []

    gen = pipeline._iter_decoded(names, str(tmp_path), max_workers=3)
    next(gen)          # start the producer thread + pool, consume one item
    gen.close()        # GeneratorExit at the yield, like process_folder's return

    # The producer must unblock, drain its pool and exit; poll briefly because
    # the executor waits for in-flight decodes before shutting down.
    deadline = time.time() + 10
    while time.time() < deadline and _decode_threads():
        time.sleep(0.05)

    leftover = _decode_threads()
    assert leftover == [], f"decode threads leaked after early close: {leftover}"
