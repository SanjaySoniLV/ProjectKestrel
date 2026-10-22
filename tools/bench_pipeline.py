"""Per-stage analysis benchmark: run the analyzer on a folder and report time per image and peak RSS.

    python tools/bench_pipeline.py FOLDER [--repo PATH] [--gpu] [--prefetch 1] [--json out.json]
    python tools/bench_pipeline.py --compare old.json new.json

The folder is copied to a scratch directory (``--workdir``, default a temp dir)
so the source photos are never touched and every run starts fresh. ``--repo``
points at any ProjectKestrel checkout (default: this one), so the SAME tool
measures an old build and pipeline 3.0 on the same photos: it times the model
calls by wrapping whatever classes that checkout has (SpeciesNet classifier,
legacy species CNN, BioCLIP, CLIP, SigLIP, gate/routing, quality CNN), so no
instrumentation has to exist in the measured code.

Stages (seconds per analysed image; ``model load`` reported separately):
  decode          RAW/JPEG decode + metering (``AnalysisPipeline._decode_image``)
  detector        MegaDetector preprocess + predict
  sam             SAM-HQ encoder + mask decode
  speciesnet      SpeciesNet classifier (pre-3.0 builds)
  bird_cnn        legacy North-American species CNN (main / pre-BioCLIP builds)
  bioclip         BioCLIP 2 image tower
  gate_route      false-positive gate + bird/wildlife routing (3.0)
  siglip_crop     SigLIP 2 on subject crops (3.0)
  siglip_frame    SigLIP 2 on the downscaled whole frame (3.0)
  clip            OpenAI CLIP crop embeddings (exp/semantic-search prototype)
  legacy_quality  quality CNN
  writes          export/crop JPEG writes + CSV saves
  other           everything else (similarity/AKAZE, exposure solve, crops, bookkeeping)
Peak RSS is the process high-water mark (``ru_maxrss``), so run one build per process
(this tool does) and compare like with like (same --prefetch, same CPU/GPU).
"""

from __future__ import annotations

import argparse
import functools
import json
import os
import platform
import resource
import shutil
import sys
import tempfile
import threading
import time
from collections import defaultdict

STAGE_ORDER = ["decode", "detector", "sam", "speciesnet", "bird_cnn", "bioclip", "gate_route",
               "siglip_crop", "siglip_frame", "clip", "legacy_quality", "writes", "other"]

_lock = threading.Lock()
_times = defaultdict(float)
_counts = defaultdict(int)
_tls = threading.local()


def _wrap(owner, name, stage, classify=None):
    """Time ``owner.name`` into ``stage`` (``classify(args)`` may pick the stage per call).
    Nested timed calls only count once (the outermost), so stages never double count."""
    fn = getattr(owner, name, None)
    if fn is None or getattr(fn, "_bench_wrapped", False):
        return False

    @functools.wraps(fn)
    def inner(*a, **kw):
        if getattr(_tls, "depth", 0):
            return fn(*a, **kw)
        _tls.depth = 1
        t0 = time.perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            _tls.depth = 0
            st = classify(a, kw) if classify else stage
            with _lock:
                _times[st] += time.perf_counter() - t0
                _counts[st] += 1

    inner._bench_wrapped = True
    setattr(owner, name, inner)
    return True


def _rss_mb() -> float:
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1024.0 if sys.platform != "darwin" else r / (1024.0 * 1024.0)


