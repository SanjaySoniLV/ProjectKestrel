"""The queue must carry the pipeline's "results can't be saved" warning to the UI.

``current_status_msg`` is overwritten after every image, so a warning sent
through it would flash and vanish. The queue item has a separate ``warning``
field that persists until the pipeline clears it and is serialised for the
queue panel.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import queue_manager

pytestmark = pytest.mark.unit


class _WarningPipeline:
    detector_name = None
    use_gpu = False

    def __init__(self, messages):
        self.messages = messages

    def process_folder(self, path, pause_event=None, cancel_event=None,
                       callbacks=None, **kwargs):
        cbs = callbacks or {}
        for msg in self.messages:
            cbs['on_warning'](msg)
            # Per-image status updates must not wipe the warning.
            cbs['on_status']('No detections in IMG_0001.CR3')


def _run(tmp_path, messages):
    mgr = queue_manager.QueueManager()
    mgr._pipeline = _WarningPipeline(messages)
    mgr.enqueue([str(tmp_path)], use_gpu=False)
    assert mgr.join_worker(timeout=30) is True
    return mgr._items[0]


def test_warning_persists_past_status_updates_and_is_serialised(tmp_path):
    item = _run(tmp_path, ["Kestrel can't save results to this folder."])
    assert item.warning == "Kestrel can't save results to this folder."
    assert item.to_dict()['warning'] == item.warning
    assert item.current_status_msg == 'No detections in IMG_0001.CR3'


def test_pipeline_can_clear_the_warning(tmp_path):
    item = _run(tmp_path, ["Kestrel can't save results to this folder.", ''])
    assert item.warning == ''


def test_new_item_has_no_warning():
    assert queue_manager._QueueItem('/x', 'x').to_dict()['warning'] == ''
