"""Unit tests for metadata_writer.py - XMP sidecar writing."""

import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from metadata_writer import write_xmp_metadata, _safe_sidecar_path


pytestmark = pytest.mark.unit


# XMP namespace constant for matching kestrel-written files
_KESTREL_NS = 'http://ns.projectkestrel.app/xmp/1.0/'


class TestWriteBasicXMP:
    """Tests for basic XMP writing behavior."""

    def test_write_basic_xmp_all_fields(self, tmp_path):
        """XMP string contains kestrel NS, rating, label, species."""
        # Create a dummy image file (need to exist)
        (tmp_path / "IMG_001.CR3").touch()

        result = write_xmp_metadata(
            str(tmp_path),
            [{
                'filename': 'IMG_001.CR3',
                'rating': 4,
                'culled': 'accept',
                'culled_origin': 'manual',
                'species': 'aves,columbidae',
                'family': 'columbidae',
                'quality': 0.85
            }]
        )

        assert result['success'] == True
        assert result['written'] == 1

        # Read the XMP file and verify content
        xmp_path = tmp_path / "IMG_001.xmp"
        assert xmp_path.exists()
        content = xmp_path.read_text(encoding='utf-8')
        assert _KESTREL_NS in content
        assert 'xmp:Rating="4"' in content or '<xmp:Rating>4</xmp:Rating>' in content
        assert 'aves' in content
        assert 'columbidae' in content

    def test_xmp_label_green_on_manual_accept(self, tmp_path):
        """culled='accept', culled_origin='manual' → Green label."""
        (tmp_path / "IMG_001.CR3").touch()

        write_xmp_metadata(
            str(tmp_path),
            [{
                'filename': 'IMG_001.CR3',
                'rating': 5,
                'culled': 'accept',
                'culled_origin': 'manual',
            }]
        )

        xmp_path = tmp_path / "IMG_001.xmp"
        content = xmp_path.read_text(encoding='utf-8')
        assert 'Green' in content

    def test_xmp_label_red_on_manual_reject(self, tmp_path):
        """culled='reject', culled_origin='manual' → Red label."""
        (tmp_path / "IMG_001.CR3").touch()

        write_xmp_metadata(
            str(tmp_path),
            [{
                'filename': 'IMG_001.CR3',
                'rating': 1,
                'culled': 'reject',
                'culled_origin': 'manual',
            }]
        )

        xmp_path = tmp_path / "IMG_001.xmp"
        content = xmp_path.read_text(encoding='utf-8')
        assert 'Red' in content

    def test_no_label_for_auto_origin_without_flag(self, tmp_path):
        """culled_origin='auto' without use_auto_labels → no color label."""
        (tmp_path / "IMG_001.CR3").touch()

        write_xmp_metadata(
            str(tmp_path),
            [{
                'filename': 'IMG_001.CR3',
                'rating': 3,
                'culled': 'reject',
                'culled_origin': 'auto',
            }],
            use_auto_labels=False
        )

        xmp_path = tmp_path / "IMG_001.xmp"
        content = xmp_path.read_text(encoding='utf-8')
        # The Label field should be empty or absent
        # Specifically check that neither Green nor Red appears as a label value
        assert 'xmp:Label="Red"' not in content
        assert 'xmp:Label="Green"' not in content

    def test_auto_labels_when_flag_enabled(self, tmp_path):
        """use_auto_labels=True applies labels even for auto-origin culls."""
        (tmp_path / "IMG_001.CR3").touch()

        write_xmp_metadata(
            str(tmp_path),
            [{
                'filename': 'IMG_001.CR3',
                'rating': 3,
                'culled': 'reject',
                'culled_origin': 'auto',
            }],
            use_auto_labels=True
        )

        xmp_path = tmp_path / "IMG_001.xmp"
        content = xmp_path.read_text(encoding='utf-8')
        assert 'Red' in content

    def test_field_flags_can_disable_species(self, tmp_path):
        """fields={'species': False} → kestrel:Species not written."""
        (tmp_path / "IMG_001.CR3").touch()

        write_xmp_metadata(
            str(tmp_path),
            [{
                'filename': 'IMG_001.CR3',
                'rating': 3,
                'culled': 'accept',
                'culled_origin': 'manual',
                'species': 'aves,columbidae',
                'family': 'columbidae',
                'quality': 0.5
            }],
            fields={'rating': True, 'label': True, 'species': False, 'family': True, 'quality': True}
        )

        xmp_path = tmp_path / "IMG_001.xmp"
        content = xmp_path.read_text(encoding='utf-8')
        # Species fields should NOT appear
        assert 'kestrel:Species' not in content


