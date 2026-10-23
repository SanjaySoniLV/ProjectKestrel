"""Regression tests: a UI save must not delete images it never loaded.

``write_kestrel_csv`` and ``write_kestrel_scenedata`` replaced their files with
whatever the frontend handed them. The frontend builds both payloads from its
in-memory row set, and analysis appends a row to the CSV after every image, so a
user who opened a folder and started culling while it was still analysing held a
snapshot that stopped at whatever had been analysed when the view loaded.
Saving that snapshot erased every image analysed afterwards -- the tail of the
run -- from the database and from scene membership, while leaving its crops and
exports on disk. The photos were intact but invisible to the app, and never got
a rating written to their metadata.

Both writers now union on filename with the caller winning -- but only for
photos still in the folder, because the Culling Assistant's reject-and-move
drops moved files from its payload on purpose and relies on this save to remove
their rows.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge

pytestmark = pytest.mark.unit

HEADER = "filename,species,scene_count,quality,culled"


@pytest.fixture
def api():
    return api_bridge.Api()


def _rows_csv(rows):
    """Serialise ``rows`` the way the frontend does (CRLF, no trailing newline)."""
    return "\r\n".join([HEADER] + [",".join(r) for r in rows])


def _touch_photos(root: Path, names):
    root.mkdir(parents=True, exist_ok=True)
    for n in names:
        (root / n).write_bytes(b"raw")


def _make_folder(root: Path, rows):
    _touch_photos(root, [r[0] for r in rows])
    (root / ".kestrel").mkdir(parents=True, exist_ok=True)
    (root / ".kestrel" / "kestrel_database.csv").write_text(
        _rows_csv(rows) + "\r\n", encoding="utf-8-sig"
    )
    return root


def _read_csv_filenames(root: Path):
    import pandas as pd

    df = pd.read_csv(
        root / ".kestrel" / "kestrel_database.csv", dtype=str, keep_default_na=False
    )
    return list(df["filename"])


# --------------------------------------------------------------------------
# kestrel_database.csv
# --------------------------------------------------------------------------


def test_save_from_stale_snapshot_keeps_rows_analysed_since(api, tmp_path):
    """The reported bug: 151 analysed, UI only knew the first 140."""
    analysed = [
        (f"IMG_{i:04d}.CR3", "Cinnamon Teal", str(i // 12), "0.5", "")
        for i in range(151)
    ]
    root = _make_folder(tmp_path / "shoot", analysed)

    # The UI loaded before the last 11 images finished analysing.
    stale = analysed[:140]
    res = api.write_kestrel_csv(str(root), _rows_csv(stale))
    assert res["success"], res

    names = _read_csv_filenames(root)
    assert len(names) == 151, "rows analysed after the view loaded were deleted"
    assert set(names) == {r[0] for r in analysed}


def test_payload_wins_for_rows_it_does_know(api, tmp_path):
    """Preserving the tail must not resurrect stale values for known rows."""
    root = _make_folder(
        tmp_path / "shoot",
        [("A.CR3", "Teal", "0", "0.5", ""), ("B.CR3", "Teal", "0", "0.5", "")],
    )
    api.write_kestrel_csv(
        str(root),
        _rows_csv([("A.CR3", "Teal", "0", "0.5", "reject")]),
    )

    import pandas as pd

    df = pd.read_csv(
        root / ".kestrel" / "kestrel_database.csv", dtype=str, keep_default_na=False
    )
    by_name = {r["filename"]: r for _, r in df.iterrows()}
    assert by_name["A.CR3"]["culled"] == "reject", "caller's edit must win"
    assert by_name["B.CR3"]["culled"] == "", "untouched row preserved as-is"


def test_scene_merge_does_not_resurrect_the_emptied_scene(api, tmp_path):
    """Preservation is keyed on filename, so a reshuffle is not undone.

    Merging scenes moves filenames to another ``scene_count`` and leaves the old
    scene with no members. Every filename is still present, so nothing is
    preserved and the merge stands.
    """
    root = _make_folder(
        tmp_path / "shoot",
        [("A.CR3", "Teal", "0", "0.5", ""), ("B.CR3", "Teal", "1", "0.5", "")],
    )
    api.write_kestrel_csv(
        str(root),
        _rows_csv([("A.CR3", "Teal", "0", "0.5", ""), ("B.CR3", "Teal", "0", "0.5", "")]),
    )

    import pandas as pd

    df = pd.read_csv(
        root / ".kestrel" / "kestrel_database.csv", dtype=str, keep_default_na=False
    )
    assert sorted(df["scene_count"]) == ["0", "0"], "scene merge must survive"
    assert len(df) == 2, "no duplicate rows"


def test_identical_payload_is_written_byte_for_byte(api, tmp_path):
    """Nothing to preserve => the previous write path, unchanged."""
    rows = [("A.CR3", "Teal", "0", "0.5", ""), ("B.CR3", "Teal", "0", "0.5", "")]
    root = _make_folder(tmp_path / "shoot", rows)
    payload = _rows_csv(rows)

    api.write_kestrel_csv(str(root), payload)

    raw = (root / ".kestrel" / "kestrel_database.csv").read_bytes()
    assert raw == b"\xef\xbb\xbf" + payload.encode("utf-8")


def test_quality_values_are_not_reformatted_by_the_merge(api, tmp_path):
    """The merge must not round-trip numbers through float parsing."""
    root = _make_folder(
        tmp_path / "shoot",
        [
            ("A.CR3", "Teal", "0", "0.78000000000000003", ""),
            ("B.CR3", "Teal", "0", "0.9", ""),
        ],
    )
    # Payload omits B, forcing the merge path.
    api.write_kestrel_csv(
        str(root), _rows_csv([("A.CR3", "Teal", "0", "0.78000000000000003", "")])
    )

    text = (root / ".kestrel" / "kestrel_database.csv").read_text(encoding="utf-8-sig")
    assert "0.78000000000000003" in text
    assert "0.9" in text


def test_unparseable_payload_is_written_through(api, tmp_path):
    """The merge has no opinion on a payload it cannot read."""
    root = _make_folder(tmp_path / "shoot", [("A.CR3", "Teal", "0", "0.5", "")])
    api.write_kestrel_csv(str(root), "")
    assert (root / ".kestrel" / "kestrel_database.csv").read_text(
        encoding="utf-8-sig"
    ) == ""


def test_reject_move_removal_is_not_undone(api, tmp_path):
    """Culling Assistant: moved rejects leave the folder, then the payload drops them.

    ``move_rejects_to_folder`` never edits the CSV; the follow-up save is the
    only place those rows are removed. Their photos are gone from the folder,
    so they must not be preserved.
    """
    root = _make_folder(
        tmp_path / "shoot",
        [("A.CR3", "Teal", "0", "0.5", ""), ("B.CR3", "Teal", "0", "0.5", "reject")],
    )
    rejects = root / "_KESTREL_Rejects"
    rejects.mkdir()
    (root / "B.CR3").rename(rejects / "B.CR3")

    api.write_kestrel_csv(str(root), _rows_csv([("A.CR3", "Teal", "0", "0.5", "")]))

    assert _read_csv_filenames(root) == ["A.CR3"], "moved reject was resurrected"


def test_row_with_path_component_is_never_preserved(api, tmp_path):
    root = _make_folder(tmp_path / "shoot", [("A.CR3", "Teal", "0", "0.5", "")])
    csv_path = root / ".kestrel" / "kestrel_database.csv"
    csv_path.write_text(
        _rows_csv([("A.CR3", "Teal", "0", "0.5", ""), ("../x.CR3", "Teal", "0", "0.5", "")]),
        encoding="utf-8-sig",
    )
    (tmp_path / "x.CR3").write_bytes(b"raw")  # exists, but outside the folder

    api.write_kestrel_csv(str(root), _rows_csv([("A.CR3", "Teal", "0", "0.5", "")]))

    assert _read_csv_filenames(root) == ["A.CR3"]


# --------------------------------------------------------------------------
# kestrel_scenedata.json
# --------------------------------------------------------------------------


def _scene(sid, filenames, **over):
    entry = {
        "scene_id": sid,
        "image_filenames": list(filenames),
        "name": "",
        "status": "pending",
        "user_tags": {"species": [], "families": [], "finalized": False},
    }
    entry.update(over)
    return entry


def _write_scenedata(root: Path, sd):
    names = {n for sc in sd.get("scenes", {}).values() for n in sc["image_filenames"]}
    _touch_photos(root, names)
    (root / ".kestrel").mkdir(parents=True, exist_ok=True)
    (root / ".kestrel" / "kestrel_scenedata.json").write_text(
        json.dumps(sd), encoding="utf-8"
    )


def _read_scenedata(root: Path):
    return json.loads(
        (root / ".kestrel" / "kestrel_scenedata.json").read_text(encoding="utf-8")
    )


def test_scenedata_save_keeps_images_the_payload_never_loaded(api, tmp_path):
    root = tmp_path / "shoot"
    _write_scenedata(
        root,
        {
            "version": 1,
            "image_ratings": {"A.CR3": 5, "B.CR3": 3, "C.CR3": 4},
            "scenes": {"0": _scene("0", ["A.CR3", "B.CR3"]), "1": _scene("1", ["C.CR3"])},
        },
    )

    # Stale UI snapshot: only scene 0, and only A within it.
    api.write_kestrel_scenedata(
        str(root),
        {"version": 1, "image_ratings": {"A.CR3": 2}, "scenes": {"0": _scene("0", ["A.CR3"])}},
    )

    out = _read_scenedata(root)
    assert sorted(out["scenes"]["0"]["image_filenames"]) == ["A.CR3", "B.CR3"]
    assert out["scenes"]["1"]["image_filenames"] == ["C.CR3"], "dropped scene restored"
    assert out["image_ratings"]["A.CR3"] == 2, "caller's rating wins"
    assert out["image_ratings"]["B.CR3"] == 3, "unknown image keeps its rating"
    assert out["image_ratings"]["C.CR3"] == 4


def test_scenedata_restored_scene_keeps_its_reviewed_tags(api, tmp_path):
    root = tmp_path / "shoot"
    reviewed = _scene(
        "1",
        ["C.CR3"],
        name="Grebe pond",
        status="accepted",
        user_tags={"species": ["Least Grebe"], "families": [], "finalized": True},
    )
    _write_scenedata(
        root,
        {"version": 1, "image_ratings": {}, "scenes": {"0": _scene("0", ["A.CR3"]), "1": reviewed}},
    )

    api.write_kestrel_scenedata(
        str(root), {"version": 1, "image_ratings": {}, "scenes": {"0": _scene("0", ["A.CR3"])}}
    )

    out = _read_scenedata(root)["scenes"]["1"]
    assert out["user_tags"]["species"] == ["Least Grebe"]
    assert out["user_tags"]["finalized"] is True
    assert out["status"] == "accepted"
    assert out["name"] == "Grebe pond"


def test_scenedata_scene_merge_is_not_undone(api, tmp_path):
    """A moved filename is still present, so its old scene stays deleted."""
    root = tmp_path / "shoot"
    _write_scenedata(
        root,
        {
            "version": 1,
            "image_ratings": {},
            "scenes": {"0": _scene("0", ["A.CR3"]), "1": _scene("1", ["B.CR3"])},
        },
    )

    api.write_kestrel_scenedata(
        str(root),
        {"version": 1, "image_ratings": {}, "scenes": {"0": _scene("0", ["A.CR3", "B.CR3"])}},
    )

    out = _read_scenedata(root)
    assert list(out["scenes"]) == ["0"], "emptied scene must not come back"
    assert sorted(out["scenes"]["0"]["image_filenames"]) == ["A.CR3", "B.CR3"]


def test_scenedata_no_existing_file_is_written_as_given(api, tmp_path):
    root = tmp_path / "shoot"
    (root / ".kestrel").mkdir(parents=True)
    payload = {"version": 1, "image_ratings": {}, "scenes": {"0": _scene("0", ["A.CR3"])}}

    api.write_kestrel_scenedata(str(root), payload)

    assert _read_scenedata(root)["scenes"]["0"]["image_filenames"] == ["A.CR3"]


def test_scenedata_no_duplicate_membership_on_repeated_saves(api, tmp_path):
    root = tmp_path / "shoot"
    _write_scenedata(
        root,
        {"version": 1, "image_ratings": {}, "scenes": {"0": _scene("0", ["A.CR3", "B.CR3"])}},
    )
    stale = {"version": 1, "image_ratings": {}, "scenes": {"0": _scene("0", ["A.CR3"])}}

    api.write_kestrel_scenedata(str(root), json.loads(json.dumps(stale)))
    api.write_kestrel_scenedata(str(root), json.loads(json.dumps(stale)))

    assert _read_scenedata(root)["scenes"]["0"]["image_filenames"].count("B.CR3") == 1


def test_scenedata_moved_reject_is_not_re_added(api, tmp_path):
    root = tmp_path / "shoot"
    _write_scenedata(
        root,
        {
            "version": 1,
            "image_ratings": {"A.CR3": 4, "B.CR3": 1},
            "scenes": {"0": _scene("0", ["A.CR3", "B.CR3"])},
        },
    )
    (root / "B.CR3").unlink()  # moved to _KESTREL_Rejects

    api.write_kestrel_scenedata(
        str(root),
        {"version": 1, "image_ratings": {"A.CR3": 4}, "scenes": {"0": _scene("0", ["A.CR3"])}},
    )

    out = _read_scenedata(root)
    assert out["scenes"]["0"]["image_filenames"] == ["A.CR3"]
    assert "B.CR3" not in out["image_ratings"]
