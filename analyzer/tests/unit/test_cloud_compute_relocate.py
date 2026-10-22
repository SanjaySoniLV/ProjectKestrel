"""Unit tests for the §4 Cloud-Compute UX rework bridge changes:

  - ``cloud_compute_relocate_job`` (moved-folder / drive-letter recovery)
  - persistence of ``terminalReason`` through ``cloud_jobs_store``

These exercise the real ``api_bridge.Api`` + ``cloud_jobs_store`` modules with
the on-disk ledger redirected into a tmp dir (so the developer's real
~/.ProjectKestrel ledger is never touched).
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
    # ambient OS keychain (signed-out → empty history).
    a._cc_owner_id = lambda: "test-owner"
    return a


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Redirect the cloud_jobs ledger into a temp dir for the duration of a
    test. Returns the cloud_jobs_store module."""
    data_dir = tmp_path / "userdata"
    data_dir.mkdir()
    monkeypatch.setattr(cloud_jobs_store, "_user_data_dir", lambda: str(data_dir))
    return cloud_jobs_store


def _seed_job(store, job_id, folder, **extra):
    job = {"jobId": job_id, "folderPath": str(folder), "status": "done"}
    job.update(extra)
    return store.upsert_job(job)


class TestRelocateJob:
    def test_happy_path_repoints_folder(self, api, store, tmp_path):
        old = tmp_path / "old_location"
        old.mkdir()
        new = tmp_path / "new_location"
        new.mkdir()
        _seed_job(store, "job-1", old, imageCount=10)

        r = api.cloud_compute_relocate_job("job-1", str(new))
        assert r["ok"] is True
        assert r["jobId"] == "job-1"
        # update is persisted to the ledger
        rows = {j["jobId"]: j for j in store.load_jobs()}
        assert Path(rows["job-1"]["folderPath"]) == Path(r["folderPath"])
        assert Path(rows["job-1"]["folderPath"]).resolve() == new.resolve()

    def test_rejects_nonexistent_dir(self, api, store, tmp_path):
        old = tmp_path / "old"
        old.mkdir()
        _seed_job(store, "job-2", old)
        missing = tmp_path / "does_not_exist"
        r = api.cloud_compute_relocate_job("job-2", str(missing))
        assert r["ok"] is False
        # ledger unchanged
        rows = {j["jobId"]: j for j in store.load_jobs()}
        assert Path(rows["job-2"]["folderPath"]).resolve() == old.resolve()

    def test_unknown_job_id(self, api, store, tmp_path):
        new = tmp_path / "somewhere"
        new.mkdir()
        r = api.cloud_compute_relocate_job("no-such-job", str(new))
        assert r["ok"] is False
        assert "unknown" in (r.get("error") or "").lower()

    def test_missing_job_id_arg(self, api, store, tmp_path):
        new = tmp_path / "x"
        new.mkdir()
        r = api.cloud_compute_relocate_job("", str(new))
        assert r["ok"] is False

    def test_inmemory_state_follows_relocation(self, api, store, tmp_path):
        old = tmp_path / "old3"
        old.mkdir()
        new = tmp_path / "new3"
        new.mkdir()
        _seed_job(store, "job-3", old)
        # Simulate an already-registered in-memory entry (e.g. a surfaced
        # failed job) so relocation must also re-point rootPath in _cc_jobs.
        with api._ensure_cc_lock():
            api._cc_jobs["job-3"] = {
                "jobId": "job-3",
                "rootPath": str(old),
                "status": "failed",
                "remote": dict(api._CC_REMOTE_DEFAULTS),
            }
        r = api.cloud_compute_relocate_job("job-3", str(new))
        assert r["ok"] is True
        assert Path(api._cc_jobs["job-3"]["rootPath"]).resolve() == new.resolve()


class TestTerminalReasonPersistence:
    def test_terminal_reason_round_trips(self, store, tmp_path):
        folder = tmp_path / "f"
        folder.mkdir()
        _seed_job(store, "job-tr", folder)
        store.update_job("job-tr", status="failed", terminalReason="modal_retries_exhausted")
        rows = {j["jobId"]: j for j in store.load_jobs()}
        assert rows["job-tr"]["terminalReason"] == "modal_retries_exhausted"
        assert rows["job-tr"]["status"] == "failed"

    def test_terminal_reason_defaults_empty(self, store, tmp_path):
        folder = tmp_path / "g"
        folder.mkdir()
        _seed_job(store, "job-empty", folder)
        rows = {j["jobId"]: j for j in store.load_jobs()}
        # New field present and empty until a reason is observed.
        assert rows["job-empty"].get("terminalReason", "") == ""


class _FakeClient:
    """Minimal stand-in for CloudComputeClient covering the calls
    list_pending_jobs makes when probing terminal jobs."""

    def __init__(self, status_map=None, results_map=None):
        self.status_map = status_map or {}
        self.results_map = results_map or {}
        self.list_results_calls = []
        self.deleted = []

    def get_status(self, job_id):
        return self.status_map.get(job_id, {"status": "failed"})

    def list_results(self, job_id):
        self.list_results_calls.append(job_id)
        return list(self.results_map.get(job_id, []))

    def delete_packs(self, job_id, packs):
        self.deleted.append((job_id, list(packs)))
        return {"deleted": len(packs), "failed": 0}


