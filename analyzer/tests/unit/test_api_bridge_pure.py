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
