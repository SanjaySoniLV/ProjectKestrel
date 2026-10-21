"""Runs every scored experiment off the cached embeddings and writes
results/<name>.json + a markdown table per experiment to stdout.

    python run_experiments.py [exp ...]      (default: all available)
"""
import json
import os
import sys

import numpy as np

import evalcore as E
import labelsets as L
from common import AMERICAN_SLUGS, CACHE, PERCHES, WORK

RES = os.environ.get("BIOCLIP_RESULTS", os.path.join(WORK, "results"))  # private: never commit
SLUGS = list(PERCHES)
ROWS, _ = E.load_rows()


# ---------------------------------------------------------------- scorers
def bioclip_logits(model, text_type="taxon_com", templates="single", variants=("f1",)):
    img = E.load_img(model, variants, ROWS)
    txt, classes = E.load_txt(model, text_type, templates)
    return E.logit_scale(model) * img @ txt.T, classes


def cnn_probs(frac=0.5):
    d = np.load(os.path.join(CACHE, f"cnn_f{frac:g}.npz"))
    order = {c: i for i, c in enumerate(d["crop_id"])}
    s = d["scores"][[order[r["crop_id"]] for r in ROWS]]
    return s, list(d["labels_sci"])


def restrict_probs(logits, classes, allowed, is_prob=False):
    s, cl = E.restrict(logits, classes, allowed)
    if is_prob:
        p = np.clip(s, 1e-12, None)
        return p / p.sum(1, keepdims=True), cl
    return E.softmax(s), cl


def eval_tier(logits, classes, tier, label, is_prob=False, agg="mean_prob", rows=ROWS):
    """Per-perch restriction to `tier`, then pooled ALL row (crop/scene weighted)."""
    per = []
    for slug in SLUGS:
        ix = [i for i, r in enumerate(rows) if r["slug"] == slug]
        if not ix:
            continue
        allowed = L.for_perch(slug, tier, rows)
        if allowed is None:
            return []
        p, cl = restrict_probs(logits[ix], classes, allowed, is_prob)
        sub = [rows[i] for i in ix]
        r = [x for x in evaluate(p, cl, sub, label, agg) if x["perch"] == "ALL"][0]
        r["perch"] = slug
        per.append(r)
    keys = [k for k in per[0] if k.startswith(("crop_", "scene_", "truth"))]
    tot = {"config": label, "perch": "ALL", "n_crops": sum(r["n_crops"] for r in per),
           "n_scenes": sum(r["n_scenes"] for r in per), "n_classes": "per-perch"}
    for k in keys:
        w = "n_scenes" if k.startswith("scene") else "n_crops"
        tot[k] = sum(r[k] * r[w] for r in per) / tot[w]
    return per + [tot]


def evaluate(p, cl, rows, label, agg):
    if agg == "mean_prob":
        return E.evaluate(p, cl, rows, label)
    # same metrics, different scene aggregation
    out = E.evaluate(p, cl, rows, label)
    sp, sr = E.aggregate(p, rows, agg, weights=[float(r["quality"] or 0) for r in rows])
    t = E.truth_idx(sr, cl)
    for o in out:
        o["scene_top1"] = E.topk_acc(sp, t, 1)
        o["scene_top5"] = E.topk_acc(sp, t, 5)
        o["scene_family"] = E.family_acc(sp, cl, sr)
    return out


# ---------------------------------------------------------------- reporting
COLS = ["crop_top1", "crop_top5", "crop_family", "scene_top1", "scene_top5", "scene_family"]


def table(results, title, perch_split=True):
    print(f"\n### {title}\n")
    print("| config | perch | crops | scenes | classes | truth in set | " + " | ".join(COLS) + " |")
    print("|" + "---|" * (6 + len(COLS)))
    for r in results:
        if not perch_split and r["perch"] != "ALL":
            continue
        name = PERCHES.get(r["perch"], r["perch"]).split(" (")[0]
        print(f"| {r['config']} | {name} | {r['n_crops']} | {r['n_scenes']} | {r['n_classes']} | "
              f"{r['truth_in_set']:.0%} | " + " | ".join(f"{100*r[c]:.1f}" for c in COLS) + " |")


def save(name, results):
    os.makedirs(RES, exist_ok=True)
    json.dump(results, open(os.path.join(RES, f"{name}.json"), "w"), indent=1)


EXPERIMENTS = {}


def exp(fn):
    EXPERIMENTS[fn.__name__] = fn
    return fn


