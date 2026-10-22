"""Round-3 search comparison: OpenAI CLIP B/16 vs SigLIP 2 B/16, judged blind.

    BIOCLIP_WORK=<work dir> python search_compare.py prepare --slug SLUG --out OUT [--frames] "query" ...
    python search_compare.py score --out OUT --judge OUT/judge.json

Methods (higher = better match; prompt "a photo of {q}."):
  clip_base   CLIP B/16, cos(q) - cos("a bird")   (what exp/semantic-search ships)
  sig_base    SigLIP 2, same baseline subtraction
  sig_raw     SigLIP 2, plain cos(q) (its sigmoid training may not need the baseline)
Unit: an image (export). Crop mode scores an image by its best crop (the app's
max-over-crops) and shows that crop; --frames scores the full export.
`prepare` pools every method's top-k images per query, shuffles them into one
numbered sheet per query (OUT/blind/q_NN.jpg + OUT/blind/queries.json, which is
all a judge sees) and writes OUT/key.json. `score` reads {"q_NN": [tile numbers
that match]} and reports precision@k per method and query.
"""
import argparse
import csv
import glob
import json
import os
import random

import numpy as np
import open_clip
import torch
from PIL import Image, ImageDraw

from common import CACHE, DATA

SPECS = {"clip": ("ViT-B-16", "openai", "img_clip-b16_f1.npz"),
         "sig": ("ViT-B-16-SigLIP2", "webli", "img_siglip2-b16_f1.npz")}
METHODS = {"clip_base": ("clip", True), "sig_base": ("sig", True), "sig_raw": ("sig", False)}
TILE = 220


def text(arch, tag, prompts):
    m, _, _ = open_clip.create_model_and_transforms(arch, pretrained=tag)
    m.eval()
    tok = open_clip.get_tokenizer(arch)
    with torch.inference_mode():
        return torch.nn.functional.normalize(m.encode_text(tok(prompts)), dim=-1).numpy()


def frame_emb(arch, tag, paths, cache):
    if os.path.exists(cache):
        return np.load(cache)["emb"]
    m, _, pre = open_clip.create_model_and_transforms(arch, pretrained=tag)
    m.eval()
    out = []
    with torch.inference_mode():
        for i in range(0, len(paths), 16):
            x = torch.stack([pre(Image.open(p).convert("RGB")) for p in paths[i:i + 16]])
            out.append(torch.nn.functional.normalize(m.encode_image(x), dim=-1).numpy())
    e = np.concatenate(out)
    np.savez(cache, emb=e)
    return e


