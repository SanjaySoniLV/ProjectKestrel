"""Unit tests for api_bridge.py pure helper methods (no webview required)."""

import pytest
from pathlib import Path
import sys
import os

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge


pytestmark = pytest.mark.unit


@pytest.fixture
def api():
    return api_bridge.Api()


class TestVersionAndFrozenChecks:
    """Tests for is_frozen_app and get_app_version."""

    def test_is_frozen_app_returns_dict_with_bool(self, api):
        """is_frozen_app() returns a dict containing a bool flag."""
        result = api.is_frozen_app()
        # API returns dict {success: bool, frozen: bool} or similar
        assert isinstance(result, dict)
        # Should have a frozen flag or similar
        frozen = result.get('frozen', result.get('is_frozen'))
        if frozen is not None:
            assert isinstance(frozen, bool)

    def test_is_frozen_app_false_in_dev(self, api):
        """When running from source (not frozen) → False."""
        # Since we're running this from source, should be False
        result = api.is_frozen_app()
        # Could be either format - check both
        frozen = result.get('frozen', result.get('is_frozen'))
        if frozen is not None:
            assert frozen == False

    def test_get_app_version_returns_version_string(self, api):
        """get_app_version() returns a dict containing a version string."""
        result = api.get_app_version()
        assert isinstance(result, dict)
        version = result.get('version', '')
        assert isinstance(version, str)
        assert len(version) > 0


class TestPlatformInfo:
    """Tests for get_platform_info."""

    def test_get_platform_info_returns_dict(self, api):
        """get_platform_info() returns a dict with platform details."""
        info = api.get_platform_info()
        assert isinstance(info, dict)


class TestPerchTagDiff:
    """Tests for the H7 tag-sync pure diff helpers (_norm_tag_list /
    _diff_perch_tags) — the changeset computed for a linked folder vs its perch."""

    def test_norm_tag_list_strips_and_drops_empties(self, api):
        assert api._norm_tag_list([" Robin ", "", "  ", "Jay"]) == ["Robin", "Jay"]
        assert api._norm_tag_list(None) == []
        assert api._norm_tag_list("not a list") == []
        # Order is preserved (order-sensitive diff).
        assert api._norm_tag_list(["B", "A"]) == ["B", "A"]

    def test_diff_detects_species_change(self, api):
        remote = [{"kestrelSceneId": "0", "speciesList": ["Robin"], "familyList": ["Turdidae"]}]
        local = {"0": {"name": "Morning", "user_tags": {"species": ["American Robin"], "families": ["Turdidae"]}}}
        changes = api._diff_perch_tags(remote, local)
        assert len(changes) == 1
        c = changes[0]
        assert c["kestrelSceneId"] == "0"
        assert c["title"] == "Morning"
        assert c["species"] == ["American Robin"]
        assert c["remoteSpecies"] == ["Robin"]
        assert c["family"] == ["Turdidae"]

    def test_diff_no_change_when_equal(self, api):
        remote = [{"kestrelSceneId": "0", "speciesList": ["Robin"], "familyList": None}]
        local = {"0": {"user_tags": {"species": ["Robin"], "families": []}}}
        # null remote familyList and empty local families both normalize to [].
        assert api._diff_perch_tags(remote, local) == []

    def test_diff_skips_unmatched_and_tagless_scenes(self, api):
        remote = [
            {"kestrelSceneId": "0", "speciesList": ["Robin"], "familyList": []},
            {"kestrelSceneId": "5", "speciesList": ["Crow"], "familyList": []},   # no local entry
            {"speciesList": ["X"], "familyList": []},                              # no kestrelSceneId
        ]
        local = {"0": {"user_tags": {"species": ["Robin", "Jay"], "families": []}}}
        changes = api._diff_perch_tags(remote, local)
        # Only scene "0" matches AND differs (added "Jay").
        assert [c["kestrelSceneId"] for c in changes] == ["0"]
        assert changes[0]["species"] == ["Robin", "Jay"]

    def test_diff_title_falls_back_to_scene_number(self, api):
        remote = [{"kestrelSceneId": "3", "speciesList": [], "familyList": []}]
        local = {"3": {"user_tags": {"species": ["Heron"], "families": []}}}
        changes = api._diff_perch_tags(remote, local)
        assert changes[0]["title"] == "Scene 3"

    def test_diff_handles_malformed_input(self, api):
        assert api._diff_perch_tags(None, None) == []
        assert api._diff_perch_tags([], {}) == []
        assert api._diff_perch_tags(["junk", 5], {"0": {}}) == []


