"""JSON stores must retry ``os.replace`` on Windows sharing violations.

CSV, sidecar, and cloud-merge writers already go through
``retry_on_file_lock``. ``settings.json``, ``cloud_jobs.json``, and
``kestrel_cloudcompute.json`` still called bare ``os.replace``, so a
transient Explorer/Defender lock aborted the save.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cloud_folder_state
import cloud_jobs_store
import settings_utils


pytestmark = pytest.mark.unit


def _flaky_replace(real_replace, calls):
    def _replace(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(32, "The process cannot access the file")
        return real_replace(src, dst, *a, **k)

    return _replace


def _always_locked(_src, _dst, *_a, **_k):
    raise PermissionError(32, "The process cannot access the file")


class TestSettingsReplaceIsRetried:
    @pytest.fixture(autouse=True)
    def _settings_dir(self, tmp_path, monkeypatch):
        self._path = tmp_path / settings_utils.SETTINGS_FILENAME
        monkeypatch.setattr(
            settings_utils, "_get_settings_path", lambda: str(self._path)
        )
        monkeypatch.setattr(settings_utils.time, "sleep", lambda *_a, **_k: None)

    def test_transient_permission_error_is_retried(self, monkeypatch):
        settings_utils.save_persisted_settings({"editor": "darktable"})
        original = self._path.read_bytes()
        calls = {"n": 0}
        monkeypatch.setattr(
            settings_utils.os, "replace", _flaky_replace(os.replace, calls)
        )

        settings_utils.save_persisted_settings({"editor": "photoshop"})

        assert calls["n"] >= 2
        loaded = settings_utils.load_persisted_settings()
        assert loaded["editor"] == "photoshop"
        assert self._path.read_bytes() != original

    def test_persistent_lock_leaves_settings_intact(self, monkeypatch):
        settings_utils.save_persisted_settings({"editor": "darktable"})
        original = self._path.read_bytes()
        monkeypatch.setattr(settings_utils.os, "replace", _always_locked)

        settings_utils.save_persisted_settings({"editor": "photoshop"})

        assert self._path.read_bytes() == original
        leftovers = [p.name for p in self._path.parent.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []


class TestCloudJobsReplaceIsRetried:
    @pytest.fixture(autouse=True)
    def _store_dir(self, tmp_path, monkeypatch):
        self._dir = tmp_path / "userdata"
        self._dir.mkdir()
        monkeypatch.setattr(
            cloud_jobs_store, "_user_data_dir", lambda: str(self._dir)
        )
        monkeypatch.setattr(cloud_jobs_store.time, "sleep", lambda *_a, **_k: None)

    def _ledger(self) -> Path:
        return self._dir / cloud_jobs_store.CLOUD_JOBS_FILENAME

    def test_transient_permission_error_is_retried(self, tmp_path, monkeypatch):
        folder = tmp_path / "shoot"
        folder.mkdir()
        cloud_jobs_store.upsert_job(
            {"jobId": "job-1", "folderPath": str(folder), "status": "done"}
        )
        original = self._ledger().read_bytes()
        calls = {"n": 0}
        monkeypatch.setattr(
            cloud_jobs_store.os, "replace", _flaky_replace(os.replace, calls)
        )

        cloud_jobs_store.update_job("job-1", status="failed")

        assert calls["n"] >= 2
        rows = {j["jobId"]: j for j in cloud_jobs_store.load_jobs()}
        assert rows["job-1"]["status"] == "failed"
        assert self._ledger().read_bytes() != original

    def test_persistent_lock_leaves_ledger_intact(self, tmp_path, monkeypatch):
        folder = tmp_path / "shoot"
        folder.mkdir()
        cloud_jobs_store.upsert_job(
            {"jobId": "job-1", "folderPath": str(folder), "status": "done"}
        )
        original = self._ledger().read_bytes()
        monkeypatch.setattr(cloud_jobs_store.os, "replace", _always_locked)

        with pytest.raises(PermissionError):
            cloud_jobs_store.update_job("job-1", status="failed")

        assert self._ledger().read_bytes() == original
        leftovers = [p.name for p in self._dir.iterdir() if p.suffix == ".tmp"]
        assert leftovers == []


class TestCloudFolderStateReplaceIsRetried:
    @pytest.fixture(autouse=True)
    def _sleep(self, monkeypatch):
        monkeypatch.setattr(cloud_folder_state.time, "sleep", lambda *_a, **_k: None)

    def _state_file(self, folder: Path) -> Path:
        return folder / ".kestrel" / cloud_folder_state.CLOUD_FOLDER_STATE_FILENAME

    def test_transient_permission_error_is_retried(self, tmp_path, monkeypatch):
        folder = tmp_path / "shoot"
        folder.mkdir()
        cloud_folder_state.mark_pack_merged(folder, "job-1", "pack_1.zip")
        original = self._state_file(folder).read_bytes()
        calls = {"n": 0}
        monkeypatch.setattr(
            cloud_folder_state.os, "replace", _flaky_replace(os.replace, calls)
        )

        cloud_folder_state.mark_pack_merged(folder, "job-1", "pack_2.zip")

        assert calls["n"] >= 2
        assert cloud_folder_state.list_merged_packs(folder, "job-1") == [
            "pack_1.zip",
            "pack_2.zip",
        ]
        assert self._state_file(folder).read_bytes() != original

    def test_persistent_lock_leaves_state_intact(self, tmp_path, monkeypatch):
        folder = tmp_path / "shoot"
        folder.mkdir()
        cloud_folder_state.mark_pack_merged(folder, "job-1", "pack_1.zip")
        original = self._state_file(folder).read_bytes()
        monkeypatch.setattr(cloud_folder_state.os, "replace", _always_locked)

        with pytest.raises(PermissionError):
            cloud_folder_state.mark_pack_merged(folder, "job-1", "pack_2.zip")

        assert self._state_file(folder).read_bytes() == original
        assert cloud_folder_state.list_merged_packs(folder, "job-1") == ["pack_1.zip"]
        leftovers = [
            p.name
            for p in (folder / ".kestrel").iterdir()
            if p.suffix == ".tmp"
        ]
        assert leftovers == []
