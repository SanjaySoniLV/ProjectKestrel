"""Unit tests for Api.move_rejects_to_folder and Api.undo_reject_move.

Uses set_e_raw_jpg_mix fixtures for RAW+JPG companion file testing.
"""

import os
import pytest
import shutil
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge


pytestmark = pytest.mark.unit


def _require_case_sensitive_fs(directory: Path) -> None:
    """Skip if this filesystem cannot host two names that differ only by case."""
    upper = directory / "CaseProbe.tmp"
    lower = directory / "caseprobe.tmp"
    upper.write_bytes(b"U")
    try:
        lower.write_bytes(b"L")
    except OSError:
        pytest.skip("filesystem cannot create a second case variant")
    listed = {
        p.name
        for p in directory.iterdir()
        if p.suffix == ".tmp" and p.name.lower() == "caseprobe.tmp"
    }
    if listed != {"CaseProbe.tmp", "caseprobe.tmp"}:
        pytest.skip("filesystem folds case; cannot host two case variants")
    upper.unlink()
    lower.unlink()


@pytest.fixture
def api():
    """Create an Api instance for testing."""
    return api_bridge.Api()


@pytest.fixture
def workdir_with_files(tmp_path):
    """Create a temp workdir with RAW+JPG companion files."""
    # Create real placeholder files
    (tmp_path / "IMG_001.CR3").write_bytes(b"\x00" * 100)
    (tmp_path / "IMG_001.jpg").write_bytes(b"\x00" * 100)
    (tmp_path / "IMG_002.CR3").write_bytes(b"\x00" * 100)
    (tmp_path / "IMG_002.jpg").write_bytes(b"\x00" * 100)
    return tmp_path


@pytest.fixture
def workdir_with_xmp_sidecar(tmp_path):
    """Create a temp workdir with RAW + XMP sidecar."""
    (tmp_path / "IMG_001.CR3").write_bytes(b"\x00" * 100)
    (tmp_path / "IMG_001.xmp").write_text("<xmp_placeholder/>", encoding='utf-8')
    return tmp_path


