"""Integration tests for QualityClassifier (custom ONNX quality model).

Loads quality.onnx + normalization data and runs inference on synthetic
and real crops.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.config import (
    QUALITY_NORMALIZATION_DATA_PATH,
    QUALITYCLASSIFIER_PATH,
)
from kestrel_analyzer.image_utils import read_image
from kestrel_analyzer.ml.provider_coordinator import (
    ProviderCoordinator,
    ResilienceConfig,
)
from kestrel_analyzer.ml.quality import QualityClassifier


pytestmark = pytest.mark.integration


_skip_no_model = pytest.mark.skipif(
    not Path(QUALITYCLASSIFIER_PATH).is_file(),
    reason="Quality classifier quality.onnx not present",
)


@pytest.fixture(scope="module")
def quality_classifier():
    if not Path(QUALITYCLASSIFIER_PATH).is_file():
        pytest.skip("Quality classifier weights missing")
    coord = ProviderCoordinator(
        user_gpu_enabled=False,
        cfg=ResilienceConfig(),
    )
    return QualityClassifier(
        str(QUALITYCLASSIFIER_PATH),
        normalization_data_path=str(QUALITY_NORMALIZATION_DATA_PATH)
            if Path(QUALITY_NORMALIZATION_DATA_PATH).is_file() else None,
        coord=coord,
    )


@_skip_no_model
class TestQualityClassifierLoading:
    def test_classifier_loads(self, quality_classifier):
        assert quality_classifier is not None
        assert quality_classifier.session is not None

    def test_input_name_resolved(self, quality_classifier):
        assert isinstance(quality_classifier._input_name, str)
        assert len(quality_classifier._input_name) > 0


@_skip_no_model
class TestQualityClassifierInference:
    def test_classify_returns_float(self, quality_classifier):
        img = np.full((512, 512, 3), 128, dtype=np.uint8)
        mask = np.ones((512, 512), dtype=np.uint8)
        result = quality_classifier.classify(img, mask)
        assert isinstance(result, float)

    def test_score_in_unit_range_or_error_sentinel(self, quality_classifier):
        """Quality returns a normalized percentile in [0,1] or -1.0 on error."""
        img = np.full((512, 512, 3), 128, dtype=np.uint8)
        mask = np.ones((512, 512), dtype=np.uint8)
        score = quality_classifier.classify(img, mask)
        assert score == -1.0 or (0.0 <= score <= 1.0)

    def test_repeatable_score_same_input(self, quality_classifier):
        img = np.full((512, 512, 3), 128, dtype=np.uint8)
        mask = np.ones((512, 512), dtype=np.uint8)
        a = quality_classifier.classify(img, mask)
        b = quality_classifier.classify(img, mask)
        assert a == pytest.approx(b, abs=1e-5)

    def test_classify_real_image_crop(self, quality_classifier, set_a_path):
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
        cy, cx = h // 2, w // 2
        crop = img[cy - s // 2: cy + s // 2, cx - s // 2: cx + s // 2]
        mask = np.ones(crop.shape[:2], dtype=np.uint8)

        score = quality_classifier.classify(crop, mask)
        assert score == -1.0 or (0.0 <= score <= 1.0)

    def test_zero_mask_handled_gracefully(self, quality_classifier):
        """An empty mask should not crash — classifier returns -1.0 sentinel on error
        or a valid number, never throws."""
        img = np.full((512, 512, 3), 128, dtype=np.uint8)
        mask = np.zeros((512, 512), dtype=np.uint8)
        result = quality_classifier.classify(img, mask)
        # Either a finite quality or the documented -1.0 sentinel
        assert isinstance(result, float)
