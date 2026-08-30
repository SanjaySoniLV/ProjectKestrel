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
    assert '<!-- edited -->' not in xmp.read_text(encoding='utf-8')


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


def test_load_fingerprints_keeps_only_sha256_hex(tmp_path):
    """A store with null/non-string/non-sha256 values or path-shaped keys
    (older or corrupt) must not feed ambiguous entries downstream; only real
    64-char hex sha256 digests keyed by a bare ``*.xmp`` basename are kept.
    Everything else falls back to legacy handling."""
    import json

    good = "a" * 64  # a valid 64-char lowercase hex digest shape
    kdir = tmp_path / ".kestrel"
    kdir.mkdir()
    (kdir / "xmp_fingerprints.json").write_text(
        json.dumps({
            "good.xmp": good,
            "short.xmp": "abc123",          # too short to be sha256
            "upper.xmp": "A" * 64,          # uppercase -> not hexdigest() output
            "null.xmp": None,
            "num.xmp": 42,
            "list.xmp": ["x"],
            "sub/dir.xmp": good,            # path separator — not a basename
            "../evil.xmp": good,            # traversal-shaped key
            "notxmp.txt": good,             # wrong suffix
        }),
        encoding='utf-8',
    )

    fps = mw._load_xmp_fingerprints(str(tmp_path))
    assert fps == {"good.xmp": good}


def test_corrupt_nonhex_fingerprint_falls_back_to_legacy_not_conflict(tmp_path):
    """A corrupt store holding a non-sha256 string for an existing Kestrel
    sidecar must be ignored (legacy overwrite), NOT routed through the conflict
    path -- otherwise an untouched sidecar would be wrongly flagged."""
    import json

    _write(tmp_path)  # creates A.xmp + a valid fingerprint
    store = tmp_path / ".kestrel" / "xmp_fingerprints.json"
    data = json.loads(store.read_text(encoding='utf-8'))
    key = next(iter(data))
    data[key] = "not-a-real-sha256"  # corrupt the recorded value
    store.write_text(json.dumps(data), encoding='utf-8')

    # The sidecar itself was NOT edited; with the bogus fingerprint dropped it
    # is treated as a legacy Kestrel sidecar and overwritten, not a conflict.
    r = mw.write_xmp_metadata(str(tmp_path), [_entry()])
    assert r['written'] == 1
    assert r['skipped_conflicts'] == []


def test_save_fingerprints_closes_fd_when_fdopen_fails(tmp_path, monkeypatch):
    """If os.fdopen fails, the mkstemp descriptor must be closed (not leaked)
    and the temp file cleaned up, and the best-effort save must swallow the
    error rather than propagate it."""
    import os as _os

    created = {}
    real_mkstemp = mw.tempfile.mkstemp

    def spy_mkstemp(*a, **k):
        fd, path = real_mkstemp(*a, **k)
        created['fd'] = fd
        created['path'] = path
        return fd, path

    closed = []
    real_close = mw.os.close

    def spy_close(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(mw.tempfile, 'mkstemp', spy_mkstemp)
    monkeypatch.setattr(mw.os, 'close', spy_close)
    monkeypatch.setattr(mw.os, 'fdopen',
                        lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))

    # Best-effort: must not raise.
    mw._save_xmp_fingerprints(str(tmp_path), {'A.xmp': 'a' * 64})

    assert created['fd'] in closed, "mkstemp fd was not closed -> leaked"
    assert not _os.path.exists(created['path']), "temp file left behind"
    assert not (tmp_path / '.kestrel' / 'xmp_fingerprints.json').exists()


def test_fingerprint_key_is_stable_across_relative_and_symlinked_roots(tmp_path, monkeypatch):
    """The overwrite protection must hold when the same folder is addressed via
    a relative path or a symlink: an externally edited sidecar must still be
    detected as a conflict, not silently overwritten due to an unstable key."""
    shoot = tmp_path / "shoot"
    shoot.mkdir()
    (shoot / "A.CR3").write_bytes(b"raw-placeholder")

    # First write via an absolute root records the fingerprint.
    assert mw.write_xmp_metadata(str(shoot), [_entry()])['written'] == 1

    # Simulate an external edit that keeps the Kestrel namespace.
    xmp = shoot / "A.xmp"
    edited = xmp.read_text(encoding='utf-8') + "\n<!-- edited elsewhere -->\n"
    xmp.write_text(edited, encoding='utf-8')

    # Address the SAME folder via a relative path (different CWD) ...
    monkeypatch.chdir(tmp_path)
    r_rel = mw.write_xmp_metadata("shoot", [_entry()])
    assert r_rel['written'] == 0
    assert 'A.xmp' in r_rel['skipped_conflicts']
    assert xmp.read_text(encoding='utf-8') == edited

    # ... and via a symlink to it. Both must resolve to the same key and keep
    # protecting the edit. (Symlink support is universal on POSIX; skip if the
    # platform/filesystem can't create one.)
    link = tmp_path / "link_to_shoot"
    try:
        link.symlink_to(shoot, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not supported here")
    r_link = mw.write_xmp_metadata(str(link), [_entry()])
    assert r_link['written'] == 0
    assert 'A.xmp' in r_link['skipped_conflicts']
    assert xmp.read_text(encoding='utf-8') == edited


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
