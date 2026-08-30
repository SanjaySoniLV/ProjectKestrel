"""Unit tests for folder_inspector.py - folder scanning without ML."""

import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from folder_inspector import inspect_folder, inspect_folders


pytestmark = pytest.mark.unit


class TestInspectFolder:
    """Tests for inspect_folder() function."""

    def test_empty_folder(self, tmp_path):
        """Inspect a folder with no images - returns 0 total."""
        result = inspect_folder(str(tmp_path))
        assert result['total'] == 0
        assert result['has_kestrel'] == False

    def test_raw_files_detected(self, tmp_path):
        """Folder with CR3 files - detects them."""
        # Create some fake CR3 file entries
        (tmp_path / "IMG_001.CR3").touch()
        (tmp_path / "IMG_002.CR3").touch()

        result = inspect_folder(str(tmp_path))
        assert result['total'] == 2
        assert result['has_kestrel'] == False
        assert 'root' in result
        assert result['root'] == str(tmp_path)

    def test_jpeg_fallback_when_no_raw(self, tmp_path):
        """Folder with only JPEGs - detects them as fallback."""
        (tmp_path / "IMG_001.JPG").touch()
        (tmp_path / "IMG_002.JPG").touch()

        result = inspect_folder(str(tmp_path))
        assert result['total'] == 2

    def test_raw_preferred_over_paired_jpeg(self, tmp_path):
        """Folder with paired RAW+JPEG - RAW kept, paired JPEG dropped."""
        (tmp_path / "IMG_001.CR3").touch()
        (tmp_path / "IMG_002.CR3").touch()
        (tmp_path / "IMG_001.JPG").touch()
        (tmp_path / "IMG_002.JPG").touch()

        result = inspect_folder(str(tmp_path))
        assert result['total'] == 2

    def test_orphan_jpeg_kept_alongside_raw(self, tmp_path):
        """Folder with RAW+JPEG pairs AND a JPEG-only photo - orphan kept.

        Repros the RAW+JPG → JPG-only mid-shoot scenario from feedback #4:
        photos with paired RAW+JPG resolve to the RAW; photos shot in
        JPG-only mode are kept as standalone files instead of being
        silently dropped.
        """
        (tmp_path / "IMG_001.CR3").touch()
        (tmp_path / "IMG_002.CR3").touch()
        (tmp_path / "IMG_001.JPG").touch()
        (tmp_path / "IMG_002.JPG").touch()
        (tmp_path / "IMG_003.JPG").touch()  # orphan — no matching RAW

        result = inspect_folder(str(tmp_path))
        assert result['total'] == 3

    def test_case_insensitive_stem_match(self, tmp_path):
        """Mixed-case stems on case-folding filesystems still pair up."""
        (tmp_path / "IMG_001.CR3").touch()
        (tmp_path / "img_001.jpg").touch()  # same capture, different case

        result = inspect_folder(str(tmp_path))
        assert result['total'] == 1

    def test_kestrel_dir_normalization(self, tmp_path):
        """Passing .kestrel/ dir as input - normalized to parent."""
        kestrel_dir = tmp_path / ".kestrel"
        kestrel_dir.mkdir()
        (tmp_path / "IMG_001.CR3").touch()

        # Inspect the .kestrel/ directory itself
        result = inspect_folder(str(kestrel_dir))
        # Should normalize to parent
        assert result['root'] == str(tmp_path)

    def test_with_existing_kestrel_analysis(self, tmp_path):
        """Folder that has been analyzed - has_kestrel=True and processed count."""
        kestrel_dir = tmp_path / ".kestrel"
        kestrel_dir.mkdir()

        # Create minimal CSV
        csv_path = kestrel_dir / "kestrel_database.csv"
        csv_path.write_text("filename\nIMG_001.CR3\nIMG_002.CR3\n")

        # Create image files
        (tmp_path / "IMG_001.CR3").touch()
        (tmp_path / "IMG_002.CR3").touch()

        result = inspect_folder(str(tmp_path))
        assert result['has_kestrel'] == True
        assert result['processed'] == 2
        assert result['total'] == 2

    def test_trailing_slash_normalized(self, tmp_path):
        """Folder path with trailing slash - handled correctly."""
        (tmp_path / "IMG_001.CR3").touch()

        path_with_slash = str(tmp_path) + "/"
        result1 = inspect_folder(path_with_slash)

        path_without_slash = str(tmp_path)
        result2 = inspect_folder(path_without_slash)

        # Both should give same root (normalized)
        assert result1['root'] == result2['root']
        assert result1['total'] == result2['total']


