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
