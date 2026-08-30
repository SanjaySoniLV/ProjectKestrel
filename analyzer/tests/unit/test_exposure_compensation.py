"""Unit tests for exposure_compensation.py - exposure math and sRGB conversion."""

import pytest
import numpy as np
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.exposure_compensation import (
    linear_to_srgb_u8,
    compute_global_meter_scale,
    compose_total_stops,
)


pytestmark = pytest.mark.unit


class TestLinearToSRGBConversion:
    """Tests for linear_to_srgb_u8() - the LUT-based gamma conversion."""

    def test_zero_maps_to_zero(self):
        """Linear 0.0 maps to sRGB 0."""
        result = linear_to_srgb_u8(np.array([0.0], dtype=np.float32))
        assert result[0] == 0

    def test_one_maps_to_255(self):
        """Linear 1.0 maps to sRGB 255."""
        result = linear_to_srgb_u8(np.array([1.0], dtype=np.float32))
        assert result[0] == 255

    def test_midpoint_approximately_128(self):
        """Linear ~0.214 maps to sRGB ~128 (due to gamma 2.2 approx)."""
        # The exact value depends on the sRGB transfer function
        result = linear_to_srgb_u8(np.array([0.214], dtype=np.float32))
        # Should be close to 128, but allow some tolerance for the exact LUT
        assert 120 <= result[0] <= 135

    def test_array_input(self):
        """Can handle multi-element arrays."""
        linear = np.array([0.0, 0.5, 1.0], dtype=np.float32)
        result = linear_to_srgb_u8(linear)
        assert len(result) == 3
        assert result[0] == 0
        assert result[2] == 255
        assert 0 < result[1] < 255

    def test_output_dtype_is_uint8(self):
        """Output is always uint8."""
        result = linear_to_srgb_u8(np.array([0.5], dtype=np.float32))
        assert result.dtype == np.uint8

    def test_clamps_out_of_range(self):
        """Values outside [0, 1] are clamped."""
        # Negative values
        result_neg = linear_to_srgb_u8(np.array([-0.5], dtype=np.float32))
        assert result_neg[0] == 0

        # Values > 1
        result_high = linear_to_srgb_u8(np.array([1.5], dtype=np.float32))
        assert result_high[0] == 255


class TestGlobalMeterScale:
    """Tests for compute_global_meter_scale() - brightness metering."""

    def test_all_white_image_scale_less_than_one(self):
        """All-white image (bright) should have meter_scale < 1 (needs darkening)."""
        white = np.ones((100, 100, 3), dtype=np.float32)
        scale, debug = compute_global_meter_scale(white)
        assert scale < 1.0

    def test_all_black_image_scale_greater_than_one(self):
        """All-black image (dark) should have meter_scale > 1 (needs brightening)."""
        black = np.zeros((100, 100, 3), dtype=np.float32)
        scale, debug = compute_global_meter_scale(black)
        assert scale > 1.0

    def test_neutral_gray_scale_is_computed_correctly(self):
        """Neutral gray (0.5 luminance) gets its meter scale from percentile targets."""
        gray = np.ones((100, 100, 3), dtype=np.float32) * 0.5
        scale, debug = compute_global_meter_scale(gray)
        # For 0.5, targets are 0.33/0.5=0.66, 0.72/0.5=1.44, 0.90/0.5=1.8
        # The minimum is 0.66, so scale should be 0.66
        assert 0.65 <= scale <= 0.67

    def test_scale_clamped_to_range(self):
        """Meter scale is clamped to [0.25, 8.0]."""
        # Extremely bright image (should clamp to 0.25)
        very_bright = np.ones((100, 100, 3), dtype=np.float32) * 2.0
        scale_bright, _ = compute_global_meter_scale(very_bright)
        assert scale_bright >= 0.25

        # Extremely dark image (should clamp to 8.0)
        very_dark = np.zeros((100, 100, 3), dtype=np.float32)
        very_dark[0, 0, :] = 0.01  # Just a tiny bit of light
        scale_dark, _ = compute_global_meter_scale(very_dark)
        assert scale_dark <= 8.0

    def test_returns_debug_dict(self):
        """Returns tuple (scale, debug_dict)."""
        image = np.ones((50, 50, 3), dtype=np.float32) * 0.5
        scale, debug = compute_global_meter_scale(image)
        assert isinstance(scale, (float, np.floating))
        assert isinstance(debug, dict)


class TestComposeStops:
    """Tests for compose_total_stops() - combines subject stops with meter scale."""

    def test_zero_subject_stops_uses_meter_scale(self):
        """Subject stops=0, meter_scale=2 → total ≈ log2(2) = 1 stop."""
        total_stops = compose_total_stops(0, 2.0)
        # log2(2) = 1
        assert 0.95 <= total_stops <= 1.05

    def test_positive_subject_adds_to_meter(self):
        """Positive subject stops increases total."""
        total_1 = compose_total_stops(2.0, 1.0)  # meter scale 1.0 = 0 stops
        total_2 = compose_total_stops(2.0, 2.0)  # meter scale 2.0 = 1 stop
        assert total_2 > total_1

    def test_negative_subject_decreases_total(self):
        """Negative subject stops decreases total."""
        total_1 = compose_total_stops(2.0, 2.0)
        total_2 = compose_total_stops(-2.0, 2.0)
        assert total_2 < total_1

    def test_meter_scale_one_gives_just_subject(self):
        """Meter scale=1.0 (log2=0) → total ≈ subject_stops."""
        subject = 1.5
        total = compose_total_stops(subject, 1.0)
        assert 1.45 <= total <= 1.55

    def test_returns_float(self):
        """Returns a float/numeric value."""
        result = compose_total_stops(1.0, 2.0)
        assert isinstance(result, (float, np.floating, int))
