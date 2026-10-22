"""SigLIP 2 ViT-B/16 (Apache-2.0): smart-search embeddings + the v3 default quality score.

Pipeline 3.0.0 runs the SigLIP image tower on every kept subject crop (the
square, exposure-corrected crop written to ``crop/``) and once on the
downscaled whole frame (the ``export/`` JPEG). From the crop embedding it also
derives the zero-shot *sharpness* score that is the new default quality::

    s = cos(img, T(pos)) - cos(img, T(neg))
    quality_siglip = np.interp(s, knots_raw, knots_quality)

with ``pos``/``neg`` the focus-only prompts in ``quality_calibration.json``
(no prompt template) and the knots a fixed quantile match onto the legacy
CNN's 0-1 distribution so the rating profiles keep their meaning. ``T(pos)``
and ``T(neg)`` are precomputed into ``quality_prompt_emb.npy`` by
``tools/build_v3_text_assets.py`` so analysis never loads the text tower.

Assets (``models/siglip/``):

* ``siglip2_b16_image_int8.onnx`` -- ``image`` float32 [N,3,224,224] (RGB,
  bicubic squash to 224x224, ``(x/255-0.5)/0.5``) -> ``emb`` [N,768].
  Dynamically quantised: run ONE image per call (one activation scale is
  shared across a batch, so batching would couple the images).
* ``siglip2_b16_text_fp16w.onnx`` -- text tower minus its token table:
  ``x`` float32 [N,64,768] gathered token embeddings -> ``emb`` [N,768].
* ``siglip2_token_emb_int8.npz`` -- pruned token table, ``q`` int8 + per-row
  ``scale``; row order = ``SiglipTokenizer.subset_ids``.
* ``siglip2_tokenizer.json.gz`` -- see ``siglip_tokenizer.py``.

Search scoring needs no baseline subtraction: ``cos(img, T("a photo of {q}."))``.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import List, Optional

import numpy as np

MODEL_ID = "siglip2-b16"
FRAME_SLOT = "siglip2-b16-frame"
DIM = 768
INPUT_SIZE = 224
CONTEXT_LENGTH = 64
SEARCH_PROMPT = "a photo of {}."
MAX_QUERY_CHARS = 200

IMAGE_MODEL_FILENAME = "siglip2_b16_image_int8.onnx"
TEXT_MODEL_FILENAME = "siglip2_b16_text_fp16w.onnx"
TOKEN_TABLE_FILENAME = "siglip2_token_emb_int8.npz"
TOKENIZER_FILENAME = "siglip2_tokenizer.json.gz"
CALIBRATION_FILENAME = "quality_calibration.json"
QUALITY_PROMPT_EMB_FILENAME = "quality_prompt_emb.npy"

# Idle time after which the shared text encoder drops its ONNX session, token
# table and tokenizer (~450 MB resident together). The next query reloads them
# (~1 s).
TEXT_IDLE_RELEASE_SEC = 300.0


def _l2(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-12)


def normalize_query(query) -> str:
    """Collapse whitespace, cap the length and drop a trailing full stop."""
    q = " ".join(str(query or "").split())[:MAX_QUERY_CHARS].strip()
    return q.rstrip(".").strip()


def default_assets_dir() -> Path:
    from ..config import MODELS_DIR
    return Path(MODELS_DIR) / "siglip"


def _big_file(p: Path) -> bool:
    # LFS pointer stubs are ~130 bytes; the real files are megabytes.
    try:
        return p.is_file() and p.stat().st_size > 1_000_000
    except OSError:
        return False


def image_assets_available(assets_dir: Optional[Path] = None) -> bool:
    d = Path(assets_dir) if assets_dir else default_assets_dir()
    return _big_file(d / IMAGE_MODEL_FILENAME)


def assets_available(assets_dir: Optional[Path] = None) -> bool:
    """Image tower AND text side present (needed for search)."""
    d = Path(assets_dir) if assets_dir else default_assets_dir()
    return all(_big_file(d / n) for n in (IMAGE_MODEL_FILENAME, TEXT_MODEL_FILENAME,
                                          TOKEN_TABLE_FILENAME, TOKENIZER_FILENAME))


def preprocess(image: np.ndarray) -> np.ndarray:
    """RGB uint8 HxWx3 -> float32 [3,224,224]: bicubic squash to 224x224, then (x/255-0.5)/0.5."""
    from PIL import Image

    arr = np.asarray(image)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    if arr.shape[-1] == 4:
        arr = arr[..., :3]
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    pil = Image.fromarray(np.ascontiguousarray(arr), mode="RGB")
    if pil.size != (INPUT_SIZE, INPUT_SIZE):
        pil = pil.resize((INPUT_SIZE, INPUT_SIZE), Image.BICUBIC)
    x = np.asarray(pil, dtype=np.float32) / 255.0
    x = (x - 0.5) / 0.5
    return np.ascontiguousarray(x.transpose(2, 0, 1), dtype=np.float32)


class SiglipImageEmbedder:
    """SigLIP 2 image tower on a ``ResilientOnnxSession`` (shares the run's ProviderCoordinator)."""

    def __init__(self, assets_dir, coord, kind: str = "siglip_image"):
        from .resilient_session import ResilientOnnxSession

        self.assets_dir = Path(assets_dir) if assets_dir else default_assets_dir()
        self.session = ResilientOnnxSession(kind, self.assets_dir / IMAGE_MODEL_FILENAME, coord)
        self._input_name = self.session.get_inputs()[0].name
        self.providers_used = list(self.session.get_providers())

    def embed(self, images) -> np.ndarray:
        """L2-normalised float32 [N,768], one ``run`` per image (see module docstring)."""
        if isinstance(images, np.ndarray) and images.ndim == 3:
            images = [images]
        if not len(images):
            return np.zeros((0, DIM), np.float32)
        out = np.concatenate([
            self.session.run(None, {self._input_name: preprocess(im)[None]})[0]
            for im in images
        ]).astype(np.float32)
        return _l2(out)


class SiglipTextEncoder:
    """Text side: tokenizer -> pruned int8 token table -> text ONNX -> L2 norm.

    Loaded lazily, cached per query (LRU), thread-safe, and released after
    ``idle_release_sec`` without use so a browse-only session does not keep
    ~450 MB resident for a search typed once.
    """

    def __init__(self, assets_dir=None, cache_size: int = 256, providers: Optional[List[str]] = None,
                 idle_release_sec: Optional[float] = TEXT_IDLE_RELEASE_SEC):
        self.assets_dir = Path(assets_dir) if assets_dir else default_assets_dir()
        self._providers = providers or ["CPUExecutionProvider"]
        self._lock = threading.RLock()
        self._session = None
        self._tokenizer = None
        self._q = None
        self._scale = None
        self._cache: "OrderedDict[str, np.ndarray]" = OrderedDict()
        self._cache_size = int(cache_size)
        self._idle_release_sec = idle_release_sec
        self._last_used = 0.0
        self._timer: Optional[threading.Timer] = None

    @property
    def loaded(self) -> bool:
        return self._session is not None

    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        import onnxruntime as ort
        from .siglip_tokenizer import SiglipTokenizer

        tok = SiglipTokenizer(self.assets_dir / TOKENIZER_FILENAME, context_length=CONTEXT_LENGTH)
        with np.load(self.assets_dir / TOKEN_TABLE_FILENAME) as z:
            q = np.ascontiguousarray(z["q"])
            scale = np.ascontiguousarray(z["scale"]).astype(np.float32)
        if q.shape[0] != tok.subset_size:
            raise ValueError(f"token table rows {q.shape[0]} != tokenizer subset {tok.subset_size}")
        so = ort.SessionOptions()
        so.log_severity_level = 3
        sess = ort.InferenceSession(str((self.assets_dir / TEXT_MODEL_FILENAME).resolve()),
                                    sess_options=so, providers=self._providers)
        self._tokenizer, self._q, self._scale, self._session = tok, q, scale, sess
        self._input_name = sess.get_inputs()[0].name

    def release(self) -> None:
        """Drop the session, token table and tokenizer (the query cache survives)."""
        with self._lock:
            self._session = None
            self._tokenizer = None
            self._q = None
            self._scale = None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _schedule_release(self) -> None:
        if not self._idle_release_sec or self._idle_release_sec <= 0:
            return
        if self._timer is not None:
            self._timer.cancel()

        def _maybe_release():
            with self._lock:
                if time.monotonic() - self._last_used >= self._idle_release_sec * 0.95:
                    self.release()

        t = threading.Timer(self._idle_release_sec, _maybe_release)
        t.daemon = True
        t.start()
        self._timer = t

    def encode_texts(self, texts: List[str]) -> np.ndarray:
        """Raw prompts (no template) -> L2-normalised float32 [N,768]. Uncached."""
        with self._lock:
            self._ensure_loaded()
            ids = self._tokenizer.tokenize(list(texts))
            rows = self._tokenizer.to_subset(ids)
            x = self._q[rows].astype(np.float32) * self._scale[rows][..., None]
            out = np.concatenate([
                self._session.run(None, {self._input_name: x[i:i + 1]})[0] for i in range(len(texts))
            ])
            self._last_used = time.monotonic()
            self._schedule_release()
            return _l2(out)

    def encode(self, query: str) -> np.ndarray:
        """L2-normalised float32 [768] for ``SEARCH_PROMPT.format(query)`` (cached)."""
        q = normalize_query(query)
        with self._lock:
            hit = self._cache.get(q)
            if hit is not None:
                self._cache.move_to_end(q)
                return hit
            emb = self.encode_texts([SEARCH_PROMPT.format(q)])[0]
            emb.setflags(write=False)
            self._cache[q] = emb
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            return emb


_shared_text_encoder: Optional[SiglipTextEncoder] = None
_shared_lock = threading.Lock()


def shared_text_encoder() -> SiglipTextEncoder:
    """Process-wide lazily created text encoder (the bridge reuses it across searches)."""
    global _shared_text_encoder
    with _shared_lock:
        if _shared_text_encoder is None:
            _shared_text_encoder = SiglipTextEncoder()
        return _shared_text_encoder


# ---------------------------------------------------------------------- quality


class SiglipQualityScorer:
    """Calibrated zero-shot sharpness from a SigLIP crop embedding (no model run)."""

    def __init__(self, assets_dir=None, text_encoder: Optional[SiglipTextEncoder] = None):
        d = Path(assets_dir) if assets_dir else default_assets_dir()
        with open(d / CALIBRATION_FILENAME, encoding="utf-8") as f:
            cal = json.load(f)
        self.knots_raw = np.asarray(cal["knots_raw"], dtype=np.float64)
        self.knots_quality = np.asarray(cal["knots_quality"], dtype=np.float64)
        if self.knots_raw.shape != self.knots_quality.shape or self.knots_raw.size < 2:
            raise ValueError("quality_calibration.json: knots_raw / knots_quality mismatch")
        self.prompts = dict(cal["prompts"])
        emb_path = d / QUALITY_PROMPT_EMB_FILENAME
        emb = None
        if emb_path.is_file():
            try:
                meta_ok = True
                meta_path = emb_path.with_suffix(".json")
                if meta_path.is_file():
                    with open(meta_path, encoding="utf-8") as f:
                        meta = json.load(f)
                    meta_ok = meta.get("prompts") == [self.prompts["pos"], self.prompts["neg"]]
                if meta_ok:
                    emb = np.load(emb_path).astype(np.float32)
            except Exception:
                emb = None
        if emb is None:
            # Fallback (asset missing / prompts changed): encode once at load.
            enc = text_encoder or SiglipTextEncoder(d, idle_release_sec=None)
            emb = enc.encode_texts([self.prompts["pos"], self.prompts["neg"]])
            if text_encoder is None:
                enc.release()
        emb = _l2(emb)
        self.direction = (emb[0] - emb[1]).astype(np.float32)

    def raw(self, emb: np.ndarray) -> np.ndarray:
        e = np.asarray(emb, dtype=np.float32)
        return e @ self.direction

    def score(self, emb: np.ndarray):
        """0-1 quality for one embedding [768] (float) or many [N,768] (array)."""
        s = self.raw(emb)
        q = np.interp(s, self.knots_raw, self.knots_quality)
        return float(q) if np.ndim(q) == 0 else q.astype(np.float32)


# ---------------------------------------------------------------------- storage


def store_dir(kestrel_dir: str, slot: str = MODEL_ID) -> str:
    from .. import embedding_store
    return embedding_store._dir(kestrel_dir, slot)


def open_store(kestrel_dir: str, slot: str = MODEL_ID, log_path: Optional[str] = None):
    """One SigLIP slot of the folder's embedding sidecar (never raises)."""
    from .. import embedding_store
    return embedding_store.EmbeddingStore(kestrel_dir, model=MODEL_ID, dim=DIM, log_path=log_path, slot=slot)


def load_store(kestrel_dir: str, slot: str = MODEL_ID):
    """``(keys, float32 [N,768], header)``; empty when the slot is absent or holds another model."""
    from .. import embedding_store
    keys, mat, header = embedding_store.load_embeddings(kestrel_dir, slot=slot, model=MODEL_ID, dim=DIM)
    return keys, mat.astype(np.float32), header
