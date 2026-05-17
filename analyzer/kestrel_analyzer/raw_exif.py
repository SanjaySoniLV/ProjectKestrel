"""
Image capture time extractor.

RAW formats (no dependencies required):
  CR3 (Canon ISOBMFF), CR2, NEF, NRW, ARW, DNG, ORF, RW2, PEF, SR2, SRW,
  ERF (Epson), DCR/KDC (Kodak), MEF (Mamiya), 3FR/FFF (Hasselblad),
  IIQ (Phase One), MOS (Leaf), RWL (Leica), MRW (Minolta),
  RAF (Fujifilm), X3F (Sigma).

JPEG/processed formats (requires Pillow):
  JPEG, PNG, TIFF
"""

import re
import struct
from datetime import datetime
from pathlib import Path

DATETIME_ORIGINAL_TAG = 0x9003
DATETIME_TAG = 0x0132
EXIF_IFD_TAG = 0x8769
XMP_TAG = 0x02bc

DATE_FORMAT = "%Y:%m:%d %H:%M:%S"

# Accepted datetime serializations encountered in the wild.
# - Standard EXIF: "2022:11:20 15:38:30"
# - ISO 8601:      "2019-05-31T14:59:00" (Hasselblad FFF SubIFD, MOS XMP)
# - ISO 8601 sp:   "2019-05-31 14:59:00"
_DATETIME_FORMATS = (
    "%Y:%m:%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)

CANON_METADATA_UUID = bytes.fromhex('85c0b687820f11e08111f4ce462b6a48')

PILLOW_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff'}
# All formats listed in config.RAW_EXTENSIONS now have in-house EXIF support.
# Kept as an empty set for backward compatibility with callers that import it.
UNSUPPORTED_EXTENSIONS: set[str] = set()


def _parse_datetime(raw_str: str) -> datetime:
    """Parse a capture-time string into a datetime.

    Handles the standard EXIF form ("YYYY:MM:DD HH:MM:SS") plus ISO 8601
    variants seen in Hasselblad FFF SubIFDs and XMP packets. Strips any
    trailing 'Z', timezone offset (`+02:00`/`-05:00`), or sub-second
    fraction (`.67`) before parsing.

    Raises ValueError if no format matches.
    """
    s = raw_str.strip().rstrip('\x00').strip()
    if not s:
        raise ValueError("empty datetime string")

    if s.endswith('Z'):
        s = s[:-1]
    # Trailing TZ offset: only consider +/- that appears AFTER the time
    # portion (position 19+). Date separators '-' inside the first 10
    # characters must not be stripped.
    if len(s) > 19:
        for marker in ('+', '-'):
            idx = s.rfind(marker)
            if idx >= 19:
                s = s[:idx]
                break
    # Trailing fractional seconds
    if len(s) > 19 and s[19] == '.':
        s = s[:19]
    s = s.strip()

    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognised datetime format: {raw_str!r}")


# XMP-embedded date markers — used as a last resort when a TIFF has no
# DateTime tag but does have an XMP packet (tag 0x02bc). Common in Leaf MOS.
_XMP_DATE_PATTERNS = (
    re.compile(rb'<exif:DateTimeOriginal>([^<]+)</exif:DateTimeOriginal>'),
    re.compile(rb'<xmp:CreateDate>([^<]+)</xmp:CreateDate>'),
    re.compile(rb'<photoshop:DateCreated>([^<]+)</photoshop:DateCreated>'),
)


def _extract_xmp_datetime(xmp_bytes: bytes) -> str | None:
    for pat in _XMP_DATE_PATTERNS:
        m = pat.search(xmp_bytes)
        if m:
            return m.group(1).decode('ascii', errors='ignore').strip()
    return None


# ---------------------------------------------------------------------------
# JPEG/PNG/TIFF via Pillow
# ---------------------------------------------------------------------------

def _read_pillow_exif(filepath: Path) -> str | None:
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError(
            f"Pillow is required to read {filepath.suffix} files. "
            "Install it with: pip install Pillow"
        )
    with Image.open(filepath) as img:
        exif = img._getexif()
    if not exif:
        return None
    val = exif.get(DATETIME_ORIGINAL_TAG) or exif.get(DATETIME_TAG)
    return val.strip() if val else None


# ---------------------------------------------------------------------------
# TIFF-based RAW parser (CR2, NEF, ARW, DNG, ORF, RW2, PEF, SR2, etc.)
# ---------------------------------------------------------------------------

def _read_tiff_exif(f) -> str | None:
    tiff_start = f.tell()  # all offsets inside TIFF are relative to this

    endian_bytes = f.read(2)  # exactly 2 bytes for byte order marker
    if endian_bytes == b'II':
        endian = '<'
    elif endian_bytes == b'MM':
        endian = '>'
    else:
        return None

    magic = struct.unpack(endian + 'H', f.read(2))[0]
    if magic not in (42, 0x4F52, 0x5552):  # TIFF, ORF variants
        return None

    ifd_offset = struct.unpack(endian + 'I', f.read(4))[0]
    return _walk_ifd(f, endian, tiff_start + ifd_offset, tiff_start)


def _walk_ifd(f, endian: str, offset: int, tiff_start: int, _depth: int = 0) -> str | None:
    if _depth > 4 or offset == 0:
        return None

    f.seek(offset)
    try:
        num_entries = struct.unpack(endian + 'H', f.read(2))[0]
    except struct.error:
        return None

    if num_entries > 256:
        return None

    exif_ifd_offset = None
    datetime_fallback = None
    xmp_offset = None
    xmp_count = 0

    for _ in range(num_entries):
        entry = f.read(12)
        if len(entry) < 12:
            break

        tag, type_, count = struct.unpack(endian + 'HHI', entry[:8])
        raw_value = entry[8:12]

        if tag == DATETIME_ORIGINAL_TAG:
            val = _read_ascii(f, endian, type_, count, raw_value, tiff_start)
            if val:
                return val.strip('\x00')

        elif tag == DATETIME_TAG:
            val = _read_ascii(f, endian, type_, count, raw_value, tiff_start)
            if val:
                datetime_fallback = val.strip('\x00')

        elif tag == EXIF_IFD_TAG:
            exif_ifd_offset = tiff_start + struct.unpack(endian + 'I', raw_value)[0]

        elif tag == XMP_TAG and count > 16:
            # XMP packet — record it as a last-resort fallback. Stored as
            # bytes (type 1) or undefined (type 7); count bytes live at the
            # offset stored in raw_value.
            xmp_offset = tiff_start + struct.unpack(endian + 'I', raw_value)[0]
            xmp_count = count

    if exif_ifd_offset:
        pos = f.tell()
        result = _walk_ifd(f, endian, exif_ifd_offset, tiff_start, _depth + 1)
        f.seek(pos)
        if result:
            return result

    if datetime_fallback:
        return datetime_fallback

    # Last resort: read XMP packet and pull a date from it (e.g. Leaf MOS,
    # which has no DateTime IFD entry at all).
    if xmp_offset and xmp_count:
        pos = f.tell()
        try:
            f.seek(xmp_offset)
            xmp_bytes = f.read(min(xmp_count, 1024 * 1024))
            xmp_val = _extract_xmp_datetime(xmp_bytes)
            if xmp_val:
                return xmp_val
        finally:
            f.seek(pos)

    return None


def _read_ascii(f, endian: str, type_: int, count: int, raw_value: bytes, tiff_start: int) -> str | None:
    if type_ != 2:
        return None
    if count <= 4:
        return raw_value[:count].decode('ascii', errors='ignore')
    offset = tiff_start + struct.unpack(endian + 'I', raw_value)[0]
    pos = f.tell()
    f.seek(offset)
    val = f.read(count).decode('ascii', errors='ignore')
    f.seek(pos)
    return val


# ---------------------------------------------------------------------------
# CR3 parser (ISOBMFF/MP4 box-walking)
# ---------------------------------------------------------------------------

def _read_cr3_exif(f) -> str | None:
    """Entry point: find file size and start recursive ISOBMFF walk."""
    f.seek(0, 2)
    file_size = f.tell()
    f.seek(0)
    return _walk_isobmff(f, file_size)


def _walk_isobmff(f, end: int, _depth: int = 0) -> str | None:
    """Recursively walk ISOBMFF boxes looking for Canon's metadata UUID.
    The UUID lives inside the moov box, not at the top level."""
    if _depth > 4:
        return None

    while f.tell() < end - 8:
        box_start = f.tell()
        header = f.read(8)
        if len(header) < 8:
            break

        size = struct.unpack('>I', header[:4])[0]
        box_type = header[4:8]

        if size == 1:
            ext = f.read(8)
            size = struct.unpack('>Q', ext)[0]
        elif size == 0:
            size = end - box_start

        box_end = box_start + size

        if box_type == b'moov':
            # Canon UUID is nested inside moov — recurse
            result = _walk_isobmff(f, box_end, _depth + 1)
            if result:
                return result

        elif box_type == b'uuid':
            uuid_bytes = f.read(16)
            if uuid_bytes == CANON_METADATA_UUID:
                result = _walk_canon_uuid(f, box_end)
                if result:
                    return result

        f.seek(box_end)

    return None


def _walk_canon_uuid(f, end: int) -> str | None:
    """Walk sub-boxes inside Canon's metadata UUID, parse CMT1/CMT2 as TIFF."""
    while f.tell() < end - 8:
        box_start = f.tell()
        header = f.read(8)
        if len(header) < 8:
            break

        size = struct.unpack('>I', header[:4])[0]
        box_type = header[4:8]
        box_end = box_start + size

        if box_type in (b'CMT1', b'CMT2'):
            # Real file handle — tiff_start resolves correctly against the file
            try:
                result = _read_tiff_exif(f)
                if result:
                    return result
            except Exception:
                pass

        f.seek(box_end)

    return None


# ---------------------------------------------------------------------------
# MRW parser (Minolta DiMAGE / Konica Minolta Dynax)
# ---------------------------------------------------------------------------
#
# Container layout (offsets relative to file start):
#   0x00  \x00MRM
#   0x04  uint32 BE  outer-block payload length
#   0x08+ sequence of sub-blocks: \x00<TAG> + uint32 BE length + payload
#         The \x00TTW sub-block ("TIFF Tag Wrapper") wraps a complete TIFF
#         stream — DateTime/DateTimeOriginal live there.

def _read_mrw_exif(f) -> str | None:
    f.seek(0)
    header = f.read(8)
    if len(header) < 8 or header[:4] != b'\x00MRM':
        return None
    outer_size = struct.unpack('>I', header[4:8])[0]

    pos = 8
    end = 8 + outer_size
    while pos < end - 8:
        f.seek(pos)
        block = f.read(8)
        if len(block) < 8:
            break
        tag = block[:4]
        size = struct.unpack('>I', block[4:8])[0]
        if tag == b'\x00TTW':
            # File handle is now positioned at the start of the TTW payload,
            # which is itself a complete TIFF stream.
            return _read_tiff_exif(f)
        pos += 8 + size
    return None


# ---------------------------------------------------------------------------
# RAF parser (Fujifilm)
# ---------------------------------------------------------------------------
#
# Container layout:
#   0x00  "FUJIFILMCCD-RAW " (16 bytes)
#   0x10  format version  (e.g. "0201")
#   0x14  camera id
#   0x1c  model (32 bytes, null-padded)
#   0x3c  directory version
#   0x54  uint32 BE  embedded-JPEG offset
#   0x58  uint32 BE  embedded-JPEG size
# The embedded JPEG is a regular JFIF with an APP1 EXIF marker — the EXIF
# block's TIFF payload starts right after "Exif\x00\x00".

def _read_raf_exif(f) -> str | None:
    f.seek(0)
    if f.read(16) != b'FUJIFILMCCD-RAW ':
        return None
    f.seek(0x54)
    offs = f.read(8)
    if len(offs) < 8:
        return None
    jpeg_offset = struct.unpack('>I', offs[:4])[0]
    jpeg_size = struct.unpack('>I', offs[4:8])[0]
    if jpeg_size <= 0 or jpeg_offset <= 0:
        return None
    return _read_exif_in_jpeg(f, jpeg_offset, jpeg_size)


# ---------------------------------------------------------------------------
# X3F parser (Sigma Foveon)
# ---------------------------------------------------------------------------
#
# Container layout:
#   0x00  "FOVb"
#   ...   header + raw image data
#   EOF-4 uint32 LE  directory offset
#   @dir  "SECd" + uint32 LE version + uint32 LE n_entries +
#         entries[N] of { uint32 offset, uint32 length, char[4] type }
#
# IMA2 entries are image sections (SECi header). image_type==18 marks the
# embedded preview JPEG, which carries a standard EXIF APP1 segment.

def _read_x3f_exif(f) -> str | None:
    f.seek(0)
    if f.read(4) != b'FOVb':
        return None

    f.seek(0, 2)
    file_size = f.tell()
    if file_size < 32:
        return None

    f.seek(file_size - 4)
    dir_offset = struct.unpack('<I', f.read(4))[0]
    if not (0 < dir_offset < file_size - 12):
        return None

    f.seek(dir_offset)
    if f.read(4) != b'SECd':
        return None
    f.read(4)  # version
    n_entries = struct.unpack('<I', f.read(4))[0]
    if n_entries > 64:
        return None

    entries = []
    for _ in range(n_entries):
        e = f.read(12)
        if len(e) < 12:
            break
        offset = struct.unpack('<I', e[0:4])[0]
        length = struct.unpack('<I', e[4:8])[0]
        type_ = e[8:12]
        entries.append((offset, length, type_))

    for offset, length, type_ in entries:
        if type_ != b'IMA2' or length < 32:
            continue
        if offset + length > file_size:
            continue
        f.seek(offset)
        sec_head = f.read(28)
        if len(sec_head) < 28 or sec_head[:4] != b'SECi':
            continue
        # SECi layout: magic(4) + version(4) + image_type(4) + image_format(4)
        # + cols(4) + rows(4) + row_size(4). For embedded previews the format
        # field is 18 (JPEG); the older thumb-type-only path also sets 11.
        image_format = struct.unpack('<I', sec_head[12:16])[0]
        if image_format not in (11, 18):
            continue
        # JPEG payload follows the 28-byte SECi header.
        jpeg_offset = offset + 28
        jpeg_size = length - 28
        result = _read_exif_in_jpeg(f, jpeg_offset, jpeg_size)
        if result:
            return result
    return None


# ---------------------------------------------------------------------------
# Embedded-JPEG EXIF helper (used by RAF and X3F)
# ---------------------------------------------------------------------------

def _read_exif_in_jpeg(f, jpeg_offset: int, jpeg_size: int) -> str | None:
    """Locate the APP1/EXIF marker inside an embedded JPEG and parse its
    TIFF payload. Scans up to the first 64 KB of the JPEG (EXIF APP1 is
    always near the front)."""
    f.seek(jpeg_offset)
    head = f.read(2)
    if head != b'\xff\xd8':
        return None

    scan_limit = min(jpeg_size, 65536)
    f.seek(jpeg_offset)
    chunk = f.read(scan_limit)

    # Find the first APP1 marker whose payload begins with "Exif\x00\x00".
    idx = chunk.find(b'\xff\xe1', 2)
    while idx >= 0:
        # APP1 layout: FF E1 <len:2 BE> "Exif\x00\x00" <tiff...>
        if idx + 10 <= len(chunk) and chunk[idx + 4:idx + 10] == b'Exif\x00\x00':
            f.seek(jpeg_offset + idx + 10)
            return _read_tiff_exif(f)
        idx = chunk.find(b'\xff\xe1', idx + 2)
    return None


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

TIFF_MAGICS = (
    b'II\x2a\x00',  # Little-endian TIFF (CR2, NEF, DNG, ARW, RW2, PEF, SR2...)
    b'MM\x00\x2a',  # Big-endian TIFF
    b'II\x52\x4f',  # ORF (Olympus) variant
    b'II\x55\x00',  # ORF variant 2
    b'MM\x00\x4f',  # ORF variant 3
)


def _is_cr3(f) -> bool:
    f.seek(0)
    header = f.read(12)
    return len(header) == 12 and header[4:8] == b'ftyp' and header[8:12] == b'crx '


def _is_tiff_based(f) -> bool:
    f.seek(0)
    return f.read(4) in TIFF_MAGICS


def _is_raf(f) -> bool:
    f.seek(0)
    return f.read(16) == b'FUJIFILMCCD-RAW '


def _is_x3f(f) -> bool:
    f.seek(0)
    return f.read(4) == b'FOVb'


def _is_mrw(f) -> bool:
    f.seek(0)
    return f.read(4) == b'\x00MRM'


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_capture_time(filepath: str | Path) -> datetime:
    """
    Extract the capture datetime from an image file.

    Supports:
      - RAW: CR3, CR2, NEF, NRW, ARW, SRW, DNG, ORF, RW2, PEF, SR2,
             ERF, DCR, KDC, MEF, 3FR, FFF, IIQ, MOS, RWL, MRW, RAF, X3F
             (no extra dependencies)
      - JPEG/PNG/TIFF: requires Pillow

    Returns a datetime object.
    Raises ValueError if the timestamp cannot be found.
    Raises RuntimeError if Pillow is needed but not installed.
    """
    filepath = Path(filepath)
    ext = filepath.suffix.lower()

    if ext in PILLOW_EXTENSIONS:
        raw_str = _read_pillow_exif(filepath)
        if not raw_str:
            raise ValueError(f"No capture time found in {filepath.name}")
        return _parse_datetime(raw_str)

    # RAW formats — native parser. Magic-byte sniffing routes to the right
    # container parser; the extension is only a hint.
    with open(filepath, 'rb') as f:
        if _is_cr3(f):
            f.seek(0)
            raw_str = _read_cr3_exif(f)
        elif _is_raf(f):
            f.seek(0)
            raw_str = _read_raf_exif(f)
        elif _is_x3f(f):
            f.seek(0)
            raw_str = _read_x3f_exif(f)
        elif _is_mrw(f):
            f.seek(0)
            raw_str = _read_mrw_exif(f)
        elif _is_tiff_based(f):
            f.seek(0)
            raw_str = _read_tiff_exif(f)
        else:
            raw_str = None

        # Fallback to exifread if installed
        if raw_str is None:
            try:
                import exifread
                f.seek(0)
                tags = exifread.process_file(f, stop_tag='EXIF DateTimeOriginal', details=False)
                tag = tags.get('EXIF DateTimeOriginal')
                raw_str = str(tag) if tag else None
            except ImportError:
                pass

    if not raw_str:
        raise ValueError(f"Could not extract capture time from {filepath.name}")

    return _parse_datetime(raw_str)


# Convenience alias
get_datetime = get_capture_time


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print("Usage: python raw_exif.py <file1> [file2 ...]")
        sys.exit(1)

    for path in sys.argv[1:]:
        try:
            dt = get_capture_time(path)
            print(f"{path}: {dt}")
        except (ValueError, RuntimeError) as e:
            print(f"{path}: ERROR — {e}")