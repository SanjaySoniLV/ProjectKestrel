"""Refuse .kestrel deletion while analysis is writing that folder.

S0-05: ``clear_kestrel_data`` and the queue's ``delete_kestrel_on_start``
wipe used ``shutil.rmtree`` while a queue item could already be ``running``.
"""

from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge
from queue_manager import QueueManager, _QueueItem


pytestmark = pytest.mark.unit

_CSV_BYTES = b"filename,culled\nIMG_001.CR3,1\n"


def _folder_with_csv(tmp_path: Path) -> tuple[Path, Path]:
    folder = tmp_path / "photos"
    kestrel = folder / ".kestrel"
    kestrel.mkdir(parents=True)
    csv_path = kestrel / "kestrel_database.csv"
    csv_path.write_bytes(_CSV_BYTES)
    return folder, csv_path


class _BlockingPipeline:
    """Stand-in for AnalysisPipeline: blocks inside process_folder."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.csv_existed_on_enter: bool | None = None
        self.calls = 0
        self.detector_name = "mdv5a"

    def process_folder(self, folder, **_kwargs):
        csv_path = Path(folder) / ".kestrel" / "kestrel_database.csv"
        self.csv_existed_on_enter = csv_path.is_file()
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=5), "test did not release process_folder"


@pytest.fixture
def api():
    return api_bridge.Api()


class TestClearKestrelWhileRunning:
    def test_clear_refuses_and_keeps_csv_when_item_running(
        self, tmp_path, monkeypatch, api
    ):
        folder, csv_path = _folder_with_csv(tmp_path)
        original = csv_path.read_bytes()

        qm = QueueManager()
        item = _QueueItem(str(folder), folder.name)
        item.status = "running"
        with qm._lock:
            qm._items.append(item)
        monkeypatch.setattr(api_bridge, "_queue_manager", qm)

        result = api.clear_kestrel_data(str(folder))

        assert result["success"] is False
        assert "running" in str(result.get("error", "")).lower()
        assert csv_path.read_bytes() == original
        assert csv_path.is_file()

    def test_clear_succeeds_when_queue_idle(self, tmp_path, monkeypatch, api):
        folder, csv_path = _folder_with_csv(tmp_path)
        qm = QueueManager()
        monkeypatch.setattr(api_bridge, "_queue_manager", qm)

        result = api.clear_kestrel_data(str(folder))

        assert result["success"] is True
        assert not csv_path.exists()
        assert not (folder / ".kestrel").exists()

    def test_clear_refuses_during_process_folder(
        self, tmp_path, monkeypatch, api
    ):
        folder, csv_path = _folder_with_csv(tmp_path)
        original = csv_path.read_bytes()

        qm = QueueManager()
        pipeline = _BlockingPipeline()
        qm._pipeline = pipeline
        monkeypatch.setattr(api_bridge, "_queue_manager", qm)

        started = qm.enqueue([str(folder)])
        assert started.get("success") is True
        assert pipeline.entered.wait(timeout=5)

        try:
            result = api.clear_kestrel_data(str(folder))
            assert result["success"] is False
            assert csv_path.read_bytes() == original
        finally:
            pipeline.release.set()
            if qm._thread is not None:
                qm._thread.join(timeout=5)

    def test_delete_on_start_waits_until_no_writer(
        self, tmp_path, monkeypatch, api
    ):
        """Re-analyze wipe must not run while process_folder holds the CSV."""
        folder, csv_path = _folder_with_csv(tmp_path)

        qm = QueueManager()
        pipeline = _BlockingPipeline()
        qm._pipeline = pipeline
        monkeypatch.setattr(api_bridge, "_queue_manager", qm)

        rmtree_during_write = []
        real_rmtree = __import__("shutil").rmtree

        def _watch_rmtree(path, *args, **kwargs):
            if pipeline.entered.is_set() and not pipeline.release.is_set():
                rmtree_during_write.append(str(path))
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr("shutil.rmtree", _watch_rmtree)
        monkeypatch.setattr("queue_manager.shutil.rmtree", _watch_rmtree)

        started = qm.enqueue(
            [str(folder)],
            per_item_options={str(folder): {"delete_kestrel_on_start": True}},
        )
        assert started.get("success") is True
        assert pipeline.entered.wait(timeout=5)

        try:
            assert rmtree_during_write == []
            result = api.clear_kestrel_data(str(folder))
            assert result["success"] is False
            assert "running" in str(result.get("error", "")).lower()
        finally:
            pipeline.release.set()
            if qm._thread is not None:
                qm._thread.join(timeout=5)

        assert pipeline.calls == 1
        assert pipeline.csv_existed_on_enter is False

    def test_failed_start_wipe_aborts_and_leaves_csv(self, tmp_path, monkeypatch):
        folder, csv_path = _folder_with_csv(tmp_path)
        original = csv_path.read_bytes()

        qm = QueueManager()
        pipeline = _BlockingPipeline()
        qm._pipeline = pipeline

        def boom(*_a, **_k):
            raise OSError("busy")

        monkeypatch.setattr("queue_manager.shutil.rmtree", boom)

        started = qm.enqueue(
            [str(folder)],
            per_item_options={str(folder): {"delete_kestrel_on_start": True}},
        )
        assert started.get("success") is True
        if qm._thread is not None:
            qm._thread.join(timeout=5)

        assert pipeline.calls == 0
        assert csv_path.read_bytes() == original
        with qm._lock:
            assert qm._items[0].status == "error"
            assert "busy" in qm._items[0].error
