"""Q1 compute benchmark: per-call CPU latency of Kestrel's existing ONNX models
vs. the BioCLIP image towers (PyTorch fp32, ONNX fp32, ONNX int8-dynamic).

    python bench.py [--threads 4] [--reps 10] [--skip-export]

All models run batch=1 on the same CPU with the same thread count, so the
numbers are comparable to each other (not to a user's GPU/DirectML/CoreML).
"""
import argparse
import glob
import json
import os
import time

import numpy as np
import onnxruntime as ort

from common import CACHE, REPO

MD = os.path.join(REPO, "analyzer", "models")
EXISTING = {
    "Kestrel species CNN (model.onnx)": "model.onnx",
    "Quality (quality.onnx)": "quality.onnx",
    "MegaDetector v5a (mdv5a.onnx)": "speciesnet/mdv5a.onnx",
    "MDv1000-cedar": "speciesnet/mdv1000-cedar.onnx",
    "SAM-HQ ViT-T encoder": "speciesnet/sam_hq_vit_tiny_encoder.onnx",
    "SAM-HQ ViT-T decoder": "speciesnet/sam_hq_vit_tiny_decoder.onnx",
    "SpeciesNet classifier": "speciesnet/speciesNet_v4.0.1a.onnx",
}


def size_mb(path):
    """File size, plus any external-data sidecars (.data file or a dedicated dir)."""
    if os.path.basename(os.path.dirname(path)).startswith("onnx_"):
        d = os.path.dirname(path)
        return sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d)
                   if not f.endswith(".int8.onnx") or path.endswith(".int8.onnx")) / 1e6
    tot = os.path.getsize(path)
    if os.path.exists(path + ".data"):
        tot += os.path.getsize(path + ".data")
    return tot / 1e6


def feed_for(sess):
    feed = {}
    for i in sess.get_inputs():
        shape = [d if isinstance(d, int) else 1 for d in i.shape]
        dt = {"tensor(float)": np.float32, "tensor(int64)": np.int64, "tensor(int32)": np.int32,
              "tensor(bool)": np.bool_, "tensor(float16)": np.float16}.get(i.type, np.float32)
        feed[i.name] = (np.random.rand(*shape) * (255 if len(shape) == 4 else 1)).astype(dt)
    return feed


def time_fn(fn, reps):
    fn()  # warm-up
    fn()
    ts = []
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t) * 1000)
    return float(np.median(ts))


def bench_onnx(path, threads, reps):
    so = ort.SessionOptions()
    so.intra_op_num_threads = threads
    sess = ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])
    feed = feed_for(sess)
    return time_fn(lambda: sess.run(None, feed), reps)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--reps", type=int, default=10)
    ap.add_argument("--skip-export", action="store_true")
    ap.add_argument("--skip-existing", action="store_true")
    ap.add_argument("--models", default="bioclip-1,bioclip-2,bioclip-2.5")
    a = ap.parse_args()
    res = []
    for name, rel in ({} if a.skip_existing else EXISTING).items():
        p = os.path.join(MD, rel)
        try:
            ms = bench_onnx(p, a.threads, a.reps)
        except Exception as e:  # noqa: BLE001
            print(name, "FAILED", str(e)[:120])
            continue
        res.append({"model": name, "runtime": "onnxruntime fp32", "ms": ms, "mb": size_mb(p)})
        print(res[-1], flush=True)

    import open_clip
    import torch
    from embed_images import MODELS

    torch.set_num_threads(a.threads)
    os.makedirs(CACHE, exist_ok=True)
    for key in a.models.split(","):
        hub = MODELS[key]
        model, _, _ = open_clip.create_model_and_transforms(hub)
        vis = model.visual.eval()
        nparam = sum(p.numel() for p in vis.parameters())
        x = torch.randn(1, 3, 224, 224)
        with torch.inference_mode():
            ms = time_fn(lambda: vis(x), a.reps)
        res.append({"model": f"{key} image tower", "runtime": "torch fp32", "ms": ms, "mb": nparam * 4 / 1e6,
                    "params_m": nparam / 1e6})
        print(res[-1], flush=True)
        with torch.inference_mode(), torch.autocast("cpu", dtype=torch.bfloat16):
            ms = time_fn(lambda: vis(x), a.reps)
        res.append({"model": f"{key} image tower", "runtime": "torch bf16 autocast (AMX)", "ms": ms,
                    "mb": nparam * 2 / 1e6})
        print(res[-1], flush=True)
        if a.skip_export:
            continue
        big = nparam * 4 > 2e9  # >2 GB protobuf limit -> external data in its own dir
        if big:
            os.makedirs(os.path.join(CACHE, f"onnx_{key}"), exist_ok=True)
            onnx_path = os.path.join(CACHE, f"onnx_{key}", "visual.onnx")
        else:
            onnx_path = os.path.join(CACHE, f"{key}_visual.onnx")
        if not os.path.exists(onnx_path):
            torch.onnx.export(vis, x, onnx_path, input_names=["image"], output_names=["emb"],
                              dynamic_axes={"image": {0: "b"}, "emb": {0: "b"}}, opset_version=17,
                              dynamo=False)
        res.append({"model": f"{key} image tower", "runtime": "onnxruntime fp32",
                    "ms": bench_onnx(onnx_path, a.threads, a.reps), "mb": size_mb(onnx_path)})
        print(res[-1], flush=True)
        q_path = onnx_path.replace(".onnx", ".int8.onnx")
        if not os.path.exists(q_path):
            from onnxruntime.quantization import QuantType, quantize_dynamic
            quantize_dynamic(onnx_path, q_path, weight_type=QuantType.QInt8, use_external_data_format=big)
        res.append({"model": f"{key} image tower", "runtime": "onnxruntime int8 (dynamic)",
                    "ms": bench_onnx(q_path, a.threads, a.reps), "mb": size_mb(q_path)})
        print(res[-1], flush=True)
        del model, vis
    with open(os.path.join(CACHE, "bench_bioclip.json" if a.skip_existing else "bench.json"), "w") as f:
        json.dump(res, f, indent=1)


if __name__ == "__main__":
    main()
