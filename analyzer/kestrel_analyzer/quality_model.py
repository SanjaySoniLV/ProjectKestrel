"""Which quality scorer the ``quality`` column holds (pipeline 3.0.0).

Since 3.0.0 every crop is scored twice and both raw 0-1 scores are stored:

* ``quality_siglip`` -- zero-shot SigLIP 2 sharpness (default; ``ml/siglip.py``)
* ``quality_legacy`` -- the pre-3.0 quality CNN (``models/quality.onnx``), kept
  for one major version as an optional scorer.

``quality`` (in the CSV and in each ``crops_json`` entry) stays *the
effective score*, so every existing reader -- ratings, culling assistant,
scene grid, XMP export, Perch uploader -- works unchanged. The
``quality_model`` setting picks which stored score is effective; switching is
free because nothing is re-analysed: :func:`apply_quality_model` rewrites the
``quality`` column and each crop's ``quality``/``rating`` from the stored
scores (the primary crop itself is never re-pointed: it may be a manual pick).

When to rewrite: lazily, when a folder's CSV is read by the app
(``Api.read_kestrel_csv``) and its ``kestrel_metadata.json`` records a
different ``quality_model`` than the setting. The settings UI reloads the
open folders after a switch, so open folders update immediately and every
other folder updates the next time it is opened. Folders analysed before
3.0.0 have no ``quality_siglip`` scores until upgraded; for them the rewrite
is a no-op (the legacy score is the only one).
"""

from __future__ import annotations

import json
import math
import os
from typing import Optional

QUALITY_MODELS = ("siglip", "legacy")
DEFAULT_QUALITY_MODEL = "siglip"
METADATA_KEY = "quality_model"


def resolve(value) -> str:
    v = str(value or "").strip().lower()
    return v if v in QUALITY_MODELS else DEFAULT_QUALITY_MODEL


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f < 0:
        return None
    return f


def effective(q_siglip, q_legacy, model: str) -> float:
    """The score ``model`` selects, falling back to the other scorer when the
    chosen one is missing (``-1`` / blank), else ``-1`` (unrated)."""
    s, l = _num(q_siglip), _num(q_legacy)
    first, second = (s, l) if resolve(model) == "siglip" else (l, s)
    if first is not None:
        return float(first)
    if second is not None:
        return float(second)
    return -1.0


def settings_quality_model() -> str:
    try:
        from ..settings_utils import load_persisted_settings  # type: ignore
    except (ImportError, ValueError):
        try:
            from settings_utils import load_persisted_settings  # type: ignore
        except ImportError:
            return DEFAULT_QUALITY_MODEL
    try:
        return resolve((load_persisted_settings() or {}).get("quality_model"))
    except Exception:
        return DEFAULT_QUALITY_MODEL


def folder_quality_model(kestrel_dir: str) -> Optional[str]:
    from .config import METADATA_FILENAME
    try:
        with open(os.path.join(kestrel_dir, METADATA_FILENAME), encoding="utf-8") as f:
            meta = json.load(f)
        v = meta.get(METADATA_KEY)
        return resolve(v) if v else None
    except Exception:
        return None


def needs_apply(kestrel_dir: str, model: str) -> bool:
    """True when the folder holds both scorers' columns and was last written for another model."""
    from .config import DATABASE_NAME
    db = os.path.join(kestrel_dir, DATABASE_NAME)
    if not os.path.isfile(db):
        return False
    cur = folder_quality_model(kestrel_dir)
    if cur is None:
        # Pre-3.0 folder (no scorer recorded): its `quality` IS the legacy
        # score; only a folder with siglip scores can switch.
        cur = "legacy"
    if cur == resolve(model):
        return False
    try:
        from .database import read_database_text
        header = read_database_text(db).split("\n", 1)[0]
    except Exception:
        return False
    return "quality_siglip" in header


def _rewrite_crops(crops_json, model: str, thresholds) -> str:
    """Per-crop ``quality``/``rating`` from each crop's stored scorer outputs."""
    from .ratings import quality_to_rating
    try:
        crops = json.loads(crops_json) if isinstance(crops_json, str) and crops_json else []
    except ValueError:
        return crops_json
    if not isinstance(crops, list) or not crops:
        return crops_json
    changed = False
    for c in crops:
        if not isinstance(c, dict):
            continue
        if _num(c.get("quality_siglip")) is None and _num(c.get("quality_legacy")) is None:
            continue
        q = effective(c.get("quality_siglip"), c.get("quality_legacy"), model)
        c["quality"] = q
        c["rating"] = int(quality_to_rating(q, thresholds)) if q >= 0 else 0
        changed = True
    return json.dumps(crops) if changed else crops_json


def apply_quality_model(kestrel_dir: str, model: str, thresholds=None, log_path: Optional[str] = None) -> bool:
    """Rewrite the folder's effective ``quality`` for ``model``. Returns True if written.

    Idempotent; writes the CSV atomically through ``save_database`` (which
    re-merges the UI's ``culled`` columns) and records ``quality_model`` in
    the metadata. Never touches ``kestrel_scenedata.json`` (manual ratings,
    tags, scene names live there).
    """
    from .config import DATABASE_NAME, METADATA_FILENAME
    from .database import read_database_csv, save_database, write_json_atomic

    model = resolve(model)
    db_path = os.path.join(kestrel_dir, DATABASE_NAME)
    if not os.path.isfile(db_path):
        return False
    df = read_database_csv(db_path)
    if df.empty or "quality_siglip" not in df.columns:
        return False
    if thresholds is None:
        try:
            from .ratings import resolve_thresholds
            try:
                from ..settings_utils import load_persisted_settings  # type: ignore
            except (ImportError, ValueError):
                from settings_utils import load_persisted_settings  # type: ignore
            sett = load_persisted_settings() or {}
            thresholds = resolve_thresholds(sett.get("rating_profile", "balanced"),
                                            sett.get("rating_thresholds_custom"))
        except Exception:
            thresholds = None
    # The primary crop is NOT re-pointed: the user may have promoted a crop by
    # hand (the CSV does not record that), so only the scores change. The
    # row's effective quality follows its own stored scorer columns.
    legacy_col = df["quality_legacy"].tolist() if "quality_legacy" in df.columns else [None] * len(df)
    old_q = df["quality"].tolist() if "quality" in df.columns else [-1.0] * len(df)
    new_q = []
    for qs, ql, qo in zip(df["quality_siglip"].tolist(), legacy_col, old_q):
        if _num(qs) is None and _num(ql) is None:
            new_q.append(qo)
        else:
            new_q.append(effective(qs, ql, model))
    df["quality"] = new_q
    if "crops_json" in df.columns:
        df["crops_json"] = [_rewrite_crops(cj, model, thresholds) for cj in df["crops_json"].tolist()]
    save_database(df, db_path)
    meta_path = os.path.join(kestrel_dir, METADATA_FILENAME)
    try:
        meta = {}
        if os.path.isfile(meta_path):
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        meta[METADATA_KEY] = model
        write_json_atomic(meta_path, meta, indent=2)
    except Exception as e:
        if log_path:
            from .logging_utils import log_warning
            log_warning(log_path, f"quality_model metadata write failed: {e}", stage="quality_model")
    return True