class TestMoveRejects:
    """Tests for Api.move_rejects_to_folder."""

    def test_move_single_file_creates_reject_folder(self, api, workdir_with_files):
        """Moving a file creates _KESTREL_Rejects folder and moves file there."""
        result = api.move_rejects_to_folder(str(workdir_with_files), ['IMG_001.CR3'])

        assert result['success'] == True
        # Reject folder should exist
        reject_dir = workdir_with_files / '_KESTREL_Rejects'
        assert reject_dir.is_dir()
        # File should be moved
        assert (reject_dir / 'IMG_001.CR3').exists()
        # Original should be gone
        assert not (workdir_with_files / 'IMG_001.CR3').exists()

    def test_move_cr3_moves_companion_jpg(self, api, workdir_with_files):
        """Moving CR3 → companion JPG also moved."""
        result = api.move_rejects_to_folder(str(workdir_with_files), ['IMG_001.CR3'])
        assert result['success'] == True

        reject_dir = workdir_with_files / '_KESTREL_Rejects'
        # Both files should be in reject folder
        assert (reject_dir / 'IMG_001.CR3').exists()
        assert (reject_dir / 'IMG_001.jpg').exists()
        # Originals should be gone
        assert not (workdir_with_files / 'IMG_001.CR3').exists()
        assert not (workdir_with_files / 'IMG_001.jpg').exists()

    def test_move_cr3_moves_xmp_sidecar(self, api, workdir_with_xmp_sidecar):
        """Moving CR3 → XMP sidecar also moved."""
        result = api.move_rejects_to_folder(str(workdir_with_xmp_sidecar), ['IMG_001.CR3'])
        assert result['success'] == True

        reject_dir = workdir_with_xmp_sidecar / '_KESTREL_Rejects'
        assert (reject_dir / 'IMG_001.CR3').exists()
        # XMP sidecar should follow the RAW
        assert (reject_dir / 'IMG_001.xmp').exists()

    def test_only_specified_files_moved(self, api, workdir_with_files):
        """Moving IMG_001 → IMG_002 stays in root."""
        api.move_rejects_to_folder(str(workdir_with_files), ['IMG_001.CR3'])

        # IMG_002 should NOT be moved
        assert (workdir_with_files / 'IMG_002.CR3').exists()
        assert (workdir_with_files / 'IMG_002.jpg').exists()

    def test_move_returns_count(self, api, workdir_with_files):
        """Move result includes count of moved files (including companions)."""
        result = api.move_rejects_to_folder(str(workdir_with_files), ['IMG_001.CR3'])
        # Should be at least 2 (CR3 + JPG companion)
        assert result['moved'] >= 2

    def test_invalid_path_returns_error(self, api):
        """Invalid root path → error response."""
        result = api.move_rejects_to_folder('/nonexistent/path/that/does/not/exist', ['IMG_001.CR3'])
        assert result['success'] == False
        assert 'error' in result

    def test_traversal_filename_rejected(self, api, workdir_with_files):
        """Filename with traversal → rejected, not moved."""
        result = api.move_rejects_to_folder(str(workdir_with_files), ['../../../etc/passwd'])
        # Either the request succeeds with errors for the bad filename, or it fails entirely
        # In either case, no file should be written outside the root
        # And the existing files should not be affected
        assert (workdir_with_files / 'IMG_001.CR3').exists()
        # Look for error in result
        if result.get('success'):
            assert len(result.get('errors', [])) > 0

    def test_empty_filename_list_no_op(self, api, workdir_with_files):
        """Empty filename list → no-op, no errors."""
        result = api.move_rejects_to_folder(str(workdir_with_files), [])
        # Should succeed but move nothing
        assert result['success'] == True
        # Original files still in place
        assert (workdir_with_files / 'IMG_001.CR3').exists()


class TestUndoRejectMove:
    """Tests for Api.undo_reject_move."""

    def test_undo_restores_file(self, api, workdir_with_files):
        """Move then undo restores the file."""
        api.move_rejects_to_folder(str(workdir_with_files), ['IMG_001.CR3'])
        # Verify moved
        assert not (workdir_with_files / 'IMG_001.CR3').exists()

        # Undo
        result = api.undo_reject_move(str(workdir_with_files), ['IMG_001.CR3'])
        assert result['success'] == True

        # File restored
        assert (workdir_with_files / 'IMG_001.CR3').exists()
        # Companion JPG also restored
        assert (workdir_with_files / 'IMG_001.jpg').exists()

    def test_undo_restores_xmp_sidecar(self, api, workdir_with_xmp_sidecar):
        """Move with XMP sidecar then undo → both restored."""
        api.move_rejects_to_folder(str(workdir_with_xmp_sidecar), ['IMG_001.CR3'])
        api.undo_reject_move(str(workdir_with_xmp_sidecar), ['IMG_001.CR3'])

        assert (workdir_with_xmp_sidecar / 'IMG_001.CR3').exists()
        assert (workdir_with_xmp_sidecar / 'IMG_001.xmp').exists()

    def test_undo_without_reject_folder_returns_error(self, api, workdir_with_files):
        """Undo when no _KESTREL_Rejects folder exists → error."""
        result = api.undo_reject_move(str(workdir_with_files), ['IMG_001.CR3'])
        assert result['success'] == False