def prepare(a):
    torch.set_num_threads(os.cpu_count())
    rng = random.Random(0)
    prompts = [f"a photo of {q}." for q in a.queries] + ["a photo of a bird."]
    if a.frames:
        paths = sorted(glob.glob(os.path.join(DATA, a.slug, "exports", "*.jpg")))
        units = [(os.path.splitext(os.path.basename(p))[0], [p]) for p in paths]  # export -> [path]
    else:
        by = {}
        for r in csv.DictReader(open(os.path.join(DATA, "crops_index.csv"))):
            if r["slug"] == a.slug:
                by.setdefault(r["export_id"], []).append((r["crop_id"], os.path.join(DATA, r["path"])))
        units = sorted(by.items())
    scores = {}  # method -> (unit score [U], winning path [U])
    for model, (arch, tag, cache_name) in SPECS.items():
        T = text(arch, tag, prompts)
        base, T = T[-1], T[:-1]
        if a.frames:
            E = frame_emb(arch, tag, [u[1][0] for u in units], os.path.join(a.out, f"frames_{model}_{a.slug}.npz"))
            groups = [[i] for i in range(len(units))]
            gpaths = [[u[1][0]] for u in units]
        else:
            d = np.load(os.path.join(CACHE, cache_name))
            pos = {c: i for i, c in enumerate(d["crop_id"])}
            E = d["emb"].astype(np.float32)
            E /= np.linalg.norm(E, axis=1, keepdims=True)
            groups = [[pos[c] for c, _ in u[1] if c in pos] for u in units]
            gpaths = [[p for c, p in u[1] if c in pos] for u in units]
        for meth, (mm, use_base) in METHODS.items():
            if mm != model:
                continue
            per_q = []
            for qi in range(len(a.queries)):
                s = E @ T[qi] - (E @ base if use_base else 0)
                us, up = [], []
                for g, gp in zip(groups, gpaths):
                    if not g:
                        us.append(-9.0)
                        up.append(None)
                        continue
                    j = int(np.argmax(s[g]))
                    us.append(float(s[g][j]))
                    up.append(gp[j])
                per_q.append((np.array(us), up))
            scores[meth] = per_q
    os.makedirs(os.path.join(a.out, "blind"), exist_ok=True)
    key, qmap = {}, {}
    for qi, q in enumerate(a.queries):
        name = f"q_{qi:02d}"
        pool = {}  # export id -> {"path", "methods": {meth: rank}}
        for meth, per_q in scores.items():
            us, up = per_q[qi]
            for rank, u in enumerate(np.argsort(-us)[: a.k]):
                e = pool.setdefault(units[u][0], {"path": up[u], "methods": {}})
                e["methods"][meth] = rank
        items = list(pool.items())
        rng.shuffle(items)
        tiles = []
        entry = {"query": q, "tiles": {}}
        for n, (eid, e) in enumerate(items, 1):
            im = Image.open(e["path"]).convert("RGB")
            im.thumbnail((TILE, TILE))
            t = Image.new("RGB", (TILE, TILE), "white")
            t.paste(im, ((TILE - im.width) // 2, (TILE - im.height) // 2))
            dr = ImageDraw.Draw(t)
            dr.rectangle((0, 0, 30, 18), fill="black")
            dr.text((4, 3), f"{n:02d}", fill="white")
            tiles.append(t)
            entry["tiles"][f"{n:02d}"] = {"export": eid, "path": e["path"], "methods": e["methods"]}
        per_row = 6
        rows = (len(tiles) + per_row - 1) // per_row
        out = Image.new("RGB", (TILE * per_row, TILE * rows), "white")
        for i, t in enumerate(tiles):
            out.paste(t, ((i % per_row) * TILE, (i // per_row) * TILE))
        out.save(os.path.join(a.out, "blind", f"{name}.jpg"), quality=88)
        key[name] = entry
        qmap[name] = {"query": q, "tiles": len(tiles)}
    json.dump(key, open(os.path.join(a.out, "key.json"), "w"), indent=1)
    json.dump(qmap, open(os.path.join(a.out, "blind", "queries.json"), "w"), indent=1)
    print({n: v["tiles"] for n, v in qmap.items()})


def score(a):
    key = json.load(open(os.path.join(a.out, "key.json")))
    judges = [json.load(open(j)) for j in a.judge]
    res = {}
    for name, e in key.items():
        rel = [set(f"{int(x):02d}" for x in j.get(name, [])) for j in judges]
        row = {"query": e["query"]}
        for meth in METHODS:
            top = [t for t, v in e["tiles"].items() if meth in v["methods"]]
            row[meth] = float(np.mean([len(set(top) & r) / max(len(top), 1) for r in rel]))
        res[name] = row
    mean = {m: float(np.mean([r[m] for r in res.values()])) for m in METHODS}
    json.dump({"per_query": res, "mean_precision": mean}, open(os.path.join(a.out, "summary.json"), "w"), indent=1)
    for n, r in res.items():
        print(f"{n} {r['query'][:50]:50s} " + " ".join(f"{m}={r[m]:.2f}" for m in METHODS))
    print("mean", {m: round(v, 3) for m, v in mean.items()})


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--slug", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--frames", action="store_true")
    p.add_argument("-k", type=int, default=10)
    p.add_argument("queries", nargs="+")
    s = sub.add_parser("score")
    s.add_argument("--out", required=True)
    s.add_argument("--judge", action="append", required=True)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    prepare(a) if a.cmd == "prepare" else score(a)


if __name__ == "__main__":
    main()
