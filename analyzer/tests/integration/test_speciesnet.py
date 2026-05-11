"""Integration tests for the full SpeciesNet + SAM-HQ wrapper (`get_prediction`).

Loads the real detector + classifier + SAM-HQ + ensemble and runs end-to-end
prediction on CR3 fixtures in set_a_fresh/.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.config import (
    DEFAULT_DETECTOR_NAME,
    DETECTOR_ONNX_PATHS,
    SAM_DEC_ONNX_PATH,
    SAM_ENC_ONNX_PATH,
    SPECIESNET_MODEL_DIR,
)
from kestrel_analyzer.image_utils import read_image
from kestrel_analyzer.ml.speciesnet_sam_hq import SpeciesNetSAMHQWrapper


pytestmark = pytest.mark.integration


_DETECTOR_PATH = DETECTOR_ONNX_PATHS[DEFAULT_DETECTOR_NAME]
_SPECIESNET_ONNX = SPECIESNET_MODEL_DIR / "speciesNet_v4.0.1a.onnx"

_skip_no_models = pytest.mark.skipif(
    not (
        _DETECTOR_PATH.is_file()
        and _SPECIESNET_ONNX.is_file()
        and Path(SAM_ENC_ONNX_PATH).is_file()
        and Path(SAM_DEC_ONNX_PATH).is_file()
    ),
    reason="One or more SpeciesNet / SAM-HQ ONNX weights are missing",
)


@pytest.fixture(scope="module")
def loaded_wrapper():
    if not (
        _DETECTOR_PATH.is_file()
        and _SPECIESNET_ONNX.is_file()
        and Path(SAM_ENC_ONNX_PATH).is_file()
        and Path(SAM_DEC_ONNX_PATH).is_file()
    ):
        pytest.skip("Required SpeciesNet / SAM-HQ weights missing")
    wrapper = SpeciesNetSAMHQWrapper(
        max_bird_crops=5,
        use_gpu=False,
        detector_name=DEFAULT_DETECTOR_NAME,
    )
    wrapper.ensure_loaded()
    return wrapper


@pytest.fixture(scope="module")
def first_cr3(request):
    set_a = (
        Path(__file__).parent.parent / "fixtures" / "test_sets" / "set_a_fresh"
    )
    if not set_a.exists():
        pytest.skip(f"Fixture {set_a} not present")
    files = sorted(set_a.glob("*.CR3"))
    if not files:
        pytest.skip("No CR3 fixtures in set_a_fresh")
    return files[0]


@pytest.fixture(scope="module")
def first_cr3_decoded(first_cr3):
    img = read_image(str(first_cr3))
    if img is None:
        pytest.skip(f"Could not decode {first_cr3}")
    return img


@_skip_no_models
class TestSpeciesNetLoading:
    def test_ensure_loaded_succeeds(self, loaded_wrapper):
        assert loaded_wrapper.detector is not None
        assert loaded_wrapper.classifier is not None
        assert loaded_wrapper.predictor is not None
        assert loaded_wrapper.ensemble is not None

    def test_classifier_has_labels(self, loaded_wrapper):
        labels = loaded_wrapper.classifier._labels
        assert isinstance(labels, list)
        assert len(labels) > 100  # SpeciesNet has thousands of labels


@_skip_no_models
class TestSpeciesNetPrediction:
    """End-to-end get_prediction() output shape and value-range tests."""

    def test_get_prediction_returns_4_tuple(self, loaded_wrapper, first_cr3, first_cr3_decoded):
        result = loaded_wrapper.get_prediction(
            first_cr3_decoded,
            first_cr3,
            wildlife_enabled=True,
            threshold=0.25,
        )
        assert isinstance(result, tuple)
        assert len(result) == 4

    def test_prediction_arrays_same_length(self, loaded_wrapper, first_cr3, first_cr3_decoded):
        masks, pred_boxes, pred_class, pred_score = loaded_wrapper.get_prediction(
            first_cr3_decoded,
            first_cr3,
            wildlife_enabled=True,
            threshold=0.25,
        )
        # masks may be empty list or a stacked array; the three label lists must align
        assert len(pred_boxes) == len(pred_class) == len(pred_score)
        if pred_class:
            # masks is a stacked numpy array when non-empty
            assert hasattr(masks, "shape") and masks.shape[0] == len(pred_class)

    def test_pred_classes_valid(self, loaded_wrapper, first_cr3, first_cr3_decoded):
        _, _, pred_class, _ = loaded_wrapper.get_prediction(
            first_cr3_decoded,
            first_cr3,
            wildlife_enabled=True,
            threshold=0.25,
        )
        for cls in pred_class:
            # After routing, only "bird" or a wildlife species label remains
            assert isinstance(cls, str)
            assert cls != ""

    def test_pred_scores_in_unit_range(self, loaded_wrapper, first_cr3, first_cr3_decoded):
        _, _, _, pred_score = loaded_wrapper.get_prediction(
            first_cr3_decoded,
            first_cr3,
            wildlife_enabled=True,
            threshold=0.25,
        )
        for s in pred_score:
            assert 0.0 <= float(s) <= 1.0

    def test_high_threshold_yields_fewer_detections(self, loaded_wrapper, first_cr3, first_cr3_decoded):
        _, _, _, low = loaded_wrapper.get_prediction(
            first_cr3_decoded, first_cr3, wildlife_enabled=True, threshold=0.10
        )
        _, _, _, high = loaded_wrapper.get_prediction(
            first_cr3_decoded, first_cr3, wildlife_enabled=True, threshold=0.99
        )
        assert len(high) <= len(low)

    def test_blank_image_no_detections(self, loaded_wrapper):
        """Pure-white synthetic image → no confident detections."""
        blank = np.full((1024, 1024, 3), 255, dtype=np.uint8)
        masks, pred_boxes, pred_class, pred_score = loaded_wrapper.get_prediction(
            blank, "blank.jpg", wildlife_enabled=True, threshold=0.5
        )
        assert len(pred_class) == 0
        assert len(pred_boxes) == 0
        assert len(pred_score) == 0

    def test_wildlife_disabled_excludes_non_bird(self, loaded_wrapper, first_cr3, first_cr3_decoded):
        _, _, pred_class, _ = loaded_wrapper.get_prediction(
            first_cr3_decoded,
            first_cr3,
            wildlife_enabled=False,
            threshold=0.25,
        )
        # With wildlife disabled, only "bird" labels survive routing
        for cls in pred_class:
            assert cls == "bird", f"Got non-bird '{cls}' with wildlife_enabled=False"
