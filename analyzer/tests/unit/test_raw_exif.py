"""Unit tests for raw_exif container parsers.

These tests build minimal synthetic byte structures for each container
format so the parsers can be exercised without checking in 100+ MB of
camera RAW samples. End-to-end coverage against real samples lives in
the integration suite (test_exif_read.py + set_b_formats/ fixtures).
"""

import io
import struct
from datetime import datetime
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer import raw_exif


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers — synthetic byte assembly
# ---------------------------------------------------------------------------

def _build_tiff(datetime_original: bytes | None = None,
                datetime_tag: bytes | None = None,
                xmp_packet: bytes | None = None,
                endian: str = '<') -> bytes:
    """Build a minimal TIFF with optional DateTime/DateTimeOriginal/XMP."""
    end = endian
    out = io.BytesIO()
    if end == '<':
        out.write(b'II\x2a\x00')
    else:
        out.write(b'MM\x00\x2a')
    out.write(struct.pack(end + 'I', 8))  # IFD0 at offset 8

    entries = []  # (tag, type, count, value_bytes_or_offset_placeholder)
    blobs = []   # extra payload to append after IFD0 + next-offset

    def add_ascii_entry(tag, payload):
        s = payload + b'\x00'  # NUL terminator
        if len(s) <= 4:
            entries.append((tag, 2, len(s), s.ljust(4, b'\x00')))
        else:
            entries.append((tag, 2, len(s), None))
            blobs.append(s)

    def add_xmp_entry(tag, payload):
        if len(payload) <= 4:
            entries.append((tag, 1, len(payload), payload.ljust(4, b'\x00')))
        else:
            entries.append((tag, 1, len(payload), None))
            blobs.append(payload)

    if datetime_original:
        add_ascii_entry(raw_exif.DATETIME_ORIGINAL_TAG, datetime_original)
    if datetime_tag:
        add_ascii_entry(raw_exif.DATETIME_TAG, datetime_tag)
    if xmp_packet:
        add_xmp_entry(raw_exif.XMP_TAG, xmp_packet)

    # Compute total IFD size: 2 bytes count + 12 bytes/entry + 4 bytes next-offset
    ifd_size = 2 + 12 * len(entries) + 4
    blob_offset_cursor = 8 + ifd_size

    out.write(struct.pack(end + 'H', len(entries)))
    blob_index = 0
    for tag, type_, count, value in entries:
        if value is None:
            value_field = struct.pack(end + 'I', blob_offset_cursor)
            blob_offset_cursor += len(blobs[blob_index])
            blob_index += 1
        else:
            value_field = value
        out.write(struct.pack(end + 'HHI', tag, type_, count) + value_field)
    out.write(struct.pack(end + 'I', 0))  # no next IFD
    for b in blobs:
        out.write(b)
    return out.getvalue()


def _build_mrw(tiff_blob: bytes) -> bytes:
    """Wrap a TIFF blob inside an MRW container with PRD + TTW sub-blocks."""
    prd = b'\x00PRD' + struct.pack('>I', 8) + b'\x00' * 8
    ttw = b'\x00TTW' + struct.pack('>I', len(tiff_blob)) + tiff_blob
    payload = prd + ttw
    return b'\x00MRM' + struct.pack('>I', len(payload)) + payload


def _build_jpeg_with_exif(tiff_blob: bytes) -> bytes:
    """Wrap a TIFF blob inside a minimal JPEG carrying an APP1 EXIF marker."""
    app1_payload = b'Exif\x00\x00' + tiff_blob
    # APP1 size includes the 2-byte size field itself but not the marker.
    app1 = b'\xff\xe1' + struct.pack('>H', len(app1_payload) + 2) + app1_payload
    return b'\xff\xd8' + app1 + b'\xff\xd9'


def _build_raf(jpeg_blob: bytes) -> bytes:
    """Build a minimal RAF container with an embedded JPEG."""
    header = bytearray(0x80)
    header[0:16] = b'FUJIFILMCCD-RAW '
    header[16:20] = b'0201'
    header[20:28] = b'TESTCAMA'
    header[28:60] = b'TestModel'.ljust(32, b'\x00')
    header[60:64] = b'0100'
    jpeg_offset = len(header)
    struct.pack_into('>I', header, 0x54, jpeg_offset)
    struct.pack_into('>I', header, 0x58, len(jpeg_blob))
    return bytes(header) + jpeg_blob


