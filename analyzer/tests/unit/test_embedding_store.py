"""Unit tests for the per-folder BioCLIP embedding sidecar."""

import json
import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.embedding_store import (
    EMBEDDINGS_DIRNAME,
    INDEX_FILENAME,
    EmbeddingStore,
    load_embeddings,
)

pytestmark = pytest.mark.unit
DIM = 768


def _vec(seed):
    v = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
    return v / np.linalg.norm(v)


def _keys(kdir):
    keys, mat, header = load_embeddings(str(kdir))
    return keys, mat, header


def test_put_and_load_roundtrip(tmp_path):
    s = EmbeddingStore(str(tmp_path), model="bioclip-2")
    s.put("a.CR3", {0: _vec(1), 1: _vec(2)})
    s.put("b.CR3", {0: _vec(3)})
    keys, mat, header = _keys(tmp_path)
    assert keys == [("a.CR3", 0), ("a.CR3", 1), ("b.CR3", 0)]
    assert mat.dtype == np.float16 and mat.shape == (3, DIM)
    np.testing.assert_allclose(mat[2].astype(np.float32), _vec(3), atol=2e-3)
    assert header["model"] == "bioclip-2" and header["dim"] == DIM


def test_reput_replaces_and_empty_put_tombstones(tmp_path):
    s = EmbeddingStore(str(tmp_path), model="m")
    s.put("a.CR3", {0: _vec(1), 1: _vec(2)})
    s.put("a.CR3", {0: _vec(9)})       # re-analysed with one crop
    keys, mat, _ = _keys(tmp_path)
    assert keys == [("a.CR3", 0)]
    np.testing.assert_allclose(mat[0].astype(np.float32), _vec(9), atol=2e-3)
    s.put("a.CR3", {})                  # re-analysed, no bird
    assert _keys(tmp_path)[0] == []


def test_remove_and_compact(tmp_path):
    s = EmbeddingStore(str(tmp_path), model="m")
    for i, fn in enumerate(["a", "b", "c"]):
        s.put(fn, {0: _vec(i)})
    s.remove(["b"])
    s.compact(["a", "c", "zzz"])
    keys, mat, header = _keys(tmp_path)
    assert keys == [("a", 0), ("c", 0)]
    np.testing.assert_allclose(mat[1].astype(np.float32), _vec(2), atol=2e-3)
    d = tmp_path / EMBEDDINGS_DIRNAME
    data_files = [p for p in os.listdir(d) if p.endswith(".f16")]
    assert data_files == [header["data"]]       # old generation deleted
    assert (d / header["data"]).stat().st_size == 2 * DIM * 2
    # Compact also drops files no longer in the database.
    s.compact(["c"])
    assert _keys(tmp_path)[0] == [("c", 0)]


def test_reopen_appends(tmp_path):
    EmbeddingStore(str(tmp_path), model="m").put("a", {0: _vec(1)})
    s2 = EmbeddingStore(str(tmp_path), model="m")
    s2.put("b", {2: _vec(2)})
    assert _keys(tmp_path)[0] == [("a", 0), ("b", 2)]


def test_model_change_resets(tmp_path):
    EmbeddingStore(str(tmp_path), model="old").put("a", {0: _vec(1)})
    EmbeddingStore(str(tmp_path), model="new")
    keys, _, header = _keys(tmp_path)
    assert keys == [] and header["model"] == "new"


def test_torn_writes_are_ignored(tmp_path):
    s = EmbeddingStore(str(tmp_path), model="m")
    s.put("a", {0: _vec(1)})
    d = tmp_path / EMBEDDINGS_DIRNAME
    header = json.loads((d / INDEX_FILENAME).read_text().splitlines()[0])
    with open(d / header["data"], "ab") as f:
        f.write(b"\x00" * 100)                      # torn row
    with open(d / INDEX_FILENAME, "a") as f:
        f.write('{"f": "b", "rows": {"0": 1')         # torn line
    s2 = EmbeddingStore(str(tmp_path), model="m")
    s2.put("c", {0: _vec(3)})
    keys, mat, _ = _keys(tmp_path)
    assert keys == [("a", 0), ("c", 0)]
    np.testing.assert_allclose(mat[1].astype(np.float32), _vec(3), atol=2e-3)


