"""Unit tests for image_utils.decode_embedded_preview() fallback.

Exercises the LibRaw HE-NEF fallback path with mocked rawpy objects
so the test stays small and CI-friendly. End-to-end coverage against a
real Nikon Z8 sample lives in the integration suite alongside other
RAW fixtures.
"""

from __future__ import annotations

import io
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import rawpy

from kestrel_analyzer import image_utils


pytestmark = pytest.mark.unit


def _make_jpeg_bytes(size=(64, 32), color=(127, 50, 200)) -> bytes:
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _mock_raw(*, thumb_format=None, thumb_data=None, raise_on_thumb=None):
    """Build a context-manager mock that mimics rawpy.imread()."""
    raw = MagicMock()
    if raise_on_thumb is not None:
        raw.extract_thumb.side_effect = raise_on_thumb
    else:
        thumb = MagicMock()
        thumb.format = thumb_format
        thumb.data = thumb_data
        raw.extract_thumb.return_value = thumb
    raw.__enter__.return_value = raw
    raw.__exit__.return_value = False
    return raw


class TestDecodeEmbeddedPreview:
    def test_jpeg_thumb_decodes(self, tmp_path):
        jpeg_bytes = _make_jpeg_bytes((128, 64))
        path = tmp_path / "fake.NEF"
        path.write_bytes(b"\x00")
        with patch.object(
            image_utils.rawpy, "imread",
            return_value=_mock_raw(
                thumb_format=rawpy.ThumbFormat.JPEG,
                thumb_data=jpeg_bytes,
            ),
        ):
            result = image_utils.decode_embedded_preview(str(path))
        assert result is not None
        assert result.shape == (64, 128, 3)
        assert result.dtype == np.uint8

    def test_bitmap_thumb_unsupported_returns_none(self, tmp_path):
        path = tmp_path / "fake.NEF"
        path.write_bytes(b"\x00")
        with patch.object(
            image_utils.rawpy, "imread",
            return_value=_mock_raw(
                thumb_format=rawpy.ThumbFormat.BITMAP,
                thumb_data=b"\x00" * 100,
            ),
        ):
            result = image_utils.decode_embedded_preview(str(path))
        assert result is None

    def test_no_thumbnail_returns_none(self, tmp_path):
        path = tmp_path / "fake.NEF"
        path.write_bytes(b"\x00")
        with patch.object(
            image_utils.rawpy, "imread",
            return_value=_mock_raw(
                raise_on_thumb=rawpy.LibRawNoThumbnailError("no thumb"),
            ),
        ):
            result = image_utils.decode_embedded_preview(str(path))
        assert result is None

    def test_unsupported_thumbnail_returns_none(self, tmp_path):
        path = tmp_path / "fake.NEF"
        path.write_bytes(b"\x00")
        with patch.object(
            image_utils.rawpy, "imread",
            return_value=_mock_raw(
                raise_on_thumb=rawpy.LibRawUnsupportedThumbnailError("bad fmt"),
            ),
        ):
            result = image_utils.decode_embedded_preview(str(path))
        assert result is None

    def test_unopenable_raw_returns_none(self, tmp_path):
        path = tmp_path / "fake.NEF"
        path.write_bytes(b"\x00")
        with patch.object(
            image_utils.rawpy, "imread",
            side_effect=rawpy.LibRawFileUnsupportedError("nope"),
        ):
            result = image_utils.decode_embedded_preview(str(path))
        assert result is None

    def test_corrupt_jpeg_bytes_returns_none(self, tmp_path):
        path = tmp_path / "fake.NEF"
        path.write_bytes(b"\x00")
        with patch.object(
            image_utils.rawpy, "imread",
            return_value=_mock_raw(
                thumb_format=rawpy.ThumbFormat.JPEG,
                thumb_data=b"not a real jpeg",
            ),
        ):
            result = image_utils.decode_embedded_preview(str(path))
        assert result is None


class TestReadImageFallback:
    """read_image() should fall back to the preview when postprocess can't
    decompress the sensor data (e.g. Nikon HE compression)."""

    def test_postprocess_unsupported_falls_back_to_preview(self, tmp_path):
        jpeg_bytes = _make_jpeg_bytes((96, 48))
        path = tmp_path / "fake.NEF"
        path.write_bytes(b"\x00")

        unsupported_raw = MagicMock()
        unsupported_raw.postprocess.side_effect = rawpy.LibRawFileUnsupportedError(
            "HE compression"
        )
        unsupported_raw.__enter__.return_value = unsupported_raw
        unsupported_raw.__exit__.return_value = False

        preview_raw = _mock_raw(
            thumb_format=rawpy.ThumbFormat.JPEG,
            thumb_data=jpeg_bytes,
        )

        # First call (in read_image): returns the unsupported_raw.
        # Second call (in decode_embedded_preview after fallback): returns
        # a fresh handle that can extract the thumb.
        with patch.object(
            image_utils.rawpy, "imread",
            side_effect=[unsupported_raw, preview_raw],
        ):
            result = image_utils.read_image(str(path))

        assert result is not None
        assert result.shape == (48, 96, 3)

    def test_postprocess_unsupported_with_no_preview_returns_none(self, tmp_path):
        path = tmp_path / "fake.NEF"
        path.write_bytes(b"\x00")

        unsupported_raw = MagicMock()
        unsupported_raw.postprocess.side_effect = rawpy.LibRawFileUnsupportedError(
            "HE compression"
        )
        unsupported_raw.__enter__.return_value = unsupported_raw
        unsupported_raw.__exit__.return_value = False

        no_thumb_raw = _mock_raw(
            raise_on_thumb=rawpy.LibRawNoThumbnailError("no thumb"),
        )

        with patch.object(
            image_utils.rawpy, "imread",
            side_effect=[unsupported_raw, no_thumb_raw],
        ):
            result = image_utils.read_image(str(path))

        assert result is None
