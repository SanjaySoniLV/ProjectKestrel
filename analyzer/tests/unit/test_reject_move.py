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
    try:
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
    finally:
        upper.unlink(missing_ok=True)
        lower.unlink(missing_ok=True)


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
        assert result['all_moved'] is True
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
        assert result['moved_requested'] == ['IMG_001.CR3']
        assert result['skipped_requested'] == []

    def test_invalid_path_returns_error(self, api):
        """Invalid root path → error response."""
        result = api.move_rejects_to_folder('/nonexistent/path/that/does/not/exist', ['IMG_001.CR3'])
        assert result['success'] == False
        assert 'error' in result

    def test_traversal_filename_rejected(self, api, workdir_with_files):
        """Filename with traversal → rejected, not moved."""
        result = api.move_rejects_to_folder(str(workdir_with_files), ['../../../etc/passwd'])
        assert result['success'] is False
        assert result['all_moved'] is False
        assert result.get('errors')
        assert (workdir_with_files / 'IMG_001.CR3').exists()

    def test_empty_filename_list_no_op(self, api, workdir_with_files):
        """Empty filename list → no-op, no errors."""
        result = api.move_rejects_to_folder(str(workdir_with_files), [])
        # Should succeed but move nothing
        assert result['success'] == True
        assert result['all_moved'] is True
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


