"""Experimental free-text "smart search" over a folder's crops (OpenAI CLIP ViT-B/16).

BioCLIP's text side cannot describe behaviour or composition, so search uses a
general-purpose CLIP instead. At analysis time ``ClipImageEmbedder`` embeds
every subject crop (the same square, exposure-corrected ``quality_crop``
BioCLIP sees) and the pipeline stores it in the embedding sidecar under
``MODEL_ID``. At search time ``ClipTextEncoder`` embeds the query.

Scoring (validated on bird crops)::

    score(crop, q) = cos(img, T(q)) - cos(img, T(BASELINE))

with ``T(x)`` the text embedding of ``"a photo of {x}."``. Subtracting the
generic "a bird" direction stops blurry / generic crops, which sit close to
every bird prompt, from topping every query. Images score as the max over
their crops and scenes as the max over their images.

Assets live in ``models/clip/`` (see its NOTICES.md):

* ``clip_b16_image_int8.onnx`` -- ``image`` float32 [N,3,224,224] -> ``emb`` [N,512]
* ``clip_b16_text_fp16w.onnx`` -- ``tokens`` int64 [N,77] -> ``emb`` [N,512]
* ``bpe_simple_vocab_16e6.txt.gz`` -- tokenizer vocab
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from .bioclip_species import preprocess  # identical CLIP eval transform

MODEL_ID = "clip-b16"
DIM = 512
IMAGE_MODEL_FILENAME = "clip_b16_image_int8.onnx"
TEXT_MODEL_FILENAME = "clip_b16_text_fp16w.onnx"
VOCAB_FILENAME = "bpe_simple_vocab_16e6.txt.gz"
PROMPT = "a photo of {}."
BASELINE_QUERY = "a bird"
MAX_QUERY_CHARS = 200


def _l2(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=np.float32)
    return x / np.maximum(np.linalg.norm(x, axis=-1, keepdims=True), 1e-12)


def normalize_query(query) -> str:
    """Collapse whitespace, cap the length and drop a trailing full stop."""
    q = " ".join(str(query or "").split())[:MAX_QUERY_CHARS].strip()
    return q.rstrip(".").strip()


def default_assets_dir() -> Path:
    from ..config import MODELS_DIR
    return Path(MODELS_DIR) / "clip"


def assets_available(assets_dir: Optional[Path] = None) -> bool:
    d = Path(assets_dir) if assets_dir else default_assets_dir()
    try:
        # LFS pointer files are ~130 bytes; real towers are tens of MB.
        return all(
            (d / n).is_file() and (d / n).stat().st_size > 1_000_000
            for n in (IMAGE_MODEL_FILENAME, TEXT_MODEL_FILENAME, VOCAB_FILENAME)
        )
    except OSError:
        return False


class ClipImageEmbedder:
    """CLIP image tower on a ``ResilientOnnxSession`` (shares the run's ProviderCoordinator)."""

    def __init__(self, assets_dir, coord):
        from .resilient_session import ResilientOnnxSession

        self.assets_dir = Path(assets_dir)
        self.session = ResilientOnnxSession("clip_image", self.assets_dir / IMAGE_MODEL_FILENAME, coord)
        self._input_name = self.session.get_inputs()[0].name
        self.providers_used = list(self.session.get_providers())

    def embed(self, images) -> np.ndarray:
        """L2-normalised float32 [N,512], one row per RGB uint8 HxWx3 crop.

        One crop per session run: the tower is dynamically quantised (one
        activation scale per tensor over the whole batch), so batching would
        couple the crops' embeddings.
        """
        if isinstance(images, np.ndarray) and images.ndim == 3:
            images = [images]
        if not len(images):
            return np.zeros((0, DIM), np.float32)
        out = np.concatenate([
            self.session.run(None, {self._input_name: preprocess(im)[None]})[0]
            for im in images
        ]).astype(np.float32)
        return _l2(out)


class ClipTextEncoder:
    """CLIP text tower, loaded on first use; caches query embeddings (thread-safe)."""

    def __init__(self, assets_dir=None, cache_size: int = 256, providers: Optional[List[str]] = None):
        self.assets_dir = Path(assets_dir) if assets_dir else default_assets_dir()
        self._providers = providers or ["CPUExecutionProvider"]
        self._lock = threading.Lock()
        self._session = None
        self._tokenizer = None
        self._input_name = "tokens"
        self._cache: "OrderedDict[str, np.ndarray]" = OrderedDict()
        self._cache_size = int(cache_size)

    def _ensure_loaded(self) -> None:
        if self._session is not None:
            return
        import onnxruntime as ort
        from .clip_tokenizer import ClipTokenizer

        self._tokenizer = ClipTokenizer(self.assets_dir / VOCAB_FILENAME)
        so = ort.SessionOptions()
        so.log_severity_level = 3
        self._session = ort.InferenceSession(
            str((self.assets_dir / TEXT_MODEL_FILENAME).resolve()), sess_options=so, providers=self._providers
        )
        self._input_name = self._session.get_inputs()[0].name

    def encode(self, query: str) -> np.ndarray:
        """L2-normalised float32 [512] for ``PROMPT.format(query)``."""
        q = normalize_query(query)
        with self._lock:
            hit = self._cache.get(q)
            if hit is not None:
                self._cache.move_to_end(q)
                return hit
            self._ensure_loaded()
            tokens = self._tokenizer.tokenize(PROMPT.format(q))
            emb = _l2(self._session.run(None, {self._input_name: tokens})[0][0])
            emb.setflags(write=False)
            self._cache[q] = emb
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
            return emb

    def baseline(self) -> np.ndarray:
        return self.encode(BASELINE_QUERY)


_shared_text_encoder: Optional[ClipTextEncoder] = None
_shared_lock = threading.Lock()


def shared_text_encoder() -> ClipTextEncoder:
    """Process-wide lazily created text encoder (the bridge reuses it across searches)."""
    global _shared_text_encoder
    with _shared_lock:
        if _shared_text_encoder is None:
            _shared_text_encoder = ClipTextEncoder()
        return _shared_text_encoder


# ---------------------------------------------------------------------- storage


def store_dir(kestrel_dir: str) -> str:
    from .. import embedding_store
    return embedding_store._dir(kestrel_dir, MODEL_ID)


def open_store(kestrel_dir: str, log_path: Optional[str] = None):
    """The folder's smart-search slot of the embedding sidecar (never raises)."""
    from .. import embedding_store
    return embedding_store.EmbeddingStore(kestrel_dir, model=MODEL_ID, dim=DIM, log_path=log_path, slot=MODEL_ID)


def load_store(kestrel_dir: str):
    """``(keys, float32 matrix [N,512], header)``; empty when the folder has no CLIP rows."""
    from .. import embedding_store
    keys, mat, header = embedding_store.load_embeddings(kestrel_dir, slot=MODEL_ID, model=MODEL_ID, dim=DIM)
    return keys, mat.astype(np.float32), header


# ---------------------------------------------------------------------- scoring


def crop_scores(image_emb: np.ndarray, query_emb: np.ndarray, baseline_emb: np.ndarray) -> np.ndarray:
    """Baseline-subtracted cosine per row of ``image_emb`` (rows assumed L2-normalised)."""
    m = np.asarray(image_emb, dtype=np.float32)
    if m.size == 0:
        return np.zeros((0,), np.float32)
    direction = np.asarray(query_emb, np.float32) - np.asarray(baseline_emb, np.float32)
    return m @ direction


def rank(
    keys: Sequence[Tuple[str, int]],
    scores: np.ndarray,
    scene_of: Mapping[str, str],
    limit: int = 50,
) -> Dict[str, list]:
    """Aggregate crop scores to images (max) and scenes (max), best first.

    ``keys`` are ``(filename, crop_index)`` aligned with ``scores``;
    ``scene_of`` maps filename -> scene id (filenames missing from it, e.g.
    embeddings for files no longer in the database, are dropped).
    Returns ``{"scenes": [{scene_id, score, best_filename, best_crop}],
    "images": [{filename, scene_id, score, crop_index}]}``.
    """
    best_img: Dict[str, Tuple[float, int]] = {}
    for (fn, crop), s in zip(keys, np.asarray(scores, dtype=np.float32).tolist()):
        if fn not in scene_of:
            continue
        cur = best_img.get(fn)
        if cur is None or s > cur[0]:
            best_img[fn] = (float(s), int(crop))
    images = sorted(
        ({"filename": fn, "scene_id": str(scene_of[fn]), "score": s, "crop_index": c}
         for fn, (s, c) in best_img.items()),
        key=lambda r: (-r["score"], r["filename"]),
    )
    best_scene: Dict[str, dict] = {}
    for im in images:  # already best-first, so the first hit per scene is its max
        if im["scene_id"] not in best_scene:
            best_scene[im["scene_id"]] = {
                "scene_id": im["scene_id"], "score": im["score"],
                "best_filename": im["filename"], "best_crop": im["crop_index"],
            }
    scenes = list(best_scene.values())
    limit = max(1, int(limit))
    return {"scenes": scenes[:limit], "images": images[: limit * 4]}


def subject_crops(crops: Iterable[dict]) -> List[dict]:
    """The crops smart search indexes: real detections, not the whole-frame fallback."""
    out = []
    for c in crops or []:
        try:
            if int(c.get("detection_index", -1)) >= 0:
                out.append(c)
        except (TypeError, ValueError, AttributeError):
            continue
    return out
