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
  the gate's keep threshold.

The shipped method is ``linear_probe``: a logistic regression on the BioCLIP
embedding (``models/bioclip/gate_probe.json``), trained on boxes Kestrel kept plus
judged MegaDetector proposals (animal vs not) and random background squares from
7 perches (experiments/bioclip/exp_blank.py). Leave-one-perch-out it separates
animals from real MegaDetector false positives at AUC 0.97 (zero-shot prompts:
0.62), and its fixed keep threshold (0.12, the 99%-recall point) rejects about
half of real false positives and ~88% of background. Because it is calibrated on
its own, the threshold does not follow the user's detection threshold — that
still filters MegaDetector's confidence, as before. ``prompt_softmax`` (zero-shot,
SpeciesNet-parity threshold) stays available for comparison.

During a v3 *upgrade* of an already-analysed folder the gate only flags crops
(``gate_flagged`` in ``crops_json``); it never deletes a detection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Dict, Optional

import numpy as np

GATE_TEXT_EMB_FILENAME = "gate_text_emb.npy"
GATE_PROBE_FILENAME = "gate_probe.json"
# Mirrors gate_probe.json's keep_threshold (the leave-one-perch-out 99%-recall point).
PROBE_KEEP_THRESHOLD = 0.12
GATE_INDEX_FILENAME = "gate_index.json"


def _prompt_softmax(gate: "AnimalGate", emb: np.ndarray) -> float:
    """Softmax over [animal prompts ∪ background prompts] at the model's logit
    scale; P(animal) = summed probability of the animal prompts."""
    logits = gate.logit_scale * (gate.text_emb @ np.asarray(emb, dtype=np.float32))
    logits = logits - logits.max()
    p = np.exp(logits)
    p /= p.sum()
    return float(p[: gate.n_animal].sum())


def _linear_probe(gate: "AnimalGate", emb: np.ndarray) -> float:
    """P(animal) from the logistic probe on the L2-normalised embedding."""
    e = np.asarray(emb, dtype=np.float32)
    e = e / max(float(np.linalg.norm(e)), 1e-12)
    z = float(gate.probe_w @ e + gate.probe_b)
    return float(1.0 / (1.0 + np.exp(-z)))


METHODS: Dict[str, Callable[["AnimalGate", np.ndarray], float]] = {
    "prompt_softmax": _prompt_softmax,
    "linear_probe": _linear_probe,
}

# ── the two knobs ────────────────────────────────────────────────────────────
GATE_METHOD = "linear_probe"


def threshold_for(detection_threshold: float, method: Optional[str] = None) -> float:
    """Gate keep-threshold. The probe has its own fixed operating point; the
    zero-shot method mirrors SpeciesNet's gate (drop when score < detection threshold)."""
    if (method or GATE_METHOD) == "linear_probe":
        return PROBE_KEEP_THRESHOLD
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
        self.probe_w = None
        self.probe_b = 0.0
        probe = d / GATE_PROBE_FILENAME
        if probe.is_file():
            with open(probe, encoding="utf-8") as f:
                pj = json.load(f)
            self.probe_w = np.asarray(pj["w"], dtype=np.float32)
            self.probe_b = float(pj["b"])
        elif self.method == "linear_probe":
            raise FileNotFoundError(f"{probe} missing (needed by the linear_probe gate)")
        if self.method not in METHODS:
            raise ValueError(f"unknown gate method {self.method!r}")
        self._fn = METHODS[self.method]

    def animal_score(self, emb: np.ndarray) -> float:
        return float(self._fn(self, emb))

    def keep_score(self, score: float, detection_threshold: float) -> bool:
        return float(score) >= threshold_for(detection_threshold, self.method)

    def keep(self, emb: np.ndarray, detection_threshold: float) -> bool:
        return self.keep_score(self.animal_score(emb), detection_threshold)
