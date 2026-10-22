"""Unit tests for cloud_compute_cancel_job remote-outcome handling.

The desktop must only finalize a job as 'cancelled' locally when the remote
cancel actually LANDED (200, or 404 = already gone server-side). On a transient
failure (network down / expired session) it must leave local state untouched —
the job is still running server-side, so a false local 'cancelled' would desync.
"""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge
import cloud_compute_client as ccc

pytestmark = pytest.mark.unit


class _FakeStore:
    """Stands in for the cloud_jobs_store module — records update_job calls."""

    def __init__(self):
        self.updates = []

    def update_job(self, job_id, **kwargs):
        self.updates.append((job_id, kwargs))


@pytest.fixture
def api():
    return api_bridge.Api()


def _seed_job(api, job_id="job_x", status="uploading"):
    ev = threading.Event()
    api._cc_jobs[job_id] = {
        "status": status,
        "cancel_event": ev,
        "remote": dict(api._CC_REMOTE_DEFAULTS),
    }
    return ev


def _wire(api, monkeypatch, client, client_err=None):
    monkeypatch.setattr(api, "_cc_import", lambda: ccc)
    monkeypatch.setattr(api, "_cc_make_client", lambda: (client, client_err))
    monkeypatch.setattr(api, "_cc_jobs_store", lambda: _FakeStore())


class _Client:
    def __init__(self, exc=None, ret=None):
        self._exc = exc
        self._ret = ret
        self.calls = 0

    def cancel_job_remote(self, job_id):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._ret if self._ret is not None else {"ok": True}


class TestCancelDesync:
    def test_network_error_is_transient_and_does_not_flip(self, api, monkeypatch):
        ev = _seed_job(api)
        _wire(api, monkeypatch, _Client(exc=ccc.CloudComputeNetworkError("conn reset")))
        r = api.cloud_compute_cancel_job("job_x")
        assert r["ok"] is False
        assert r["transient"] is True
        assert api._cc_jobs["job_x"]["status"] == "uploading"
        assert not ev.is_set()

    def test_auth_error_is_transient_and_does_not_flip(self, api, monkeypatch):
        ev = _seed_job(api)
        _wire(api, monkeypatch, _Client(exc=ccc.CloudComputeAuthError()))
        r = api.cloud_compute_cancel_job("job_x")
        assert r["ok"] is False
        assert r["transient"] is True
        assert api._cc_jobs["job_x"]["status"] == "uploading"
        assert not ev.is_set()

    def test_success_finalizes_cancelled(self, api, monkeypatch):
        ev = _seed_job(api)
        _wire(api, monkeypatch, _Client(ret={"ok": True}))
        r = api.cloud_compute_cancel_job("job_x")
        assert r["ok"] is True
        assert api._cc_jobs["job_x"]["status"] == "cancelled"
        assert ev.is_set()

    def test_404_finalizes_cancelled(self, api, monkeypatch):
        # Job already gone server-side → nothing running → safe to finalize.
        ev = _seed_job(api)
        _wire(api, monkeypatch, _Client(exc=ccc.CloudComputeError(404, "not found")))
        r = api.cloud_compute_cancel_job("job_x")
        assert r["ok"] is True
        assert api._cc_jobs["job_x"]["status"] == "cancelled"
        assert ev.is_set()

    def test_server_refusal_does_not_flip_and_is_not_transient(self, api, monkeypatch):
        ev = _seed_job(api)
        _wire(api, monkeypatch, _Client(exc=ccc.CloudComputeError(500, "boom")))
        r = api.cloud_compute_cancel_job("job_x")
        assert r["ok"] is False
        assert r.get("transient") is not True  # server refused, not a transport blip
        assert api._cc_jobs["job_x"]["status"] == "uploading"
        assert not ev.is_set()

    def test_no_client_is_transient(self, api, monkeypatch):
        ev = _seed_job(api)
        monkeypatch.setattr(api, "_cc_import", lambda: ccc)
        monkeypatch.setattr(api, "_cc_make_client", lambda: (None, {"error": "session expired"}))
        monkeypatch.setattr(api, "_cc_jobs_store", lambda: _FakeStore())
        r = api.cloud_compute_cancel_job("job_x")
        assert r["ok"] is False
        assert r["transient"] is True
        assert api._cc_jobs["job_x"]["status"] == "uploading"
        assert not ev.is_set()

    def test_unknown_job_id(self, api, monkeypatch):
        _wire(api, monkeypatch, _Client(ret={"ok": True}))
        r = api.cloud_compute_cancel_job("nope")
        assert r["ok"] is False


class TestPauseRemoved:
    def test_pause_resume_bridge_methods_are_gone(self, api):
        assert not hasattr(api, "cloud_compute_pause_job")
        assert not hasattr(api, "cloud_compute_resume_job")


class TestStopUploadsForShutdown:
    """App shutdown must release in-flight upload pools by signalling every
    job's cancel_event locally (no Worker round-trip), so the ThreadPoolExecutor
    atexit join doesn't keep uploading after the window closes."""

    def test_signals_all_inflight_events(self, api):
        ev1 = _seed_job(api, job_id="job_a")
        ev2 = _seed_job(api, job_id="job_b")
        n = api.stop_cloud_uploads_for_shutdown()
        assert n == 2
        assert ev1.is_set()
        assert ev2.is_set()

    def test_no_jobs_returns_zero(self, api):
        assert api.stop_cloud_uploads_for_shutdown() == 0

    def test_already_set_events_not_double_counted(self, api):
        ev1 = _seed_job(api, job_id="job_a")
        ev1.set()  # e.g. user already cancelled this one
        ev2 = _seed_job(api, job_id="job_b")
        n = api.stop_cloud_uploads_for_shutdown()
        assert n == 1  # only the still-running job is freshly signalled
        assert ev2.is_set()

    def test_does_not_touch_persistent_store_or_status(self, api, monkeypatch):
        # Shutdown is local-only: the job keeps running server-side and resumes
        # next launch, so we must NOT flip status or write the ledger.
        store = _FakeStore()
        monkeypatch.setattr(api, "_cc_jobs_store", lambda: store)
        _seed_job(api, job_id="job_a")
        api.stop_cloud_uploads_for_shutdown()
        assert api._cc_jobs["job_a"]["status"] == "uploading"
        assert store.updates == []

    def test_tolerates_missing_cancel_event(self, api):
        api._cc_jobs["job_a"] = {"status": "uploading"}  # no cancel_event key
        assert api.stop_cloud_uploads_for_shutdown() == 0
