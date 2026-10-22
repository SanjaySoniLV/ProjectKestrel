"""Calibrate the zero-shot SigLIP 2 sharpness score onto Kestrel's 0-1 quality scale.

    python quality_calibrate.py --work DIR [--work DIR ...] --out calib.json [--exp1b DIR]

Raw score: s = cos(img, P) - cos(img, N), P = "a sharp, in-focus photo of a bird",
N = "a blurry, out-of-focus photo of a bird" (the direction exp 1/1b judged). Its
range is small (~+-0.1) and means nothing to a rating profile, so we fit one
fixed, global, monotone map s -> q by quantile matching: q(s) = the CNN quality
at the same percentile of the pooled reference corpus (every crop with a CNN
score, all perches). The output is a lookup table of KNOTS (s_i, q_i) for
np.interp, so the app needs no model, just the JSON.

Why this shape:
- Fixed and per-image (no per-folder rank normalisation): an image's stars never
  change because of what else is in the folder, which is the current contract
  (api_bridge.apply_normalization maps raw scores through profile thresholds).
- Distribution-matched to the CNN: the rating profiles' cutoffs (balanced 5* =
  q >= 0.85 ...) keep giving roughly the same share of each star overall, so
  users' tuned profiles and custom thresholds stay meaningful across the switch.
- Monotone, so within-folder ordering is exactly SigLIP's.

Reports: leave-one-perch-out stability of the map, per-perch star histograms
(CNN vs SigLIP-mapped, 'balanced' profile), and how many images move by >= 2
stars. With --exp1b, also scores a CNN+SigLIP blend on the judged scenes.
"""
import argparse
import csv
import json
import os
import sys

import numpy as np
import open_clip
import torch
from scipy.stats import spearmanr

POS = "a sharp, in-focus photo of a bird"
NEG = "a blurry, out-of-focus photo of a bird"
BALANCED = {"five": 0.85, "four": 0.60, "three": 0.40, "two": 0.15}
N_KNOTS = 101


def stars(q, th=BALANCED):
    q = np.asarray(q)
    return np.select([q >= th["five"], q >= th["four"], q >= th["three"], q >= th["two"]], [5, 4, 3, 2], 1)


def fit(s, q, n=N_KNOTS):
    p = np.linspace(0, 100, n)
    ks, kq = np.percentile(s, p), np.percentile(q, p)
    ks = np.maximum.accumulate(ks + np.arange(n) * 1e-9)  # strictly increasing for interp
    return ks, kq


def apply(ks, kq, s):
    return np.interp(s, ks, kq)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", action="append", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--exp1b")
    a = ap.parse_args()
    m, _, _ = open_clip.create_model_and_transforms("ViT-B-16-SigLIP2", pretrained="webli")
    tok = open_clip.get_tokenizer("ViT-B-16-SigLIP2")
    m.eval()
    with torch.inference_mode():
        t = torch.nn.functional.normalize(m.encode_text(tok([POS, NEG])), dim=-1).numpy()
    d = t[0] - t[1]
    S, Q, SLUG = [], [], []
    for w in a.work:
        e = np.load(os.path.join(w, "cache", "img_siglip2-b16_f1.npz"))
        emb = e["emb"].astype(np.float32)
        emb /= np.linalg.norm(emb, axis=1, keepdims=True)
        sc = dict(zip(e["crop_id"], emb @ d))
        for r in csv.DictReader(open(os.path.join(w, "data", "crops_index.csv"))):
            if r["quality"] in ("", "None") or r["crop_id"] not in sc:
                continue
            S.append(sc[r["crop_id"]])
            Q.append(float(r["quality"]))
            SLUG.append(r["slug"])
    S, Q, SLUG = np.array(S), np.array(Q), np.array(SLUG)
    ks, kq = fit(S, Q)
    rep = {"n": int(len(S)), "spearman_raw_vs_cnn": float(spearmanr(S, Q).correlation), "per_perch": {}}
    sq_all = apply(ks, kq, S)
    for slug in sorted(set(SLUG)):
        i = SLUG == slug
        ks_l, kq_l = fit(S[~i], Q[~i])  # map fitted without this perch
        loo = apply(ks_l, kq_l, S[i])
        cs, ss, ls = stars(Q[i]), stars(sq_all[i]), stars(loo)
        rep["per_perch"][slug] = {
            "n": int(i.sum()),
            "stars_cnn": np.bincount(cs, minlength=6)[1:].tolist(),
            "stars_siglip": np.bincount(ss, minlength=6)[1:].tolist(),
            "stars_siglip_loo": np.bincount(ls, minlength=6)[1:].tolist(),
            "mean_abs_q_shift_loo_vs_pooled": float(np.mean(np.abs(loo - sq_all[i]))),
            "share_same_star_loo_vs_pooled": float(np.mean(ls == ss)),
            "share_moved_2plus_stars_vs_cnn": float(np.mean(np.abs(ss - cs) >= 2)),
            "spearman_vs_cnn": float(spearmanr(S[i], Q[i]).correlation),
        }
    rep["overall"] = {
        "stars_cnn": np.bincount(stars(Q), minlength=6)[1:].tolist(),
        "stars_siglip": np.bincount(stars(sq_all), minlength=6)[1:].tolist(),
        "share_same_star_vs_cnn": float(np.mean(stars(sq_all) == stars(Q))),
        "share_moved_2plus_stars_vs_cnn": float(np.mean(np.abs(stars(sq_all) - stars(Q)) >= 2)),
    }
    if a.exp1b:  # blend test on the judged scenes (key.json + judge_*.json from exp_quality.py)
        key = json.load(open(os.path.join(a.exp1b, "key.json")))
        judges = [json.load(open(os.path.join(a.exp1b, f))) for f in ("judge_sonnet.json", "judge_haiku.json")]
        out = {}
        for wname, wsig in (("cnn", 0.0), ("blend_25", 0.25), ("blend_50", 0.5), ("blend_75", 0.75), ("siglip", 1.0)):
            rhos = []
            for name, e in key.items():
                labs = list(e["tiles"])
                comb = [wsig * apply(ks, kq, e["tiles"][l]["siglip2"]) + (1 - wsig) * e["tiles"][l]["kestrel"] for l in labs]
                rhos.append(np.mean([spearmanr(comb, [-j[name].index(l) for l in labs]).correlation for j in judges]))
            out[wname] = float(np.nanmean(rhos))
        rep["exp1b_blend_mean_rho"] = out
    json.dump({"prompts": {"pos": POS, "neg": NEG}, "model": "ViT-B-16-SigLIP2 webli",
               "knots_raw": ks.round(6).tolist(), "knots_quality": kq.round(4).tolist(),
               "report": rep}, open(a.out, "w"), indent=1)
    print(json.dumps(rep, indent=1))


if __name__ == "__main__":
    sys.exit(main())
