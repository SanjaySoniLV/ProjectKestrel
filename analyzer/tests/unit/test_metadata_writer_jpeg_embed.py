"""Unit tests for the embed-XMP-into-JPEG path in metadata_writer.py.

These exercise ``write_xmp_metadata(..., embed_jpeg=True)``, which writes the
same XMP fields Kestrel puts in a .xmp sidecar *directly into the JPEG's own
XMP segment* via exiv2/pyexiv2. Adobe Lightroom ignores .xmp sidecars for
JPEGs, so embedding is the only way those ratings/labels reach Lightroom.

The key safety property is that embedding must NOT recompress or otherwise
alter the pixel data — exiv2 rewrites only the metadata segment. The tests
assert that byte-for-byte on the compressed scan, plus round-trip, merge,
field-gating, opt-out, idempotency, and the path-traversal jail.

Real JPEG fixtures are copied into ``tmp_path`` first so the checked-in
fixtures are never modified.
"""

import hashlib
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from metadata_writer import write_xmp_metadata

# pyexiv2 is required to actually rewrite a JPEG's APP1 segment. Conflict-skip
# tests below monkeypatch ``_embed_xmp_in_jpeg`` so they still run when the
# native module is missing. Tests that call pyexiv2 skip via ``_read_xmp``.
try:
    import pyexiv2
except ImportError:
    pyexiv2 = None

pytestmark = pytest.mark.unit

_KESTREL_NS = "http://ns.projectkestrel.app/xmp/1.0/"

_FOREIGN_XMP = """<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
      xmlns:xmp="http://ns.adobe.com/xap/1.0/"
      xmp:Rating="3">
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""

_KESTREL_IN_FILE_XMP = f"""<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
    <rdf:Description rdf:about=""
      xmlns:xmp="http://ns.adobe.com/xap/1.0/"
      xmlns:kestrel="{_KESTREL_NS}"
      xmp:Rating="4"
      kestrel:CullStatus="accept">
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""


def _jpeg_with_app1_xmp(xmp_xml: str) -> bytes:
    """Minimal JPEG: SOI + APP1 XMP + EOI. Enough for the in-file origin probe."""
    ident = b"http://ns.adobe.com/xap/1.0/\x00"
    payload = ident + xmp_xml.encode("utf-8")
    app1 = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    return b"\xff\xd8" + app1 + b"\xff\xd9"


def _stub_embed_mutates(monkeypatch):
    """If embed runs, the JPEG is overwritten so a skip is observable without pyexiv2."""
    called: list[str] = []

    def fake(path, **_kwargs):
        called.append(os.path.abspath(path))
        Path(path).write_bytes(b"MUTATED")

    monkeypatch.setattr("metadata_writer._embed_xmp_in_jpeg", fake)
    return called


def _jpeg_scan_bytes(path):
    """Return the compressed scan (SOS marker → EOF). Identical bytes across
    two files means the pixel data was not recompressed."""
    data = Path(path).read_bytes()
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            break
        m = data[i + 1]
        if m == 0xD8:
            i += 2
            continue
        if 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if m == 0xDA:  # Start Of Scan — pixel data follows to EOI
            return data[i:]
        seg_len = int.from_bytes(data[i + 2:i + 4], "big")
        i += 2 + seg_len
    return b""


def _scan_hash(path):
    return hashlib.sha256(_jpeg_scan_bytes(path)).hexdigest()


def _read_xmp(path):
    if pyexiv2 is None:
        pytest.skip("pyexiv2 not installed")
    img = pyexiv2.Image(str(path))
    try:
        return img.read_xmp()
    finally:
        img.close()


@pytest.fixture
def jpeg_dir(set_d_path, tmp_path):
    """Copy the checked-in JPEG-only fixture set into a writable tmp dir."""
    if not set_d_path.exists():
        pytest.skip("set_d_jpeg_only fixtures not present")
    dst = tmp_path / "jpegs"
    dst.mkdir()
    copied = []
    for f in sorted(set_d_path.glob("*.JPG")) + sorted(set_d_path.glob("*.jpg")):
        target = dst / f.name
        shutil.copy(f, target)
        copied.append(target.name)
    if not copied:
        pytest.skip("no JPEG fixtures found in set_d_jpeg_only")
    return dst, copied


def _entry(filename, **over):
    e = {
        "filename": filename,
        "rating": 4,
        "culled": "accept",
        "culled_origin": "manual",
        "species": "Atlantic Puffin",
        "family": "Alcidae",
        "quality": 0.8734,
    }
    e.update(over)
    return e


