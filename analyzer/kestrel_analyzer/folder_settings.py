"""Per-folder settings that can change after analysis, without re-analysing.

* ``kestrel_folder_settings.json`` -- small display/filter preferences that live
  next to the analysis output (today: ``ignore_wildlife``). Separate from
  ``kestrel_scenedata.json`` and the CSV so the schema shared with the hosted
  add-ons is untouched.
* ``folder_info`` -- read-only facts the Change Settings dialog shows.
* ``rescore_species_regions`` -- re-run the species/family head of BioCLIP over
  the *stored* crop embeddings with a different region geofence. No detection,
  no image decode, no ONNX image tower.

Every image's own prediction is re-scored, including images in scenes the user
edited or finalized. Scene-level user tags live in ``kestrel_scenedata.json``,
which this module never writes, so reviewed scene tags are never changed
(``reviewed_scene_images`` counts the images that sit in such scenes). Crops
routed to non-bird wildlife keep their label.
"""

from __future__ import annotations

import json
import os
from typing import Callable, Dict, List, Optional

import numpy as np

from .config import DATABASE_NAME, METADATA_FILENAME

SETTINGS_FILENAME = "kestrel_folder_settings.json"
DEFAULT_SETTINGS: Dict[str, object] = {"ignore_wildlife": False}
_BOOL_KEYS = frozenset(DEFAULT_SETTINGS)


# ----------------------------------------------------------------- settings file

