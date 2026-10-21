"""Folder-level smart search (experimental): query a folder's CLIP crop index, or build it.

The model maths lives in ``ml/clip_search.py``; this module joins it with the
folder's ``.kestrel/`` data:

* ``read_folder_rows`` -- filename -> scene id + subject crops, from
  ``kestrel_database.csv``.
* ``search_folder`` -- rank the folder's scenes for a free-text query.
* ``index_folder`` -- backfill: embed the crop JPEGs that analysis already
  wrote under ``.kestrel/crop/`` (folders analysed before smart search
  existed, or with it switched off), so search works without re-analysis.

Callers (the ``Api`` bridge) are responsible for validating ``root`` /
``kestrel_dir`` against the root jail; ``index_folder`` additionally refuses
any crop path that resolves outside ``<kestrel_dir>/crop``.
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
from collections import OrderedDict
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from .ml import clip_search

DB_FILENAME = "kestrel_database.csv"
DEFAULT_LIMIT = 48
MAX_LIMIT = 500


class FolderRows:
    __slots__ = ("scene_of", "crops")

    def __init__(self):
        self.scene_of: Dict[str, str] = {}
        self.crops: Dict[str, List[dict]] = {}  # filename -> subject crops (crops_json entries)

    @property
    def indexable(self) -> set:
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
        for row in csv.DictReader(io.StringIO(text)):
            fn = str(row.get("filename") or "").strip()
            if not fn:
                continue
            sc = str(row.get("scene_count") or "").strip()
            out.scene_of[fn] = sc or fn
            crops: List[dict] = []
            raw = row.get("crops_json") or ""
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        crops = clip_search.subject_crops(c for c in parsed if isinstance(c, dict))
                except ValueError:
                    crops = []
            out.crops[fn] = crops
    except Exception:
        pass
    finally:
        csv.field_size_limit(old_limit)
    return out


# ---------------------------------------------------------------------- search

_cache_lock = threading.Lock()
_index_cache: "OrderedDict[str, tuple]" = OrderedDict()
_INDEX_CACHE_SIZE = 4


def _stat_sig(path: str) -> Tuple[float, int]:
    try:
        st = os.stat(path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return (0.0, -1)


def _load_cached(kestrel_dir: str):
    """``(keys, matrix, rows)`` for the folder, cached on index + database mtimes."""
    store_index = os.path.join(clip_search.store_dir(kestrel_dir), "index.jsonl")
    sig = (_stat_sig(store_index), _stat_sig(os.path.join(kestrel_dir, DB_FILENAME)))
    key = os.path.normcase(os.path.realpath(kestrel_dir))
    with _cache_lock:
        hit = _index_cache.get(key)
        if hit is not None and hit[0] == sig:
            _index_cache.move_to_end(key)
            return hit[1], hit[2], hit[3]
    keys, mat, _ = clip_search.load_store(kestrel_dir)
    rows = read_folder_rows(kestrel_dir)
    with _cache_lock:
        _index_cache[key] = (sig, keys, mat, rows)
        _index_cache.move_to_end(key)
        while len(_index_cache) > _INDEX_CACHE_SIZE:
            _index_cache.popitem(last=False)
    return keys, mat, rows


def coverage(kestrel_dir: str) -> dict:
    """How much of the folder is indexed: ``{indexed_images, indexable_images}``."""
    rows = read_folder_rows(kestrel_dir)
    indexable = rows.indexable
    from . import embedding_store
    live = embedding_store.live_filenames(kestrel_dir, slot=clip_search.MODEL_ID, model=clip_search.MODEL_ID)
    return {"indexed_images": len(indexable & live), "indexable_images": len(indexable)}


def search_folder(kestrel_dir: str, query: str, limit: int = DEFAULT_LIMIT, encoder=None) -> dict:
    """Rank the folder's scenes for ``query``.

    Returns ``{query, scenes, images, indexed_images, indexable_images,
    needs_index}``. ``scenes`` are the top ``limit`` scenes (best first) with
    their best image; ``images`` carries every indexed image of those scenes
    with its score (the UI aggregates over its own, possibly edited, scene
    grouping). ``needs_index`` is true when some analysed images have no CLIP
    embedding yet (offer the backfill).
    """
    q = clip_search.normalize_query(query)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DEFAULT_LIMIT
    limit = max(1, min(MAX_LIMIT, limit))
    keys, mat, rows = _load_cached(kestrel_dir)
    indexable = rows.indexable
    indexed = {fn for fn, _ in keys} & indexable
    result = {
        "query": q,
        "scenes": [],
        "images": [],
        "indexed_images": len(indexed),
        "indexable_images": len(indexable),
        "needs_index": len(indexed) < len(indexable),
    }
    if not q or mat.shape[0] == 0:
        return result
    enc = encoder or clip_search.shared_text_encoder()
    scores = clip_search.crop_scores(mat, enc.encode(q), enc.baseline())
    ranked = clip_search.rank(keys, scores, rows.scene_of, limit=limit)
    top = {s["scene_id"] for s in ranked["scenes"]}
    best_img: Dict[str, Tuple[float, int]] = {}
    for (fn, crop), s in zip(keys, scores.tolist()):
        if rows.scene_of.get(fn) in top:
            cur = best_img.get(fn)
            if cur is None or s > cur[0]:
                best_img[fn] = (float(s), int(crop))
    result["scenes"] = [{**s, "score": round(s["score"], 5)} for s in ranked["scenes"]]
    result["images"] = sorted(
        ({"filename": fn, "scene_id": rows.scene_of[fn], "score": round(s, 5), "crop_index": c}
         for fn, (s, c) in best_img.items()),
        key=lambda r: (-r["score"], r["filename"]),
    )
    return result


# ---------------------------------------------------------------------- backfill


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


def _load_rgb(path: str) -> np.ndarray:
    from PIL import Image
    with Image.open(path) as im:
        return np.asarray(im.convert("RGB"))


def index_folder(
    kestrel_dir: str,
    embedder,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    log_path: Optional[str] = None,
    only_missing: bool = True,
) -> dict:
    """Embed the folder's existing subject crops into the smart-search slot.

    ``progress_cb(done, total, filename)`` is called after each image.
    Returns ``{indexed, skipped, failed, total, canceled, stopped}``.
    """
    rows = read_folder_rows(kestrel_dir)
    store = clip_search.open_store(kestrel_dir, log_path=log_path)
    todo = sorted(fn for fn in rows.indexable if not (only_missing and store.has(fn)))
    total = len(todo)
    stats = {"indexed": 0, "skipped": 0, "failed": 0, "total": total, "canceled": False, "stopped": False}
    if progress_cb:
        progress_cb(0, total, "")
    for i, fn in enumerate(todo):
        if cancel_event is not None and cancel_event.is_set():
            stats["canceled"] = True
            break
        if should_stop is not None and should_stop():
            stats["stopped"] = True
            break
        vectors = {}
        for crop in rows.crops.get(fn, []):
            try:
                ci = int(crop.get("crop_index"))
            except (TypeError, ValueError):
                continue
            path = resolve_crop_file(kestrel_dir, fn, crop)
            if path is None:
                continue
            try:
                vectors[ci] = embedder.embed(_load_rgb(path))[0]
            except Exception:
                stats["failed"] += 1
        if vectors:
            store.put(fn, vectors)
            stats["indexed"] += 1
        else:
            stats["skipped"] += 1
        if progress_cb:
            progress_cb(i + 1, total, fn)
    if not stats["canceled"] and not stats["stopped"]:
        store.compact(list(rows.scene_of))
    return stats
