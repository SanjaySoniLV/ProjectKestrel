"""Build the runtime SigLIP 2 tokenizer asset from the HF ``tokenizer.json``.

    python export_siglip_tokenizer.py [--tokenizer-json PATH] [--out ../../analyzer/models/siglip]
    python export_siglip_tokenizer.py --fixture   # regenerate the unit-test fixture

Writes ``siglip2_tokenizer.json.gz``, consumed by
``analyzer/kestrel_analyzer/ml/siglip_tokenizer.py`` (stdlib + numpy only). The
source is the Gemma BPE that open_clip's ``ViT-B-16-SigLIP2`` uses
(``timm/ViT-B-16-SigLIP2`` on the HF hub: 256000 pieces, ~580k merges,
byte_fallback, fuse_unk, normalizer ``' ' -> '▁'``, no dummy prefix).

Asset layout (gzipped JSON, format ``kestrel-siglip2-tokenizer/1``):

  pieces          list[str], the vocab by id
  special         {"pad": 0, "eos": 1, "bos": 2, "unk": 3}
  byte_ids        list[256], id of ``<0xXX>`` per byte, -1 if absent (0x09 is
                  absent: ``\\t`` is a real piece)
  merges          base64 little-endian int32 arrays, in rank order:
                    left, right   ids of the merged pair
                    result_delta  id of left+right, delta-coded against the
                                  previous merge (result ids are ~monotone in
                                  rank, so the deltas gzip to ~100 kB)
  added           added tokens that can survive open_clip's cleaning (no ASCII
                  punctuation, no whitespace): the ``▁▁``..``▁``*31 runs. HF
                  splits these out of the raw text before BPE.
  subset          base64 int32, sorted Gemma ids kept in the pruned text
                  embedding table: specials + byte-fallback tokens + every
                  piece matching ``^▁?[a-z0-9à-öø-ÿ]+$`` or ``^▁+$``. Row i of the
                  pruned table is Gemma id subset[i]; other ids map to <unk>.

``--fixture`` instead writes ``analyzer/tests/fixtures/siglip_tokenizer_cases.json``:
exact ids from ``open_clip.get_tokenizer("ViT-B-16-SigLIP2")`` (needs open_clip +
transformers) for bird names from ``analyzer/models/birds/birds_global.csv`` and
hand-written edge cases.

Dev-only (stdlib + numpy; open_clip for ``--fixture``). Default ``--tokenizer-json`` is the HF cache copy
that ``open_clip.get_tokenizer("ViT-B-16-SigLIP2")`` downloads.
"""
import argparse
import base64
import csv
import random
import gzip
import json
import os
import re
import string

import numpy as np

DEFAULT_TOKENIZER_JSON = os.path.expanduser(
    "~/.cache/huggingface/hub/models--timm--ViT-B-16-SigLIP2/snapshots/"
    "eee10eff6dd8cabae2d7f379d4e8cfcd352030aa/tokenizer.json"
)
HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.normpath(os.path.join(HERE, "..", "..", "analyzer", "models", "siglip"))
FILENAME = "siglip2_tokenizer.json.gz"
FORMAT = "kestrel-siglip2-tokenizer/1"

SUBSET_RE = re.compile(r"^▁?[a-z0-9à-öø-ÿ]+$|^▁+$")


def _b64(arr: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(arr, dtype="<i4").tobytes()).decode("ascii")


