"""Unit tests for Api.move_rejects_to_folder and Api.undo_reject_move.

Uses set_e_raw_jpg_mix fixtures for RAW+JPG companion file testing.
"""

import pytest
import shutil
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge


pytestmark = pytest.mark.unit


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
