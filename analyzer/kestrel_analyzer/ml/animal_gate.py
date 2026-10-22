"""Zero-shot false-positive gate for MegaDetector boxes (pipeline 3.0.0).

Replaces SpeciesNet's blank / "no cv result" / confidence gate. MegaDetector
over-proposes on purpose (branches, bark, rocks, posts...); every animal box it
keeps is segmented, cropped and embedded once with BioCLIP 2, and this module
turns that ONE embedding into ``P(animal)`` without running any text tower:
the prompt embeddings are precomputed into ``models/bioclip/gate_text_emb.npy``
(``experiments/bioclip/export_app_assets.py --gate``).

Swapping the method or the threshold mapping is deliberately a one-line change:

* ``GATE_METHOD`` names an entry of ``METHODS`` (each ``fn(gate, emb) -> float``
  in [0, 1]);
* ``threshold_for(detection_threshold)`` maps the user's detection threshold to
  the gate's keep threshold. The default mirrors SpeciesNet's gate exactly: a
  box was dropped when ``pred_score < detector_threshold``, so it is kept here
  iff ``animal_score >= detection_threshold``.

During a v3 *upgrade* of an already-analysed folder the gate only flags crops
(``gate_flagged`` in ``crops_json``); it never deletes a detection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, Optional

import numpy as np

GATE_TEXT_EMB_FILENAME = "gate_text_emb.npy"
GATE_INDEX_FILENAME = "gate_index.json"


def _prompt_softmax(gate: "AnimalGate", emb: np.ndarray) -> float:
    """Softmax over [animal prompts ∪ background prompts] at the model's logit
    scale; P(animal) = summed probability of the animal prompts."""
    logits = gate.logit_scale * (gate.text_emb @ np.asarray(emb, dtype=np.float32))
    logits = logits - logits.max()
    p = np.exp(logits)
    p /= p.sum()
    return float(p[: gate.n_animal].sum())


METHODS: Dict[str, Callable[["AnimalGate", np.ndarray], float]] = {
    "prompt_softmax": _prompt_softmax,
}

# ── the two knobs ────────────────────────────────────────────────────────────
GATE_METHOD = "prompt_softmax"


def threshold_for(detection_threshold: float) -> float:
    """Gate keep-threshold for a user detection threshold (SpeciesNet parity)."""
    try:
        t = float(detection_threshold)
    except (TypeError, ValueError):
        t = 0.25
    return max(0.0, min(1.0, t))
# ─────────────────────────────────────────────────────────────────────────────


def default_assets_dir() -> Path:
    from ..config import BIOCLIP_MODELS_DIR
    return Path(BIOCLIP_MODELS_DIR)


class AnimalGate:
    """``animal_score(emb)`` / ``keep(emb, detection_threshold)`` on a normalised BioCLIP embedding."""

    def __init__(self, assets_dir=None, method: Optional[str] = None):
        d = Path(assets_dir) if assets_dir else default_assets_dir()
        with open(d / GATE_INDEX_FILENAME, encoding="utf-8") as f:
            index = json.load(f)
        self.animal_prompts = list(index["animal"])
        self.background_prompts = list(index["background"])
        self.n_animal = len(self.animal_prompts)
        self.logit_scale = float(index.get("logit_scale", 100.0))
        text = np.load(d / GATE_TEXT_EMB_FILENAME).astype(np.float32)
        if text.shape[0] != self.n_animal + len(self.background_prompts):
            raise ValueError("gate_text_emb rows do not match gate_index prompts")
        text /= np.maximum(np.linalg.norm(text, axis=1, keepdims=True), 1e-12)
        self.text_emb = text
        self.method = method or GATE_METHOD
        if self.method not in METHODS:
            raise ValueError(f"unknown gate method {self.method!r}")
        self._fn = METHODS[self.method]

    def animal_score(self, emb: np.ndarray) -> float:
        return float(self._fn(self, emb))

    def keep_score(self, score: float, detection_threshold: float) -> bool:
        return float(score) >= threshold_for(detection_threshold)

    def keep(self, emb: np.ndarray, detection_threshold: float) -> bool:
        return self.keep_score(self.animal_score(emb), detection_threshold)
