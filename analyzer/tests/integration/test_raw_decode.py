"""Integration tests for RAW image decoding via image_utils.py.

Uses fixtures in set_a_fresh/ (CR3), set_b_formats/ (diverse RAW), and set_d_jpeg_only/ (JPEG).
"""

import pytest
import numpy as np
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.image_utils import read_image, read_image_for_pipeline
from kestrel_analyzer.exposure_compensation import build_metered_detection_image


pytestmark = pytest.mark.integration


class TestReadImage:
    """Tests for read_image() across RAW and JPEG formats."""

    def test_cr3_decodes_to_rgb_array(self, set_a_path):
        """CR3 from set_a → returns RGB array (H, W, 3)."""
        if not set_a_path.exists():
            pytest.skip(f"Fixture {set_a_path} not present")

        cr3_files = list(set_a_path.glob("*.CR3"))
        if not cr3_files:
            pytest.skip("No CR3 files in set_a_fresh")

        img = read_image(str(cr3_files[0]))
        assert img is not None
        assert img.ndim == 3
        assert img.shape[2] == 3  # RGB
        assert img.dtype == np.uint8

    def test_cr3_dimensions_plausible(self, set_a_path):
        """Decoded CR3 has reasonable resolution (not tiny, not absurdly large)."""
        if not set_a_path.exists():
            pytest.skip(f"Fixture {set_a_path} not present")

        cr3_files = list(set_a_path.glob("*.CR3"))
        if not cr3_files:
            pytest.skip("No CR3 files in set_a_fresh")

        img = read_image(str(cr3_files[0]))
        H, W = img.shape[:2]
        # Camera RAW images should be at least a few MP
        assert H > 1000 and W > 1000
        # And not absurdly huge
        assert H < 20000 and W < 20000

    def test_jpeg_decodes(self, set_d_path):
        """JPEG file → returns RGB array."""
        if not set_d_path.exists():
            pytest.skip(f"Fixture {set_d_path} not present")

        jpg_files = list(set_d_path.glob("*.JPG"))
        if not jpg_files:
            pytest.skip("No JPG files in set_d")

        img = read_image(str(jpg_files[0]))
        assert img is not None
        assert img.ndim == 3
        assert img.shape[2] == 3

    def test_invalid_file_returns_none(self, tmp_path):
        """Garbage file → returns None (no crash)."""
        bad_file = tmp_path / "garbage.cr3"
        bad_file.write_bytes(b"\x00" * 1000)

        result = read_image(str(bad_file))
        assert result is None

    def test_nonexistent_file_returns_none(self, tmp_path):
        """Nonexistent file → returns None."""
        result = read_image(str(tmp_path / "nonexistent.cr3"))
        assert result is None


class TestReadImageForPipeline:
    """Tests for read_image_for_pipeline() — keeps RawPy open for re-use."""

    def test_cr3_returns_none_and_raw_obj(self, set_a_path):
        """CR3 → returns (None, RawPy object)."""
        if not set_a_path.exists():
            pytest.skip(f"Fixture {set_a_path} not present")

        cr3_files = list(set_a_path.glob("*.CR3"))
        if not cr3_files:
            pytest.skip("No CR3 files in set_a_fresh")

        rgb, raw_obj = read_image_for_pipeline(str(cr3_files[0]))
        try:
            assert rgb is None  # First element is None for RAW
            assert raw_obj is not None
        finally:
            if raw_obj is not None:
                raw_obj.close()

    def test_jpeg_returns_array_and_none(self, set_d_path):
        """JPEG → returns (array, None)."""
        if not set_d_path.exists():
            pytest.skip(f"Fixture {set_d_path} not present")

        jpg_files = list(set_d_path.glob("*.JPG"))
        if not jpg_files:
            pytest.skip("No JPG files in set_d")

        rgb, raw_obj = read_image_for_pipeline(str(jpg_files[0]))
        assert rgb is not None
        assert raw_obj is None
        assert rgb.ndim == 3
        assert rgb.shape[2] == 3

    def test_invalid_file_returns_none_none(self, tmp_path):
        """Invalid RAW → (None, None)."""
        bad_file = tmp_path / "garbage.cr3"
        bad_file.write_bytes(b"\x00" * 1000)

        rgb, raw_obj = read_image_for_pipeline(str(bad_file))
        assert rgb is None
        assert raw_obj is None


class TestDiverseRAWFormats:
    """Tests across all RAW formats in set_b_formats/."""

    def test_all_formats_decode(self, set_b_paths):
        """Every fixture in set_b_formats successfully decodes."""
        if not set_b_paths:
            pytest.skip("No set_b_formats fixtures")

        for ext, path in set_b_paths.items():
            img = read_image(str(path))
            assert img is not None, f"Failed to decode {ext}: {path}"
            assert img.ndim == 3, f"{ext}: wrong shape"
            assert img.shape[2] == 3, f"{ext}: not RGB"


class TestBuildMeteredDetectionImage:
    """Tests for build_metered_detection_image() — pipeline-level RAW decode."""

    def test_cr3_returns_expected_tuple(self, set_a_path):
        """CR3 → (metered8, meter_scale, debug_dict, noauto_linear) 4-tuple."""
        if not set_a_path.exists():
            pytest.skip(f"Fixture {set_a_path} not present")

        cr3_files = list(set_a_path.glob("*.CR3"))
        if not cr3_files:
            pytest.skip("No CR3 files in set_a_fresh")

        _, raw_obj = read_image_for_pipeline(str(cr3_files[0]))
        if raw_obj is None:
            pytest.skip("Could not open RAW file")

        try:
            metered8, meter_scale, debug, noauto_linear = build_metered_detection_image(raw_obj)
            assert metered8 is not None
            assert metered8.dtype == np.uint8
            assert metered8.shape[2] == 3  # RGB
            assert isinstance(meter_scale, float)
            assert isinstance(debug, dict)
            assert noauto_linear is not None
            assert noauto_linear.dtype == np.float32
        finally:
            raw_obj.close()

    def test_meter_scale_in_valid_range(self, set_a_path):
        """meter_scale is clamped to [0.25, 8.0]."""
        if not set_a_path.exists():
            pytest.skip(f"Fixture {set_a_path} not present")

        cr3_files = list(set_a_path.glob("*.CR3"))
        if not cr3_files:
            pytest.skip("No CR3 files in set_a_fresh")

        _, raw_obj = read_image_for_pipeline(str(cr3_files[0]))
        if raw_obj is None:
            pytest.skip("Could not open RAW file")

        try:
            _, meter_scale, _, _ = build_metered_detection_image(raw_obj)
            assert 0.25 <= meter_scale <= 8.0
        finally:
            raw_obj.close()

    def test_noauto_linear_in_zero_one_range(self, set_a_path):
        """noauto_linear float32 array is in [0, 1] range."""
        if not set_a_path.exists():
            pytest.skip(f"Fixture {set_a_path} not present")

        cr3_files = list(set_a_path.glob("*.CR3"))
        if not cr3_files:
            pytest.skip("No CR3 files in set_a_fresh")

        _, raw_obj = read_image_for_pipeline(str(cr3_files[0]))
        if raw_obj is None:
            pytest.skip("Could not open RAW file")

        try:
            _, _, _, noauto_linear = build_metered_detection_image(raw_obj)
            assert noauto_linear.min() >= 0.0
            assert noauto_linear.max() <= 1.0
        finally:
            raw_obj.close()
