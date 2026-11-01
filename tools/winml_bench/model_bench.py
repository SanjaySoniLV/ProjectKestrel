"""Load one model on one device, check outputs against CPU, and time it.

Usage:
  python model_bench.py --model <key> --device <dev> --feeds feeds.pkl --out result.json [--runs N] [--warmup W]

Devices:  cpu | dml | ov_gpu | ov_npu | ov_cpu | webgpu
Models:   see MODELS below.
The interpreter decides which ORT is used: .venv312 = onnxruntime-directml 1.24.4 (only cpu, dml),
.venv-winml = onnxruntime-windowsml (all devices; vendor EPs are registered through winml_setup).
"""
import argparse
import json
import os
import pickle
import statistics
import sys
import time
import traceback

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.abspath(os.path.join(HERE, "..", "..", "analyzer", "models"))
SP, BC, SG = (os.path.join(MODELS_DIR, d) for d in ("speciesnet", "bioclip", "siglip"))
GPU_CFG = {"session.disable_quant_qdq": "1"}

# key -> (file, feed kind recorded by capture_feeds, session config, ref key on CPU)
MODELS = {
    "detector_mdv5a": (os.path.join(SP, "mdv5a.onnx"), "detector", {}),
    "sam_enc": (os.path.join(SP, "sam_hq_vit_tiny_encoder.onnx"), "sam_enc", {}),
    "sam_dec": (os.path.join(SP, "sam_hq_vit_tiny_decoder.onnx"), "sam_dec", {}),
    "bioclip_int8": (os.path.join(BC, "bioclip2_image_int8.onnx"), "bird_species", {}),
    "bioclip_wdq16": (os.path.join(BC, "bioclip2_image_wdq16.onnx"), "bird_species", GPU_CFG),
    "siglip_int8": (os.path.join(SG, "siglip2_b16_image_int8.onnx"), "siglip_image", {}),
    "siglip_wdq16": (os.path.join(SG, "siglip2_b16_image_wdq16.onnx"), "siglip_image", GPU_CFG),
    "siglip_text": (os.path.join(SG, "siglip2_b16_text_fp16w.onnx"), "__text__", {}),
    "quality_legacy": (os.path.join(MODELS_DIR, "quality.onnx"), "quality", {}),
}
EP_FOR = {"dml": ("DmlExecutionProvider", "GPU"), "ov_gpu": ("OpenVINOExecutionProvider", "GPU"),
          "ov_npu": ("OpenVINOExecutionProvider", "NPU"), "ov_cpu": ("OpenVINOExecutionProvider", "CPU"),
          "webgpu": ("WebGpuExecutionProvider", "GPU")}


def make_session(ort, path, device, cfg, ov_opts=None):
    so = ort.SessionOptions()
    for k, v in cfg.items():
        so.add_session_config_entry(k, v)
    if device == "cpu":
        return ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])
    ep, typ = EP_FOR[device]
    if device == "dml" and not hasattr(ort, "get_ep_devices"):
        return ort.InferenceSession(path, sess_options=so, providers=["DmlExecutionProvider", "CPUExecutionProvider"])
    if device == "dml":
        # same path the pipeline takes today: provider-name list
        return ort.InferenceSession(path, sess_options=so, providers=["DmlExecutionProvider", "CPUExecutionProvider"])
    devs = [d for d in ort.get_ep_devices() if d.ep_name == ep and str(d.device.type).endswith(typ)]
    if not devs:
        raise RuntimeError(f"no EP device {ep}/{typ}")
    so.add_provider_for_devices(devs, ov_opts or {})
    # no fallback provider listed: ORT adds CPU for unsupported nodes
    return ort.InferenceSession(path, sess_options=so)


