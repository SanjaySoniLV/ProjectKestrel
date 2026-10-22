"""Follow-up experiment 1: zero-shot CLIP sharpness vs Kestrel's quality CNN.

    python exp_quality.py prepare --work DIR [--work DIR ...] --out OUT --n-disagree 16 --n-agree 8 --n-random 16
    python exp_quality.py score --out OUT --judge OUT/judge_a.json [--judge OUT/judge_b.json]

Within-scene ranking task. Only images with exactly one crop are used, so which
crop represents an image is never in question. Rankers (higher = sharper):

  kestrel   the crop's stored quality score (Kestrel's quality CNN)
  clip      OpenAI CLIP ViT-B/16, cos(img, P) - cos(img, N) with
            P = "a sharp, in-focus photo of a bird", N = "a blurry, out-of-focus photo of a bird"
  siglip2   the same text direction with SigLIP 2 ViT-B/16 (Apache-2.0), as a licence-clean alternative
  laplacian variance of the Laplacian over the central 50% of the crop (bird region), a classical baseline

`prepare` picks scenes (>=5 single-crop images, <=8 images each, spread over Kestrel's
quality range): the n-disagree scenes with the lowest clip-vs-kestrel Spearman, the
n-agree with the highest, and n-random from the rest. It writes
  OUT/blind/scene_NN.jpg      shuffled tiles labelled A.. (what the judges see)
  OUT/key.json                tile -> crop + every ranker's score (judges must not see it)
`score` reads judge rankings ({"scene_NN": "CAEBD..."} sharpest -> blurriest), reports
each ranker's Spearman vs. each judge (and judge vs. judge), by scene group, and
writes OUT/review/scene_NN.jpg with one row per ordering for a human to check.
"""
import argparse
import csv
import json
import os
import random
import string

import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from scipy.stats import spearmanr

POS = "a sharp, in-focus photo of a bird"
NEG = "a blurry, out-of-focus photo of a bird"
TILE = 400


def rows_for(work_dirs):
    out = []
    for w in work_dirs:
        for r in csv.DictReader(open(os.path.join(w, "data", "crops_index.csv"))):
            if r["n_crops_in_export"] != "1" or r["quality"] in ("", "None"):
                continue
            r["abspath"] = os.path.join(w, "data", r["path"])
            r["work"] = w
            if os.path.exists(r["abspath"]):
                out.append(r)
    return out


def load_emb(work_dirs, name):
    emb = {}
    for w in work_dirs:
        p = os.path.join(w, "cache", name)
        if os.path.exists(p):
            d = np.load(p)
            for c, e in zip(d["crop_id"], d["emb"].astype(np.float32)):
                emb[c] = e / np.linalg.norm(e)
    return emb


def text_dir(arch, tag):
    import open_clip
    import torch
    m, _, _ = open_clip.create_model_and_transforms(arch, pretrained=tag)
    tok = open_clip.get_tokenizer(arch)
    m.eval()
    with torch.inference_mode():
        t = torch.nn.functional.normalize(m.encode_text(tok([POS, NEG])), dim=-1).numpy()
    return t[0] - t[1]


def siglip_scores(paths):
    import open_clip
    import torch
    torch.set_num_threads(os.cpu_count())
    m, _, pre = open_clip.create_model_and_transforms("ViT-B-16-SigLIP2", pretrained="webli")
    tok = open_clip.get_tokenizer("ViT-B-16-SigLIP2")
    m.eval()
    with torch.inference_mode():
        t = torch.nn.functional.normalize(m.encode_text(tok([POS, NEG])), dim=-1).numpy()
        d = t[0] - t[1]
        out = {}
        for i in range(0, len(paths), 16):
            x = torch.stack([pre(Image.open(p).convert("RGB")) for p in paths[i:i + 16]])
            e = torch.nn.functional.normalize(m.encode_image(x), dim=-1).numpy()
            for p, v in zip(paths[i:i + 16], e @ d):
                out[p] = float(v)
    return out


def laplacian(path):
    g = Image.open(path).convert("L").resize((512, 512))
    g = g.crop((128, 128, 384, 384))
    a = np.asarray(g, np.float32)
    lap = a[1:-1, 2:] + a[1:-1, :-2] + a[2:, 1:-1] + a[:-2, 1:-1] - 4 * a[1:-1, 1:-1]
    return float(lap.var())


def tile(path, label=None, caption=None):
    im = Image.open(path).convert("RGB").resize((TILE, TILE), Image.LANCZOS)
    canvas = Image.new("RGB", (TILE, TILE + 22), "white")
    canvas.paste(im, (0, 0))
    d = ImageDraw.Draw(canvas)
    if label:
        d.rectangle((0, 0, 34, 30), fill="black")
        d.text((10, 8), label, fill="white")
    if caption:
        d.text((4, TILE + 5), caption, fill="black")
    return canvas


