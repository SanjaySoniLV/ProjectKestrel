"""Scoring core shared by the experiment scripts.

Conventions
- Ground truth is the scene's single IOC species (common.load_eval_crops).
- "crop" accuracy: each Kestrel bird crop scored independently.
- "scene" accuracy: one prediction per scene from aggregated crop scores.
- A label set is a list of IOC scientific names; predictions are restricted
  to it by masking text-embedding columns (BioCLIP) or CNN outputs.
"""
import collections
import json
import os

import numpy as np

from common import CACHE, load_catalog, load_eval_crops

_CAT = None


def catalog():
    global _CAT
    if _CAT is None:
        _CAT = {r["scientific_name"]: r for r in load_catalog()}
    return _CAT


def load_rows():
    rows = load_eval_crops()
    return rows, {r["crop_id"]: i for i, r in enumerate(rows)}


def load_img(model, variants=("f1",), rows=None):
    """Mean of several cached embedding variants (test-time augmentation)."""
    embs = []
    for v in variants:
        d = np.load(os.path.join(CACHE, f"img_{model}_{v}.npz"))
        e, ids = d["emb"].astype(np.float32), list(d["crop_id"])
        if rows is not None:
            order = {c: i for i, c in enumerate(ids)}
            e = e[[order[r["crop_id"]] for r in rows]]
        embs.append(e)
    e = np.mean(embs, axis=0)
    return e / np.linalg.norm(e, axis=1, keepdims=True)


def load_txt(model, text_type, templates="single", subset=""):
    d = np.load(os.path.join(CACHE, f"txt_{model}_{text_type}_{templates}{subset}.npz"))
    return d["emb"].astype(np.float32), list(d["sci"])


def logit_scale(model):
    """Saved by embed_text.py in each text cache; logit_scale.json covers early caches."""
    for f in sorted(os.listdir(CACHE)):
        if f.startswith(f"txt_{model}_") and f.endswith(".npz"):
            d = np.load(os.path.join(CACHE, f))
            if "logit_scale" in d:
                return float(d["logit_scale"])
    return json.load(open(os.path.join(CACHE, "logit_scale.json")))[model]


def softmax(x, axis=1):
    x = x - x.max(axis=axis, keepdims=True)
    e = np.exp(x)
    return e / e.sum(axis=axis, keepdims=True)


def restrict(scores, classes, allowed):
    """Column-subset scores [N,C] to `allowed` (iterable of sci names)."""
    allowed = set(allowed)
    keep = [i for i, c in enumerate(classes) if c in allowed]
    return scores[:, keep], [classes[i] for i in keep]


def truth_idx(rows, classes):
    pos = {c: i for i, c in enumerate(classes)}
    return np.array([pos.get(r["truth"][0], -1) for r in rows])


def topk_acc(probs, t, k=1):
    top = np.argsort(-probs, axis=1)[:, :k]
    return float(np.mean([(ti in tk) for ti, tk in zip(t, top)])) if len(t) else float("nan")


def family_acc(probs, classes, rows):
    """Family of argmax species == true family."""
    cat = catalog()
    pred = [cat[classes[i]]["family_sci"] for i in probs.argmax(1)]
    return float(np.mean([p == r["truth_family"][0] for p, r in zip(pred, rows)]))


def family_sum_acc(probs, classes, rows):
    """Argmax over family-summed probability (Kestrel's family_matrix trick)."""
    cat = catalog()
    fams = sorted({cat[c]["family_sci"] for c in classes})
    fi = {f: i for i, f in enumerate(fams)}
    M = np.zeros((len(classes), len(fams)), np.float32)
    for j, c in enumerate(classes):
        M[j, fi[cat[c]["family_sci"]]] = 1
    fp = probs @ M
    pred = [fams[i] for i in fp.argmax(1)]
    return float(np.mean([p == r["truth_family"][0] for p, r in zip(pred, rows)]))


def scene_groups(rows, idx=None):
    g = collections.defaultdict(list)
    for i, r in enumerate(rows):
        if idx is None or i in idx:
            g[(r["slug"], r["scene_id"])].append(i)
    return g


def aggregate(probs, rows, method="mean_prob", weights=None):
    """Return (scene_probs [S,C], scene_rows) aggregated per scene."""
    g = scene_groups(rows)
    out, srows = [], []
    for key, ix in g.items():
        p = probs[ix]
        if method == "mean_prob":
            s = p.mean(0)
        elif method == "mean_logprob":  # product of experts / geometric mean
            s = np.log(p + 1e-12).mean(0)
            s = np.exp(s - s.max())
            s /= s.sum()
        elif method == "vote":
            s = np.bincount(p.argmax(1), minlength=p.shape[1]).astype(np.float32)
            s = s + 1e-3 * p.mean(0)  # tie-break on mean prob
        elif method == "weighted":
            w = np.asarray([weights[i] for i in ix], np.float32)
            s = (p * w[:, None]).sum(0) / max(w.sum(), 1e-9)
        elif method == "max_conf":  # trust the single most confident crop
            s = p[p.max(1).argmax()]
        else:
            raise ValueError(method)
        out.append(s)
        srows.append(rows[ix[0]])
    return np.array(out), srows


def evaluate(probs, classes, rows, label=""):
    """Crop + scene metrics overall and per perch. Returns a flat dict list."""
    res = []
    slugs = sorted({r["slug"] for r in rows})
    sprobs, srows = aggregate(probs, rows)
    for slug in slugs + ["ALL"]:
        ci = [i for i, r in enumerate(rows) if slug in ("ALL", r["slug"])]
        si = [i for i, r in enumerate(srows) if slug in ("ALL", r["slug"])]
        t = truth_idx([rows[i] for i in ci], classes)
        st = truth_idx([srows[i] for i in si], classes)
        res.append({
            "config": label, "perch": slug, "n_crops": len(ci), "n_scenes": len(si),
            "n_classes": len(classes),
            "truth_in_set": float(np.mean(t >= 0)),
            "crop_top1": topk_acc(probs[ci], t, 1),
            "crop_top5": topk_acc(probs[ci], t, 5),
            "crop_family": family_acc(probs[ci], classes, [rows[i] for i in ci]),
            "scene_top1": topk_acc(sprobs[si], st, 1),
            "scene_top5": topk_acc(sprobs[si], st, 5),
            "scene_family": family_acc(sprobs[si], classes, [srows[i] for i in si]),
        })
    return res


def bioclip_probs(img, txt, scale):
    return softmax(scale * img @ txt.T)