class TestIncludeTerminalHistory:
    """§4 follow-up: account-panel history surfaces salvageable packs on
    terminal failed/cancelled jobs when include_terminal=True."""

    def _entry(self, result, job_id):
        return next(j for j in result["jobs"] if j["jobId"] == job_id)

    def test_include_terminal_fetches_available_packs(self, api, store, tmp_path, monkeypatch):
        folder = tmp_path / "failedjob"
        folder.mkdir()
        _seed_job(store, "job-f", folder, status="failed",
                  terminalReason="modal_retries_exhausted", imageCount=50)
        fake = _FakeClient(
            status_map={"job-f": {
                "status": "failed", "analyzedCount": 30, "retrievedCount": 10,
                "terminal_reason": "modal_retries_exhausted", "active_container_count": 0,
            }},
            results_map={"job-f": [{"filename": "pack_1.zip"}, {"filename": "pack_2.zip"}]},
        )
        monkeypatch.setattr(api, "_cc_make_client", lambda: (fake, None))
        r = api.cloud_compute_list_pending_jobs(include_terminal=True)
        entry = self._entry(r, "job-f")
        assert entry["availablePacks"] == ["pack_1.zip", "pack_2.zip"]
        assert entry["terminalReason"] == "modal_retries_exhausted"
        assert entry["retrievedCount"] == 10

    def test_default_skips_terminal_query(self, api, store, tmp_path, monkeypatch):
        folder = tmp_path / "failedjob2"
        folder.mkdir()
        _seed_job(store, "job-f2", folder, status="failed", imageCount=50)
        fake = _FakeClient(
            status_map={"job-f2": {"status": "failed"}},
            results_map={"job-f2": [{"filename": "pack_1.zip"}]},
        )
        monkeypatch.setattr(api, "_cc_make_client", lambda: (fake, None))
        r = api.cloud_compute_list_pending_jobs()  # default → terminal skipped
        entry = self._entry(r, "job-f2")
        assert entry["availablePacks"] is None
        assert "job-f2" not in fake.list_results_calls

    def test_upload_interrupted_orphan_excluded(self, api, store, tmp_path, monkeypatch):
        folder = tmp_path / "orphan"
        folder.mkdir()
        _seed_job(store, "job-o", folder, status="failed",
                  failureReason="upload_interrupted")
        fake = _FakeClient(
            status_map={"job-o": {"status": "cancelled"}},
            results_map={"job-o": [{"filename": "pack_1.zip"}]},
        )
        monkeypatch.setattr(api, "_cc_make_client", lambda: (fake, None))
        r = api.cloud_compute_list_pending_jobs(include_terminal=True)
        entry = self._entry(r, "job-o")
        # Orphan never produced server-side results → not probed.
        assert entry["availablePacks"] is None
        assert "job-o" not in fake.list_results_calls

    def test_cancelled_job_with_packs_is_probed(self, api, store, tmp_path, monkeypatch):
        folder = tmp_path / "cancelledjob"
        folder.mkdir()
        _seed_job(store, "job-c", folder, status="cancelled", imageCount=20)
        fake = _FakeClient(
            status_map={"job-c": {"status": "cancelled", "retrievedCount": 0}},
            results_map={"job-c": [{"filename": "pack_1.zip"}]},
        )
        monkeypatch.setattr(api, "_cc_make_client", lambda: (fake, None))
        r = api.cloud_compute_list_pending_jobs(include_terminal=True)
        entry = self._entry(r, "job-c")
        assert entry["availablePacks"] == ["pack_1.zip"]
        assert "job-c" in fake.list_results_calls


class TestRemoteSnapshotCarriesNewFields:
    """§0 plumbing: the new fields survive a snapshot merge + serialise."""

    def test_snapshot_and_serialise_expose_new_fields(self, api):
        job_id = "job-snap"
        with api._ensure_cc_lock():
            api._cc_jobs[job_id] = {
                "jobId": job_id,
                "rootPath": "/tmp/x",
                "imageCount": 100,
                "newImageCount": 100,
                "status": "running",
                "remote": dict(api._CC_REMOTE_DEFAULTS),
            }
        # Simulate a worker GET /api/jobs/{id} payload (snake_case raw row
        # fields + camelCase derived counts).
        api._cc_apply_remote_snapshot(job_id, {
            "status": "processing",
            "uploadedCount": 100,
            "analyzedCount": 60,
            "retrievedCount": 25,
            "active_container_count": 2,
            "terminal_reason": None,
        })
        with api._ensure_cc_lock():
            desc = api._cc_serialise_job(job_id, api._cc_jobs[job_id])
        assert desc["retrievedCount"] == 25
        assert desc["activeContainerCount"] == 2
        assert desc["terminalReason"] is None

    def test_terminal_reason_propagates_when_present(self, api):
        job_id = "job-snap2"
        with api._ensure_cc_lock():
            api._cc_jobs[job_id] = {
                "jobId": job_id,
                "rootPath": "/tmp/y",
                "status": "running",
                "remote": dict(api._CC_REMOTE_DEFAULTS),
            }
        api._cc_apply_remote_snapshot(job_id, {
            "status": "failed",
            "terminal_reason": "modal_retries_exhausted",
        })
        with api._ensure_cc_lock():
            desc = api._cc_serialise_job(job_id, api._cc_jobs[job_id])
        assert desc["terminalReason"] == "modal_retries_exhausted"
