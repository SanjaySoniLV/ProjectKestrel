"""Dependency-free port of open_clip's SigLIP 2 text tokenizer (Gemma BPE, context 64).

open_clip's ``get_tokenizer("ViT-B-16-SigLIP2")`` wraps transformers' Gemma fast
tokenizer and cleans each string with ``_clean_canonicalize``:
``ftfy.fix_text`` + double ``html.unescape`` + strip, then ``canonicalize_text``
('_' -> ' ', drop ``string.punctuation``, lower-case, collapse whitespace). The
ids are the BPE pieces followed by ``<eos>`` (1), truncated to 63 + ``<eos>`` and
right-padded with ``<pad>`` (0) to 64. No ``<bos>``.

This port uses only the standard library and numpy:

* The BPE is the HF ``tokenizers`` algorithm: one "word" per segment (the
  normalizer turns spaces into '▁' before the space pre-tokenizer runs, so the
  whole query is one word), symbols are characters, the lowest-rank pair is
  merged first (leftmost on ties), characters missing from the vocab fall back
  to their UTF-8 bytes ``<0xXX>``, and a character with no byte tokens becomes
  ``<unk>`` (consecutive ones fused). Added tokens that survive cleaning (runs
  of 2+ '▁' typed literally) are split out first, longest match, as HF does.
* ``ftfy.fix_text`` is reproduced except for its mojibake repair
  (``fix_encoding``): HTML-entity decoding including ftfy's all-caps entities,
  C1 controls as Windows-1252, Latin ligatures, half/full-width folding, curly
  quotes, line breaks, surrogates, terminal escapes, control characters and NFC,
  repeated until stable as ftfy does. Only text that is already mojibake (e.g.
  ``"cafÃ©"``) tokenizes differently from open_clip.

The asset ``models/siglip/siglip2_tokenizer.json.gz`` is built by
``experiments/bioclip/export_siglip_tokenizer.py``; it also lists the Gemma ids
kept in the app's pruned text-embedding table (:meth:`SiglipTokenizer.to_subset`).
``analyzer/tests/fixtures/siglip_tokenizer_cases.json`` holds exact open_clip ids;
``tests/unit/test_siglip_tokenizer.py`` checks this port reproduces them.
"""

from __future__ import annotations

import base64
import gzip
import heapq
import html
import html.entities
import json
import re
import string
import threading
import unicodedata
from pathlib import Path
from typing import Iterable, List, Optional, Union

import numpy as np

ASSET_FILENAME = "siglip2_tokenizer.json.gz"
ASSET_FORMAT = "kestrel-siglip2-tokenizer/1"
CONTEXT_LENGTH = 64


def default_asset_path() -> Path:
    from ..config import MODELS_DIR
    return Path(MODELS_DIR) / "siglip" / ASSET_FILENAME


# --------------------------------------------------------------------------- #
# Cleaning: ftfy.fix_text (minus fix_encoding) + open_clip's _clean_canonicalize
# --------------------------------------------------------------------------- #

def _build_html_entities() -> dict:
    # ftfy.chardata._build_html_entities
    entities = {}
    for name, char in html.entities.html5.items():
        if name.endswith(";"):
            entities["&" + name] = char
            if name == name.lower():
                upper = "&" + name.upper()
                if html.unescape(upper) == upper:
                    entities[upper] = char.upper()
    return entities


_HTML_ENTITIES = _build_html_entities()
_HTML_ENTITY_RE = re.compile(r"&#?[0-9A-Za-z]{1,24};")


def _unescape_fixup(match: "re.Match[str]") -> str:
    text = match.group(0)
    if text in _HTML_ENTITIES:
        return _HTML_ENTITIES[text]
    if text.startswith("&#"):
        unescaped = html.unescape(text)
        return text if ";" in unescaped else unescaped
    return text


def _c1_fixer(match: "re.Match[str]") -> str:
    # ftfy decodes C1 controls with "sloppy-windows-1252": cp1252, with the five
    # undefined bytes passed through as Latin-1.
    out = []
    for ch in match.group(0):
        try:
            out.append(bytes([ord(ch)]).decode("cp1252"))
        except UnicodeDecodeError:
            out.append(ch)
    return "".join(out)