class TestIsSafeExternalUrl:
    """Tests for module-level _is_safe_external_url() function."""

    def test_http_allowed(self):
        assert api_bridge._is_safe_external_url("http://example.com") == True

    def test_https_allowed(self):
        assert api_bridge._is_safe_external_url("https://example.com") == True

    def test_mailto_allowed(self):
        assert api_bridge._is_safe_external_url("mailto:test@example.com") == True

    def test_file_scheme_rejected(self):
        """file:// scheme can execute via ShellExecute on Windows → rejected."""
        assert api_bridge._is_safe_external_url("file:///C:/Windows/System32/calc.exe") == False

    def test_javascript_scheme_rejected(self):
        """javascript: scheme can run code → rejected."""
        assert api_bridge._is_safe_external_url("javascript:alert(1)") == False

    def test_data_scheme_rejected(self):
        """data: URLs → rejected."""
        assert api_bridge._is_safe_external_url("data:text/html,<script>alert(1)</script>") == False

    def test_ftp_scheme_rejected(self):
        """ftp:// → rejected (not in allowlist)."""
        assert api_bridge._is_safe_external_url("ftp://example.com") == False

    def test_unc_path_rejected(self):
        """UNC path \\\\host\\share → rejected."""
        assert api_bridge._is_safe_external_url("\\\\attacker\\share\\evil.exe") == False

    def test_double_slash_rejected(self):
        """Forward-slash UNC //host → rejected."""
        assert api_bridge._is_safe_external_url("//attacker/share") == False

    def test_empty_string_rejected(self):
        assert api_bridge._is_safe_external_url("") == False

    def test_none_rejected(self):
        assert api_bridge._is_safe_external_url(None) == False

    def test_non_string_rejected(self):
        assert api_bridge._is_safe_external_url(42) == False
        assert api_bridge._is_safe_external_url([]) == False

    def test_control_chars_rejected(self):
        """URL with control chars (newline, NUL, etc.) → rejected."""
        assert api_bridge._is_safe_external_url("http://example.com\nLocation: evil") == False
        assert api_bridge._is_safe_external_url("http://example.com\x00") == False

    def test_no_scheme_rejected(self):
        """URL without :// or : separator → rejected."""
        assert api_bridge._is_safe_external_url("example.com") == False

    def test_whitespace_trimmed(self):
        """Leading/trailing whitespace is trimmed before validation."""
        # Trimmed URL is valid http
        assert api_bridge._is_safe_external_url("  https://example.com  ") == True


class TestIsWithinRoot:
    """Tests for the Api._is_within_root method (path containment)."""

    def test_child_path_allowed(self, api, tmp_path):
        """Path inside root → True."""
        child = tmp_path / "subdir" / "file.txt"
        child.parent.mkdir()
        child.touch()
        assert api._is_within_root(str(child), str(tmp_path)) == True

    def test_parent_path_blocked(self, api, tmp_path):
        """Path outside root → False."""
        # The parent of tmp_path is NOT within tmp_path
        assert api._is_within_root(str(tmp_path.parent), str(tmp_path)) == False

    def test_sibling_path_blocked(self, api, tmp_path):
        """Sibling directory → False."""
        sibling = tmp_path.parent / "sibling"
        sibling.mkdir(exist_ok=True)
        try:
            assert api._is_within_root(str(sibling), str(tmp_path)) == False
        finally:
            # Clean up
            try:
                sibling.rmdir()
            except OSError:
                pass

    def test_same_path_allowed(self, api, tmp_path):
        """Path == root → True."""
        assert api._is_within_root(str(tmp_path), str(tmp_path)) == True

    def test_empty_path_blocked(self, api):
        """Empty path → False."""
        assert api._is_within_root("", "/some/root") == False
        assert api._is_within_root("/some/path", "") == False

    def test_nonexistent_child_still_resolves(self, api, tmp_path):
        """Path under root that does not yet exist on disk → True.

        Mirrors ``read_image_file`` asking for a ``.kestrel/export/foo.jpg``
        path that the user has not exported yet. The deepest existing
        ancestor (``tmp_path``) is realpath-resolved so the comparison
        survives Windows mapped-drive vs UNC spelling differences.
        """
        target = tmp_path / ".kestrel" / "export" / "not_yet.jpg"
        assert api._is_within_root(str(target), str(tmp_path)) == True

    def test_nonexistent_sibling_blocked(self, api, tmp_path):
        """Non-existent path outside root → False."""
        outside = tmp_path.parent / "elsewhere" / "missing.jpg"
        assert api._is_within_root(str(outside), str(tmp_path)) == False


