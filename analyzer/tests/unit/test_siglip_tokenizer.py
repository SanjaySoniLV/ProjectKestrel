"""The stdlib SigLIP 2 tokenizer must reproduce open_clip's ViT-B-16-SigLIP2 ids exactly."""

import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.ml.siglip_tokenizer import (
    CONTEXT_LENGTH,
    SiglipTokenizer,
    clean,
    default_asset_path,
)

pytestmark = pytest.mark.unit

ASSET = default_asset_path()
FIXTURE = Path(__file__).parent.parent / "fixtures" / "siglip_tokenizer_cases.json"
_CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]

needs_asset = pytest.mark.skipif(
    not ASSET.is_file() or ASSET.stat().st_size < 100_000,
    reason="SigLIP tokenizer asset missing (git lfs pull)",
)


@pytest.fixture(scope="module")
def tok():
    return SiglipTokenizer(ASSET)


@needs_asset
def test_fixture_is_substantial():
    assert len(_CASES) >= 300


@needs_asset
@pytest.mark.parametrize("case", _CASES, ids=[repr(c["text"])[:40] for c in _CASES])
def test_matches_open_clip(tok, case):
    assert tok(case["text"])[0].tolist() == case["tokens"]


@needs_asset
def test_shape_dtype_and_specials(tok):
    out = tok(["a bird in flight", "two birds fighting"])
    assert out.shape == (2, CONTEXT_LENGTH) and out.dtype == np.int64
    assert (tok.pad_id, tok.eos_id, tok.bos_id, tok.unk_id) == (0, 1, 2, 3)
    ids = tok.encode("a bird in flight")
    assert out[0, : len(ids) + 1].tolist() == ids + [1]
    assert (out[0, len(ids) + 1 :] == 0).all()
    assert tok("x").shape == (1, CONTEXT_LENGTH)
    assert tok("").tolist() == [[1] + [0] * (CONTEXT_LENGTH - 1)]


@needs_asset
def test_truncation_keeps_eos(tok):
    out = tok("heron " * 200)[0]
    assert out[-1] == tok.eos_id and (out != 0).all()


@needs_asset
def test_byte_fallback_and_decode(tok):
    # A character outside the vocab becomes its UTF-8 byte tokens.
    ids = tok.encode("\U00020001")  # CJK Ext. B, not a Gemma piece
    assert [tok.pieces[i] for i in ids] == ["<0xF0>", "<0xA0>", "<0x80>", "<0x81>"]
    assert tok.decode(tok("bald eagle \U00020001 🪶")[0]) == "bald eagle \U00020001 🪶"


def test_clean_matches_open_clip_canonicalize():
    assert clean("  Cooper’s   Hawk!! ") == "coopers hawk"
    assert clean("Red_winged_Blackbird") == "red winged blackbird"
    assert clean("ＢＩＲＤ　ｉｎ　ｆｌｉｇｈｔ") == "bird in flight"
    assert clean("&amp; caf&eacute; P&EACUTE;REZ") == "café pérez"
    assert clean("ﬁnch") == "finch"


@needs_asset
def test_subset_remap(tok):
    subset = tok.subset_ids
    assert (np.diff(subset) > 0).all()
    for i in (tok.pad_id, tok.eos_id, tok.bos_id, tok.unk_id):
        assert i in subset
    # Every byte-fallback token is kept.
    byte_ids = [i for i, p in enumerate(tok.pieces) if len(p) == 6 and p.startswith("<0x")]
    assert np.isin(byte_ids, subset).all()
    # Row i of the pruned table is Gemma id subset[i].
    assert (tok.to_subset(subset) == np.arange(len(subset))).all()
    unk_row = int(tok.to_subset(tok.unk_id))
    assert subset[unk_row] == tok.unk_id
    # Lowercase word pieces are kept; others (uppercase, punctuation) fall to <unk>.
    ids = tok("a bird in flight")
    sub = tok.to_subset(ids)
    assert sub.shape == ids.shape and sub.dtype == np.int64
    assert (subset[sub] == ids).all()
    dropped = [i for i, p in enumerate(tok.pieces[:5000]) if p.startswith("<unused")]
    assert dropped and (tok.to_subset(dropped) == unk_row).all()
    assert tok.to_subset(np.array([[0, 1], [2, 3]])).shape == (2, 2)


@needs_asset
def test_every_fixture_maps_into_subset_without_loss(tok):
    # Cleaned queries only contain lowercase text, so nearly all tokens survive the
    # pruning; anything that doesn't must be a CJK/emoji/symbol piece.
    for case in _CASES:
        ids = np.array(case["tokens"])
        lost = ids[tok.subset_ids[tok.to_subset(ids)] != ids]
        for i in lost:
            p = tok.pieces[int(i)].lstrip("▁")
            assert not p.isascii() or not p.isalnum(), (case["text"], p)
