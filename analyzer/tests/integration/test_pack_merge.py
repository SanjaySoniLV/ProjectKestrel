"""Pack-merge safety tests (Stage 4 — non-destructive merge).

Exercises `cloud_compute_client.merge_pack_into_kestrel` against a local
folder that already has user-edited data, and asserts the user data
survives. Before Stage 4 these scenarios silently clobbered user state:

- CSV: last-write-wins meant a cloud row would overwrite the local
  `culled` / `culled_origin` columns.
- Scenedata: full-replace meant scene names, ratings, and user_tags were
  silently overwritten by every pack merge.
- Artifacts: crops/exports were unconditionally overwritten.

These tests pin the new behaviour: existing local data wins; new
filenames / scenes / artifacts from the pack are added.
"""

from __future__ import annotations

import csv
import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

# conftest.py prepends analyzer/ to sys.path; mirror it just in case.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cloud_compute_client import merge_pack_into_kestrel  # noqa: E402


pytestmark = pytest.mark.integration


# ── Helpers ────────────────────────────────────────────────────────────────


def _write_local_csv(kestrel_dir: Path, rows: list[dict]) -> None:
    """Write a kestrel_database.csv with the given rows. Keeps the test
    decoupled from BASE_COLUMNS additions in the production code."""
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    csv_path = kestrel_dir / "kestrel_database.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def _read_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _build_pack_zip(
    pack_path: Path,
    csv_rows: list[dict],
    scenedata: dict,
    metadata: dict,
    crops: dict[str, bytes] | None = None,
    exports: dict[str, bytes] | None = None,
) -> None:
    """Build a synthetic .kestrel pack zip that mirrors what Modal produces."""
    crops = crops or {}
    exports = exports or {}
    with zipfile.ZipFile(pack_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if csv_rows:
            buf = []
            buf.append(",".join(csv_rows[0].keys()))
            for row in csv_rows:
                buf.append(",".join(str(v) for v in row.values()))
            zf.writestr(".kestrel/kestrel_database.csv", "\n".join(buf) + "\n")
        zf.writestr(".kestrel/kestrel_scenedata.json", json.dumps(scenedata))
        zf.writestr(".kestrel/kestrel_metadata.json", json.dumps(metadata))
        for name, data in crops.items():
            zf.writestr(f".kestrel/crop/{name}", data)
        for name, data in exports.items():
            zf.writestr(f".kestrel/export/{name}", data)


# ── Tests ──────────────────────────────────────────────────────────────────


def test_csv_skip_if_row_exists_locally(tmp_path: Path) -> None:
    """Stage 4A: a local CSV row with `culled=1` survives a pack merge that
    contains the same filename with `culled=0`. New cloud-only filenames
    are appended."""
    folder = tmp_path / "shoot"
    kestrel = folder / ".kestrel"
    kestrel.mkdir(parents=True)

    _write_local_csv(kestrel, [
        {
            "filename": "IMG_001.CR3",
            "species": "American Goldfinch",
            "species_confidence": "0.92",
            "culled": "1",
            "culled_origin": "user",
        },
    ])

    pack = tmp_path / "pack_1.zip"
    _build_pack_zip(
        pack,
        csv_rows=[
            # Cloud overwrite attempt: different species, no cull marker.
            {
                "filename": "IMG_001.CR3",
                "species": "House Finch",        # would clobber local
                "species_confidence": "0.71",
                "culled": "0",                    # would clobber user cull
                "culled_origin": "",
            },
            # New row from cloud — should be appended.
            {
                "filename": "IMG_002.CR3",
                "species": "Northern Cardinal",
                "species_confidence": "0.88",
                "culled": "0",
                "culled_origin": "",
            },
        ],
        scenedata={"version": "2.0", "image_ratings": {}, "scenes": {}},
        metadata={"kestrel_version": "2.0.1"},
    )

    merge_pack_into_kestrel(pack, folder)

    rows = {r["filename"]: r for r in _read_csv(kestrel / "kestrel_database.csv")}
    assert "IMG_001.CR3" in rows
    assert "IMG_002.CR3" in rows
    assert rows["IMG_001.CR3"]["species"] == "American Goldfinch", \
        "local row clobbered by cloud — Stage 4A regression"
    assert rows["IMG_001.CR3"]["culled"] == "1", \
        "user culled flag wiped by cloud merge"
    assert rows["IMG_001.CR3"]["culled_origin"] == "user"
    assert rows["IMG_002.CR3"]["species"] == "Northern Cardinal"


def test_scenedata_additive_preserves_user_edits(tmp_path: Path) -> None:
    """Stage 4B: image_ratings, scene name, status, and user_tags survive
    a pack merge. New scenes and new images-in-scene get added."""
    folder = tmp_path / "shoot"
    kestrel = folder / ".kestrel"
    kestrel.mkdir(parents=True)

    local_scenedata = {
        "version": "2.0",
        "image_ratings": {"IMG_001.CR3": 5},
        "scenes": {
            "1": {
                "scene_id": "1",
                "image_filenames": ["IMG_001.CR3"],
                "name": "Goldfinch in flight",      # USER
                "status": "reviewed",                # USER
                "user_tags": {
                    "species": ["American Goldfinch"],
                    "families": [],
                    "finalized": True,
                },
            },
        },
    }
    (kestrel / "kestrel_scenedata.json").write_text(json.dumps(local_scenedata))

    pack = tmp_path / "pack_1.zip"
    _build_pack_zip(
        pack,
        csv_rows=[],
        scenedata={
            "version": "2.0",
            "image_ratings": {
                "IMG_001.CR3": 0,    # cloud default — must NOT overwrite 5
                "IMG_002.CR3": 0,    # new — should be added
            },
            "scenes": {
                # Same scene_id "1" — local user-edits must win, but new
                # image_filenames in this scene should be unioned.
                "1": {
                    "scene_id": "1",
                    "image_filenames": ["IMG_001.CR3", "IMG_002.CR3"],
                    "name": "Generic scene 1",       # WOULD CLOBBER
                    "status": "pending",              # WOULD CLOBBER
                    "user_tags": {"species": [], "families": [], "finalized": False},
                },
                # New scene_id — should be added wholesale.
                "2": {
                    "scene_id": "2",
                    "image_filenames": ["IMG_003.CR3"],
                    "name": "",
                    "status": "pending",
                    "user_tags": {"species": [], "families": [], "finalized": False},
                },
            },
        },
        metadata={"kestrel_version": "2.0.1"},
    )

    merge_pack_into_kestrel(pack, folder)

    merged = json.loads((kestrel / "kestrel_scenedata.json").read_text())
    assert merged["image_ratings"]["IMG_001.CR3"] == 5, \
        "user rating wiped by pack merge — Stage 4B regression"
    assert merged["image_ratings"]["IMG_002.CR3"] == 0, \
        "new cloud rating not added"
    assert merged["scenes"]["1"]["name"] == "Goldfinch in flight", \
        "user scene name overwritten"
    assert merged["scenes"]["1"]["status"] == "reviewed", \
        "user scene status overwritten"
    assert merged["scenes"]["1"]["user_tags"]["finalized"] is True
    # image_filenames union (order-preserving for local entries).
    assert "IMG_001.CR3" in merged["scenes"]["1"]["image_filenames"]
    assert "IMG_002.CR3" in merged["scenes"]["1"]["image_filenames"]
    # New scene added.
    assert "2" in merged["scenes"]
    assert merged["scenes"]["2"]["image_filenames"] == ["IMG_003.CR3"]


def test_artifact_skip_if_local_file_exists(tmp_path: Path) -> None:
    """Stage 4C: a local crop file is preserved byte-for-byte; new
    crop files from the pack are added."""
    folder = tmp_path / "shoot"
    kestrel = folder / ".kestrel"
    crop_dir = kestrel / "crop"
    crop_dir.mkdir(parents=True)

    local_crop_bytes = b"LOCAL_CROP_PIXELS_1234567890"
    (crop_dir / "IMG_001_crop_0.jpg").write_bytes(local_crop_bytes)

    pack = tmp_path / "pack_1.zip"
    _build_pack_zip(
        pack,
        csv_rows=[],
        scenedata={"version": "2.0", "image_ratings": {}, "scenes": {}},
        metadata={"kestrel_version": "2.0.1"},
        crops={
            "IMG_001_crop_0.jpg": b"CLOUD_CROP_DIFFERENT",   # would clobber
            "IMG_002_crop_0.jpg": b"CLOUD_NEW_CROP",          # should be added
        },
    )

    merge_pack_into_kestrel(pack, folder)

    assert (crop_dir / "IMG_001_crop_0.jpg").read_bytes() == local_crop_bytes, \
        "local crop overwritten by cloud — Stage 4C regression"
    assert (crop_dir / "IMG_002_crop_0.jpg").read_bytes() == b"CLOUD_NEW_CROP"


def test_metadata_full_replace_safe(tmp_path: Path) -> None:
    """Metadata file has no user data — full replacement is correct.
    This test guards against accidental over-application of the
    additive semantics to metadata."""
    folder = tmp_path / "shoot"
    kestrel = folder / ".kestrel"
    kestrel.mkdir(parents=True)

    (kestrel / "kestrel_metadata.json").write_text(json.dumps({
        "kestrel_version": "1.5.0",
        "analyzer_name": "old",
    }))

    pack = tmp_path / "pack_1.zip"
    _build_pack_zip(
        pack,
        csv_rows=[],
        scenedata={"version": "2.0", "image_ratings": {}, "scenes": {}},
        metadata={"kestrel_version": "2.0.1", "analyzer_name": "cloud"},
    )

    merge_pack_into_kestrel(pack, folder)
    merged = json.loads((kestrel / "kestrel_metadata.json").read_text())
    assert merged["kestrel_version"] == "2.0.1"
    assert merged["analyzer_name"] == "cloud"


# ── Retry-errored merge tests ──────────────────────────────────────────────


def test_overwrite_errors_replaces_errored_row(tmp_path: Path) -> None:
    """overwrite_errors=True: a local row whose species=='Error' is
    replaced when the cloud row has a real classification. This is the
    retry-errored happy path."""
    folder = tmp_path / "shoot"
    kestrel = folder / ".kestrel"
    kestrel.mkdir(parents=True)

    _write_local_csv(kestrel, [
        {
            "filename": "IMG_001.CR3",
            "species": "Error",
            "species_confidence": "",
            "culled": "0",
            "culled_origin": "",
        },
        {
            "filename": "IMG_002.CR3",
            "species": "American Goldfinch",   # healthy, must not be overwritten
            "species_confidence": "0.92",
            "culled": "1",
            "culled_origin": "user",
        },
    ])

    pack = tmp_path / "pack_1.zip"
    _build_pack_zip(
        pack,
        csv_rows=[
            {
                "filename": "IMG_001.CR3",
                "species": "House Finch",   # successful re-analysis
                "species_confidence": "0.88",
                "culled": "0",
                "culled_origin": "",
            },
            {
                "filename": "IMG_002.CR3",
                "species": "Different Bird",  # would clobber a healthy row
                "species_confidence": "0.50",
                "culled": "0",
                "culled_origin": "",
            },
        ],
        scenedata={"version": "2.0", "image_ratings": {}, "scenes": {}},
        metadata={"kestrel_version": "2.0.1"},
    )

    merge_pack_into_kestrel(pack, folder, overwrite_errors=True)

    rows = {r["filename"]: r for r in _read_csv(kestrel / "kestrel_database.csv")}
    assert rows["IMG_001.CR3"]["species"] == "House Finch", \
        "errored row not overwritten despite overwrite_errors=True"
    assert rows["IMG_002.CR3"]["species"] == "American Goldfinch", \
        "healthy row clobbered — overwrite_errors must scope to errored rows only"
    assert rows["IMG_002.CR3"]["culled"] == "1"
    assert rows["IMG_002.CR3"]["culled_origin"] == "user"


def test_overwrite_errors_keeps_local_when_cloud_also_errored(tmp_path: Path) -> None:
    """overwrite_errors=True + cloud row is also 'Error' → keep local row
    (no churn, avoids overwriting a local error with a cloud error)."""
    folder = tmp_path / "shoot"
    kestrel = folder / ".kestrel"
    kestrel.mkdir(parents=True)

    _write_local_csv(kestrel, [
        {
            "filename": "IMG_001.CR3",
            "species": "Error",
            "species_confidence": "",
        },
    ])

    pack = tmp_path / "pack_1.zip"
    _build_pack_zip(
        pack,
        csv_rows=[
            {
                "filename": "IMG_001.CR3",
                "species": "Error",   # cloud also failed on the same file
                "species_confidence": "",
            },
        ],
        scenedata={"version": "2.0", "image_ratings": {}, "scenes": {}},
        metadata={"kestrel_version": "2.0.1"},
    )

    merge_pack_into_kestrel(pack, folder, overwrite_errors=True)
    rows = {r["filename"]: r for r in _read_csv(kestrel / "kestrel_database.csv")}
    assert rows["IMG_001.CR3"]["species"] == "Error"
    # We don't care which Error row "wins" — both are identical in the user's
    # eyes — but the row count must not double.
    assert len(rows) == 1


def test_overwrite_errors_off_keeps_legacy_behavior(tmp_path: Path) -> None:
    """overwrite_errors=False (default): errored local row + healthy cloud
    row → local errored row WINS (Stage 4A semantics preserved)."""
    folder = tmp_path / "shoot"
    kestrel = folder / ".kestrel"
    kestrel.mkdir(parents=True)

    _write_local_csv(kestrel, [
        {
            "filename": "IMG_001.CR3",
            "species": "Error",
            "species_confidence": "",
        },
    ])

    pack = tmp_path / "pack_1.zip"
    _build_pack_zip(
        pack,
        csv_rows=[
            {
                "filename": "IMG_001.CR3",
                "species": "House Finch",
                "species_confidence": "0.88",
            },
        ],
        scenedata={"version": "2.0", "image_ratings": {}, "scenes": {}},
        metadata={"kestrel_version": "2.0.1"},
    )

    merge_pack_into_kestrel(pack, folder)  # overwrite_errors defaults to False
    rows = {r["filename"]: r for r in _read_csv(kestrel / "kestrel_database.csv")}
    assert rows["IMG_001.CR3"]["species"] == "Error", \
        "default merge silently replaced an errored row — regression"


def test_overwrite_errors_respects_protected_filenames(tmp_path: Path) -> None:
    """protected_filenames takes priority over overwrite_errors. A row
    in BOTH sets (errored locally + protected anchor) must be kept."""
    folder = tmp_path / "shoot"
    kestrel = folder / ".kestrel"
    kestrel.mkdir(parents=True)

    _write_local_csv(kestrel, [
        {
            "filename": "ANCHOR.CR3",
            "species": "Error",         # local is errored
            "species_confidence": "",
        },
    ])

    pack = tmp_path / "pack_1.zip"
    _build_pack_zip(
        pack,
        csv_rows=[
            {
                "filename": "ANCHOR.CR3",
                "species": "House Finch",  # cloud has a real classification
                "species_confidence": "0.88",
            },
        ],
        scenedata={"version": "2.0", "image_ratings": {}, "scenes": {}},
        metadata={"kestrel_version": "2.0.1"},
    )

    merge_pack_into_kestrel(
        pack, folder,
        protected_filenames={"ANCHOR.CR3"},
        overwrite_errors=True,
    )
    rows = {r["filename"]: r for r in _read_csv(kestrel / "kestrel_database.csv")}
    # Protected wins even though the local row is errored and the cloud has
    # a real classification. The anchor's local row is authoritative.
    assert rows["ANCHOR.CR3"]["species"] == "Error", \
        "protected anchor was overwritten by retry_errored path"
