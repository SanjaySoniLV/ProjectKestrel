"""Run model_bench.py for every (model, device) pair, one subprocess each, with a timeout.

Usage: python run_matrix.py --python <interp> --devices cpu,dml,... --models a,b,... --feeds feeds.pkl --outdir DIR
                            [--runs N] [--warmup W] [--timeout S] [--tag TAG]
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ALL = ["detector_mdv5a", "sam_enc", "sam_dec", "bioclip_int8", "bioclip_wdq16",
       "siglip_int8", "siglip_wdq16", "siglip_text", "quality_legacy"]

ap = argparse.ArgumentParser()
ap.add_argument("--python", required=True)
ap.add_argument("--devices", required=True)
ap.add_argument("--models", default=",".join(ALL))
ap.add_argument("--feeds", required=True)
ap.add_argument("--outdir", required=True)
ap.add_argument("--runs", type=int, default=5)
ap.add_argument("--warmup", type=int, default=2)
ap.add_argument("--timeout", type=int, default=900)
ap.add_argument("--tag", default="")
ap.add_argument("--ov-opt", action="append", default=[])
a = ap.parse_args()
os.makedirs(a.outdir, exist_ok=True)
for dev in a.devices.split(","):
    for m in a.models.split(","):
        out = os.path.join(a.outdir, f"{m}__{dev}{a.tag}.json")
        cmd = [a.python, "-I", os.path.join(HERE, "model_bench.py"), "--model", m, "--device", dev, "--feeds", a.feeds,
               "--out", out, "--runs", str(a.runs), "--warmup", str(a.warmup)]
        for o in a.ov_opt:
            cmd += ["--ov-opt", o]
        print(">>", m, dev, flush=True)
        try:
            env = dict(os.environ, PYTHONPATH=HERE)
            r = subprocess.run(cmd, timeout=a.timeout, capture_output=True, text=True, env=env)
            line = [l for l in r.stdout.splitlines() if l.startswith("{")]
            print(line[-1] if line else f"no result (rc={r.returncode}) {r.stderr[-400:]}", flush=True)
            if not line:
                json.dump({"model": m, "device": dev, "ok": False, "error": f"crash rc={r.returncode}: {r.stderr[-600:]}"}, open(out, "w"))
        except subprocess.TimeoutExpired:
            print("TIMEOUT", flush=True)
            json.dump({"model": m, "device": dev, "ok": False, "error": f"timeout > {a.timeout}s"}, open(out, "w"))