def have(*files):
    return all(os.path.exists(os.path.join(CACHE, f)) for f in files)


# ================================================================ experiments
TIERS = ["global", "continent", "continent+", "checklist", "cnn500", "oracle"]


@exp
def cnn_baseline():
    out = []
    for frac in (1.0, 0.5):
        if not have(f"cnn_f{frac:g}.npz"):
            continue
        s, cl = cnn_probs(frac)
        for tier in ("cnn500", "checklist", "oracle"):
            out += eval_tier(s, cl, tier, f"CNN crop f{frac:g} / {tier}", is_prob=True)
    return out


@exp
def prompt_text_types():
    """Which caption format (BioCLIP training text types) works best?"""
    out = []
    for model in ("bioclip-1", "bioclip-2", "bioclip-2.5"):
        if not have(f"img_{model}_f1.npz"):
            continue
        for tt in ("com", "sci", "taxon", "sci_com", "taxon_com"):
            if not have(f"txt_{model}_{tt}_single.npz"):
                continue
            lg, cl = bioclip_logits(model, tt)
            for tier in ("global", "checklist"):
                out += eval_tier(lg, cl, tier, f"{model} {tt} / {tier}")
    return out


@exp
def prompt_templates():
    """Template ensembles, on the 1.9k-species union of checklists + CNN-500
    (80 templates x 11k species is too slow on CPU). Scored on the checklist tier."""
    out = []
    model = "bioclip-2"
    img = E.load_img(model, ("f1",), ROWS)
    for tt, tmpls in (("taxon_com", ("single", "bird7")), ("com", ("single", "bird7", "openai80"))):
        for t in tmpls:
            if not have(f"txt_{model}_{tt}_{t}_union.npz"):
                continue
            txt, cl = E.load_txt(model, tt, t, "_union")
            out += eval_tier(E.logit_scale(model) * img @ txt.T, cl, "checklist", f"{model} {tt} x {t}")
    return out


def best_text(model):
    """Mean of taxon_com and com class embeddings (prompt-type ensemble)."""
    a, cl = E.load_txt(model, "taxon_com")
    b, cl2 = E.load_txt(model, "com")
    assert cl == cl2
    t = a + b
    return t / np.linalg.norm(t, axis=1, keepdims=True), cl


def logits_for(model, text="ens", variants=("f1",)):
    img = E.load_img(model, variants, ROWS)
    if text == "ens":
        txt, cl = best_text(model)
    else:
        txt, cl = E.load_txt(model, text)
    return E.logit_scale(model) * img @ txt.T, cl


@exp
def prompt_ensemble():
    out = []
    for model in ("bioclip-1", "bioclip-2", "bioclip-2.5"):
        if not (have(f"img_{model}_f1.npz", f"txt_{model}_com_single.npz", f"txt_{model}_taxon_com_single.npz")):
            continue
        lg, cl = logits_for(model, "ens")
        for tier in ("global", "checklist"):
            out += eval_tier(lg, cl, tier, f"{model} taxon_com+com / {tier}")
    return out


@exp
def geofence_tiers():
    out = []
    for model in ("bioclip-1", "bioclip-2", "bioclip-2.5"):
        if not have(f"img_{model}_f1.npz", f"txt_{model}_com_single.npz"):
            continue
        lg, cl = logits_for(model)
        for tier in TIERS:
            out += eval_tier(lg, cl, tier, f"{model} / {tier}")
    return out


@exp
def crop_framing():
    out = []
    for model in ("bioclip-2", "bioclip-2.5"):
        for v in (("f1",), ("f0.6",), ("f1", "f0.6")):
            if not have(*[f"img_{model}_{x}.npz" for x in v]):
                continue
            lg, cl = logits_for(model, variants=v)
            for tier in ("global", "checklist"):
                out += eval_tier(lg, cl, tier, f"{model} {'+'.join(v)} / {tier}")
    return out


@exp
def scene_aggregation():
    out = []
    model = "bioclip-2.5" if have("img_bioclip-2.5_f1.npz", "txt_bioclip-2.5_com_single.npz") else "bioclip-2"
    lg, cl = logits_for(model)
    for agg in ("mean_prob", "mean_logprob", "vote", "weighted", "max_conf"):
        out += eval_tier(lg, cl, "checklist", f"{model} checklist / agg={agg}", agg=agg)
    s, ccl = cnn_probs(1.0)
    for agg in ("mean_prob", "mean_logprob", "vote", "weighted", "max_conf"):
        out += eval_tier(s, ccl, "cnn500", f"CNN cnn500 / agg={agg}", is_prob=True, agg=agg)
    return out