class TestRawJpgMixFixtures:
    """Tests using the real set_e_raw_jpg_mix fixture data."""

    def test_set_e_fixtures_exist(self, set_e_path):
        """Verify set_e_raw_jpg_mix/ fixtures are present."""
        if not set_e_path.exists():
            pytest.skip(f"Test fixtures not present at {set_e_path}")
        # Check we have at least one pair
        cr2_files = list(set_e_path.glob("*.CR2"))
        cr3_files = list(set_e_path.glob("*.CR3"))
        jpg_files = list(set_e_path.glob("*.JPG")) + list(set_e_path.glob("*.jpg"))

        raw_files = cr2_files + cr3_files
        assert len(raw_files) > 0, "Expected at least one RAW file"
        assert len(jpg_files) > 0, "Expected at least one JPG file"

    def test_move_raw_with_real_jpg_companion(self, api, set_e_path, tmp_path):
        """Real RAW+JPG pair from fixtures → move and verify both go."""
        if not set_e_path.exists():
            pytest.skip(f"Test fixtures not present at {set_e_path}")

        # Find a RAW file with a matching JPG companion
        raw_files = list(set_e_path.glob("*.CR2")) + list(set_e_path.glob("*.CR3"))
        if not raw_files:
            pytest.skip("No RAW files in set_e fixture")

        raw_file = raw_files[0]
        # Look for matching JPG
        jpg_companion = None
        for suffix in ['.jpg', '.JPG']:
            candidate = set_e_path / (raw_file.stem + suffix)
            if candidate.exists():
                jpg_companion = candidate
                break

        if jpg_companion is None:
            pytest.skip(f"No JPG companion for {raw_file.name}")

        # Copy to temp dir (don't modify fixtures)
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        shutil.copy2(raw_file, workdir / raw_file.name)
        shutil.copy2(jpg_companion, workdir / jpg_companion.name)

        result = api.move_rejects_to_folder(str(workdir), [raw_file.name])
        assert result['success'] == True

        reject_dir = workdir / '_KESTREL_Rejects'
        assert (reject_dir / raw_file.name).exists()
        assert (reject_dir / jpg_companion.name).exists()
        # Originals gone
        assert not (workdir / raw_file.name).exists()
        assert not (workdir / jpg_companion.name).exists()


