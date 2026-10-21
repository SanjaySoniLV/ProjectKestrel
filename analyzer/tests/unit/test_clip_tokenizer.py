"""The stdlib CLIP tokenizer must reproduce open_clip's SimpleTokenizer ids exactly."""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.config import MODELS_DIR
from kestrel_analyzer.ml.clip_tokenizer import CONTEXT_LENGTH, ClipTokenizer, VOCAB_FILENAME

pytestmark = pytest.mark.unit

VOCAB = Path(MODELS_DIR) / "clip" / VOCAB_FILENAME
FIXTURE = Path(__file__).parent.parent / "fixtures" / "clip_tokenizer_cases.json"
_CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]

needs_vocab = pytest.mark.skipif(
    not VOCAB.is_file() or VOCAB.stat().st_size < 100_000, reason="CLIP BPE vocab missing (git lfs pull)"
)


@pytest.fixture(scope="module")
def tok():
    return ClipTokenizer(VOCAB)


@needs_vocab
@pytest.mark.parametrize("case", _CASES, ids=[repr(c["text"])[:40] for c in _CASES])
def test_matches_open_clip(tok, case):
    assert tok.tokenize(case["text"])[0].tolist() == case["tokens"]


@needs_vocab
def test_shape_dtype_and_batch(tok):
    out = tok.tokenize(["a photo of a bird.", "two birds"])
    assert out.shape == (2, CONTEXT_LENGTH) and out.dtype == np.int64
    assert out[0, 0] == tok.sot_token_id == 49406
    assert tok.eot_token_id == 49407
    assert tok("x").shape == (1, CONTEXT_LENGTH)


@needs_vocab
def test_truncation_keeps_eot(tok):
    out = tok.tokenize("heron " * 200)[0]
    assert out[-1] == tok.eot_token_id and (out != 0).all()


@needs_vocab
def test_curly_quotes_and_fullwidth_fold(tok):
    # The ftfy steps this port emulates: curly quotes -> straight, full-width -> ASCII.
    assert tok.encode("Cooper’s hawk") == tok.encode("Cooper's hawk")
    assert tok.encode("ｂｉｒｄ") == tok.encode("bird")
