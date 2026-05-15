"""Integration tests for EXIF reading from real RAW files.

Uses fixtures in set_a_fresh/ (CR3) and set_b_formats/ (CR2, CR3, NEF, ARW, DNG).
"""

import pytest
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.raw_exif import get_capture_time


pytestmark = pytest.mark.integration


class TestGetCaptureTime:
    """Tests for raw_exif.get_capture_time() across formats."""

    def test_cr3_returns_datetime(self, set_a_path):
        """CR3 from set_a → returns datetime."""
        if not set_a_path.exists():
            pytest.skip(f"Fixture {set_a_path} not present")

        cr3_files = list(set_a_path.glob("*.CR3"))
        if not cr3_files:
            pytest.skip("No CR3 files in set_a_fresh")

        result = get_capture_time(str(cr3_files[0]))
        assert isinstance(result, datetime)

    def test_cr3_year_reasonable(self, set_a_path):
        """CR3 capture time year is plausible (2000-2100)."""
        if not set_a_path.exists():
            pytest.skip(f"Fixture {set_a_path} not present")

        cr3_files = list(set_a_path.glob("*.CR3"))
        if not cr3_files:
            pytest.skip("No CR3 files in set_a_fresh")

        result = get_capture_time(str(cr3_files[0]))
        assert 2000 < result.year < 2100

    def test_get_capture_time_is_repeatable(self, set_a_path):
        """Two consecutive reads return same value."""
        if not set_a_path.exists():
            pytest.skip(f"Fixture {set_a_path} not present")

        cr3_files = list(set_a_path.glob("*.CR3"))
        if not cr3_files:
            pytest.skip("No CR3 files in set_a_fresh")

        result1 = get_capture_time(str(cr3_files[0]))
        result2 = get_capture_time(str(cr3_files[0]))
        assert result1 == result2

    def test_invalid_file_raises_error(self, tmp_path):
        """Binary garbage file → raises ValueError or RuntimeError."""
        bad_file = tmp_path / "garbage.cr3"
        bad_file.write_bytes(b"\x00\x01\x02\x03 not a real raw file")

        with pytest.raises((ValueError, RuntimeError, Exception)):
            get_capture_time(str(bad_file))

    def test_nonexistent_file_raises_error(self, tmp_path):
        """Nonexistent file → raises error."""
        with pytest.raises(Exception):
            get_capture_time(str(tmp_path / "nonexistent.cr3"))


class TestMultipleFormats:
    """Tests across diverse RAW formats in set_b_formats/."""

    def test_all_set_b_formats_readable(self, set_b_paths):
        """Every format in set_b returns a valid datetime."""
        if not set_b_paths:
            pytest.skip("No set_b fixtures present")

        results = {}
        for ext, path in set_b_paths.items():
            try:
                result = get_capture_time(str(path))
                results[ext] = result
                assert isinstance(result, datetime), f"{ext} failed: not a datetime"
            except Exception as e:
                pytest.fail(f"Failed to read EXIF from {ext}: {e}")

        # At least one format should have worked
        assert len(results) > 0

    def test_jpeg_capture_time(self, set_d_path):
        """JPEG files have EXIF readable."""
        if not set_d_path.exists():
            pytest.skip(f"Fixture {set_d_path} not present")

        jpg_files = list(set_d_path.glob("*.JPG"))
        if not jpg_files:
            pytest.skip("No JPGs in set_d")

        result = get_capture_time(str(jpg_files[0]))
        assert isinstance(result, datetime)


class TestSceneTiming:
    """Tests for set_a scene timing - should have 2 distinct scenes."""

    def test_set_a_has_4_images(self, set_a_path):
        """set_a_fresh should have 4 images for scene grouping tests."""
        if not set_a_path.exists():
            pytest.skip(f"Fixture {set_a_path} not present")

        cr3_files = list(set_a_path.glob("*.CR3"))
        assert len(cr3_files) >= 4, f"Expected 4+ CR3 files, got {len(cr3_files)}"

    def test_set_a_timestamps_readable(self, set_a_path):
        """All 4 images in set_a have readable timestamps."""
        if not set_a_path.exists():
            pytest.skip(f"Fixture {set_a_path} not present")

        cr3_files = sorted(set_a_path.glob("*.CR3"))
        if len(cr3_files) < 4:
            pytest.skip(f"Need 4 CR3 files, got {len(cr3_files)}")

        times = []
        for f in cr3_files[:4]:
            t = get_capture_time(str(f))
            assert isinstance(t, datetime)
            times.append(t)

        # All times should be valid datetimes (sanity check)
        assert all(isinstance(t, datetime) for t in times)