def test_bad_vectors_skipped_and_never_raises(tmp_path):
    s = EmbeddingStore(str(tmp_path), model="m")
    s.put("a", {0: np.zeros(5), 1: None, 2: np.full(DIM, np.nan)})
    assert _keys(tmp_path)[0] == []
    # Unwritable target: constructing/using the store must not raise.
    blocker = tmp_path / "file_not_dir"
    blocker.write_text("x")
    bad = EmbeddingStore(str(blocker), model="m")
    bad.put("a", {0: _vec(1)})
    bad.remove(["a"])
    bad.compact([])


def test_load_missing_is_empty(tmp_path):
    keys, mat, header = load_embeddings(str(tmp_path))
    assert keys == [] and mat.shape == (0, DIM) and header is None


# ---------------------------------------------------------------- model slots


def _vec_dim(seed, dim):
    v = np.random.default_rng(seed).standard_normal(dim).astype(np.float32)
    return v / np.linalg.norm(v)


def test_slots_side_by_side_roundtrip(tmp_path):
    bio = EmbeddingStore(str(tmp_path), model="bioclip-2")
    clip = EmbeddingStore(str(tmp_path), model="clip-b16", dim=512, slot="clip-b16")
    bio.put("a", {0: _vec(1)})
    clip.put("a", {0: _vec_dim(2, 512), 1: _vec_dim(3, 512)})
    clip.put("b", {0: _vec_dim(4, 512)})
    keys, mat, header = load_embeddings(str(tmp_path))
    assert keys == [("a", 0)] and header["model"] == "bioclip-2" and mat.shape == (1, DIM)
    keys, mat, header = load_embeddings(str(tmp_path), slot="clip-b16", model="clip-b16", dim=512)
    assert keys == [("a", 0), ("a", 1), ("b", 0)] and mat.shape == (3, 512)
    np.testing.assert_allclose(mat[2].astype(np.float32), _vec_dim(4, 512), atol=2e-3)
    # Compacting / reopening one slot leaves the other untouched.
    clip.compact(["b"])
    EmbeddingStore(str(tmp_path), model="bioclip-2").compact([])
    assert load_embeddings(str(tmp_path))[0] == []
    assert load_embeddings(str(tmp_path), slot="clip-b16")[0] == [("b", 0)]


def test_primary_reset_does_not_touch_slot(tmp_path):
    clip = EmbeddingStore(str(tmp_path), model="clip-b16", dim=512, slot="clip-b16")
    clip.put("a", {0: _vec_dim(1, 512)})
    EmbeddingStore(str(tmp_path), model="old").put("x", {0: _vec(1)})
    EmbeddingStore(str(tmp_path), model="new")  # resets the primary slot only
    assert load_embeddings(str(tmp_path))[0] == []
    assert load_embeddings(str(tmp_path), slot="clip-b16")[0] == [("a", 0)]


def test_bioclip_only_store_reads_unchanged(tmp_path):
    """A folder written before slots existed: primary store only, no subfolder."""
    EmbeddingStore(str(tmp_path), model="bioclip-2").put("a", {0: _vec(1)})
    d = tmp_path / EMBEDDINGS_DIRNAME
    assert sorted(p.name for p in d.iterdir() if p.is_dir()) == []
    # Reading the absent slot is empty and must not create anything.
    keys, mat, header = load_embeddings(str(tmp_path), slot="clip-b16", dim=512)
    assert keys == [] and mat.shape == (0, 512) and header is None
    assert not (d / "clip-b16").exists()
    # Opening the slot keeps the primary rows.
    EmbeddingStore(str(tmp_path), model="clip-b16", dim=512, slot="clip-b16")
    assert load_embeddings(str(tmp_path))[0] == [("a", 0)]


def test_model_filter_and_bad_slot(tmp_path):
    EmbeddingStore(str(tmp_path), model="bioclip-2").put("a", {0: _vec(1)})
    assert load_embeddings(str(tmp_path), model="other")[0] == []
    bad = EmbeddingStore(str(tmp_path), model="m", slot="../escape")
    bad.put("a", {0: _vec(1)})  # disabled, never raises
    assert not (tmp_path.parent / "escape").exists()
    assert load_embeddings(str(tmp_path), slot="../escape")[0] == []