_C1_RE = re.compile(r"[\x80-\x9f]")
_LIGATURES = {
    ord("Ĳ"): "IJ", ord("ĳ"): "ij", ord("ŉ"): "ʼn",
    ord("Ǳ"): "DZ", ord("ǲ"): "Dz", ord("ǳ"): "dz",
    ord("Ǆ"): "DŽ", ord("ǅ"): "Dž", ord("ǆ"): "dž",
    ord("Ǉ"): "LJ", ord("ǈ"): "Lj", ord("ǉ"): "lj",
    ord("Ǌ"): "NJ", ord("ǋ"): "Nj", ord("ǌ"): "nj",
    ord("ﬀ"): "ff", ord("ﬁ"): "fi", ord("ﬂ"): "fl",
    ord("ﬃ"): "ffi", ord("ﬄ"): "ffl", ord("ﬅ"): "ſt", ord("ﬆ"): "st",
}


def _build_width_map() -> dict:
    width_map = {0x3000: " "}
    for i in range(0xFF01, 0xFFF0):
        alt = unicodedata.normalize("NFKC", chr(i))
        if alt != chr(i):
            width_map[i] = alt
    return width_map


_WIDTH_MAP = _build_width_map()
_SINGLE_QUOTE_RE = re.compile("[ʼ‘-‛]")
_DOUBLE_QUOTE_RE = re.compile("[“-‟]")
_ANSI_RE = re.compile("\033\\[((?:\\d|;)*)([a-zA-Z])")
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_SURROGATE_PAIR_RE = re.compile("[\ud800-\udbff][\udc00-\udfff]")
_CONTROL_CHARS = dict.fromkeys(
    list(range(0x00, 0x09)) + [0x0B] + list(range(0x0E, 0x20)) + [0x7F]
    + list(range(0x206A, 0x2070)) + [0xFEFF] + list(range(0xFFF9, 0xFFFD))
)


def _fix_surrogates(text: str) -> str:
    if _SURROGATE_RE.search(text):
        text = _SURROGATE_PAIR_RE.sub(
            lambda m: chr(0x10000 + (ord(m.group(0)[0]) - 0xD800) * 0x400 + (ord(m.group(0)[1]) - 0xDC00)),
            text,
        )
        text = _SURROGATE_RE.sub("�", text)
    return text


def _fix_segment(text: str, unescape: bool) -> str:
    # ftfy.fix_and_explain with fix_encoding disabled.
    while True:
        orig = text
        if unescape:
            text = _HTML_ENTITY_RE.sub(_unescape_fixup, text)
        text = _C1_RE.sub(_c1_fixer, text)
        text = text.translate(_LIGATURES)
        text = text.translate(_WIDTH_MAP)
        text = _SINGLE_QUOTE_RE.sub("'", _DOUBLE_QUOTE_RE.sub('"', text))
        text = (text.replace("\r\n", "\n").replace("\r", "\n")
                .replace(" ", "\n").replace(" ", "\n").replace("\u0085", "\n"))
        text = _fix_surrogates(text)
        text = _ANSI_RE.sub("", text)
        text = text.translate(_CONTROL_CHARS)
        text = unicodedata.normalize("NFC", text)
        if text == orig:
            return text


def fix_text(text: str) -> str:
    """``ftfy.fix_text`` without mojibake repair (line-by-line, "auto" HTML unescape)."""
    out = []
    pos = 0
    unescape = True
    while pos < len(text):
        brk = text.find("\n", pos) + 1 or len(text)
        segment = text[pos:brk]
        if unescape and "<" in segment:
            unescape = False  # ftfy: sticky for the rest of the string
        out.append(_fix_segment(segment, unescape))
        pos = brk
    return "".join(out)


_PUNCT = str.maketrans("", "", string.punctuation)


def clean(text: str) -> str:
    """open_clip ``_clean_canonicalize``: basic_clean then canonicalize_text."""
    text = fix_text(str(text))
    text = html.unescape(html.unescape(text)).strip()
    text = text.replace("_", " ").translate(_PUNCT).lower()
    return " ".join(text.split()).strip()


# --------------------------------------------------------------------------- #
# Tokenizer
# --------------------------------------------------------------------------- #