def build(tokenizer_json: str) -> dict:
    with open(tokenizer_json, encoding="utf-8") as f:
        tj = json.load(f)
    model = tj["model"]
    assert model["type"] == "BPE" and model["byte_fallback"] and model["fuse_unk"], model.keys()
    assert model.get("continuing_subword_prefix") is None and model.get("end_of_word_suffix") is None
    assert not model.get("ignore_merges") and model.get("dropout") is None
    assert tj["normalizer"] == {"type": "Replace", "pattern": {"String": " "}, "content": "▁"}, tj["normalizer"]
    vocab = model["vocab"]
    pieces = [None] * len(vocab)
    for piece, i in vocab.items():
        pieces[i] = piece
    assert all(p is not None for p in pieces)

    merges = model["merges"]
    merges = [tuple(m.split(" ", 1)) if isinstance(m, str) else tuple(m) for m in merges]
    left = np.array([vocab[a] for a, _ in merges], dtype=np.int64)
    right = np.array([vocab[b] for _, b in merges], dtype=np.int64)
    result = np.array([vocab[a + b] for a, b in merges], dtype=np.int64)
    # HF keeps the first (lowest-rank) entry if a pair repeats; there are none, but be safe.
    keys = left * len(pieces) + right
    assert len(np.unique(keys)) == len(keys), "duplicate merge pair"

    special = {name: vocab[f"<{name}>"] for name in ("pad", "eos", "bos", "unk")}
    byte_ids = [vocab.get(f"<0x{b:02X}>", -1) for b in range(256)]

    punct = set(string.punctuation)
    added = [
        {"id": t["id"], "content": t["content"]}
        for t in tj["added_tokens"]
        if t["content"] and not (set(t["content"]) & punct) and not any(c.isspace() for c in t["content"])
    ]
    for t in tj["added_tokens"]:
        assert not (t["normalized"] or t["lstrip"] or t["rstrip"] or t["single_word"]), t

    keep = set(special.values()) | {i for i in byte_ids if i >= 0}
    keep |= {i for i, p in enumerate(pieces) if SUBSET_RE.match(p)}
    subset = np.array(sorted(keep), dtype=np.int64)

    return {
        "format": FORMAT,
        "source": "timm/ViT-B-16-SigLIP2 tokenizer.json (Gemma BPE, Apache-2.0 / Gemma terms)",
        "context_length": 64,
        "special": special,
        "byte_ids": byte_ids,
        "pieces": pieces,
        "merges": {
            "count": int(len(merges)),
            "left": _b64(left),
            "right": _b64(right),
            "result_delta": _b64(np.diff(result, prepend=0)),
        },
        "added": added,
        "subset": _b64(subset),
        "subset_size": int(len(subset)),
    }


BIRDS_CSV = os.path.normpath(os.path.join(HERE, "..", "..", "analyzer", "models", "birds", "birds_global.csv"))
FIXTURE = os.path.normpath(os.path.join(HERE, "..", "..", "analyzer", "tests", "fixtures", "siglip_tokenizer_cases.json"))

HAND_CASES = [
    "", " ", "   ", "a", "A", "bird", "a photo of a bird.", "a bird in flight", "two birds fighting",
    "juvenile bald eagle on a post", "a heron standing in shallow water", "hawk eating a fish",
    "flock of starlings at dusk", "owl at night", "bird with a worm in its beak", "nest with eggs",
    "duck swimming", "male and female mallards", "close-up of a hummingbird's eye",
    "blurry photo", "sharp, well-exposed portrait", "backlit silhouette against the sky",
    "bird on a feeder in the snow", "eagle diving for prey!!!", "a robin... maybe?",
    "kingfisher (perched) [blue]", "50 geese", "3 crows on a wire", "2nd-year gull", "1st winter",
    "2026-05-12 sunrise", "f/5.6 1/2000s ISO 800", "100%", "#birding @audubon", "$5 & 10 cents",
    "Cooper's Hawk", "Cooper’s Hawk", "Cooper`s hawk", "“quoted” bird", "Black-capped Chickadee",
    "great-tailed grackle", "Red_winged_Blackbird", "o'clock", "rock-pigeon -- city", "a\tbird\nin\rflight",
    "Pájaro", "Rüppell's Vulture", "Rüppell’s Griffon", "Bewick's Wren", "Ñandú", "Œdicnème criard",
    "Gänsegeier", "Pássaro-preto", "Cigüeña blanca", "Ibis à face nue", "Mésange charbonnière",
    "Grünfink", "Ørn", "ÆØÅ æøå", "İstanbul kuşu", "ﬁnch ﬂock", "ＢＩＲＤ　ｉｎ　ｆｌｉｇｈｔ",
    "鳥", "白头海雕", "ハクトウワシ", "독수리", "Орёл", "αετός", "عقاب", "גשר", "पक्षी",
    "🦅", "bird 🐦 photo", "🦆🦆🦆", "👍🏽 nice", "❤\ufe0f heron", "☀\ufe0f sunny", "\u200bzero width",
    "tab\tseparated", "multiple     spaces   here", "  leading and trailing  ", "line\nbreak",
    "&amp; &lt;b&gt;bold&lt;/b&gt;", "P&EACUTE;REZ", "caf&eacute;", "&#233;", "<b>html</b> tag",
    "x" * 100, "bird " * 40, "the quick brown fox jumps over the lazy dog " * 4,
    "Pneumonoultramicroscopicsilicovolcanoconiosis", "supercalifragilisticexpialidocious bird",
    "a" * 300, "▁", "▁▁ two marks", "word▁▁▁▁word", "_underscore_", "under__score", "UPPER CASE BIRD",
    "MiXeD cAsE", "ALL-CAPS-HYPHEN", "e-mail", "pre-/post-breeding", "Mr. Owl's", "mother's day",
    "don't shoot", "it's a bird", "rock'n'roll", "co-operative", "1,000 birds", "3.14 owls",
    "½ bird", "²nd", "Ⅻ", "℃ cold", "™ brand", "© photo", "naïve", "façade", "résumé", "coöperate",
    "\u0301accent alone", "e\u0301 decomposed", "Å vs A\u030a",
]