class TestEmbedIntoJpeg:
    @pytest.fixture(autouse=True)
    def _require_pyexiv2(self):
        pytest.importorskip("pyexiv2")

    def test_embed_writes_xmp_and_reports_count(self, jpeg_dir):
        root, names = jpeg_dir
        payload = [_entry(n) for n in names]
        res = write_xmp_metadata(
            str(root), payload, embed_jpeg=True, overwrite_external=True
        )

        assert res["success"] is True
        assert res["embedded"] == len(names)
        assert res["embed_errors"] == []
        # sidecars are still written for every file (unchanged behaviour)
        assert res["written"] == len(names)

        xmp = _read_xmp(root / names[0])
        assert xmp.get("Xmp.xmp.Rating") == "4"
        assert xmp.get("Xmp.xmp.Label") == "Green"
        assert xmp.get("Xmp.kestrel.Species") == "Atlantic Puffin"
        assert xmp.get("Xmp.kestrel.CullStatus") == "accept"

    def test_embed_does_not_recompress_pixels(self, jpeg_dir):
        root, names = jpeg_dir
        target = root / names[0]
        before = _scan_hash(target)
        write_xmp_metadata(
            str(root), [_entry(names[0])], embed_jpeg=True, overwrite_external=True
        )
        after = _scan_hash(target)
        assert before == after, "compressed pixel scan changed — file was recompressed"

    def test_embed_preserves_preexisting_xmp(self, jpeg_dir):
        root, names = jpeg_dir
        target = root / names[0]
        before_keys = set(_read_xmp(target).keys())
        write_xmp_metadata(
            str(root), [_entry(names[0])], embed_jpeg=True, overwrite_external=True
        )
        after_keys = set(_read_xmp(target).keys())
        # every pre-existing property survives (merge, not clobber)
        assert before_keys.issubset(after_keys)

    def test_embed_respects_field_selection(self, jpeg_dir):
        root, names = jpeg_dir
        fields = {"rating": True, "label": False, "species": False,
                  "family": False, "quality": False}
        write_xmp_metadata(
            str(root),
            [_entry(names[0])],
            fields=fields,
            embed_jpeg=True,
            overwrite_external=True,
        )
        xmp = _read_xmp(root / names[0])
        assert xmp.get("Xmp.xmp.Rating") == "4"
        assert "Xmp.xmp.Label" not in xmp
        assert "Xmp.kestrel.Species" not in xmp
        assert "Xmp.kestrel.QualityScore" not in xmp

    def test_embed_is_idempotent(self, jpeg_dir):
        root, names = jpeg_dir
        target = root / names[0]
        write_xmp_metadata(
            str(root),
            [_entry(names[0], rating=4)],
            embed_jpeg=True,
            overwrite_external=True,
        )
        # Second write: in-file XMP is now Kestrel-authored; no flag required.
        write_xmp_metadata(str(root), [_entry(names[0], rating=5)], embed_jpeg=True)
        xmp = _read_xmp(target)
        assert xmp.get("Xmp.xmp.Rating") == "5"
        # unrelated Kestrel fields still present after the second write
        assert xmp.get("Xmp.kestrel.Species") == "Atlantic Puffin"
        # still a structurally valid JPEG (scan intact)
        assert _jpeg_scan_bytes(target), "scan segment missing after re-embed"


class TestEmbedNonJpegAndSafety:
    def test_embed_off_by_default_leaves_file_untouched(self, jpeg_dir):
        root, names = jpeg_dir
        target = root / names[0]
        before = Path(target).read_bytes()
        res = write_xmp_metadata(str(root), [_entry(names[0])])  # embed_jpeg defaults False
        assert res.get("embedded", 0) == 0
        assert Path(target).read_bytes() == before, "JPEG modified despite embed_jpeg=False"

    def test_non_jpeg_not_embedded(self, tmp_path):
        # A RAW file: sidecar is written, but nothing is embedded.
        (tmp_path / "IMG_9001.CR3").touch()
        res = write_xmp_metadata(
            str(tmp_path), [_entry("IMG_9001.CR3")], embed_jpeg=True
        )
        assert res["success"] is True
        assert res["written"] == 1          # sidecar
        assert res["embedded"] == 0          # RAW never modified
        assert res["embed_errors"] == []

    def test_missing_jpeg_file_no_embed_no_crash(self, tmp_path):
        # Entry names a JPEG that isn't on disk: sidecar still writes (it does
        # not require the image to exist), embed is skipped silently.
        res = write_xmp_metadata(
            str(tmp_path), [_entry("ghost.jpg")], embed_jpeg=True
        )
        assert res["success"] is True
        assert res["written"] == 1
        assert res["embedded"] == 0
        assert res["embed_errors"] == []

    def test_path_traversal_rejected_for_embed(self, jpeg_dir):
        root, names = jpeg_dir
        # Point a traversal filename at a real JPEG one level up; it must be
        # rejected before any embed and never touch a file outside root.
        outside = root.parent / "victim.jpg"
        shutil.copy(root / names[0], outside)
        outside_scan_before = _scan_hash(outside)

        res = write_xmp_metadata(
            str(root), [{"filename": "../victim.jpg", "rating": 5,
                         "culled": "accept", "culled_origin": "manual"}],
            embed_jpeg=True,
        )
        assert res["embedded"] == 0
        assert any("victim.jpg" in e for e in res["errors"])
        # the file outside root is byte-identical — never opened for write
        assert _scan_hash(outside) == outside_scan_before
        if pyexiv2 is not None:
            xmp = _read_xmp(outside)
            assert xmp.get("Xmp.xmp.Rating") != "5"


