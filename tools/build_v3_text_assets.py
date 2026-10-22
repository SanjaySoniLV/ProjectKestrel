"""Precompute the small text-side assets the v3 pipeline reads at analysis time.

    python tools/build_v3_text_assets.py

Writes ``analyzer/models/siglip/quality_prompt_emb.npy`` (float32 [2,768], the
L2-normalised SigLIP 2 embeddings of the ``pos`` / ``neg`` focus prompts in
``quality_calibration.json``, exactly as given -- no template) plus a sidecar
``quality_prompt_emb.json`` naming the prompts, so ``SiglipQualityScorer``
never has to load the text tower during analysis. Re-run whenever the
calibration prompts change (the scorer falls back to encoding them at load if
the sidecar does not match).

Uses only the app's own runtime (onnxruntime + numpy); no torch needed.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "analyzer"))

import numpy as np  # noqa: E402

from kestrel_analyzer.ml import siglip  # noqa: E402


def main() -> None:
    d = siglip.default_assets_dir()
    with open(d / siglip.CALIBRATION_FILENAME, encoding="utf-8") as f:
        prompts = json.load(f)["prompts"]
    enc = siglip.SiglipTextEncoder(d, idle_release_sec=None)
    emb = enc.encode_texts([prompts["pos"], prompts["neg"]]).astype(np.float32)
    np.save(d / siglip.QUALITY_PROMPT_EMB_FILENAME, emb)
    with open((d / siglip.QUALITY_PROMPT_EMB_FILENAME).with_suffix(".json"), "w", encoding="utf-8") as f:
        json.dump({"model": siglip.MODEL_ID, "prompts": [prompts["pos"], prompts["neg"]],
                   "built_by": "tools/build_v3_text_assets.py"}, f, indent=1)
    print("wrote", d / siglip.QUALITY_PROMPT_EMB_FILENAME, emb.shape, "cos(pos,neg)=%.4f" % float(emb[0] @ emb[1]))


if __name__ == "__main__":
    main()
