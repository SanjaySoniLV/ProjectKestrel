"""Unit tests for Api.read_raw_full()'s embedded-JPEG fallback for RAW
files whose sensor data can't be decoded by LibRaw (Nikon Z8/Z9 HE / HE*
NEFs are the canonical trigger — intoPIX TicoRAW ships in neither
LibRaw nor rawpy).

Uses mocked rawpy handles so the tests are fast and CI-friendly. A
smoke check against the real Z8 samples on raw.pixls.us was verified
manually during development.
"""

from __future__ import annotations

import io
import os
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import rawpy
import api_bridge


pytestmark = pytest.mark.unit


def _make_jpeg_bytes(size=(8256, 5504), color=(100, 140, 200)) -> bytes:
    """Return raw JPEG bytes at the requested size. Used to fake the
    full-resolution embedded preview extracted by extract_thumb()."""
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _make_rotated_jpeg_bytes(size=(200, 100)) -> bytes:
    """Return JPEG bytes carrying an EXIF Orientation=6 (rotate 90° CW).
    Used to verify the fallback applies orientation."""
    img = Image.new("RGB", size, color=(50, 200, 90))
    exif = img.getexif()
    exif[0x0112] = 6  # rotate 90° CW
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, exif=exif)
    return buf.getvalue()


def _mock_raw_thumb(*, thumb_format=None, thumb_data=None, raise_on_thumb=None):
    """Build a context-manager mock that mimics rawpy.imread() returning
    a handle whose extract_thumb() behaves as specified."""
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


class TestExtractFullResEmbeddedJpeg:
    """Direct tests of the Api._extract_full_res_embedded_jpeg helper.

    Bootstrapping a full Api() instance for a helper test is heavy, so
    each test calls the unbound method with a lightweight object as
    self — the helper doesn't touch instance state."""

    def _call(self, path):
        return api_bridge.Api._extract_full_res_embedded_jpeg(
            MagicMock(spec=object), str(path)
        )

    def test_returns_original_jpeg_when_no_orientation_needed(self, tmp_path):
        jpeg_bytes = _make_jpeg_bytes((320, 200))
        path = tmp_path / "z8_he.NEF"
        path.write_bytes(b"\x00")
        with patch.object(
            rawpy, "imread",
            return_value=_mock_raw_thumb(
                thumb_format=rawpy.ThumbFormat.JPEG,
                thumb_data=jpeg_bytes,
            ),
        ):
            data, dims = self._call(path)
        # No rotation → helper hands back the in-camera JPEG bytes byte-for-byte.
        assert data == jpeg_bytes
        assert dims == (320, 200)

    def test_applies_exif_orientation_and_reencodes(self, tmp_path):
        rotated = _make_rotated_jpeg_bytes(size=(200, 100))
        path = tmp_path / "z8_he_portrait.NEF"
        path.write_bytes(b"\x00")
        with patch.object(
            rawpy, "imread",
            return_value=_mock_raw_thumb(
                thumb_format=rawpy.ThumbFormat.JPEG,
                thumb_data=rotated,
            ),
        ):
            data, dims = self._call(path)
        # Orientation 6 = 90° CW → dims should be swapped (200x100 -> 100x200).
        assert dims == (100, 200)
        # And the returned bytes are a fresh JPEG (not the original).
        assert data != rotated
        with Image.open(io.BytesIO(data)) as reopened:
            assert reopened.format == "JPEG"
            assert (reopened.width, reopened.height) == (100, 200)

    def test_libraw_file_unsupported_from_imread_returns_none(self, tmp_path):
        """Some containers can't even be opened for thumbnail extraction —
        distinct from HE where imread + extract_thumb both work."""
        path = tmp_path / "unopenable.NEF"
        path.write_bytes(b"\x00")
        with patch.object(
            rawpy, "imread",
            side_effect=rawpy.LibRawFileUnsupportedError("nope"),
        ):
            data, dims = self._call(path)
        assert data is None
        assert dims is None

    def test_no_thumbnail_returns_none(self, tmp_path):
        path = tmp_path / "no_thumb.NEF"
        path.write_bytes(b"\x00")
        with patch.object(
            rawpy, "imread",
            return_value=_mock_raw_thumb(
                raise_on_thumb=rawpy.LibRawNoThumbnailError("no thumb"),
            ),
        ):
            data, dims = self._call(path)
        assert data is None
        assert dims is None

    def test_bitmap_thumb_returns_none(self, tmp_path):
        """Some cameras store an uncompressed bitmap thumb rather than a
        JPEG — currently unhandled; the helper should say so, not lie."""
        path = tmp_path / "bitmap_thumb.NEF"
        path.write_bytes(b"\x00")
        with patch.object(
            rawpy, "imread",
            return_value=_mock_raw_thumb(
                thumb_format=rawpy.ThumbFormat.BITMAP,
                thumb_data=b"\x00" * 4096,
            ),
        ):
            data, dims = self._call(path)
        assert data is None
        assert dims is None

    def test_corrupt_jpeg_returns_none(self, tmp_path):
        path = tmp_path / "corrupt.NEF"
        path.write_bytes(b"\x00")
        with patch.object(
            rawpy, "imread",
            return_value=_mock_raw_thumb(
                thumb_format=rawpy.ThumbFormat.JPEG,
                thumb_data=b"not a real jpeg at all",
            ),
        ):
            data, dims = self._call(path)
        assert data is None
        assert dims is None