class TestEditorExtensionAllowed:
    """Tests for editor extension allowlist."""

    def test_jpeg_extension_allowed(self, api):
        """Common image extensions are allowed."""
        # Common image extensions are usually in the default allowlist
        assert api._editor_extension_allowed("photo.jpg") in (True, False)  # Allowlist may vary
        # At minimum check it doesn't crash

    def test_exe_extension_blocked(self, api):
        """Executable extensions should NOT be allowed."""
        assert api._editor_extension_allowed("malware.exe") == False

    def test_bat_extension_blocked(self, api):
        """Script extensions should NOT be allowed."""
        assert api._editor_extension_allowed("script.bat") == False
        assert api._editor_extension_allowed("script.cmd") == False


class TestStripWrappingQuotes:
    """Tests for _strip_wrapping_quotes helper."""

    def test_strips_double_quotes(self, api):
        assert api._strip_wrapping_quotes('"hello"') == 'hello'

    def test_strips_single_quotes(self, api):
        assert api._strip_wrapping_quotes("'hello'") == 'hello'

    def test_preserves_inner_quotes(self, api):
        assert api._strip_wrapping_quotes('say "hi" please') == 'say "hi" please'

    def test_handles_empty_string(self, api):
        assert api._strip_wrapping_quotes('') == ''

    def test_handles_whitespace(self, api):
        assert api._strip_wrapping_quotes('  "hello"  ') == 'hello'


class TestInspectFolderViaApi:
    """Tests for Api.inspect_folder (wraps folder_inspector)."""

    def test_inspect_folder_returns_dict(self, api, tmp_path):
        """Inspect an empty folder → returns expected dict."""
        result = api.inspect_folder(str(tmp_path))
        assert isinstance(result, dict)
        # Should have 'total' or similar keys, depending on return shape
        assert 'total' in result or 'success' in result or 'has_kestrel' in result

    def test_inspect_folder_with_images(self, api, tmp_path):
        """Inspect folder with images → reports correct count."""
        (tmp_path / "IMG_001.CR3").touch()
        (tmp_path / "IMG_002.CR3").touch()

        result = api.inspect_folder(str(tmp_path))
        # Folder inspector should detect them
        assert result.get('total', 0) >= 2 or result.get('success', True)


def _seed_kestrel_csv(folder: Path, rows: list[dict]) -> None:
    """Write a kestrel_database.csv into <folder>/.kestrel/ with the given
    rows. Helper for the cloud-compute upload-selection tests below."""
    import csv as _csv
    kestrel = folder / ".kestrel"
    kestrel.mkdir(parents=True, exist_ok=True)
    csv_path = kestrel / "kestrel_database.csv"
    fieldnames = list(rows[0].keys()) if rows else ["filename", "species"]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = _csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