class TestCompanionCaseInsensitivity:
    """Companion lookup must not depend on filesystem case-folding.

    Companion extensions are normalized to lowercase by
    `_normalize_extensions`, but cameras write `IMG_2265.JPG`. The lookup used
    to probe the literal lowercase path, which Windows and macOS resolve for
    us and Linux does not — so on a case-sensitive filesystem the JPG stayed
    behind while the reject reported success with zero errors. There is no
    Linux job in CI, so nothing caught it.
    """

    @pytest.fixture
    def api(self):
        return api_bridge.Api()

    def _shoot(self, tmp_path, raw_name, companion_name):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        (workdir / raw_name).write_bytes(b"\x00" * 64)
        (workdir / companion_name).write_bytes(b"\xff" * 64)
        return workdir

    @pytest.mark.parametrize("companion_name", [
        "IMG_9001.JPG",   # as the camera writes it
        "IMG_9001.jpg",   # already lowercase
        "IMG_9001.JpG",   # mixed, e.g. renamed by another tool
    ])
    def test_jpg_companion_moves_regardless_of_extension_case(
        self, api, tmp_path, companion_name
    ):
        workdir = self._shoot(tmp_path, "IMG_9001.CR2", companion_name)

        result = api.move_rejects_to_folder(str(workdir), ["IMG_9001.CR2"])
        assert result["success"] is True
        assert result["errors"] == []

        reject_dir = workdir / "_KESTREL_Rejects"
        assert (reject_dir / "IMG_9001.CR2").exists()
        # Moved under its real on-disk spelling, not a case-folded guess.
        assert (reject_dir / companion_name).exists()
        assert not (workdir / companion_name).exists()

    def test_xmp_sidecar_moves_regardless_of_extension_case(self, api, tmp_path):
        """The XMP path had the identical flaw, not just the JPEG one."""
        workdir = self._shoot(tmp_path, "IMG_9002.CR2", "IMG_9002.XMP")

        result = api.move_rejects_to_folder(str(workdir), ["IMG_9002.CR2"])
        assert result["success"] is True

        reject_dir = workdir / "_KESTREL_Rejects"
        assert (reject_dir / "IMG_9002.XMP").exists()
        assert not (workdir / "IMG_9002.XMP").exists()

    def test_undo_restores_uppercase_companion(self, api, tmp_path):
        """Restore uses the same lookup, so it must round-trip."""
        workdir = self._shoot(tmp_path, "IMG_9003.CR2", "IMG_9003.JPG")
        reject_dir = workdir / "_KESTREL_Rejects"

        api.move_rejects_to_folder(str(workdir), ["IMG_9003.CR2"])
        # Assert the midpoint explicitly. Without it this test passes even when
        # the companion was never moved: the JPG would still be sitting in
        # workdir and the post-undo assertions would hold vacuously.
        assert (reject_dir / "IMG_9003.JPG").exists()
        assert not (workdir / "IMG_9003.JPG").exists()

        result = api.undo_reject_move(str(workdir), ["IMG_9003.CR2"])
        assert result["success"] is True

        assert (workdir / "IMG_9003.CR2").exists()
        assert (workdir / "IMG_9003.JPG").exists()
        assert not (reject_dir / "IMG_9003.JPG").exists()

    def test_batch_lists_each_directory_once(self, api, tmp_path, monkeypatch):
        """The batch shares one directory listing across all rejected files.

        Guards the O(files x extensions x dirsize) regression: without the
        shared index this listed the folder 6x per file.
        """
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        names = []
        for i in range(10):
            raw = f"IMG_8{i:03d}.CR2"
            (workdir / raw).write_bytes(b"\x00" * 16)
            (workdir / f"IMG_8{i:03d}.JPG").write_bytes(b"\xff" * 16)
            names.append(raw)

        real_listdir = os.listdir
        calls = []

        def counting_listdir(path):
            calls.append(str(path))
            return real_listdir(path)

        monkeypatch.setattr(api_bridge.os, "listdir", counting_listdir)
        result = api.move_rejects_to_folder(str(workdir), names)
        assert result["success"] is True

        listings_of_workdir = [c for c in calls if os.path.realpath(c) == os.path.realpath(str(workdir))]
        assert len(listings_of_workdir) == 1, (
            f"expected 1 listing of the shoot folder, got {len(listings_of_workdir)}"
        )
        reject_dir = workdir / "_KESTREL_Rejects"
        for i in range(10):
            assert (reject_dir / f"IMG_8{i:03d}.JPG").exists()