def best_model():
    return "bioclip-2.5" if have("img_bioclip-2.5_f1.npz", "txt_bioclip-2.5_com_single.npz") else "bioclip-2"


@exp
def temperature():
    """Softmax temperature before scene averaging (logit_scale=100 is very peaky)."""
    out = []
    model = best_model()
    lg, cl = logits_for(model)
    for T in (0.25, 0.5, 1, 2, 4):
        out += eval_tier(lg / T, cl, "checklist", f"{model} checklist / T={T}")
    return out


def prototypes(img, rows, cl, holdout):
    """Mean image embedding per true class, excluding rows where holdout(i) is True."""
    pos = {c: j for j, c in enumerate(cl)}
    P = np.zeros((len(cl), img.shape[1]), np.float32)
    n = np.zeros(len(cl))
    for i, r in enumerate(rows):
        j = pos.get(r["truth"][0])
        if j is not None and not holdout(i):
            P[j] += img[i]
            n[j] += 1
    has = n > 0
    P[has] /= np.linalg.norm(P[has], axis=1, keepdims=True)
    return P, has


@exp
def few_shot():
    """Zero-shot text logits + alpha * image-prototype affinities from *other* labelled data.

    loso: prototypes from every other scene (any perch) -> "your own verified
          history helps tag new scenes" (optimistic: same trips, same birds).
    lopo: prototypes only from other perches -> transfer across trips.

    Image-image cosines run much higher than image-text cosines (the CLIP
    modality gap), so a class with no prototype would be unfairly penalised.
    Such classes fall back to a*cos(img, text) + b, with (a, b) a least-squares
    fit of cos(x, P_j) on cos(x, T_j) over labelled crops x and prototype
    classes j (a first attempt with a single correct-class offset overshot).
    """
    out = []
    model = best_model()
    img = E.load_img(model, ("f1",), ROWS)
    txt, cl = best_text(model)
    scale = E.logit_scale(model)
    zs_cos = img @ txt.T
    pos = {c: j for j, c in enumerate(cl)}
    y = np.array([pos.get(r["truth"][0], -1) for r in ROWS])
    scenes = [(r["slug"], r["scene_id"]) for r in ROWS]
    for mode in ("loso", "lopo"):
        groups = sorted(set(scenes)) if mode == "loso" else sorted({(s, None) for s, _ in scenes})
        aff = np.empty_like(zs_cos)
        for key in groups:
            if mode == "loso":
                hold = np.array([k == key for k in scenes])
            else:
                hold = np.array([k[0] == key[0] for k in scenes])
            P, has = prototypes(img, ROWS, cl, lambda i: hold[i])
            tr = np.where(~hold & (y >= 0))[0]
            rng = np.random.default_rng(0)
            smp = rng.choice(tr, size=min(400, len(tr)), replace=False)
            hj = np.where(has)[0]
            X = zs_cos[np.ix_(smp, hj)].ravel()
            Y = (img[smp] @ P[hj].T).ravel()
            ca, cb = np.polyfit(X, Y, 1)
            ix = np.where(hold)[0]
            a_proto = img[ix] @ P.T
            aff[ix] = np.where(has[None, :], a_proto, ca * zs_cos[ix] + cb)
        for alpha in (0.5, 1.0, 2.0):
            lg = scale * (zs_cos + alpha * aff)
            out += eval_tier(lg, cl, "checklist", f"{model} checklist + protos {mode} a={alpha}")
    return out


@exp
def cnn_ensemble():
    """US perches only: mix BioCLIP (checklist tier) with the CNN in the CNN's class space."""
    out = []
    model = best_model()
    lg, cl = logits_for(model)
    s, ccl = cnn_probs(1.0)
    us = [i for i, r in enumerate(ROWS) if r["slug"] in AMERICAN_SLUGS]
    rows = [ROWS[i] for i in us]
    allowed = L.cnn500() & set().union(*(L.for_perch(s, "checklist") for s in AMERICAN_SLUGS))
    pb, bcl = restrict_probs(lg[us], cl, allowed)
    pc, ccl2 = restrict_probs(s[us], ccl, allowed, is_prob=True)
    # align columns
    pos = {c: j for j, c in enumerate(ccl2)}
    pc = pc[:, [pos[c] for c in bcl]]
    for w in (0.0, 0.25, 0.5, 0.75, 1.0):
        geo = np.exp(w * np.log(pb + 1e-9) + (1 - w) * np.log(pc + 1e-9))
        geo /= geo.sum(1, keepdims=True)
        for r in E.evaluate(geo, bcl, rows, f"w_bioclip={w} (geo-mean, CNN∩Texas classes)"):
            out.append(r)
    return out


