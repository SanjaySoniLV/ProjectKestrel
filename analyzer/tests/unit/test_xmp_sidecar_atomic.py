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
