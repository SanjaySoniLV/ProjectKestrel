"""Unit tests for SAM-HQ runtime batch-decode fallback caching.

Verifies that once a batch decode_boxes() call fails at runtime (typical
CoreMLExecutionProvider symptom on macOS), the predictor remembers and
routes subsequent multi-box calls straight to per-box decode without
re-attempting the batched path.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.ml.speciesnet_sam_hq import OnnxSamPredictor


pytestmark = pytest.mark.unit


class _StubPredictor:
    """Minimal OnnxSamPredictor stand-in that records which decode path ran."""

    def __init__(self, supports_prompt_batching: bool):
        self._supports_prompt_batching = supports_prompt_batching
        self._batch_decode_runtime_failed = False
        self._batch_unsupported_logged = False
        self.per_box_calls = 0
        self.batched_calls = 0

    def decode_box(self, *_args, **_kwargs):
        self.per_box_calls += 1
        return (object(), 0.5)

    # Reuse the real routing logic so we test the actual code path.
    decode_boxes = OnnxSamPredictor.decode_boxes


def test_decode_boxes_routes_to_per_box_when_runtime_failed_flag_set():
    pred = _StubPredictor(supports_prompt_batching=True)
    pred._batch_decode_runtime_failed = True
    boxes = [(0, 0, 10, 10), (20, 20, 30, 30), (40, 40, 50, 50)]

    results = pred.decode_boxes(None, None, boxes, (256, 256), (400, 400))

    assert len(results) == 3
    assert pred.per_box_calls == 3
    assert pred._batch_unsupported_logged is True


def test_decode_boxes_routes_to_per_box_when_static_unsupported():
    pred = _StubPredictor(supports_prompt_batching=False)
    boxes = [(0, 0, 10, 10), (20, 20, 30, 30)]

    pred.decode_boxes(None, None, boxes, (256, 256), (400, 400))

    assert pred.per_box_calls == 2


def test_decode_boxes_single_box_always_per_box_regardless_of_flag():
    pred = _StubPredictor(supports_prompt_batching=True)
    pred._batch_decode_runtime_failed = False

    pred.decode_boxes(None, None, [(0, 0, 10, 10)], (256, 256), (400, 400))

    assert pred.per_box_calls == 1
