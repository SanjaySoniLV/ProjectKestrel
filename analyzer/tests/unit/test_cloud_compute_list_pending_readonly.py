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
    a = api_bridge.Api()
    # Pin a deterministic owner so job-history filtering doesn't depend on the
    # ambient OS keychain (signed-out → empty history). Upserted test jobs have
    # no ownerId and are adopted to this owner on first list.
    a._cc_owner_id = lambda: "test-owner"
    return a


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

    def test_done_job_backfills_retrieved_count_from_image_count(self, api, store, tmp_path, monkeypatch):
        """Bug fix: a 'done' job is never queried against the Worker (terminal,
        not in the salvageable set), so retrievedCount stayed None and the
        account panel rendered "0 / N retrieved". It must backfill to
        imageCount — a done job has, by construction, pulled every pack."""
        folder = tmp_path / "completed"
        folder.mkdir()
        store.upsert_job({
            "jobId": "job-done",
            "folderPath": str(folder),
            "status": "done",
            "imageCount": 42,
            "downloadedPacks": ["pack_1.zip", "pack_2.zip"],
        })
        # Client present but should NOT be consulted for a done job.
        fake = _TrackingClient(remote_status="complete", packs=["pack_1.zip"])
        monkeypatch.setattr(api, "_cc_make_client", lambda: (fake, None))

        r = api.cloud_compute_list_pending_jobs(include_terminal=True)
        assert r["ok"] is True
        entry = next(j for j in r["jobs"] if j["jobId"] == "job-done")
        assert entry["retrievedCount"] == 42
        # Done job was not queried → no remote probe happened.
        assert entry["remoteStatus"] is None
        assert entry["availablePacks"] is None

    def test_running_job_retrieved_count_not_overwritten(self, api, store, tmp_path, monkeypatch):
        """A non-terminal job keeps the Worker's live retrievedCount; the
        done-backfill must not touch it."""
        folder = tmp_path / "running"
        folder.mkdir()
        store.upsert_job({
            "jobId": "job-running",
            "folderPath": str(folder),
            "status": "uploading",
            "imageCount": 400,
        })

        class _LiveClient(_TrackingClient):
            def get_status(self, job_id):
                d = super().get_status(job_id)
                d["retrievedCount"] = 150
                return d

        fake = _LiveClient(remote_status="processing", packs=[])
        monkeypatch.setattr(api, "_cc_make_client", lambda: (fake, None))

        r = api.cloud_compute_list_pending_jobs()
        entry = next(j for j in r["jobs"] if j["jobId"] == "job-running")
        assert entry["retrievedCount"] == 150

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


class TestJobHistoryOwnerScoping:
    def test_history_filtered_to_current_owner(self, api, store, tmp_path, monkeypatch):
        """Account B never sees account A's jobs; A's stay on disk."""
        folder = tmp_path / "f"
        folder.mkdir()
        store.upsert_job({
            "jobId": "job-A", "ownerId": "owner-A",
            "folderPath": str(folder), "status": "done", "imageCount": 5,
        })
        store.upsert_job({
            "jobId": "job-B", "ownerId": "owner-B",
            "folderPath": str(folder), "status": "done", "imageCount": 5,
        })
        monkeypatch.setattr(api, "_cc_make_client", lambda: (None, {"ok": False, "error": "x"}))

        api._cc_owner_id = lambda: "owner-B"
        r = api.cloud_compute_list_pending_jobs(include_terminal=True)
        ids = {j["jobId"] for j in r["jobs"]}
        assert ids == {"job-B"}
        # A's job is filtered from the view but NOT deleted from the ledger.
        assert {j["jobId"] for j in store.load_jobs()} == {"job-A", "job-B"}

    def test_signed_out_sees_no_history(self, api, store, tmp_path, monkeypatch):
        folder = tmp_path / "f"
        folder.mkdir()
        store.upsert_job({
            "jobId": "job-A", "ownerId": "owner-A",
            "folderPath": str(folder), "status": "done", "imageCount": 5,
        })
        monkeypatch.setattr(api, "_cc_make_client", lambda: (None, {"ok": False, "error": "x"}))
        api._cc_owner_id = lambda: ""
        r = api.cloud_compute_list_pending_jobs(include_terminal=True)
        assert r == {"ok": True, "jobs": []}

    def test_legacy_unowned_jobs_adopted_by_current_owner(self, api, store, tmp_path, monkeypatch):
        """A pre-tagging row (no ownerId) is claimed by the first lister and
        then scoped to that owner — preserving pre-upgrade history."""
        folder = tmp_path / "f"
        folder.mkdir()
        store.upsert_job({
            "jobId": "job-legacy",
            "folderPath": str(folder), "status": "done", "imageCount": 5,
        })
        monkeypatch.setattr(api, "_cc_make_client", lambda: (None, {"ok": False, "error": "x"}))

        api._cc_owner_id = lambda: "owner-A"
        r = api.cloud_compute_list_pending_jobs(include_terminal=True)
        assert {j["jobId"] for j in r["jobs"]} == {"job-legacy"}
        # Persisted claim: a different account no longer sees it.
        adopted = {j["jobId"]: j for j in store.load_jobs()}
        assert adopted["job-legacy"]["ownerId"] == "owner-A"
        api._cc_owner_id = lambda: "owner-B"
        r2 = api.cloud_compute_list_pending_jobs(include_terminal=True)
        assert r2["jobs"] == []