QUERY_TEMPLATES = [
    "a photo of a {}.", "{} in flight", "juvenile {}", "two {}s", "{} on a branch", "{}",
]


def fixture_texts() -> list:
    rng = random.Random(20261022)
    with open(BIRDS_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    commons = [r["canonical_common_name"] for r in rows if r["canonical_common_name"]]
    scis = [r["scientific_name"] for r in rows if r["scientific_name"]]
    aliases = [a.strip() for r in rows for a in (r.get("aliases") or "").split(";") if a.strip()]
    # Prefer names with punctuation / non-ASCII, then a random sample.
    tricky = [c for c in commons if re.search(r"[^A-Za-z ]", c)]
    texts = list(HAND_CASES)
    texts += rng.sample(tricky, min(80, len(tricky)))
    texts += rng.sample(commons, 120)
    texts += rng.sample(scis, 60)
    texts += rng.sample(aliases, min(20, len(aliases)))
    texts += [rng.choice(QUERY_TEMPLATES).format(c.lower()) for c in rng.sample(commons, 40)]
    seen, out = set(), []
    for t in texts:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def write_fixture(path: str) -> None:
    import open_clip  # dev venv only

    tok = open_clip.get_tokenizer("ViT-B-16-SigLIP2")
    texts = fixture_texts()
    ids = tok(texts).tolist()
    lines = [json.dumps({"text": t, "tokens": row}, ensure_ascii=False) for t, row in zip(texts, ids)]
    header = json.dumps('open_clip ViT-B-16-SigLIP2 (HF GemmaTokenizer, _clean_canonicalize), context_length=64')
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write('{\n"tokenizer": ' + header + ',\n"cases": [\n' + ",\n".join(lines) + "\n]\n}\n")
    print(f"wrote {path}: {len(texts)} cases")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tokenizer-json", default=DEFAULT_TOKENIZER_JSON)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--fixture", nargs="?", const=FIXTURE, default=None,
                    help="write the unit-test fixture (default path: %(const)s) instead of the asset")
    args = ap.parse_args()
    if args.fixture:
        write_fixture(args.fixture)
        return
    asset = build(args.tokenizer_json)
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, FILENAME)
    data = json.dumps(asset, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    # mtime=0 keeps the output byte-identical across runs.
    with open(path, "wb") as f:
        f.write(gzip.compress(data, compresslevel=9, mtime=0))
    print(f"wrote {path}: {os.path.getsize(path) / 1e6:.2f} MB "
          f"({len(asset['pieces'])} pieces, {asset['merges']['count']} merges, "
          f"subset {asset['subset_size']})")


if __name__ == "__main__":
    main()
