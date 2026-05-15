"""End-to-end validation harness for Project Kestrel.

Runs 9 sequential checks against the current environment (source-mode or
frozen-binary) and reports PASS/FAIL for each. Used by ``cli.py --validate``
to prove a build is shippable before it leaves CI.

This module MUST NOT import ``pytest``, ``unittest``, or any test framework —
it is bundled into the PyInstaller binary and needs to stay dependency-light.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
import tempfile
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# Per-check `except Exception` only catches Python exceptions, so native crashes
# in onnxruntime / libraw / etc. exit the process without a traceback. The
# --cli --validate entry point doesn't go through visualizer.main(), so the
# faulthandler enable there is bypassed — turn it on here so the CI log shows
# which native call segfaulted on the next failure.
try:
    import faulthandler
    faulthandler.enable()
except Exception:
    pass


@dataclasses.dataclass
class _Ctx:
    """Per-run state shared between checks."""
    images_dir: Optional[Path] = None
    scratch_dir: Optional[Path] = None
    sample_images: list[Path] = dataclasses.field(default_factory=list)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _open_tee_log() -> Optional[object]:
    try:
        try:
            from kestrel_analyzer.logging_utils import get_log_path
        except ImportError:
            from analyzer.kestrel_analyzer.logging_utils import get_log_path  # type: ignore
        base = get_log_path(None)
        # get_log_path returns a file path inside a log directory; tee log
        # alongside it.
        log_dir = os.path.dirname(base) if base else None
        if not log_dir:
            return None
        os.makedirs(log_dir, exist_ok=True)
        tee_path = os.path.join(log_dir, f"kestrel_validate_{_utc_iso()}.log")
        return open(tee_path, "w", encoding="utf-8")
    except Exception:
        return None


def _emit(line: str, tee=None) -> None:
    print(line, flush=True)
    if tee is not None:
        try:
            tee.write(line + "\n")
            tee.flush()
        except Exception:
            pass


def run_validation(images_dir: Optional[str], output_path: Optional[str]) -> int:
    """Run all 9 checks, write a structured JSON report (when output_path is
    given), tee human-readable PASS/FAIL lines to stdout and a log file.

    Returns the process exit code: 0 if every check passed, 1 otherwise.
    """
    tee = _open_tee_log()
    _emit(f"=== Kestrel validation starting at {datetime.now(timezone.utc).isoformat()} ===", tee)

    ctx = _Ctx(
        images_dir=Path(images_dir).resolve() if images_dir else None,
    )

    # Prepare a scratch directory for any check that needs to write files
    # (pipeline, XMP). Cleaned up at the end.
    scratch_root = Path(tempfile.mkdtemp(prefix="kestrel_validate_"))
    ctx.scratch_dir = scratch_root

    checks: list[tuple[str, Callable[[_Ctx], tuple[bool, str]]]] = [
        ("frozen_app_flag", _check_frozen_app_flag),
        ("model_loading", _check_model_loading),
        ("settings_roundtrip", _check_settings_roundtrip),
        ("folder_inspection", _check_folder_inspection),
        ("exif_read", _check_exif_read),
        ("pipeline_execution", _check_pipeline_execution),
        ("database_read", _check_database_read),
        ("xmp_write", _check_xmp_write),
        ("exposure_check", _check_exposure_column),
    ]

    results: list[dict] = []
    for name, fn in checks:
        try:
            ok, detail = fn(ctx)
        except Exception as exc:
            tb = traceback.format_exc(limit=4)
            ok = False
            detail = f"{type(exc).__name__}: {exc}\n{tb}"
        verdict = "PASS" if ok else "FAIL"
        _emit(f"{verdict} {name}: {detail}".rstrip(), tee)
        results.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    all_ok = all(r["ok"] for r in results)
    _emit(f"=== Validation {'PASSED' if all_ok else 'FAILED'} — {sum(r['ok'] for r in results)}/{len(results)} checks OK ===", tee)

    if output_path:
        try:
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "ok": all_ok,
                        "checks": results,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "frozen": bool(getattr(sys, "frozen", False)),
                    },
                    f,
                    indent=2,
                )
        except Exception as exc:
            _emit(f"WARN: failed to write --validate-output JSON: {exc}", tee)

    try:
        shutil.rmtree(scratch_root, ignore_errors=True)
    except Exception:
        pass
    if tee is not None:
        try:
            tee.close()
        except Exception:
            pass

    return 0 if all_ok else 1


# --- Individual checks --------------------------------------------------------

def _check_frozen_app_flag(ctx: _Ctx) -> tuple[bool, str]:
    """is_frozen_app() returns a dict with a bool 'frozen' key."""
    try:
        from api_bridge import Api
    except ImportError:
        from analyzer.api_bridge import Api  # type: ignore
    result = Api().is_frozen_app()
    if not isinstance(result, dict):
        return False, f"expected dict, got {type(result).__name__}"
    if "frozen" not in result:
        return False, f"missing 'frozen' key: {result!r}"
    if not isinstance(result["frozen"], bool):
        return False, f"'frozen' is {type(result['frozen']).__name__}, expected bool"
    return True, f"frozen={result['frozen']}"


def _check_model_loading(ctx: _Ctx) -> tuple[bool, str]:
    """All 3 ONNX model wrappers instantiate without raising."""
    try:
        from kestrel_analyzer.ml.speciesnet_sam_hq import SpeciesNetSAMHQWrapper
        from kestrel_analyzer.ml.bird_species import BirdSpeciesClassifier
        from kestrel_analyzer.ml.quality import QualityClassifier
        from kestrel_analyzer.config import (
            MODELS_DIR,
            QUALITY_NORMALIZATION_DATA_PATH,
            QUALITYCLASSIFIER_PATH,
            SPECIESCLASSIFIER_LABELS,
            SPECIESCLASSIFIER_PATH,
        )
    except ImportError:
        from analyzer.kestrel_analyzer.ml.speciesnet_sam_hq import SpeciesNetSAMHQWrapper  # type: ignore
        from analyzer.kestrel_analyzer.ml.bird_species import BirdSpeciesClassifier  # type: ignore
        from analyzer.kestrel_analyzer.ml.quality import QualityClassifier  # type: ignore
        from analyzer.kestrel_analyzer.config import (  # type: ignore
            MODELS_DIR,
            QUALITY_NORMALIZATION_DATA_PATH,
            QUALITYCLASSIFIER_PATH,
            SPECIESCLASSIFIER_LABELS,
            SPECIESCLASSIFIER_PATH,
        )
    wrapper = SpeciesNetSAMHQWrapper(use_gpu=False)
    wrapper.ensure_loaded()
    # Reuse the same provider coordinator so we don't double-register sessions.
    coord = wrapper.coord
    BirdSpeciesClassifier(
        str(SPECIESCLASSIFIER_PATH),
        str(SPECIESCLASSIFIER_LABELS),
        coord,
        models_dir=str(MODELS_DIR),
    )
    QualityClassifier(
        str(QUALITYCLASSIFIER_PATH),
        normalization_data_path=str(QUALITY_NORMALIZATION_DATA_PATH)
            if Path(QUALITY_NORMALIZATION_DATA_PATH).is_file() else None,
        coord=coord,
    )
    return True, "SpeciesNet+SAM-HQ+bird+quality loaded"


_SENTINEL_KEY = "_validate_sentinel"


def _check_settings_roundtrip(ctx: _Ctx) -> tuple[bool, str]:
    """Write a sentinel key, reload, assert equal, then restore original."""
    try:
        from settings_utils import load_persisted_settings, save_persisted_settings
    except ImportError:
        from analyzer.settings_utils import load_persisted_settings, save_persisted_settings  # type: ignore
    original = load_persisted_settings() or {}
    had_sentinel = _SENTINEL_KEY in original
    original_sentinel = original.get(_SENTINEL_KEY)
    sentinel_value = f"probe-{uuid.uuid4()}"
    try:
        mutated = dict(original)
        mutated[_SENTINEL_KEY] = sentinel_value
        save_persisted_settings(mutated)
        reloaded = load_persisted_settings() or {}
        if reloaded.get(_SENTINEL_KEY) != sentinel_value:
            return False, (
                f"sentinel not preserved: wrote {sentinel_value!r}, "
                f"got {reloaded.get(_SENTINEL_KEY)!r} — likely dropped by sanitizer"
            )
        return True, "roundtrip preserved sentinel"
    finally:
        # Always restore: leak-proof even if assertions above failed.
        restored = dict(load_persisted_settings() or {})
        if had_sentinel:
            restored[_SENTINEL_KEY] = original_sentinel
        else:
            restored.pop(_SENTINEL_KEY, None)
        try:
            save_persisted_settings(restored)
        except Exception:
            pass


def _check_folder_inspection(ctx: _Ctx) -> tuple[bool, str]:
    """inspect_folder() returns total >= 2 on the validate images dir."""
    if ctx.images_dir is None or not ctx.images_dir.is_dir():
        return False, f"--validate-images dir not found: {ctx.images_dir}"
    try:
        from folder_inspector import inspect_folder
    except ImportError:
        from analyzer.folder_inspector import inspect_folder  # type: ignore
    result = inspect_folder(str(ctx.images_dir))
    total = int(result.get("total", 0))
    if total < 2:
        return False, f"total={total} (need >=2 images)"
    # Cache sample image paths for downstream checks
    try:
        from kestrel_analyzer.config import RAW_EXTENSIONS, JPEG_EXTENSIONS
    except ImportError:
        from analyzer.kestrel_analyzer.config import RAW_EXTENSIONS, JPEG_EXTENSIONS  # type: ignore
    all_exts = set(RAW_EXTENSIONS) | set(JPEG_EXTENSIONS)
    files = sorted(
        f for f in ctx.images_dir.iterdir()
        if f.is_file() and f.suffix.lower() in all_exts
    )
    ctx.sample_images = files[:2]
    return True, f"total={total}, has_kestrel={result.get('has_kestrel', False)}"


def _check_exif_read(ctx: _Ctx) -> tuple[bool, str]:
    """get_capture_time() on first sample image returns a datetime."""
    if not ctx.sample_images:
        return False, "no sample images cached (folder_inspection must run first)"
    try:
        from kestrel_analyzer.raw_exif import get_capture_time
    except ImportError:
        from analyzer.kestrel_analyzer.raw_exif import get_capture_time  # type: ignore
    sample = ctx.sample_images[0]
    ts = get_capture_time(str(sample))
    if not isinstance(ts, datetime):
        return False, f"{sample.name}: expected datetime, got {type(ts).__name__}"
    return True, f"{sample.name} -> {ts.isoformat()}"


def _check_pipeline_execution(ctx: _Ctx) -> tuple[bool, str]:
    """Copy 2 sample images into scratch, run AnalysisPipeline.process_folder()."""
    if not ctx.sample_images:
        return False, "no sample images cached"
    if ctx.scratch_dir is None:
        return False, "no scratch dir"
    work = ctx.scratch_dir / "pipeline"
    work.mkdir(exist_ok=True)
    for src in ctx.sample_images[:2]:
        shutil.copy2(src, work / src.name)
    try:
        from kestrel_analyzer.pipeline import AnalysisPipeline
        from kestrel_analyzer.config import DEFAULT_DETECTOR_NAME
    except ImportError:
        from analyzer.kestrel_analyzer.pipeline import AnalysisPipeline  # type: ignore
        from analyzer.kestrel_analyzer.config import DEFAULT_DETECTOR_NAME  # type: ignore
    pipeline = AnalysisPipeline(use_gpu=False, detector_name=DEFAULT_DETECTOR_NAME)
    pipeline.process_folder(
        folder=str(work),
        analyzer_name="cli_validate",
        wildlife_enabled=True,
        species_detection_enabled=True,
        detection_threshold=0.25,
        scene_time_threshold=60.0,
        max_bird_crops=5,
        parallel_prefetch=1,
    )
    kestrel_dir = work / ".kestrel"
    if not kestrel_dir.is_dir():
        return False, f"pipeline did not create {kestrel_dir}"
    csv_path = kestrel_dir / "kestrel_database.csv"
    if not csv_path.is_file():
        return False, "kestrel_database.csv not created"
    return True, f"pipeline produced {kestrel_dir.name}/kestrel_database.csv"


def _check_database_read(ctx: _Ctx) -> tuple[bool, str]:
    """load_database() returns a frame whose columns are a superset of BASE_COLUMNS."""
    if ctx.scratch_dir is None:
        return False, "no scratch dir"
    kestrel_dir = ctx.scratch_dir / "pipeline" / ".kestrel"
    if not kestrel_dir.is_dir():
        return False, f"missing .kestrel dir: {kestrel_dir}"
    try:
        from kestrel_analyzer.database import BASE_COLUMNS, load_database
    except ImportError:
        from analyzer.kestrel_analyzer.database import BASE_COLUMNS, load_database  # type: ignore
    db, _ = load_database(str(kestrel_dir), "cli_validate")
    missing = [c for c in BASE_COLUMNS if c not in db.columns]
    if missing:
        return False, f"missing columns: {missing}"
    if len(db) < 1:
        return False, f"database empty: {len(db)} rows"
    return True, f"{len(db)} rows, {len(db.columns)} cols"


def _check_xmp_write(ctx: _Ctx) -> tuple[bool, str]:
    """write_xmp_metadata() produces .xmp sidecars for each row."""
    if ctx.scratch_dir is None:
        return False, "no scratch dir"
    work = ctx.scratch_dir / "pipeline"
    kestrel_dir = work / ".kestrel"
    if not kestrel_dir.is_dir():
        return False, f"pipeline output missing: {kestrel_dir}"
    try:
        from metadata_writer import write_xmp_metadata
        from kestrel_analyzer.database import load_database
    except ImportError:
        from analyzer.metadata_writer import write_xmp_metadata  # type: ignore
        from analyzer.kestrel_analyzer.database import load_database  # type: ignore
    db, _ = load_database(str(kestrel_dir), "cli_validate")
    image_data = [
        {
            "filename": str(row["filename"]),
            "rating": 3,
            "culled": "accept",
            "culled_origin": "manual",
            "species": str(row.get("species", "")),
            "family": str(row.get("family", "")),
            "quality": float(row.get("quality", 0.0)) if row.get("quality") is not None else 0.0,
        }
        for _, row in db.iterrows()
    ]
    write_xmp_metadata(str(work), image_data, overwrite_external=True, use_auto_labels=False)
    sidecars = list(work.glob("*.xmp"))
    if len(sidecars) < len(image_data):
        return False, f"wrote {len(sidecars)} sidecars for {len(image_data)} rows"
    return True, f"{len(sidecars)} XMP sidecars written"


def _check_exposure_column(ctx: _Ctx) -> tuple[bool, str]:
    """exposure_correction column is non-null for every pipeline row."""
    if ctx.scratch_dir is None:
        return False, "no scratch dir"
    kestrel_dir = ctx.scratch_dir / "pipeline" / ".kestrel"
    try:
        from kestrel_analyzer.database import load_database
    except ImportError:
        from analyzer.kestrel_analyzer.database import load_database  # type: ignore
    db, _ = load_database(str(kestrel_dir), "cli_validate")
    if "exposure_correction" not in db.columns:
        return False, "missing exposure_correction column"
    if not db["exposure_correction"].notna().all():
        nans = int(db["exposure_correction"].isna().sum())
        return False, f"{nans}/{len(db)} rows have null exposure_correction"
    return True, f"all {len(db)} rows have a value"