def _i32(b64: str) -> np.ndarray:
    return np.frombuffer(base64.b64decode(b64), dtype="<i4").astype(np.int64)


class SiglipTokenizer:
    """``tok(texts) -> int64 [N, 64]`` Gemma ids, identical to open_clip's SigLIP 2 tokenizer.

    The asset loads on first use (~0.3 s); construction is free.
    """

    def __init__(self, asset_path: Union[str, Path, None] = None, context_length: int = CONTEXT_LENGTH):
        self.asset_path = Path(asset_path) if asset_path is not None else None
        self.context_length = int(context_length)
        self._lock = threading.Lock()
        self._loaded = False
        self._cache: dict = {}

    # -- loading ------------------------------------------------------------ #
    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            path = self.asset_path or default_asset_path()
            with gzip.open(path, "rb") as f:
                asset = json.loads(f.read().decode("utf-8"))
            if asset.get("format") != ASSET_FORMAT:
                raise ValueError(f"{path}: unexpected tokenizer asset format {asset.get('format')!r}")
            self.pieces: List[str] = asset["pieces"]
            n = len(self.pieces)
            sp = asset["special"]
            self.pad_id, self.eos_id, self.bos_id, self.unk_id = sp["pad"], sp["eos"], sp["bos"], sp["unk"]
            self._char_ids = {p: i for i, p in enumerate(self.pieces) if len(p) == 1}
            self._byte_ids = list(asset["byte_ids"])

            m = asset["merges"]
            left, right = _i32(m["left"]), _i32(m["right"])
            result = np.cumsum(_i32(m["result_delta"]))
            keys = left * n + right
            order = np.argsort(keys, kind="stable")
            self._n = n
            self._merge_keys = keys[order]
            self._merge_rank = order
            self._merge_result = result[order]

            added = sorted(asset.get("added", []), key=lambda t: -len(t["content"]))
            self._added_ids = {t["content"]: t["id"] for t in added}
            self._added_re = (
                re.compile("|".join(re.escape(t["content"]) for t in added)) if added else None
            )

            subset = _i32(asset["subset"])
            remap = np.full(n, -1, dtype=np.int64)
            remap[subset] = np.arange(len(subset), dtype=np.int64)
            remap[remap < 0] = remap[self.unk_id]
            self.subset_ids = subset
            self._subset_remap = remap
            self._loaded = True

    @property
    def vocab_size(self) -> int:
        self._ensure_loaded()
        return self._n

    @property
    def subset_size(self) -> int:
        self._ensure_loaded()
        return int(len(self.subset_ids))

    # -- BPE ---------------------------------------------------------------- #
    def _lookup(self, keys: np.ndarray):
        """Vectorised merge lookup: (rank, result) arrays, rank -1 where no merge."""
        idx = np.searchsorted(self._merge_keys, keys)
        idx_c = np.minimum(idx, len(self._merge_keys) - 1)
        hit = self._merge_keys[idx_c] == keys
        return np.where(hit, self._merge_rank[idx_c], -1), self._merge_result[idx_c]

    def _initial_symbols(self, word: str) -> List[int]:
        # tokenizers BPE::merge_word: chars -> ids, byte fallback, fused <unk>.
        # (A pending <unk> is only flushed by the next in-vocab char or the end,
        # so byte-fallback ids can precede it; we keep that quirk.)
        ids: List[int] = []
        unk_pending = False
        for ch in word:
            cid = self._char_ids.get(ch)
            if cid is not None:
                if unk_pending:
                    ids.append(self.unk_id)
                    unk_pending = False
                ids.append(cid)
                continue
            bids = [self._byte_ids[b] for b in ch.encode("utf-8")]
            if all(b >= 0 for b in bids):
                ids.extend(bids)
                continue
            unk_pending = True  # fuse_unk: consecutive unknowns -> one <unk>
        if unk_pending:
            ids.append(self.unk_id)
        return ids

    def _bpe(self, word: str) -> List[int]:
        cached = self._cache.get(word)
        if cached is not None:
            return cached
        sym = self._initial_symbols(word)
        L = len(sym)
        if L > 1:
            n = self._n
            prev = list(range(-1, L - 1))
            nxt = list(range(1, L + 1))
            nxt[-1] = -1
            alive = [True] * L
            pair_keys = np.array(sym[:-1], dtype=np.int64) * n + np.array(sym[1:], dtype=np.int64)
            ranks, results = self._lookup(pair_keys)
            heap = [(int(r), i, int(c)) for i, (r, c) in enumerate(zip(ranks, results)) if r >= 0]
            heapq.heapify(heap)
            merge_keys, merge_rank, merge_result = self._merge_keys, self._merge_rank, self._merge_result
            nkeys = len(merge_keys)

            def lookup1(a: int, b: int):
                k = a * n + b
                j = int(np.searchsorted(merge_keys, k))
                if j < nkeys and merge_keys[j] == k:
                    return int(merge_rank[j]), int(merge_result[j])
                return None

            while heap:
                rank, pos, new_id = heapq.heappop(heap)
                if not alive[pos]:
                    continue
                q = nxt[pos]
                if q < 0:
                    continue
                hit = lookup1(sym[pos], sym[q])
                if hit is None or hit[1] != new_id:
                    continue  # stale entry (same test as tokenizers' Word::merge_all)
                sym[pos] = new_id
                alive[q] = False
                nxt[pos] = nxt[q]
                if nxt[q] >= 0:
                    prev[nxt[q]] = pos
                p = prev[pos]
                if p >= 0:
                    hit = lookup1(sym[p], new_id)
                    if hit is not None:
                        heapq.heappush(heap, (hit[0], p, hit[1]))
                r = nxt[pos]
                if r >= 0:
                    hit = lookup1(new_id, sym[r])
                    if hit is not None:
                        heapq.heappush(heap, (hit[0], pos, hit[1]))
            out = []
            i = 0
            while i >= 0:
                out.append(sym[i])
                i = nxt[i]
            sym = out
        if len(self._cache) < 10000:
            self._cache[word] = sym
        return sym

    # -- public API --------------------------------------------------------- #
    clean = staticmethod(clean)

    def encode(self, text: str) -> List[int]:
        """Gemma ids for ``text`` after open_clip's cleaning, without <eos>/padding."""
        self._ensure_loaded()
        text = clean(text)
        ids: List[int] = []
        pos = 0
        segments = []
        if self._added_re is not None:
            for m in self._added_re.finditer(text):
                segments.append((text[pos:m.start()], None))
                segments.append((None, self._added_ids[m.group(0)]))
                pos = m.end()
        segments.append((text[pos:], None))
        for seg, added_id in segments:
            if added_id is not None:
                ids.append(added_id)
            elif seg:
                ids.extend(self._bpe(seg.replace(" ", "▁")))
        return ids

    def tokenize(self, texts: Union[str, Iterable[str]], context_length: Optional[int] = None) -> np.ndarray:
        if isinstance(texts, str):
            texts = [texts]
        texts = list(texts)
        ctx = int(context_length or self.context_length)
        self._ensure_loaded()
        out = np.full((len(texts), ctx), self.pad_id, dtype=np.int64)
        for i, t in enumerate(texts):
            toks = self.encode(t)[: ctx - 1] + [self.eos_id]
            out[i, : len(toks)] = toks
        return out

    __call__ = tokenize

    def to_subset(self, ids) -> np.ndarray:
        """Map Gemma ids to rows of the pruned embedding table (others -> the <unk> row)."""
        self._ensure_loaded()
        return self._subset_remap[np.asarray(ids, dtype=np.int64)]

    def decode(self, ids: Iterable[int]) -> str:
        """Debug helper: pieces joined, '▁' -> ' ', byte tokens re-assembled; specials dropped."""
        self._ensure_loaded()
        specials = {self.pad_id, self.eos_id, self.bos_id}
        buf = bytearray()
        for i in ids:
            i = int(i)
            if i in specials:
                continue
            p = self.pieces[i]
            if len(p) == 6 and p.startswith("<0x") and p.endswith(">"):
                buf.append(int(p[3:5], 16))
            else:
                buf.extend(p.replace("▁", " ").encode("utf-8"))
        return buf.decode("utf-8", errors="replace")
