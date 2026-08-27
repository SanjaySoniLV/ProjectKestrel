"""Regression tests for atomic backup restore.

``restore_kestrel_db_backup`` used ``shutil.copy2`` to overwrite the live
kestrel_database.csv / kestrel_scenedata.json in place. An interrupted restore
(this runs right after a risky reject-and-move) could leave the live file
half-written and corrupt. It now copies through a temp file + os.replace via
``copy_file_atomic``.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge
import kestrel_analyzer.database as _dbmod
from kestrel_analyzer.database import copy_file_atomic

pytestmark = pytest.mark.unit


def test_copy_file_atomic_is_byte_exact_and_leaves_no_temp(tmp_path):
    # Include a UTF-8 BOM to prove the raw bytes (encoding) are preserved.
    src = tmp_path / "src.csv"
    src.write_bytes(b"\xef\xbb\xbffilename,rating\nA.CR3,5\n")
    dst = tmp_path / "kestrel_database.csv"
    dst.write_bytes(b"OLD CONTENT")

    copy_file_atomic(str(src), str(dst))

    assert dst.read_bytes() == src.read_bytes()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["kestrel_database.csv", "src.csv"]


def test_existing_dst_survives_a_failed_replace(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.write_bytes(b"NEW")
    dst = tmp_path / "live"
    dst.write_bytes(b"GOOD")

    def boom(*_a, **_k):
        raise OSError("simulated crash during replace")

    monkeypatch.setattr(_dbmod.os, "replace", boom)
    with pytest.raises(OSError):
        copy_file_atomic(str(src), str(dst))

    assert dst.read_bytes() == b"GOOD", "live file must be untouched on failure"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["live", "src"]


def test_missing_source_raises_and_leaves_no_temp(tmp_path):
    """If opening the source fails, the pre-created temp handle must still be
    closed and the .tmp removed. A leaked destination handle would otherwise
    strand the .tmp (and on Windows block later replaces)."""
    src = tmp_path / "does_not_exist"
    dst = tmp_path / "live"
    dst.write_bytes(b"GOOD")

    with pytest.raises((FileNotFoundError, OSError)):
        copy_file_atomic(str(src), str(dst))

    assert dst.read_bytes() == b"GOOD", "live file must be untouched"
    # No stranded temp file -> the destination handle was released so cleanup
    # could remove it.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["live"]


def test_restore_kestrel_db_backup_roundtrips_without_temp_leftovers(tmp_path):
    api = api_bridge.Api()
    kdir = tmp_path / ".kestrel"
    kdir.mkdir()
    (kdir / "kestrel_database.csv").write_text("live-csv", encoding="utf-8")
    (kdir / "kestrel_database_old.csv").write_text("backup-csv", encoding="utf-8")
    (kdir / "kestrel_scenedata.json").write_text("{}", encoding="utf-8")
    (kdir / "kestrel_scenedata_old.json").write_text('{"version": "2.0"}', encoding="utf-8")

    res = api.restore_kestrel_db_backup(str(tmp_path))

    assert res["success"] is True, res
    assert (kdir / "kestrel_database.csv").read_text(encoding="utf-8") == "backup-csv"
    assert (kdir / "kestrel_scenedata.json").read_text(encoding="utf-8") == '{"version": "2.0"}'
    assert [p.name for p in kdir.iterdir() if p.name.endswith(".tmp")] == []
