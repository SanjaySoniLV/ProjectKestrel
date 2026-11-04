"""Validation of the vendor execution provider for the enhanced GPU mode.

``validate_kind`` loads one routable model on the vendor EP and on the baseline (DirectML,
else CPU), runs bundled sample frames through both, and decides whether the model may be
routed to the vendor EP:

* the first vendor inference must succeed (the legacy quality model failed here on OpenVINO),
* every output must be finite and match the baseline (cosine >= ``MIN_COSINE``),
* the median model-run time, measured in steady state after ``WARMUP_RUNS`` runs, must be
  more than ``MIN_SPEEDUP`` times as fast as the baseline. Only model time is compared; RAW
  decode and other CPU work are not part of the criterion.

Vendor EP crashes (access violations) cannot be caught, so ``main`` is meant to run in a child
process: it appends one JSON line per finished model to the output file, and the parent treats
a model with no line as crashed. ``python cli.py --cli --enhanced-gpu-validate ...`` starts it.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .enhanced_gpu import MIN_COSINE, MIN_SPEEDUP, ROUTABLE_KINDS, TIMED_RUNS, WARMUP_RUNS

# kind -> (model path relative to MODELS_DIR, session config, normalisation, sample source)
_MODELS: Dict[str, dict] = {
    "detector": {"file": "speciesnet/mdv5a.onnx", "config": {}, "norm": "unit", "sample": "export"},
    "sam_enc": {"file": "speciesnet/sam_hq_vit_tiny_encoder.onnx", "config": {}, "norm": "imagenet",
                "sample": "export"},
    "bird_species": {"file": "bioclip/bioclip2_image_wdq16.onnx",
                     "config": {"session.disable_quant_qdq": "1"}, "norm": "imagenet", "sample": "crop"},
}
_IMAGENET_MEAN = (0.485, 0.456, 0.406)
_IMAGENET_STD = (0.229, 0.224, 0.225)
_SAMPLE_COUNT = 3


def model_file(kind: str) -> str:
    return Path(_MODELS[kind]["file"]).name


def _sample_images(source: str) -> List[Path]:
    from ..config import ANALYZER_DIR
    base = Path(ANALYZER_DIR) / "sample_sets" / "backyard_birds" / ".kestrel" / source
    files = sorted(base.glob("*.jpg"))
    if not files:
        raise FileNotFoundError(f"no bundled sample images in {base}")
    step = max(1, len(files) // _SAMPLE_COUNT)
    return files[::step][:_SAMPLE_COUNT]


def _feeds(sess, norm: str, source: str) -> List[dict]:
    import numpy as np
    from PIL import Image
    inp = sess.get_inputs()[0]
    shape = list(inp.shape)
    h = shape[2] if isinstance(shape[2], int) else 640
    w = shape[3] if isinstance(shape[3], int) else 640
    feeds = []
    for p in _sample_images(source):
        with Image.open(p) as im:
            arr = np.asarray(im.convert("RGB").resize((w, h)), dtype=np.float32) / 255.0
        if norm == "imagenet":
            arr = (arr - np.array(_IMAGENET_MEAN, dtype=np.float32)) / np.array(_IMAGENET_STD, dtype=np.float32)
        feeds.append({inp.name: np.ascontiguousarray(arr.transpose(2, 0, 1)[None], dtype=np.float32)})
    return feeds


def _cosine(a, b) -> float:
    import numpy as np
    a = np.asarray(a, dtype=np.float64).ravel()
    b = np.asarray(b, dtype=np.float64).ravel()
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na == 0.0 and nb == 0.0:
        return 1.0
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def compare_outputs(base_outs: Sequence[Sequence], vendor_outs: Sequence[Sequence]) -> dict:
    """Minimum cosine over all outputs and inputs, and whether every vendor value is finite."""
    import numpy as np
    cos = 1.0
    finite = True
    for b_run, v_run in zip(base_outs, vendor_outs):
        for b, v in zip(b_run, v_run):
            if not np.all(np.isfinite(v)):
                finite = False
            cos = min(cos, _cosine(b, v))
    return {"cosine": cos, "finite": finite}


def decide(cosine: float, finite: bool, speedup: float) -> Optional[str]:
    """None when the model passes, else the reason it does not."""
    if not finite:
        return "vendor output contained non-finite values"
    if cosine < MIN_COSINE:
        return f"output differs from DirectML (cosine {cosine:.6f} < {MIN_COSINE})"
    if speedup <= MIN_SPEEDUP:
        return f"not faster than DirectML ({speedup:.2f}x <= {MIN_SPEEDUP}x)"
    return None


def _timed(sess, feeds: List[dict]) -> float:
    for i in range(WARMUP_RUNS):
        sess.run(None, feeds[i % len(feeds)])
    times = []
    for i in range(TIMED_RUNS):
        t0 = time.perf_counter()
        sess.run(None, feeds[i % len(feeds)])
        times.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(times)


def validate_kind(kind: str, ep_name: str) -> dict:
    """Validate one model on ``ep_name``. Never raises (a Python-level failure is a failed model)."""
    spec = _MODELS[kind]
    result: dict = {"kind": kind, "file": model_file(kind), "ep": ep_name, "enabled": False}
    try:
        import onnxruntime as ort
        from ..config import MODELS_DIR
        from .enhanced_gpu import ensure_runtime, gpu_devices_for
        path = str(Path(MODELS_DIR) / spec["file"])
        ensure_runtime(download=False)

        def opts():
            so = ort.SessionOptions()
            for k, v in spec["config"].items():
                so.add_session_config_entry(k, v)
            return so

        base_prov = ["DmlExecutionProvider", "CPUExecutionProvider"] \
            if "DmlExecutionProvider" in ort.get_available_providers() else ["CPUExecutionProvider"]
        base = ort.InferenceSession(path, sess_options=opts(), providers=base_prov)
        result["baseline_ep"] = base.get_providers()[0]
        feeds = _feeds(base, spec["norm"], spec["sample"])
        base_outs = [base.run(None, f) for f in feeds]
        base_ms = _timed(base, feeds)
        del base

        so = opts()
        so.add_provider_for_devices(gpu_devices_for(ort, ep_name), {})
        t0 = time.perf_counter()
        vend = ort.InferenceSession(path, sess_options=so)
        result["load_s"] = round(time.perf_counter() - t0, 2)
        vend_outs = [vend.run(None, f) for f in feeds]  # first inference
        cmp = compare_outputs(base_outs, vend_outs)
        vend_ms = _timed(vend, feeds)
        speedup = base_ms / vend_ms if vend_ms > 0 else 0.0
        result.update(cosine=round(cmp["cosine"], 6), baseline_ms=round(base_ms, 1), vendor_ms=round(vend_ms, 1),
                      speedup=round(speedup, 2))
        reason = decide(cmp["cosine"], cmp["finite"], speedup)
        result["enabled"] = reason is None
        result["reason"] = reason or ""
    except Exception as e:
        result["reason"] = f"{type(e).__name__}: {str(e)[:300]}"
    return result


def main(ep_name: str, kinds: Sequence[str], out_path: str) -> int:
    """Child-process entry: append one JSON line per finished model to ``out_path``."""
    rc = 0
    for kind in kinds:
        if kind not in ROUTABLE_KINDS:
            continue
        r = validate_kind(kind, ep_name)
        try:
            with open(out_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(r) + "\n")
                f.flush()
        except OSError:
            rc = 2
    return rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1], sys.argv[2].split(","), sys.argv[3]))
