"""Unit tests for config.is_supported_image_file()."""

from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.config import (
    JPEG_EXTENSIONS,
    RAW_EXTENSIONS,
    is_supported_image_file,
)


pytestmark = pytest.mark.unit


class TestIsSupportedImageFile:
    """Predicate filters real images from hidden/AppleDouble companions."""

    def test_real_arw_passes(self):
        assert is_supported_image_file("IMG_0141.ARW", RAW_EXTENSIONS)

    def test_real_arw_lowercase_passes(self):
        assert is_supported_image_file("img_0141.arw", RAW_EXTENSIONS)

    def test_real_jpeg_passes(self):
        assert is_supported_image_file("DSC_0001.JPG", JPEG_EXTENSIONS)

    def test_apple_double_arw_filtered(self):
        """The actual crash trigger — Sony A1 trace was full of these."""
        assert not is_supported_image_file("._IMG_0141.ARW", RAW_EXTENSIONS)

    def test_apple_double_with_long_name(self):
        name = (
            "._2026.05.16_Vorerst_Letzte_Bilder_der_Alpha_1_"
            "UNKNOWN_CAMERA_GPS_0141.ARW"
        )
        assert not is_supported_image_file(name, RAW_EXTENSIONS)

    def test_hidden_file_filtered(self):
        assert not is_supported_image_file(".hidden.NEF", RAW_EXTENSIONS)

    def test_ds_store_filtered(self):
        """macOS .DS_Store has no matching extension anyway, but the
        leading-dot guard also catches it."""
        assert not is_supported_image_file(".DS_Store", RAW_EXTENSIONS)
        assert not is_supported_image_file(".DS_Store", JPEG_EXTENSIONS)

    def test_unknown_extension_filtered(self):
        assert not is_supported_image_file("notes.txt", RAW_EXTENSIONS)
        assert not is_supported_image_file("video.mp4", JPEG_EXTENSIONS)

    def test_no_extension_filtered(self):
        assert not is_supported_image_file("LICENSE", RAW_EXTENSIONS)

    def test_empty_name_filtered(self):
        assert not is_supported_image_file("", RAW_EXTENSIONS)

    def test_combined_extension_set_works(self):
        all_exts = set(RAW_EXTENSIONS) | set(JPEG_EXTENSIONS)
        assert is_supported_image_file("IMG.CR3", all_exts)
        assert is_supported_image_file("IMG.JPG", all_exts)
        assert not is_supported_image_file("._IMG.CR3", all_exts)
        assert not is_supported_image_file("._IMG.JPG", all_exts)

    def test_filename_starting_with_underscore_passes(self):
        """Real Sony filenames like '_DSC7969.ARW' (leading underscore,
        not dot) should still pass."""
        assert is_supported_image_file("_DSC7969.ARW", RAW_EXTENSIONS)

    def test_filename_with_dots_in_basename_passes(self):
        """Filenames with extra dots like 'IMG.2026.05.16.ARW' should pass
        as long as the leading character isn't a dot."""
        assert is_supported_image_file("IMG.2026.05.16.ARW", RAW_EXTENSIONS)