class TestCompanionLastWinsAndExistsProbe:
    """Companion lookup must not last-wins-collapse ``.xmp`` / ``.XMP``.

    ``_DirIndex`` still maps a folded name to whichever listing came last, and
    ``_find_sidecar_file`` used that string. Two on-disk sidecars that differ
    only by extension case then collapse to one name: reject moves the last
    listing and leaves the other beside the RAW. When ``listdir`` fails, the
    empty index skipped companions entirely; an exists-probe for ``.xmp`` and
    ``.XMP`` must still find the sidecar.
    """

    def test_find_companion_returns_both_xmp_case_variants(self, api):
        index = api._DirIndex(["IMG_17.CR3", "IMG_17.xmp", "IMG_17.XMP"])
        assert index["img_17.xmp"] == "IMG_17.XMP"
        found = api._find_companion_files("/unused", "IMG_17.CR3", dir_index=index)
        assert sorted(found) == ["IMG_17.XMP", "IMG_17.xmp"]

    def test_find_sidecar_prefers_exact_spelling_not_last_wins(self, api):
        index = api._DirIndex(["IMG_17.xmp", "IMG_17.XMP"])
        hit = api._find_sidecar_file("/unused", "IMG_17.CR3", ".xmp", dir_index=index)
        assert hit == "IMG_17.xmp"

    def test_reject_moves_both_xmp_case_variants(self, api, tmp_path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        _require_case_sensitive_fs(workdir)
        (workdir / "IMG_17.CR3").write_bytes(b"raw")
        (workdir / "IMG_17.xmp").write_text("lower-xmp", encoding="utf-8")
        (workdir / "IMG_17.XMP").write_text("UPPER-xmp", encoding="utf-8")

        result = api.move_rejects_to_folder(str(workdir), ["IMG_17.CR3"])
        assert result["success"] is True
        assert result["errors"] == []

        reject_dir = workdir / "_KESTREL_Rejects"
        assert (reject_dir / "IMG_17.CR3").exists()
        assert (reject_dir / "IMG_17.xmp").read_text(encoding="utf-8") == "lower-xmp"
        assert (reject_dir / "IMG_17.XMP").read_text(encoding="utf-8") == "UPPER-xmp"
        assert not (workdir / "IMG_17.xmp").exists()
        assert not (workdir / "IMG_17.XMP").exists()

    def test_uppercase_xmp_found_when_listdir_fails(self, api, tmp_path, monkeypatch):
        (tmp_path / "IMG_17.CR3").write_bytes(b"raw")
        (tmp_path / "IMG_17.XMP").write_text("xmp", encoding="utf-8")

        def fail_listdir(_path):
            raise OSError("listdir blocked")

        monkeypatch.setattr(api_bridge.os, "listdir", fail_listdir)
        found = api._find_companion_files(str(tmp_path), "IMG_17.CR3")
        # The exists-probe tries the lowercase companion extension first.
        # On a case-sensitive filesystem that misses, and ``.XMP`` hits.
        # Windows/macOS fold the two names onto one inode, so ``.xmp`` exists
        # and is the name we join; both spellings are valid paths to the file.
        assert len(found) == 1
        assert found[0].lower() == "img_17.xmp"
        assert (tmp_path / found[0]).exists()

    def test_lowercase_xmp_found_when_listdir_fails(self, api, tmp_path, monkeypatch):
        (tmp_path / "IMG_17.CR3").write_bytes(b"raw")
        (tmp_path / "IMG_17.xmp").write_text("xmp", encoding="utf-8")

        def fail_listdir(_path):
            raise OSError("listdir blocked")

        monkeypatch.setattr(api_bridge.os, "listdir", fail_listdir)
        found = api._find_companion_files(str(tmp_path), "IMG_17.CR3")
        assert found == ["IMG_17.xmp"]

    def test_exists_probe_returns_both_distinct_xmp_variants(
        self, api, tmp_path, monkeypatch
    ):
        _require_case_sensitive_fs(tmp_path)
        (tmp_path / "IMG_17.CR3").write_bytes(b"raw")
        (tmp_path / "IMG_17.xmp").write_text("lower-xmp", encoding="utf-8")
        (tmp_path / "IMG_17.XMP").write_text("UPPER-xmp", encoding="utf-8")

        def fail_listdir(_path):
            raise OSError("listdir blocked")

        monkeypatch.setattr(api_bridge.os, "listdir", fail_listdir)
        found = api._find_companion_files(str(tmp_path), "IMG_17.CR3")
        assert sorted(found) == ["IMG_17.XMP", "IMG_17.xmp"]

    def test_listing_does_not_steal_stem_case_sibling_sidecar(self, api):
        index = api._DirIndex([
            "IMG_X.CR3", "img_x.cr3",
            "IMG_X.xmp", "IMG_X.XMP", "img_x.xmp",
        ])
        assert sorted(api._find_companion_files(
            "/unused", "IMG_X.CR3", dir_index=index
        )) == ["IMG_X.XMP", "IMG_X.xmp"]
        assert api._find_companion_files(
            "/unused", "img_x.cr3", dir_index=index
        ) == ["img_x.xmp"]

    def test_reject_leaves_stem_case_sibling_sidecar(self, api, tmp_path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        _require_case_sensitive_fs(workdir)
        (workdir / "IMG_X.CR3").write_bytes(b"UPPER")
        (workdir / "img_x.cr3").write_bytes(b"lower")
        (workdir / "IMG_X.xmp").write_text("upper-xmp", encoding="utf-8")
        (workdir / "img_x.xmp").write_text("lower-xmp", encoding="utf-8")

        result = api.move_rejects_to_folder(str(workdir), ["IMG_X.CR3"])
        assert result["success"] is True
        assert result["errors"] == []

        reject_dir = workdir / "_KESTREL_Rejects"
        assert (reject_dir / "IMG_X.CR3").exists()
        assert (reject_dir / "IMG_X.xmp").read_text(encoding="utf-8") == "upper-xmp"
        assert (workdir / "img_x.cr3").read_bytes() == b"lower"
        assert (workdir / "img_x.xmp").read_text(encoding="utf-8") == "lower-xmp"
        assert not (reject_dir / "img_x.xmp").exists()


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
        # This assertion was written against the pre-#120 contract, where a
        # batch that moved nothing still reported success. #120 deliberately
        # changed that: success is False when the caller asked for at least one
        # main file and none of them completed, so a UI gating on success alone
        # cannot drop state for files that never left disk. One requested main,
        # zero moved -> False.
        assert result["success"] is False
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


class TestRejectNoOverwrite:
    """A reject must never overwrite a file already in the reject folder.

    Camera filename counters recur (e.g. after an SD-card reformat), so a stale
    IMG_0001.CR3 from an earlier session can already sit in _KESTREL_Rejects.
    shutil.move() falls through to os.rename(), which on POSIX silently replaces
    the destination -- permanently destroying the older RAW while the API
    reported success. The move must refuse and surface an error instead.
    """

    def test_existing_reject_is_not_overwritten(self, api, tmp_path):
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        reject_dir = workdir / "_KESTREL_Rejects"
        reject_dir.mkdir()
        # An older, irreplaceable RAW already rejected in a previous session.
        (reject_dir / "IMG_0001.CR3").write_bytes(b"OLD-IRREPLACEABLE-RAW")
        # Today's brand-new photo happens to reuse the same camera filename.
        (workdir / "IMG_0001.CR3").write_bytes(b"NEW-PHOTO-TODAY")

        result = api.move_rejects_to_folder(str(workdir), ["IMG_0001.CR3"])

        # The old reject content must survive untouched.
        assert (reject_dir / "IMG_0001.CR3").read_bytes() == b"OLD-IRREPLACEABLE-RAW"
        # The new file must NOT have been silently destroyed; it stays put.
        assert (workdir / "IMG_0001.CR3").read_bytes() == b"NEW-PHOTO-TODAY"
        # And the conflict must be reported, not swallowed as success.
        assert result["moved"] == 0
        assert result["success"] is False
        assert result["all_moved"] is False
        assert result["errors"], "expected a conflict error, got none"
        # Structured result lets the UI reconcile which files were skipped.
        assert result["moved_filenames"] == []
        assert result["moved_requested"] == []
        assert any(s["filename"] == "IMG_0001.CR3" for s in result["skipped"]), result["skipped"]
        assert any(s["filename"] == "IMG_0001.CR3" for s in result["skipped_requested"]), result["skipped_requested"]

    def test_existing_companion_reject_is_not_overwritten(self, api, tmp_path):
        """The same no-overwrite rule applies to companion files (e.g. the JPG)."""
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        reject_dir = workdir / "_KESTREL_Rejects"
        reject_dir.mkdir()
        # Stale companion JPG already in the reject folder; no stale RAW.
        (reject_dir / "IMG_0002.JPG").write_bytes(b"OLD-JPG")
        (workdir / "IMG_0002.CR3").write_bytes(b"NEW-RAW")
        (workdir / "IMG_0002.JPG").write_bytes(b"NEW-JPG")

        result = api.move_rejects_to_folder(str(workdir), ["IMG_0002.CR3"])

        # The RAW moves (its destination was free)...
        assert (reject_dir / "IMG_0002.CR3").exists()
        assert result["success"] is True
        assert result["all_moved"] is True
        # ...but the pre-existing companion JPG is preserved, not clobbered.
        assert (reject_dir / "IMG_0002.JPG").read_bytes() == b"OLD-JPG"
        assert (workdir / "IMG_0002.JPG").read_bytes() == b"NEW-JPG"
        # ...and the companion conflict is surfaced in the API result, not just
        # logged, so the UI can tell the user the JPG stayed behind.
        assert any("IMG_0002.JPG" in e for e in result["errors"]), result["errors"]
        # Structured result: RAW is in moved_filenames, JPG is in skipped.
        assert "IMG_0002.CR3" in result["moved_filenames"]
        assert result["moved_requested"] == ["IMG_0002.CR3"]
        assert any(s["filename"] == "IMG_0002.JPG" for s in result["skipped"]), result["skipped"]
        assert result["skipped_requested"] == []

    def test_undo_does_not_overwrite_existing_file(self, api, tmp_path):
        """Undo/restore must not clobber a file the user re-added to the shoot."""
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        reject_dir = workdir / "_KESTREL_Rejects"
        reject_dir.mkdir()
        # A rejected copy sits in the reject folder...
        (reject_dir / "IMG_0003.CR3").write_bytes(b"REJECTED-COPY")
        # ...but the user re-added a different file with the same name.
        (workdir / "IMG_0003.CR3").write_bytes(b"CURRENT-IN-FOLDER")

        result = api.undo_reject_move(str(workdir), ["IMG_0003.CR3"])

        # The current file is preserved; the rejected copy stays put.
        assert (workdir / "IMG_0003.CR3").read_bytes() == b"CURRENT-IN-FOLDER"
        assert (reject_dir / "IMG_0003.CR3").read_bytes() == b"REJECTED-COPY"
        assert result["restored"] == 0
        assert result["success"] is False
        assert result["all_restored"] is False
        assert any("IMG_0003.CR3" in e for e in result["errors"]), result["errors"]
        assert result["restored_filenames"] == []
        assert result["restored_requested"] == []
        assert any(s["filename"] == "IMG_0003.CR3" for s in result["skipped"]), result["skipped"]
        assert any(
            s["filename"] == "IMG_0003.CR3" and "shoot folder" in s["reason"]
            for s in result["skipped_requested"]
        ), result["skipped_requested"]

    def test_undo_companion_conflict_is_surfaced(self, api, tmp_path):
        """A companion conflict on undo is surfaced, not silently skipped."""
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        reject_dir = workdir / "_KESTREL_Rejects"
        reject_dir.mkdir()
        # RAW + JPG both previously rejected.
        (reject_dir / "IMG_0004.CR3").write_bytes(b"REJECTED-RAW")
        (reject_dir / "IMG_0004.JPG").write_bytes(b"REJECTED-JPG")
        # The JPG already exists back in the shoot folder; the RAW does not.
        (workdir / "IMG_0004.JPG").write_bytes(b"CURRENT-JPG")

        result = api.undo_reject_move(str(workdir), ["IMG_0004.CR3"])

        # The RAW restores (its destination was free)...
        assert (workdir / "IMG_0004.CR3").exists()
        # ...but the existing JPG is preserved and the conflict surfaced.
        assert (workdir / "IMG_0004.JPG").read_bytes() == b"CURRENT-JPG"
        assert (reject_dir / "IMG_0004.JPG").read_bytes() == b"REJECTED-JPG"
        assert any("IMG_0004.JPG" in e for e in result["errors"]), result["errors"]
        assert "IMG_0004.CR3" in result["restored_filenames"]
        assert result["restored_requested"] == ["IMG_0004.CR3"]
        assert result["success"] is True
        assert result["all_restored"] is True
        assert any(s["filename"] == "IMG_0004.JPG" for s in result["skipped"]), result["skipped"]
        assert result["skipped_requested"] == []
        assert any("shoot folder" in (s.get("reason") or "") for s in result["skipped"])

    def test_partial_batch_keeps_success_but_clears_all_moved(self, api, tmp_path):
        """A mixed batch: one main moves, one conflicts.

        Callers that gate only on success must still look at all_moved /
        moved_filenames so they do not drop the conflicted file from UI state.
        """
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        reject_dir = workdir / "_KESTREL_Rejects"
        reject_dir.mkdir()
        (reject_dir / "IMG_0005.CR3").write_bytes(b"OLD-RAW")
        (workdir / "IMG_0005.CR3").write_bytes(b"NEW-CONFLICT")
        (workdir / "IMG_0006.CR3").write_bytes(b"NEW-FREE")

        result = api.move_rejects_to_folder(
            str(workdir), ["IMG_0005.CR3", "IMG_0006.CR3"]
        )

        assert result["success"] is True
        assert result["all_moved"] is False
        assert "IMG_0006.CR3" in result["moved_filenames"]
        assert "IMG_0005.CR3" not in result["moved_filenames"]
        assert result["moved_requested"] == ["IMG_0006.CR3"]
        assert any(s["filename"] == "IMG_0005.CR3" for s in result["skipped"])
        assert [s["filename"] for s in result["skipped_requested"]] == ["IMG_0005.CR3"]
        assert (workdir / "IMG_0005.CR3").read_bytes() == b"NEW-CONFLICT"
        assert (reject_dir / "IMG_0006.CR3").exists()
        assert not (workdir / "IMG_0006.CR3").exists()

    def test_all_invalid_filenames_are_not_success(self, api, workdir_with_files):
        """A non-empty request of only unsanitary names is not a successful no-op."""
        result = api.move_rejects_to_folder(
            str(workdir_with_files), ["../escape.CR3", ""]
        )
        assert result["success"] is False
        assert result["all_moved"] is False
        assert result["moved_filenames"] == []
        assert result["moved_requested"] == []
        assert any("invalid filename" in (s.get("reason") or "") for s in result["skipped"])
        assert len(result["skipped_requested"]) == 2
        assert (workdir_with_files / "IMG_001.CR3").exists()

        undo = api.undo_reject_move(str(workdir_with_files), ["../escape.CR3"])
        assert undo["success"] is False
        assert undo["all_restored"] is False
        assert undo["restored_requested"] == []
        assert len(undo["skipped_requested"]) == 1

    def test_missing_companion_is_in_skipped(self, api, tmp_path, monkeypatch):
        """A companion the index named but that is gone from disk is skipped."""
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        (workdir / "IMG_0007.CR3").write_bytes(b"RAW")

        monkeypatch.setattr(
            api, "_find_companion_files", lambda *_a, **_k: ["IMG_0007.JPG"]
        )
        result = api.move_rejects_to_folder(str(workdir), ["IMG_0007.CR3"])
        assert result["success"] is True
        assert "IMG_0007.CR3" in result["moved_filenames"]
        assert any(
            s["filename"] == "IMG_0007.JPG" and "not found" in s["reason"]
            for s in result["skipped"]
        ), result["skipped"]
        assert result["moved_requested"] == ["IMG_0007.CR3"]
        assert result["skipped_requested"] == []

    def test_move_failure_reason_is_exception_type(self, api, tmp_path, monkeypatch):
        """Generic move errors return the exception type, not str(e) paths."""
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        (workdir / "IMG_0008.CR3").write_bytes(b"RAW")

        def boom(_src, _dst):
            raise PermissionError("/secret/path leaked")

        monkeypatch.setattr(api_bridge, "_move_no_overwrite", boom)
        result = api.move_rejects_to_folder(str(workdir), ["IMG_0008.CR3"])
        assert any(
            s["filename"] == "IMG_0008.CR3" and s["reason"] == "PermissionError"
            for s in result["skipped"]
        ), result["skipped"]
        assert not any("/secret/path" in (s.get("reason") or "") for s in result["skipped"])
        assert not any("/secret/path" in e for e in result["errors"])


class TestMoveNoOverwrite:
    """Direct tests for the exclusive dest-create move helper."""

    def test_refuses_existing_dest(self, tmp_path):
        src = tmp_path / "src.bin"
        dst = tmp_path / "dst.bin"
        src.write_bytes(b"NEW")
        dst.write_bytes(b"OLD")
        with pytest.raises(FileExistsError):
            api_bridge._move_no_overwrite(str(src), str(dst))
        assert dst.read_bytes() == b"OLD"
        assert src.read_bytes() == b"NEW"

    def test_moves_when_dest_is_free(self, tmp_path):
        src = tmp_path / "src.bin"
        dst = tmp_path / "dst.bin"
        src.write_bytes(b"PAYLOAD")
        api_bridge._move_no_overwrite(str(src), str(dst))
        assert not src.exists()
        assert dst.read_bytes() == b"PAYLOAD"

    def test_copy_fallback_refuses_existing_dest(self, tmp_path, monkeypatch):
        src = tmp_path / "src.bin"
        dst = tmp_path / "dst.bin"
        src.write_bytes(b"NEW")
        dst.write_bytes(b"OLD")

        def boom(_src, _dst):
            raise OSError("link unsupported")

        monkeypatch.setattr(api_bridge.os, "link", boom)
        with pytest.raises(FileExistsError):
            api_bridge._move_no_overwrite(str(src), str(dst))
        assert dst.read_bytes() == b"OLD"
        assert src.read_bytes() == b"NEW"

    def test_copy_fallback_moves_when_dest_is_free(self, tmp_path, monkeypatch):
        src = tmp_path / "src.bin"
        dst = tmp_path / "dst.bin"
        src.write_bytes(b"PAYLOAD")

        def boom(_src, _dst):
            raise OSError("link unsupported")

        monkeypatch.setattr(api_bridge.os, "link", boom)
        api_bridge._move_no_overwrite(str(src), str(dst))
        assert not src.exists()
        assert dst.read_bytes() == b"PAYLOAD"

    def test_unlink_src_failure_rolls_back_dest(self, tmp_path, monkeypatch):
        """If unlink(src) fails after dest is created, dest must not remain.

        Otherwise the no-overwrite guard treats dest as a permanent conflict
        and a retry of the same name can never succeed.
        """
        src = tmp_path / "src.bin"
        dst = tmp_path / "dst.bin"
        src.write_bytes(b"PAYLOAD")
        real_unlink = api_bridge.os.unlink
        src_path = os.fspath(src)

        def boom_src_once(path):
            if os.fspath(path) == src_path:
                raise OSError("unlink busy")
            return real_unlink(path)

        monkeypatch.setattr(api_bridge.os, "unlink", boom_src_once)
        with pytest.raises(OSError, match="unlink busy"):
            api_bridge._move_no_overwrite(str(src), str(dst))
        monkeypatch.undo()
        assert src.read_bytes() == b"PAYLOAD"
        assert not dst.exists()
        api_bridge._move_no_overwrite(str(src), str(dst))
        assert not src.exists()
        assert dst.read_bytes() == b"PAYLOAD"

    def test_copy_fallback_unlink_src_failure_rolls_back_dest(self, tmp_path, monkeypatch):
        src = tmp_path / "src.bin"
        dst = tmp_path / "dst.bin"
        src.write_bytes(b"PAYLOAD")
        real_unlink = api_bridge.os.unlink
        src_path = os.fspath(src)

        def boom_link(_src, _dst):
            raise OSError("link unsupported")

        def boom_src_once(path):
            if os.fspath(path) == src_path:
                raise OSError("unlink busy")
            return real_unlink(path)

        monkeypatch.setattr(api_bridge.os, "link", boom_link)
        monkeypatch.setattr(api_bridge.os, "unlink", boom_src_once)
        with pytest.raises(OSError, match="unlink busy"):
            api_bridge._move_no_overwrite(str(src), str(dst))
        monkeypatch.undo()
        assert src.read_bytes() == b"PAYLOAD"
        assert not dst.exists()
        api_bridge._move_no_overwrite(str(src), str(dst))
        assert not src.exists()
        assert dst.read_bytes() == b"PAYLOAD"

    # --- metadata preservation on the copy fallback -------------------------
    #
    # The hard-link path keeps mtime and mode for free: dst is the same inode as
    # src. The copy fallback creates a genuinely new file, so without an explicit
    # copystat the reject lands stamped with the time of the cull. That fallback
    # runs whenever os.link fails -- cross-device, and on exFAT/FAT32, i.e. camera
    # cards and portable drives -- so it is the common path for a reject folder on
    # a second volume, not a rare one.

    def test_copy_fallback_preserves_mtime(self, tmp_path, monkeypatch):
        src = tmp_path / "src.bin"
        dst = tmp_path / "dst.bin"
        src.write_bytes(b"PAYLOAD")
        # A capture time well in the past, so "kept" vs "stamped now" cannot be
        # confused by clock granularity.
        old = 1_000_000_000.0  # 2001-09-09 UTC
        os.utime(src, (old, old))
        expected = src.stat().st_mtime

        def boom(_src, _dst):
            raise OSError("link unsupported")

        monkeypatch.setattr(api_bridge.os, "link", boom)
        api_bridge._move_no_overwrite(str(src), str(dst))

        assert not src.exists()
        assert dst.read_bytes() == b"PAYLOAD"
        assert dst.stat().st_mtime == pytest.approx(expected, abs=2), (
            "the copy fallback stamped the reject with the time of the move; "
            "sorting a reject folder by date would be meaningless"
        )

    def test_both_paths_agree_on_mtime(self, tmp_path, monkeypatch):
        """The same cull must not depend on which drive the reject folder is on."""
        old = 1_000_000_000.0

        linked_src = tmp_path / "linked_src.bin"
        linked_dst = tmp_path / "linked_dst.bin"
        linked_src.write_bytes(b"PAYLOAD")
        os.utime(linked_src, (old, old))
        api_bridge._move_no_overwrite(str(linked_src), str(linked_dst))

        copied_src = tmp_path / "copied_src.bin"
        copied_dst = tmp_path / "copied_dst.bin"
        copied_src.write_bytes(b"PAYLOAD")
        os.utime(copied_src, (old, old))

        def boom(_src, _dst):
            raise OSError("link unsupported")

        monkeypatch.setattr(api_bridge.os, "link", boom)
        api_bridge._move_no_overwrite(str(copied_src), str(copied_dst))

        assert linked_dst.stat().st_mtime == pytest.approx(
            copied_dst.stat().st_mtime, abs=2
        ), "hard-link and copy fallback disagree on the destination's mtime"

    def test_copy_fallback_survives_copystat_failure(self, tmp_path, monkeypatch):
        """copystat is best-effort: it must never fail an otherwise good move.

        It can legitimately raise on SMB/NFS shares and on some exFAT targets,
        which are exactly the volumes that force the fallback in the first place.
        Losing a timestamp is acceptable; losing the cull is not.
        """
        src = tmp_path / "src.bin"
        dst = tmp_path / "dst.bin"
        src.write_bytes(b"PAYLOAD")

        def boom_link(_src, _dst):
            raise OSError("link unsupported")

        def boom_copystat(_src, _dst, **_kw):
            raise OSError("copystat unsupported on this filesystem")

        monkeypatch.setattr(api_bridge.os, "link", boom_link)
        monkeypatch.setattr(api_bridge.shutil, "copystat", boom_copystat)

        api_bridge._move_no_overwrite(str(src), str(dst))

        assert not src.exists()
        assert dst.read_bytes() == b"PAYLOAD"
