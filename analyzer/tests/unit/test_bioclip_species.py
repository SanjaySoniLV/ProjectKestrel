"""Unit tests for the BioCLIP species head, without running the ONNX tower.

Uses the real bundled text embeddings + index (small, always committed) and
replaces the image session with a stub whose output we choose, so region
masking, probability/family aggregation and the output contract are tested
deterministically in the fast lane.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.bird_catalog import get_catalog
from kestrel_analyzer.config import BIOCLIP_MODELS_DIR
from kestrel_analyzer.ml import bioclip_species as bs

pytestmark = pytest.mark.unit

_HAVE_ASSETS = (
    (Path(BIOCLIP_MODELS_DIR) / bs.TEXT_EMB_FILENAME).is_file()
    and (Path(BIOCLIP_MODELS_DIR) / bs.INDEX_FILENAME).is_file()
    and (Path(BIOCLIP_MODELS_DIR) / bs.TEXT_EMB_FILENAME).stat().st_size > 1_000_000
)
needs_assets = pytest.mark.skipif(not _HAVE_ASSETS, reason="BioCLIP text assets missing")


class _StubInput:
    name = "image"


class _StubSession:
    """Stands in for ResilientOnnxSession; returns ``self.out`` per batch row."""

    def __init__(self, kind, path, coord):
        self.kind, self.path, self.coord = kind, path, coord
        self.out = None
        self.last_batch = None

    def get_providers(self):
        return ["CPUExecutionProvider"]

    def get_inputs(self):
        return [_StubInput()]

    def run(self, _names, feed):
        x = feed["image"]
        self.last_batch = x
        return [np.repeat(self.out[None, :], x.shape[0], axis=0) * 3.0]  # un-normalised


@pytest.fixture(scope="module")
def clf():
    if not _HAVE_ASSETS:
        pytest.skip("BioCLIP text assets missing")
    mp = pytest.MonkeyPatch()
    mp.setattr(bs, "ResilientOnnxSession", _StubSession)
    c = bs.BioClipSpeciesClassifier(str(BIOCLIP_MODELS_DIR), coord=None)
    mp.undo()
    return c


def _row(clf, common_name):
    rec = get_catalog().lookup(common_name)
    assert rec is not None, common_name
    return clf.scientific_names.index(rec.scientific_name)


def _point_at(clf, common_name):
    """Make the stub tower emit exactly that species' text embedding."""
    clf.session.out = clf.text_emb[_row(clf, common_name)].copy()


# ---------------------------------------------------------------- preprocessing

class TestPreprocess:
    def test_shape_dtype(self):
        x = bs.preprocess(np.zeros((1024, 1024, 3), np.uint8))
        assert x.shape == (3, 224, 224) and x.dtype == np.float32

    def test_normalisation_constants(self):
        # A flat image of value v maps to (v/255 - mean) / std per channel.
        for v in (0, 128, 255):
            x = bs.preprocess(np.full((600, 900, 3), v, np.uint8))
            expect = (v / 255.0 - bs.CLIP_MEAN) / bs.CLIP_STD
            np.testing.assert_allclose(x.reshape(3, -1).mean(axis=1), expect, atol=1e-5)

    def test_channel_order_is_rgb(self):
        img = np.zeros((300, 300, 3), np.uint8)
        img[..., 0] = 255  # red only
        x = bs.preprocess(img)
        assert x[0].mean() > 1.5 and x[1].mean() < -1.0 and x[2].mean() < -1.0

    def test_centre_crop_of_non_square(self):
        # Left third black, middle white, right third black (landscape): the
        # centre crop keeps the white middle stripe.
        img = np.zeros((300, 900, 3), np.uint8)
        img[:, 300:600] = 255
        x = bs.preprocess(img)
        assert x[:, :, 112].mean() > 1.5

    def test_grayscale_and_rgba_inputs(self):
        assert bs.preprocess(np.zeros((300, 300), np.uint8)).shape == (3, 224, 224)
        assert bs.preprocess(np.zeros((300, 300, 4), np.uint8)).shape == (3, 224, 224)


# ---------------------------------------------------------------- assets

@needs_assets
def test_text_assets_align_with_catalog(clf):
    assert clf.text_emb.shape == (len(clf.scientific_names), 768)
    np.testing.assert_allclose(np.linalg.norm(clf.text_emb, axis=1), 1.0, atol=1e-3)
    # Every row resolves to a catalog record (names match birds_global.csv).
    assert all(r is not None for r in clf._records)  # noqa: SLF001


# ---------------------------------------------------------------- output contract

