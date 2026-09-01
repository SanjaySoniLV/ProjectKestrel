"""Unit tests for the folder-repair bridge methods.

Covers Api.compute_folder_diff, repair_forget_missing, repair_apply_renames and
repair_relocate. No models required.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge

pytestmark = pytest.mark.unit


CSV_HEADER = "filename,species,scene_count,crop_path,export_path,crops_json,file_size"


@pytest.fixture
def api():
    return api_bridge.Api()


def _make_folder(root: Path, rows, with_assets: bool = True):
    """Build a photo folder with a .kestrel database and matching crop/export files.

    ``rows`` is ``[(filename, size)]``. Each row gets a crop and an export JPEG
    at the relative paths the pipeline would have written.
    """
    root.mkdir(parents=True, exist_ok=True)
    kdir = root / ".kestrel"
    (kdir / "crop").mkdir(parents=True, exist_ok=True)
    (kdir / "export").mkdir(parents=True, exist_ok=True)

    lines = [CSV_HEADER]
    for i, (name, size) in enumerate(rows):
        (root / name).write_bytes(b"\0" * size)
        stem = os.path.splitext(name)[0]
        crop_rel = f".kestrel/crop/{stem}_crop_0.jpg"
        export_rel = f".kestrel/export/{stem}_export.jpg"
        if with_assets:
            (root / crop_rel).write_bytes(b"CROP")
            (root / export_rel).write_bytes(b"EXPORT")
        crops_json = json.dumps([{"crop_path": crop_rel}]).replace('"', '""')
        lines.append(
            f'{name},Unknown,{i},{crop_rel},{export_rel},"{crops_json}",{size}'
        )
    (kdir / "kestrel_database.csv").write_text("\n".join(lines), encoding="utf-8")

    scenedata = {
        "version": "2.0",
        "image_ratings": {name: 4 for name, _ in rows},
        "scenes": {
            str(i): {
                "scene_id": str(i),
                "image_filenames": [name],
                "name": f"Scene {i}",
                "status": "pending",
                "user_tags": {"species": [], "families": [], "finalized": False},
            }
            for i, (name, _) in enumerate(rows)
        },
    }
    (kdir / "kestrel_scenedata.json").write_text(json.dumps(scenedata), encoding="utf-8")
    return root


def _read_db(root: Path):
    import pandas as pd
    return pd.read_csv(root / ".kestrel" / "kestrel_database.csv")


def _read_scenedata(root: Path) -> dict:
    return json.loads((root / ".kestrel" / "kestrel_scenedata.json").read_text(encoding="utf-8"))


class TestComputeFolderDiff:
    def test_reports_missing_and_new(self, api, tmp_path):
        root = _make_folder(tmp_path / "shoot", [("IMG_001.CR3", 100), ("IMG_002.CR3", 200)])
        (root / "IMG_002.CR3").unlink()
        (root / "IMG_003.CR3").write_bytes(b"\0" * 300)

        res = api.compute_folder_diff(str(root))
        assert res["success"] is True
        assert res["diff"]["scan_status"] == "ok"
        assert res["diff"]["missing"] == ["IMG_002.CR3"]
        assert res["diff"]["new"] == ["IMG_003.CR3"]

    def test_rejects_invalid_path(self, api):
        res = api.compute_folder_diff("")
        assert res["success"] is False


class TestForgetMissing:
    def test_removes_rows_and_reclaims_assets(self, api, tmp_path):
        root = _make_folder(tmp_path / "shoot", [("IMG_001.CR3", 100), ("IMG_002.CR3", 200)])
        (root / "IMG_002.CR3").unlink()

        res = api.repair_forget_missing(str(root), ["IMG_002.CR3"])
        assert res["success"] is True
        assert res["removed"] == 1
        # Crop + export + the crops_json entry (same file, removed once).
        assert res["freed_files"] >= 2

        remaining = _read_db(root)
        assert list(remaining["filename"]) == ["IMG_001.CR3"]
        assert not (root / ".kestrel/crop/IMG_002_crop_0.jpg").exists()
        assert not (root / ".kestrel/export/IMG_002_export.jpg").exists()
        # The surviving row's assets are untouched.
        assert (root / ".kestrel/crop/IMG_001_crop_0.jpg").exists()

    def test_prunes_scenedata(self, api, tmp_path):
        root = _make_folder(tmp_path / "shoot", [("IMG_001.CR3", 100), ("IMG_002.CR3", 200)])
        api.repair_forget_missing(str(root), ["IMG_002.CR3"])

        sd = _read_scenedata(root)
        assert "IMG_002.CR3" not in sd["image_ratings"]
        assert "IMG_001.CR3" in sd["image_ratings"]
        for scene in sd["scenes"].values():
            assert "IMG_002.CR3" not in scene["image_filenames"]

    def test_emptied_scene_is_removed(self, api, tmp_path):
        """A scene whose every photo is gone must not linger in the timeline."""
        root = _make_folder(tmp_path / "shoot", [("IMG_001.CR3", 100), ("IMG_002.CR3", 200)])
        # _make_folder puts each photo in its own scene, so removing IMG_002
        # empties scene "1" entirely.
        api.repair_forget_missing(str(root), ["IMG_002.CR3"])

        sd = _read_scenedata(root)
        assert "1" not in sd["scenes"]
        assert sd["scenes"]["0"]["image_filenames"] == ["IMG_001.CR3"]

    def test_writes_a_repair_backup_not_the_cull_undo_slot(self, api, tmp_path):
        """The reject-move undo point must survive a repair.

        get_reject_restore_state offers "Undo Move" purely on the existence of
        kestrel_database_old.csv, and restore_kestrel_db_backup restores from it
        unconditionally. If a repair wrote there, undoing a culling move would
        silently restore the pre-repair database instead.
        """
        root = _make_folder(tmp_path / "shoot", [("IMG_001.CR3", 100), ("IMG_002.CR3", 200)])
        api.repair_forget_missing(str(root), ["IMG_002.CR3"])

        kdir = root / ".kestrel"
        assert not (kdir / "kestrel_database_old.csv").exists()
        backups = list(kdir.glob("prerepair_kestrel_database_*.csv"))
        assert len(backups) == 1
        # The backup holds the pre-repair state.
        assert "IMG_002.CR3" in backups[0].read_text(encoding="utf-8")

    def test_rejects_path_traversal_in_crop_path(self, api, tmp_path):
        """A hand-edited crop_path must not delete anything outside the root."""
        root = _make_folder(tmp_path / "shoot", [("IMG_001.CR3", 100)])
        outsider = tmp_path / "precious.jpg"
        outsider.write_bytes(b"KEEP")

        kdir = root / ".kestrel"
        (kdir / "kestrel_database.csv").write_text(
            f"{CSV_HEADER}\n"
            f'IMG_001.CR3,Unknown,0,../precious.jpg,../precious.jpg,[],100',
            encoding="utf-8",
        )
        api.repair_forget_missing(str(root), ["IMG_001.CR3"])
        assert outsider.exists(), "repair escaped the folder root"


class TestApplyRenames:
    def test_rekeys_rows_and_scenedata(self, api, tmp_path):
        root = _make_folder(tmp_path / "shoot", [("IMG_001.CR3", 100)])
        (root / "IMG_001.CR3").rename(root / "Kingfisher.CR3")

        res = api.repair_apply_renames(
            str(root), [{"from": "IMG_001.CR3", "to": "Kingfisher.CR3"}]
        )
        assert res["success"] is True
        assert res["renamed"] == 1

        assert list(_read_db(root)["filename"]) == ["Kingfisher.CR3"]
        sd = _read_scenedata(root)
        assert sd["image_ratings"] == {"Kingfisher.CR3": 4}
        assert sd["scenes"]["0"]["image_filenames"] == ["Kingfisher.CR3"]

    def test_refuses_rename_onto_an_existing_row(self, api, tmp_path):
        """Renaming onto a name already in the database would merge two photos."""
        root = _make_folder(tmp_path / "shoot", [("IMG_001.CR3", 100), ("IMG_002.CR3", 200)])

        res = api.repair_apply_renames(
            str(root), [{"from": "IMG_001.CR3", "to": "IMG_002.CR3"}]
        )
        assert res["success"] is True
        assert res["renamed"] == 0
        assert sorted(_read_db(root)["filename"]) == ["IMG_001.CR3", "IMG_002.CR3"]

    def test_rejects_path_components_in_names(self, api, tmp_path):
        root = _make_folder(tmp_path / "shoot", [("IMG_001.CR3", 100)])
        res = api.repair_apply_renames(
            str(root), [{"from": "IMG_001.CR3", "to": "../evil.CR3"}]
        )
        assert res["renamed"] == 0


class TestRelocate:
    def test_moves_rows_assets_and_scenedata(self, api, tmp_path):
        src = _make_folder(
            tmp_path / "A", [("IMG_001.CR3", 100), ("IMG_002.CR3", 200), ("IMG_003.CR3", 300)]
        )
        dest = tmp_path / "B"
        dest.mkdir()
        # The user moved two photos to B.
        for name in ("IMG_002.CR3", "IMG_003.CR3"):
            (src / name).rename(dest / name)

        res = api.repair_relocate(str(src), str(dest))
        assert res["success"] is True, res.get("error")
        assert res["moved"] == 2

        # Source keeps only the photo still in it.
        assert list(_read_db(src)["filename"]) == ["IMG_001.CR3"]
        # Destination has the other two.
        assert sorted(_read_db(dest)["filename"]) == ["IMG_002.CR3", "IMG_003.CR3"]

        # Crops and exports travelled. Without these the destination renders
        # broken thumbnails even though its CSV looks correct.
        for name in ("IMG_002", "IMG_003"):
            assert (dest / f".kestrel/crop/{name}_crop_0.jpg").exists()
            assert (dest / f".kestrel/export/{name}_export.jpg").exists()
            assert not (src / f".kestrel/crop/{name}_crop_0.jpg").exists()
        assert (src / ".kestrel/crop/IMG_001_crop_0.jpg").exists()

    def test_relative_asset_paths_resolve_at_destination(self, api, tmp_path):
        """crop_path is stored relative to the root, so it needs no rewriting."""
        src = _make_folder(tmp_path / "A", [("IMG_001.CR3", 100)])
        dest = tmp_path / "B"
        dest.mkdir()
        (src / "IMG_001.CR3").rename(dest / "IMG_001.CR3")

        api.repair_relocate(str(src), str(dest))

        row = _read_db(dest).iloc[0]
        assert (dest / row["crop_path"]).exists()
        assert (dest / row["export_path"]).exists()

    def test_scenedata_follows_the_photos(self, api, tmp_path):
        src = _make_folder(tmp_path / "A", [("IMG_001.CR3", 100), ("IMG_002.CR3", 200)])
        dest = tmp_path / "B"
        dest.mkdir()
        (src / "IMG_002.CR3").rename(dest / "IMG_002.CR3")

        api.repair_relocate(str(src), str(dest))

        src_sd = _read_scenedata(src)
        dest_sd = _read_scenedata(dest)
        assert "IMG_002.CR3" not in src_sd["image_ratings"]
        assert dest_sd["image_ratings"] == {"IMG_002.CR3": 4}
        assert dest_sd["scenes"]["1"]["image_filenames"] == ["IMG_002.CR3"]
        # The source must not keep a memberless scene behind.
        assert "1" not in src_sd["scenes"]
        assert src_sd["scenes"]["0"]["image_filenames"] == ["IMG_001.CR3"]

    def test_refuses_destination_that_already_has_kestrel(self, api, tmp_path):
        src = _make_folder(tmp_path / "A", [("IMG_001.CR3", 100)])
        dest = _make_folder(tmp_path / "B", [("IMG_009.CR3", 900)])
        (src / "IMG_001.CR3").rename(dest / "IMG_001.CR3")

        res = api.repair_relocate(str(src), str(dest))
        assert res["success"] is False
        assert res.get("reason") == "destination_has_kestrel"
        # Nothing moved.
        assert list(_read_db(src)["filename"]) == ["IMG_001.CR3"]

    def test_skips_rows_whose_size_disagrees(self, api, tmp_path):
        """A same-named but different-sized file at the destination is a
        different photo, and its analysis data must not follow the name."""
        src = _make_folder(tmp_path / "A", [("IMG_001.CR3", 100)])
        dest = tmp_path / "B"
        dest.mkdir()
        (src / "IMG_001.CR3").unlink()
        (dest / "IMG_001.CR3").write_bytes(b"\0" * 999)

        res = api.repair_relocate(str(src), str(dest))
        assert res["success"] is True
        assert res["moved"] == 0
        assert list(_read_db(src)["filename"]) == ["IMG_001.CR3"]

    def test_refuses_same_folder(self, api, tmp_path):
        src = _make_folder(tmp_path / "A", [("IMG_001.CR3", 100)])
        res = api.repair_relocate(str(src), str(src))
        assert res["success"] is False

    def test_subset_via_filenames(self, api, tmp_path):
        src = _make_folder(tmp_path / "A", [("IMG_001.CR3", 100), ("IMG_002.CR3", 200)])
        dest = tmp_path / "B"
        dest.mkdir()
        for name in ("IMG_001.CR3", "IMG_002.CR3"):
            (src / name).rename(dest / name)

        res = api.repair_relocate(str(src), str(dest), ["IMG_002.CR3"])
        assert res["moved"] == 1
        assert list(_read_db(src)["filename"]) == ["IMG_001.CR3"]
        assert list(_read_db(dest)["filename"]) == ["IMG_002.CR3"]
