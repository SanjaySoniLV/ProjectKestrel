"""Upgrade an already-analysed folder to pipeline 3.0.0 from its stored crops (local only).

No RAW decode, no detector, no SAM: everything comes from ``.kestrel/``:

* each subject crop JPEG (``crop/<base>_crop_<i>.jpg``) -> BioCLIP 2 embedding
  -> bird species/family re-ID (geofenced to the folder's regions), bird vs
  wildlife routing, and the false-positive gate, which may only **flag** a
  crop (``gate_flagged``) -- an upgrade never deletes a detection;
* the same crop -> SigLIP 2 embedding -> ``quality_siglip``; the crop's
  existing ``quality`` (pre-3.0 = the legacy CNN) becomes ``quality_legacy``;
* each export JPEG (the downscaled whole frame) -> SigLIP frame embedding;
* metadata stamped ``pipeline_major: 3`` once every analysed row is covered.

What it never touches: ``kestrel_scenedata.json`` (scene membership, names,
approve status, manual star ratings in ``image_ratings``, user tags), the
``culled`` / ``culled_origin`` columns (``save_database`` re-merges them from
disk), XMP sidecars, the primary-crop choice, and the species/family of any
image whose scene has user-edited or finalized tags.

Eligibility (``eligibility``): the folder must have been analysed by
pipeline >= 2.0.0 -- the first version that wrote MegaDetector + SAM-HQ
square subject crops plus downscaled export frames (the 2.0.x line;
``config.MIN_UPGRADABLE_VERSION``) -- and those JPEGs must still be on disk.
Older folders (1.x: Mask R-CNN-era or no ``crops_json``) need a re-analysis.

Crash safety / idempotency: the CSV is backed up once
(``OLD_kestrel_database_pre-v3_<ts>.csv``), rows are rewritten in memory and
saved atomically every ``SAVE_EVERY`` images and at the end; embeddings are
append-only sidecar writes. A cancelled or crashed run leaves a consistent
partial upgrade that the next run completes (rows with ``quality_siglip`` and
both embeddings are skipped).
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from typing import Callable, Dict, List, Optional

import numpy as np

from .config import (
    DATABASE_NAME,
    METADATA_FILENAME,
    MIN_UPGRADABLE_VERSION,
    PIPELINE_MAJOR,
    LEGACY_QUALITY_MODEL_ID,
)
from . import quality_model as quality_model_mod

SAVE_EVERY = 25
BACKUP_PREFIX = "OLD_kestrel_database_pre-v3_"


def _parse_version(v) -> Optional[tuple]:
    try:
        parts = [int(p) for p in str(v or "").strip().lstrip("v").split(".")[:3]]
    except ValueError:
        return None
    if not parts:
        return None
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _read_meta(kestrel_dir: str) -> dict:
    try:
        with open(os.path.join(kestrel_dir, METADATA_FILENAME), encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def eligibility(kestrel_dir: str) -> dict:
    """``{status, reason, version, pipeline_major, rows, missing}``.

    ``status``: ``"not_analyzed"`` | ``"current"`` (pipeline 3, fully indexed)
    | ``"upgradable"`` | ``"reanalyze"`` (too old or crops missing).
    """
    from . import smart_search
    out = {"status": "not_analyzed", "reason": "", "version": "", "pipeline_major": 0,
           "rows": 0, "missing": 0}
    if not os.path.isfile(os.path.join(kestrel_dir, DATABASE_NAME)):
        return out
    meta = _read_meta(kestrel_dir)
    try:
        major = int(meta.get("pipeline_major") or 0)
    except (TypeError, ValueError):
        major = 0
    created = str(meta.get("version") or "")
    out.update(version=created, pipeline_major=major)
    rows = smart_search.read_folder_rows(kestrel_dir)
    out["rows"] = len(rows.analysed)
    cov = smart_search.coverage(kestrel_dir, None)
    missing = max(0, cov["indexable_images"] - cov["indexed_images"])
    missing = max(missing, cov["frame_indexable_images"] - cov["frame_indexed_images"])
    out["missing"] = int(missing)
    if major >= PIPELINE_MAJOR and missing == 0:
        out.update(status="current", reason="Analysed with pipeline 3")
        return out
    ver = _parse_version(created)
    if major < PIPELINE_MAJOR and (ver is None or ver < MIN_UPGRADABLE_VERSION):
        out.update(status="reanalyze",
                   reason=f"Analysed by pipeline {created or 'unknown'}; re-analyse to use smart search "
                          f"(upgrade needs {'.'.join(map(str, MIN_UPGRADABLE_VERSION))}+ crops)")
        return out
    # Crops / exports must still be on disk for (most of) the folder.
    checked = present = 0
    for fn in sorted(rows.analysed)[:200]:
        for crop in rows.crops.get(fn, [])[:1]:
            checked += 1
            if smart_search.resolve_crop_file(kestrel_dir, fn, crop):
                present += 1
    if checked and present < checked * 0.5:
        out.update(status="reanalyze", reason="Most stored crop images are missing; re-analyse the folder")
        return out
    out.update(status="upgradable",
               reason="Can be upgraded to pipeline 3 from its stored crops (no re-analysis)")
    return out


def _scene_locks(kestrel_dir: str, rows) -> set:
    """Filenames whose species/family must not change: their scene has user-edited or finalized tags."""
    from . import smart_search
    tags = smart_search.read_scene_tags(kestrel_dir)
    locked = set()
    for fn in rows.analysed:
        sid = tags.scene_of.get(fn, rows.scene_of.get(fn, fn))
        if tags.finalized.get(sid) or tags.user_species.get(sid) or tags.user_families.get(sid):
            locked.add(fn)
    return locked


def _det_idx(c: dict) -> int:
    try:
        return int(c.get("detection_index", -1))
    except (TypeError, ValueError):
        return -1


def _json_list(raw) -> list:
    try:
        v = json.loads(raw) if isinstance(raw, str) and raw else (raw if isinstance(raw, list) else [])
        return v if isinstance(v, list) else []
    except ValueError:
        return []


def upgrade_folder(
    kestrel_dir: str,
    models,
    progress_cb: Optional[Callable[[int, int, str], None]] = None,
    cancel_event: Optional[threading.Event] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    log_path: Optional[str] = None,
    quality_model: Optional[str] = None,
    only_missing: bool = True,
) -> dict:
    """Upgrade ``kestrel_dir`` in place. ``models`` is a loaded ``AnalysisPipeline``
    (``species_clf``, ``siglip_img``, ``siglip_quality``, ``animal_gate``, ``sn_sam``).

    Returns ``{upgraded, skipped, failed, flagged, total, canceled, stopped, backup}``.
    """
    from . import embedding_store, smart_search
    from .database import ensure_columns, read_database_csv, save_database, write_json_atomic
    from .ml import siglip
    from .ml.animal_gate import threshold_for, GATE_METHOD
    from .ml.bioclip_species import MODEL_ID as SPECIES_MODEL_ID
    from .ratings import quality_to_rating, resolve_thresholds
    from .logging_utils import log_event, log_warning

    if models.species_clf is None or models.siglip_img is None:
        raise RuntimeError("upgrade needs the BioCLIP and SigLIP image towers")
    quality_model = quality_model_mod.resolve(quality_model or quality_model_mod.settings_quality_model())
    meta = _read_meta(kestrel_dir)
    settings = meta.get("analysis_settings") if isinstance(meta.get("analysis_settings"), dict) else {}
    regions = meta.get("species_regions") or settings.get("species_regions") or ["NA"]
    wildlife_enabled = bool(settings.get("wildlife_enabled", False))
    species_enabled = bool(settings.get("species_detection_enabled", True))
    try:
        det_threshold = float(settings.get("detection_threshold", 0.25))
    except (TypeError, ValueError):
        det_threshold = 0.25
    thresholds = None
    try:
        try:
            from ..settings_utils import load_persisted_settings  # type: ignore
        except (ImportError, ValueError):
            from settings_utils import load_persisted_settings  # type: ignore
        sett = load_persisted_settings() or {}
        thresholds = resolve_thresholds(sett.get("rating_profile", "balanced"), sett.get("rating_thresholds_custom"))
    except Exception:
        thresholds = None

    clf = models.species_clf
    clf.set_regions(regions)
    run = getattr(getattr(models, "sn_sam", None), "run_with_resilience", None) or (lambda fn: fn())

    db_path = os.path.join(kestrel_dir, DATABASE_NAME)
    df = ensure_columns(read_database_csv(db_path))
    rows = smart_search.read_folder_rows(kestrel_dir)
    locked = _scene_locks(kestrel_dir, rows)

    bio_store = embedding_store.EmbeddingStore(kestrel_dir, model=SPECIES_MODEL_ID, log_path=log_path)
    crop_store = siglip.open_store(kestrel_dir, slot=siglip.MODEL_ID, log_path=log_path)
    frame_store = siglip.open_store(kestrel_dir, slot=siglip.FRAME_SLOT, log_path=log_path)

    def _done(i: int) -> bool:
        fn = str(df.at[df.index[i], "filename"])
        if fn not in rows.analysed:
            return True
        has_frame = frame_store.has(fn)
        subs = rows.crops.get(fn, [])
        if not subs:
            return has_frame
        try:
            qs = float(df.at[df.index[i], "quality_siglip"])
        except (TypeError, ValueError):
            qs = -1.0
        return has_frame and crop_store.has(fn) and qs >= 0

    todo = [i for i in range(len(df)) if not (only_missing and _done(i))]
    total = len(todo)
    stats = {"upgraded": 0, "skipped": 0, "failed": 0, "flagged": 0, "total": total,
             "canceled": False, "stopped": False, "backup": ""}
    if progress_cb:
        progress_cb(0, total, "")

    if total:
        from .logging_utils import utc_now_naive
        backup = os.path.join(kestrel_dir, f"{BACKUP_PREFIX}{utc_now_naive().strftime('%Y%m%d_%H%M%S')}.csv")
        try:
            if not any(n.startswith(BACKUP_PREFIX) for n in os.listdir(kestrel_dir)):
                shutil.copy2(db_path, backup)
                stats["backup"] = os.path.basename(backup)
        except OSError as e:
            raise RuntimeError(f"could not back up the database before upgrading: {e}")

    for n_done, i in enumerate(todo):
        if cancel_event is not None and cancel_event.is_set():
            stats["canceled"] = True
            break
        if should_stop is not None and should_stop():
            stats["stopped"] = True
            break
        ix = df.index[i]
        fn = str(df.at[ix, "filename"])
        try:
            # ── whole frame ────────────────────────────────────────────
            exp = smart_search.resolve_export_file(kestrel_dir, fn, str(df.at[ix, "export_path"] or ""))
            if exp is not None:
                fe = run(lambda _p=exp: models.siglip_img.embed(smart_search.load_rgb(_p))[0])
                frame_store.put(fn, {0: fe})
            # ── subject crops ──────────────────────────────────────────
            crops = _json_list(df.at[ix, "crops_json"])
            bio_vecs: Dict[int, np.ndarray] = {}
            sig_vecs: Dict[int, np.ndarray] = {}
            changed_species = False
            for c in crops:
                if not isinstance(c, dict):
                    continue
                try:
                    di = int(c.get("detection_index", -1))
                    ci = int(c.get("crop_index"))
                except (TypeError, ValueError):
                    continue
                if di < 0:
                    continue
                path = smart_search.resolve_crop_file(kestrel_dir, fn, c)
                if path is None:
                    continue
                img = smart_search.load_rgb(path)
                emb = run(lambda _im=img: clf.embed(_im)[0])
                bio_vecs[ci] = emb
                if models.animal_gate is not None:
                    score = float(models.animal_gate.animal_score(emb))
                    c["animal_score"] = round(score, 4)
                    flagged = score < threshold_for(det_threshold, getattr(models.animal_gate, "method", None))
                    c["gate_flagged"] = bool(flagged)
                    stats["flagged"] += int(flagged)
                route = clf.route(emb)
                c["bird_prob"] = round(float(route["bird_prob"]), 4)
                if fn not in locked:
                    if route["route"] == "wildlife" and wildlife_enabled:
                        c["subject"] = "wildlife"
                        c["species"] = route["wildlife_label"] or "Unknown"
                        c["species_confidence"] = float(route["wildlife_confidence"])
                        c["family"] = "N/A"
                        c["family_confidence"] = 0.0
                        c["wildlife_scientific"] = route["wildlife_scientific"]
                        c["wildlife_family"] = route["wildlife_family"]
                        c["wildlife_class"] = route["wildlife_class"]
                    else:
                        c["subject"] = "bird"
                        if species_enabled:
                            res = clf.classify_embedding(emb)
                            labels, scores = res["top_species_labels"], res["top_species_scores"]
                            fams, fscores = res["top_family_labels"], res["top_family_scores"]
                            c["species"] = str(labels[0]) if len(labels) else "Unknown"
                            c["species_confidence"] = float(scores[0]) if len(scores) else 0.0
                            c["family"] = str(fams[0]) if len(fams) else "Unknown"
                            c["family_confidence"] = float(fscores[0]) if len(fscores) else 0.0
                    changed_species = True
                sig = run(lambda _im=img: models.siglip_img.embed(_im)[0])
                sig_vecs[ci] = sig
                q_s = float(models.siglip_quality.score(sig)) if models.siglip_quality is not None else -1.0
                if "quality_legacy" not in c:
                    try:
                        c["quality_legacy"] = round(float(c.get("quality", -1.0)), 6)
                    except (TypeError, ValueError):
                        c["quality_legacy"] = -1.0
                c["quality_siglip"] = round(q_s, 6) if q_s >= 0 else -1.0
                q = quality_model_mod.effective(c["quality_siglip"], c["quality_legacy"], quality_model)
                c["quality"] = q
                c["rating"] = int(quality_to_rating(q, thresholds)) if q >= 0 else 0
            if bio_vecs:
                bio_store.put(fn, bio_vecs)
            if sig_vecs:
                crop_store.put(fn, sig_vecs)
            subjects = [c for c in crops if isinstance(c, dict) and _det_idx(c) >= 0]
            if subjects:
                df.at[ix, "crops_json"] = json.dumps(crops)
                try:
                    p = int(float(df.at[ix, "primary_crop_index"]))
                except (TypeError, ValueError):
                    p = 0
                p = max(0, min(len(crops) - 1, p))
                prim = crops[p] if isinstance(crops[p], dict) else subjects[0]
                df.at[ix, "quality_siglip"] = prim.get("quality_siglip", -1.0)
                df.at[ix, "quality_legacy"] = prim.get("quality_legacy", df.at[ix, "quality_legacy"])
                df.at[ix, "quality"] = prim.get("quality", df.at[ix, "quality"])
                if changed_species:
                    for col in ("species", "species_confidence", "family", "family_confidence"):
                        if col in prim:
                            df.at[ix, col] = prim[col]
                    sub_or_all = [c for c in crops if isinstance(c, dict)]
                    df.at[ix, "secondary_species_list"] = json.dumps([str(c.get("species", "Unknown")) for c in sub_or_all])
                    df.at[ix, "secondary_species_scores"] = json.dumps([float(c.get("species_confidence", 0.0)) for c in sub_or_all])
                    df.at[ix, "secondary_family_list"] = json.dumps([str(c.get("family", "Unknown")) for c in sub_or_all])
                    df.at[ix, "secondary_family_scores"] = json.dumps([float(c.get("family_confidence", 0.0)) for c in sub_or_all])
            stats["upgraded"] += 1
        except Exception as e:
            stats["failed"] += 1
            if stats["failed"] <= 5:
                log_warning(log_path, f"[upgrade_v3] {fn}: {type(e).__name__}: {e}", stage="upgrade_v3")
        if (n_done + 1) % SAVE_EVERY == 0:
            save_database(df, db_path)
        if progress_cb:
            progress_cb(n_done + 1, total, fn)

    save_database(df, db_path)
    for st in (bio_store, crop_store, frame_store):
        st.compact(df["filename"].astype(str).tolist())
    stale = os.path.join(kestrel_dir, embedding_store.EMBEDDINGS_DIRNAME, "clip-b16")
    if os.path.isdir(stale):
        shutil.rmtree(stale, ignore_errors=True)

    complete = not stats["canceled"] and not stats["stopped"] and stats["failed"] == 0
    meta = _read_meta(kestrel_dir)
    meta[quality_model_mod.METADATA_KEY] = quality_model
    meta["schema_version"] = max(3, int(meta.get("schema_version") or 0))
    if complete:
        from .config import VERSION
        from .logging_utils import utc_now_naive
        meta["pipeline_major"] = PIPELINE_MAJOR
        meta["upgraded_to_v3_utc"] = utc_now_naive().isoformat() + "Z"
        meta["kestrel_version"] = VERSION
        meta["models"] = {
            "detector": str(settings.get("detector_name", "") or ""),
            "species": SPECIES_MODEL_ID,
            "siglip": siglip.MODEL_ID,
            "legacy_quality": LEGACY_QUALITY_MODEL_ID,
            "animal_gate": GATE_METHOD if models.animal_gate is not None else None,
        }
    write_json_atomic(os.path.join(kestrel_dir, METADATA_FILENAME), meta, indent=2)
    try:
        log_event(log_path, {"level": "info", "event": "upgrade_v3", **{k: v for k, v in stats.items()}})
    except Exception:
        pass
    return stats