def compare(ref, out):
    res = []
    for r, o in zip(ref, out):
        r = np.asarray(r, dtype=np.float64)
        o = np.asarray(o, dtype=np.float64)
        if r.shape != o.shape:
            res.append({"shape_mismatch": [list(r.shape), list(o.shape)]})
            continue
        if not np.isfinite(o).all():
            res.append({"nonfinite": int((~np.isfinite(o)).sum())})
            continue
        rf, of = r.ravel(), o.ravel()
        denom = (np.linalg.norm(rf) * np.linalg.norm(of)) or 1.0
        res.append({"cos": float(rf @ of / denom), "max_abs": float(np.abs(rf - of).max()),
                    "ref_absmax": float(np.abs(rf).max()), "n": int(rf.size)})
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--device", required=True)
    ap.add_argument("--feeds", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--ov-opt", action="append", default=[], help="key=value OpenVINO provider option")
    a = ap.parse_args()
    ov_opts = dict(kv.split("=", 1) for kv in a.ov_opt)

    path, kind, cfg = MODELS[a.model]
    result = {"model": a.model, "device": a.device, "file": os.path.basename(path), "ov_opts": ov_opts}
    try:
        import onnxruntime as ort
        result["ort"] = ort.__version__
        if a.device.startswith("ov_") or a.device == "webgpu":
            import winml_setup
            winml_setup.init(download=False, verbose=False)
        if kind == "__text__":
            rng = np.random.default_rng(0)
            feeds = [{"x": (rng.standard_normal((1, 64, 768)) * 0.02).astype(np.float32)} for _ in range(4)]
        else:
            feeds = pickle.load(open(a.feeds, "rb"))["feeds"][kind]
        result["n_feeds"] = len(feeds)
        result["input_shapes"] = {k: list(v.shape) for k, v in feeds[0].items()}

        # CPU reference (always the ORT in use, CPU EP, same graph)
        ref_sess = make_session(ort, path, "cpu", cfg)
        refs = [ref_sess.run(None, f) for f in feeds]
        del ref_sess

        t0 = time.perf_counter()
        sess = make_session(ort, path, a.device, cfg, ov_opts)
        result["load_s"] = time.perf_counter() - t0
        result["providers"] = sess.get_providers()

        # first run = compile / kernel setup
        t0 = time.perf_counter()
        outs = [sess.run(None, feeds[0])]
        result["first_run_s"] = time.perf_counter() - t0
        outs += [sess.run(None, f) for f in feeds[1:]]
        result["compare"] = [compare(r, o) for r, o in zip(refs, outs)]
        cos = [c["cos"] for cl in result["compare"] for c in cl if "cos" in c]
        mx = [c["max_abs"] / (c["ref_absmax"] or 1.0) for cl in result["compare"] for c in cl if "max_abs" in c]
        result["cos_min"] = min(cos) if cos else None
        result["relmax_worst"] = max(mx) if mx else None
        result["bad"] = sum(1 for cl in result["compare"] for c in cl if "cos" not in c)

        # latency: each timed run uses the next feed in rotation
        for i in range(a.warmup):
            sess.run(None, feeds[i % len(feeds)])
        lat = []
        for i in range(a.runs):
            t0 = time.perf_counter()
            sess.run(None, feeds[i % len(feeds)])
            lat.append((time.perf_counter() - t0) * 1000)
        lat.sort()
        result["lat_ms"] = {"median": statistics.median(lat), "p90": lat[min(len(lat) - 1, int(0.9 * len(lat)))],
                            "min": lat[0], "max": lat[-1], "n": len(lat)}
        result["ok"] = True
    except Exception as e:
        result["ok"] = False
        result["error"] = f"{type(e).__name__}: {str(e)[:1500]}"
        result["trace"] = traceback.format_exc()[-1500:]
    with open(a.out, "w") as f:
        json.dump(result, f, indent=1)
    print(json.dumps({k: result.get(k) for k in ("model", "device", "ok", "load_s", "first_run_s", "cos_min", "relmax_worst", "lat_ms", "error")}))


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    main()
