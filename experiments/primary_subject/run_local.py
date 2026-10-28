"""Run the bird-linking tracker on an analysed folder (local `.kestrel/` data only).

    python experiments/primary_subject/run_local.py <photo folder> [--scene N]
        [--seed FILE:CROP] [--sheets OUT_DIR] [--min-birds 2]

* Without --seed: prints, per multi-bird scene, how each bird in the first frame is
  linked through the scene (one row per bird, one column per frame; '.' = not found,
  '?' marks a low-confidence link).
* With --seed FILE:CROP (crop index as in crops_json, 0-based): follows that bird
  through its scene and prints today's frame order vs the order by that bird's quality.
* --sheets OUT_DIR: writes one JPEG per scene with every frame's export image and boxes
  coloured/numbered by track, so the same number = the same bird. That is the quickest
  way to eyeball whether the linking is right.

Needs a folder analysed with pipeline 3.0 (or upgraded) so `.kestrel/embeddings/` has the
BioCLIP and SigLIP crop embeddings. Nothing is written into the photo folder.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "..", "analyzer"))

import tracker as T  # noqa: E402
from kestrel_analyzer import embedding_store  # noqa: E402

SIGLIP_SLOT = "siglip2-b16"   # kestrel_analyzer.ml.siglip.MODEL_ID; BioCLIP is the primary slot
MIN_MARGIN = 1.0

csv.field_size_limit(10 ** 9)


def load_folder(folder: str):
    kdir = os.path.join(folder, ".kestrel")
    db = os.path.join(kdir, "kestrel_database.csv")
    if not os.path.isfile(db):
        sys.exit(f"no {db}")
    emb = {}
    for slot, key in ((None, "b"), (SIGLIP_SLOT, "s")):
        keys, mat, _ = embedding_store.load_embeddings(kdir, slot=slot)
        for (fn, ci), row in zip(keys, mat):
            emb.setdefault((fn, int(ci)), {})[key] = row.astype("float32")
    if not emb:
        print("warning: no embeddings found; linking will use position only", file=sys.stderr)
    scenes = defaultdict(list)
    with open(db, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                crops = json.loads(r.get("crops_json") or "[]")
            except ValueError:
                crops = []
            frame = []
            for ci, c in enumerate(crops):
                bb = c.get("bbox") or {}
                if "x_min_norm" not in bb:
                    continue
                e = emb.get((r["filename"], ci), {})
                frame.append(T.make_crop(bb, c.get("quality", -1.0), e.get("b"), e.get("s"), crop_index=ci))
            scenes[r.get("scene_count", "")].append(dict(
                filename=r["filename"], t=r.get("capture_time") or "", crops=frame,
                export=r.get("export_path") or "", quality=_f(r.get("quality"))))
    for sid in scenes:
        scenes[sid].sort(key=lambda fr: (fr["t"], fr["filename"]))
    return scenes


def _f(x, d=-1.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def print_tracks(sid, frames):
    tracks, margins = T.track_scene([fr["crops"] for fr in frames])
    print(f"\nscene {sid}: {len(frames)} frames, birds per frame {[len(fr['crops']) for fr in frames]}")
    print("  bird | " + " ".join(f"{i:>4d}" for i in range(len(frames))))
    for k, (tr, mg) in enumerate(zip(tracks, margins)):
        cells = []
        for f in range(len(frames)):
            if f not in tr:
                cells.append("   .")
            else:
                cells.append(f"{tr[f]:>3d}{'?' if mg.get(f, 9) < MIN_MARGIN else ' '}")
        print(f"  {k:>4d} | " + " ".join(cells))
    return tracks


def print_seed(sid, frames, seed_file, seed_crop):
    fi = next((i for i, fr in enumerate(frames) if fr["filename"] == seed_file), None)
    if fi is None:
        sys.exit(f"{seed_file} not in scene {sid}")
    m = T.follow_subject([fr["crops"] for fr in frames], fi, seed_crop)
    print(f"\nsubject = crop {seed_crop} of {seed_file} (scene {sid})")
    print(f"  {'frame':<28} {'crop':>5} {'margin':>7} {'subject q':>10} {'frame q today':>14}")
    for i, fr in enumerate(frames):
        j, mg = m.get(i, (None, 0))
        q = fr["crops"][j]["quality"] if j is not None else float("nan")
        print(f"  {fr['filename']:<28} {str(j):>5} {mg:7.2f} {q:10.3f} {fr['quality']:14.3f}")
    today = sorted(range(len(frames)), key=lambda i: -frames[i]["quality"])
    subj = T.rank_by_subject([fr["crops"] for fr in frames], m, MIN_MARGIN)
    print("  order today      :", [frames[i]["filename"] for i in today])
    print("  order by subject :", [frames[i]["filename"] for i in subj])


def write_sheet(folder, sid, frames, tracks, out_dir):
    from PIL import Image, ImageDraw, ImageFont
    try:
        font = ImageFont.load_default(size=22)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    colours = [(255, 80, 80), (80, 200, 255), (255, 220, 0), (120, 255, 120), (255, 120, 255),
               (255, 160, 40), (200, 200, 255), (255, 255, 255), (0, 200, 160), (200, 120, 60)]
    owner = {}
    for k, tr in enumerate(tracks):
        for f, j in tr.items():
            owner[(f, j)] = k
    tiles = []
    for f, fr in enumerate(frames):
        p = fr["export"]
        p = p if os.path.isabs(p) else os.path.join(folder, p)
        if not os.path.isfile(p):
            p2 = os.path.join(folder, ".kestrel", fr["export"])
            p = p2 if os.path.isfile(p2) else None
        im = Image.open(p).convert("RGB") if p else Image.new("RGB", (900, 600), (40, 40, 40))
        im.thumbnail((900, 900))
        d = ImageDraw.Draw(im); w, h = im.size
        for j, c in enumerate(fr["crops"]):
            k = owner.get((f, j))
            col = colours[k % len(colours)] if k is not None else (128, 128, 128)
            box = [(c["cx"] - c["w"] / 2) * w, (c["cy"] - c["h"] / 2) * h, (c["cx"] + c["w"] / 2) * w, (c["cy"] + c["h"] / 2) * h]
            d.rectangle(box, outline=col, width=3)
            label = "new" if k is None else str(k)
            ty = max(0, box[1] - 26)
            d.rectangle([box[0], ty, box[0] + 14 * len(label) + 8, ty + 24], fill=(0, 0, 0))
            d.text((box[0] + 4, ty), label, fill=col, font=font)
        d.text((6, 4), fr["filename"], fill=(255, 255, 0), font=font)
        tiles.append(im)
    cols = min(4, len(tiles)); rows = (len(tiles) + cols - 1) // cols
    W = max(t.width for t in tiles); H = max(t.height for t in tiles)
    sheet = Image.new("RGB", (cols * W, rows * H), (20, 20, 20))
    for i, t in enumerate(tiles):
        sheet.paste(t, ((i % cols) * W, (i // cols) * H))
    os.makedirs(out_dir, exist_ok=True)
    sheet.save(os.path.join(out_dir, f"scene_{sid}.jpg"), quality=80)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder")
    ap.add_argument("--scene")
    ap.add_argument("--seed", help="FILE:CROP_INDEX")
    ap.add_argument("--sheets")
    ap.add_argument("--min-birds", type=int, default=2)
    a = ap.parse_args()
    scenes = load_folder(a.folder)
    if a.seed:
        fn, ci = a.seed.rsplit(":", 1)
        sid = next((s for s, frs in scenes.items() if any(fr["filename"] == fn for fr in frs)), None)
        if sid is None:
            sys.exit(f"{fn} not found")
        print_seed(sid, scenes[sid], fn, int(ci))
        return
    for sid, frames in sorted(scenes.items(), key=lambda kv: kv[0]):
        if a.scene and sid != a.scene:
            continue
        if len(frames) < 2 or max(len(fr["crops"]) for fr in frames) < a.min_birds:
            continue
        tracks = print_tracks(sid, frames)
        if a.sheets:
            write_sheet(a.folder, sid, frames, tracks, a.sheets)


if __name__ == "__main__":
    main()