class TestEmbedConflictGuard:
    """S0-07: embed only when the batch is allowed to write this JPEG."""

    def test_foreign_sidecar_skips_embed(self, tmp_path, monkeypatch):
        """Arrange A: foreign .xmp + embed_jpeg, overwrite_external=False."""
        jpeg = tmp_path / "IMG_001.jpg"
        original = _jpeg_with_app1_xmp(_FOREIGN_XMP)
        jpeg.write_bytes(original)
        (tmp_path / "IMG_001.xmp").write_text(_FOREIGN_XMP, encoding="utf-8")
        called = _stub_embed_mutates(monkeypatch)

        res = write_xmp_metadata(
            str(tmp_path),
            [_entry("IMG_001.jpg", rating=5)],
            embed_jpeg=True,
            overwrite_external=False,
        )

        assert res["success"] is True
        assert res["embedded"] == 0
        assert called == []
        assert jpeg.read_bytes() == original
        assert "IMG_001.xmp" in (res.get("skipped_conflicts") or [])

    def test_foreign_in_file_xmp_skips_embed_without_flag(self, tmp_path, monkeypatch):
        """Arrange B: foreign in-file rating, no sidecar conflict."""
        jpeg = tmp_path / "IMG_001.jpg"
        original = _jpeg_with_app1_xmp(_FOREIGN_XMP)
        jpeg.write_bytes(original)
        called = _stub_embed_mutates(monkeypatch)

        res = write_xmp_metadata(
            str(tmp_path),
            [_entry("IMG_001.jpg", rating=5)],
            embed_jpeg=True,
            overwrite_external=False,
        )

        assert res["success"] is True
        assert res["embedded"] == 0
        assert res["written"] == 0
        assert called == []
        assert jpeg.read_bytes() == original
        assert "IMG_001.xmp" in (res.get("skipped_conflicts") or [])
        assert not (tmp_path / "IMG_001.xmp").exists()

    def test_overwrite_external_embeds_foreign_in_file(self, tmp_path, monkeypatch):
        jpeg = tmp_path / "IMG_001.jpg"
        jpeg.write_bytes(_jpeg_with_app1_xmp(_FOREIGN_XMP))
        called = _stub_embed_mutates(monkeypatch)

        res = write_xmp_metadata(
            str(tmp_path),
            [_entry("IMG_001.jpg", rating=5)],
            embed_jpeg=True,
            overwrite_external=True,
        )

        assert res["embedded"] == 1
        assert called == [os.path.abspath(str(jpeg))]
        assert jpeg.read_bytes() == b"MUTATED"

    def test_kestrel_in_file_embeds_without_flag(self, tmp_path, monkeypatch):
        jpeg = tmp_path / "IMG_001.jpg"
        jpeg.write_bytes(_jpeg_with_app1_xmp(_KESTREL_IN_FILE_XMP))
        called = _stub_embed_mutates(monkeypatch)

        res = write_xmp_metadata(
            str(tmp_path),
            [_entry("IMG_001.jpg", rating=5)],
            embed_jpeg=True,
            overwrite_external=False,
        )

        assert res["embedded"] == 1
        assert called == [os.path.abspath(str(jpeg))]

    def test_jpeg_without_xmp_still_embeds(self, tmp_path, monkeypatch):
        jpeg = tmp_path / "IMG_001.jpg"
        jpeg.write_bytes(b"\xff\xd8\xff\xd9")
        called = _stub_embed_mutates(monkeypatch)

        res = write_xmp_metadata(
            str(tmp_path),
            [_entry("IMG_001.jpg", rating=5)],
            embed_jpeg=True,
            overwrite_external=False,
        )

        assert res["embedded"] == 1
        assert called == [os.path.abspath(str(jpeg))]