def grid(tiles, per_row=4):
    rows = (len(tiles) + per_row - 1) // per_row
    w, h = tiles[0].size
    out = Image.new("RGB", (w * per_row, h * rows), "white")
    for i, t in enumerate(tiles):
        out.paste(t, ((i % per_row) * w, (i // per_row) * h))
    return out


def prepare(a):
    rows = rows_for(a.work)
    clip = load_emb(a.work, "img_clip-b16_f1.npz")
    d_clip = text_dir("ViT-B-16", "openai")
    scenes = {}
    for r in rows:
        if r["crop_id"] in clip:
            scenes.setdefault((r["slug"], r["scene_id"]), []).append(r)
    cands = []
    rng = random.Random(0)
    for key, rs in scenes.items():
        if len(rs) < 5:
            continue
        rs = sorted(rs, key=lambda r: float(r["quality"]))
        if len(rs) > 8:  # spread across Kestrel's range
            idx = np.linspace(0, len(rs) - 1, 8).round().astype(int)
            rs = [rs[i] for i in sorted(set(idx))]
        k = [float(r["quality"]) for r in rs]
        c = [float(clip[r["crop_id"]] @ d_clip) for r in rs]
        if np.std(k) < 1e-6:
            continue
        cands.append((spearmanr(k, c).correlation, key, rs))
    cands.sort(key=lambda x: x[0])
    dis = cands[: a.n_disagree]
    agr = cands[-a.n_agree:]
    rest = cands[a.n_disagree:-a.n_agree]
    rnd = rng.sample(rest, min(a.n_random, len(rest)))
    chosen = [("disagree", *x) for x in dis] + [("agree", *x) for x in agr] + [("random", *x) for x in rnd]
    rng.shuffle(chosen)
    print(f"{len(cands)} candidate scenes; chosen {len(chosen)}")
    paths = [r["abspath"] for _, _, _, rs in chosen for r in rs]
    sig = siglip_scores(paths)
    os.makedirs(os.path.join(a.out, "blind"), exist_ok=True)
    key = {}
    for n, (group, rho, (slug, sid), rs) in enumerate(chosen):
        name = f"scene_{n:02d}"
        order = list(range(len(rs)))
        rng.shuffle(order)
        labels = string.ascii_uppercase[: len(rs)]
        tiles, entry = [], {"group": group, "slug": slug, "scene_id": sid, "clip_vs_kestrel_rho": rho, "tiles": {}}
        for lab, i in zip(labels, order):
            r = rs[i]
            tiles.append(tile(r["abspath"], lab))
            entry["tiles"][lab] = {
                "crop_id": r["crop_id"], "export": r["export_filename"], "path": r["abspath"],
                "kestrel": float(r["quality"]), "clip": float(clip[r["crop_id"]] @ d_clip),
                "siglip2": sig[r["abspath"]], "laplacian": laplacian(r["abspath"]),
            }
        grid(tiles).save(os.path.join(a.out, "blind", f"{name}.jpg"), quality=90)
        key[name] = entry
    json.dump(key, open(os.path.join(a.out, "key.json"), "w"), indent=1)


RANKERS = ["kestrel", "clip", "siglip2", "laplacian"]


def score(a):
    key = json.load(open(os.path.join(a.out, "key.json")))
    judges = {os.path.splitext(os.path.basename(j))[0]: json.load(open(j)) for j in a.judge}
    res = {g: {f"{rk}_vs_{jn}": [] for rk in RANKERS for jn in judges} for g in ("disagree", "agree", "random", "all")}
    for g in res:
        if len(judges) > 1:
            res[g]["judge_vs_judge"] = []
    os.makedirs(os.path.join(a.out, "review"), exist_ok=True)
    for name, e in key.items():
        labs = list(e["tiles"])
        rows = {}
        for jn, jr in judges.items():
            order = [c for c in str(jr.get(name, "")).upper() if c in labs]
            if sorted(order) != sorted(labs):
                continue
            jrank = {lab: -order.index(lab) for lab in labs}  # higher = sharper
            rows[jn] = order
            for rk in RANKERS:
                rho = spearmanr([e["tiles"][l][rk] for l in labs], [jrank[l] for l in labs]).correlation
                for g in (e["group"], "all"):
                    res[g][f"{rk}_vs_{jn}"].append(rho)
        if len(rows) == 2:
            (j1, o1), (j2, o2) = rows.items()
            rho = spearmanr([-o1.index(l) for l in labs], [-o2.index(l) for l in labs]).correlation
            for g in (e["group"], "all"):
                res[g]["judge_vs_judge"].append(rho)
        # review sheet: one row per ordering
        lines = {f"judge {jn}": o for jn, o in rows.items()}
        for rk in RANKERS:
            lines[rk] = sorted(labs, key=lambda l: -e["tiles"][l][rk])
        sheet = []
        for title, order in lines.items():
            tl = [tile(e["tiles"][l]["path"], l) for l in order]
            row = grid([t.resize((200, 211)) for t in tl], per_row=len(tl))
            band = Image.new("RGB", (row.width + 150, row.height), "white")
            band.paste(row, (150, 0))
            ImageDraw.Draw(band).text((6, row.height // 2), f"{title}\n(sharp -> blurry)", fill="black")
            sheet.append(band)
        W = max(b.width for b in sheet)
        out = Image.new("RGB", (W, sum(b.height for b in sheet)), "white")
        y = 0
        for b in sheet:
            out.paste(b, (0, y))
            y += b.height
        out.save(os.path.join(a.out, "review", f"{name}_{e['group']}.jpg"), quality=85)
    summary = {g: {k: {"mean_rho": float(np.nanmean(v)) if v else None, "n": len(v)} for k, v in d.items()} for g, d in res.items()}
    json.dump(summary, open(os.path.join(a.out, "summary.json"), "w"), indent=1)
    print(json.dumps(summary, indent=1))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--work", action="append", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--n-disagree", type=int, default=16)
    p.add_argument("--n-agree", type=int, default=8)
    p.add_argument("--n-random", type=int, default=16)
    s = sub.add_parser("score")
    s.add_argument("--out", required=True)
    s.add_argument("--judge", action="append", required=True)
    a = ap.parse_args()
    prepare(a) if a.cmd == "prepare" else score(a)


if __name__ == "__main__":
    main()
