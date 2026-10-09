"""cloud_jobs.json and kestrel_cloudcompute.json must fsync before replace.

Both stores wrote a tempfile and ``os.replace``'d it without flush/fsync,
so a crash after replace could leave an empty or half-written ledger. Match
``_to_csv_atomic``: ``flush()`` errors abort the replace; ``fsync`` OSError
on network FS still replaces.
"""

from __future__ import annotations

import errno
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cloud_folder_state
import cloud_jobs_store


pytestmark = pytest.mark.unit


class _FlushBoom:
    """Delegates to the real fdopen file; flush is not assignable on TextIOWrapper."""

    def __init__(self, real):
        self._real = real

    def write(self, *a, **k):
        return self._real.write(*a, **k)

    def flush(self):
        raise OSError(errno.ENOSPC, "No space left on device")

    def fileno(self):
        return self._real.fileno()

    def close(self):
        return self._real.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _wrap_fdopen(real_fdopen):
    def wrapping_fdopen(*a, **k):
        return _FlushBoom(real_fdopen(*a, **k))

    return wrapping_fdopen


def _tmp_leftovers(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if p.name.endswith(".tmp")]


def _job(job_id: str = "job-1", folder: str = "/photos/trip") -> dict:
    return {
        "jobId": job_id,
        "folderPath": folder,
        "status": "downloading",
        "ownerId": "owner-1",
    }


class TestCloudJobsFsync:
    @pytest.fixture
    def store_dir(self, tmp_path, monkeypatch):
        data_dir = tmp_path / "userdata"
        data_dir.mkdir()
        monkeypatch.setattr(cloud_jobs_store, "_user_data_dir", lambda: str(data_dir))
        return data_dir

    def test_save_calls_fsync_before_replace(self, store_dir, monkeypatch):
        seen: list[int] = []
        real = cloud_jobs_store.os.fsync

        def spy(fd):
            seen.append(fd)
            return real(fd)

        monkeypatch.setattr(cloud_jobs_store.os, "fsync", spy)
        cloud_jobs_store.save_jobs([_job()])

        dest = store_dir / "cloud_jobs.json"
        assert dest.is_file()
        assert json.loads(dest.read_text(encoding="utf-8"))["jobs"][0]["jobId"] == "job-1"
        assert seen, "save_jobs replaced without fsync"
        assert _tmp_leftovers(store_dir) == []

    def test_flush_error_leaves_existing_jobs_file(self, store_dir, monkeypatch):
        cloud_jobs_store.save_jobs([_job("job-old")])
        dest = store_dir / "cloud_jobs.json"
        original = dest.read_bytes()

        monkeypatch.setattr(
            cloud_jobs_store.os, "fdopen", _wrap_fdopen(cloud_jobs_store.os.fdopen)
        )
        with pytest.raises(OSError, match="No space left on device"):
            cloud_jobs_store.save_jobs([_job("job-new")])

        assert dest.read_bytes() == original
        assert _tmp_leftovers(store_dir) == []

    def test_fsync_error_still_replaces(self, store_dir, monkeypatch):
        def boom(_fd):
            raise OSError(errno.EINVAL, "Operation not supported")

        monkeypatch.setattr(cloud_jobs_store.os, "fsync", boom)
        cloud_jobs_store.save_jobs([_job("job-netfs")])

        dest = store_dir / "cloud_jobs.json"
        assert json.loads(dest.read_text(encoding="utf-8"))["jobs"][0]["jobId"] == "job-netfs"
        assert _tmp_leftovers(store_dir) == []


class TestCloudFolderStateFsync:
    def test_mark_pack_calls_fsync_before_replace(self, tmp_path, monkeypatch):
        seen: list[int] = []
        real = cloud_folder_state.os.fsync

        def spy(fd):
            seen.append(fd)
            return real(fd)

        monkeypatch.setattr(cloud_folder_state.os, "fsync", spy)
        cloud_folder_state.mark_pack_merged(tmp_path, "job-1", "pack-001.zip")

        dest = tmp_path / ".kestrel" / "kestrel_cloudcompute.json"
        assert dest.is_file()
        data = json.loads(dest.read_text(encoding="utf-8"))
        assert data["jobs"]["job-1"]["mergedPacks"] == ["pack-001.zip"]
        assert seen, "mark_pack_merged replaced without fsync"
        assert _tmp_leftovers(tmp_path / ".kestrel") == []

    def test_flush_error_leaves_existing_folder_state(self, tmp_path, monkeypatch):
        cloud_folder_state.mark_pack_merged(tmp_path, "job-1", "pack-old.zip")
        dest = tmp_path / ".kestrel" / "kestrel_cloudcompute.json"
        original = dest.read_bytes()

        monkeypatch.setattr(
            cloud_folder_state.os,
            "fdopen",
            _wrap_fdopen(cloud_folder_state.os.fdopen),
        )
        with pytest.raises(OSError, match="No space left on device"):
            cloud_folder_state.mark_pack_merged(tmp_path, "job-1", "pack-new.zip")

        assert dest.read_bytes() == original
        assert _tmp_leftovers(tmp_path / ".kestrel") == []

    def test_fsync_error_still_replaces(self, tmp_path, monkeypatch):
        def boom(_fd):
            raise OSError(errno.EINVAL, "Operation not supported")

        monkeypatch.setattr(cloud_folder_state.os, "fsync", boom)
        cloud_folder_state.mark_pack_merged(tmp_path, "job-1", "pack-netfs.zip")

        dest = tmp_path / ".kestrel" / "kestrel_cloudcompute.json"
        data = json.loads(dest.read_text(encoding="utf-8"))
        assert data["jobs"]["job-1"]["mergedPacks"] == ["pack-netfs.zip"]
        assert _tmp_leftovers(tmp_path / ".kestrel") == []