class TestInspectFolders:
    """Tests for inspect_folders() batch function."""

    def test_single_folder(self, tmp_path):
        """Inspect a single folder - returns dict with one entry."""
        (tmp_path / "IMG_001.CR3").touch()

        result = inspect_folders([str(tmp_path)])
        assert len(result) == 1
        assert str(tmp_path) in result
        assert result[str(tmp_path)]['total'] == 1

    def test_multiple_folders(self, tmp_path):
        """Inspect multiple folders - returns all results."""
        folder_a = tmp_path / "folder_a"
        folder_b = tmp_path / "folder_b"
        folder_a.mkdir()
        folder_b.mkdir()

        (folder_a / "IMG_001.CR3").touch()
        (folder_b / "IMG_002.CR3").touch()
        (folder_b / "IMG_003.CR3").touch()

        result = inspect_folders([str(folder_a), str(folder_b)])
        assert len(result) == 2
        assert result[str(folder_a)]['total'] == 1
        assert result[str(folder_b)]['total'] == 2

    def test_deduplicates_same_path(self, tmp_path):
        """Same path listed twice - deduplicates to one result."""
        (tmp_path / "IMG_001.CR3").touch()

        result = inspect_folders([str(tmp_path), str(tmp_path)])
        assert len(result) == 1
        assert str(tmp_path) in result

    def test_shallow_paths_first(self, tmp_path):
        """Paths sorted by depth - shallower ones first."""
        deep = tmp_path / "a" / "b" / "c"
        shallow = tmp_path / "a"
        deep.mkdir(parents=True)

        (shallow / "IMG_001.CR3").touch()
        (deep / "IMG_002.CR3").touch()

        result = inspect_folders([str(deep), str(shallow)])
        paths = list(result.keys())

        # Shallower path should come first
        assert paths.index(str(shallow)) < paths.index(str(deep))


class TestAppleDoubleFiltering:
    """Tests that macOS AppleDouble (``._``-prefixed) companion files are
    excluded from enumeration. macOS creates these automatically when
    writing to non-HFS/APFS volumes (exFAT/NTFS USB drives, network
    shares) to preserve extended attributes. They share the same
    extension as the real file (e.g. ``._IMG_0142.ARW``) so an
    extension-only filter lets them through, but they contain ~4 KB of
    metadata, not image data, and LibRaw rejects every one of them."""

    def test_apple_double_files_skipped_raw(self, tmp_path):
        """A folder with both real ARWs and AppleDouble companions counts only the real files."""
        (tmp_path / "IMG_0141.ARW").touch()
        (tmp_path / "IMG_0142.ARW").touch()
        (tmp_path / "._IMG_0141.ARW").write_bytes(b'\x00\x05\x16\x07' + b'\x00' * 100)
        (tmp_path / "._IMG_0142.ARW").write_bytes(b'\x00\x05\x16\x07' + b'\x00' * 100)

        result = inspect_folder(str(tmp_path))
        assert result['total'] == 2  # only the real ARWs

    def test_apple_double_jpegs_skipped(self, tmp_path):
        """JPEG fallback path also filters ._ companions."""
        (tmp_path / "IMG_001.JPG").touch()
        (tmp_path / "._IMG_001.JPG").write_bytes(b'\x00' * 50)
        (tmp_path / "._IMG_999.JPG").write_bytes(b'\x00' * 50)

        result = inspect_folder(str(tmp_path))
        assert result['total'] == 1

    def test_hidden_files_skipped(self, tmp_path):
        """Dot-prefixed files (`.foo.CR3`, `.DS_Store`) are skipped too."""
        (tmp_path / "IMG_001.CR3").touch()
        (tmp_path / ".hidden.CR3").touch()
        (tmp_path / ".DS_Store").write_bytes(b'\x00' * 10)

        result = inspect_folder(str(tmp_path))
        assert result['total'] == 1

    def test_folder_with_only_apple_doubles_returns_zero(self, tmp_path):
        """A folder containing nothing but ._ companions reports 0 images."""
        (tmp_path / "._IMG_0141.ARW").write_bytes(b'\x00' * 100)
        (tmp_path / "._IMG_0142.ARW").write_bytes(b'\x00' * 100)
        (tmp_path / "._IMG_0143.ARW").write_bytes(b'\x00' * 100)

        result = inspect_folder(str(tmp_path))
        assert result['total'] == 0