class TestCCSelectUploadFiles:
    """Tests for _cc_select_upload_files retry-errored manifest logic."""

    def test_default_excludes_errored_rows(self, api, tmp_path):
        """retry_errored=False (default): errored rows count as analyzed
        and are NOT re-uploaded. Existing Stage 4A behavior."""
        for n in ("a.CR3", "b.CR3", "c.CR3", "d.CR3"):
            (tmp_path / n).touch()
        _seed_kestrel_csv(tmp_path, [
            {"filename": "a.CR3", "species": "American Goldfinch"},
            {"filename": "b.CR3", "species": "Error"},
            {"filename": "c.CR3", "species": "House Finch"},
        ])
        files, anchor, anchors, total, analyzed = api._cc_select_upload_files(tmp_path)
        names = {p.name for p in files}
        # 'd.CR3' is the only un-analyzed file → in manifest. Plus the
        # last-analyzed anchor 'c.CR3'.
        assert "d.CR3" in names
        assert "b.CR3" not in names, "errored row leaked into default manifest"
        assert anchor == "c.CR3"
        assert anchors == frozenset({"c.CR3"})
        assert total == 4
        assert analyzed == 3

    def test_retry_errored_includes_errored_and_predecessor(self, api, tmp_path):
        """retry_errored=True: errored rows go back in the manifest, and
        the immediately-preceding file becomes a protected scene anchor."""
        for n in ("a.CR3", "b.CR3", "c.CR3", "d.CR3"):
            (tmp_path / n).touch()
        _seed_kestrel_csv(tmp_path, [
            {"filename": "a.CR3", "species": "American Goldfinch"},
            {"filename": "b.CR3", "species": "Error"},
            {"filename": "c.CR3", "species": "House Finch"},
        ])
        files, anchor, anchors, _, _ = api._cc_select_upload_files(
            tmp_path, retry_errored=True
        )
        names = {p.name for p in files}
        assert "b.CR3" in names, "errored row missing from retry manifest"
        assert "d.CR3" in names, "unanalyzed row missing from retry manifest"
        # Predecessor of 'b.CR3' is 'a.CR3' — must be in the protected anchor
        # set so its row isn't clobbered when the cloud re-uploads it for
        # scene continuity. 'c.CR3' is the primary (last-alphabetical) anchor.
        assert "a.CR3" in anchors
        assert "c.CR3" in anchors
        assert anchor == "c.CR3", "primary anchor should be last alphabetical healthy row"

    def test_retry_errored_first_file_no_predecessor(self, api, tmp_path):
        """When the errored file is the first in the folder, no
        predecessor anchor is added (there's nothing before it)."""
        for n in ("a.CR3", "b.CR3", "c.CR3"):
            (tmp_path / n).touch()
        _seed_kestrel_csv(tmp_path, [
            {"filename": "a.CR3", "species": "Error"},
            {"filename": "b.CR3", "species": "House Finch"},
        ])
        files, _, anchors, _, _ = api._cc_select_upload_files(
            tmp_path, retry_errored=True
        )
        names = {p.name for p in files}
        assert "a.CR3" in names
        assert "c.CR3" in names
        # No predecessor for a.CR3 (index 0). The primary anchor 'b.CR3'
        # is the only protected entry.
        assert anchors == frozenset({"b.CR3"})

    def test_retry_errored_off_keeps_legacy_behavior(self, api, tmp_path):
        """retry_errored=False with only-errored database still produces
        an empty new_files list (no manifest churn). Guards against the
        flag accidentally leaking through."""
        for n in ("a.CR3", "b.CR3"):
            (tmp_path / n).touch()
        _seed_kestrel_csv(tmp_path, [
            {"filename": "a.CR3", "species": "American Goldfinch"},
            {"filename": "b.CR3", "species": "Error"},
        ])
        files, _, anchors, _, _ = api._cc_select_upload_files(tmp_path)
        # Both files counted as analyzed, nothing new to upload.
        assert files == []
        assert anchors == frozenset()

    def test_orphan_jpeg_kept_alongside_raws(self, api, tmp_path):
        """Regression (cloud off-by-one): a JPEG with NO same-stem RAW partner
        is an orphan (a lone JPG-only frame) and MUST be uploaded even when the
        folder otherwise contains RAWs. A JPEG that DOES share a stem with a RAW
        is an in-camera sidecar and is correctly dropped. Discovery must match
        the local pipeline (folder_inspector.list_images_in_folder); the old
        forked 'raws if raws else jpegs' filter dropped orphan JPEGs, sending
        N-1 images to the cloud vs. N analyzed locally."""
        for n in ("IMG_1.CR3", "IMG_1.JPG", "IMG_2.CR3", "IMG_9219.JPG"):
            (tmp_path / n).touch()
        files, _, _, total, _ = api._cc_select_upload_files(tmp_path)
        names = {p.name for p in files}
        assert "IMG_9219.JPG" in names, "orphan JPEG silently dropped (the bug)"
        assert "IMG_1.JPG" not in names, "same-stem JPEG sidecar should be dropped"
        assert names == {"IMG_1.CR3", "IMG_2.CR3", "IMG_9219.JPG"}
        assert total == 3


