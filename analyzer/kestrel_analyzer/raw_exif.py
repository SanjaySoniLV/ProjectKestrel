"""
Image capture time extractor.

RAW formats (no dependencies required):
  CR3 (Canon ISOBMFF), CR2, NEF, ARW, DNG, ORF, RW2, PEF, SR2
  and any other TIFF-based RAW format.

JPEG/processed formats (requires Pillow):
  JPEG, PNG, TIFF

Unsupported: RAF (Fujifilm), X3F (Sigma)
"""

import struct
from datetime import datetime
from pathlib import Path

DATETIME_ORIGINAL_TAG = 0x9003
DATETIME_TAG = 0x0132
EXIF_IFD_TAG = 0x8769

DATE_FORMAT = "%Y:%m:%d %H:%M:%S"

CANON_METADATA_UUID = bytes.fromhex('85c0b687820f11e08111f4ce462b6a48')

PILLOW_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.tif', '.tiff'}
UNSUPPORTED_EXTENSIONS = {'.raf', '.x3f'}


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

    if exif_ifd_offset:
        pos = f.tell()
        result = _walk_ifd(f, endian, exif_ifd_offset, tiff_start, _depth + 1)
        f.seek(pos)
        if result:
            return result

    return datetime_fallback


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
# Format detection
# ---------------------------------------------------------------------------

TIFF_MAGICS = (
    b'II\x2a\x00',  # Little-endian TIFF (CR2, NEF, DNG, ARW, RW2, PEF, SR2...)
    b'MM\x00\x2a',  # Big-endian TIFF
    b'II\x52\x4f',  # ORF little-endian magic 0x4F52 ('OR')
    b'II\x52\x55',  # ORF little-endian magic 0x5552 ('UR') — used by some Olympus models
    b'MM\x4f\x52',  # ORF big-endian magic 0x4F52 ('OR')
)


def _is_cr3(f) -> bool:
    f.seek(0)
    header = f.read(12)
    return len(header) == 12 and header[4:8] == b'ftyp' and header[8:12] == b'crx '


def _is_tiff_based(f) -> bool:
    f.seek(0)
    return f.read(4) in TIFF_MAGICS


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_capture_time(filepath: str | Path) -> datetime:
    """
    Extract the capture datetime from an image file.

    Supports:
      - RAW: CR3, CR2, NEF, ARW, DNG, ORF, RW2, PEF, SR2 (no extra dependencies)
      - JPEG/PNG/TIFF: requires Pillow

    Returns a datetime object.
    Raises ValueError if the timestamp cannot be found.
    Raises RuntimeError if Pillow is needed but not installed.
    """
    filepath = Path(filepath)
    ext = filepath.suffix.lower()

    if ext in UNSUPPORTED_EXTENSIONS:
        raise ValueError(
            f"{ext.upper()} ({filepath.name}) is not supported. "
            "RAF (Fujifilm) and X3F (Sigma) are not implemented."
        )

    if ext in PILLOW_EXTENSIONS:
        raw_str = _read_pillow_exif(filepath)
        if not raw_str:
            raise ValueError(f"No capture time found in {filepath.name}")
        try:
            return datetime.strptime(raw_str.strip(), DATE_FORMAT)
        except ValueError:
            raise ValueError(f"Unrecognised datetime format: {raw_str!r}")

    # RAW formats — native parser
    with open(filepath, 'rb') as f:
        if _is_cr3(f):
            f.seek(0)
            raw_str = _read_cr3_exif(f)
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

    try:
        return datetime.strptime(raw_str.strip(), DATE_FORMAT)
    except ValueError:
        raise ValueError(f"Unrecognised datetime format: {raw_str!r}")


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