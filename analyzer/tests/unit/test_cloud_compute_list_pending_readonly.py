"""cloud_compute_list_pending_jobs is read-only on load (handoff-cc-client-load-simplification).

No orphan reaper, no cancel_job_remote, no delete_packs, no local status mutation.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge
import cloud_jobs_store

pytestmark = pytest.mark.unit


@pytest.fixture
def api():
    return api_bridge.Api()


@pytest.fixture
def store(tmp_path, monkeypatch):
    data_dir = tmp_path / "userdata"
    data_dir.mkdir()
    monkeypatch.setattr(cloud_jobs_store, "_user_data_dir", lambda: str(data_dir))
    return cloud_jobs_store


class _TrackingClient:
    """Records mutating calls; returns server truth for reconcile."""

    def __init__(self, *, remote_status="processing", upload_complete=True, packs=None):
        self.remote_status = remote_status
        self.upload_complete = upload_complete
        self.packs = packs or []
        self.cancel_calls = []
        self.delete_calls = []

    def get_status(self, job_id):
        return {
            "status": self.remote_status,
            "uploadComplete": self.upload_complete,
            "analyzedCount": 10,
            "retrievedCount": 0,
            "active_container_count": 0,
            "terminal_reason": None,
        }

    def list_results(self, job_id):
        return [{"filename": p} for p in self.packs]

    def cancel_job_remote(self, job_id, origin="user"):
        self.cancel_calls.append((job_id, origin))
        return {"status": "cancelled"}

    def delete_packs(self, job_id, packs):
        self.delete_calls.append((job_id, list(packs)))
        return {"deleted": len(packs), "failed": 0}


class TestListPendingJobsReadOnly:
    def test_uploading_local_processing_remote_does_not_cancel(self, api, store, tmp_path, monkeypatch):
        """Regression: job_04f3f6d5 — local uploading, server processing + upload complete."""
        folder = tmp_path / "job_folder"
        folder.mkdir()
        store.upsert_job({
            "jobId": "job-regression",
            "folderPath": str(folder),
            "status": "uploading",
            "imageCount": 100,
        })
        fake = _TrackingClient(remote_status="processing", upload_complete=True, packs=["pack_1.zip"])
        monkeypatch.setattr(api, "_cc_make_client", lambda: (fake, None))

        r = api.cloud_compute_list_pending_jobs()
        assert r["ok"] is True
        assert fake.cancel_calls == []
        assert fake.delete_calls == []

        rows = {j["jobId"]: j for j in store.load_jobs()}
        assert rows["job-regression"]["status"] == "uploading"
        entry = next(j for j in r["jobs"] if j["jobId"] == "job-regression")
        assert entry["remoteStatus"] == "processing"
        assert entry["availablePacks"] == ["pack_1.zip"]

    def test_does_not_mark_orphan_failed(self, api, store, tmp_path, monkeypatch):
        folder = tmp_path / "stale_upload"
        folder.mkdir()
        store.upsert_job({
            "jobId": "job-stale",
            "folderPath": str(folder),
            "status": "upload_paused",
            "imageCount": 5,
        })
        fake = _TrackingClient(remote_status="uploading", upload_complete=False)
        monkeypatch.setattr(api, "_cc_make_client", lambda: (fake, None))

        api.cloud_compute_list_pending_jobs()
        rows = {j["jobId"]: j for j in store.load_jobs()}
        assert rows["job-stale"]["status"] == "upload_paused"
        assert (rows["job-stale"].get("failureReason") or "") == ""
        assert fake.cancel_calls == []

    def test_does_not_delete_already_merged_packs(self, api, store, tmp_path, monkeypatch):
        folder = tmp_path / "merged"
        folder.mkdir()
        store.upsert_job({
            "jobId": "job-merged",
            "folderPath": str(folder),
            "status": "incomplete",
            "imageCount": 20,
            "downloadedPacks": ["pack_1.zip"],
        })
        fake = _TrackingClient(
            remote_status="incomplete",
            packs=["pack_1.zip", "pack_2.zip"],
        )
        monkeypatch.setattr(api, "_cc_make_client", lambda: (fake, None))

        r = api.cloud_compute_list_pending_jobs()
        assert fake.delete_calls == []
        entry = next(j for j in r["jobs"] if j["jobId"] == "job-merged")
        assert "pack_1.zip" in entry["availablePacks"]
        assert "pack_2.zip" in entry["availablePacks"]

    def test_no_client_still_read_only(self, api, store, tmp_path, monkeypatch):
        folder = tmp_path / "offline"
        folder.mkdir()
        store.upsert_job({
            "jobId": "job-offline",
            "folderPath": str(folder),
            "status": "uploading",
            "imageCount": 1,
        })
        monkeypatch.setattr(api, "_cc_make_client", lambda: (None, {"ok": False, "error": "no auth"}))

        r = api.cloud_compute_list_pending_jobs()
        assert r["ok"] is True
        rows = {j["jobId"]: j for j in store.load_jobs()}
        assert rows["job-offline"]["status"] == "uploading"
