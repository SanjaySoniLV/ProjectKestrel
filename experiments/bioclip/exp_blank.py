"""Can BioCLIP 2 / SigLIP 2 replace SpeciesNet's false-positive gate?

In the current pipeline SpeciesNet does three jobs after MegaDetector proposes a
box: drop "no cv result" (blank) boxes, drop boxes whose classifier score is
below the user threshold, and route bird vs other wildlife. Dropping SpeciesNet
means something else must reject MegaDetector false positives (branches, rocks,
glare). We have no labelled false positives, so this builds them:

    python exp_blank.py detect --work W --slug S [--slug S ...] --n 300 --out OUT
        Runs the app's MegaDetector (mdv5a, analyzer/ on the main checkout) on a
        sample of each perch's exports. Writes square "box crops" (2x the box,
        the same recipe for every kind) to OUT/crops/ and OUT/index.csv:
          pos    a box Kestrel kept (it has a crop in the perch): animal
          hard   an MD animal proposal (conf >= 0.05) that overlaps no kept box:
                 the realistic false-positive population. Some are real animals
                 Kestrel dropped (tiny/distant birds), so a judge labels them.
          rand   a random square of typical crop size that overlaps no kept box
    python exp_blank.py sheets --out OUT        numbered sheets of the hard set for a blind judge
    python exp_blank.py embed --out OUT         BioCLIP 2 / SigLIP 2 / CLIP B/16 embeddings of every crop
    python exp_blank.py speciesnet --out OUT    the current gate, for a baseline
    python exp_blank.py score --out OUT --judge J.json
        Zero-shot (animal vs background prompts) and linear probes, trained
        leave-one-perch-out on pos vs rand only (synthetic negatives, no judged
        labels), evaluated on judged hard negatives. Reports the share of
        negatives rejected at 99% / 99.5% animal recall, and AUC.
"""
import argparse
import csv
import glob
import json
import os
import random
import sys

import numpy as np
from PIL import Image, ImageDraw

APP = "/home/user/projectkestrel/analyzer"  # checkout with real LFS weights
BACKGROUND = [
    "grass", "a lawn", "bare branches", "a tree branch", "leaves", "foliage", "tree bark", "a tree trunk",
    "reeds", "tall grass", "water", "rippling water", "a lake surface", "mud", "a mudflat", "sand", "rocks",
    "a rock", "a pebble", "gravel", "soil", "the sky", "clouds", "a cloudy sky", "a wooden post", "a fence",
    "a power line", "a sign", "a building", "a road", "snow", "ice", "a log", "dead leaves", "flowers",
    "a blurry background", "an empty scene", "sun glare", "a shadow", "a bush", "moss",
]
ANIMAL = ["a bird", "an animal", "a mammal", "a reptile", "a turtle", "a frog", "an insect", "a butterfly",
          "a dragonfly", "a fish", "a snake", "a lizard", "a squirrel", "a deer", "a rabbit", "a duck",
          "a wading bird", "a small bird", "a bird of prey", "a flock of birds"]
CROP_PX = 320


def square(box, w, h, scale=2.0):
    x0, y0, x1, y1 = box
    cx, cy, s = (x0 + x1) / 2, (y0 + y1) / 2, max(x1 - x0, y1 - y0) * scale
    s = max(s, 32)
    return (cx - s / 2, cy - s / 2, cx + s / 2, cy + s / 2)


def cut(img, sq):
    return img.crop(tuple(int(round(v)) for v in sq)).resize((CROP_PX, CROP_PX), Image.BICUBIC)


def overlap(a, b):
    """intersection / area of a."""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy / max((a[2] - a[0]) * (a[3] - a[1]), 1e-9)


