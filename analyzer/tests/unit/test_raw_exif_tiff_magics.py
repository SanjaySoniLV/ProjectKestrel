"""Regression tests for the TIFF/ORF container-magic table.

``raw_exif`` decides whether a file is a TIFF-based RAW in two independent
places, and they have to agree:

* ``_is_tiff_based`` matches the file's first four bytes against the
  ``TIFF_MAGICS`` byte-prefix table. This is the gate — a file that fails it
  is never handed to the TIFF parser at all.
* ``_read_tiff_exif`` reads the byte-order marker, then unpacks the next two
  bytes *in that byte order* and checks the resulting value against
  ``(42, 0x4F52, 0x5552)``.

Two of the ORF entries in the table used to disagree with the parser:
``II\\x55\\x00`` decodes little-endian to 0x0055 and ``MM\\x00\\x4f`` decodes
big-endian to 0x004F, neither of which the parser accepts. The correct
little-endian encoding of 0x5552 is ``\\x52\\x55`` and the correct big-endian
encoding of 0x4F52 is ``\\x4f\\x52``.

The effect was silent: an Olympus ORF using one of those magics failed the
gate, so no EXIF was read and the file got no ``capture_time`` — which scene
clustering groups by, and which the analysis-database identity work is built
on. Nothing errored; the timestamp was simply absent.

The consistency test below is the one that matters. Asserting specific byte
strings would only re-state the table; deriving the expected bytes from the
values the parser accepts is what catches the class of bug.
"""

import io
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer import raw_exif


pytestmark = pytest.mark.unit


#: The magic values ``_read_tiff_exif`` will accept, mirroring its own tuple.
_PARSER_MAGICS = (42, 0x4F52, 0x5552)


def _header(order: bytes, magic: int) -> bytes:
    """A minimal TIFF-ish header: byte-order marker, magic, IFD offset."""
    endian = "<" if order == b"II" else ">"
    return order + struct.pack(endian + "H", magic) + struct.pack(endian + "I", 8)


class TestMagicTableAgreesWithParser:
    def test_every_table_entry_decodes_to_a_magic_the_parser_accepts(self):
        """The gate must not admit a prefix the parser will then reject.

        This is the exact failure the wrong ORF bytes produced, in reverse:
        an entry in the table whose decoded value is not in the parser's
        accepted set is dead weight at best, and at worst — as here — sits in
        the slot where a real camera's magic should have been.
        """
        offenders = []
        for entry in raw_exif.TIFF_MAGICS:
            order, raw_magic = entry[:2], entry[2:4]
            endian = "<" if order == b"II" else ">"
            value = struct.unpack(endian + "H", raw_magic)[0]
            if value not in _PARSER_MAGICS:
                offenders.append(f"{entry!r} -> 0x{value:04X}")
        assert not offenders, (
            "TIFF_MAGICS entries that _read_tiff_exif would reject: "
            + ", ".join(offenders)
        )

    @pytest.mark.parametrize("magic", [0x4F52, 0x5552])
    def test_orf_magics_are_reachable_in_little_endian(self, magic):
        """An Olympus ORF must clear the gate, not just the parser."""
        header = _header(b"II", magic)
        assert raw_exif._is_tiff_based(io.BytesIO(header)), (
            f"little-endian ORF magic 0x{magic:04X} "
            f"(bytes {header[2:4]!r}) is not in TIFF_MAGICS"
        )

    def test_standard_tiff_still_passes_both_orders(self):
        """Guards against a fix to the ORF rows disturbing the common case."""
        assert raw_exif._is_tiff_based(io.BytesIO(_header(b"II", 42)))
        assert raw_exif._is_tiff_based(io.BytesIO(_header(b"MM", 42)))


class TestGateStillRejectsNonRaw:
    @pytest.mark.parametrize(
        "data",
        [
            b"\xff\xd8\xff\xe0",       # JPEG SOI + APP0
            b"%PDF",                    # PDF
            b"II\x00\x00",             # right byte-order marker, nonsense magic
            b"XX\x2a\x00",             # right magic, nonsense byte-order marker
            b"",                        # empty file
            b"II",                      # truncated below four bytes
        ],
    )
    def test_non_raw_headers_are_not_admitted(self, data):
        assert not raw_exif._is_tiff_based(io.BytesIO(data))
