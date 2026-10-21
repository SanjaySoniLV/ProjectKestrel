"""Final validation of the shipping recipe on held-out perches.

    BIOCLIP_WORK=<val work dir> python validate.py --stress <slug> [--sheet-dir DIR]

Recipe under test (what the app prototype does):
  BioCLIP 2 image tower, int8 ONNX as shipped (cache tag f1_app, from
  embed_images.py --onnx analyzer/models/bioclip/bioclip2_image_int8.onnx --tag _app),
  prompt "a photo of {common name}.", region geofence ("continent" tier),
  scene = mean of crop probabilities.

Tagged perches: the standard scene/crop metrics vs. the CNN.
Untagged --stress perch: no truth, so it reports plausibility instead:
  confidence distribution, share of no-geofence predictions that land on the
  local checklist, scene agreement with the CNN, top predicted species, and a
  contact sheet of random scenes (crop + BioCLIP / CNN top-1) to eyeball.
"""
import argparse
import collections
import csv
import json
import os
import random

import numpy as np
from PIL import Image, ImageDraw

import evalcore as E
import labelsets as L
import run_experiments as R
from common import CACHE, DATA, PERCHES, load_all_crops

VARIANT = (os.environ.get("BIOCLIP_VARIANT", "f1_app"),)


def tagged():
    lg, cl = R.logits_for("bioclip-2", variants=VARIANT)
    s, ccl = R.cnn_probs(1.0)
    out = []
    for tier in ("global", "continent"):
        out += R.eval_tier(lg, cl, tier, f"BioCLIP 2 int8 (app) / {tier}")
    out += R.eval_tier(s, ccl, "cnn500", "Kestrel CNN / cnn500", is_prob=True)
    R.table(out, "Validation: tagged perches")
    return out


def stress(slug, sheet_dir, n_sheet=24):
    rows = [r for r in load_all_crops() if r["slug"] == slug]
    d = np.load(os.path.join(CACHE, f"img_bioclip-2_{VARIANT[0]}.npz"))
    pos = {c: i for i, c in enumerate(d["crop_id"])}
    rows = [r for r in rows if r["crop_id"] in pos]
    img = d["emb"][[pos[r["crop_id"]] for r in rows]].astype(np.float32)
    img /= np.linalg.norm(img, axis=1, keepdims=True)
    txt, cl = E.load_txt("bioclip-2", "com")
    lg = 100 * img @ txt.T
    cat = E.catalog()
    name = lambda s: cat[s]["canonical_common_name"]

    p_cont, cl_cont = R.restrict_probs(lg, cl, L.for_perch(slug, "continent"))
    sp, sr = E.aggregate(p_cont, rows)
    p_glob = E.softmax(lg)
    gp, _ = E.aggregate(p_glob, rows)
    tx = L.checklist("texas")
    glob_top = [cl[i] for i in gp.argmax(1)]
    # CNN on the same crops
    c = np.load(os.path.join(CACHE, "cnn_f1.npz"))
    cpos = {x: i for i, x in enumerate(c["crop_id"])}
    cs = c["scores"][[cpos[r["crop_id"]] for r in rows]]
    cnn_cl = list(c["labels_sci"])
    cp, _ = E.aggregate(cs, rows)
    conf = sp.max(1)
    bc_top = [cl_cont[i] for i in sp.argmax(1)]
    cnn_top = [cnn_cl[i] for i in cp.argmax(1)]
    agree = np.mean([a == b for a, b in zip(bc_top, cnn_top)])
    agree_conf = np.mean([a == b for a, b, k in zip(bc_top, cnn_top, conf) if k >= 0.7])
    res = {
        "perch": PERCHES.get(slug, slug), "crops": len(rows), "scenes": len(sr),
        "bioclip_scene_conf_ge_0.7": float(np.mean(conf >= 0.7)),
        "bioclip_scene_conf_ge_0.9": float(np.mean(conf >= 0.9)),
        "no_geofence_top1_on_texas_checklist": float(np.mean([t in tx for t in glob_top])),
        "scene_agreement_bioclip_vs_cnn": float(agree),
        "scene_agreement_when_bioclip_conf_ge_0.7": float(agree_conf),
        "distinct_species_bioclip": len(set(bc_top)),
        "distinct_species_cnn": len(set(cnn_top)),
        "top_species_bioclip": collections.Counter(name(t) for t in bc_top).most_common(25),
    }
    print(json.dumps(res, indent=1))
    # contact sheet: random scenes, best-quality crop of each
    if sheet_dir:
        os.makedirs(sheet_dir, exist_ok=True)
        g = E.scene_groups(rows)
        keys = list(g)
        random.Random(1).shuffle(keys)
        tiles = []
        key_to_i = {k: i for i, k in enumerate(g)}
        for k in keys[:n_sheet]:
            ix = g[k]
            best = max(ix, key=lambda i: float(rows[i]["quality"] or 0))
            si = key_to_i[k]
            im = Image.open(rows[best]["abspath"]).convert("RGB").resize((300, 300))
            t = Image.new("RGB", (300, 345), "white")
            t.paste(im, (0, 0))
            dr = ImageDraw.Draw(t)
            dr.text((4, 303), f"#{si} BC: {name(bc_top[si])} ({conf[si]:.2f})", fill="black")
            dr.text((4, 322), f"CNN: {name(cnn_top[si]) if cnn_top[si] else '?'} ({cp[si].max():.2f})", fill="gray")
            tiles.append(t)
        for j in range(0, len(tiles), 6):
            row = Image.new("RGB", (300 * 6, 345), "white")
            for i, t in enumerate(tiles[j:j + 6]):
                row.paste(t, (300 * i, 0))
            row.save(os.path.join(sheet_dir, f"stress_{j // 6}.jpg"), quality=85)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stress")
    ap.add_argument("--sheet-dir")
    a = ap.parse_args()
    out = {"tagged": tagged()}
    if a.stress:
        out["stress"] = stress(a.stress, a.sheet_dir)
    os.makedirs(R.RES, exist_ok=True)
    json.dump(out, open(os.path.join(R.RES, "validation.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
