"""Dependency-free port of open_clip's ``SimpleTokenizer`` (CLIP BPE, context 77).

open_clip's tokenizer needs ``regex`` (for ``\\p{L}`` / ``\\p{N}``) and ``ftfy``.
This port uses only the standard library:

* ``\\p{L}`` is approximated by ``[^\\W\\d_]`` and ``\\p{N}`` by ``\\d``. The two
  differ only on rare characters (letter-like numerics such as Roman numerals
  or superscripts, and combining marks), which never come up in a search box.
* ``ftfy.fix_text`` is approximated by NFC normalisation, straight quotes and
  half-width folding of full-width ASCII, followed (as upstream) by a double
  ``html.unescape``, whitespace collapsing and lower-casing.

``analyzer/tests/fixtures/clip_tokenizer_cases.json`` holds exact open_clip
token ids for a set of strings; ``tests/unit/test_clip_tokenizer.py`` checks
that this port reproduces them.

Adapted from https://github.com/mlfoundations/open_clip (MIT) which is derived
from https://github.com/openai/CLIP (MIT, Copyright (c) 2021 OpenAI).
"""

from __future__ import annotations

import gzip
import html
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Iterable, List, Sequence, Union

import numpy as np

VOCAB_FILENAME = "bpe_simple_vocab_16e6.txt.gz"
CONTEXT_LENGTH = 77
SOT = "<start_of_text>"
EOT = "<end_of_text>"


@lru_cache(maxsize=1)
def bytes_to_unicode() -> dict:
    """Reversible byte -> printable-unicode map used by the CLIP BPE."""
    bs = (
        list(range(ord("!"), ord("~") + 1))
        + list(range(ord("¡"), ord("¬") + 1))
        + list(range(ord("®"), ord("ÿ") + 1))
    )
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, (chr(c) for c in cs)))


def _get_pairs(word: Sequence[str]) -> set:
    return {(word[i], word[i + 1]) for i in range(len(word) - 1)}


_QUOTES = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
})


def _fix_text(text: str) -> str:
    """The parts of ``ftfy.fix_text`` that matter for typed queries."""
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_QUOTES)
    # ftfy.fix_character_width: full-width ASCII (U+FF01..U+FF5E) and the
    # ideographic space become their half-width forms.
    text = "".join(
        chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else (" " if c == "　" else c)
        for c in text
    )
    return text


def basic_clean(text: str) -> str:
    text = _fix_text(text)
    text = html.unescape(html.unescape(text))
    return text.strip()


def whitespace_clean(text: str) -> str:
    return " ".join(text.split()).strip()


class ClipTokenizer:
    """``tokenize(texts) -> int64 [N, 77]``, identical to open_clip's for normal text."""

    def __init__(self, vocab_path: Union[str, Path], context_length: int = CONTEXT_LENGTH):
        self.context_length = int(context_length)
        self.byte_encoder = bytes_to_unicode()
        with gzip.open(vocab_path, "rb") as f:
            merges_raw = f.read().decode("utf-8").split("\n")
        merges_raw = merges_raw[1 : 49152 - 256 - 2 + 1]
        merges = [tuple(m.split()) for m in merges_raw]
        vocab = list(self.byte_encoder.values())
        vocab = vocab + [v + "</w>" for v in vocab]
        vocab.extend("".join(m) for m in merges)
        vocab.extend([SOT, EOT])
        self.encoder = {tok: i for i, tok in enumerate(vocab)}
        self.bpe_ranks = {m: i for i, m in enumerate(merges)}
        self.cache = {SOT: SOT, EOT: EOT}
        self.sot_token_id = self.encoder[SOT]
        self.eot_token_id = self.encoder[EOT]
        # open_clip: special | 's|'t|'re|'ve|'m|'ll|'d | [\p{L}]+ | [\p{N}] | [^\s\p{L}\p{N}]+
        self.pat = re.compile(
            r"<start_of_text>|<end_of_text>|'s|'t|'re|'ve|'m|'ll|'d|[^\W\d_]+|\d|(?:[^\s\w]|_)+",
            re.IGNORECASE,
        )

    def bpe(self, token: str) -> str:
        cached = self.cache.get(token)
        if cached is not None:
            return cached
        word = tuple(token[:-1]) + (token[-1] + "</w>",)
        pairs = _get_pairs(word)
        if not pairs:
            return token + "</w>"
        while True:
            bigram = min(pairs, key=lambda p: self.bpe_ranks.get(p, float("inf")))
            if bigram not in self.bpe_ranks:
                break
            first, second = bigram
            new_word: List[str] = []
            i = 0
            while i < len(word):
                try:
                    j = word.index(first, i)
                except ValueError:
                    new_word.extend(word[i:])
                    break
                new_word.extend(word[i:j])
                i = j
                if word[i] == first and i < len(word) - 1 and word[i + 1] == second:
                    new_word.append(first + second)
                    i += 2
                else:
                    new_word.append(word[i])
                    i += 1
            word = tuple(new_word)
            if len(word) == 1:
                break
            pairs = _get_pairs(word)
        out = " ".join(word)
        if len(self.cache) < 10000:
            self.cache[token] = out
        return out

    def encode(self, text: str) -> List[int]:
        text = whitespace_clean(basic_clean(str(text))).lower()
        ids: List[int] = []
        for token in self.pat.findall(text):
            token = "".join(self.byte_encoder[b] for b in token.encode("utf-8"))
            ids.extend(self.encoder[t] for t in self.bpe(token).split(" "))
        return ids

    def tokenize(self, texts: Union[str, Iterable[str]]) -> np.ndarray:
        if isinstance(texts, str):
            texts = [texts]
        texts = list(texts)
        out = np.zeros((len(texts), self.context_length), dtype=np.int64)
        for i, t in enumerate(texts):
            toks = [self.sot_token_id] + self.encode(t) + [self.eot_token_id]
            if len(toks) > self.context_length:
                toks = toks[: self.context_length]
                toks[-1] = self.eot_token_id
            out[i, : len(toks)] = toks
        return out

    __call__ = tokenize
