"""Folder-level smart search on SigLIP 2 (pipeline 3.0.0).

The model maths lives in ``ml/siglip.py`` and the query parsing in
``query_parser.py``; this module joins them with the folder's ``.kestrel/``:

* ``read_folder_rows`` -- per image: scene id, subject crops, species/family
  tags (from ``crops_json`` + the row), effective quality.
* ``search_folder`` -- parse the query into species/family filters + a
  free-text remainder, score the remainder against the folder's SigLIP crop
  AND whole-frame embeddings, and rank scenes.

Scoring, per query ``q`` with ``T = SigLIP text embedding of "a photo of {q}."``:

    crop score   = cos(crop, T)              (no baseline subtraction)
    s_crop(img)  = max over the image's subject crops
    s_frame(img) = cos(frame, T)
    image score  = max(z(s_frame), z(s_crop))   z over the folder's images
    scene score  = max over its images (the UI aggregates over its own,
                   possibly user-edited, scene grouping)

Whole-frame embeddings win for context queries ("bird on a wire", "two birds
fighting", "wide landscape"), crops for bird-intrinsic ones; the per-query z
puts both on one scale (an image missing one of the two just uses the other).

Filters come from ``query_parser``: matched species / families / wildlife
are a hard filter on the folder's stored tags (OR across chips). A scene's
user-edited tags in ``kestrel_scenedata.json`` win: a finalized scene is
judged by its ``user_tags`` only; a non-finalized scene also passes when its
``user_tags`` match. With nothing left for SigLIP the result is filter-only,
sorted by quality.

Callers (the ``Api`` bridge) validate ``kestrel_dir`` against the root jail;
``resolve_crop_file`` additionally refuses crop paths outside
``<kestrel_dir>/crop``.
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
from collections import OrderedDict
from typing import Dict, Iterable, List, Optional, Set, Tuple

import numpy as np

from .ml import siglip
from . import query_parser

DB_FILENAME = "kestrel_database.csv"
SCENEDATA_FILENAME = "kestrel_scenedata.json"
DEFAULT_LIMIT = 48
MAX_LIMIT = 500

_PLACEHOLDER = frozenset({"", "unknown", "n/a", "no bird", "error", "none", "nan"})


def subject_crops(crops: Iterable[dict]) -> List[dict]:
    """The crops smart search indexes: real detections, not the whole-frame fallback."""
    out = []
    for c in crops or []:
        try:
            if int(c.get("detection_index", -1)) >= 0:
                out.append(c)
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def _meaningful(label) -> bool:
    return str(label or "").strip().lower() not in _PLACEHOLDER


def _json_list(raw) -> list:
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw) if raw else []
        return v if isinstance(v, list) else []
    except (TypeError, ValueError):
        return []


class FolderRows:
    __slots__ = ("scene_of", "crops", "species", "families", "quality", "analysed")

    def __init__(self):
        self.scene_of: Dict[str, str] = {}
        self.crops: Dict[str, List[dict]] = {}     # filename -> subject crops (crops_json entries)
        self.species: Dict[str, Set[str]] = {}     # filename -> species / wildlife tags
        self.families: Dict[str, Set[str]] = {}    # filename -> family tags
        self.quality: Dict[str, float] = {}
        self.analysed: Set[str] = set()            # rows that are not errors

    @property
    def indexable(self) -> set:
        """Images with at least one subject crop (crop-embedding coverage)."""
        return {fn for fn, c in self.crops.items() if c}


def read_folder_rows(kestrel_dir: str) -> FolderRows:
    """Parse the folder database. Missing / unreadable database -> empty rows."""
    out = FolderRows()
    path = os.path.join(kestrel_dir, DB_FILENAME)
    if not os.path.isfile(path):
        return out
    try:
        from .database import read_database_text
        text = read_database_text(path)
    except Exception:
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            return out
    old_limit = csv.field_size_limit()
    try:
        csv.field_size_limit(max(old_limit, 64 * 1024 * 1024))
        for row in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
            fn = str(row.get("filename") or "").strip()
            if not fn:
                continue
            sc = str(row.get("scene_count") or "").strip()
            out.scene_of[fn] = sc or fn
            if str(row.get("species") or "") != "Error":
                out.analysed.add(fn)
            try:
                out.quality[fn] = float(row.get("quality") or -1)
            except (TypeError, ValueError):
                out.quality[fn] = -1.0
            parsed = [c for c in _json_list(row.get("crops_json")) if isinstance(c, dict)]
            subs = subject_crops(parsed)
            out.crops[fn] = subs
            sp = {str(c.get("species")) for c in subs if _meaningful(c.get("species"))}
            fa = {str(c.get("family")) for c in subs if _meaningful(c.get("family"))}
            if _meaningful(row.get("species")):
                sp.add(str(row.get("species")))
            if _meaningful(row.get("family")):
                fa.add(str(row.get("family")))
            sp.update(str(s) for s in _json_list(row.get("secondary_species_list")) if _meaningful(s))
            fa.update(str(s) for s in _json_list(row.get("secondary_family_list")) if _meaningful(s))
            out.species[fn] = sp
            out.families[fn] = fa
    except Exception:
        pass
    finally:
        csv.field_size_limit(old_limit)
    return out


class SceneTags:
    """Scene membership + user-edited tags from ``kestrel_scenedata.json``."""

    __slots__ = ("scene_of", "user_species", "user_families", "finalized")

    def __init__(self):
        self.scene_of: Dict[str, str] = {}
        self.user_species: Dict[str, Set[str]] = {}
        self.user_families: Dict[str, Set[str]] = {}
        self.finalized: Dict[str, bool] = {}


def read_scene_tags(kestrel_dir: str) -> SceneTags:
    out = SceneTags()
    try:
        with open(os.path.join(kestrel_dir, SCENEDATA_FILENAME), encoding="utf-8") as f:
            data = json.load(f)
        scenes = data.get("scenes") if isinstance(data, dict) else None
        if not isinstance(scenes, dict):
            return out
        for sid, sc in scenes.items():
            if not isinstance(sc, dict):
                continue
            sid = str(sid)
            for fn in sc.get("image_filenames") or []:
                out.scene_of[str(fn)] = sid
            tags = sc.get("user_tags") if isinstance(sc.get("user_tags"), dict) else {}
            out.user_species[sid] = {str(s) for s in tags.get("species") or [] if _meaningful(s)}
            out.user_families[sid] = {str(s) for s in tags.get("families") or [] if _meaningful(s)}
            out.finalized[sid] = bool(tags.get("finalized"))
    except Exception:
        pass
    return out


def folder_regions(kestrel_dir: str) -> List[str]:
    try:
        with open(os.path.join(kestrel_dir, "kestrel_metadata.json"), encoding="utf-8") as f:
            meta = json.load(f)
        regs = meta.get("species_regions") or (meta.get("analysis_settings") or {}).get("species_regions")
        return [str(r) for r in regs] if isinstance(regs, list) else []
    except Exception:
        return []


# ---------------------------------------------------------------------- cache

_cache_lock = threading.Lock()
_index_cache: "OrderedDict[str, tuple]" = OrderedDict()
_INDEX_CACHE_SIZE = 4


def _stat_sig(path: str) -> Tuple[float, int]:
    try:
        st = os.stat(path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return (0.0, -1)


class FolderIndex:
    __slots__ = ("crop_keys", "crop_mat", "frame_keys", "frame_mat", "rows", "scenes", "regions")


def load_folder_index(kestrel_dir: str) -> FolderIndex:
    """Embeddings + rows + scene tags for the folder, cached on file signatures."""
    sig = tuple(
        _stat_sig(p) for p in (
            os.path.join(siglip.store_dir(kestrel_dir, siglip.MODEL_ID), "index.jsonl"),
            os.path.join(siglip.store_dir(kestrel_dir, siglip.FRAME_SLOT), "index.jsonl"),
            os.path.join(kestrel_dir, DB_FILENAME),
            os.path.join(kestrel_dir, SCENEDATA_FILENAME),
            os.path.join(kestrel_dir, "kestrel_metadata.json"),
        )
    )
    key = os.path.normcase(os.path.realpath(kestrel_dir))
    with _cache_lock:
        hit = _index_cache.get(key)
        if hit is not None and hit[0] == sig:
            _index_cache.move_to_end(key)
            return hit[1]
    fi = FolderIndex()
    fi.crop_keys, fi.crop_mat, _ = siglip.load_store(kestrel_dir, siglip.MODEL_ID)
    fi.frame_keys, fi.frame_mat, _ = siglip.load_store(kestrel_dir, siglip.FRAME_SLOT)
    fi.rows = read_folder_rows(kestrel_dir)
    fi.scenes = read_scene_tags(kestrel_dir)
    fi.regions = folder_regions(kestrel_dir)
    with _cache_lock:
        _index_cache[key] = (sig, fi)
        _index_cache.move_to_end(key)
        while len(_index_cache) > _INDEX_CACHE_SIZE:
            _index_cache.popitem(last=False)
    return fi


def coverage(kestrel_dir: str, fi: Optional[FolderIndex] = None) -> dict:
    """SigLIP coverage: crops (images with subjects) and whole frames (analysed images)."""
    from . import embedding_store
    rows = fi.rows if fi is not None else read_folder_rows(kestrel_dir)
    indexable = rows.indexable
    crop_live = embedding_store.live_filenames(kestrel_dir, slot=siglip.MODEL_ID, model=siglip.MODEL_ID)
    frame_live = embedding_store.live_filenames(kestrel_dir, slot=siglip.FRAME_SLOT, model=siglip.MODEL_ID)
    return {
        "indexed_images": len(indexable & crop_live),
        "indexable_images": len(indexable),
        "frame_indexed_images": len(rows.analysed & frame_live),
        "frame_indexable_images": len(rows.analysed),
    }


# ---------------------------------------------------------------------- search


def _zscore(values: Dict[str, float]) -> Dict[str, float]:
    if not values:
        return {}
    arr = np.fromiter(values.values(), dtype=np.float64, count=len(values))
    if arr.size < 2:
        return {k: 0.0 for k in values}
    mu, sd = float(arr.mean()), float(arr.std())
    sd = max(sd, 1e-6)
    return {k: (v - mu) / sd for k, v in values.items()}


# Display mapping for the UI's "NN% match". The ranking score is a per-query z-score over the
# folder's images (see the module docstring), not a probability and not comparable across
# queries or folders, so we do not pretend it is one. We rescale it linearly and clamp:
# z <= Z_FLOOR (about as similar as an average photo of the folder) -> 0%, z >= Z_CEIL
# (a strong outlier; top hits of a real match typically land around 3-5) -> 100%. A linear
# map keeps the percentages in the same order as the ranking and stable as results are
# added or removed, unlike a softmax over the current results, which would change every
# card's value whenever the result set changes. It reads as "how far above this folder's
# typical photo", not as a calibrated chance that the photo is right.
MATCH_Z_FLOOR = 0.0
MATCH_Z_CEIL = 4.0


def match_percent(z: float) -> int:
    """Map an image z-score to a 0-100 integer for display (see ``MATCH_Z_FLOOR``)."""
    try:
        z = float(z)
    except (TypeError, ValueError):
        return 0
    if z != z:  # NaN
        return 0
    frac = (z - MATCH_Z_FLOOR) / (MATCH_Z_CEIL - MATCH_Z_FLOOR)
    return int(round(100.0 * min(1.0, max(0.0, frac))))


def merge_image_scores(crop_best: Dict[str, float], frame: Dict[str, float]) -> Dict[str, float]:
    """``max(z(frame), z(best crop))`` per image; z over the folder's images per source."""
    zc, zf = _zscore(crop_best), _zscore(frame)
    out: Dict[str, float] = {}
    for fn in set(zc) | set(zf):
        out[fn] = max(v for v in (zc.get(fn), zf.get(fn)) if v is not None)
    return out