def _instrument(analyzer_dir: str) -> list:
    import importlib

    hooked = []

    def mod(name):
        try:
            return importlib.import_module(name)
        except Exception:
            return None

    pl = mod("kestrel_analyzer.pipeline")
    if pl is None:
        raise SystemExit(f"cannot import kestrel_analyzer.pipeline from {analyzer_dir}")
    if _wrap(pl.AnalysisPipeline, "_decode_image", "decode"):
        hooked.append("decode")
    if _wrap(pl, "_write_jpeg", "writes"):
        hooked.append("writes:jpeg")
    if _wrap(pl.AnalysisPipeline, "_save_database_soft", "writes"):
        hooked.append("writes:csv")
    if _wrap(pl, "save_database", "writes"):
        hooked.append("writes:csv_final")
    sn = mod("kestrel_analyzer.ml.speciesnet_sam_hq")
    if sn is not None:
        for cls in ("OnnxMDv5Detector", "OnnxMDv1000CedarDetector"):
            c = getattr(sn, cls, None)
            if c is not None:
                _wrap(c, "predict", "detector")
                _wrap(c, "preprocess", "detector")
        if getattr(sn, "OnnxSamPredictor", None) is not None:
            for m in ("encode", "decode_boxes", "decode_box"):
                _wrap(sn.OnnxSamPredictor, m, "sam")
        if getattr(sn, "OnnxClassifier", None) is not None:
            for m in ("predict_many", "predict", "preprocess_many"):
                _wrap(sn.OnnxClassifier, m, "speciesnet")
            hooked.append("speciesnet")
        hooked += ["detector", "sam"]
    bc = mod("kestrel_analyzer.ml.bioclip_species")
    if bc is not None:
        _wrap(bc.BioClipSpeciesClassifier, "embed", "bioclip")
        if hasattr(bc.BioClipSpeciesClassifier, "route"):
            _wrap(bc.BioClipSpeciesClassifier, "route", "gate_route")
        hooked.append("bioclip")
    for legacy in ("kestrel_analyzer.ml.bird_species", "kestrel_analyzer.ml.species"):
        m = mod(legacy)
        if m is not None:
            for cname in dir(m):
                c = getattr(m, cname)
                if isinstance(c, type) and "Species" in cname and hasattr(c, "classify"):
                    _wrap(c, "classify", "bird_cnn")
                    if hasattr(c, "classify_batch"):
                        _wrap(c, "classify_batch", "bird_cnn")
                    hooked.append(f"bird_cnn:{cname}")
    ag = mod("kestrel_analyzer.ml.animal_gate")
    if ag is not None:
        _wrap(ag.AnimalGate, "animal_score", "gate_route")
        hooked.append("gate")
    sg = mod("kestrel_analyzer.ml.siglip")
    if sg is not None:
        def _which(a, kw):
            img = a[1] if len(a) > 1 else kw.get("images")
            try:
                import numpy as np
                arr = img if hasattr(img, "shape") else img[0]
                h, w = np.asarray(arr).shape[:2]
                return "siglip_crop" if h == w else "siglip_frame"
            except Exception:
                return "siglip_crop"
        _wrap(sg.SiglipImageEmbedder, "embed", "siglip_crop", classify=_which)
        hooked.append("siglip")
    cs = mod("kestrel_analyzer.ml.clip_search")
    if cs is not None:
        _wrap(cs.ClipImageEmbedder, "embed", "clip")
        hooked.append("clip")
    q = mod("kestrel_analyzer.ml.quality")
    if q is not None:
        _wrap(q.QualityClassifier, "classify", "legacy_quality")
        hooked.append("legacy_quality")
    return hooked


