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

    def test_raw_preferred_over_jpeg(self, tmp_path):
        """Folder with both RAW and JPEG - RAW takes precedence."""
        (tmp_path / "IMG_001.CR3").touch()
        (tmp_path / "IMG_002.CR3").touch()
        (tmp_path / "IMG_001.JPG").touch()
        (tmp_path / "IMG_002.JPG").touch()
        (tmp_path / "IMG_003.JPG").touch()

        result = inspect_folder(str(tmp_path))
        # Should count CR3 files, not JPEGs
        assert result['total'] == 2

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
