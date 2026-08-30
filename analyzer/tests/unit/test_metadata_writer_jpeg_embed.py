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

# pyexiv2 is the whole point of this feature; skip the module cleanly if the
# native dependency isn't installed in this environment.
pyexiv2 = pytest.importorskip("pyexiv2")

pytestmark = pytest.mark.unit

_KESTREL_NS = "http://ns.projectkestrel.app/xmp/1.0/"


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
    def test_embed_writes_xmp_and_reports_count(self, jpeg_dir):
        root, names = jpeg_dir
        payload = [_entry(n) for n in names]
        res = write_xmp_metadata(str(root), payload, embed_jpeg=True)

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
        write_xmp_metadata(str(root), [_entry(names[0])], embed_jpeg=True)
        after = _scan_hash(target)
        assert before == after, "compressed pixel scan changed — file was recompressed"

    def test_embed_preserves_preexisting_xmp(self, jpeg_dir):
        root, names = jpeg_dir
        target = root / names[0]
        before_keys = set(_read_xmp(target).keys())
        write_xmp_metadata(str(root), [_entry(names[0])], embed_jpeg=True)
        after_keys = set(_read_xmp(target).keys())
        # every pre-existing property survives (merge, not clobber)
        assert before_keys.issubset(after_keys)

    def test_embed_off_by_default_leaves_file_untouched(self, jpeg_dir):
        root, names = jpeg_dir
        target = root / names[0]
        before = Path(target).read_bytes()
        res = write_xmp_metadata(str(root), [_entry(names[0])])  # embed_jpeg defaults False
        assert res.get("embedded", 0) == 0
        assert Path(target).read_bytes() == before, "JPEG modified despite embed_jpeg=False"

    def test_embed_respects_field_selection(self, jpeg_dir):
        root, names = jpeg_dir
        fields = {"rating": True, "label": False, "species": False,
                  "family": False, "quality": False}
        write_xmp_metadata(str(root), [_entry(names[0])], fields=fields, embed_jpeg=True)
        xmp = _read_xmp(root / names[0])
        assert xmp.get("Xmp.xmp.Rating") == "4"
        assert "Xmp.xmp.Label" not in xmp
        assert "Xmp.kestrel.Species" not in xmp
        assert "Xmp.kestrel.QualityScore" not in xmp

    def test_embed_is_idempotent(self, jpeg_dir):
        root, names = jpeg_dir
        target = root / names[0]
        write_xmp_metadata(str(root), [_entry(names[0], rating=4)], embed_jpeg=True)
        write_xmp_metadata(str(root), [_entry(names[0], rating=5)], embed_jpeg=True)
        xmp = _read_xmp(target)
        assert xmp.get("Xmp.xmp.Rating") == "5"
        # unrelated Kestrel fields still present after the second write
        assert xmp.get("Xmp.kestrel.Species") == "Atlantic Puffin"
        # still a structurally valid JPEG (scan intact)
        assert _jpeg_scan_bytes(target), "scan segment missing after re-embed"


class TestEmbedRespectsSidecarConflictGate:
    """Embedding rewrites the user's ORIGINAL file, so it must only happen for
    files the batch is actually cleared to write.

    The sidecar-conflict check refuses to touch a .xmp Kestrel did not author
    until the user confirms an overwrite. The embed step originally ran *above*
    that check, so a file whose sidecar was withheld pending confirmation had
    already had its original modified — and was then rewritten a second time
    when the user confirmed.
    """

    def _external_sidecar(self, root, name):
        """Write a non-Kestrel .xmp beside ``name`` to trigger the conflict."""
        sidecar = root / (os.path.splitext(name)[0] + ".xmp")
        sidecar.write_text(
            '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
            'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            "</rdf:RDF></x:xmpmeta>",
            encoding="utf-8",
        )
        return sidecar

    def test_conflicted_file_is_not_embedded(self, jpeg_dir):
        root, names = jpeg_dir
        target_name = names[0]
        target = root / target_name
        self._external_sidecar(root, target_name)
        before = target.read_bytes()

        res = write_xmp_metadata(
            str(root), [_entry(target_name)], embed_jpeg=True
        )

        # The sidecar was refused...
        assert os.path.splitext(target_name)[0] + ".xmp" in res["skipped_conflicts"]
        assert res["written"] == 0
        # ...so the original must be byte-for-byte untouched, not just
        # unrecompressed. Nothing may be written without consent.
        assert target.read_bytes() == before
        assert res["embedded"] == 0

    def test_non_conflicted_files_still_embed(self, jpeg_dir):
        """The gate must not become a blanket veto: only the conflicted file is
        skipped, its neighbours are still written."""
        root, names = jpeg_dir
        if len(names) < 2:
            pytest.skip("need at least 2 JPEG fixtures")
        conflicted, clean = names[0], names[1]
        self._external_sidecar(root, conflicted)

        res = write_xmp_metadata(
            str(root), [_entry(conflicted), _entry(clean)], embed_jpeg=True
        )

        assert res["embedded"] == 1
        assert res["written"] == 1
        assert _read_xmp(root / clean).get("Xmp.xmp.Rating") == "4"

    def test_confirmed_overwrite_embeds_once(self, jpeg_dir):
        """With overwrite_external=True the file is written — and the embed
        happens on that pass, not twice."""
        root, names = jpeg_dir
        target_name = names[0]
        self._external_sidecar(root, target_name)

        res = write_xmp_metadata(
            str(root),
            [_entry(target_name)],
            embed_jpeg=True,
            overwrite_external=True,
        )

        assert res["skipped_conflicts"] == []
        assert res["embedded"] == 1
        assert res["written"] == 1
        assert _read_xmp(root / target_name).get("Xmp.xmp.Rating") == "4"


class TestEmbedNonJpegAndSafety:
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
        xmp = _read_xmp(outside)
        assert xmp.get("Xmp.xmp.Rating") != "5"
