"""Integration tests for BirdSpeciesClassifier (custom ONNX bird model).

Loads model.onnx + labels.txt and verifies it classifies a real bird crop
plus synthetic inputs.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.config import (
    MODELS_DIR,
    SPECIESCLASSIFIER_LABELS,
    SPECIESCLASSIFIER_PATH,
)
from kestrel_analyzer.image_utils import read_image
from kestrel_analyzer.ml.bird_species import BirdSpeciesClassifier
from kestrel_analyzer.ml.provider_coordinator import (
    ProviderCoordinator,
    ResilienceConfig,
)


pytestmark = pytest.mark.integration


_skip_no_model = pytest.mark.skipif(
    not (Path(SPECIESCLASSIFIER_PATH).is_file() and Path(SPECIESCLASSIFIER_LABELS).is_file()),
    reason="Bird classifier model.onnx or labels.txt not present",
)


@pytest.fixture(scope="module")
def bird_classifier():
    if not (Path(SPECIESCLASSIFIER_PATH).is_file() and Path(SPECIESCLASSIFIER_LABELS).is_file()):
        pytest.skip("Bird classifier weights missing")
    coord = ProviderCoordinator(
        user_gpu_enabled=False,
        cfg=ResilienceConfig(),
    )
    return BirdSpeciesClassifier(
        str(SPECIESCLASSIFIER_PATH),
        str(SPECIESCLASSIFIER_LABELS),
        coord,
        models_dir=str(MODELS_DIR),
    )


@_skip_no_model
class TestBirdClassifierLoading:
    def test_classifier_loads(self, bird_classifier):
        assert bird_classifier is not None
        assert bird_classifier.session is not None

    def test_labels_loaded(self, bird_classifier):
        assert len(bird_classifier.labels) > 100  # Bird taxonomy has thousands

    def test_family_matrix_built(self, bird_classifier):
        # If species-to-family mapping CSVs exist, family_matrix should be populated.
        assert bird_classifier.family_matrix.ndim == 2
        assert bird_classifier.family_matrix.shape[1] == len(bird_classifier.labels)


@_skip_no_model
class TestBirdClassifierInference:
    def test_classify_returns_expected_keys(self, bird_classifier):
        img = np.full((300, 300, 3), 128, dtype=np.uint8)
        result = bird_classifier.classify(img, top_k=5)

        assert "top_species_labels" in result
        assert "top_species_scores" in result
        assert "top_family_labels" in result
        assert "top_family_scores" in result

    def test_top_k_respected(self, bird_classifier):
        img = np.full((300, 300, 3), 128, dtype=np.uint8)
        result = bird_classifier.classify(img, top_k=3)
        assert len(result["top_species_labels"]) == 3
        assert len(result["top_species_scores"]) == 3

    def test_top_species_labels_are_strings(self, bird_classifier):
        img = np.full((300, 300, 3), 128, dtype=np.uint8)
        result = bird_classifier.classify(img, top_k=5)
        for label in result["top_species_labels"]:
            assert isinstance(str(label), str)
            assert len(str(label)) > 0

    def test_classify_on_real_image(self, bird_classifier, set_a_path):
        if not set_a_path.exists():
            pytest.skip(f"Fixture {set_a_path} not present")
        cr3_files = sorted(set_a_path.glob("*.CR3"))
        if not cr3_files:
            pytest.skip("No CR3 fixtures")
        img = read_image(str(cr3_files[0]))
        if img is None:
            pytest.skip("Could not decode CR3")

        # Crop centre square so we don't run a 6000-px image through a 300x300 classifier
        h, w = img.shape[:2]
        s = min(h, w)
        cy, cx = h // 2, w // 2
        crop = img[cy - s // 2: cy + s // 2, cx - s // 2: cx + s // 2]

        result = bird_classifier.classify(crop, top_k=5)
        assert len(result["top_species_labels"]) == 5

    def test_repeatable_scores(self, bird_classifier):
        img = np.full((300, 300, 3), 80, dtype=np.uint8)
        r1 = bird_classifier.classify(img, top_k=5)
        r2 = bird_classifier.classify(img, top_k=5)
        assert list(r1["top_species_labels"]) == list(r2["top_species_labels"])
        np.testing.assert_allclose(
            np.array(r1["top_species_scores"]),
            np.array(r2["top_species_scores"]),
            rtol=1e-4,
        )
