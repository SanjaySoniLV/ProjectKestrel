"""Integration tests for QualityClassifier (custom ONNX quality model).

Loads quality.onnx + normalization data and runs real inference.

These tests previously handed ``classify()`` a hand-built 512x512 array and had
never passed. ``QualityClassifier._preprocess`` computes a Sobel edge map and
masks it -- it does **not** resize. The resize to the model's input size lives in
the *caller*: the pipeline calls ``SpeciesNetSAMHQWrapper.get_square_crop(...,
resize=True)`` before handing the crop over. Calling the classifier directly
therefore skipped the resize entirely and onnxruntime rejected the input with
``Got: 512 Expected: 1024``.

Git history dates the resize five weeks *before* these tests were written, and
the model file is byte-identical since before that, so this was never drift --
they were broken on arrival and nothing ran them (no workflow has a
``pull_request`` trigger, and the one dev workflow that runs this lane marks the
step ``continue-on-error``).

So fixtures here go through ``get_square_crop`` rather than hard-coding a size.
If the pipeline's input size changes again, these tests follow it instead of
silently diverging.

The old fixtures were also a flat grey square, whose Sobel edge map is entirely
zero -- the score came back identical whatever the mask was, so several tests
would have gone green while asserting nothing about the model. The synthetic
fixture is now textured, and ``test_textured_scores_differently_than_flat`` pins
that the input actually reaches the model.
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
from kestrel_analyzer.ml.speciesnet_sam_hq import SpeciesNetSAMHQWrapper


pytestmark = pytest.mark.integration


_skip_no_model = pytest.mark.skipif(
    not Path(QUALITYCLASSIFIER_PATH).is_file(),
    reason="Quality classifier quality.onnx not present",
)


def _model_ready_crop(img, mask=None):
    """Crop and resize exactly as the pipeline does before calling classify().

    Goes through the production ``get_square_crop`` rather than reshaping here,
    so the test cannot drift from the pipeline's input size. The wrapper is
    built via ``__new__``: cropping and resizing touch no model state, so this
    avoids loading SAM-HQ just to reuse the geometry.
    """
    if mask is None:
        mask = np.ones(img.shape[:2], dtype=np.uint8)
    wrapper = SpeciesNetSAMHQWrapper.__new__(SpeciesNetSAMHQWrapper)
    return wrapper.get_square_crop(mask, img, resize=True)


def _textured_image(size=900, seed=7):
    """A crop with real gradient content.

    Flat fill produces an all-zero Sobel response, so the model returns the same
    value no matter what else changes -- which is how the previous fixtures could
    have passed without exercising anything.
    """
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(size, size, 3), dtype=np.uint8)


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
class TestCropContract:
    """Where the resize lives, and what happens when it is skipped.

    This is the contract the old tests violated, so it is worth pinning
    explicitly rather than leaving implicit in the other fixtures.
    """

    def test_crop_helper_produces_what_the_model_declares(self, quality_classifier):
        crop, mask = _model_ready_crop(_textured_image())
        expected = quality_classifier.session.get_inputs()[0].shape

        assert crop.shape[0] == crop.shape[1], "the model takes a square crop"
        assert crop.shape[:2] == mask.shape[:2]
        # Dimensions 1 and 2 of the declared input; index 0 is batch and may be
        # symbolic. Only compare the ones the model fixes to an integer.
        for axis, declared in zip(crop.shape[:2], expected[1:3]):
            if isinstance(declared, int):
                assert axis == declared, (
                    f"get_square_crop produced {crop.shape[:2]} but the model "
                    f"declares {expected}; the fixture and the pipeline have drifted"
                )

    def test_classifier_does_not_resize_its_input(self, quality_classifier):
        """Resizing is the caller's job — skipping it is a hard error, not a warning.

        This is exactly the mistake the previous fixtures made, and pinning it
        keeps the reason visible: _preprocess only builds the masked edge map.
        """
        raw = _textured_image(size=512)
        with pytest.raises(Exception):
            quality_classifier.classify(raw, np.ones((512, 512), dtype=np.uint8))


@_skip_no_model
class TestQualityClassifierInference:
    def test_classify_returns_float(self, quality_classifier):
        crop, mask = _model_ready_crop(_textured_image())
        result = quality_classifier.classify(crop, mask)
        assert isinstance(result, float)

    def test_score_in_unit_range_or_error_sentinel(self, quality_classifier):
        """Quality returns a normalized percentile in [0,1] or -1.0 on error."""
        crop, mask = _model_ready_crop(_textured_image())
        score = quality_classifier.classify(crop, mask)
        assert score == -1.0 or (0.0 <= score <= 1.0)

    def test_repeatable_score_same_input(self, quality_classifier):
        crop, mask = _model_ready_crop(_textured_image())
        a = quality_classifier.classify(crop, mask)
        b = quality_classifier.classify(crop, mask)
        assert a == pytest.approx(b, abs=1e-5)

    def test_textured_scores_differently_than_flat(self, quality_classifier):
        """The input must actually reach the model.

        A flat fill has no gradients, so its edge map is all zeros. If a textured
        crop scored the same, the pipeline would be feeding the model something
        constant and no other assertion here would mean anything.
        """
        flat_crop, flat_mask = _model_ready_crop(
            np.full((900, 900, 3), 128, dtype=np.uint8)
        )
        tex_crop, tex_mask = _model_ready_crop(_textured_image())

        flat_score = quality_classifier.classify(flat_crop, flat_mask)
        tex_score = quality_classifier.classify(tex_crop, tex_mask)

        assert flat_score != pytest.approx(tex_score, abs=1e-3), (
            f"a flat crop and a textured crop scored the same ({flat_score}); "
            "the classifier is not responding to its input"
        )

    def test_classify_real_image_crop(self, quality_classifier, set_a_path):
        """The path that matters: a real RAW frame through the real crop code."""
        if not set_a_path.exists():
            pytest.skip(f"Fixture {set_a_path} not present")
        cr3_files = sorted(set_a_path.glob("*.CR3"))
        if not cr3_files:
            pytest.skip("No CR3 fixtures")
        img = read_image(str(cr3_files[0]))
        if img is None:
            pytest.skip("Could not decode CR3")

        # A centre region stands in for a detection mask.
        h, w = img.shape[:2]
        mask = np.zeros((h, w), dtype=np.uint8)
        mask[h // 4: 3 * h // 4, w // 4: 3 * w // 4] = 1

        crop, crop_mask = _model_ready_crop(img, mask)
        score = quality_classifier.classify(crop, crop_mask)
        assert score == -1.0 or (0.0 <= score <= 1.0)

    def test_zero_mask_handled_gracefully(self, quality_classifier):
        """An empty mask must not crash — a sentinel or a number, never a throw.

        get_square_crop falls back to the full frame when the mask is empty, so
        this still produces a correctly sized crop; the empty mask then zeroes
        the edge map at the classifier.
        """
        crop, _ = _model_ready_crop(_textured_image())
        zero_mask = np.zeros(crop.shape[:2], dtype=np.uint8)
        result = quality_classifier.classify(crop, zero_mask)
        assert isinstance(result, float)
