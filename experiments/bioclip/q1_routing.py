"""Q1: could BioCLIP 2 replace the SpeciesNet classifier for bird / non-bird routing?

    python q1_routing.py --work DIR [--work DIR ...] [--max-birds 1500]

Truth comes from scene tags. A scene is "bird" if every tag resolves to an IOC
bird (or is "Domestic Duck"); "non-bird" if every tag is a non-bird animal; it
is skipped otherwise (mixed, "Unknown", untagged). Every crop in the scene takes
the scene's truth.

- SpeciesNet: the app's ONNX classifier (480 px, bilinear, /255) on the crop.
  Bird iff the best Aves score beats the best other non-ignored label, which is
  the pipeline's tie-break (speciesnet_taxonomy.bird_vs_wildlife_classifier_scores).
- BioCLIP 2: classes = 11,251 IOC birds + SpeciesNet's non-bird labels, prompt
  "a photo of {common name}.". Bird iff summed bird probability > 0.5.

Writes $BIOCLIP_WORK/results/q1_routing.json (the first --work dir).
"""
import argparse
import csv
import json
import os
import random
import sys

import numpy as np
import onnxruntime as ort
import open_clip
import torch
from PIL import Image

from common import REPO, load_catalog, name_index, resolve

sys.path.insert(0, os.path.join(REPO, "analyzer"))
from kestrel_analyzer.ml.speciesnet_taxonomy import is_bird_taxon, is_ignored_prediction  # noqa: E402

SN = os.path.join(REPO, "analyzer", "models", "speciesnet")
SN_ONNX = os.path.join(SN, "speciesNet_v4.0.1a.onnx")
SN_LABELS = os.path.join(SN, "always_crop_99710272_22x8_v12_epoch_00148.labels.20251208.txt")
NONBIRD_IGNORE = {"unknown", "unknown!"}


def load_rows(work_dirs):
    idx = name_index(load_catalog())
    rows = []
    for w in work_dirs:
        for r in csv.DictReader(open(os.path.join(w, "data", "crops_index.csv"))):
            tags = json.loads(r["scene_species"])
            if not tags or any(t.lower() in NONBIRD_IGNORE for t in tags):
                continue
            kinds = {"bird" if (t == "Domestic Duck" or resolve(t, idx)) else "nonbird" for t in tags}
            if len(kinds) != 1:
                continue
            r["truth"] = kinds.pop()
            r["tags"] = tags
            r["abspath"] = os.path.join(w, "data", r["path"])
            rows.append(r)
    return [r for r in rows if os.path.exists(r["abspath"])]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", action="append", required=True)
    ap.add_argument("--max-birds", type=int, default=1500)
    a = ap.parse_args()
    rows = load_rows(a.work)
    birds = [r for r in rows if r["truth"] == "bird"]
    random.Random(0).shuffle(birds)
    nonbirds = [r for r in rows if r["truth"] == "nonbird"]
    rows = birds[: a.max_birds] + nonbirds
    print(f"{len(rows)} crops: {min(len(birds), a.max_birds)} bird, {len(nonbirds)} non-bird "
          f"from tags {sorted({t for r in nonbirds for t in r['tags']})}")

    sn_labels = [l.strip() for l in open(SN_LABELS, encoding="utf-8-sig")]
    so = ort.SessionOptions()
    so.intra_op_num_threads = os.cpu_count()
    sn = ort.InferenceSession(SN_ONNX, so, providers=["CPUExecutionProvider"])

    torch.set_num_threads(os.cpu_count())
    model, _, pre = open_clip.create_model_and_transforms("hf-hub:imageomics/bioclip-2")
    model.eval()
    tok = open_clip.get_tokenizer("hf-hub:imageomics/bioclip-2")
    cat = load_catalog()
    nb = [l for l in sn_labels if not is_bird_taxon(l) and not is_ignored_prediction(l)]
    nb_names = [l.split(";")[-1] or l.split(";")[-2] for l in nb]
    names = [r["canonical_common_name"] for r in cat] + nb_names
    is_bird_cls = np.array([True] * len(cat) + [False] * len(nb))
    with torch.inference_mode():
        txt = []
        for i in range(0, len(names), 256):
            txt.append(torch.nn.functional.normalize(
                model.encode_text(tok([f"a photo of {n}." for n in names[i:i + 256]])), dim=-1))
        txt = torch.cat(txt).numpy()

    out = []
    for i, r in enumerate(rows):
        img = Image.open(r["abspath"]).convert("RGB")
        x = (np.asarray(img.resize((480, 480), Image.BILINEAR), np.float32) / 255.0)[None]
        lg = sn.run(None, {"input": x})[0][0]
        p = np.exp(lg - lg.max())
        p /= p.sum()
        best_bird = max((p[j] for j, l in enumerate(sn_labels) if is_bird_taxon(l)), default=0.0)
        best_other = max((p[j] for j, l in enumerate(sn_labels)
                          if not is_bird_taxon(l) and not is_ignored_prediction(l)), default=0.0)
        sn_top = sn_labels[int(p.argmax())].split(";")[-1]
        with torch.inference_mode():
            e = torch.nn.functional.normalize(model.encode_image(pre(img)[None]), dim=-1).numpy()[0]
        q = np.exp(100 * e @ txt.T - (100 * e @ txt.T).max())
        q /= q.sum()
        out.append({
            "slug": r["slug"], "crop_id": r["crop_id"], "truth": r["truth"], "tags": r["tags"],
            "sn_bird": bool(best_bird > best_other), "sn_top": sn_top,
            "bc_pbird": float(q[is_bird_cls].sum()), "bc_top": names[int(q.argmax())],
        })
        if i % 200 == 0:
            print(i, flush=True)

    def acc(key, truth):
        sub = [o for o in out if o["truth"] == truth]
        good = [(o[key] if key == "sn_bird" else o["bc_pbird"] > 0.5) == (truth == "bird") for o in sub]
        return float(np.mean(good)) if sub else float("nan")

    summary = {k: {"bird_recall": acc(k, "bird"), "nonbird_recall": acc(k, "nonbird")} for k in ("sn_bird", "bc_bird")}
    print(json.dumps(summary, indent=1))
    res_dir = os.path.join(a.work[0], "results")
    os.makedirs(res_dir, exist_ok=True)
    json.dump({"summary": summary, "rows": out}, open(os.path.join(res_dir, "q1_routing.json"), "w"), indent=1)


if __name__ == "__main__":
    main()
