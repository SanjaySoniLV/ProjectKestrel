"""Unit tests for the post-exclusion projection that builds the `expected`
body for POST /v1/perches. Server-side per-image / user-wide quota gating
relies on this body being accurate after the user de-selects scenes from
the upload dialog."""

from perch_uploader import (
    PerchPreflight,
    PerchPreflightScene,
    project_expected_after_exclusion,
)


def _scene(sid: str, exports: int, crops: int, total_bytes: int) -> PerchPreflightScene:
    return PerchPreflightScene(
        scene_id=sid,
        title=f"scene {sid}",
        capture_time_ms=None,
        image_count=exports if exports else crops,
        export_count=exports,
        crop_count=crops,
        total_bytes=total_bytes,
        top_quality=None,
    )


def _preflight(*scenes: PerchPreflightScene) -> PerchPreflight:
    return PerchPreflight(
        scene_count=len(scenes),
        image_count=sum(s.image_count for s in scenes),
        export_count=sum(s.export_count for s in scenes),
        crop_count=sum(s.crop_count for s in scenes),
        total_bytes=sum(s.total_bytes for s in scenes),
        file_count=sum(s.export_count + s.crop_count for s in scenes),
        scenes=list(scenes),
    )


class TestProjectExpectedAfterExclusion:
    def test_no_preflight_returns_zeros(self):
        # Old code paths or unusual flows may not have a cached preflight; the
        # server then falls back to legacy count-only check.
        got = project_expected_after_exclusion(None, ())
        assert got == {
            "totalBytes": 0,
            "exportCount": 0,
            "cropCount": 0,
            "fileCount": 0,
        }

    def test_no_exclusions_sums_full_preflight(self):
        pf = _preflight(
            _scene("1", exports=2, crops=3, total_bytes=1_000_000),
            _scene("2", exports=4, crops=0, total_bytes=2_500_000),
        )
        got = project_expected_after_exclusion(pf, ())
        assert got["totalBytes"] == 3_500_000
        assert got["exportCount"] == 6
        assert got["cropCount"] == 3
        assert got["fileCount"] == 9  # exports + crops

    def test_excluding_a_scene_subtracts_its_totals(self):
        pf = _preflight(
            _scene("1", exports=2, crops=3, total_bytes=1_000_000),
            _scene("2", exports=4, crops=0, total_bytes=2_500_000),
        )
        got = project_expected_after_exclusion(pf, ["2"])
        assert got["totalBytes"] == 1_000_000
        assert got["exportCount"] == 2
        assert got["cropCount"] == 3
        assert got["fileCount"] == 5

    def test_excluding_every_scene_yields_zeros(self):
        pf = _preflight(
            _scene("1", exports=2, crops=3, total_bytes=1_000_000),
            _scene("2", exports=4, crops=0, total_bytes=2_500_000),
        )
        got = project_expected_after_exclusion(pf, ["1", "2"])
        assert got == {
            "totalBytes": 0,
            "exportCount": 0,
            "cropCount": 0,
            "fileCount": 0,
        }

    def test_excluded_id_normalised_to_string(self):
        # The uploader stores excluded scene ids as strings but a caller
        # (or older JS bridge build) might pass ints; helper must coerce.
        pf = _preflight(_scene("42", exports=1, crops=0, total_bytes=500))
        got = project_expected_after_exclusion(pf, [42])  # type: ignore[list-item]
        assert got["totalBytes"] == 0
        assert got["exportCount"] == 0
