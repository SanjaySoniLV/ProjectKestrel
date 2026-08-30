"""Unit tests for cloud-compute job submit error surfacing (503 cloud_busy)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge
import cloud_compute_client as ccc

pytestmark = pytest.mark.unit


@pytest.fixture
def api():
    return api_bridge.Api()


class TestCloudBusySubmitError:
    def test_submit_error_response_maps_cloud_busy(self, api):
        exc = ccc.CloudComputeError(
            503,
            json.dumps({"error": "cloud_busy", "message": "Cloud Compute is busy"}),
        )
        r = api._cc_submit_error_response(exc)
        assert r is not None
        assert r["ok"] is False
        assert r["errorCode"] == "cloud_busy"
        assert r["status"] == 503
        assert r["error"] == api_bridge._CC_CLOUD_BUSY_USER_MESSAGE

    def test_entitlements_503_not_mapped(self, api):
        exc = ccc.CloudComputeError(
            503,
            json.dumps({"error": "entitlements_unavailable", "message": "try later"}),
        )
        assert api._cc_submit_error_response(exc) is None

    def test_non_503_not_mapped(self, api):
        exc = ccc.CloudComputeError(403, json.dumps({"error": "job_in_progress"}))
        assert api._cc_submit_error_response(exc) is None

    def test_submit_job_surfaces_cloud_busy_message(self, api, tmp_path, monkeypatch):
        folder = tmp_path / "photos"
        folder.mkdir()
        (folder / "IMG_0001.CR3").write_bytes(b"x")

        class FakeClient:
            def submit_job(self, *args, **kwargs):
                raise ccc.CloudComputeError(
                    503,
                    json.dumps({"error": "cloud_busy"}),
                )

        monkeypatch.setattr(api, "_cc_import", lambda: ccc)
        monkeypatch.setattr(api, "_cc_make_client", lambda: (FakeClient(), None))
        monkeypatch.setattr(
            api,
            "_cc_select_upload_files",
            lambda *args, **kwargs: ([folder / "IMG_0001.CR3"], None, [], 1, 0),
        )
        monkeypatch.setattr(api, "_cc_analysis_settings_snapshot", lambda: {})

        r = api.cloud_compute_submit_job(str(folder))
        assert r["ok"] is False
        assert r["error"] == api_bridge._CC_CLOUD_BUSY_USER_MESSAGE
        assert r.get("errorCode") == "cloud_busy"
