"""Folder-index cache for smart search on large multi-folder libraries.

A library of many day-folders is searched with one ``smart_search`` call per
folder. The cache used to hold 4 folders, so every query on a 5+ folder
library re-read every folder's embeddings, CSV and scenedata from disk, and
``coverage()`` re-parsed both embedding index files even on a cache hit.
"""

import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer import embedding_store
from kestrel_analyzer import smart_search as ss
from kestrel_analyzer.ml import siglip

pytestmark = pytest.mark.unit

HEADER = "filename,species,family,quality,scene_count,crop_path,export_path,crops_json"


def _folder(root: Path, name: str, n: int = 6, seed: int = 0) -> str:
    kdir = root / name / ".kestrel"
    kdir.mkdir(parents=True)
    rng = np.random.default_rng(seed)
    lines = [HEADER]
    for i in range(n):
        cj = json.dumps([{"crop_index": 0, "crop_path": "", "detection_index": 0,
                          "species": "Song Sparrow", "family": "Sparrow sp.", "quality": 0.5}]).replace('"', '""')
        lines.append(f'IMG_{i:04d}.JPG,Song Sparrow,Sparrow sp.,0.5,{i // 2},,,"{cj}"')
    (kdir / "kestrel_database.csv").write_text("\n".join(lines), encoding="utf-8")
    (kdir / "kestrel_metadata.json").write_text(json.dumps({"version": "3.0.0", "species_regions": ["NA"]}))
    for slot in (siglip.MODEL_ID, siglip.FRAME_SLOT):
        st = embedding_store.EmbeddingStore(str(kdir), model=siglip.MODEL_ID, dim=siglip.DIM, slot=slot)
        for i in range(n):
            v = rng.standard_normal(siglip.DIM).astype(np.float32)
            st.put(f"IMG_{i:04d}.JPG", {0: v / np.linalg.norm(v)})
    return str(kdir)


class Enc:
    def encode(self, q):
        v = np.zeros(siglip.DIM, np.float32)
        v[3] = 1.0
        return v


@pytest.fixture(autouse=True)
def _fresh_cache():
    ss._index_cache.clear()
    yield
    ss._index_cache.clear()


def test_cache_keeps_more_than_four_folders(tmp_path):
    dirs = [_folder(tmp_path, f"day{i}", seed=i) for i in range(8)]
    first = [ss.load_folder_index(d) for d in dirs]
    assert len(ss._index_cache) == 8
    # Second pass is served from the cache (same objects, nothing re-read).
    assert all(ss.load_folder_index(d) is fi for d, fi in zip(dirs, first))


def test_cache_is_bounded_by_bytes_and_keeps_newest(tmp_path, monkeypatch):
    dirs = [_folder(tmp_path, f"day{i}", seed=i) for i in range(5)]
    one = ss._index_nbytes(ss.load_folder_index(dirs[0]))
    ss._index_cache.clear()
    monkeypatch.setattr(ss, "_INDEX_CACHE_BYTES", int(one * 2.5))
    for d in dirs:
        ss.load_folder_index(d)
    keys = list(ss._index_cache)
    assert len(keys) == 2
    assert keys[-1] == os.path.normcase(os.path.realpath(dirs[-1]))
    # A budget smaller than one folder still caches the folder in use.
    ss._index_cache.clear()
    monkeypatch.setattr(ss, "_INDEX_CACHE_BYTES", 1)
    ss.load_folder_index(dirs[0])
    assert len(ss._index_cache) == 1


def test_matrices_cached_as_float16_and_scores_unchanged(tmp_path):
    d = _folder(tmp_path, "day0", n=10)
    fi = ss.load_folder_index(d)
    assert fi.crop_mat.dtype == np.float16 and fi.frame_mat.dtype == np.float16
    _, crop32, _ = siglip.load_store(d, siglip.MODEL_ID)
    # On-disk precision is float16, so the cached copy loses nothing.
    assert np.array_equal(fi.crop_mat.astype(np.float32), crop32)
    res = ss.search_folder(d, "purple", encoder=Enc())
    assert res["mode"] == "semantic" and res["images"]


def test_coverage_computed_once_per_cache_entry(tmp_path, monkeypatch):
    d = _folder(tmp_path, "day0")
    calls = {"n": 0}
    real = embedding_store.live_filenames

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(embedding_store, "live_filenames", counting)
    first = ss.search_folder(d, "purple", encoder=Enc())
    after_first = calls["n"]
    for _ in range(3):
        again = ss.search_folder(d, "purple", encoder=Enc())
    assert calls["n"] == after_first, "coverage re-read the embedding index on a cache hit"
    assert again["indexed_images"] == first["indexed_images"] == 6
    # Without a cached entry the public helper still reads from disk.
    assert ss.coverage(d)["indexed_images"] == 6
    assert calls["n"] > after_first


def test_new_embeddings_invalidate_cached_coverage(tmp_path):
    d = _folder(tmp_path, "day0", n=4)
    assert ss.search_folder(d, "purple", encoder=Enc())["frame_indexed_images"] == 4
    lines = (Path(d) / "kestrel_database.csv").read_text(encoding="utf-8").splitlines()
    lines.append(lines[-1].replace("IMG_0003", "IMG_0004"))
    (Path(d) / "kestrel_database.csv").write_text("\n".join(lines), encoding="utf-8")
    st = embedding_store.EmbeddingStore(d, model=siglip.MODEL_ID, dim=siglip.DIM, slot=siglip.FRAME_SLOT)
    v = np.ones(siglip.DIM, np.float32)
    st.put("IMG_0004.JPG", {0: v / np.linalg.norm(v)})
    res = ss.search_folder(d, "purple", encoder=Enc())
    assert res["frame_indexed_images"] == 5 and res["frame_indexable_images"] == 5
