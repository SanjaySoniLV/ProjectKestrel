"""Smart search: scoring/aggregation (synthetic) and the real CLIP towers' output contract."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.ml import clip_search as cs

pytestmark = pytest.mark.unit

needs_assets = pytest.mark.skipif(not cs.assets_available(), reason="CLIP assets missing (git lfs pull)")


def _unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


def test_normalize_query():
    assert cs.normalize_query("  in   flight. ") == "in flight"
    assert cs.normalize_query(None) == ""
    assert len(cs.normalize_query("x" * 1000)) == cs.MAX_QUERY_CHARS


def test_baseline_subtraction_demotes_generic_crops():
    # Axis 0 = "a bird" (generic), axis 1 = the query concept.
    base = _unit([1, 0, 0])
    query = _unit([0.9, 0.436, 0])  # like every bird prompt, the query shares the generic direction
    generic = _unit([1, 0, 0])  # blurry/generic crop: close to "a bird"
    match = _unit([0.5, 0.866, 0])  # actually shows the concept
    imgs = np.stack([generic, match])
    raw = imgs @ query
    assert raw[0] > raw[1]  # without the baseline the generic crop wins
    s = cs.crop_scores(imgs, query, base)
    assert s[1] > s[0]
    assert cs.crop_scores(np.zeros((0, 3)), query, base).shape == (0,)


def test_rank_aggregates_max_over_crops_and_images():
    keys = [("a.CR3", 0), ("a.CR3", 1), ("b.CR3", 0), ("c.CR3", 0), ("gone.CR3", 0)]
    scores = np.array([0.1, 0.5, 0.4, 0.3, 9.0], np.float32)
    scene_of = {"a.CR3": "1", "b.CR3": "1", "c.CR3": "2"}  # gone.CR3 no longer in the database
    out = cs.rank(keys, scores, scene_of, limit=10)
    assert [s["scene_id"] for s in out["scenes"]] == ["1", "2"]
    top = out["scenes"][0]
    assert top["best_filename"] == "a.CR3" and top["best_crop"] == 1 and top["score"] == pytest.approx(0.5)
    assert [(i["filename"], i["crop_index"]) for i in out["images"]] == [("a.CR3", 1), ("b.CR3", 0), ("c.CR3", 0)]
    assert len(cs.rank(keys, scores, scene_of, limit=1)["scenes"]) == 1


def test_subject_crops_skips_whole_frame_fallback():
    crops = [{"detection_index": -1}, {"detection_index": 0}, {"detection_index": "x"}, {}]
    assert cs.subject_crops(crops) == [{"detection_index": 0}]


@needs_assets
def test_image_embedder_shape_and_norm():
    from kestrel_analyzer.ml.provider_coordinator import ProviderCoordinator

    emb = cs.ClipImageEmbedder(cs.default_assets_dir(), ProviderCoordinator(user_gpu_enabled=False))
    rng = np.random.default_rng(0)
    crops = [rng.integers(0, 255, (300, 200, 3), dtype=np.uint8), rng.integers(0, 255, (64, 64, 3), dtype=np.uint8)]
    out = emb.embed(crops)
    assert out.shape == (2, cs.DIM) and out.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-5)
    # One crop per run: a crop's embedding does not depend on its batch-mates.
    np.testing.assert_allclose(emb.embed(crops[0])[0], out[0], atol=1e-6)
    assert emb.embed([]).shape == (0, cs.DIM)


@needs_assets
def test_text_encoder_norm_cache_and_semantics():
    enc = cs.ClipTextEncoder()
    a = enc.encode("in flight")
    assert a.shape == (cs.DIM,) and a.dtype == np.float32
    assert np.linalg.norm(a) == pytest.approx(1.0, abs=1e-5)
    assert enc.encode(" in  flight. ") is a  # normalised query hits the cache
    b = enc.baseline()
    assert float(a @ b) < 0.999 and float(a @ enc.encode("a bird flying")) > float(a @ enc.encode("a red car"))