@needs_assets
class TestClassifyContract:
    def test_dict_shape_matches_legacy_cnn(self, clf):
        clf.set_regions(None)
        _point_at(clf, "American Robin")
        r = clf.classify(np.zeros((1024, 1024, 3), np.uint8), top_k=5)
        for key in ("top_species_labels", "top_species_scores",
                    "top_family_labels", "top_family_scores"):
            assert key in r and len(r[key]) == 5
        assert r["top_species_labels"][0] == "American Robin"
        assert r["top_family_labels"][0] == "Thrush sp."
        assert r["embedding"].shape == (768,) and r["embedding"].dtype == np.float32
        np.testing.assert_allclose(np.linalg.norm(r["embedding"]), 1.0, atol=1e-5)
        assert len(r["top_species_scientific"]) == 5

    def test_scores_are_probabilities(self, clf):
        clf.set_regions(["NA"])
        _point_at(clf, "Snowy Egret")
        emb = clf.embed(np.zeros((300, 300, 3), np.uint8))[0]
        idx, p = clf.scores_from_embedding(emb)
        assert idx.size == clf.allowed_count
        np.testing.assert_allclose(p.sum(), 1.0, atol=1e-5)
        r = clf.classify_embedding(emb, top_k=5)
        assert list(r["top_species_scores"]) == sorted(r["top_species_scores"], reverse=True)

    def test_family_score_is_sum_of_member_species(self, clf):
        clf.set_regions(["NA"])
        _point_at(clf, "Red-tailed Hawk")
        emb = clf.embed(np.zeros((300, 300, 3), np.uint8))[0]
        idx, p = clf.scores_from_embedding(emb)
        r = clf.classify_embedding(emb, top_k=5)
        fam = r["top_family_labels"][0]
        manual = sum(pi for i, pi in zip(idx, p) if clf._records[i].family_common == fam)  # noqa: SLF001
        assert r["top_family_scores"][0] == pytest.approx(manual, rel=1e-5)
        assert r["top_family_scores"][0] >= r["top_species_scores"][0]

    def test_batch(self, clf):
        clf.set_regions(None)
        _point_at(clf, "House Sparrow")
        out = clf.classify_batch([np.zeros((300, 300, 3), np.uint8)] * 3, top_k=2)
        # One crop per run: dynamic int8 quantisation scales over the batch.
        assert len(out) == 3 and clf.session.last_batch.shape == (1, 3, 224, 224)
        assert all(o["top_species_labels"][0] == "House Sparrow" for o in out)
        assert clf.classify_batch([], top_k=2) == []


# ---------------------------------------------------------------- geofence

@needs_assets
class TestRegions:
    def test_global_is_everything(self, clf):
        clf.set_regions(None)
        n = clf.allowed_count
        assert n == len(clf.scientific_names)
        clf.set_regions(["global"])
        assert clf.allowed_count == n and clf.regions == ["global"]
        clf.set_regions([])
        assert clf.allowed_count == n

    def test_region_mask_matches_catalog_filter(self, clf):
        cat = get_catalog()
        clf.set_regions(["NA"])
        expected = {r.scientific_name for r in cat.filter(["NA"])}
        got = {clf.scientific_names[i] for i in clf._allowed}  # noqa: SLF001
        assert got == expected
        assert clf.regions == ["NA"]

    def test_union_of_regions(self, clf):
        clf.set_regions(["NA"])
        na = clf.allowed_count
        clf.set_regions(["NA", "PAL"])
        assert clf.allowed_count > na

    def test_out_of_region_species_never_predicted(self, clf):
        # Pygmy Sunbird is Afrotropical only: with an NA geofence the stub
        # pointing straight at it must still yield an NA species.
        clf.set_regions(["NA"])
        _point_at(clf, "Pygmy Sunbird")
        r = clf.classify(np.zeros((300, 300, 3), np.uint8), top_k=5)
        cat = get_catalog()
        for name in r["top_species_labels"]:
            assert cat.lookup(name).matches_any_region(frozenset({"NA"}))
        clf.set_regions(["AF"])
        r = clf.classify(np.zeros((300, 300, 3), np.uint8), top_k=1)
        assert r["top_species_labels"][0] == "Pygmy Sunbird"

    def test_introduced_species_in_na(self, clf):
        clf.set_regions(["NA"])
        _point_at(clf, "House Sparrow")
        r = clf.classify(np.zeros((300, 300, 3), np.uint8), top_k=1)
        assert r["top_species_labels"][0] == "House Sparrow"

    def test_mask_is_cached(self, clf):
        clf.set_regions(["AU"])
        a = clf._allowed  # noqa: SLF001
        clf.set_regions(["AU"])
        assert clf._allowed is a  # noqa: SLF001

    def test_unknown_region_falls_back_to_global(self, clf):
        clf.set_regions(["XX"])
        assert clf.regions == ["global"]


def test_normalize_regions():
    assert bs.normalize_regions(None) is None
    assert bs.normalize_regions([]) is None
    assert bs.normalize_regions(["Global"]) is None
    assert bs.normalize_regions("NA") == frozenset({"NA"})
    assert bs.normalize_regions(["NA", "PAL", ""]) == frozenset({"NA", "PAL"})
