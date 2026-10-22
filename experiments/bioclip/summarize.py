"""Headline tables for the write-up, computed from the cached embeddings.

    python summarize.py > summary.md

Prints markdown: per-perch head-to-head, cost/accuracy, int8/bf16 fidelity,
and a "worth a second look" list of scenes where the best model confidently
disagrees with the scene tag.
"""
import json
import os

import numpy as np

import evalcore as E
import labelsets as L
import run_experiments as R
from common import AMERICAN_SLUGS, CACHE, PERCHES

SHORT = {s: n.split(" (")[0] for s, n in PERCHES.items()}


def row(res, perch, k):
    return next(r[k] for r in res if r["perch"] == perch)


def head_to_head():
    cfgs = [("Kestrel CNN (its 500 species)", *R.cnn_probs(1.0), "cnn500", True)]
    for m in ("bioclip-1", "bioclip-2", "bioclip-2.5"):
        if R.have(f"img_{m}_f1.npz"):
            lg, cl = R.logits_for(m)
            cfgs.append((f"{m}, no geofence (11,251 species)", lg, cl, "global", False))
            cfgs.append((f"{m} + checklist", lg, cl, "checklist", False))
    print("\n### Scene top-1 / crop top-1 by perch\n")
    print("| Model | " + " | ".join(SHORT[s] for s in R.SLUGS) + " | All |")
    print("|---|" + "---|" * (len(R.SLUGS) + 1))
    for name, lg, cl, tier, isp in cfgs:
        res = R.eval_tier(lg, cl, tier, name, is_prob=isp)
        cells = [f"{100*row(res, p, 'scene_top1'):.0f} / {100*row(res, p, 'crop_top1'):.0f}"
                 for p in R.SLUGS + ["ALL"]]
        print(f"| {name} | " + " | ".join(cells) + " |")


def fidelity():
    print("\n### Quantisation / precision fidelity (BioCLIP 2 image tower)\n")
    ref = E.load_img("bioclip-2", ("f1",), R.ROWS)
    for tag, label in (("f1_int8", "ONNX int8 (dynamic)"),):
        if not R.have(f"img_bioclip-2_{tag}.npz"):
            continue
        e = E.load_img("bioclip-2", (tag,), R.ROWS)
        cos = np.sum(ref * e, 1)
        txt, cl = E.load_txt("bioclip-2", R.DEFAULT_TEXT)
        a = R.eval_tier(100 * ref @ txt.T, cl, "checklist", "fp32")
        b = R.eval_tier(100 * e @ txt.T, cl, "checklist", label)
        print(f"- {label}: median cos vs fp32 {np.median(cos):.4f} (min {cos.min():.3f}); "
              f"checklist scene top-1 {100*row(a,'ALL','scene_top1'):.1f} -> {100*row(b,'ALL','scene_top1'):.1f}, "
              f"crop top-1 {100*row(a,'ALL','crop_top1'):.1f} -> {100*row(b,'ALL','crop_top1'):.1f}")
    p = os.path.join(CACHE, "img_bioclip-2.5_f1_fp32check.npz")
    if os.path.exists(p):
        d = np.load(p)
        bf = np.load(os.path.join(CACHE, "img_bioclip-2.5_f1.npz"))
        pos = {c: i for i, c in enumerate(bf["crop_id"])}
        b = bf["emb"][[pos[c] for c in d["crop_id"]]].astype(np.float32)
        cos = np.sum(d["emb"].astype(np.float32) * b, 1)
        print(f"- BioCLIP 2.5 bf16 (batch 1) vs fp32 on {len(cos)} crops: median cos {np.median(cos):.4f}, min {cos.min():.4f}")


def audit(model="bioclip-2", thresh=0.8):
    cat = E.catalog()
    lg, cl = R.logits_for(model)
    print(f"\n### Worth a second look: scenes where {model} + checklist confidently (>= {thresh}) disagrees with the tag\n")
    print("| Perch | Scene # | First export | Tagged | BioCLIP says | Confidence |")
    print("|---|---|---|---|---|---|")
    for slug in R.SLUGS:
        ix = [i for i, r in enumerate(R.ROWS) if r["slug"] == slug]
        p, pcl = R.restrict_probs(lg[ix], cl, L.for_perch(slug, "checklist", R.ROWS))
        sp, sr = E.aggregate(p, [R.ROWS[i] for i in ix])
        for prob, r in zip(sp, sr):
            pred = pcl[prob.argmax()]
            if pred != r["truth"][0] and prob.max() >= thresh:
                print(f"| {SHORT[slug]} | {r['scene_sort']} | {r['export_filename']} | "
                      f"{cat[r['truth'][0]]['canonical_common_name']} | {cat[pred]['canonical_common_name']} | {prob.max():.2f} |")


if __name__ == "__main__":
    head_to_head()
    fidelity()
    audit()