@exp
def quality_filter():
    """Drop low-quality crops before scene aggregation (scenes keep >=1 crop)."""
    out = []
    model = best_model()
    lg, cl = logits_for(model)
    for q in (0.0, 0.2, 0.4):
        keep = []
        for key, ix in E.scene_groups(ROWS).items():
            good = [i for i in ix if float(ROWS[i]["quality"] or 0) >= q]
            keep += good or [max(ix, key=lambda i: float(ROWS[i]["quality"] or 0))]
        keep.sort()
        rows = [ROWS[i] for i in keep]
        out += eval_tier(lg[keep], cl, "checklist", f"{model} checklist / crops q>={q}", rows=rows)
    return out


def _tier_probs(lg, cl, tier, rows=ROWS):
    """Per-perch restricted probabilities, stitched back into one list per row."""
    out = [None] * len(rows)
    for slug in SLUGS:
        ix = [i for i, r in enumerate(rows) if r["slug"] == slug]
        if not ix:
            continue
        p, pcl = restrict_probs(lg[ix], cl, L.for_perch(slug, tier, rows))
        t = E.truth_idx([rows[i] for i in ix], pcl)
        for k, i in enumerate(ix):
            out[i] = (p[k], pcl, t[k])
    return out


@exp
def by_quality():
    """Q1 'crystal-clear bird' check: crop accuracy by Kestrel quality-score bin."""
    out = []
    bins = [(0, 0.1), (0.1, 0.25), (0.25, 0.5), (0.5, 1.01)]
    model = best_model()
    for name, (lg, cl, tier, isp) in {
        f"{model} / global": (*logits_for(model), "global", False),
        f"{model} / checklist": (*logits_for(model), "checklist", False),
        "CNN / cnn500": (*cnn_probs(1.0), "cnn500", True),
    }.items():
        for lo, hi in bins:
            keep = [i for i, r in enumerate(ROWS) if lo <= float(r["quality"] or 0) < hi]
            rows = [ROWS[i] for i in keep]
            out += [r for r in eval_tier(lg[keep], cl, tier, f"{name} / quality [{lo},{hi})", is_prob=isp, rows=rows)
                    if r["perch"] == "ALL"]
    return out


@exp
def coverage():
    """Scene-level accuracy if Kestrel only auto-tags scenes above a confidence threshold."""
    out = []
    model = best_model()
    configs = {f"{model} / checklist": (*logits_for(model), "checklist", False),
               "CNN / cnn500 (US perches only)": (*cnn_probs(1.0), "cnn500", True)}
    for name, (lg, cl, tier, isp) in configs.items():
        conf, correct = [], []
        for slug in SLUGS:
            if name.startswith("CNN") and slug not in AMERICAN_SLUGS:
                continue
            ix = [i for i, r in enumerate(ROWS) if r["slug"] == slug]
            p, pcl = restrict_probs(lg[ix], cl, L.for_perch(slug, tier, ROWS), isp)
            sp, sr = E.aggregate(p, [ROWS[i] for i in ix])
            t = E.truth_idx(sr, pcl)
            conf += list(sp.max(1))
            correct += list(sp.argmax(1) == t)
        conf, correct = np.array(conf), np.array(correct)
        order = np.argsort(-conf)
        for cov in (0.25, 0.5, 0.75, 0.9, 1.0):
            k = max(1, int(round(cov * len(order))))
            out.append({"config": name, "coverage": cov, "threshold": float(conf[order][k - 1]),
                        "scene_top1": float(correct[order][:k].mean()), "n_scenes": k})
        for th in (0.5, 0.7, 0.9):
            m = conf >= th
            out.append({"config": name, "threshold": th, "coverage": float(m.mean()),
                        "scene_top1": float(correct[m].mean()) if m.any() else float("nan"), "n_scenes": int(m.sum())})
    return out


def main():
    names = sys.argv[1:] or list(EXPERIMENTS)
    for n in names:
        res = EXPERIMENTS[n]()
        if res:
            save(n, res)
            if "crop_top1" in res[0]:
                table(res, n)
            else:
                print(f"\n### {n}\n")
                for r in res:
                    print(r)


if __name__ == "__main__":
    main()
