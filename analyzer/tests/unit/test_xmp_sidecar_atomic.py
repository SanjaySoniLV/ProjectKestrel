"""S0-06: XMP sidecar writes must not truncate an existing .xmp in place.

``write_xmp_metadata`` used ``open(xmp_path, 'w')``, which truncates the
destination before the new packet is written. A crash or a later ``os.replace``
failure then leaves a Kestrel (or Lightroom) sidecar empty or half-written.

The write must go to a unique temp file in the same directory and
``os.replace`` it into place. A failed replace leaves the original bytes
untouched and must not leave ``.xmp.tmp`` orphans.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from metadata_writer import write_xmp_metadata

pytestmark = pytest.mark.unit

_KESTREL_NS = "http://ns.projectkestrel.app/xmp/1.0/"

_ENTRY_R1 = {
    "filename": "IMG_001.CR3",
    "rating": 1,
    "culled": "reject",
    "culled_origin": "manual",
}
_ENTRY_R5 = {
    "filename": "IMG_001.CR3",
    "rating": 5,
    "culled": "accept",
    "culled_origin": "manual",
}


def _xmp_tmp_names(folder: Path) -> list[str]:
    return sorted(
        p.name
        for p in folder.iterdir()
        if p.name.startswith(".kestrel_xmp_") or p.name.endswith(".xmp.tmp")
    )


def _write_kestrel_sidecar(root: Path) -> Path:
    (root / "IMG_001.CR3").touch()
    result = write_xmp_metadata(str(root), [_ENTRY_R1])
    assert result["success"] is True
    assert result["written"] == 1
    xmp_path = root / "IMG_001.xmp"
    content = xmp_path.read_bytes()
    assert _KESTREL_NS.encode("utf-8") in content
    return xmp_path


class TestXmpSidecarAtomicWrite:
    def test_existing_kestrel_sidecar_survives_replace_failure(self, tmp_path, monkeypatch):
        """Failed ``os.replace`` must not have already truncated the live .xmp."""
        xmp_path = _write_kestrel_sidecar(tmp_path)
        original = xmp_path.read_bytes()

        real_replace = os.replace

        def boom(src, dst, *args, **kwargs):
            if os.path.abspath(dst) == os.path.abspath(str(xmp_path)):
                raise OSError("simulated replace failure")
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", boom)

        result = write_xmp_metadata(str(tmp_path), [_ENTRY_R5])

        assert result["success"] is True
        assert result["written"] == 0
        errors = result.get("errors") or []
        assert errors, "replace failure must be recorded per entry, not swallowed"
        assert "IMG_001.CR3" in errors[0]
        assert xmp_path.read_bytes() == original
        assert _xmp_tmp_names(tmp_path) == []

    def test_does_not_open_existing_sidecar_for_write(self, tmp_path, monkeypatch):
        """Acceptance: no ``open(..., 'w')`` on an existing ``.xmp``."""
        xmp_path = _write_kestrel_sidecar(tmp_path)
        dest = os.path.abspath(str(xmp_path))
        write_opens: list[str] = []

        real_open = open

        def tracking_open(file, mode="r", *args, **kwargs):
            try:
                path = os.path.abspath(os.fspath(file))
            except TypeError:
                path = None
            if path == dest and any(flag in str(mode) for flag in "wax+"):
                write_opens.append(str(mode))
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", tracking_open)

        result = write_xmp_metadata(str(tmp_path), [_ENTRY_R5])

        assert result["success"] is True
        assert result["written"] == 1
        assert write_opens == []
        content = xmp_path.read_text(encoding="utf-8")
        assert "xmp:Rating=\"5\"" in content or "<xmp:Rating>5</xmp:Rating>" in content

    def test_successful_overwrite_leaves_no_tmp(self, tmp_path):
        xmp_path = _write_kestrel_sidecar(tmp_path)

        result = write_xmp_metadata(str(tmp_path), [_ENTRY_R5])

        assert result["success"] is True
        assert result["written"] == 1
        content = xmp_path.read_text(encoding="utf-8")
        assert "xmp:Rating=\"5\"" in content or "<xmp:Rating>5</xmp:Rating>" in content
        assert _xmp_tmp_names(tmp_path) == []


class TestSidecarReplaceIsRetried:
    """A sidecar the user's photo editor holds open must not fail the write.

    On Windows ``os.replace`` fails while any other process has a handle on the
    destination. Sidecars are the files most exposed to that: Lightroom, Bridge
    and Capture One watch and hold them by design, which is the whole reason
    they exist. The CSV writer has always retried through this; the sidecar
    writer did not.
    """

    def test_transient_permission_error_is_retried(self, tmp_path, monkeypatch):
        import metadata_writer

        xmp_path = _write_kestrel_sidecar(tmp_path)
        original = xmp_path.read_bytes()

        real_replace = os.replace
        calls = {"n": 0}

        def flaky(src, dst, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError(32, "The process cannot access the file")
            return real_replace(src, dst, *a, **k)

        monkeypatch.setattr(metadata_writer.os, "replace", flaky)

        result = write_xmp_metadata(str(tmp_path), [_ENTRY_R5])

        assert calls["n"] >= 2, "the replace was not retried"
        assert result["written"] == 1
        assert xmp_path.read_bytes() != original, "the retry did not land the new packet"
        assert _xmp_tmp_names(tmp_path) == [], "a retried write left a temp file behind"

    def test_persistent_lock_still_leaves_the_original_intact(self, tmp_path, monkeypatch):
        """Retrying is a mitigation, not a guarantee — the failure path still holds."""
        import metadata_writer

        xmp_path = _write_kestrel_sidecar(tmp_path)
        original = xmp_path.read_bytes()

        def always_locked(_src, _dst, *_a, **_k):
            raise PermissionError(32, "The process cannot access the file")

        monkeypatch.setattr(metadata_writer.os, "replace", always_locked)

        write_xmp_metadata(str(tmp_path), [_ENTRY_R5])

        assert xmp_path.read_bytes() == original
        assert _xmp_tmp_names(tmp_path) == []


class TestSidecarPermissions:
    """mkstemp creates at 0600 and os.replace carries that onto the destination.

    Left alone, every sidecar Kestrel writes becomes owner-only regardless of
    what the previous one allowed — invisible on a single-user Windows box, but
    it silently strips access on a NAS, a shared catalog, or a second account.
    """

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_new_sidecar_is_not_owner_only(self, tmp_path):
        xmp_path = _write_kestrel_sidecar(tmp_path)
        mode = xmp_path.stat().st_mode & 0o777
        assert mode != 0o600, (
            "a new sidecar landed with mkstemp's private mode; other software "
            "reading sidecars on a shared volume would lose access"
        )
        # Whatever the umask allows, the owner must at least be able to read it.
        assert mode & 0o600 == 0o600

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_rewrite_preserves_an_existing_sidecar_mode(self, tmp_path):
        """Rewriting must never narrow access someone deliberately granted."""
        xmp_path = _write_kestrel_sidecar(tmp_path)
        os.chmod(xmp_path, 0o664)

        result = write_xmp_metadata(str(tmp_path), [_ENTRY_R5])
        assert result["written"] == 1

        assert xmp_path.stat().st_mode & 0o777 == 0o664, (
            "the rewrite dropped the group-write bit the previous sidecar had"
        )

    def test_mode_fixup_failure_does_not_fail_the_write(self, tmp_path, monkeypatch):
        """Best-effort: exFAT and some SMB mounts cannot represent these bits."""
        import metadata_writer

        def boom(*_a, **_k):
            raise OSError("chmod unsupported on this filesystem")

        monkeypatch.setattr(metadata_writer.os, "chmod", boom)

        (tmp_path / "IMG_001.CR3").touch()
        result = write_xmp_metadata(str(tmp_path), [_ENTRY_R1])

        assert result["written"] == 1
        assert (tmp_path / "IMG_001.xmp").exists()
        assert _xmp_tmp_names(tmp_path) == []


class TestSidecarModeLogic:
    """The mode decision itself, verifiable on any platform.

    The two tests above assert real POSIX bits and therefore skip on Windows —
    which is the platform this is usually developed on, so without these the
    permission fix would have no executed coverage here at all. These drive
    ``_apply_sidecar_mode`` directly and assert what it asks the OS for.
    """

    def test_existing_destination_mode_is_copied_to_the_temp_file(self, tmp_path, monkeypatch):
        import metadata_writer

        dest = tmp_path / "IMG_001.xmp"
        dest.write_text("<x/>", encoding="utf-8")
        tmp = tmp_path / "tmp.xmp.tmp"
        tmp.write_text("<y/>", encoding="utf-8")

        seen = []
        monkeypatch.setattr(
            metadata_writer.os, "chmod", lambda p, m: seen.append((str(p), m))
        )
        monkeypatch.setattr(
            metadata_writer.os,
            "stat",
            lambda p: type("S", (), {"st_mode": 0o100664})(),
        )

        metadata_writer._apply_sidecar_mode(str(tmp), str(dest))

        assert seen == [(str(tmp), 0o664)], (
            f"expected the destination's permission bits to be applied, got {seen}"
        )

    def test_mtime_is_not_carried_over_from_the_old_sidecar(self, tmp_path, monkeypatch):
        """Only permission bits — copystat would make new content look stale."""
        import metadata_writer

        dest = tmp_path / "IMG_001.xmp"
        dest.write_text("<x/>", encoding="utf-8")
        tmp = tmp_path / "tmp.xmp.tmp"
        tmp.write_text("<y/>", encoding="utf-8")

        called = []
        if hasattr(metadata_writer, "shutil"):
            monkeypatch.setattr(
                metadata_writer.shutil, "copystat", lambda *a, **k: called.append(a)
            )
        monkeypatch.setattr(metadata_writer.os, "chmod", lambda p, m: None)

        old = 1_000_000_000.0
        os.utime(dest, (old, old))
        metadata_writer._apply_sidecar_mode(str(tmp), str(dest))

        assert not called, "copystat was used; the new sidecar would inherit a stale mtime"
        assert tmp.stat().st_mtime != pytest.approx(old, abs=2)

    def test_new_sidecar_asks_for_the_umask_default_not_0600(self, tmp_path, monkeypatch):
        import metadata_writer

        if metadata_writer._DEFAULT_FILE_MODE is None:
            pytest.skip("no umask-derived default on this platform")

        tmp = tmp_path / "tmp.xmp.tmp"
        tmp.write_text("<y/>", encoding="utf-8")

        seen = []
        monkeypatch.setattr(
            metadata_writer.os, "chmod", lambda p, m: seen.append((str(p), m))
        )

        metadata_writer._apply_sidecar_mode(str(tmp), str(tmp_path / "nope.xmp"))

        assert seen, "a brand-new sidecar kept mkstemp's 0600"
        assert seen[0][1] != 0o600
        assert seen[0][1] == metadata_writer._DEFAULT_FILE_MODE

    def test_the_mode_fixup_is_actually_wired_into_the_write(self, tmp_path, monkeypatch):
        """The tests above exercise _apply_sidecar_mode in isolation.

        Without this one, deleting the call from _write_text_atomic would leave
        them all green while every sidecar went back to being owner-only.
        """
        import metadata_writer

        seen = []
        real = metadata_writer._apply_sidecar_mode
        monkeypatch.setattr(
            metadata_writer,
            "_apply_sidecar_mode",
            lambda tmp, dest: (seen.append((tmp, dest)), real(tmp, dest))[1],
        )

        (tmp_path / "IMG_001.CR3").touch()
        result = write_xmp_metadata(str(tmp_path), [_ENTRY_R1])

        assert result["written"] == 1
        assert seen, "_write_text_atomic did not apply the sidecar mode at all"
        assert seen[0][1].endswith("IMG_001.xmp")