def detect(a):
    sys.path.insert(0, APP)
    from kestrel_analyzer.ml.provider_coordinator import ProviderCoordinator
    from kestrel_analyzer.ml.speciesnet_sam_hq import OnnxMDv5Detector
    det = OnnxMDv5Detector(os.path.join(APP, "models", "speciesnet", "mdv5a.onnx"), ProviderCoordinator(user_gpu_enabled=False))
    rng = random.Random(0)
    os.makedirs(os.path.join(a.out, "crops"), exist_ok=True)
    idx_path = os.path.join(a.out, "index.csv")
    done = set()
    if os.path.exists(idx_path):
        done = {(r["slug"], r["export"]) for r in csv.DictReader(open(idx_path))}
    f = open(idx_path, "a", newline="")
    wr = csv.DictWriter(f, ["id", "slug", "export", "kind", "conf", "box", "path"])
    if not done:
        wr.writeheader()
    for work, slug in zip(a.work, a.slug):
        boxes = {}
        for r in csv.DictReader(open(os.path.join(work, "data", "crops_index.csv"))):
            if r["slug"] == slug and r["bbox"] not in ("", "null"):
                b = json.loads(r["bbox"])
                boxes.setdefault(r["export_id"], []).append((b["xMinNorm"], b["yMinNorm"], b["xMaxNorm"], b["yMaxNorm"]))
        exps = sorted(e for e in boxes if os.path.exists(os.path.join(work, "data", slug, "exports", e + ".jpg")))
        rng.shuffle(exps)
        sizes = [max(b[2] - b[0], b[3] - b[1]) for e in exps for b in boxes[e]]
        for n, e in enumerate(exps[: a.n]):
            if (slug, e) in done:
                continue
            p = os.path.join(work, "data", slug, "exports", e + ".jpg")
            img = Image.open(p).convert("RGB")
            w, h = img.size
            kept = [(b[0] * w, b[1] * h, b[2] * w, b[3] * h) for b in boxes[e]]
            rows = [("pos", 1.0, b) for b in kept]
            out = det.predict(p, det.preprocess(img))
            for d in out.get("detections", []):
                if d.get("label") != "animal" or d["conf"] < 0.05:
                    continue
                x, y, bw, bh = d["bbox"]
                b = (x * w, y * h, (x + bw) * w, (y + bh) * h)
                if max((overlap(b, k) for k in kept), default=0) < 0.1 and max((overlap(k, b) for k in kept), default=0) < 0.1:
                    rows.append(("hard", d["conf"], b))
            for _ in range(2):
                for _try in range(30):
                    s = rng.choice(sizes) * w
                    x0, y0 = rng.uniform(0, w - s), rng.uniform(0, h - s)
                    b = (x0, y0, x0 + s, y0 + s)
                    if all(overlap(square(b, w, h), k) == 0 and overlap(k, square(b, w, h)) == 0 for k in kept):
                        rows.append(("rand", 0.0, b))
                        break
            for i, (kind, conf, b) in enumerate(rows):
                cid = f"{slug}_{e}_{kind}{i}"
                cp = os.path.join(a.out, "crops", cid + ".jpg")
                cut(img, square(b, w, h)).save(cp, quality=90)
                wr.writerow({"id": cid, "slug": slug, "export": e, "kind": kind, "conf": round(conf, 4),
                             "box": json.dumps([round(v / (w if j % 2 == 0 else h), 4) for j, v in enumerate(b)]),
                             "path": cp})
            f.flush()
            if n % 25 == 0:
                print(slug, n, flush=True)
    f.close()


def rows(out):
    return list(csv.DictReader(open(os.path.join(out, "index.csv"))))