def _matches_tags(match: dict, species: Set[str], families: Set[str]) -> bool:
    norm_sp = {query_parser.normalize(s) for s in species}
    norm_fa = {query_parser.normalize(f) for f in families}
    labels = [query_parser.normalize(x) for x in (match.get("labels") or [match.get("label", "")])]
    if match.get("kind") == "family":
        return any(l in norm_fa for l in labels)
    return any(l in norm_sp for l in labels)


def _filter_images(fi: FolderIndex, chips: List[dict]) -> Tuple[Set[str], Dict[int, int]]:
    """Images passing ANY chip (OR), honouring user-edited scene tags. Also per-chip hit counts."""
    rows, scenes = fi.rows, fi.scenes
    passing: Set[str] = set()
    hits = {i: 0 for i in range(len(chips))}
    by_scene: Dict[str, List[str]] = {}
    for fn in rows.analysed:
        sid = scenes.scene_of.get(fn, rows.scene_of.get(fn, fn))
        by_scene.setdefault(sid, []).append(fn)
    for sid, files in by_scene.items():
        u_sp, u_fa = scenes.user_species.get(sid, set()), scenes.user_families.get(sid, set())
        finalized = scenes.finalized.get(sid, False)
        for ci, chip in enumerate(chips):
            scene_hit = bool(u_sp or u_fa) and _matches_tags(chip, u_sp, u_fa)
            if finalized:
                matched = files if scene_hit else []
            elif scene_hit:
                matched = files
            else:
                matched = [fn for fn in files
                           if _matches_tags(chip, rows.species.get(fn, set()), rows.families.get(fn, set()))]
            if matched:
                hits[ci] += len(matched)
                passing.update(matched)
    return passing, hits