def _build_x3f(jpeg_blob: bytes, image_format: int = 18, image_type: int = 2) -> bytes:
    """Build a minimal X3F with one IMA2 section wrapping a JPEG preview.

    SECi layout: magic(4) version(4) type(4) format(4) cols(4) rows(4) row_size(4).
    The parser keys off `format` (18 = JPEG)."""
    sec_i = (b'SECi' + struct.pack('<I', 0x20000)
             + struct.pack('<I', image_type)
             + struct.pack('<I', image_format)
             + struct.pack('<I', 16) + struct.pack('<I', 16) + struct.pack('<I', 0))
    ima2 = sec_i + jpeg_blob

    file_header = b'FOVb' + b'\x00' * 40
    image_offset = len(file_header)
    image_length = len(ima2)
    body = file_header + ima2

    dir_offset = len(body)
    sec_d = (b'SECd' + struct.pack('<I', 0x20000) + struct.pack('<I', 1)
             + struct.pack('<I', image_offset) + struct.pack('<I', image_length) + b'IMA2')
    return body + sec_d + struct.pack('<I', dir_offset)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestParseDatetime:
    def test_standard_exif(self):
        assert raw_exif._parse_datetime("2022:11:20 15:38:30") == datetime(2022, 11, 20, 15, 38, 30)

    def test_iso8601_t(self):
        assert raw_exif._parse_datetime("2019-05-31T14:59:00") == datetime(2019, 5, 31, 14, 59, 0)

    def test_iso8601_trailing_z(self):
        assert raw_exif._parse_datetime("2013-07-24T17:50:30Z") == datetime(2013, 7, 24, 17, 50, 30)

    def test_iso8601_with_timezone(self):
        assert raw_exif._parse_datetime("2022:11:20 15:38:30+02:00") == datetime(2022, 11, 20, 15, 38, 30)

    def test_strip_subseconds(self):
        assert raw_exif._parse_datetime("2023:06:02 18:53:25.67+02:00") == datetime(2023, 6, 2, 18, 53, 25)

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            raw_exif._parse_datetime("not a date")


class TestMrwParser:
    def test_basic(self, tmp_path):
        tiff = _build_tiff(datetime_original=b"2010:07:29 11:43:16")
        mrw = _build_mrw(tiff)
        path = tmp_path / "test.MRW"
        path.write_bytes(mrw)
        assert raw_exif.get_capture_time(path) == datetime(2010, 7, 29, 11, 43, 16)

    def test_rejects_non_mrw(self, tmp_path):
        path = tmp_path / "not-mrw.MRW"
        path.write_bytes(b'NOPE' + b'\x00' * 100)
        with pytest.raises((ValueError, Exception)):
            raw_exif.get_capture_time(path)


class TestRafParser:
    def test_basic(self, tmp_path):
        tiff = _build_tiff(datetime_original=b"2022:11:20 15:38:30")
        jpeg = _build_jpeg_with_exif(tiff)
        raf = _build_raf(jpeg)
        path = tmp_path / "test.RAF"
        path.write_bytes(raf)
        assert raw_exif.get_capture_time(path) == datetime(2022, 11, 20, 15, 38, 30)

    def test_rejects_non_raf(self, tmp_path):
        path = tmp_path / "not-raf.RAF"
        path.write_bytes(b'NOTAFUJIFILMHEADER')
        with pytest.raises((ValueError, Exception)):
            raw_exif.get_capture_time(path)


class TestX3fParser:
    def test_preview_jpeg(self, tmp_path):
        tiff = _build_tiff(datetime_original=b"2018:04:09 11:49:15")
        jpeg = _build_jpeg_with_exif(tiff)
        x3f = _build_x3f(jpeg, image_format=18)
        path = tmp_path / "test.X3F"
        path.write_bytes(x3f)
        assert raw_exif.get_capture_time(path) == datetime(2018, 4, 9, 11, 49, 15)

    def test_thumb_jpeg(self, tmp_path):
        tiff = _build_tiff(datetime_original=b"2017:01:02 03:04:05")
        jpeg = _build_jpeg_with_exif(tiff)
        x3f = _build_x3f(jpeg, image_format=11)
        path = tmp_path / "test.X3F"
        path.write_bytes(x3f)
        assert raw_exif.get_capture_time(path) == datetime(2017, 1, 2, 3, 4, 5)


class TestTiffXmpFallback:
    def test_no_datetime_but_xmp_create_date(self, tmp_path):
        """MOS-style: no DateTime entries, only an XMP packet."""
        xmp = (b'<?xpacket begin?>'
               b'<x:xmpmeta xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
               b'<rdf:Description>'
               b'<xmp:CreateDate>2013-07-24T17:50:30Z</xmp:CreateDate>'
               b'</rdf:Description>'
               b'</x:xmpmeta>'
               b'<?xpacket end?>')
        tiff = _build_tiff(xmp_packet=xmp)
        path = tmp_path / "test.MOS"
        path.write_bytes(tiff)
        assert raw_exif.get_capture_time(path) == datetime(2013, 7, 24, 17, 50, 30)


class TestIso8601InSubIfd:
    """FFF-style: DateTimeOriginal lives in EXIF SubIFD as ISO 8601."""

    def test_iso8601_in_ifd(self, tmp_path):
        tiff = _build_tiff(datetime_original=b"2019-05-31T14:59:00")
        path = tmp_path / "test.FFF"
        path.write_bytes(tiff)
        assert raw_exif.get_capture_time(path) == datetime(2019, 5, 31, 14, 59, 0)


class TestUnsupportedExtensionsCleared:
    """Ensure RAF/X3F are no longer in the unsupported set."""

    def test_raf_not_unsupported(self):
        assert '.raf' not in raw_exif.UNSUPPORTED_EXTENSIONS

    def test_x3f_not_unsupported(self):
        assert '.x3f' not in raw_exif.UNSUPPORTED_EXTENSIONS
