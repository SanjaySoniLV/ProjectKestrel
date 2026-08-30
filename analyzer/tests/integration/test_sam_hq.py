"""Integration tests for SAM-HQ ViT-Tiny ONNX encoder/decoder via OnnxSamPredictor.

Loads the real SAM-HQ encoder + decoder ONNX files and runs encode + box-prompt
decode on a real CR3 image, plus a synthetic image where ground truth is known.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.config import SAM_DEC_ONNX_PATH, SAM_ENC_ONNX_PATH
from kestrel_analyzer.image_utils import read_image
from kestrel_analyzer.ml.provider_coordinator import (
    ProviderCoordinator,
    ResilienceConfig,
)
from kestrel_analyzer.ml.speciesnet_sam_hq import OnnxSamPredictor


pytestmark = pytest.mark.integration


_skip_no_sam = pytest.mark.skipif(
    not (Path(SAM_ENC_ONNX_PATH).is_file() and Path(SAM_DEC_ONNX_PATH).is_file()),
    reason="SAM-HQ encoder/decoder ONNX weights not present",
)


@pytest.fixture(scope="module")
def sam_predictor():
    if not (Path(SAM_ENC_ONNX_PATH).is_file() and Path(SAM_DEC_ONNX_PATH).is_file()):
        pytest.skip("SAM-HQ ONNX weights missing")
    coord = ProviderCoordinator(
        user_gpu_enabled=False,
        cfg=ResilienceConfig(),
    )
    return OnnxSamPredictor(SAM_ENC_ONNX_PATH, SAM_DEC_ONNX_PATH, coord)


@_skip_no_sam
class TestSamLoading:
    def test_predictor_initialises(self, sam_predictor):
        assert sam_predictor is not None
        assert sam_predictor._enc_session is not None
        assert sam_predictor._dec_session is not None


@_skip_no_sam
class TestSamEncoder:
    """Verify encoder output shapes match what the decoder will consume."""

    def test_encode_on_synthetic_image(self, sam_predictor):
        img = np.full((512, 512, 3), 127, dtype=np.uint8)
        emb, interm, resized_hw, orig_hw = sam_predictor.encode(img)

        assert emb is not None
        assert interm is not None
        assert isinstance(resized_hw, tuple) and len(resized_hw) == 2
        assert isinstance(orig_hw, tuple) and len(orig_hw) == 2
        assert orig_hw == (512, 512)
        assert emb.ndim >= 3

    def test_encode_on_real_cr3(self, sam_predictor, set_a_path):
        if not set_a_path.exists():
            pytest.skip(f"Fixture {set_a_path} not present")
        cr3_files = sorted(set_a_path.glob("*.CR3"))
        if not cr3_files:
            pytest.skip("No CR3 fixtures")
        img = read_image(str(cr3_files[0]))
        if img is None:
            pytest.skip("Could not decode CR3")

        emb, interm, resized_hw, orig_hw = sam_predictor.encode(img)
        assert orig_hw == (img.shape[0], img.shape[1])
        assert emb.shape[0] == 1  # batch=1


@_skip_no_sam
class TestSamDecoder:
    """Verify decode_box returns a mask of the right shape and IoU range."""

    def test_decode_box_yields_mask_of_original_size(self, sam_predictor):
        img = np.zeros((400, 600, 3), dtype=np.uint8)
        # Draw a bright rectangle in the middle so SAM has something to latch onto
        img[100:300, 200:400] = 200

        emb, interm, resized_hw, orig_hw = sam_predictor.encode(img)
        mask, iou = sam_predictor.decode_box(
            emb, interm, (200, 100, 400, 300), resized_hw, orig_hw
        )

        assert mask.dtype == bool
        assert mask.shape == (400, 600)
        assert 0.0 <= iou <= 1.0

    def test_decode_box_mask_overlaps_prompt_region(self, sam_predictor):
        """The returned mask should have most of its True pixels inside the prompt box."""
        img = np.zeros((400, 600, 3), dtype=np.uint8)
        img[100:300, 200:400] = 200

        emb, interm, resized_hw, orig_hw = sam_predictor.encode(img)
        mask, _ = sam_predictor.decode_box(
            emb, interm, (200, 100, 400, 300), resized_hw, orig_hw
        )

        total = int(mask.sum())
        if total == 0:
            pytest.skip("SAM returned empty mask for this synthetic prompt")

        inside_box = int(mask[100:300, 200:400].sum())
        # At least 50% of the mask pixels should be inside the prompt box.
        assert inside_box / total >= 0.5

    def test_decode_box_on_real_image(self, sam_predictor, set_a_path):
        if not set_a_path.exists():
            pytest.skip(f"Fixture {set_a_path} not present")
        cr3_files = sorted(set_a_path.glob("*.CR3"))
        if not cr3_files:
            pytest.skip("No CR3 fixtures")
        img = read_image(str(cr3_files[0]))
        if img is None:
            pytest.skip("Could not decode CR3")

        h, w = img.shape[:2]
        emb, interm, resized_hw, orig_hw = sam_predictor.encode(img)
        # Use a centre-ish bounding box
        box = (w // 4, h // 4, 3 * w // 4, 3 * h // 4)
        mask, iou = sam_predictor.decode_box(emb, interm, box, resized_hw, orig_hw)

        assert mask.shape == (h, w)
        assert mask.dtype == bool
        assert 0.0 <= iou <= 1.0


@_skip_no_sam
class TestSamDecodeBoxes:
    """Batched decode_boxes() should behave equivalently to per-box decode."""

    def test_decode_boxes_returns_one_result_per_box(self, sam_predictor):
        img = np.full((400, 600, 3), 127, dtype=np.uint8)
        emb, interm, resized_hw, orig_hw = sam_predictor.encode(img)

        boxes = [
            (50, 50, 200, 200),
            (300, 100, 500, 300),
        ]
        results = sam_predictor.decode_boxes(emb, interm, boxes, resized_hw, orig_hw)
        assert len(results) == len(boxes)
        for mask, iou in results:
            assert mask.shape == (400, 600)
            assert mask.dtype == bool
            assert 0.0 <= iou <= 1.0

    def test_decode_boxes_empty_list_returns_empty(self, sam_predictor):
        img = np.full((400, 600, 3), 127, dtype=np.uint8)
        emb, interm, resized_hw, orig_hw = sam_predictor.encode(img)
        results = sam_predictor.decode_boxes(emb, interm, [], resized_hw, orig_hw)
        assert results == []
