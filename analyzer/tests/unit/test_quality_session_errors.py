"""Regression tests for QualityClassifier.classify error handling.

classify() used a single blanket ``except Exception: return -1.0`` around
preprocessing AND session.run(). A dead GPU/ONNX session was therefore swallowed
into a -1.0 for every remaining image -- the coordinator never demoted to CPU
and the run reported "complete" with corrupted quality scores.

The fix keeps input/output problems graceful (still -1.0) but lets session/ONNX
errors propagate so the pipeline records the error and the coordinator can
recover.

These are unit tests: they bypass __init__ (which needs the ONNX weights) and
inject a stub session, so they run without model files.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.ml.quality import QualityClassifier

pytestmark = pytest.mark.unit


def _classifier_with_session(session):
    qc = QualityClassifier.__new__(QualityClassifier)  # skip __init__ (needs weights)
    qc.session = session
    qc._input_name = "input"
    # Identity normalization so we don't need the percentile CSV loaded by __init__.
    qc._normalize_quality_to_percentile = lambda v: v
    return qc


def _img_and_mask():
    return (np.full((32, 32, 3), 128, dtype=np.uint8),
            np.ones((32, 32), dtype=np.uint8))


def test_session_error_propagates_not_swallowed():
    class DeadSession:
        def run(self, *_a, **_k):
            raise RuntimeError("GPU device instance has been suspended (0x887A0005)")

    qc = _classifier_with_session(DeadSession())
    img, mask = _img_and_mask()
    with pytest.raises(RuntimeError):
        qc.classify(img, mask)


def test_malformed_input_still_returns_sentinel():
    class DeadSession:
        def run(self, *_a, **_k):
            raise AssertionError("session must not be reached for bad input")

    qc = _classifier_with_session(DeadSession())
    # None makes _preprocess (cv2.cvtColor) raise -> graceful -1.0, session not reached.
    assert qc.classify(None, None) == -1.0


def test_all_zero_mask_is_valid_and_reaches_session():
    """An all-zero mask is not malformed: _preprocess bitwise_and's it and
    session.run still runs. classify must not treat it as the sentinel path."""
    class MarkerSession:
        def run(self, *_a, **_k):
            return [np.array([[0.25]], dtype=np.float32)]

    qc = _classifier_with_session(MarkerSession())
    img = np.full((32, 32, 3), 128, dtype=np.uint8)
    mask = np.zeros((32, 32), dtype=np.uint8)
    assert qc.classify(img, mask) == pytest.approx(0.25)


def test_normal_run_returns_value():
    class OkSession:
        def run(self, *_a, **_k):
            return [np.array([[0.5]], dtype=np.float32)]

    qc = _classifier_with_session(OkSession())
    img, mask = _img_and_mask()
    assert qc.classify(img, mask) == pytest.approx(0.5)


def test_malformed_output_returns_sentinel():
    class BadOutputSession:
        def run(self, *_a, **_k):
            return ["not-a-number-structure"]

    qc = _classifier_with_session(BadOutputSession())
    img, mask = _img_and_mask()
    assert qc.classify(img, mask) == -1.0
