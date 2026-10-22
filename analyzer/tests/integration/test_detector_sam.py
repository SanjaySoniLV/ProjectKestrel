"""Integration tests for the MegaDetector + SAM-HQ wrapper (`get_prediction`).

Pipeline 3.0 removed the SpeciesNet classifier: the wrapper proposes and
segments animal boxes only (pred_class is always "animal", pred_score the
detector confidence); the BioCLIP gate/routing runs in the pipeline. Runs on
the CR3 fixtures in set_a_fresh/.
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
)
from kestrel_analyzer.image_utils import read_image
from kestrel_analyzer.ml.speciesnet_sam_hq import DetectorSAMHQWrapper, SpeciesNetSAMHQWrapper


pytestmark = pytest.mark.integration


_DETECTOR_PATH = DETECTOR_ONNX_PATHS[DEFAULT_DETECTOR_NAME]

_skip_no_models = pytest.mark.skipif(
    not (
        _DETECTOR_PATH.is_file()
        and Path(SAM_ENC_ONNX_PATH).is_file()
        and Path(SAM_DEC_ONNX_PATH).is_file()
    ),
    reason="MegaDetector / SAM-HQ ONNX weights are missing",
)


@pytest.fixture(scope="module")
def loaded_wrapper():
    if not (
        _DETECTOR_PATH.is_file()
        and Path(SAM_ENC_ONNX_PATH).is_file()
        and Path(SAM_DEC_ONNX_PATH).is_file()
    ):
        pytest.skip("Required MegaDetector / SAM-HQ weights missing")
    wrapper = DetectorSAMHQWrapper(
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
class TestDetectorSamLoading:
    def test_ensure_loaded_succeeds(self, loaded_wrapper):
        assert loaded_wrapper.detector is not None
        assert loaded_wrapper.predictor is not None
        assert not hasattr(loaded_wrapper, "classifier") or loaded_wrapper.classifier is None

    def test_old_name_is_an_alias(self):
        assert SpeciesNetSAMHQWrapper is DetectorSAMHQWrapper

    def test_candidate_cap(self, loaded_wrapper):
        assert loaded_wrapper.candidate_cap == 2 * loaded_wrapper.max_bird_crops


@_skip_no_models
class TestDetectorSamPrediction:
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

    def test_pred_classes_are_animal_proposals(self, loaded_wrapper, first_cr3, first_cr3_decoded):
        _, _, pred_class, pred_score = loaded_wrapper.get_prediction(
            first_cr3_decoded,
            first_cr3,
            wildlife_enabled=True,
            threshold=0.25,
        )
        assert pred_class, "expected the bird in the fixture to be detected"
        assert set(pred_class) == {"animal"}
        assert all(float(s) >= 0.25 for s in pred_score)

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

    def test_wildlife_flag_does_not_filter_proposals(self, loaded_wrapper, first_cr3, first_cr3_decoded):
        """Routing moved to the pipeline (BioCLIP): the wrapper output must not depend on it."""
        a = loaded_wrapper.get_prediction(first_cr3_decoded, first_cr3, wildlife_enabled=False, threshold=0.25)
        b = loaded_wrapper.get_prediction(first_cr3_decoded, first_cr3, wildlife_enabled=True, threshold=0.25)
        assert len(a[2]) == len(b[2])
