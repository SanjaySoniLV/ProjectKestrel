"""Integration tests for the SigLIP 2 towers, the BioCLIP gate/routing and their assets."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.ml import siglip
from kestrel_analyzer.ml.provider_coordinator import ProviderCoordinator

pytestmark = pytest.mark.integration

_skip = pytest.mark.skipif(not siglip.assets_available(), reason="SigLIP assets missing (LFS not pulled?)")
_CROP = (Path(__file__).parent.parent / "fixtures" / "test_sets" / "set_c_preanalyzed" / ".kestrel" / "crop"
         / "IMG_2013_crop_0.jpg")


@pytest.fixture(scope="module")
def crop_rgb():
    from PIL import Image
    if not _CROP.is_file():
        pytest.skip("pre-analysed crop fixture missing")
    return np.asarray(Image.open(_CROP).convert("RGB"))


@pytest.fixture(scope="module")
def image_embedder():
    return siglip.SiglipImageEmbedder(None, ProviderCoordinator(user_gpu_enabled=False))


@pytest.fixture(scope="module")
def text_encoder():
    enc = siglip.SiglipTextEncoder(idle_release_sec=None)
    yield enc
    enc.release()


@_skip
def test_image_encode_shape_and_norm(image_embedder, crop_rgb):
    e = image_embedder.embed(crop_rgb)
    assert e.shape == (1, siglip.DIM) and e.dtype == np.float32
    assert np.linalg.norm(e[0]) == pytest.approx(1.0, abs=1e-4)
    two = image_embedder.embed([crop_rgb, crop_rgb[::2, ::2]])
    assert two.shape == (2, siglip.DIM)
    # One image per run: the batch result equals the single-image result.
    np.testing.assert_allclose(two[0], e[0], atol=1e-5)


@_skip
def test_text_encode_shape_cache_and_release(text_encoder):
    t = text_encoder.encode("bird in flight")
    assert t.shape == (siglip.DIM,) and np.linalg.norm(t) == pytest.approx(1.0, abs=1e-4)
    assert text_encoder.encode("bird in flight.") is t          # cached (normalised query)
    batch = text_encoder.encode_texts(["a photo of a heron.", "a photo of a sparrow."])
    assert batch.shape == (2, siglip.DIM)
    text_encoder.release()
    assert not text_encoder.loaded
    np.testing.assert_allclose(text_encoder.encode_texts(["a photo of bird in flight."])[0], t, atol=1e-5)


@_skip
def test_text_image_agreement(image_embedder, text_encoder, crop_rgb):
    img = image_embedder.embed(crop_rgb)[0]
    bird = float(img @ text_encoder.encode("a small songbird"))
    car = float(img @ text_encoder.encode("a red sports car"))
    assert bird > car


@_skip
def test_quality_score_in_range_and_prompt_asset_matches(image_embedder, text_encoder, crop_rgb):
    q = siglip.SiglipQualityScorer()
    s = q.score(image_embedder.embed(crop_rgb)[0])
    assert 0.0 <= s <= 1.0
    live = text_encoder.encode_texts([q.prompts["pos"], q.prompts["neg"]])
    np.testing.assert_allclose(live[0] - live[1], q.direction, atol=1e-4)


def test_gate_and_routing_on_a_real_crop(crop_rgb):
    from kestrel_analyzer.config import BIOCLIP_IMAGE_MODEL_PATH
    if not Path(BIOCLIP_IMAGE_MODEL_PATH).is_file() or Path(BIOCLIP_IMAGE_MODEL_PATH).stat().st_size < 1_000_000:
        pytest.skip("BioCLIP weights missing")
    from kestrel_analyzer.config import BIOCLIP_MODELS_DIR
    from kestrel_analyzer.ml.animal_gate import AnimalGate
    from kestrel_analyzer.ml.bioclip_species import BioClipSpeciesClassifier

    clf = BioClipSpeciesClassifier(str(BIOCLIP_MODELS_DIR), ProviderCoordinator(user_gpu_enabled=False))
    clf.set_regions(["NA"])
    emb = clf.embed(crop_rgb)[0]
    gate = AnimalGate()
    assert gate.animal_score(emb) > 0.5
    r = clf.route(emb)
    assert r["route"] == "bird" and r["bird_prob"] > 0.5
    assert len(clf.wildlife_labels) > 1000
    # A flat grey patch is background.
    flat = np.full((256, 256, 3), 128, np.uint8)
    assert gate.animal_score(clf.embed(flat)[0]) < 0.5
