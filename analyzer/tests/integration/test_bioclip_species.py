"""Integration tests for BioClipSpeciesClassifier with the real int8 ONNX tower.

Runs the image tower on CPU against a real RAW fixture and on synthetic
inputs. Region masking, family aggregation and the output contract are covered
without the ONNX model in ``unit/test_bioclip_species.py``.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.config import BIOCLIP_IMAGE_MODEL_PATH, BIOCLIP_MODELS_DIR
from kestrel_analyzer.image_utils import read_image
from kestrel_analyzer.ml.bioclip_species import BioClipSpeciesClassifier
from kestrel_analyzer.ml.provider_coordinator import ProviderCoordinator, ResilienceConfig

pytestmark = pytest.mark.integration


def _have_model() -> bool:
    p = Path(BIOCLIP_IMAGE_MODEL_PATH)
    # A Git LFS pointer is ~130 bytes; the real tower is ~300 MB.
    return p.is_file() and p.stat().st_size > 1_000_000


@pytest.fixture(scope="module")
def clf():
    if not _have_model():
        pytest.skip("BioCLIP image tower not present (Git LFS not pulled?)")
    coord = ProviderCoordinator(user_gpu_enabled=False, cfg=ResilienceConfig())
    return BioClipSpeciesClassifier(str(BIOCLIP_MODELS_DIR), coord)


def test_session_is_registered_with_the_coordinator(clf):
    # Shared-coordinator registration is what lets GPU->CPU demotion rebuild
    # this session together with the detector/SAM/quality sessions.
    assert clf.session in clf.session._coord._sessions  # noqa: SLF001
    assert clf.providers_used


def test_embedding_is_normalised_768(clf):
    img = np.full((1024, 1024, 3), 128, dtype=np.uint8)
    e = clf.embed(img)
    assert e.shape == (1, 768)
    assert e.dtype == np.float32
    np.testing.assert_allclose(np.linalg.norm(e, axis=1), 1.0, atol=1e-4)


def test_batch_matches_single(clf):
    # Must be exact: a crop's species may not depend on the other crops in
    # the frame (dynamic int8 quantisation would make it so if batched).
    rng = np.random.default_rng(0)
    imgs = [rng.integers(0, 255, (512, 512, 3), dtype=np.uint8) for _ in range(3)]
    batch = clf.classify_batch(imgs, top_k=3)
    single = [clf.classify(im, top_k=3) for im in imgs]
    for b, s in zip(batch, single):
        assert list(b["top_species_labels"]) == list(s["top_species_labels"])
        np.testing.assert_allclose(b["embedding"], s["embedding"], atol=1e-6)


def test_classify_on_real_image(clf, set_a_path):
    if not set_a_path.exists():
        pytest.skip(f"Fixture {set_a_path} not present")
    cr3_files = sorted(set_a_path.glob("*.CR3"))
    if not cr3_files:
        pytest.skip("No CR3 fixtures")
    img = read_image(str(cr3_files[0]))
    if img is None:
        pytest.skip("Could not decode CR3")
    h, w = img.shape[:2]
    s = min(h, w)
    crop = img[(h - s) // 2:(h + s) // 2, (w - s) // 2:(w + s) // 2]
    clf.set_regions(["NA"])
    res = clf.classify(crop, top_k=5)
    assert len(res["top_species_labels"]) == 5
    assert 0.0 < res["top_species_scores"][0] <= 1.0
    assert all(isinstance(str(x), str) and str(x) for x in res["top_species_labels"])


def test_repeatable_scores(clf):
    img = np.full((300, 300, 3), 80, dtype=np.uint8)
    r1 = clf.classify(img, top_k=5)
    r2 = clf.classify(img, top_k=5)
    assert list(r1["top_species_labels"]) == list(r2["top_species_labels"])
    np.testing.assert_allclose(r1["top_species_scores"], r2["top_species_scores"], rtol=1e-4)
