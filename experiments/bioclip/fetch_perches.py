"""Download a public/unlisted Perch's crops + scene labels into a local eval dataset.

Usage:
    python fetch_perches.py --out DATA_DIR SLUG [SLUG ...]

Writes, per slug:
    DATA_DIR/<slug>/perch.json        raw /v1/public/perches/<slug> response
    DATA_DIR/<slug>/crops/<assetId>.jpg
    DATA_DIR/<slug>/exports/<assetId>.jpg   (with --exports)
And one combined index:
    DATA_DIR/crops_index.csv          one row per crop (scene labels joined on)

Stdlib only. Images are never committed — DATA_DIR lives outside the repo.
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

API = "https://perchapi.projectkestrel.org"
# Cloudflare 403s the default "Python-urllib" UA; identify ourselves instead.
UA = {"User-Agent": "kestrel-bioclip-eval/0.1 (+experiments/bioclip)"}


def get(url, retries=4):
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1 or getattr(e, "code", None) == 404:
                raise
            time.sleep(2 ** attempt)
            print(f"  retry {url[:80]}: {e}", file=sys.stderr)


def fetch_slug(slug, out, exports=False):
    sdir = os.path.join(out, slug)
    os.makedirs(os.path.join(sdir, "crops"), exist_ok=True)
    if exports:
        os.makedirs(os.path.join(sdir, "exports"), exist_ok=True)
    data = json.loads(get(f"{API}/v1/public/perches/{slug}"))
    with open(os.path.join(sdir, "perch.json"), "w") as f:
        json.dump(data, f)

    rows, jobs = [], []
    for scene in data["scenes"]:
        for exp in scene["exports"]:
            crops = exp.get("crops") or []
            if exports:
                epath = os.path.join(sdir, "exports", f"{exp['assetId']}.jpg")
                if not os.path.exists(epath):
                    jobs.append((exp["fileUrl"], epath))
            for ci, crop in enumerate(crops):
                path = os.path.join(sdir, "crops", f"{crop['assetId']}.jpg")
                rows.append({
                    "slug": slug,
                    "scene_id": scene["sceneId"],
                    "scene_sort": scene["sortIndex"],
                    "scene_species": json.dumps(scene.get("speciesList") or []),
                    "scene_families": json.dumps(scene.get("familyList") or []),
                    "finalized": int(bool(scene.get("userTagsFinalized"))),
                    "export_id": exp["assetId"],
                    "export_filename": exp["filename"],
                    "capture_ms": exp.get("captureTimeMs"),
                    "crop_id": crop["assetId"],
                    "crop_filename": crop["filename"],
                    "crop_rank": ci,
                    "n_crops_in_export": len(crops),
                    "quality": crop.get("quality"),
                    "bbox": json.dumps(crop.get("bbox")),
                    "path": os.path.relpath(path, out),
                })
                if not os.path.exists(path):
                    jobs.append((crop["fileUrl"], path))

    def dl(job):
        url, path = job
        try:
            data = get(url)
        except urllib.error.HTTPError as e:  # e.g. an asset whose bytes are gone from R2
            print(f"  skip {os.path.basename(path)}: HTTP {e.code}", file=sys.stderr)
            return
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)

    print(f"{slug}: {len(rows)} crops, {len(jobs)} to download")
    with ThreadPoolExecutor(8) as ex:
        for i, _ in enumerate(ex.map(dl, jobs)):
            if i % 200 == 0:
                print(f"  {slug}: {i}/{len(jobs)}")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--exports", action="store_true", help="also download each export (full frame)")
    ap.add_argument("slugs", nargs="+")
    a = ap.parse_args()
    rows = []
    for s in a.slugs:
        rows += fetch_slug(s, a.out, a.exports)
    with open(os.path.join(a.out, "crops_index.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows")


if __name__ == "__main__":
    main()