def folder_tags(fi: FolderIndex) -> Tuple[Set[str], Set[str]]:
    sp: Set[str] = set()
    fa: Set[str] = set()
    for fn in fi.rows.analysed:
        sp |= fi.rows.species.get(fn, set())
        fa |= fi.rows.families.get(fn, set())
    for s in fi.scenes.user_species.values():
        sp |= s
    for f in fi.scenes.user_families.values():
        fa |= f
    return sp, fa


def search_folder(kestrel_dir: str, query: str, limit: int = DEFAULT_LIMIT, encoder=None,
                  disabled_terms: Iterable[str] = (), index: Optional[query_parser.QueryIndex] = None) -> dict:
    """Rank the folder's scenes for ``query``.

    Returns ``{query, semantic_query, chips, mode, scenes, images,
    indexed_images, indexable_images, frame_indexed_images,
    frame_indexable_images, needs_index}``. ``chips`` are the species /
    family filters detected in the query (``{kind, label, text, labels,
    scientific, fuzzy, hits}``); ``mode`` is ``semantic`` / ``filter`` /
    ``semantic+filter`` / ``empty``. ``scenes`` are the top ``limit`` scenes
    (best first, keyed by the scenedata scene id when present); ``images``
    every scored image of those scenes. ``needs_index`` is true when some
    analysed images have no SigLIP embedding yet (offer the v3 upgrade).
    """
    q = siglip.normalize_query(query)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(MAX_LIMIT, limit))
    fi = load_folder_index(kestrel_dir)
    cov = coverage(kestrel_dir, fi)
    result = {
        "query": q,
        "semantic_query": "",
        "chips": [],
        "mode": "empty",
        "scenes": [],
        "images": [],
        "needs_index": (cov["indexed_images"] < cov["indexable_images"]
                        or cov["frame_indexed_images"] < cov["frame_indexable_images"]),
        **cov,
    }
    if not q:
        return result
    sp, fa = folder_tags(fi)
    parsed = query_parser.parse_query(q, index=index, folder_species=sp, folder_families=fa,
                                      regions=fi.regions, disabled_terms=disabled_terms)
    chips = [m.to_dict() for m in parsed.matches]
    sem = query_parser.semantic_text(parsed)
    result["semantic_query"] = sem
    candidates: Optional[Set[str]] = None
    if chips:
        candidates, hits = _filter_images(fi, chips)
        for i, c in enumerate(chips):
            c["hits"] = int(hits.get(i, 0))
    result["chips"] = chips

    scores: Dict[str, float] = {}
    best_crop: Dict[str, int] = {}
    if sem and (fi.crop_mat.shape[0] or fi.frame_mat.shape[0]):
        enc = encoder or siglip.shared_text_encoder()
        t = np.asarray(enc.encode(sem), dtype=np.float32)
        crop_best: Dict[str, float] = {}
        if fi.crop_mat.shape[0]:
            cs = fi.crop_mat @ t
            for (fn, ci), s in zip(fi.crop_keys, cs.tolist()):
                if fn not in fi.rows.scene_of:
                    continue
                if fn not in crop_best or s > crop_best[fn]:
                    crop_best[fn], best_crop[fn] = float(s), int(ci)
        frame: Dict[str, float] = {}
        if fi.frame_mat.shape[0]:
            fs = fi.frame_mat @ t
            for (fn, _ci), s in zip(fi.frame_keys, fs.tolist()):
                if fn in fi.rows.scene_of:
                    frame[fn] = float(s)
        scores = merge_image_scores(crop_best, frame)
        result["mode"] = "semantic+filter" if chips else "semantic"
    elif chips:
        scores = {fn: fi.rows.quality.get(fn, -1.0) for fn in (candidates or ())}
        result["mode"] = "filter"
    if candidates is not None:
        scores = {fn: s for fn, s in scores.items() if fn in candidates}
    if not scores:
        return result

    scene_of = {fn: fi.scenes.scene_of.get(fn, fi.rows.scene_of.get(fn, fn)) for fn in scores}
    best_scene: Dict[str, Tuple[float, str]] = {}
    for fn, s in scores.items():
        sid = scene_of[fn]
        cur = best_scene.get(sid)
        if cur is None or s > cur[0] or (s == cur[0] and fn < cur[1]):
            best_scene[sid] = (s, fn)
    ranked = sorted(best_scene.items(), key=lambda kv: (-kv[1][0], kv[1][1]))[:limit]
    top = {sid for sid, _ in ranked}
    # ``percent`` only for SigLIP scores (z-scores); filter-only scores are quality values.
    def pct(s: float) -> Optional[int]:
        return match_percent(s) if result["mode"].startswith("semantic") else None

    result["scenes"] = [
        {"scene_id": sid, "score": round(s, 5), "percent": pct(s), "best_filename": fn,
         "best_crop": best_crop.get(fn, 0)}
        for sid, (s, fn) in ranked
    ]
    result["images"] = sorted(
        ({"filename": fn, "scene_id": scene_of[fn], "score": round(s, 5), "percent": pct(s),
          "crop_index": best_crop.get(fn, 0)}
         for fn, s in scores.items() if scene_of[fn] in top),
        key=lambda r: (-r["score"], r["filename"]),
    )
    return result