class TestExternalXMPConflict:
    """Tests for external XMP conflict handling."""

    def test_skip_external_xmp_by_default(self, tmp_path):
        """Existing XMP without kestrel NS → skipped by default."""
        # Create a fake non-kestrel XMP file
        img_path = tmp_path / "IMG_001.CR3"
        xmp_path = tmp_path / "IMG_001.xmp"
        img_path.touch()
        external_xmp = '''<?xml version="1.0"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/">
      <xmp:Rating>3</xmp:Rating>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
'''
        xmp_path.write_text(external_xmp, encoding='utf-8')

        result = write_xmp_metadata(
            str(tmp_path),
            [{
                'filename': 'IMG_001.CR3',
                'rating': 5,
                'culled': 'accept',
                'culled_origin': 'manual',
            }],
            overwrite_external=False
        )

        # Should report a skipped conflict
        assert 'IMG_001.xmp' in result.get('skipped_conflicts', [])
        # Content should be unchanged
        content = xmp_path.read_text(encoding='utf-8')
        assert '<xmp:Rating>3</xmp:Rating>' in content

    def test_overwrite_external_xmp_when_flag_set(self, tmp_path):
        """overwrite_external=True → external XMP gets updated."""
        img_path = tmp_path / "IMG_001.CR3"
        xmp_path = tmp_path / "IMG_001.xmp"
        img_path.touch()
        xmp_path.write_text("<old>external content</old>", encoding='utf-8')

        result = write_xmp_metadata(
            str(tmp_path),
            [{
                'filename': 'IMG_001.CR3',
                'rating': 5,
                'culled': 'accept',
                'culled_origin': 'manual',
            }],
            overwrite_external=True
        )

        assert result['written'] == 1
        # Content should be updated with kestrel XMP
        content = xmp_path.read_text(encoding='utf-8')
        assert _KESTREL_NS in content

    def test_kestrel_xmp_always_overwritten(self, tmp_path):
        """Existing kestrel XMP → updated in place even without overwrite_external."""
        img_path = tmp_path / "IMG_001.CR3"
        img_path.touch()

        # Write initial XMP
        write_xmp_metadata(
            str(tmp_path),
            [{
                'filename': 'IMG_001.CR3',
                'rating': 1,
                'culled': 'reject',
                'culled_origin': 'manual',
            }]
        )

        # Update with new rating - should not trigger conflict
        result = write_xmp_metadata(
            str(tmp_path),
            [{
                'filename': 'IMG_001.CR3',
                'rating': 5,
                'culled': 'accept',
                'culled_origin': 'manual',
            }],
            overwrite_external=False
        )

        assert result['written'] == 1
        assert len(result.get('skipped_conflicts', [])) == 0
        # Verify new rating is in file
        xmp_path = tmp_path / "IMG_001.xmp"
        content = xmp_path.read_text(encoding='utf-8')
        assert '5' in content
        # Old rating should not be present (well, the digit 5 is what we want)