class TestReadRawFullHeFallback:
    """Higher-level tests: read_raw_full() must route to the embedded
    JPEG when postprocess() raises LibRawFileUnsupportedError (Z8 HE)."""

    @pytest.fixture
    def he_scene(self, tmp_path):
        """Build a tmp root with a fake .NEF and a raw handle whose
        postprocess() raises LibRawFileUnsupportedError."""
        root = tmp_path / "shoot"
        root.mkdir()
        nef = root / "DSC_7191.NEF"
        nef.write_bytes(b"\x00")

        unsupported = MagicMock()
        unsupported.sizes = MagicMock(
            width=8280, height=5520, raw_width=8280, raw_height=5520,
            iwidth=8280, iheight=5520, flip=0,
        )
        unsupported.postprocess.side_effect = rawpy.LibRawFileUnsupportedError(
            b"Unsupported file format or not RAW file"
        )
        unsupported.__enter__.return_value = unsupported
        unsupported.__exit__.return_value = False

        embedded_bytes = _make_jpeg_bytes((8256, 5504))
        preview_raw = _mock_raw_thumb(
            thumb_format=rawpy.ThumbFormat.JPEG,
            thumb_data=embedded_bytes,
        )
        return {
            "root": root,
            "nef": nef,
            "unsupported_raw": unsupported,
            "preview_raw": preview_raw,
            "embedded_bytes": embedded_bytes,
        }

    def test_he_falls_back_to_embedded_jpeg(self, he_scene):
        api = api_bridge.Api()
        # First imread returns the handle whose postprocess raises;
        # second imread (inside the helper) returns the preview handle
        # from which extract_thumb succeeds.
        with patch.object(
            rawpy, "imread",
            side_effect=[he_scene["unsupported_raw"], he_scene["preview_raw"]],
        ):
            result = api.read_raw_full(he_scene["nef"].name, str(he_scene["root"]))
        assert result["success"] is True
        assert result.get("fallback") == "embedded_jpeg_preview"
        assert result.get("fallback_reason")
        assert result["mime"] == "image/jpeg"
        # Data is base64-encoded JPEG bytes. Decode and verify shape.
        import base64
        decoded = base64.b64decode(result["data"])
        assert decoded == he_scene["embedded_bytes"]
        # Debug metadata carries the fallback marker.
        assert result["debug"].get("fallback") == "embedded_jpeg_preview"
        assert result["debug"]["jpeg_dimensions"] == {"width": 8256, "height": 5504}

    def test_he_writes_cache_entry_by_default(self, he_scene):
        api = api_bridge.Api()
        with patch.object(
            rawpy, "imread",
            side_effect=[he_scene["unsupported_raw"], he_scene["preview_raw"]],
        ):
            result = api.read_raw_full(he_scene["nef"].name, str(he_scene["root"]))
        assert result["success"] is True
        # A JPEG must have landed in the culling_TMP cache alongside the
        # image. Same file → subsequent calls hit that cache instead of
        # re-running the failing rawpy.postprocess.
        cache_dir = he_scene["root"] / ".kestrel" / "culling_TMP"
        jpegs = list(cache_dir.glob("*_preview.jpg"))
        assert len(jpegs) == 1
        assert jpegs[0].read_bytes() == he_scene["embedded_bytes"]

    def test_he_with_no_embedded_preview_reports_original_error(self, he_scene):
        """If both the RAW decode AND the embedded preview extraction
        fail, the caller must see the LibRaw error, not silent success."""
        api = api_bridge.Api()
        no_thumb_raw = _mock_raw_thumb(
            raise_on_thumb=rawpy.LibRawNoThumbnailError("no thumb"),
        )
        with patch.object(
            rawpy, "imread",
            side_effect=[he_scene["unsupported_raw"], no_thumb_raw],
        ):
            result = api.read_raw_full(he_scene["nef"].name, str(he_scene["root"]))
        assert result["success"] is False
        assert "Unsupported file format" in result["error"]

    def test_supported_raw_bypasses_fallback(self, tmp_path):
        """When postprocess() returns normally the fallback branch must
        not fire — otherwise every raw would be silently downgraded."""
        import numpy as np
        root = tmp_path / "shoot"
        root.mkdir()
        nef = root / "DSC_0001.NEF"
        nef.write_bytes(b"\x00")

        rgb = np.zeros((64, 96, 3), dtype=np.uint8)
        rgb[:, :, 0] = 128
        ok_raw = MagicMock()
        ok_raw.sizes = MagicMock(
            width=96, height=64, raw_width=96, raw_height=64,
            iwidth=96, iheight=64, flip=0,
        )
        ok_raw.postprocess.return_value = rgb
        ok_raw.__enter__.return_value = ok_raw
        ok_raw.__exit__.return_value = False

        api = api_bridge.Api()
        with patch.object(rawpy, "imread", return_value=ok_raw):
            result = api.read_raw_full(nef.name, str(root))

        assert result["success"] is True
        assert "fallback" not in result
        assert result["debug"].get("fallback") is None
        assert "postprocess_rgb_shape" in result["debug"]