class TestMainFilenameCaseInsensitivity:
    """Main-file reject/undo must use on-disk spelling, not a case-sensitive join.

    Companion lookup already goes through ``_build_dir_index``. The RAW/JPEG
    itself was joined as ``os.path.join(root, requested_name)``, so
    ``img.cr3`` missed ``IMG.CR3`` on Linux and could rewrite casing on
    macOS/Windows.
    """

    @pytest.fixture
    def api(self):
        return api_bridge.Api()

    def test_resolve_dir_filename_returns_on_disk_spelling(self, api, tmp_path):
        (tmp_path / "IMG_0100.CR3").write_bytes(b"\x00" * 16)
        index = api._build_dir_index(str(tmp_path))
        assert api._resolve_dir_filename("img_0100.cr3", index, str(tmp_path)) == "IMG_0100.CR3"
        assert api._resolve_dir_filename("IMG_0100.CR3", index, str(tmp_path)) == "IMG_0100.CR3"
        assert api._resolve_dir_filename("missing.CR3", index, str(tmp_path)) is None

    def test_reject_moves_when_requested_case_differs(self, api, tmp_path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        (workdir / "IMG_0101.CR3").write_bytes(b"\x00" * 64)
        (workdir / "IMG_0101.JPG").write_bytes(b"\xff" * 64)

        result = api.move_rejects_to_folder(str(workdir), ["img_0101.cr3"])
        assert result["success"] is True
        assert result["errors"] == []

        reject_dir = workdir / "_KESTREL_Rejects"
        assert (reject_dir / "IMG_0101.CR3").exists()
        assert (reject_dir / "IMG_0101.JPG").exists()
        assert not (workdir / "IMG_0101.CR3").exists()
        # Must not create a second, case-folded name in the reject folder.
        reject_names = [p.name for p in reject_dir.iterdir() if p.is_file()]
        assert "img_0101.cr3" not in reject_names

    def test_undo_restores_when_requested_case_differs(self, api, tmp_path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        (workdir / "IMG_0102.CR3").write_bytes(b"\x00" * 64)
        (workdir / "IMG_0102.JPG").write_bytes(b"\xff" * 64)

        api.move_rejects_to_folder(str(workdir), ["IMG_0102.CR3"])
        reject_dir = workdir / "_KESTREL_Rejects"
        assert (reject_dir / "IMG_0102.CR3").exists()

        result = api.undo_reject_move(str(workdir), ["img_0102.cr3"])
        assert result["success"] is True
        assert result["errors"] == []

        assert (workdir / "IMG_0102.CR3").exists()
        assert (workdir / "IMG_0102.JPG").exists()
        assert not (reject_dir / "IMG_0102.CR3").exists()

    def test_reject_unknown_name_still_fails(self, api, tmp_path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        (workdir / "IMG_0103.CR3").write_bytes(b"\x00" * 16)

        result = api.move_rejects_to_folder(str(workdir), ["IMG_9999.CR3"])
        assert result["success"] is True
        assert result["errors"]
        assert (workdir / "IMG_0103.CR3").exists()
        assert not (workdir / "_KESTREL_Rejects" / "IMG_0103.CR3").exists()

    def test_resolve_prefers_exact_spelling_when_both_case_variants_exist(
        self, api, tmp_path
    ):
        """A folded index last-wins; mains must still pick the requested file."""
        _require_case_sensitive_fs(tmp_path)
        (tmp_path / "IMG_X.CR3").write_bytes(b"UPPER")
        (tmp_path / "img_x.cr3").write_bytes(b"lower")
        index = api._build_dir_index(str(tmp_path))
        assert api._resolve_dir_filename("IMG_X.CR3", index, str(tmp_path)) == "IMG_X.CR3"
        assert api._resolve_dir_filename("img_x.cr3", index, str(tmp_path)) == "img_x.cr3"
        assert api._resolve_dir_filename("Img_x.CR3", index, str(tmp_path)) is None

    def test_reject_moves_only_the_requested_case_variant(self, api, tmp_path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        _require_case_sensitive_fs(workdir)
        (workdir / "IMG_X.CR3").write_bytes(b"UPPER")
        (workdir / "img_x.cr3").write_bytes(b"lower")

        result = api.move_rejects_to_folder(str(workdir), ["IMG_X.CR3"])
        assert result["success"] is True
        assert result["errors"] == []

        reject_dir = workdir / "_KESTREL_Rejects"
        assert (reject_dir / "IMG_X.CR3").read_bytes() == b"UPPER"
        assert (workdir / "img_x.cr3").read_bytes() == b"lower"
        assert not (workdir / "IMG_X.CR3").exists()
        assert not (reject_dir / "img_x.cr3").exists()

    def test_reject_refuses_ambiguous_folded_name(self, api, tmp_path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        _require_case_sensitive_fs(workdir)
        (workdir / "IMG_X.CR3").write_bytes(b"UPPER")
        (workdir / "img_x.cr3").write_bytes(b"lower")

        result = api.move_rejects_to_folder(str(workdir), ["Img_x.CR3"])
        assert result["errors"]
        assert (workdir / "IMG_X.CR3").read_bytes() == b"UPPER"
        assert (workdir / "img_x.cr3").read_bytes() == b"lower"
        reject_dir = workdir / "_KESTREL_Rejects"
        if reject_dir.exists():
            moved = [p.name for p in reject_dir.iterdir() if p.is_file()]
            assert moved == []

