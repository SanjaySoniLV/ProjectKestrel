"""Multi-pass culling: Api.record_cull_pass / get_cull_history / undo_last_cull_pass.

Drives the same bridge sequence the Culling Assistant runs for each
"Done Culling": write the CSV, ``backup_kestrel_db``, ``move_rejects_to_folder``,
then ``record_cull_pass``. Undo must walk passes back newest first and leave the
folder (files and database) exactly as it was before each pass.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def api():
    return api_bridge.Api()


def _csv(names):
    return "filename,culled\n" + "".join(f"{n},\n" for n in names)


@pytest.fixture
def folder(tmp_path):
    names = [f"IMG_{i:03d}.CR3" for i in range(1, 7)]
    for n in names:
        (tmp_path / n).write_bytes(b"\x00" * 10)
    (tmp_path / "IMG_001.xmp").write_text("<x/>", encoding="utf-8")
    k = tmp_path / ".kestrel"
    k.mkdir()
    (k / "kestrel_database.csv").write_text(_csv(names), encoding="utf-8")
    (k / "kestrel_scenedata.json").write_text(json.dumps({"scenes": {"1": {"image_filenames": names}}}), encoding="utf-8")
    return tmp_path


def _db(folder):
    return (folder / ".kestrel" / "kestrel_database.csv").read_text(encoding="utf-8")


def _cull(api, folder, rejects):
    """One Culling Assistant pass, as culling.html runs it."""
    assert api.backup_kestrel_db(str(folder))["success"]
    res = api.move_rejects_to_folder(str(folder), rejects)
    assert res["all_moved"] is True
    remaining = [l.split(",")[0] for l in _db(folder).splitlines()[1:] if l.split(",")[0] not in rejects]
    (folder / ".kestrel" / "kestrel_database.csv").write_text(_csv(remaining), encoding="utf-8")
    rec = api.record_cull_pass(str(folder), res["moved_requested"])
    assert rec["success"] and rec["recorded"]
    return rec


def test_two_passes_undo_newest_first(api, folder):
    before = _db(folder)
    _cull(api, folder, ["IMG_001.CR3", "IMG_002.CR3"])
    after_first = _db(folder)
    _cull(api, folder, ["IMG_003.CR3"])

    hist = api.get_cull_history(str(folder))
    assert hist["undoable_count"] == 2
    assert [p["file_count"] for p in hist["passes"]] == [2, 1]
    assert hist["next_undo"]["number"] == 2

    r = api.undo_last_cull_pass(str(folder))
    assert r["success"] and r["db_restored"] and r["remaining_passes"] == 1
    assert (folder / "IMG_003.CR3").exists()
    assert not (folder / "IMG_001.CR3").exists()
    assert _db(folder) == after_first

    r = api.undo_last_cull_pass(str(folder))
    assert r["success"] and r["remaining_passes"] == 0
    assert (folder / "IMG_001.CR3").exists() and (folder / "IMG_001.xmp").exists()
    assert _db(folder) == before
    assert not (folder / ".kestrel" / "cull_history.json").exists()
    assert not list((folder / ".kestrel").glob("cullpass_*"))
    # Nothing left: the single-slot backup is moved aside, not left for Undo.
    assert not (folder / ".kestrel" / "kestrel_database_old.csv").exists()
    assert api.get_cull_history(str(folder))["undoable_count"] == 0
    assert api.undo_last_cull_pass(str(folder))["success"] is False


def test_old_slot_tracks_newest_remaining_pass(api, folder):
    """An older build's single-slot Undo must still restore the right state."""
    _cull(api, folder, ["IMG_001.CR3"])
    after_first_backup = (folder / ".kestrel" / "kestrel_database_old.csv").read_text()
    _cull(api, folder, ["IMG_002.CR3"])
    api.undo_last_cull_pass(str(folder))
    assert (folder / ".kestrel" / "kestrel_database_old.csv").read_text() == after_first_backup


def test_pass_with_files_removed_by_hand_is_skipped(api, folder):
    _cull(api, folder, ["IMG_001.CR3"])
    _cull(api, folder, ["IMG_002.CR3"])
    (folder / "_KESTREL_Rejects" / "IMG_002.CR3").unlink()
    hist = api.get_cull_history(str(folder))
    assert hist["passes"][1]["restorable_count"] == 0
    assert hist["undoable_count"] == 1 and hist["next_undo"]["number"] == 1
    r = api.undo_last_cull_pass(str(folder))
    assert r["success"] and (folder / "IMG_001.CR3").exists()


def test_legacy_rejects_become_bottom_pass(api, folder):
    """A folder culled by an older build (rejects, _old slot, no history)."""
    before = _db(folder)
    # Older build: backup + move, no record_cull_pass.
    api.backup_kestrel_db(str(folder))
    api.move_rejects_to_folder(str(folder), ["IMG_001.CR3"])
    (folder / ".kestrel" / "kestrel_database.csv").write_text(_csv([f"IMG_{i:03d}.CR3" for i in range(2, 7)]))
    legacy_hist = api.get_cull_history(str(folder))
    assert legacy_hist["undoable_count"] == 1 and legacy_hist["passes"][0]["legacy"]

    _cull(api, folder, ["IMG_004.CR3"])
    hist = api.get_cull_history(str(folder))
    assert [p["legacy"] for p in hist["passes"]] == [True, False]
    assert hist["passes"][0]["has_db_backup"] is True

    assert api.undo_last_cull_pass(str(folder))["success"]
    assert (folder / "IMG_004.CR3").exists()
    assert api.undo_last_cull_pass(str(folder))["success"]
    assert (folder / "IMG_001.CR3").exists()
    assert _db(folder) == before


def test_legacy_only_folder_undo_matches_old_behaviour(api, folder):
    before = _db(folder)
    api.backup_kestrel_db(str(folder))
    api.move_rejects_to_folder(str(folder), ["IMG_001.CR3", "IMG_002.CR3"])
    (folder / ".kestrel" / "kestrel_database.csv").write_text(_csv([]))
    r = api.undo_last_cull_pass(str(folder))
    assert r["success"] and r["pass_id"] == "legacy" and r["db_restored"]
    assert (folder / "IMG_002.CR3").exists()
    assert _db(folder) == before


def test_history_is_capped(api, folder, monkeypatch):
    monkeypatch.setattr(api_bridge.Api, "_CULL_HISTORY_MAX_PASSES", 2)
    for n in ["IMG_001.CR3", "IMG_002.CR3", "IMG_003.CR3"]:
        _cull(api, folder, [n])
    hist = api.get_cull_history(str(folder))
    assert len(hist["passes"]) == 2
    assert len(list((folder / ".kestrel").glob("cullpass_*_kestrel_database.csv"))) == 2


def test_record_rejects_traversal_names(api, folder):
    assert api.record_cull_pass(str(folder), ["../evil.CR3"])["recorded"] is False


def test_corrupt_history_is_ignored(api, folder):
    _cull(api, folder, ["IMG_001.CR3"])
    (folder / ".kestrel" / "cull_history.json").write_text("{not json")
    hist = api.get_cull_history(str(folder))
    assert hist["success"] and hist["passes"][0]["legacy"]
