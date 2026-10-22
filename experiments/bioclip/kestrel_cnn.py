"""Run Kestrel's current species CNN (analyzer/models/model.onnx) on the eval crops.

Reproduces BirdSpeciesClassifier._preprocess exactly: RGB uint8 -> cv2.resize
to 300x300 (stretch) -> float32 0..255 CHW, no normalisation.

Kestrel feeds the CNN the *tight detector box*; Perch only has the square
crop (bird ~ middle half), so we score centre fractions to approximate it.
    python kestrel_cnn.py --frac 0.5
Output: CACHE/cnn_f<frac>.npz  (scores [N,498], crop_id, labels_sci)
"""
import argparse
import os
import time

import cv2
import numpy as np
import onnxruntime as ort
from PIL import Image

from common import CACHE, REPO, load_all_crops, load_catalog, load_eval_crops, name_index, resolve
from embed_images import centre

# The CNN was removed from the app when BioCLIP replaced it; recover it with
#   git show c48bc3d:analyzer/models/model.onnx > legacy_model.onnx
MODEL = os.environ.get("KESTREL_CNN_ONNX", os.path.join(REPO, "analyzer", "models", "model.onnx"))
LABELS = os.path.join(REPO, "analyzer", "models", "labels.txt")


def cnn_labels_sci():
    idx = name_index(load_catalog())
    labels = [l.strip() for l in open(LABELS, encoding="utf-8-sig") if l.strip()]
    out = []
    for l in labels:
        r = resolve(l, idx, americas=True)
        out.append(r["scientific_name"] if r else "")
    return labels, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--frac", type=float, default=0.5)
    ap.add_argument("--all", action="store_true", help="every crop in crops_index.csv, not just the scored eval set")
    a = ap.parse_args()
    rows = load_all_crops() if a.all else load_eval_crops()
    labels, sci = cnn_labels_sci()
    print(f"{sum(1 for s in sci if not s)} CNN labels unmapped to IOC: {[l for l, s in zip(labels, sci) if not s]}")
    sess = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
    name = sess.get_inputs()[0].name
    out, t0 = [], time.time()
    for i, r in enumerate(rows):
        img = np.asarray(centre(Image.open(r["abspath"]).convert("RGB"), a.frac))
        x = np.transpose(cv2.resize(img, (300, 300)).astype(np.float32), (2, 0, 1))[None]
        out.append(sess.run(None, {name: x})[0][0])
        if i % 500 == 0:
            print(i, f"{(time.time()-t0)/(i+1)*1000:.0f} ms/img", flush=True)
    np.savez(os.path.join(CACHE, f"cnn_f{a.frac:g}.npz"), scores=np.array(out, np.float32),
             crop_id=np.array([r["crop_id"] for r in rows]), labels_sci=np.array(sci))
    print("done", time.time() - t0)


if __name__ == "__main__":
    main()
