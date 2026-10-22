"""The analyze dialog's species geofence travels per queue item to the pipeline."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import queue_manager

pytestmark = pytest.mark.unit


class _RecordingPipeline:
    detector_name = None
    use_gpu = False

    def __init__(self):
        self.calls = []

    def process_folder(self, path, pause_event=None, cancel_event=None,
                       callbacks=None, **kwargs):
        self.calls.append((path, kwargs.get("species_regions", "MISSING")))


def test_per_item_species_regions_reach_process_folder(tmp_path):
    a, b, c, d = (tmp_path / n for n in "abcd")
    for p in (a, b, c, d):
        p.mkdir()
    mgr = queue_manager.QueueManager()
    rec = _RecordingPipeline()
    mgr._pipeline = rec
    mgr.enqueue(
        [str(a), str(b), str(c), str(d)],
        use_gpu=False,
        per_item_options={
            str(a): {"species_regions": ["PAL", "AF"]},
            str(b): {"species_regions": ["global"]},
            str(c): {"species_regions": ["nonsense"]},
            # d: no option -> None (pipeline falls back to settings)
        },
    )
    assert mgr.join_worker(timeout=30) is True
    got = dict(rec.calls)
    assert got[str(a)] == ["AF", "PAL"]   # canonical ALLOWED_REGION_CODES order
    assert got[str(b)] == ["global"]
    assert got[str(c)] == ["NA"]          # sanitised to the default
    assert got[str(d)] is None
    status = {it["path"]: it for it in mgr.get_status()["items"]}
    assert status[str(a)]["species_regions"] == ["AF", "PAL"]


def test_coerce_species_regions():
    f = queue_manager._coerce_species_regions
    assert f(None) is None
    assert f([]) is None
    assert f(5) is None
    assert f("Global") == ["global"]
    assert f(["NA", "PAL"]) == ["NA", "PAL"]
