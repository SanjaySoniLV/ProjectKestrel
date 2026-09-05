"""Atomic backup *create* for kestrel_database.csv / kestrel_scenedata.json.

``backup_kestrel_db`` used ``shutil.copy2`` onto ``kestrel_database_old.csv``
(and the scenedata sibling). An interrupted copy truncates the destination
and can leave a half-written backup; a later restore would then install
garbage. Copy through a temp file + ``os.replace`` via ``copy_file_atomic``.

Restore stays on ``copy2`` (WP-16 out of scope; that is #125).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge
import kestrel_analyzer.database as _dbmod
from kestrel_analyzer.database import copy_file_atomic


pytestmark = pytest.mark.unit


def test_copy_file_atomic_is_byte_exact_and_leaves_no_temp(tmp_path: Path) -> None:
    src = tmp_path / "src.csv"
    src.write_bytes(b"\xef\xbb\xbffilename,rating\nA.CR3,5\n")
    dst = tmp_path / "kestrel_database_old.csv"
    dst.write_bytes(b"OLD CONTENT")

    copy_file_atomic(str(src), str(dst))

    assert dst.read_bytes() == src.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "kestrel_database_old.csv",
        "src.csv",
    ]


def test_copy_file_atomic_existing_dst_survives_failed_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "src"
    src.write_bytes(b"NEW")
    dst = tmp_path / "live"
    dst.write_bytes(b"GOOD")

    def boom(*_a, **_k):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(_dbmod.os, "replace", boom)
    with pytest.raises(OSError):
        copy_file_atomic(str(src), str(dst))

    assert dst.read_bytes() == b"GOOD"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["live", "src"]


def test_backup_kestrel_db_interrupted_replace_keeps_live_and_prior_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arrange: live CSV + complete prior backup. Copy crashes at replace.

    Assert: backup is not half-written; live CSV is untouched.
    """
    api = api_bridge.Api()
    kdir = tmp_path / ".kestrel"
    kdir.mkdir()
    live = kdir / "kestrel_database.csv"
    live.write_bytes(b"LIVE-FULL-CONTENT\n")
    prior = kdir / "kestrel_database_old.csv"
    prior.write_bytes(b"PRIOR-COMPLETE-BACKUP\n")
    (kdir / "kestrel_scenedata.json").write_bytes(b'{"version":"2.0"}\n')

    def boom(*_a, **_k):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(_dbmod.os, "replace", boom)
    res = api.backup_kestrel_db(str(tmp_path))

    assert res["success"] is False
    assert live.read_bytes() == b"LIVE-FULL-CONTENT\n"
    assert prior.read_bytes() == b"PRIOR-COMPLETE-BACKUP\n"
    assert [p.name for p in kdir.iterdir() if p.name.endswith(".tmp")] == []


def test_backup_kestrel_db_writes_complete_csv_and_scenedata(tmp_path: Path) -> None:
    api = api_bridge.Api()
    kdir = tmp_path / ".kestrel"
    kdir.mkdir()
    csv = kdir / "kestrel_database.csv"
    csv.write_text("filename,quality\nIMG_001.CR3,0.5\n", encoding="utf-8")
    scene = kdir / "kestrel_scenedata.json"
    scene.write_text('{"version":"2.0","image_ratings":{},"scenes":{}}\n', encoding="utf-8")

    res = api.backup_kestrel_db(str(tmp_path))

    assert res["success"] is True, res
    assert (kdir / "kestrel_database_old.csv").read_bytes() == csv.read_bytes()
    assert (kdir / "kestrel_scenedata_old.json").read_bytes() == scene.read_bytes()
    assert csv.read_text(encoding="utf-8").startswith("filename,")
    assert [p.name for p in kdir.iterdir() if p.name.endswith(".tmp")] == []