# ---------------------------------------------------------------------- crop files


def resolve_crop_file(kestrel_dir: str, filename: str, crop: dict) -> Optional[str]:
    """The crop JPEG for one ``crops_json`` entry, jailed to ``<kestrel_dir>/crop``.

    ``crop_path`` is stored relative to the photo folder (with the writer's
    path separator, so Windows-written folders use backslashes); when absent
    or unusable fall back to the pipeline's naming ``<base>_crop_<i>.jpg``.
    """
    crop_root = os.path.realpath(os.path.join(kestrel_dir, "crop"))
    root = os.path.dirname(os.path.realpath(kestrel_dir))
    candidates = []
    rel = str(crop.get("crop_path") or "").replace("\\", "/").strip()
    if rel and not os.path.isabs(rel) and not rel.startswith("/"):
        candidates.append(os.path.join(root, *[p for p in rel.split("/") if p]))
    try:
        idx = int(crop.get("crop_index"))
        base = os.path.splitext(os.path.basename(str(filename)))[0]
        candidates.append(os.path.join(crop_root, f"{base}_crop_{idx}.jpg"))
    except (TypeError, ValueError):
        pass
    for cand in candidates:
        real = os.path.realpath(cand)
        try:
            if os.path.commonpath([real, crop_root]) != crop_root:
                continue
        except ValueError:
            continue
        if os.path.isfile(real):
            return real
    return None


def resolve_export_file(kestrel_dir: str, filename: str, export_path: str = "") -> Optional[str]:
    """The export (downscaled whole-frame) JPEG for an image, jailed to ``<kestrel_dir>/export``."""
    exp_root = os.path.realpath(os.path.join(kestrel_dir, "export"))
    root = os.path.dirname(os.path.realpath(kestrel_dir))
    candidates = []
    rel = str(export_path or "").replace("\\", "/").strip()
    if rel and rel != "N/A" and not os.path.isabs(rel) and not rel.startswith("/"):
        candidates.append(os.path.join(root, *[p for p in rel.split("/") if p]))
    base = os.path.splitext(os.path.basename(str(filename)))[0]
    candidates.append(os.path.join(exp_root, f"{base}_export.jpg"))
    for cand in candidates:
        real = os.path.realpath(cand)
        try:
            if os.path.commonpath([real, exp_root]) != exp_root:
                continue
        except ValueError:
            continue
        if os.path.isfile(real):
            return real
    return None


def load_rgb(path: str) -> np.ndarray:
    from PIL import Image
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"))
