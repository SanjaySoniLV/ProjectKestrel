"""Can CLIP-style embeddings say anything about photo quality / pose?

    python quality_probe.py --work DIR [--work DIR ...] --emb TAG=npz_name [...] --out OUT

For each embedding set (e.g. BioCLIP 2 = img_bioclip-2_f1.npz, a general CLIP
= img_clip-b16_f1.npz, all keyed by crop_id) and every crop with a Kestrel
quality score:

1. Zero-shot "text directions": score = cos(img, "a photo of <pos>") -
   cos(img, "a photo of <neg>") for a few prompt pairs (sharp vs blurry, ...),
   reported as Spearman rho against Kestrel's quality score (higher = the
   model ranks images the way Kestrel's quality model does).
2. Linear probe: ridge regression embedding -> quality, trained on all but one
   perch, tested on the held-out perch (Spearman rho). This asks whether the
   embedding Kestrel already computes could replace / feed a quality model.

Text prompts for (1) are encoded with the matching model (needs open_clip).
"""
import argparse
import csv
import json
import os

import numpy as np
import open_clip
import torch
from scipy.stats import spearmanr

PAIRS = [
    ("a sharp, in-focus photo of a bird", "a blurry, out-of-focus photo of a bird"),
    ("a clear, detailed photo of a bird", "a noisy, grainy photo of a bird"),
    ("a photo of a bird with its eye visible", "a photo of a bird with its eye hidden"),
    ("an unobstructed photo of a bird", "a photo of a bird hidden behind branches and leaves"),
    ("a well-exposed photo of a bird", "an underexposed, dark photo of a bird"),
    ("a high quality wildlife photograph", "a low quality snapshot"),
]
MODELS = {"bioclip-2": ("hf-hub:imageomics/bioclip-2", None), "clip-b16": ("ViT-B-16", "openai"),
          "clip-l14": ("ViT-L-14", "openai")}


def load_quality(work_dirs):
    q = {}
    for w in work_dirs:
        for r in csv.DictReader(open(os.path.join(w, "data", "crops_index.csv"))):
            if r["quality"] not in ("", "None"):
                q[r["crop_id"]] = (float(r["quality"]), r["slug"])
    return q


def load_emb(work_dirs, name):
    E, ids = [], []
    for w in work_dirs:
        # validation dirs hold the shipped-model embeddings as *_app.npz
        cands = [os.path.join(w, "cache", n) for n in (name, name.replace(".npz", "_app.npz"))]
        p = next((c for c in cands if os.path.exists(c)), None)
        if p:
            d = np.load(p)
            E.append(d["emb"].astype(np.float32))
            ids += list(d["crop_id"])
    E = np.concatenate(E)
    return E / np.linalg.norm(E, axis=1, keepdims=True), ids


def text_dirs(model_key):
    hub, tag = MODELS[model_key]
    m, _, _ = open_clip.create_model_and_transforms(hub, pretrained=tag) if tag else open_clip.create_model_and_transforms(hub)
    tok = open_clip.get_tokenizer(hub)
    m.eval()
    with torch.inference_mode():
        t = torch.nn.functional.normalize(m.encode_text(tok([s for p in PAIRS for s in p])), dim=-1).numpy()
    return t.reshape(len(PAIRS), 2, -1)


def ridge_lopo(X, y, groups, lam=10.0):
    out = {}
    for g in sorted(set(groups)):
        tr, te = groups != g, groups == g
        if te.sum() < 30:
            continue
        mu = X[tr].mean(0)
        A = X[tr] - mu
        w = np.linalg.solve(A.T @ A + lam * np.eye(A.shape[1]), A.T @ (y[tr] - y[tr].mean()))
        pred = (X[te] - mu) @ w
        out[g] = float(spearmanr(pred, y[te]).correlation)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", action="append", required=True)
    ap.add_argument("--emb", action="append", required=True, help="model_key=cache_file.npz")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    qual = load_quality(a.work)
    res = {}
    for spec in a.emb:
        key, fname = spec.split("=")
        X, ids = load_emb(a.work, fname)
        keep = [i for i, c in enumerate(ids) if c in qual]
        X = X[keep]
        y = np.array([qual[ids[i]][0] for i in keep])
        groups = np.array([qual[ids[i]][1] for i in keep])
        T = text_dirs(key)
        zs = {f"{p[0]}  vs  {p[1]}": float(spearmanr(X @ T[k, 0] - X @ T[k, 1], y).correlation)
              for k, p in enumerate(PAIRS)}
        res[key] = {"n": int(len(y)), "zero_shot_spearman_vs_kestrel_quality": zs,
                    "ridge_probe_leave_one_perch_out_spearman": ridge_lopo(X, y, groups)}
        print(json.dumps({key: res[key]}, indent=1), flush=True)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    json.dump(res, open(a.out, "w"), indent=1)


if __name__ == "__main__":
    main()
