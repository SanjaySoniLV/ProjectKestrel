"""Integration tests for MegaDetector (via SpeciesNetSAMHQWrapper).

Parametrized over every detector in ``DETECTOR_ONNX_PATHS`` (currently
``mdv5a`` and ``mdv6-e``). Each test runs once per detector whose ONNX
weights are present; missing weights are cleanly skipped.
"""

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.config import DETECTOR_ONNX_PATHS
from kestrel_analyzer.image_utils import read_image
from kestrel_analyzer.ml.speciesnet_sam_hq import SpeciesNetSAMHQWrapper


pytestmark = pytest.mark.integration


_ALL_DETECTORS = list(DETECTOR_ONNX_PATHS.keys())


@pytest.fixture(scope="module", params=_ALL_DETECTORS)
def detector_wrapper(request):
    """Module-scoped wrapper with detector + classifier loaded once per detector model.

    Parametrized over every detector defined in ``DETECTOR_ONNX_PATHS`` — each
    test runs once per detector. Skips cleanly if the weights for the current
    parameter aren't present.
    """
    detector_name = request.param
    weights = DETECTOR_ONNX_PATHS[detector_name]
    if not weights.is_file():
        pytest.skip(f"Detector weights for '{detector_name}' not present: {weights}")
    wrapper = SpeciesNetSAMHQWrapper(
        max_bird_crops=5,
        use_gpu=False,  # CPU is sufficient and reliable across CI runners
        detector_name=detector_name,
    )
    wrapper._ensure_speciesnet()
    return wrapper


class TestMegaDetectorLoading:
    """Verify each detector loads without exceptions."""

    def test_detector_loads(self, detector_wrapper):
        assert detector_wrapper.detector is not None

    def test_classifier_loads(self, detector_wrapper):
        assert detector_wrapper.classifier is not None

    def test_detector_has_predict_method(self, detector_wrapper):
        assert callable(getattr(detector_wrapper.detector, "predict", None))
        assert callable(getattr(detector_wrapper.detector, "preprocess", None))

    def test_detector_name_recorded_on_wrapper(self, detector_wrapper):
        assert detector_wrapper.detector_name in _ALL_DETECTORS


class TestMegaDetectorInference:
    """Run each detector against real fixtures and validate output shape."""

    def _first_cr3(self, set_a_path):
        if not set_a_path.exists():
            pytest.skip(f"Fixture {set_a_path} not present")
        cr3_files = sorted(set_a_path.glob("*.CR3"))
        if not cr3_files:
            pytest.skip("No CR3 files in set_a_fresh")
        return cr3_files[0]

    def test_detector_returns_dict_with_detections(self, detector_wrapper, set_a_path):
        cr3 = self._first_cr3(set_a_path)
        rgb = read_image(str(cr3))
        if rgb is None:
            pytest.skip(f"Could not decode {cr3}")

        img_pil = Image.fromarray(rgb)
        det_input = detector_wrapper.detector.preprocess(img_pil)
        result = detector_wrapper.detector.predict(str(cr3), det_input)

        assert isinstance(result, dict)
        assert "detections" in result
        assert isinstance(result["detections"], list)

    def test_detection_entries_have_expected_keys(self, detector_wrapper, set_a_path):
        cr3 = self._first_cr3(set_a_path)
        rgb = read_image(str(cr3))
        if rgb is None:
            pytest.skip(f"Could not decode {cr3}")

        img_pil = Image.fromarray(rgb)
        det_input = detector_wrapper.detector.preprocess(img_pil)
        result = detector_wrapper.detector.predict(str(cr3), det_input)

        for det in result["detections"]:
            assert "label" in det
            assert "conf" in det
            assert "bbox" in det
            assert det["label"] in ("animal", "person", "vehicle", "unknown")
            assert 0.0 <= float(det["conf"]) <= 1.0
            assert len(det["bbox"]) == 4

    def test_detection_bboxes_normalized(self, detector_wrapper, set_a_path):
        """Every bbox should be normalized to [0,1] (xmin, ymin, width, height)."""
        cr3 = self._first_cr3(set_a_path)
        rgb = read_image(str(cr3))
        if rgb is None:
            pytest.skip(f"Could not decode {cr3}")

        img_pil = Image.fromarray(rgb)
        det_input = detector_wrapper.detector.preprocess(img_pil)
        result = detector_wrapper.detector.predict(str(cr3), det_input)

        for det in result["detections"]:
            xmin, ymin, w, h = det["bbox"]
            assert -0.01 <= xmin <= 1.01
            assert -0.01 <= ymin <= 1.01
            assert 0.0 <= w <= 1.01
            assert 0.0 <= h <= 1.01

    def test_blank_image_yields_no_high_conf_detections(self, detector_wrapper):
        """A pure-white image should not produce confident animal detections."""
        blank = np.full((640, 640, 3), 255, dtype=np.uint8)
        img_pil = Image.fromarray(blank)
        det_input = detector_wrapper.detector.preprocess(img_pil)
        result = detector_wrapper.detector.predict("blank.jpg", det_input)

        high_conf = [d for d in result["detections"]
                     if float(d.get("conf", 0.0)) >= 0.5
                     and str(d.get("label")) == "animal"]
        assert len(high_conf) == 0

    def test_detector_repeatable(self, detector_wrapper, set_a_path):
        """Running the same image through detector twice yields identical detections."""
        cr3 = self._first_cr3(set_a_path)
        rgb = read_image(str(cr3))
        if rgb is None:
            pytest.skip(f"Could not decode {cr3}")

        img_pil = Image.fromarray(rgb)
        det_input_1 = detector_wrapper.detector.preprocess(img_pil)
        result_1 = detector_wrapper.detector.predict(str(cr3), det_input_1)

        det_input_2 = detector_wrapper.detector.preprocess(img_pil)
        result_2 = detector_wrapper.detector.predict(str(cr3), det_input_2)

        assert len(result_1["detections"]) == len(result_2["detections"])
        for a, b in zip(result_1["detections"], result_2["detections"]):
            assert a["label"] == b["label"]
            assert float(a["conf"]) == pytest.approx(float(b["conf"]), abs=1e-4)
