"""Embed eval crops with a BioCLIP model; cache L2-normalised embeddings.

    python embed_images.py --model bioclip-2 --frac 1.0 [--flip] [--limit N]

--frac  centre-crop fraction of Kestrel's square crop before the model's own
        224 resize. Kestrel's square crop is 2x the box holding 80% of the
        SAM mask, so the bird fills roughly the middle half; frac=0.6 zooms in.
--flip  also embed the horizontal mirror (stored separately for TTA).
Output: CACHE/img_<model>_f<frac>[_flip].npz with keys emb (float16), crop_id.
"""
import argparse
import os
import time

import numpy as np
import open_clip
import torch
from PIL import Image

from common import CACHE, load_eval_crops

MODELS = {
    "bioclip-1": "hf-hub:imageomics/bioclip",  # ViT-B/16, for the cost/accuracy curve
    "bioclip-2": "hf-hub:imageomics/bioclip-2",
    "bioclip-2.5": "hf-hub:imageomics/bioclip-2.5-vith14",
}


def centre(img, frac):
    if frac >= 1.0:
        return img
    w, h = img.size
    cw, ch = w * frac, h * frac
    l, t = (w - cw) / 2, (h - ch) / 2
    return img.crop((round(l), round(t), round(l + cw), round(t + ch)))


class Crops(torch.utils.data.Dataset):
    def __init__(self, rows, pre, frac, flip):
        self.rows, self.pre, self.frac, self.flip = rows, pre, frac, flip

    def __len__(self):
        return len(self.rows)

    def __getitem__(self, i):
        img = centre(Image.open(self.rows[i]["abspath"]).convert("RGB"), self.frac)
        if self.flip:
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        return self.pre(img)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", choices=MODELS, required=True)
    ap.add_argument("--frac", type=float, default=1.0)
    ap.add_argument("--flip", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--onnx", help="run this exported image-tower ONNX (e.g. int8) via onnxruntime instead of torch")
    ap.add_argument("--tag", default="", help="suffix for the cache file, e.g. _int8")
    ap.add_argument("--bf16", action="store_true",
                    help="CPU bf16 autocast (AMX/AVX512-bf16). ~2.5-3x faster at --batch 1; batched bf16 "
                         "hit a very slow attention path in our runs, so pair it with --batch 1")
    a = ap.parse_args()

    torch.set_num_threads(os.cpu_count())
    rows = load_eval_crops()
    if a.limit:
        rows = rows[: a.limit]
    os.makedirs(CACHE, exist_ok=True)
    out = os.path.join(CACHE, f"img_{a.model}_f{a.frac:g}{'_flip' if a.flip else ''}{a.tag}.npz")

    model, _, pre = open_clip.create_model_and_transforms(MODELS[a.model])
    model.eval()
    if a.onnx:
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = os.cpu_count()
        sess = ort.InferenceSession(a.onnx, so, providers=["CPUExecutionProvider"])
        encode = lambda x: torch.from_numpy(sess.run(None, {"image": x.numpy()})[0])
    else:
        encode = model.encode_image
    dl = torch.utils.data.DataLoader(Crops(rows, pre, a.frac, a.flip), batch_size=a.batch, num_workers=2)
    embs, t0 = [], time.time()
    with torch.inference_mode(), torch.autocast("cpu", dtype=torch.bfloat16, enabled=a.bf16):
        for bi, x in enumerate(dl):
            e = encode(x).float()
            embs.append(torch.nn.functional.normalize(e, dim=-1).numpy().astype(np.float16))
            if bi % 20 == 0:
                done = (bi + 1) * a.batch
                print(f"{done}/{len(rows)}  {(time.time()-t0)/done*1000:.0f} ms/img", flush=True)
    np.savez(out, emb=np.concatenate(embs), crop_id=np.array([r["crop_id"] for r in rows]))
    print(f"wrote {out}  {len(rows)} crops in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
