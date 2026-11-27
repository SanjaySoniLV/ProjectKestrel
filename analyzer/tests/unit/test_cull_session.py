"""Cull mode passes: Api.get/save_cull_session, undo_cull_move, revert_cull_session.

Each finished pass is driven the way js/cull-flow.js runs it: write the CSV with
the pass's decisions, ``backup_kestrel_db``, ``move_rejects_to_folder``, drop
the moved rows, ``record_cull_pass``, then save the session with the pass
record. Revert and Reset must move later passes' photos back and leave the
decision columns as the target pass ended them, keeping favorites.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge  # noqa: E402
import cull_session  # noqa: E402

pytestmark = pytest.mark.unit

NAMES = [f"IMG_{i:03d}.CR3" for i in range(1, 7)]
FIELDS = ["filename", "scene_count", "quality", "culled", "culled_origin", "favorite"]


@pytest.fixture
def api():
    return api_bridge.Api()


def _write_db(folder, rows):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in FIELDS})
    (folder / ".kestrel" / "kestrel_database.csv").write_text(buf.getvalue(), encoding="utf-8")


def _db(folder):
    text = (folder / ".kestrel" / "kestrel_database.csv").read_text(encoding="utf-8")
    return {r["filename"]: r for r in csv.DictReader(io.StringIO(text))}


@pytest.fixture
def folder(tmp_path):
    for n in NAMES:
        (tmp_path / n).write_bytes(b"\x00" * 10)
    (tmp_path / ".kestrel").mkdir()
    _write_db(tmp_path, [{"filename": n, "scene_count": "1", "quality": str(0.9 - i * 0.1)} for i, n in enumerate(NAMES)])
    (tmp_path / ".kestrel" / "kestrel_scenedata.json").write_text(
        json.dumps({"scenes": {"1": {"image_filenames": NAMES}}}), encoding="utf-8")
    return tmp_path


def _finish_pass(api, folder, accepts, move=True, session=None):
    """Confirm every in-scope photo, then finish the pass as Cull mode does."""
    rows = _db(folder)
    for fn, r in rows.items():
        if r["culled"] and fn not in accepts and r["culled"] == "reject" and r["culled_origin"] == "verified":
            continue  # rejected in an earlier pass and left in place
        r["culled"] = "accept" if fn in accepts else "reject"
        r["culled_origin"] = "verified"
    _write_db(folder, rows.values())
    decisions = {fn: r["culled"] for fn, r in rows.items()}
    session = session or {"passes": []}
    record = {"decisions": decisions, "accepted": len(accepts), "rejected": len(rows) - len(accepts),
              "rules": {"threshold": 0.5}}
    if move:
        rejects = [fn for fn, r in rows.items() if r["culled"] == "reject"]
        assert api.backup_kestrel_db(str(folder))["success"]
        res = api.move_rejects_to_folder(str(folder), rejects)
        moved = res["moved_requested"]
        _write_db(folder, [r for fn, r in rows.items() if fn not in moved])
        rec = api.record_cull_pass(str(folder), moved)
        record.update(moved=len(moved), history_id=rec["pass_id"],
                      moved_rows=[{"filename": fn, "favorite": rows[fn]["favorite"]} for fn in moved])
    session["passes"].append(record)
    session["ended"] = True
    assert api.save_cull_session(str(folder), session)["success"]
    return api.get_cull_session(str(folder))["session"]


def test_session_round_trip_is_sanitized(api, folder):
    assert api.get_cull_session(str(folder))["session"] is None
    bad = {"passes": [{"decisions": {"../x": "accept", "IMG_001.CR3": "accept", "IMG_002.CR3": "nope"},
                       "history_id": "a/b"}],
           "confirmed": ["1", "../2"], "rules": {"threshold": 7, "no_bird": "maybe"}}
    assert api.save_cull_session(str(folder), bad)["success"]
    s = api.get_cull_session(str(folder))["session"]
    assert s["passes"][0]["decisions"] == {"IMG_001.CR3": "accept"}
    assert s["passes"][0]["history_id"] == ""
    assert s["confirmed"] == ["1"]
    assert s["rules"] == {"threshold": 1.0, "min_per_scene": 1, "max_per_scene": 0, "no_bird": "reject"}
    assert s["current"] == 2 and s["passes"][0]["on_disk"] is True


def test_revert_to_pass_one_moves_pass_two_back_and_restores_decisions(api, folder):
    s = _finish_pass(api, folder, accepts=NAMES[:4])
    assert s["passes"][0]["on_disk"] is True
    # Pass 2: a favorite, then a stricter cut; IMG_003/004 are rejected and moved.
    rows = _db(folder)
    rows["IMG_001.CR3"]["favorite"] = "1"
    _write_db(folder, rows.values())
    raw = json.loads((folder / ".kestrel" / "cull_session.json").read_text())
    raw["ended"] = False
    s = _finish_pass(api, folder, accepts=NAMES[:2], session=raw)
    assert not (folder / "IMG_003.CR3").exists()

    res = api.revert_cull_session(str(folder), 1)
    assert res["success"], res
    assert (folder / "IMG_003.CR3").exists() and (folder / "IMG_004.CR3").exists()
    assert not (folder / "IMG_005.CR3").exists()  # pass 1's own rejects stay moved
    db = _db(folder)
    assert set(db) == set(NAMES[:4])
    assert all(db[n]["culled"] == "accept" for n in NAMES[:4])
    assert db["IMG_001.CR3"]["favorite"] == "1"
    s = api.get_cull_session(str(folder))["session"]
    assert len(s["passes"]) == 1 and s["ended"] and s["current"] == 1


def test_reset_moves_everything_back_and_clears_decisions(api, folder):
    _finish_pass(api, folder, accepts=NAMES[:3])
    rows = _db(folder)
    rows["IMG_002.CR3"]["favorite"] = "1"
    _write_db(folder, rows.values())

    res = api.revert_cull_session(str(folder), 0)
    assert res["success"], res
    assert all((folder / n).exists() for n in NAMES)
    db = _db(folder)
    assert set(db) == set(NAMES)
    assert all(r["culled"] == "" and r["culled_origin"] == "" for r in db.values())
    assert db["IMG_002.CR3"]["favorite"] == "1"
    assert not (folder / ".kestrel" / "cull_session.json").exists()


def test_revert_not_offered_once_a_photo_is_gone(api, folder):
    _finish_pass(api, folder, accepts=NAMES[:3])
    (folder / "_KESTREL_Rejects" / "IMG_006.CR3").unlink()
    s = api.get_cull_session(str(folder))["session"]
    assert s["passes"][0]["on_disk"] is False and s["passes"][0]["missing"] == 1


def test_undo_move_keeps_current_decisions(api, folder):
    _finish_pass(api, folder, accepts=NAMES[:3])
    # Pass 2 in progress: the user flips IMG_002 to reject.
    rows = _db(folder)
    rows["IMG_002.CR3"].update(culled="reject", culled_origin="manual")
    _write_db(folder, rows.values())

    res = api.undo_cull_move(str(folder))
    assert res["success"], res
    assert all((folder / n).exists() for n in NAMES)
    db = _db(folder)
    assert db["IMG_002.CR3"]["culled"] == "reject" and db["IMG_002.CR3"]["culled_origin"] == "manual"
    assert db["IMG_005.CR3"]["culled"] == "reject"
    s = api.get_cull_session(str(folder))["session"]
    assert s["passes"][0]["move_undone"] is True
    assert api.undo_cull_move(str(folder))["success"] is False


def test_bad_pass_number_is_refused(api, folder):
    _finish_pass(api, folder, accepts=NAMES[:3], move=False)
    assert api.revert_cull_session(str(folder), 5)["success"] is False
    assert api.revert_cull_session(str(folder), "x")["success"] is False


def test_decision_codes_map_to_columns():
    assert cull_session.decision_columns("accept") == {"culled": "accept", "culled_origin": "verified"}
    assert cull_session.decision_columns("reject:manual") == {"culled": "reject", "culled_origin": "manual"}
