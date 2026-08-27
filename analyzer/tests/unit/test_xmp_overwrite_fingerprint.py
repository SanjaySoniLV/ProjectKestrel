"""Regression tests for XMP sidecar overwrite protection.

Previously an existing .xmp that merely *contained* the Kestrel namespace was
treated as "ours" and silently overwritten -- so a sidecar Kestrel wrote and the
user then edited in Lightroom/darktable lost those edits with no conflict
reported. Kestrel now records a content hash of each sidecar it writes and only
overwrites without confirmation when the file is unchanged since (hash match).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import metadata_writer as mw

pytestmark = pytest.mark.unit


def _entry():
    return {
        'filename': 'A.CR3',
        'rating': 4,
        'culled': 'accept',
        'culled_origin': 'manual',
        'species': 'Chipping Sparrow',
        'family': 'Sparrow sp.',
        'quality': 0.5,
    }


def _write(root):
    (root / "A.CR3").write_bytes(b"raw-placeholder")
    return mw.write_xmp_metadata(str(root), [_entry()])


def test_unchanged_kestrel_sidecar_is_overwritten_silently(tmp_path):
    r1 = _write(tmp_path)
    assert r1['success'] and r1['written'] == 1
    xmp = tmp_path / "A.xmp"
    assert xmp.exists()
    assert (tmp_path / ".kestrel" / "xmp_fingerprints.json").exists()

    # Nothing touched the file -> second write overwrites it, no conflict.
    r2 = mw.write_xmp_metadata(str(tmp_path), [_entry()])
    assert r2['written'] == 1
    assert r2['skipped_conflicts'] == []


def test_externally_edited_kestrel_sidecar_is_not_clobbered(tmp_path):
    _write(tmp_path)
    xmp = tmp_path / "A.xmp"

    # Simulate an external edit that keeps the Kestrel namespace (so the old
    # substring check would still call it "ours").
    edited = xmp.read_text(encoding='utf-8') + "\n<!-- edited in Lightroom -->\n"
    xmp.write_text(edited, encoding='utf-8')

    r = mw.write_xmp_metadata(str(tmp_path), [_entry()])  # overwrite_external=False
    assert r['written'] == 0
    assert 'A.xmp' in r['skipped_conflicts']
    # The user's edit must survive untouched.
    assert xmp.read_text(encoding='utf-8') == edited


def test_overwrite_external_true_forces_write(tmp_path):
    _write(tmp_path)
    xmp = tmp_path / "A.xmp"
    xmp.write_text(xmp.read_text(encoding='utf-8') + "\n<!-- edited -->\n", encoding='utf-8')

    r = mw.write_xmp_metadata(str(tmp_path), [_entry()], overwrite_external=True)
    assert r['written'] == 1
    assert 'edited' not in xmp.read_text(encoding='utf-8')


def test_legacy_kestrel_sidecar_without_fingerprint_is_overwritten(tmp_path):
    """A Kestrel sidecar written before fingerprinting (no recorded hash) keeps
    the prior behavior: treated as ours and overwritten, not flagged."""
    _write(tmp_path)
    # Drop the fingerprint store to emulate a pre-fingerprint sidecar.
    (tmp_path / ".kestrel" / "xmp_fingerprints.json").unlink()

    r = mw.write_xmp_metadata(str(tmp_path), [_entry()])
    assert r['written'] == 1
    assert r['skipped_conflicts'] == []


def test_non_kestrel_external_xmp_is_skipped(tmp_path):
    (tmp_path / "A.CR3").write_bytes(b"raw")
    xmp = tmp_path / "A.xmp"
    xmp.write_text("<x:xmpmeta xmlns:x='adobe:ns:meta/'></x:xmpmeta>", encoding='utf-8')

    r = mw.write_xmp_metadata(str(tmp_path), [_entry()])
    assert r['written'] == 0
    assert 'A.xmp' in r['skipped_conflicts']


def test_load_fingerprints_drops_non_string_values(tmp_path):
    """A store with null/non-string values (older or corrupt) must not feed
    ambiguous entries downstream; only hex-string fingerprints are kept."""
    import json

    kdir = tmp_path / ".kestrel"
    kdir.mkdir()
    (kdir / "xmp_fingerprints.json").write_text(
        json.dumps({
            "good.xmp": "abc123",
            "null.xmp": None,
            "num.xmp": 42,
            "list.xmp": ["x"],
        }),
        encoding='utf-8',
    )

    fps = mw._load_xmp_fingerprints(str(tmp_path))
    assert fps == {"good.xmp": "abc123"}


def test_failed_post_write_hash_is_not_persisted_as_null(tmp_path, monkeypatch):
    """If hashing the just-written sidecar fails, we must not persist a null
    fingerprint (which reads back as "no fingerprint" and silently re-enables
    overwrites). The stale entry is dropped instead, and the store never
    contains a null value."""
    import json

    # First write records a real fingerprint for A.xmp.
    _write(tmp_path)
    store = tmp_path / ".kestrel" / "xmp_fingerprints.json"
    fp_key = next(iter(json.loads(store.read_text(encoding='utf-8'))))

    # Second write, but hashing fails (e.g. transient I/O). With _file_sha256
    # returning None the pre-write conflict check can't confirm the file is
    # unchanged, so force the write through with overwrite_external=True; the
    # point under test is what the *post-write* None does to the store.
    monkeypatch.setattr(mw, "_file_sha256", lambda _p: None)
    r = mw.write_xmp_metadata(str(tmp_path), [_entry()], overwrite_external=True)
    assert r['written'] == 1

    persisted = json.loads(store.read_text(encoding='utf-8'))
    # No null values anywhere, and the stale entry was dropped rather than
    # overwritten with null.
    assert None not in persisted.values()
    assert fp_key not in persisted