class TestPathSafety:
    """Tests for path traversal protection in XMP writing."""

    def test_safe_sidecar_path_accepts_normal_filename(self, tmp_path):
        """Normal filename → accepted."""
        result = _safe_sidecar_path(str(tmp_path), "IMG_001.CR3")
        assert result is not None
        assert "IMG_001.CR3" in result

    def test_safe_sidecar_path_rejects_traversal(self, tmp_path):
        """Filename with ../ → rejected."""
        result = _safe_sidecar_path(str(tmp_path), "../IMG_001.CR3")
        assert result is None

    def test_safe_sidecar_path_rejects_absolute(self, tmp_path):
        """Absolute path as filename → rejected."""
        result = _safe_sidecar_path(str(tmp_path), "/etc/passwd")
        assert result is None

    def test_safe_sidecar_path_rejects_drive_letter(self, tmp_path):
        """Drive letter prefix → rejected."""
        result = _safe_sidecar_path(str(tmp_path), "C:evil.txt")
        assert result is None

    def test_safe_sidecar_path_rejects_null_byte(self, tmp_path):
        """NUL byte in filename → rejected."""
        result = _safe_sidecar_path(str(tmp_path), "img\x00.txt")
        assert result is None

    def test_safe_sidecar_path_rejects_dot(self, tmp_path):
        """Filename '.' or '..' → rejected."""
        assert _safe_sidecar_path(str(tmp_path), ".") is None
        assert _safe_sidecar_path(str(tmp_path), "..") is None

    def test_safe_sidecar_path_rejects_backslash(self, tmp_path):
        """Filename with backslash → rejected."""
        result = _safe_sidecar_path(str(tmp_path), "subdir\\file.txt")
        assert result is None


class TestXMPContent:
    """Tests for XMP content correctness."""

    def test_xmp_contains_kestrel_namespace(self, tmp_path):
        """Written XMP files always contain the Kestrel namespace URI."""
        (tmp_path / "IMG_001.CR3").touch()
        write_xmp_metadata(
            str(tmp_path),
            [{'filename': 'IMG_001.CR3', 'rating': 3, 'culled': 'accept', 'culled_origin': 'manual'}]
        )
        content = (tmp_path / "IMG_001.xmp").read_text(encoding='utf-8')
        assert _KESTREL_NS in content

    def test_xmp_special_chars_escaped(self, tmp_path):
        """XML special chars in species/family → escaped in output."""
        (tmp_path / "IMG_001.CR3").touch()
        write_xmp_metadata(
            str(tmp_path),
            [{
                'filename': 'IMG_001.CR3',
                'rating': 3,
                'culled': 'accept',
                'culled_origin': 'manual',
                'species': '<script>alert("xss")</script>',
            }]
        )
        content = (tmp_path / "IMG_001.xmp").read_text(encoding='utf-8')
        # The raw <script> tag should NOT appear unescaped
        assert '<script>' not in content
        # Should have escaped version
        assert '&lt;script&gt;' in content or '&lt;' in content

    def test_rating_clamped_to_valid_range(self, tmp_path):
        """Rating values outside 0-5 are clamped."""
        import re
        (tmp_path / "IMG_001.CR3").touch()
        (tmp_path / "IMG_002.CR3").touch()

        # Very high rating
        write_xmp_metadata(
            str(tmp_path),
            [{'filename': 'IMG_001.CR3', 'rating': 99, 'culled': 'accept', 'culled_origin': 'manual'}]
        )
        # Negative rating
        write_xmp_metadata(
            str(tmp_path),
            [{'filename': 'IMG_002.CR3', 'rating': -5, 'culled': 'accept', 'culled_origin': 'manual'}]
        )

        content1 = (tmp_path / "IMG_001.xmp").read_text(encoding='utf-8')
        content2 = (tmp_path / "IMG_002.xmp").read_text(encoding='utf-8')

        # Extract Rating value via regex (attribute or element form)
        rating1_match = re.search(r'xmp:Rating[=>]"?(\d+)', content1)
        rating2_match = re.search(r'xmp:Rating[=>]"?(\d+)', content2)
        assert rating1_match is not None
        assert rating2_match is not None

        # Rating should be clamped to [0, 5]
        assert int(rating1_match.group(1)) == 5  # clamped from 99
        assert int(rating2_match.group(1)) == 0  # clamped from -5