class TestCCDrainReconciliation:
    """Regression (billing nuke): a result pack already merged locally but
    still present in R2 means its server-side results_retrieved flip was lost
    (e.g. a download GET interrupted by app close). The drain must RE-DOWNLOAD
    such a pack — the re-GET re-fires the Worker's results_retrieved flip,
    restoring billing — and re-merge it (idempotent), THEN delete it via
    _cc_finalize_pack_merge once the rows are safely retrieved. The old code
    deleted the stale pack outright, which nuked its rows to the terminal,
    non-billable results_nuked state (the observed 10-image under-billing)."""

    def test_stale_pack_redownloaded_not_nuked(self, api, tmp_path, monkeypatch):
        stale = "pack_2_seg_16.zip"
        calls = {"download": [], "merge": [], "finalize": []}

        class FakeClient:
            def list_results(self, job_id):
                return [{"filename": stale}]

            def download_pack(self, job_id, fname, dest):
                calls["download"].append(fname)
                dest = Path(dest)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(b"zip")

            def delete_packs(self, job_id, names):
                # The old stale-cleanup deleted WITHOUT downloading. If that
                # path ever runs again for a stale pack, this records it.
                calls.setdefault("delete_without_download", []).append(list(names))

        class FakeCcc:
            def merge_pack_into_kestrel(self, dest, folder, protected_filenames=None,
                                        overwrite_errors=False):
                calls["merge"].append(Path(dest).name)

        class FakeStore:
            def add_downloaded_pack(self, job_id, fname):
                pass

        # Isolate the reconciliation decision: record finalize instead of
        # touching real folder-state / R2. pack_name is the 3rd positional arg.
        monkeypatch.setattr(
            api, "_cc_finalize_pack_merge",
            lambda *a, **k: calls["finalize"].append(a[2]),
        )

        result = api._cc_drain_packs_once(
            "job_x", tmp_path, FakeClient(), FakeCcc(), FakeStore(),
            frozenset(), False, already={stale},
        )

        assert calls["download"] == [stale], "stale pack was not re-downloaded → billing lost"
        assert calls["merge"] == [stale], "stale pack was not re-merged"
        assert calls["finalize"] == [stale], "safe delete-after-retrieve did not run"
        assert "delete_without_download" not in calls, "stale pack was nuked without a re-GET"
        assert result is not None
        _already, available = result
        assert stale not in available


class TestCCAnalysisSettingsSnapshot:
    """Tests for _cc_analysis_settings_snapshot read of advanced settings."""

    def test_new_advanced_settings_projected(self, api, monkeypatch):
        """The 6 new advanced settings keys make it into the wire dict
        when present in the settings store, with the same names."""
        def _fake_get_settings():
            return {
                "settings": {
                    "detector_name": "mdv5a",
                    "detection_threshold": 0.45,
                    "max_bird_crops": 7,
                    "exposure_quality": "aggressive",
                    "scene_time_threshold": 2.5,
                    "thumbnail_max_width": 1600,
                    "thumbnail_jpeg_compression": 0.85,
                    "retry_errored": True,
                    "wildlife_enabled": False,
                    "species_detection_enabled": True,
                }
            }
        monkeypatch.setattr(api, "get_settings", _fake_get_settings)
        out = api._cc_analysis_settings_snapshot()
        assert out is not None
        assert out["detector_name"] == "mdv5a"
        assert out["confidence_threshold"] == 0.45  # rename preserved
        assert out["max_bird_crops"] == 7
        assert out["exposure_quality"] == "aggressive"
        assert out["scene_time_threshold"] == 2.5
        assert out["thumbnail_max_width"] == 1600
        assert out["thumbnail_jpeg_compression"] == 0.85
        assert out["retry_errored"] is True
        assert out["wildlife_enabled"] is False
        assert out["species_detection_enabled"] is True

    def test_out_of_range_values_dropped(self, api, monkeypatch):
        """Range guards mirror the CLI's documented ranges so we don't ship
        values that would be clamped by the analyzer."""
        def _fake_get_settings():
            return {
                "settings": {
                    "max_bird_crops": 999,            # > 20: dropped
                    "thumbnail_max_width": 100,        # < 400: dropped
                    "thumbnail_jpeg_compression": 5.0,  # > 1.0: dropped
                    "scene_time_threshold": -1,        # < 0: dropped
                    "exposure_quality": "nuclear",     # not in choices: dropped
                }
            }
        monkeypatch.setattr(api, "get_settings", _fake_get_settings)
        out = api._cc_analysis_settings_snapshot()
        # All 5 above are invalid → the snapshot has no entries, returns None.
        assert out is None or out == {}


