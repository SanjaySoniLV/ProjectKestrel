"""Q2: does BioCLIP 2 support free-text semantic search over a folder?

    python search_test.py --dir DATA/<slug> --out OUT_DIR "query one" "query two" ...

Embeds every crop (and, with --exports, every full frame) in the folder with
BioCLIP 2, then ranks them against each text query (prompt "a photo of {q}.").
Writes OUT_DIR/<n>_<query>.jpg contact sheets (top-k, ranked left to right)
and OUT_DIR/results.json. Contact sheets are for eyeballing; never commit them.
"""
import argparse
import glob
import json
import os

import numpy as np
import open_clip
import torch
from PIL import Image, ImageDraw

HUB = "hf-hub:imageomics/bioclip-2"


def load(spec):
    """'hf-hub:...' or 'ARCH:PRETRAINED' (e.g. ViT-B-16:openai, a general-purpose CLIP)."""
    if spec.startswith("hf-hub:"):
        m, _, pre = open_clip.create_model_and_transforms(spec)
        return m, pre, open_clip.get_tokenizer(spec)
    arch, tag = spec.split(":")
    m, _, pre = open_clip.create_model_and_transforms(arch, pretrained=tag)
    return m, pre, open_clip.get_tokenizer(arch)


def embed_images(model, pre, paths, cache):
    if os.path.exists(cache):
        d = np.load(cache)
        if list(d["paths"]) == paths:
            return d["emb"]
    embs = []
    with torch.inference_mode():
        for i in range(0, len(paths), 16):
            x = torch.stack([pre(Image.open(p).convert("RGB")) for p in paths[i:i + 16]])
            embs.append(torch.nn.functional.normalize(model.encode_image(x), dim=-1).numpy())
    e = np.concatenate(embs)
    np.savez(cache, emb=e, paths=np.array(paths))
    return e


def sheet(paths, scores, dst, k):
    tiles = []
    for p, s in zip(paths[:k], scores[:k]):
        im = Image.open(p).convert("RGB")
        im.thumbnail((256, 256))
        canvas = Image.new("RGB", (256, 276), "white")
        canvas.paste(im, ((256 - im.width) // 2, 0))
        ImageDraw.Draw(canvas).text((4, 258), f"{s:.3f}", fill="black")
        tiles.append(canvas)
    out = Image.new("RGB", (256 * len(tiles), 276), "white")
    for i, t in enumerate(tiles):
        out.paste(t, (256 * i, 0))
    out.save(dst, quality=85)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--exports", action="store_true", help="search full frames instead of crops")
    ap.add_argument("-k", type=int, default=8)
    ap.add_argument("--model", default=HUB)
    ap.add_argument("--baseline", help="subtract this prompt's score (e.g. 'a bird') to cancel generic/blurry hubs")
    ap.add_argument("--emb", help="precomputed embed_images.py npz (emb, crop_id) to use instead of embedding")
    ap.add_argument("queries", nargs="+")
    a = ap.parse_args()
    torch.set_num_threads(os.cpu_count())
    os.makedirs(a.out, exist_ok=True)
    model, pre, tok = load(a.model)
    model.eval()
    sub = "exports" if a.exports else "crops"
    tag = a.model.replace("hf-hub:imageomics/", "").replace(":", "_")
    paths = sorted(glob.glob(os.path.join(a.dir, sub, "*.jpg")))
    if a.emb:
        d = np.load(a.emb)
        pos = {c: i for i, c in enumerate(d["crop_id"])}
        paths = [p for p in paths if os.path.splitext(os.path.basename(p))[0] in pos]
        img = d["emb"][[pos[os.path.splitext(os.path.basename(p))[0]] for p in paths]].astype(np.float32)
        img /= np.linalg.norm(img, axis=1, keepdims=True)
        tag += "_app"
    else:
        img = embed_images(model, pre, paths, os.path.join(a.out, f"emb_{tag}_{sub}.npz"))
    with torch.inference_mode():
        txt = torch.nn.functional.normalize(model.encode_text(tok([f"a photo of {q}." for q in a.queries])), dim=-1).numpy()
    sims = img @ txt.T
    if a.baseline:
        with torch.inference_mode():
            b = torch.nn.functional.normalize(model.encode_text(tok([f"a photo of {a.baseline}."])), dim=-1).numpy()
        sims = sims - img @ b.T
        tag += "_minus_baseline"
    res = {}
    for qi, q in enumerate(a.queries):
        order = np.argsort(-sims[:, qi])
        res[q] = [(os.path.basename(paths[i]), float(sims[i, qi])) for i in order[: a.k]]
        sheet([paths[i] for i in order], sims[order, qi], os.path.join(a.out, f"{tag}_{sub}_{qi:02d}_{q[:40].replace(' ', '_')}.jpg"), a.k)
    # how separable are the queries? correlation of score vectors
    json.dump({"queries": a.queries, "top": res, "score_corr": np.corrcoef(sims.T).round(3).tolist()},
              open(os.path.join(a.out, f"results_{tag}_{sub}.json"), "w"), indent=1)
    print(json.dumps({q: v[:3] for q, v in res.items()}, indent=1))
    print("score correlation between queries:\n", np.corrcoef(sims.T).round(2))


if __name__ == "__main__":
    main()
