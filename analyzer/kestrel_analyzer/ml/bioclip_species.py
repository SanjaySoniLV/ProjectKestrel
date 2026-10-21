"""Zero-shot bird species classifier built on BioCLIP 2 (ViT-L/14, MIT licence).

Replaces the legacy 498-species North-American CNN. The image tower is an
int8 ONNX export (dynamic quantisation of the MatMuls only); the label space
is a precomputed, L2-normalised text embedding per IOC world bird species
(prompt ``"a photo of {common name}."``), so classification is one image
forward pass plus a single matrix product.

Assets live in ``models/bioclip/``:

* ``bioclip2_image_int8.onnx`` -- input ``image`` float32 [N,3,224,224]
  (CLIP-normalised), output ``emb`` float32 [N,768] (not normalised).
* ``species_text_emb.npy``     -- float16 [S,768], rows L2-normalised.
* ``species_index.json``       -- ``scientific_names`` (row order of the
  ``.npy``, keyed on ``birds_global.csv``'s ``scientific_name``) and
  ``logit_scale``.

The label space is restricted per folder by a geofence (``set_regions``): the
softmax runs over the allowed species only, so scores are real probabilities
over the folder's regional list. Family scores are the summed probability of
the allowed species in each display family (``family_common`` in the catalog,
e.g. "Hawk/Eagle/Kite sp.").
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from ..logging_utils import debug
from .provider_coordinator import ProviderCoordinator
from .resilient_session import ResilientOnnxSession

IMAGE_MODEL_FILENAME = "bioclip2_image_int8.onnx"
TEXT_EMB_FILENAME = "species_text_emb.npy"
INDEX_FILENAME = "species_index.json"

# open_clip's OPENAI_DATASET_MEAN / _STD (BioCLIP 2's eval transform uses them).
CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
INPUT_SIZE = 224

GLOBAL_REGION = "global"
MODEL_ID = "bioclip-2"


def normalize_regions(regions: Optional[Iterable[str]]) -> Optional[frozenset[str]]:
    """``None`` / empty / containing ``"global"`` -> ``None`` (all species)."""
    if regions is None:
        return None
    if isinstance(regions, str):
        regions = [regions]
    sel = frozenset(str(r).strip() for r in regions if r and str(r).strip())
    if not sel or GLOBAL_REGION in {r.lower() for r in sel}:
        return None
    return sel


def preprocess(image: np.ndarray) -> np.ndarray:
    """RGB uint8 HxWx3 -> float32 [3,224,224], matching open_clip's eval transform.

    Resize the shortest side to 224 with PIL BICUBIC (PIL antialiases when
    downscaling, which matters for the 1024 px square crop), centre-crop
    224x224, scale to [0,1], then CLIP mean/std normalisation.
    """
    from PIL import Image

    arr = np.asarray(image)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    if arr.shape[-1] == 4:
        arr = arr[..., :3]
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8)
    pil = Image.fromarray(np.ascontiguousarray(arr), mode="RGB")
    w, h = pil.size
    # torchvision.transforms.Resize(int): short side -> 224, long side truncated.
    short, long = (w, h) if w <= h else (h, w)
    new_long = int(INPUT_SIZE * long / short)
    new_w, new_h = (INPUT_SIZE, new_long) if w <= h else (new_long, INPUT_SIZE)
    if (new_w, new_h) != (w, h):
        pil = pil.resize((new_w, new_h), Image.BICUBIC)
    left = int(round((new_w - INPUT_SIZE) / 2.0))
    top = int(round((new_h - INPUT_SIZE) / 2.0))
    pil = pil.crop((left, top, left + INPUT_SIZE, top + INPUT_SIZE))
    x = np.asarray(pil, dtype=np.float32) / 255.0
    x = (x - CLIP_MEAN) / CLIP_STD
    return np.ascontiguousarray(x.transpose(2, 0, 1), dtype=np.float32)


class BioClipSpeciesClassifier:
    """BioCLIP 2 zero-shot classifier over the (geofenced) IOC world bird list."""

    def __init__(self, models_dir: str | Path, coord: ProviderCoordinator, catalog=None):
        base = Path(models_dir)
        if base.name != "bioclip" and (base / "bioclip").is_dir():
            base = base / "bioclip"
        self.assets_dir = base
        with open(base / INDEX_FILENAME, encoding="utf-8") as f:
            index = json.load(f)
        self.model_name = str(index.get("model", MODEL_ID))
        self.logit_scale = float(index.get("logit_scale", 100.0))
        sci_names = list(index["scientific_names"])
        text = np.load(base / TEXT_EMB_FILENAME).astype(np.float32)
        if text.shape[0] != len(sci_names):
            raise ValueError(
                f"species_text_emb rows ({text.shape[0]}) != index names ({len(sci_names)})"
            )
        norms = np.linalg.norm(text, axis=1, keepdims=True)
        text /= np.maximum(norms, 1e-12)
        self.text_emb = text

        if catalog is None:
            from ..bird_catalog import get_catalog
            catalog = get_catalog()
        by_sci = {r.scientific_name: r for r in catalog.records}
        self._records = [by_sci.get(s) for s in sci_names]
        missing = [s for s, r in zip(sci_names, self._records) if r is None]
        if missing:
            debug(f"[BioClipSpeciesClassifier] {len(missing)} index names not in catalog, e.g. {missing[:3]}")
        # Rows without a catalog record have no display name; never offer them.
        self._known = np.array([r is not None for r in self._records], dtype=bool)
        self.species_labels = np.array(
            [r.canonical_common_name if r else s for s, r in zip(sci_names, self._records)],
            dtype=object,
        )
        self.scientific_names = sci_names
        fam = [(r.family_common or "Unknown Family") if r else "Unknown Family" for r in self._records]
        _, first = np.unique(np.array(fam), return_index=True)
        self.family_display_names = [fam[i] for i in sorted(first)]
        fam_idx = {f: i for i, f in enumerate(self.family_display_names)}
        self._species_family_idx = np.array([fam_idx[f] for f in fam], dtype=np.int64)

        self.session = ResilientOnnxSession("bird_species", base / IMAGE_MODEL_FILENAME, coord)
        self.providers_used = list(self.session.get_providers())
        self._input_name = self.session.get_inputs()[0].name
        debug(
            f"[BioClipSpeciesClassifier] {len(sci_names)} species, providers: {self.providers_used}"
        )

        self._known_regions = frozenset().union(*(r.regions for r in self._records if r is not None))
        self._mask_cache: dict[Optional[frozenset[str]], np.ndarray] = {}
        self._regions: Optional[frozenset[str]] = None
        self._allowed = self._mask_for(None)

    # ------------------------------------------------------------------ geofence

    def _mask_for(self, regions: Optional[frozenset[str]]) -> np.ndarray:
        cached = self._mask_cache.get(regions)
        if cached is not None:
            return cached
        if regions is None:
            mask = self._known.copy()
        else:
            mask = np.array(
                [bool(r is not None and r.matches_any_region(regions)) for r in self._records],
                dtype=bool,
            )
        idx = np.flatnonzero(mask)
        self._mask_cache[regions] = idx
        return idx

    def set_regions(self, regions: Optional[Iterable[str]]) -> None:
        """Restrict the label space. ``None`` / ``["global"]`` = all species."""
        sel = normalize_regions(regions)
        if sel is not None:
            # Unknown codes would otherwise leave only the "Worldwide" rows.
            sel = (sel & self._known_regions) or None
        idx = self._mask_for(sel)
        if idx.size == 0:
            debug(f"[BioClipSpeciesClassifier] region set {sorted(sel or [])} matched no species; using global")
            sel, idx = None, self._mask_for(None)
        self._regions = sel
        self._allowed = idx

    @property
    def regions(self) -> list[str]:
        return ["global"] if self._regions is None else sorted(self._regions)

    @property
    def allowed_count(self) -> int:
        return int(self._allowed.size)

    # ------------------------------------------------------------------ inference

    def embed(self, images) -> np.ndarray:
        """L2-normalised float32 embeddings, one row per RGB uint8 image."""
        if isinstance(images, np.ndarray) and images.ndim == 3:
            images = [images]
        # One crop per run, deliberately. The tower is dynamically quantised:
        # DynamicQuantizeLinear picks one scale per tensor over the WHOLE batch,
        # so batching would make a crop's embedding (and its species) depend
        # on which other birds share the frame.
        emb = np.concatenate([
            self.session.run(None, {self._input_name: preprocess(im)[None]})[0]
            for im in images
        ]).astype(np.float32)
        emb /= np.maximum(np.linalg.norm(emb, axis=1, keepdims=True), 1e-12)
        return emb

    def scores_from_embedding(self, emb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Return ``(allowed_row_indices, probabilities)`` for one embedding."""
        idx = self._allowed
        logits = self.logit_scale * (self.text_emb[idx] @ emb.astype(np.float32))
        logits -= logits.max()
        p = np.exp(logits)
        p /= p.sum()
        return idx, p

    def classify_embedding(self, emb: np.ndarray, top_k: int = 5) -> dict:
        idx, p = self.scores_from_embedding(emb)
        k = min(int(top_k), p.size)
        top = np.argsort(p)[::-1][:k]
        fam_scores = np.bincount(
            self._species_family_idx[idx], weights=p, minlength=len(self.family_display_names)
        )
        fam_top = np.argsort(fam_scores)[::-1][:k]
        fam_top = [i for i in fam_top if fam_scores[i] > 0]
        return {
            "top_species_labels": self.species_labels[idx[top]],
            "top_species_scores": p[top].astype(float),
            "top_species_scientific": [self.scientific_names[i] for i in idx[top]],
            "top_family_labels": [self.family_display_names[i] for i in fam_top],
            "top_family_scores": fam_scores[fam_top].astype(float).tolist(),
            "embedding": emb.astype(np.float32),
        }

    def classify_batch(self, images, top_k: int = 5) -> list[dict]:
        """``classify`` for several crops (one session run per crop; see ``embed``)."""
        if not len(images):
            return []
        return [self.classify_embedding(e, top_k=top_k) for e in self.embed(list(images))]

    def classify(self, image, top_k: int = 5) -> dict:
        """Same dict shape as the legacy ``BirdSpeciesClassifier.classify`` plus
        ``top_species_scientific`` and ``embedding`` (normalised float32 [768])."""
        return self.classify_embedding(self.embed(image)[0], top_k=top_k)