def sheets(a):
    hard = [r for r in rows(a.out) if r["kind"] == "hard"]
    d = os.path.join(a.out, "blind")
    os.makedirs(d, exist_ok=True)
    key = {}
    T = 200
    for si in range(0, len(hard), 30):
        name = f"sheet_{si // 30:03d}"
        chunk = hard[si: si + 30]
        out = Image.new("RGB", (T * 6, T * ((len(chunk) + 5) // 6)), "white")
        for i, r in enumerate(chunk):
            im = Image.open(r["path"]).resize((T, T))
            dr = ImageDraw.Draw(im)
            dr.rectangle((0, 0, 26, 16), fill="black")
            dr.text((3, 2), f"{i + 1:02d}", fill="white")
            out.paste(im, ((i % 6) * T, (i // 6) * T))
            key[f"{name}:{i + 1:02d}"] = r["id"]
        out.save(os.path.join(d, name + ".jpg"), quality=85)
    json.dump(key, open(os.path.join(a.out, "sheet_key.json"), "w"))
    print(len(hard), "hard crops on", (len(hard) + 29) // 30, "sheets")


def embed(a):
    import open_clip
    import torch
    torch.set_num_threads(os.cpu_count())
    rs = rows(a.out)
    for key, spec in (("bioclip2", ("hf-hub:imageomics/bioclip-2", None)), ("siglip2", ("ViT-B-16-SigLIP2", "webli")),
                      ("clip", ("ViT-B-16", "openai"))):
        dst = os.path.join(a.out, f"emb_{key}.npz")
        if os.path.exists(dst):
            continue
        m, _, pre = (open_clip.create_model_and_transforms(spec[0]) if spec[1] is None
                     else open_clip.create_model_and_transforms(spec[0], pretrained=spec[1]))
        tok = open_clip.get_tokenizer(spec[0])
        m.eval()
        E = []
        with torch.inference_mode():
            for i in range(0, len(rs), 32):
                x = torch.stack([pre(Image.open(r["path"]).convert("RGB")) for r in rs[i:i + 32]])
                E.append(torch.nn.functional.normalize(m.encode_image(x), dim=-1).numpy())
            tb = torch.nn.functional.normalize(m.encode_text(tok([f"a photo of {t}." for t in BACKGROUND])), dim=-1).numpy()
            ta = torch.nn.functional.normalize(m.encode_text(tok([f"a photo of {t}." for t in ANIMAL])), dim=-1).numpy()
        np.savez(dst, emb=np.concatenate(E), ids=np.array([r["id"] for r in rs]), txt_bg=tb, txt_an=ta,
                 logit_scale=float(m.logit_scale.exp()))
        print("wrote", dst, flush=True)


def speciesnet(a):
    """The current gate on the same box (SpeciesNet sees the MD box, not our 2x square)."""
    sys.path.insert(0, APP)
    from kestrel_analyzer.ml.provider_coordinator import ProviderCoordinator
    from kestrel_analyzer.ml.speciesnet_sam_hq import OnnxClassifier
    from kestrel_analyzer.ml._speciesnet_ensemble import BBox
    sn = os.path.join(APP, "models", "speciesnet")
    lab = glob.glob(os.path.join(sn, "*.labels.*.txt"))[0]
    clf = OnnxClassifier(os.path.join(sn, "speciesNet_v4.0.1a.onnx"), lab, ProviderCoordinator(user_gpu_enabled=False))
    work_of = {s: w for w, s in zip(a.work, a.slug)}
    res = {}
    for n, r in enumerate(rows(a.out)):
        x0, y0, x1, y1 = json.loads(r["box"])
        p = os.path.join(work_of[r["slug"]], "data", r["slug"], "exports", r["export"] + ".jpg")
        img = Image.open(p).convert("RGB")
        pr = clf.predict(p, clf.preprocess(img, bboxes=[BBox(x0, y0, x1 - x0, y1 - y0)]))
        c = pr.get("classifications", {})
        res[r["id"]] = {"top": str(c.get("classes", ["?"])[0]).split(";")[-1], "score": float(c.get("scores", [0])[0])}
        if n % 200 == 0:
            print(n, flush=True)
    json.dump(res, open(os.path.join(a.out, "speciesnet.json"), "w"))


def at_recall(pos, neg, rec):
    t = np.quantile(pos, 1 - rec)  # keep everything scoring >= t
    return float(np.mean(neg < t)), float(t)


def auc(pos, neg):
    from scipy.stats import mannwhitneyu
    return float(mannwhitneyu(pos, neg).statistic / (len(pos) * len(neg)))


def score(a):
    rs = rows(a.out)
    judge = json.load(open(a.judge))  # {"sheet_000": [tile numbers with an animal], ...}
    sk = json.load(open(os.path.join(a.out, "sheet_key.json")))
    animal_hard = {sk[f"{s}:{int(t):02d}"] for s, ts in judge.items() for t in ts if f"{s}:{int(t):02d}" in sk}
    for r in rs:
        r["truth"] = "animal" if r["kind"] == "pos" or r["id"] in animal_hard else (
            "hard_neg" if r["kind"] == "hard" else "rand_neg")
    slugs = np.array([r["slug"] for r in rs])
    truth = np.array([r["truth"] for r in rs])
    kind = np.array([r["kind"] for r in rs])
    rep = {"counts": {t: int((truth == t).sum()) for t in ("animal", "hard_neg", "rand_neg")},
           "hard_that_were_animals": int(len(animal_hard)), "methods": {}}
    scores = {}
    from sklearn.linear_model import LogisticRegression
    for key in ("bioclip2", "siglip2", "clip"):
        d = np.load(os.path.join(a.out, f"emb_{key}.npz"))
        pos = {c: i for i, c in enumerate(d["ids"])}
        E = d["emb"][[pos[r["id"]] for r in rs]].astype(np.float32)
        ls = float(d["logit_scale"])
        # zero-shot: P(animal) from softmax over animal + background prompts
        lg = ls * np.concatenate([E @ d["txt_an"].T, E @ d["txt_bg"].T], 1)
        lg -= lg.max(1, keepdims=True)
        p = np.exp(lg)
        p /= p.sum(1, keepdims=True)
        scores[f"{key}_zeroshot"] = p[:, : len(ANIMAL)].sum(1)
        # linear probe: pos vs rand only (synthetic negatives), leave one perch out
        s = np.zeros(len(rs))
        train_mask = (kind == "pos") | (kind == "rand")
        for sl in sorted(set(slugs)):
            tr = train_mask & (slugs != sl)
            clf = LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced")
            clf.fit(E[tr], (kind[tr] == "pos").astype(int))
            s[slugs == sl] = clf.decision_function(E[slugs == sl])
        scores[f"{key}_probe_synth"] = s
        # ceiling: probe that also sees judged hard negatives (other perches)
        s2 = np.zeros(len(rs))
        for sl in sorted(set(slugs)):
            tr = slugs != sl
            clf = LogisticRegression(C=1.0, max_iter=2000, class_weight="balanced")
            clf.fit(E[tr], (truth[tr] == "animal").astype(int))
            s2[slugs == sl] = clf.decision_function(E[slugs == sl])
        scores[f"{key}_probe_with_hard"] = s2
    snp = os.path.join(a.out, "speciesnet.json")
    if os.path.exists(snp):
        sn = json.load(open(snp))
        blank = {"blank", "no cv result", "vehicle", "human"}
        scores["speciesnet"] = np.array([0.0 if sn[r["id"]]["top"].strip().lower() in blank else sn[r["id"]]["score"] for r in rs])
    P = truth == "animal"
    for m, s in scores.items():
        row = {"auc_vs_hard": auc(s[P], s[truth == "hard_neg"]), "auc_vs_rand": auc(s[P], s[truth == "rand_neg"])}
        for rec in (0.99, 0.995):
            row[f"hard_rejected_at_{rec}"], _ = at_recall(s[P], s[truth == "hard_neg"], rec)
            row[f"rand_rejected_at_{rec}"], _ = at_recall(s[P], s[truth == "rand_neg"], rec)
        rep["methods"][m] = row
    if "speciesnet" in scores:  # the app's own operating point (threshold 0.75 gate + blank skip)
        s = scores["speciesnet"]
        rep["speciesnet_at_app_gate_0.75"] = {"animal_kept": float(np.mean(s[P] >= 0.75)),
                                              "hard_rejected": float(np.mean(s[truth == "hard_neg"] < 0.75)),
                                              "rand_rejected": float(np.mean(s[truth == "rand_neg"] < 0.75))}
    json.dump(rep, open(os.path.join(a.out, "summary.json"), "w"), indent=1)
    np.savez(os.path.join(a.out, "scores.npz"), ids=np.array([r["id"] for r in rs]), truth=truth,
             **{k: v for k, v in scores.items()})
    print(json.dumps(rep, indent=1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["detect", "sheets", "embed", "speciesnet", "score"])
    ap.add_argument("--work", action="append", default=[])
    ap.add_argument("--slug", action="append", default=[])
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--out", required=True)
    ap.add_argument("--judge")
    a = ap.parse_args()
    {"detect": detect, "sheets": sheets, "embed": embed, "speciesnet": speciesnet, "score": score}[a.cmd](a)


if __name__ == "__main__":
    main()