def read_folder_settings(kestrel_dir: str) -> dict:
    """Stored settings merged over the defaults. Missing/corrupt file -> defaults."""
    out = dict(DEFAULT_SETTINGS)
    try:
        with open(os.path.join(kestrel_dir, SETTINGS_FILENAME), encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in _BOOL_KEYS:
                if k in data:
                    out[k] = bool(data[k])
    except Exception:
        pass
    return out


def write_folder_setting(kestrel_dir: str, key: str, value) -> dict:
    """Set one allow-listed boolean setting and return the full settings dict."""
    if key not in _BOOL_KEYS:
        raise ValueError(f"unknown folder setting: {key!r}")
    from .database import write_json_atomic
    cur = read_folder_settings(kestrel_dir)
    cur[key] = bool(value)
    os.makedirs(kestrel_dir, exist_ok=True)
    write_json_atomic(os.path.join(kestrel_dir, SETTINGS_FILENAME), cur, indent=2)
    return cur


# ----------------------------------------------------------------- folder info

def _read_meta(kestrel_dir: str) -> dict:
    try:
        with open(os.path.join(kestrel_dir, METADATA_FILENAME), encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _is_wildlife(crop: dict) -> bool:
    return str(crop.get("subject") or "").lower() == "wildlife"


def rescore_support(kestrel_dir: str) -> dict:
    """Whether species regions can be re-scored here: ``{supported, reason, crops}``.

    Needs the BioCLIP embeddings written by pipeline 3 (or its upgrade) and an
    analysis that actually ran species detection.
    """
    from . import embedding_store
    from .ml.bioclip_species import MODEL_ID as SPECIES_MODEL_ID
    out = {"supported": False, "reason": "", "crops": 0}
    if not os.path.isfile(os.path.join(kestrel_dir, DATABASE_NAME)):
        out["reason"] = "This folder has not been analyzed."
        return out
    settings = _read_meta(kestrel_dir).get("analysis_settings")
    if isinstance(settings, dict) and settings.get("species_detection_enabled") is False:
        out["reason"] = "Species detection was turned off when this folder was analyzed."
        return out
    keys, _mat, _hdr = embedding_store.load_embeddings(kestrel_dir, model=SPECIES_MODEL_ID)
    if not keys:
        out["reason"] = ("This folder has no stored species embeddings (it was analyzed before "
                         "pipeline 3.0). Upgrade it to 3.0 first.")
        return out
    out.update(supported=True, crops=len(keys))
    return out


def folder_info(kestrel_dir: str) -> dict:
    """Facts for the Change Settings dialog (counts that need culling state are added client-side)."""
    from . import smart_search
    meta = _read_meta(kestrel_dir)
    settings = meta.get("analysis_settings") if isinstance(meta.get("analysis_settings"), dict) else {}
    try:
        major = int(meta.get("pipeline_major") or 0)
    except (TypeError, ValueError):
        major = 0
    rows = smart_search.read_folder_rows(kestrel_dir)
    wildlife_crops = sum(1 for crops in rows.crops.values() for c in crops if _is_wildlife(c))
    regions = meta.get("species_regions") or settings.get("species_regions") or []
    support = rescore_support(kestrel_dir)
    return {
        "pipeline_version": str(meta.get("version") or ""),
        "pipeline_major": major,
        "analyzed_utc": str(meta.get("analyzed_utc") or ""),
        "image_count": len(rows.scene_of),
        "scene_count": len(set(rows.scene_of.values())),
        "species_regions": [str(r) for r in regions] if isinstance(regions, list) else [],
        "wildlife_enabled": bool(settings.get("wildlife_enabled", False)),
        "wildlife_crops": int(wildlife_crops),
        "rescore_supported": bool(support["supported"]),
        "rescore_reason": support["reason"],
        "settings": read_folder_settings(kestrel_dir),
    }


# ----------------------------------------------------------------- region re-scoring

def _json_list(raw) -> list:
    try:
        v = json.loads(raw) if isinstance(raw, str) and raw else (raw if isinstance(raw, list) else [])
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def rescore_species_regions(
    kestrel_dir: str,
    regions: List[str],
    classifier,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """Re-classify every bird crop from its stored BioCLIP embedding under ``regions``.

    ``classifier`` is a ``BioClipSpeciesClassifier`` (text-only is enough).
    Returns ``{updated, changed, images, locked, wildlife_skipped, missing, total, canceled, regions}``;
    ``changed`` counts crops whose species label actually differs from before.
    """
    from . import embedding_store, smart_search
    from .database import ensure_columns, read_database_csv, save_database, write_json_atomic
    from .ml.bioclip_species import MODEL_ID as SPECIES_MODEL_ID
    from .upgrade_v3 import _scene_locks

    keys, mat, _hdr = embedding_store.load_embeddings(kestrel_dir, model=SPECIES_MODEL_ID)
    if not keys:
        raise RuntimeError("no stored species embeddings in this folder")
    vecs = {k: mat[i] for i, k in enumerate(keys)}

    classifier.set_regions(regions)
    applied = classifier.regions

    db_path = os.path.join(kestrel_dir, DATABASE_NAME)
    df = ensure_columns(read_database_csv(db_path))
    for col in ("secondary_species_list", "secondary_species_scores",
                "secondary_family_list", "secondary_family_scores"):
        if col not in df.columns:
            df[col] = ""
    rows = smart_search.read_folder_rows(kestrel_dir)
    locked = _scene_locks(kestrel_dir, rows)

    stats = {"updated": 0, "changed": 0, "locked": 0, "wildlife_skipped": 0, "missing": 0, "images": 0,
             "total": len(df), "canceled": False, "regions": applied}
    if progress_cb:
        progress_cb(0, len(df))
    dirty = False
    for n, ix in enumerate(df.index):
        if cancel is not None and cancel():
            stats["canceled"] = True
            break
        fn = str(df.at[ix, "filename"])
        if fn in rows.analysed:
            if fn in locked:
                stats["locked"] += 1
            crops = _json_list(df.at[ix, "crops_json"])
            touched = False
            for c in crops:
                if not isinstance(c, dict):
                    continue
                try:
                    di, ci = int(c.get("detection_index", -1)), int(c.get("crop_index"))
                except (TypeError, ValueError):
                    continue
                if di < 0:
                    continue
                if _is_wildlife(c):
                    stats["wildlife_skipped"] += 1
                    continue
                emb = vecs.get((fn, ci))
                if emb is None:
                    stats["missing"] += 1
                    continue
                res = classifier.classify_embedding(np.asarray(emb, dtype=np.float32))
                labels, scores = res["top_species_labels"], res["top_species_scores"]
                fams, fscores = res["top_family_labels"], res["top_family_scores"]
                new_sp = str(labels[0]) if len(labels) else "Unknown"
                if new_sp != str(c.get("species", "")):
                    stats["changed"] += 1
                c["species"] = new_sp
                c["species_confidence"] = float(scores[0]) if len(scores) else 0.0
                c["family"] = str(fams[0]) if len(fams) else "Unknown"
                c["family_confidence"] = float(fscores[0]) if len(fscores) else 0.0
                stats["updated"] += 1
                touched = True
            if touched:
                stats["images"] += 1
                df.at[ix, "crops_json"] = json.dumps(crops)
                try:
                    p = int(float(df.at[ix, "primary_crop_index"]))
                except (TypeError, ValueError):
                    p = 0
                p = max(0, min(len(crops) - 1, p))
                prim = crops[p] if isinstance(crops[p], dict) else next(
                    (c for c in crops if isinstance(c, dict) and int(c.get("detection_index", -1)) >= 0), None)
                if prim is not None:
                    for col in ("species", "species_confidence", "family", "family_confidence"):
                        if col in prim:
                            df.at[ix, col] = prim[col]
                cs = [c for c in crops if isinstance(c, dict)]
                df.at[ix, "secondary_species_list"] = json.dumps([str(c.get("species", "Unknown")) for c in cs])
                df.at[ix, "secondary_species_scores"] = json.dumps([float(c.get("species_confidence", 0.0)) for c in cs])
                df.at[ix, "secondary_family_list"] = json.dumps([str(c.get("family", "Unknown")) for c in cs])
                df.at[ix, "secondary_family_scores"] = json.dumps([float(c.get("family_confidence", 0.0)) for c in cs])
                dirty = True
        if progress_cb and (n % 25 == 0 or n + 1 == len(df)):
            progress_cb(n + 1, len(df))

    if dirty and not stats["canceled"]:
        save_database(df, db_path)
        meta = _read_meta(kestrel_dir)
        meta["species_regions"] = list(applied)
        if isinstance(meta.get("analysis_settings"), dict):
            meta["analysis_settings"]["species_regions"] = list(applied)
        write_json_atomic(os.path.join(kestrel_dir, METADATA_FILENAME), meta, indent=2)
    elif not stats["canceled"]:
        meta = _read_meta(kestrel_dir)
        if meta:
            meta["species_regions"] = list(applied)
            if isinstance(meta.get("analysis_settings"), dict):
                meta["analysis_settings"]["species_regions"] = list(applied)
            write_json_atomic(os.path.join(kestrel_dir, METADATA_FILENAME), meta, indent=2)
    return stats