def run(args) -> dict:
    repo = os.path.abspath(args.repo)
    analyzer_dir = os.path.join(repo, "analyzer")
    sys.path.insert(0, analyzer_dir)
    os.environ.setdefault("KESTREL_LOG_LEVEL", "WARN")
    hooked = _instrument(analyzer_dir)
    from kestrel_analyzer import pipeline as pl
    try:
        from kestrel_analyzer.config import VERSION
    except Exception:
        VERSION = "?"

    work = args.workdir or tempfile.mkdtemp(prefix="kestrel_bench_")
    dst = os.path.join(work, os.path.basename(os.path.abspath(args.folder).rstrip(os.sep)) or "photos")
    if os.path.exists(dst):
        shutil.rmtree(dst)
    os.makedirs(dst)
    for n in sorted(os.listdir(args.folder)):
        p = os.path.join(args.folder, n)
        if os.path.isfile(p) and not n.startswith("."):
            shutil.copy2(p, dst)

    p = pl.AnalysisPipeline(use_gpu=args.gpu, detector_name=args.detector)
    t_load = time.perf_counter()
    if args.preload:
        kw = {"max_bird_crops": args.max_crops}
        p.load_models(**kw)
        # Load the lazily-built detector/SAM sessions too, so the per-image
        # numbers exclude model construction.
        for attr in ("ensure_loaded",):
            fn = getattr(getattr(p, "sn_sam", None), attr, None)
            if callable(fn):
                fn()
    t_load = time.perf_counter() - t_load
    rss_after_load = _rss_mb()
    with _lock:
        _times.clear()
        _counts.clear()
    kwargs = dict(
        analyzer_name="bench",
        wildlife_enabled=args.wildlife,
        species_detection_enabled=True,
        detection_threshold=args.threshold,
        max_bird_crops=args.max_crops,
        parallel_prefetch=args.prefetch,
    )
    import inspect
    sig = inspect.signature(p.process_folder).parameters
    if "species_regions" in sig:
        kwargs["species_regions"] = ["NA"]
    if "smart_search_enabled" in sig:
        kwargs["smart_search_enabled"] = not args.no_smart_search
    t0 = time.perf_counter()
    p.process_folder(dst, **kwargs)
    wall = time.perf_counter() - t0
    try:
        import pandas as pd
        db = pd.read_csv(os.path.join(dst, ".kestrel", "kestrel_database.csv"))
        n_images = int(len(db))
        n_crops = int(sum(len([c for c in json.loads(cj or "[]") if int(c.get("detection_index", -1)) >= 0])
                          for cj in db.get("crops_json", [])))
        errors = int((db["species"].astype(str) == "Error").sum())
    except Exception:
        n_images, n_crops, errors = len(os.listdir(dst)), -1, -1
    stages = dict(_times)
    counted = sum(stages.values())
    stages["other"] = max(0.0, wall - counted)
    out = {
        "repo": repo,
        "version": VERSION,
        "git": _git_head(repo),
        "folder": os.path.abspath(args.folder),
        "images": n_images,
        "crops": n_crops,
        "errors": errors,
        "use_gpu": bool(args.gpu),
        "prefetch": args.prefetch,
        "cpu": platform.processor() or platform.machine(),
        "cpu_count": os.cpu_count(),
        "model_load_s": round(t_load, 2),
        "wall_s": round(wall, 2),
        "s_per_image": round(wall / max(1, n_images), 3),
        "stage_s_per_image": {k: round(v / max(1, n_images), 4) for k, v in stages.items()},
        "stage_calls": dict(_counts),
        "peak_rss_mb_after_load": round(rss_after_load, 1),
        "peak_rss_mb": round(_rss_mb(), 1),
        "hooked": hooked,
    }
    if not args.keep:
        shutil.rmtree(dst, ignore_errors=True)
        if not args.workdir:
            shutil.rmtree(work, ignore_errors=True)
    return out


def _git_head(repo):
    try:
        import subprocess
        return subprocess.check_output(["git", "-C", repo, "log", "-1", "--format=%h %s"], text=True).strip()[:80]
    except Exception:
        return ""


def table(results) -> str:
    cols = [r.get("label") or r.get("git", "")[:20] or r["version"] for r in results]
    lines = ["| stage (s/image) | " + " | ".join(cols) + " |", "|---|" + "---:|" * len(cols)]
    for st in STAGE_ORDER:
        vals = [r["stage_s_per_image"].get(st) for r in results]
        if all(not v for v in vals):
            continue
        lines.append(f"| {st} | " + " | ".join("—" if not v else f"{v:.3f}" for v in vals) + " |")
    for key, lab in (("s_per_image", "**total s/image**"), ("model_load_s", "model load (s)"),
                     ("peak_rss_mb_after_load", "RSS after load (MB)"), ("peak_rss_mb", "peak RSS (MB)"),
                     ("images", "images"), ("crops", "subject crops"), ("errors", "errors")):
        lines.append(f"| {lab} | " + " | ".join(str(r.get(key)) for r in results) + " |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("folder", nargs="?")
    ap.add_argument("--repo", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    ap.add_argument("--gpu", action="store_true")
    ap.add_argument("--prefetch", type=int, default=1)
    ap.add_argument("--threshold", type=float, default=0.25)
    ap.add_argument("--max-crops", type=int, default=5)
    ap.add_argument("--detector", default="mdv5a")
    ap.add_argument("--wildlife", action="store_true")
    ap.add_argument("--no-smart-search", action="store_true")
    ap.add_argument("--no-preload", dest="preload", action="store_false")
    ap.add_argument("--workdir")
    ap.add_argument("--keep", action="store_true", help="keep the analysed scratch copy")
    ap.add_argument("--label")
    ap.add_argument("--json")
    ap.add_argument("--compare", nargs="+", metavar="JSON")
    a = ap.parse_args()
    if a.compare:
        res = [json.load(open(p)) for p in a.compare]
        print(table(res))
        return
    if not a.folder:
        ap.error("folder is required")
    out = run(a)
    if a.label:
        out["label"] = a.label
    print(json.dumps(out, indent=1))
    print(table([out]))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(out, f, indent=1)


if __name__ == "__main__":
    main()
