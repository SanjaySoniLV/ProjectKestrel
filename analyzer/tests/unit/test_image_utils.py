"""Unit tests for image_utils decode fallbacks and failure reporting.

Exercises the LibRaw HE-NEF fallback path with mocked rawpy objects
so the test stays small and CI-friendly. End-to-end coverage against a
real Nikon Z8 sample lives in the integration suite alongside other
RAW fixtures.

Also covers what read_image_for_pipeline() reports when a decode fails:
the cause has to reach the caller, because the pipeline logs it verbatim
to the JSONL analysis log and the Live Analysis dialog.
"""

from __future__ import annotations

import errno
import io
import os
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


class TestDecodeFailureReporting:
    """read_image_for_pipeline() must name the real cause of a failure.

    Every case below used to arrive at the caller as the same
    ``RuntimeError("Image read returned None for both img and raw_obj")``,
    which is what made a real ``.ARW`` bug report untriageable. LibRaw itself
    cannot tell most of them apart — it reports a missing file, a permission
    denial and a read error on a failing drive all as
    ``LibRawIOError: b'Input/output error'`` — so the distinction has to come
    from re-checking the file, not from rawpy.
    """

    def test_unsupported_raw_raises_libraw_unsupported(self, tmp_path):
        path = tmp_path / "garbage.ARW"
        path.write_bytes(b"\x00" * 4096)

        with pytest.raises(rawpy.LibRawFileUnsupportedError) as excinfo:
            image_utils.read_image_for_pipeline(str(path))

        assert "unsupported" in str(excinfo.value).lower()

    def test_missing_file_raises_file_not_found(self, tmp_path):
        path = tmp_path / "absent.ARW"

        with pytest.raises(FileNotFoundError) as excinfo:
            image_utils.read_image_for_pipeline(str(path))

        assert excinfo.value.errno == errno.ENOENT

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root bypasses the permission bits this test relies on",
    )
    def test_permission_denied_raises_permission_error(self, tmp_path):
        path = tmp_path / "locked.ARW"
        path.write_bytes(b"\x00" * 4096)
        path.chmod(0o000)
        try:
            with pytest.raises(PermissionError) as excinfo:
                image_utils.read_image_for_pipeline(str(path))
        finally:
            path.chmod(0o644)

        assert excinfo.value.errno == errno.EACCES

    def test_io_error_raises_oserror_with_eio(self, tmp_path):
        """A failing drive: the open succeeds but the read raises EIO.

        ``builtins.open`` is patched rather than the module's own name
        because the probe is the only thing that opens the file through
        Python — rawpy reads it in C and is unaffected.
        """
        path = tmp_path / "flaky.ARW"
        path.write_bytes(b"\x00" * 4096)

        with patch("builtins.open", side_effect=OSError(
            errno.EIO, "Input/output error", str(path),
        )):
            with pytest.raises(OSError) as excinfo:
                image_utils.read_image_for_pipeline(str(path))

        assert excinfo.value.errno == errno.EIO

    def test_the_three_causes_are_distinguishable(self, tmp_path):
        """The headline requirement: one message per cause, not one for all."""
        unsupported = tmp_path / "garbage.ARW"
        unsupported.write_bytes(b"\x00" * 4096)
        missing = tmp_path / "absent.ARW"
        flaky = tmp_path / "flaky.ARW"
        flaky.write_bytes(b"\x00" * 4096)

        messages = set()
        for path, patcher in (
            (unsupported, None),
            (missing, None),
            (flaky, patch("builtins.open", side_effect=OSError(
                errno.EIO, "Input/output error", str(flaky),
            ))),
        ):
            try:
                if patcher is None:
                    image_utils.read_image_for_pipeline(str(path))
                else:
                    with patcher:
                        image_utils.read_image_for_pipeline(str(path))
            except Exception as exc:
                messages.add(f"{type(exc).__name__}: {exc}")

        assert len(messages) == 3, messages

    def test_message_does_not_leak_the_containing_directory(self, tmp_path):
        """The file name may appear; the folders above it may not.

        The JSON analysis log records the full path in its own field, but the
        runtime stderr log and the Live Analysis status line render only
        ``str(exc)`` and carry just the file name today.
        """
        path = tmp_path / "absent.ARW"

        with pytest.raises(FileNotFoundError) as excinfo:
            image_utils.read_image_for_pipeline(str(path))

        message = str(excinfo.value)
        assert "absent.ARW" in message
        assert str(tmp_path) not in message

    def test_corrupt_non_raw_propagates_pil_error(self, tmp_path):
        """The non-RAW branch reports its cause too, without read_image()
        losing its own ``None`` contract."""
        path = tmp_path / "broken.jpg"
        path.write_bytes(b"not a jpeg")

        with pytest.raises(Exception) as excinfo:
            image_utils.read_image_for_pipeline(str(path))
        assert not isinstance(excinfo.value, RuntimeError)

        # read_image() is unchanged: its callers still expect None.
        assert image_utils.read_image(str(path)) is None


class TestPipelineSurfacesDecodeCause:
    """The cause must survive the trip through AnalysisPipeline._decode_image().

    This is the end of the chain the bug report exposed: whatever lands on
    ``result["error"]`` is what log_exception() writes to the JSONL analysis
    log and what the Live Analysis dialog shows.
    """

    def test_decode_image_records_the_real_exception(self, tmp_path):
        from kestrel_analyzer.pipeline import AnalysisPipeline

        path = tmp_path / "absent.ARW"
        pipeline = AnalysisPipeline(use_gpu=False)

        result = pipeline._decode_image(str(path), path.name)

        error = result["error"]
        assert isinstance(error, FileNotFoundError)
        assert "Image read returned None" not in str(error)

    def test_decode_image_distinguishes_unsupported_from_missing(self, tmp_path):
        from kestrel_analyzer.pipeline import AnalysisPipeline

        garbage = tmp_path / "garbage.ARW"
        garbage.write_bytes(b"\x00" * 4096)
        missing = tmp_path / "absent.ARW"
        pipeline = AnalysisPipeline(use_gpu=False)

        unsupported_error = pipeline._decode_image(str(garbage), garbage.name)["error"]
        missing_error = pipeline._decode_image(str(missing), missing.name)["error"]

        assert type(unsupported_error) is not type(missing_error)
        assert str(unsupported_error) != str(missing_error)
