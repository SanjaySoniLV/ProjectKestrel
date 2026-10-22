"""Cloud pack merge carries pipeline-3 data: embedding sidecars and ``pipeline_major``.

A Modal segment pack holds the embedding rows of its own images only, and its
metadata lacks ``pipeline_major`` (the caught-up exit skips post-analysis), so
the desktop merge has to append the rows and decide the stamp itself.
"""

import csv
import json
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cloud_compute_client import merge_pack_into_kestrel  # noqa: E402
from kestrel_analyzer import embedding_store as es  # noqa: E402

pytestmark = pytest.mark.unit

DIM = 8


def _vec(seed):
    v = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def _make_pack(tmp_path: Path, name: str, filenames, *, v3=True, meta=None) -> Path:
    src = tmp_path / f"src_{name}" / ".kestrel"
    src.mkdir(parents=True)
    cols = ["filename", "species", "quality"] + (["quality_siglip", "quality_legacy"] if v3 else [])
    with (src / "kestrel_database.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for fn in filenames:
            row = {"filename": fn, "species": "Roseate Spoonbill", "quality": "0.5"}
            if v3:
                row.update(quality_siglip="0.5", quality_legacy="0.4")
            w.writerow(row)
    (src / "kestrel_metadata.json").write_text(json.dumps(meta or {"version": "3.0.0"}))
    if v3:
        bio = es.EmbeddingStore(str(src), "bioclip-2", DIM)
        sig = es.EmbeddingStore(str(src), "siglip2-b16", DIM, slot="siglip2-b16")
        for i, fn in enumerate(filenames):
            bio.put(fn, {0: _vec(i)})
            sig.put(fn, {0: _vec(100 + i), 1: _vec(200 + i)})
    pack = tmp_path / f"{name}.zip"
    with zipfile.ZipFile(pack, "w") as zf:
        for f in sorted(src.rglob("*")):
            if f.is_file():
                zf.write(f, arcname=str(Path(".kestrel") / f.relative_to(src)))
    return pack


def _major(root: Path) -> int:
    return int(json.loads((root / ".kestrel" / "kestrel_metadata.json").read_text()).get("pipeline_major") or 0)


def test_segments_append_embeddings_and_stamp_v3(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    merge_pack_into_kestrel(_make_pack(tmp_path, "s1", ["a.CR3", "b.CR3"]), root)
    merge_pack_into_kestrel(_make_pack(tmp_path, "s2", ["c.CR3"]), root)

    kd = str(root / ".kestrel")
    keys, mat, header = es.load_embeddings(kd)
    assert header["model"] == "bioclip-2"
    assert sorted(k[0] for k in keys) == ["a.CR3", "b.CR3", "c.CR3"]
    skeys, smat, _ = es.load_embeddings(kd, slot="siglip2-b16")
    assert len(skeys) == 6
    # Vectors survive the round trip (float16 precision).
    got = dict(zip(skeys, smat.astype(np.float32)))
    assert np.allclose(got[("c.CR3", 1)], _vec(200 + 0), atol=1e-2)
    assert _major(root) == 3


def test_local_rows_win_and_protected_are_skipped(tmp_path):
    root = tmp_path / "photos"
    kd = root / ".kestrel"
    kd.mkdir(parents=True)
    local = es.EmbeddingStore(str(kd), "bioclip-2", DIM)
    local.put("a.CR3", {0: _vec(999)})

    merge_pack_into_kestrel(
        _make_pack(tmp_path, "s1", ["a.CR3", "b.CR3", "anchor.CR3"]), root,
        protected_filenames={"anchor.CR3"},
    )
    keys, mat, _ = es.load_embeddings(str(kd))
    got = dict(zip(keys, mat.astype(np.float32)))
    assert np.allclose(got[("a.CR3", 0)], _vec(999), atol=1e-2)
    assert ("b.CR3", 0) in got
    assert ("anchor.CR3", 0) not in got


def test_v2_pack_into_v3_folder_drops_the_stamp(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    merge_pack_into_kestrel(_make_pack(tmp_path, "s1", ["a.CR3"]), root)
    assert _major(root) == 3
    merge_pack_into_kestrel(
        _make_pack(tmp_path, "old", ["b.CR3"], v3=False, meta={"version": "2.4.0"}), root
    )
    assert _major(root) == 0


def test_v3_pack_into_v2_folder_is_not_stamped(tmp_path):
    root = tmp_path / "photos"
    root.mkdir()
    merge_pack_into_kestrel(
        _make_pack(tmp_path, "old", ["a.CR3"], v3=False, meta={"version": "2.4.0"}), root
    )
    merge_pack_into_kestrel(_make_pack(tmp_path, "s1", ["b.CR3"]), root)
    assert _major(root) == 0
