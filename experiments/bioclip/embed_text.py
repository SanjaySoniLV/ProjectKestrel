"""Encode IOC species label strings with a BioCLIP text tower; cache them.

    python embed_text.py --model bioclip-2 --text-type taxon_com --templates single [--subset FILE]

Each class embedding = L2-normalised mean over templates of the normalised
per-template embeddings (standard CLIP prompt ensembling).
Output: CACHE/txt_<model>_<text_type>_<templates>[_<subset>].npz  (emb, sci)
"""
import argparse
import os
import time

import numpy as np
import open_clip
import torch

from common import CACHE, TEXT_TYPES, load_catalog, templates, text_for
from embed_images import MODELS


def encode_text_trimmed(model, tokens):
    """open_clip CLIP.encode_text, minus the trailing pad positions.

    The text tower is causal, so tokens up to EOT never see the padding;
    trimming to the batch's longest prompt is exact and 2-4x faster on CPU.
    """
    L = int((tokens != 0).sum(1).max())
    tokens = tokens[:, :L]
    x = model.token_embedding(tokens)
    x = x + model.positional_embedding[:L]
    x = model.transformer(x, attn_mask=model.attn_mask[:L, :L])
    x = model.ln_final(x)
    x = x[torch.arange(x.shape[0]), tokens.argmax(dim=-1)]  # EOT has the largest id
    return x @ model.text_projection


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=MODELS, required=True)
    ap.add_argument("--text-type", choices=TEXT_TYPES, required=True)
    ap.add_argument("--templates", default="single")
    ap.add_argument("--subset", help="file of IOC scientific names to restrict to")
    ap.add_argument("--batch", type=int, default=256)
    a = ap.parse_args()

    torch.set_num_threads(os.cpu_count())
    cat = load_catalog()
    tag = ""
    if a.subset:
        keep = {l.strip() for l in open(a.subset) if l.strip()}
        cat = [r for r in cat if r["scientific_name"] in keep]
        tag = "_" + os.path.splitext(os.path.basename(a.subset))[0]
    tmpls = templates(a.templates)
    model, _, _ = open_clip.create_model_and_transforms(MODELS[a.model])
    model.eval()
    tok = open_clip.get_tokenizer(MODELS[a.model])

    names = [text_for(r, a.text_type) for r in cat]
    order = np.argsort([len(n) for n in names])  # batch similar lengths together
    acc = np.zeros((len(names), model.text_projection.shape[1] if model.text_projection is not None else 768), np.float32)
    t0 = time.time()
    with torch.inference_mode():
        for t in tmpls:
            for i in range(0, len(names), a.batch):
                ix = order[i : i + a.batch]
                e = encode_text_trimmed(model, tok([t(names[j]) for j in ix]))
                acc[ix] += torch.nn.functional.normalize(e, dim=-1).numpy()
            print(f"template done ({time.time()-t0:.0f}s)", flush=True)
    acc /= np.linalg.norm(acc, axis=1, keepdims=True)
    os.makedirs(CACHE, exist_ok=True)
    out = os.path.join(CACHE, f"txt_{a.model}_{a.text_type}_{a.templates}{tag}.npz")
    np.savez(out, emb=acc.astype(np.float16), sci=np.array([r["scientific_name"] for r in cat]))
    print(f"wrote {out}: {len(names)} classes x {len(tmpls)} templates in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