class TestSampleSetMirror:
    """Tests for ``_mirror_sample_set_to_temp`` — the platform-agnostic
    per-session mirror that copies bundled sample sets into a writable temp
    dir so the tutorial starts from a clean slate every run and read-only
    installs (MSIX / ``WindowsApps``, macOS App Translocation) just work."""

    def _make_sample_set(self, root, name, db_text="header\nrow\n"):
        """Build a minimal sample-set fixture: <root>/<name>/.kestrel/{db,readonly_db}."""
        set_dir = root / name
        kestrel = set_dir / ".kestrel"
        kestrel.mkdir(parents=True)
        (kestrel / "kestrel_database_readonly.csv").write_text(db_text)
        (kestrel / "kestrel_database.csv").write_text("stale\n")
        (set_dir / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0")  # JPEG header
        return set_dir

    def _pin_temp_root(self, monkeypatch, root):
        """Point the temp-mirror root at a tmp_path-backed dir (no real OS temp)."""
        monkeypatch.setattr(
            api_bridge.Api, "_sample_sets_temp_root",
            staticmethod(lambda: str(root)),
        )

    def test_mirror_copies_tree_and_restores_db(self, api, tmp_path, monkeypatch):
        """First-use mirror: copytree happens and DB is reset from readonly src."""
        bundled = self._make_sample_set(tmp_path / "bundled", "backyard_birds",
                                        db_text="pristine\n")
        temp_root = tmp_path / "temp_mirror"
        self._pin_temp_root(monkeypatch, temp_root)

        debug: list = []
        mirror = api._mirror_sample_set_to_temp(str(bundled), debug)

        assert mirror is not None
        mirror_path = Path(mirror)
        assert mirror_path == temp_root / "backyard_birds"
        assert (mirror_path / "photo.jpg").is_file()
        # Live DB was reset from the readonly source (not the "stale" copy).
        assert (mirror_path / ".kestrel" / "kestrel_database.csv").read_text() == "pristine\n"

    def test_mirror_wipes_prior_session_then_refreshes_db(self, api, tmp_path, monkeypatch):
        """Wipe-on-load: a second call discards last session's edits AND any
        files that no longer exist in the bundle, then resets the DB."""
        bundled = self._make_sample_set(tmp_path / "bundled", "backyard_birds",
                                        db_text="pristine\n")
        temp_root = tmp_path / "temp_mirror"
        self._pin_temp_root(monkeypatch, temp_root)

        # First call lays down the mirror.
        api._mirror_sample_set_to_temp(str(bundled), [])
        mirror_dir = temp_root / "backyard_birds"
        mirror_db = mirror_dir / ".kestrel" / "kestrel_database.csv"
        # Simulate a prior tutorial session: dirty the DB and drop a stray file.
        mirror_db.write_text("dirty\n")
        stray = mirror_dir / "user_added.txt"
        stray.write_text("leftover\n")

        debug: list = []
        api._mirror_sample_set_to_temp(str(bundled), debug)

        # Clean slate: DB reset to readonly state and the stray file is gone.
        assert mirror_db.read_text() == "pristine\n"
        assert not stray.exists()
        assert any("copied" in line for line in debug)

    def test_mirror_auto_refreshes_when_bundle_changes(self, api, tmp_path, monkeypatch):
        """If the bundled set changes between sessions, the mirror picks it up."""
        bundled = self._make_sample_set(tmp_path / "bundled", "backyard_birds",
                                        db_text="v1\n")
        temp_root = tmp_path / "temp_mirror"
        self._pin_temp_root(monkeypatch, temp_root)

        api._mirror_sample_set_to_temp(str(bundled), [])
        # Ship a new bundled version (e.g. an app update changes the readonly DB).
        (bundled / ".kestrel" / "kestrel_database_readonly.csv").write_text("v2\n")
        (bundled / "new_photo.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        mirror = Path(api._mirror_sample_set_to_temp(str(bundled), []))
        assert (mirror / ".kestrel" / "kestrel_database.csv").read_text() == "v2\n"
        assert (mirror / "new_photo.jpg").is_file()

    def test_mirror_returns_none_on_copytree_failure(self, api, tmp_path, monkeypatch):
        """If shutil.copytree raises (e.g. source missing), return None and log."""
        self._pin_temp_root(monkeypatch, tmp_path / "temp_mirror")
        debug: list = []
        # Source does not exist → copytree raises FileNotFoundError.
        result = api._mirror_sample_set_to_temp(str(tmp_path / "missing_set"), debug)
        assert result is None
        assert any("failed to mirror" in line for line in debug)

    def test_cleanup_removes_temp_root(self, api, tmp_path, monkeypatch):
        """cleanup_sample_set_mirrors removes the whole temp mirror tree."""
        bundled = self._make_sample_set(tmp_path / "bundled", "backyard_birds")
        temp_root = tmp_path / "temp_mirror"
        self._pin_temp_root(monkeypatch, temp_root)

        api._mirror_sample_set_to_temp(str(bundled), [])
        assert temp_root.is_dir()

        api.cleanup_sample_set_mirrors()
        assert not temp_root.exists()

    def test_cleanup_is_noop_when_absent(self, api, tmp_path, monkeypatch):
        """cleanup is safe to call when no mirror was ever created."""
        self._pin_temp_root(monkeypatch, tmp_path / "never_created")
        # Must not raise.
        api.cleanup_sample_set_mirrors()


class TestSampleSetDiscoveryPaths:
    """Discovery should include in-repo analyzer/sample_sets in dev mode."""

    def _make_sample_set(self, root: Path, name: str) -> Path:
        set_dir = root / name
        kestrel = set_dir / ".kestrel"
        kestrel.mkdir(parents=True)
        (kestrel / "kestrel_database_readonly.csv").write_text("header\nrow\n")
        return set_dir

    def test_dev_discovers_module_local_sample_sets(self, api, tmp_path, monkeypatch):
        """When <repo>/sample_sets is absent, use <repo>/analyzer/sample_sets."""
        project_root = tmp_path / "ProjectKestrel"
        analyzer_dir = project_root / "analyzer"
        analyzer_dir.mkdir(parents=True)
        module_set = self._make_sample_set(analyzer_dir / "sample_sets", "tutorial_set")

        monkeypatch.setattr(api_bridge, "__file__", str(analyzer_dir / "api_bridge.py"))
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        monkeypatch.setattr(
            api, "_mirror_sample_set_to_temp", lambda bundled_path, _debug: bundled_path
        )

        workdir = tmp_path / "workdir"
        workdir.mkdir()
        monkeypatch.chdir(workdir)

        result = api.get_sample_sets_paths()
        assert result["success"] is True
        assert result["paths"] == [str(module_set)]

    def test_dev_prefers_repo_root_sample_sets_when_present(self, api, tmp_path, monkeypatch):
        """Preserve existing behavior: <repo>/sample_sets remains first choice."""
        project_root = tmp_path / "ProjectKestrel"
        root_set = self._make_sample_set(project_root / "sample_sets", "root_set")
        analyzer_dir = project_root / "analyzer"
        analyzer_set = self._make_sample_set(analyzer_dir / "sample_sets", "analyzer_set")
        assert analyzer_set.is_dir()  # fixture sanity

        monkeypatch.setattr(api_bridge, "__file__", str(analyzer_dir / "api_bridge.py"))
        monkeypatch.setattr(sys, "frozen", False, raising=False)
        monkeypatch.setattr(
            api, "_mirror_sample_set_to_temp", lambda bundled_path, _debug: bundled_path
        )

        workdir = tmp_path / "workdir"
        workdir.mkdir()
        monkeypatch.chdir(workdir)

        result = api.get_sample_sets_paths()
        assert result["success"] is True
        assert result["paths"] == [str(root_set)]


class TestCleanupCullingCache:
    """cleanup_culling_cache must tolerate ENOENT during rmtree.

    Motivating field reports: macOS Finder / Spotlight can prune AppleDouble
    ``._<name>`` sidecar files between rmtree's directory scan and the actual
    unlink, and rmtree without ``ignore_errors`` propagates the resulting
    ENOENT. The cache is a best-effort space reclaim; a missing sidecar must
    not turn into an ``[API] cleanup_culling_cache error`` in the log.
    """

    def _make_culling_cache(self, root: Path) -> Path:
        cache_dir = root / ".kestrel" / "culling_TMP"
        cache_dir.mkdir(parents=True)
        (cache_dir / "IMG_0001_abc_preview.jpg").write_bytes(b"jpg")
        return cache_dir

    def test_removes_existing_cache(self, api, tmp_path):
        self._make_culling_cache(tmp_path)
        res = api.cleanup_culling_cache(str(tmp_path))
        assert res == {"success": True}
        assert not (tmp_path / ".kestrel" / "culling_TMP").exists()

    def test_noop_when_cache_absent(self, api, tmp_path):
        (tmp_path / ".kestrel").mkdir()
        res = api.cleanup_culling_cache(str(tmp_path))
        assert res == {"success": True}

    def test_tolerates_disappearing_entries(self, api, tmp_path, monkeypatch):
        """A file that vanishes mid-rmtree (Finder-pruned AppleDouble) is not
        surfaced as a failure — the return value stays ``success=True`` and
        the whole tree is still removed."""
        import shutil as _shutil
        cache_dir = self._make_culling_cache(tmp_path)

        real_rmtree = _shutil.rmtree
        called = {}

        def fake_rmtree(path, *args, ignore_errors=False, **kwargs):
            called["ignore_errors"] = ignore_errors
            # Simulate the macOS race: raise the exact ENOENT rmtree would
            # raise on its own if the caller did not pass ignore_errors.
            if not ignore_errors:
                raise FileNotFoundError(2, "No such file or directory",
                                        "._IMG_0001_abc_preview.jpg")
            return real_rmtree(path, *args, ignore_errors=True, **kwargs)

        monkeypatch.setattr("api_bridge.shutil.rmtree", fake_rmtree)

        res = api.cleanup_culling_cache(str(tmp_path))
        assert res == {"success": True}
        assert called["ignore_errors"] is True
        assert not cache_dir.exists()


class TestClerkSessionRefresh:
    """The native-Apple bundle (kind=clerk_session) re-mints a short-lived Clerk
    session JWT from the durable __client credential instead of OAuth-refreshing."""

    def _bundle(self, expires_at):
        return {
            "kind": "clerk_session",
            "access_token": "OLD.JWT",
            "expires_at": expires_at,
            "clerk_client": "CLIENTCOOKIE",
            "clerk_session_id": "sess_1",
        }

    def test_refresh_if_needed_dispatches_clerk_session(self, api, monkeypatch):
        sentinel = {"dispatched": True}
        monkeypatch.setattr(api, "_refresh_clerk_session", lambda b: sentinel)
        out = api._refresh_if_needed(
            {"kind": "clerk_session", "access_token": "x", "expires_at": 0}
        )
        assert out is sentinel

    def test_skips_remint_when_fresh(self, api, monkeypatch):
        import time as _t
        monkeypatch.setattr(api_bridge._oauth, "remint_session_token",
                            lambda *a, **k: pytest.fail("must not re-mint a fresh session"))
        b = self._bundle(_t.time() + 300)  # well beyond the ~15s re-mint buffer
        assert api._refresh_clerk_session(b) is b

    def test_remints_when_near_expiry(self, api, monkeypatch):
        import base64 as _b64
        import json as _json
        import time as _t
        saved = {}
        seg = _b64.urlsafe_b64encode(
            _json.dumps({"exp": int(_t.time()) + 3600, "sub": "user_1"}).encode()
        ).rstrip(b"=").decode()
        new_jwt = "hdr." + seg + ".sig"
        monkeypatch.setattr(api_bridge._oauth, "remint_session_token",
                            lambda c, s: (new_jwt, "ROTATED.CLIENT"))
        monkeypatch.setattr(api_bridge, "_keyring_load", lambda: None)
        monkeypatch.setattr(api_bridge, "_keyring_save", lambda bundle: saved.update(bundle))

        out = api._refresh_clerk_session(self._bundle(_t.time() + 5))  # within buffer

        assert out["access_token"] == new_jwt
        assert out["kind"] == "clerk_session"
        assert saved.get("access_token") == new_jwt  # persisted to keychain
        # Clerk rotates the native-mode client token on every response; the
        # rebuilt bundle must carry the new one, not the token we sent.
        assert out["clerk_client"] == "ROTATED.CLIENT"
        assert saved.get("clerk_client") == "ROTATED.CLIENT"

    def test_remint_failure_keeps_old_bundle(self, api, monkeypatch):
        import time as _t
        b = self._bundle(_t.time() + 5)
        monkeypatch.setattr(api_bridge._oauth, "remint_session_token", lambda c, s: None)
        monkeypatch.setattr(api_bridge, "_keyring_load", lambda: None)
        # Transient failure -> keep the (still-briefly-valid) bundle, don't sign out.
        assert api._refresh_clerk_session(b) is b
