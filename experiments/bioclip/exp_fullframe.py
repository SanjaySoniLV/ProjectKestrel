"""Follow-up experiment 3: does indexing whole frames (exports) help smart search?

    BIOCLIP_WORK=<val dir> python exp_fullframe.py --slug SLUG --out OUT "query" ...

Compares two image-level indexes with the same model (OpenAI CLIP ViT-B/16) and
the same scoring as the app prototype (cos(q) - cos("a bird"), prompt "a photo of {q}."):
  crop   max over the image's bird crops (what exp/semantic-search indexes today)
  frame  the full export (Kestrel's downscaled JPEG of the whole photo)
and a third, "both" = max(crop, frame) after z-scoring each. Writes contact
sheets of the top-k exports per index per query, and counts how many top-k
frame hits have no detector crop at all.
"""
import argparse
import csv
import glob
import json
import os

import numpy as np
import open_clip
import torch
from PIL import Image, ImageDraw

from common import DATA, CACHE


def embed(model, pre, paths):
    out = []
    with torch.inference_mode():
        for i in range(0, len(paths), 16):
            x = torch.stack([pre(Image.open(p).convert("RGB")) for p in paths[i:i + 16]])
            out.append(torch.nn.functional.normalize(model.encode_image(x), dim=-1).numpy())
    return np.concatenate(out)


def sheet(paths, scores, flags, dst, title):
    tiles = []
    for p, s, f in zip(paths, scores, flags):
        im = Image.open(p).convert("RGB")
        im.thumbnail((300, 200))
        t = Image.new("RGB", (300, 222), "white")
        t.paste(im, ((300 - im.width) // 2, 0))
        ImageDraw.Draw(t).text((4, 205), f"{s:+.3f}{'  NO CROP' if f else ''}", fill="red" if f else "black")
        tiles.append(t)
    out = Image.new("RGB", (300 * 5, 222 * 2 + 20), "white")
    ImageDraw.Draw(out).text((4, 4), title, fill="black")
    for i, t in enumerate(tiles[:10]):
        out.paste(t, ((i % 5) * 300, 20 + (i // 5) * 222))
    out.save(dst, quality=85)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("-k", type=int, default=10)
    ap.add_argument("queries", nargs="+")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    torch.set_num_threads(os.cpu_count())
    m, _, pre = open_clip.create_model_and_transforms("ViT-B-16", pretrained="openai")
    m.eval()
    tok = open_clip.get_tokenizer("ViT-B-16")
    # crop embeddings -> export
    d = np.load(os.path.join(CACHE, "img_clip-b16_f1.npz"))
    cemb = {c: e for c, e in zip(d["crop_id"], d["emb"].astype(np.float32))}
    crops_by_export = {}
    for r in csv.DictReader(open(os.path.join(DATA, "crops_index.csv"))):
        if r["slug"] == a.slug and r["crop_id"] in cemb:
            crops_by_export.setdefault(r["export_id"], []).append(cemb[r["crop_id"]])
    exp_paths = sorted(glob.glob(os.path.join(DATA, a.slug, "exports", "*.jpg")))
    exp_ids = [os.path.splitext(os.path.basename(p))[0] for p in exp_paths]
    cache = os.path.join(a.out, f"frames_{a.slug}.npz")
    if os.path.exists(cache):
        fe = np.load(cache)["emb"]
    else:
        fe = embed(m, pre, exp_paths)
        np.savez(cache, emb=fe)
    with torch.inference_mode():
        T = torch.nn.functional.normalize(m.encode_text(tok([f"a photo of {q}." for q in a.queries] + ["a photo of a bird."])), dim=-1).numpy()
    base, T = T[-1], T[:-1]
    no_crop = np.array([e not in crops_by_export for e in exp_ids])
    res = {"exports": len(exp_ids), "exports_without_crops": int(no_crop.sum()), "queries": {}}
    z = lambda v: (v - np.nanmean(v)) / (np.nanstd(v) + 1e-9)
    for qi, q in enumerate(a.queries):
        frame = fe @ T[qi] - fe @ base
        crop = np.array([max(float(c @ T[qi] - c @ base) for c in crops_by_export[e]) if e in crops_by_export else np.nan for e in exp_ids])
        both = np.fmax(z(frame), np.nan_to_num(z(crop), nan=-9))
        qres = {}
        for name, s in (("crop", crop), ("frame", frame), ("both", both)):
            order = [i for i in np.argsort(-np.nan_to_num(s, nan=-9))][: a.k]
            qres[name] = {"top": [exp_ids[i] for i in order], "no_crop_in_top": int(no_crop[order].sum())}
            sheet([exp_paths[i] for i in order], s[order], no_crop[order],
                  os.path.join(a.out, f"{a.slug}_{qi:02d}_{name}.jpg"), f"{name} index: {q}")
        qres["top10_overlap_crop_frame"] = len(set(qres["crop"]["top"]) & set(qres["frame"]["top"]))
        res["queries"][q] = qres
    json.dump(res, open(os.path.join(a.out, f"summary_{a.slug}.json"), "w"), indent=1)
    print(json.dumps({q: {k: (v["no_crop_in_top"] if isinstance(v, dict) else v) for k, v in r.items()} for q, r in res["queries"].items()}, indent=1))
    print("exports", res["exports"], "without crops", res["exports_without_crops"])


if __name__ == "__main__":
    main()
