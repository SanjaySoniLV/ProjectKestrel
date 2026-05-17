import concurrent.futures
import json
import os
import queue
import threading
import time
import warnings
from typing import Callable, Dict, Generator, Optional

import cv2
import numpy as np
import pandas as pd

from .config import (
    DEFAULT_DETECTOR_NAME,
    JPEG_EXTENSIONS,
    RAW_EXTENSIONS,
    SPECIESCLASSIFIER_LABELS,
    SPECIESCLASSIFIER_PATH,
    QUALITYCLASSIFIER_PATH,
    QUALITY_NORMALIZATION_DATA_PATH,
    MODELS_DIR,
    KESTREL_DIR_NAME,
    METADATA_FILENAME,
    VERSION,
)
from .database import (
    load_database,
    save_database,
    load_scenedata,
    save_scenedata,
    build_scenedata_from_database,
    update_scenedata_with_database,
)
from .exposure_compensation import (
    apply_exposure_crop_numpy,
    build_metered_detection_image,
    compose_total_stops as ec_compose_total_stops,
    compute_stops_numpy_solver,
    linear_to_srgb_u8,
)
from .image_utils import decode_embedded_preview, read_image, read_image_for_pipeline
from .ratings import quality_to_rating, get_profile_thresholds
from .similarity import compute_image_similarity_akaze, compute_similarity_timestamp
from .raw_exif import get_capture_time
from .logging_utils import get_log_path, log_event, log_exception, log_warning

try:
    from ..settings_utils import load_persisted_settings, debug as _debug, info as _info
except (ImportError, ValueError):
    # Top-level package case: cli.py adds analyzer/ to sys.path and imports
    # ``kestrel_analyzer`` as a root package, so the relative ``..`` walks
    # beyond it. The bare-name fallback works because ``settings_utils`` is
    # then directly importable from sys.path.
    try:
        from settings_utils import load_persisted_settings, debug as _debug, info as _info  # type: ignore
    except ImportError:
        load_persisted_settings = None
        def _debug(*_a, **_kw): pass
        def _info(*_a, **_kw): pass
from .ml.speciesnet_sam_hq import SpeciesNetSAMHQWrapper
from .ml.provider_coordinator import ResilienceConfig
from .ml.bird_species import BirdSpeciesClassifier
from .ml.quality import QualityClassifier


def _write_jpeg(path: str, bgr: np.ndarray, params) -> bool:
    # Some cv2 builds (headless/full conflict) reject imwrite's params arg.
    # imencode + tofile works on every wheel and handles unicode Windows paths.
    ok, buf = cv2.imencode('.jpg', bgr, params)
    if not ok:
        return False
    buf.tofile(path)
    return True


class AnalysisPipeline:
    def __init__(self, use_gpu: bool, detector_name: str = DEFAULT_DETECTOR_NAME):
        self.use_gpu = use_gpu
        self.detector_name = str(detector_name)
        self.sn_sam: Optional[SpeciesNetSAMHQWrapper] = None
        self.species_clf: Optional[BirdSpeciesClassifier] = None
        self.quality_clf: Optional[QualityClassifier] = None
        self._log_path: Optional[str] = None

    @staticmethod
    def _create_mask_overlay(
        thumbnail: np.ndarray,
        masks: Optional[np.ndarray],
        indices: Optional[list],
        color=(255, 64, 64),
        alpha: float = 0.45,
    ) -> Optional[np.ndarray]:
        if thumbnail is None:
            return None
        overlay = thumbnail.copy()
        if masks is None or not indices:
            return overlay
        h, w = overlay.shape[:2]
        for i in indices:
            mask = masks[i].astype(np.uint8)
            mask_small = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
            mask_bool = mask_small.astype(bool)
            if not np.any(mask_bool):
                continue
            overlay[mask_bool] = (
                overlay[mask_bool] * (1.0 - alpha) + np.array(color, dtype=np.uint8) * alpha
            ).astype(np.uint8)
        return overlay

    @staticmethod
    def _compute_stops_numpy(
        noauto_linear: np.ndarray,
        mask: np.ndarray,
        meter_scale: float,
        quality: str = "balanced",
    ) -> float:
        return compute_stops_numpy_solver(noauto_linear, mask, meter_scale, quality)

    @staticmethod
    def _apply_crop_numpy(
        noauto_linear: np.ndarray,
        crop_bbox,
        total_scale: float,
    ) -> np.ndarray:
        return apply_exposure_crop_numpy(noauto_linear, crop_bbox, total_scale)

    @staticmethod
    def _get_image_orientation(img: np.ndarray) -> str:
        if img is None or img.ndim < 2:
            return "unknown"
        h, w = img.shape[:2]
        if h > w:
            return "portrait"
        if w > h:
            return "landscape"
        return "square"

    # Default parallel RAW decode workers when caller omits parallel_prefetch (matches settings default).
    _DEFAULT_DECODE_WORKERS: int = 3

    def _decode_image(self, image_path: str, raw_file: str) -> dict:
        """Decode one image.  Safe to call from a worker thread.

        Returns a dict with keys:
            file_name, image_path, img, noauto_linear, raw_meter_scale,
            exposure_pipeline, meter_warning, orientation, capture_time, error
        """
        result: dict = {
            "file_name": raw_file,
            "image_path": image_path,
            "img": None,
            "noauto_linear": None,
            "raw_meter_scale": 1.0,
            "exposure_pipeline": "legacy_auto_bright_v1",
            "meter_warning": None,
            "orientation": "unknown",
            "capture_time": None,
            "capture_time_warning": None,
            "error": None,
        }
        try:
            img, raw_obj = read_image_for_pipeline(image_path)
            if img is None and raw_obj is None:
                raise RuntimeError("Image read returned None for both img and raw_obj")

            if raw_obj is not None:
                result["exposure_pipeline"] = "numpy_linear_v2"
                try:
                    metered_img, raw_meter_scale, meter_debug, noauto_linear = (
                        build_metered_detection_image(raw_obj)
                    )
                finally:
                    raw_obj.close()
                result["raw_meter_scale"] = float(raw_meter_scale)
                result["noauto_linear"] = noauto_linear
                if metered_img is not None:
                    img = metered_img
                elif meter_debug.get("error"):
                    # Metered RAW decode failed. Try the embedded JPEG
                    # preview before giving up — most modern bodies ship a
                    # full-resolution preview that's still useful for ML
                    # inference (Nikon HE/HE* NEFs are the typical trigger:
                    # LibRaw reads metadata but can't decompress TicoRAW
                    # sensor data). Reopens the file because the
                    # already-closed raw_obj is in an out-of-order state
                    # after a failed postprocess.
                    preview = decode_embedded_preview(image_path)
                    if preview is not None:
                        img = preview
                        result["exposure_pipeline"] = "embedded_preview_jpeg"
                        result["meter_warning"] = (
                            f"RAW decode unsupported ({meter_debug['error']}); "
                            "analyzing embedded JPEG preview instead "
                            "(highlight recovery disabled)"
                        )
                    else:
                        result["meter_warning"] = (
                            f"RAW metering fallback to default decode: "
                            f"{meter_debug['error']}"
                        )

            if img is None:
                raise RuntimeError("Image build returned None after decode")

            result["img"] = img
            result["orientation"] = self._get_image_orientation(img)

            try:
                ct = get_capture_time(image_path)
                result["capture_time"] = ct
            except Exception as ct_exc:
                # Scene grouping falls back to AKAZE feature similarity when
                # capture_time is missing; not fatal. We record the reason so
                # the main loop can surface it once via log_warning.
                result["capture_time_warning"] = (
                    f"Capture-time extraction failed: {ct_exc}"
                )

        except Exception as exc:
            result["error"] = exc

        return result

    def _iter_decoded(
        self,
        file_list: list,
        folder: str,
        max_workers: int = 3,
    ) -> Generator[dict, None, None]:
        """Yield decoded image dicts in file_list order.

        Up to *max_workers* files are decoded concurrently by background threads.
        A semaphore limits total submitted-but-not-yet-consumed results to
        ``max_workers + 1`` so peak memory stays bounded regardless of how
        slowly the consumer processes each image.

        Observability
        -------------
        Emits periodic ``[decode-queue]`` stderr lines (roughly every 10% of
        the run, plus the first few images) and a final structured
        ``decode_queue_summary`` log event so the operator can tell whether
        the queue is actually hiding decode latency behind pipeline work.

        Interpreting the numbers:
          * ``wait_ms=0`` consistently     → pipeline is the bottleneck, the
            queue is fully hiding decode cost (good).
          * ``wait_ms`` grows vs ``decode_ms``  → decoder is the bottleneck,
            raising ``parallel_prefetch`` may help (if disk/CPU headroom).
          * ``inflight`` never exceeds 1   → decodes are effectively serial;
            check ``parallel_prefetch`` setting or whether the file list
            fell into the degenerate inline path.
        """
        total = len(file_list)
        if max_workers <= 1 or total == 0:
            for raw_file in file_list:
                yield self._decode_image(os.path.join(folder, raw_file), raw_file)
            return

        metrics_lock = threading.Lock()
        submitted = 0        # decodes handed to the pool
        completed = 0        # decodes that have finished (set by workers)
        total_decode_ms = 0.0
        total_wait_ms = 0.0
        peak_inflight = 0

        max_buffered = max_workers + 1
        semaphore = threading.Semaphore(max_buffered)
        futures_q: queue.Queue = queue.Queue()

        # Fixed cadence (every 10 images) rather than a percentage of total so
        # long runs remain observable in real time. First 3 images always
        # print for warm-up visibility, and the last image always prints.
        log_every_n = 10

        def _decode_and_time(image_path: str, raw_file: str) -> dict:
            t0 = time.monotonic()
            decoded = self._decode_image(image_path, raw_file)
            decode_ms = (time.monotonic() - t0) * 1000.0
            decoded["_decode_ms"] = decode_ms
            with metrics_lock:
                nonlocal completed, total_decode_ms
                completed += 1
                total_decode_ms += decode_ms
            return decoded

        def _submit_all() -> None:
            nonlocal submitted, peak_inflight
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                for raw_file in file_list:
                    semaphore.acquire()
                    future = executor.submit(
                        _decode_and_time,
                        os.path.join(folder, raw_file),
                        raw_file,
                    )
                    with metrics_lock:
                        submitted += 1
                        inflight = submitted - completed
                        if inflight > peak_inflight:
                            peak_inflight = inflight
                    futures_q.put(future)
            futures_q.put(None)

        submit_thread = threading.Thread(target=_submit_all, daemon=True)
        submit_thread.start()

        idx = 0
        _debug(
            f"[decode-queue] starting: files={total} workers={max_workers} "
            f"buffer={max_buffered}"
        )

        while True:
            future = futures_q.get()
            if future is None:
                break
            t_wait = time.monotonic()
            decoded = future.result()
            wait_ms = (time.monotonic() - t_wait) * 1000.0
            with metrics_lock:
                total_wait_ms += wait_ms
                snap_submitted = submitted
                snap_completed = completed
            idx += 1

            # Print for the first 3 images (warm-up visibility), then every
            # ~10% of the run, and always on the last image.
            if idx <= 3 or idx % log_every_n == 0 or idx == total:
                decode_ms = decoded.get("_decode_ms")
                decode_ms_str = f"{decode_ms:.0f}" if decode_ms is not None else "?"
                inflight_now = max(0, snap_submitted - snap_completed)
                ahead = max(0, snap_submitted - idx)
                _debug(
                    f"[decode-queue] idx={idx}/{total} "
                    f"inflight={inflight_now} ahead={ahead} "
                    f"decode_ms={decode_ms_str} wait_ms={wait_ms:.0f}"
                )

            yield decoded
            semaphore.release()

        submit_thread.join()

        # Final summary — this is the number you actually want to watch
        # when deciding whether to raise parallel_prefetch.
        avg_decode = total_decode_ms / max(1, total)
        avg_wait = total_wait_ms / max(1, total)
        try:
            log_event(
                self._log_path,
                {
                    "level": "info",
                    "event": "decode_queue_summary",
                    "max_workers": int(max_workers),
                    "file_count": int(total),
                    "peak_inflight": int(peak_inflight),
                    "sum_decode_ms": round(total_decode_ms, 1),
                    "sum_wait_ms": round(total_wait_ms, 1),
                    "avg_decode_ms": round(avg_decode, 1),
                    "avg_wait_ms": round(avg_wait, 1),
                },
            )
        except Exception:
            pass
        _debug(
            f"[decode-queue] done: files={total} peak_inflight={peak_inflight} "
            f"avg_decode_ms={avg_decode:.0f} avg_wait_ms={avg_wait:.0f}"
        )

    def load_models(
        self,
        status_cb: Optional[Callable[[str], None]] = None,
        max_bird_crops: int = 5,
    ) -> None:
        try:
            max_bird_crops = int(max_bird_crops)
        except (TypeError, ValueError):
            max_bird_crops = 5
        max_bird_crops = max(1, min(20, max_bird_crops))

        mask_ready_for_cap = bool(
            self.sn_sam
            and int(getattr(self.sn_sam, "max_bird_crops", 5)) == max_bird_crops
            and getattr(self.sn_sam, "use_gpu", None) == self.use_gpu
            and str(getattr(self.sn_sam, "detector_name", DEFAULT_DETECTOR_NAME)) == self.detector_name
        )
        if mask_ready_for_cap and self.species_clf and self.quality_clf:
            # Wrapper is being reused — repoint its status_cb at the active
            # folder so coord notifications (e.g., "Switched to CPU after GPU
            # error") land on this folder's queue item, not whichever folder
            # first constructed the wrapper.
            try:
                self.sn_sam.update_status_cb(status_cb)
            except Exception:
                pass
            return
        if status_cb:
            status_cb(
                f"Loading models (detector={self.detector_name})... This may take a while on first run."
            )
        if not mask_ready_for_cap:
            resilience_cfg = None
            if callable(load_persisted_settings):
                try:
                    resilience_cfg = ResilienceConfig.from_settings(load_persisted_settings() or {})
                except Exception:
                    resilience_cfg = None
            self.sn_sam = SpeciesNetSAMHQWrapper(
                max_bird_crops=max_bird_crops,
                use_gpu=self.use_gpu,
                detector_name=self.detector_name,
                status_cb=status_cb,
                resilience_cfg=resilience_cfg,
            )
            # The new wrapper has a fresh coord. Any existing species/quality
            # classifiers were registered with the OLD coord, so drop them and
            # rebuild below — otherwise their sessions wouldn't migrate when
            # the new coord recreates.
            self.species_clf = None
            self.quality_clf = None
        # Share the wrapper's coordinator with the species/quality classifiers
        # so demote/promote rebuilds every session in the run together. Without
        # this, a DML/CoreML failure recovers the wrapper but leaves these two
        # sessions on the dead provider — every later image errors at the
        # species step and the run looks "stuck on error".
        coord = self.sn_sam.coord
        if not self.species_clf:
            self.species_clf = BirdSpeciesClassifier(
                str(SPECIESCLASSIFIER_PATH),
                str(SPECIESCLASSIFIER_LABELS),
                coord,
                models_dir=str(MODELS_DIR),
            )
        if not self.quality_clf:
            self.quality_clf = QualityClassifier(
                str(QUALITYCLASSIFIER_PATH),
                normalization_data_path=str(QUALITY_NORMALIZATION_DATA_PATH),
                coord=coord,
            )
        if status_cb:
            status_cb("Models loaded. Processing started.")

    def process_folder(
        self,
        folder: str,
        pause_event=None,
        cancel_event=None,
        callbacks: Optional[Dict[str, Callable]] = None,
        analyzer_name: str = "pipeline",
        wildlife_enabled: bool = True,
        species_detection_enabled: bool = True,
        detection_threshold: float = 0.25,
        scene_time_threshold: float = 1.0,
        mask_threshold: float = 0.5,
        max_bird_crops: int = 5,
        parallel_prefetch: int = 3,
        retry_errored: bool = False,
        exposure_quality: Optional[str] = None,
        thumbnail_max_width: Optional[int] = None,
        thumbnail_jpeg_compression: Optional[float] = None,
    ) -> None:
        callbacks = callbacks or {}
        status_cb = callbacks.get("on_status")
        progress_cb = callbacks.get("on_progress")
        image_cb = callbacks.get("on_image")
        thumbnail_cb = callbacks.get("on_thumbnail")
        detection_cb = callbacks.get("on_detection")
        crops_cb = callbacks.get("on_crops")
        quality_cb = callbacks.get("on_quality")
        species_cb = callbacks.get("on_species")
        error_cb = callbacks.get("on_error")

        try:
            max_bird_crops = int(max_bird_crops)
        except (TypeError, ValueError):
            max_bird_crops = 5
        max_bird_crops = max(1, min(20, max_bird_crops))

        try:
            decode_workers = int(parallel_prefetch)
        except (TypeError, ValueError):
            decode_workers = self._DEFAULT_DECODE_WORKERS
        decode_workers = max(1, min(5, decode_workers))

        rating_thresholds = None
        rating_profile = "balanced"
        # Caller-provided overrides win over persisted settings. None means
        # "fall back to settings.json (or default)".
        eq_override = exposure_quality
        tmw_override = thumbnail_max_width
        tjc_override = thumbnail_jpeg_compression
        exposure_quality = "balanced"
        thumbnail_max_width = 1200
        thumbnail_jpeg_compression = 0.75
        thumbnail_jpeg_quality = 75
        if callable(load_persisted_settings):
            try:
                sett = load_persisted_settings() or {}
                rating_profile = str(sett.get('rating_profile', 'balanced') or 'balanced').strip().lower() or 'balanced'
                rating_thresholds = get_profile_thresholds(rating_profile)
                raw_eq = str(sett.get('exposure_quality', 'balanced') or 'balanced').strip().lower()
                if raw_eq in {'lenient', 'balanced', 'aggressive'}:
                    exposure_quality = raw_eq
                try:
                    thumbnail_max_width = int(sett.get('thumbnail_max_width', 1200))
                except (TypeError, ValueError):
                    thumbnail_max_width = 1200
                raw_compression = sett.get('thumbnail_jpeg_compression', None)
                if raw_compression is None:
                    try:
                        legacy_quality = int(sett.get('thumbnail_jpeg_quality', 75))
                    except (TypeError, ValueError):
                        legacy_quality = 75
                    thumbnail_jpeg_compression = float(legacy_quality) / 100.0
                else:
                    try:
                        thumbnail_jpeg_compression = float(raw_compression)
                    except (TypeError, ValueError):
                        thumbnail_jpeg_compression = 0.75
            except Exception:
                rating_thresholds = None
        if eq_override is not None:
            raw_eq = str(eq_override).strip().lower()
            if raw_eq in {'lenient', 'balanced', 'aggressive'}:
                exposure_quality = raw_eq
        if tmw_override is not None:
            try:
                thumbnail_max_width = int(tmw_override)
            except (TypeError, ValueError):
                pass
        if tjc_override is not None:
            try:
                thumbnail_jpeg_compression = float(tjc_override)
            except (TypeError, ValueError):
                pass
        thumbnail_max_width = max(400, min(2400, thumbnail_max_width))
        thumbnail_jpeg_compression = max(0.5, min(1.0, thumbnail_jpeg_compression))
        thumbnail_jpeg_quality = int(round(thumbnail_jpeg_compression * 100.0))
        thumbnail_jpeg_quality = max(50, min(100, thumbnail_jpeg_quality))
        jpeg_params = [int(cv2.IMWRITE_JPEG_QUALITY), thumbnail_jpeg_quality]

        self._log_path = get_log_path(folder)
        stage_ctx = {"stage": "startup", "file": None}

        original_showwarning = warnings.showwarning

        def _showwarning(message, category, filename, lineno, file=None, line=None):
            log_warning(
                self._log_path,
                message,
                category=category,
                filename=filename,
                lineno=lineno,
                stage=stage_ctx["stage"],
                context={"file": stage_ctx["file"], "folder": folder},
            )
            if original_showwarning:
                original_showwarning(message, category, filename, lineno, file=file, line=line)

        warnings.showwarning = _showwarning

        try:
            stage_ctx["stage"] = "list_files"
            files = [
                f
                for f in os.listdir(folder)
                if os.path.isfile(os.path.join(folder, f))
                and os.path.splitext(f)[1].lower() in RAW_EXTENSIONS
            ]
            if not files:
                files = [
                    f
                    for f in os.listdir(folder)
                    if os.path.isfile(os.path.join(folder, f))
                    and os.path.splitext(f)[1].lower() in JPEG_EXTENSIONS
                ]
            files.sort()
            if not files:
                if status_cb:
                    status_cb("No supported image files found.")
                log_event(
                    self._log_path,
                    {
                        "level": "warning",
                        "event": "no_supported_files",
                        "analyzer": analyzer_name,
                        "folder": folder,
                    },
                )
                return

            log_event(
                self._log_path,
                {
                    "level": "info",
                    "event": "analysis_start",
                    "analyzer": analyzer_name,
                    "folder": folder,
                    "file_count": len(files),
                    "detector_name": self.detector_name,
                    "detection_threshold": float(detection_threshold),
                    "parallel_prefetch": int(decode_workers),
                },
            )

            stage_ctx["stage"] = "create_kestrel_dirs"
            kestrel_dir = os.path.join(folder, KESTREL_DIR_NAME)
            export_dir = os.path.join(kestrel_dir, "export")
            crop_dir = os.path.join(kestrel_dir, "crop")
            os.makedirs(export_dir, exist_ok=True)
            os.makedirs(crop_dir, exist_ok=True)

            stage_ctx["stage"] = "load_database"
            database, db_path = load_database(kestrel_dir, analyzer_name, log_path=self._log_path)

            # When retrying errored images, capture each errored row's previously-
            # computed values BEFORE dropping it. Many errors hit during detection
            # or later, after similarity / capture_time / orientation are already
            # filled in — the retry can reuse those instead of recomputing AKAZE
            # against the wrong neighbor (the rolling ``previous_image`` would be
            # the last surviving DB row, not the alphabetical predecessor).
            errored_rows_by_filename: Dict[str, dict] = {}
            if retry_errored and not database.empty and "species" in database.columns:
                errored_mask = database["species"].astype(str) == "Error"
                errored_count = int(errored_mask.sum())
                if errored_count > 0:
                    if status_cb:
                        status_cb(f"Including {errored_count} previously errored image(s) for retry.")
                    for _, row in database.loc[errored_mask].iterrows():
                        try:
                            errored_rows_by_filename[str(row["filename"])] = row.to_dict()
                        except Exception:
                            continue
                    # Drop errored rows so the pipeline's later concat doesn't
                    # produce duplicate (filename, species=Error) rows. The
                    # retried images will append fresh rows below.
                    database = database.loc[~errored_mask].copy()
                    save_database(database, db_path)

            processed_set = set(database["filename"].values)
            new_files = [f for f in files if f not in processed_set]
            # Index of every file in the sorted ``files`` list, so retry
            # iterations can look up each errored image's alphabetical
            # predecessor in O(1) for the previous_image reset path.
            files_idx: Dict[str, int] = {f: i for i, f in enumerate(files)}
            processed_count = len(files) - len(new_files)
            total = len(files)
            if progress_cb:
                progress_cb(processed_count, total)
            if processed_count > 0 and status_cb:
                status_cb("Picking up where Kestrel left off...")
            if not new_files:
                if status_cb:
                    status_cb("No new files to process.")
                if progress_cb:
                    progress_cb(total, total)
                return

            stage_ctx["stage"] = "load_models"
            self.load_models(status_cb=status_cb, max_bird_crops=max_bird_crops)

            previous_image = None
            previous_image_path = None
            previous_orientation = None
            if not database.empty:
                last_row = database.iloc[-1]
                last_filename = last_row["filename"]
                last_image_path = os.path.join(folder, last_filename)
                if os.path.exists(last_image_path):
                    img = read_image(last_image_path)
                    if img is not None:
                        previous_image = img
                        previous_image_path = last_image_path
                        previous_orientation = self._get_image_orientation(img)
            scene_count = database["scene_count"].max() if not database.empty else 0

            for idx, decoded in enumerate(
                self._iter_decoded(new_files, folder, max_workers=decode_workers),
                start=1,
            ):
                raw_file = decoded["file_name"]

                # Pause: wait until resume or until cancel_event is set.
                if pause_event is not None:
                    while not pause_event.is_set():
                        if cancel_event is not None and cancel_event.is_set():
                            if status_cb:
                                status_cb('Cancelled')
                            return
                        # Wait with timeout to be interruptible
                        pause_event.wait(timeout=0.5)
                if cancel_event is not None and cancel_event.is_set():
                    if status_cb:
                        status_cb('Cancelled')
                    return

                entry = {
                    "filename": raw_file,
                    "species": "Unknown",
                    "species_confidence": 0.0,
                    "family": "Unknown",
                    "family_confidence": 0.0,
                    "quality": -1.0,
                    "export_path": "N/A",
                    "crop_path": "N/A",
                    "crops_json": "[]",
                    "primary_crop_index": 0,
                    "scene_count": scene_count,
                    "feature_similarity": -1.0,
                    "feature_confidence": -1.0,
                    "color_similarity": -1.0,
                    "color_confidence": -1.0,
                    "similar": False,
                    "secondary_species_list": [],
                    "secondary_species_scores": [],
                    "secondary_family_list": [],
                    "secondary_family_scores": [],
                    "exposure_correction": 0.0,
                    "exposure_pipeline": "legacy_auto_bright_v1",
                    "exposure_subject_stops": 0.0,
                    "exposure_meter_scale": 1.0,
                    "detection_scores": [],
                    "capture_time": "",
                    "orientation": "unknown",
                }

                image_path = decoded["image_path"]
                raw_meter_scale = decoded["raw_meter_scale"]
                try:
                    stage_ctx["stage"] = "read_image"
                    stage_ctx["file"] = raw_file

                    # Re-raise any decode error inside the per-image error handler.
                    if decoded["error"] is not None:
                        raise decoded["error"]

                    img = decoded["img"]
                    noauto_linear = decoded["noauto_linear"]

                    if img is None:
                        raise RuntimeError("Image build returned None")

                    entry["exposure_pipeline"] = decoded["exposure_pipeline"]
                    entry["exposure_meter_scale"] = float(raw_meter_scale)
                    if decoded.get("meter_warning"):
                        log_warning(
                            self._log_path,
                            decoded["meter_warning"],
                            stage=stage_ctx["stage"],
                            context={"file": raw_file, "folder": folder},
                        )
                    if decoded.get("capture_time_warning"):
                        log_warning(
                            self._log_path,
                            decoded["capture_time_warning"],
                            stage=stage_ctx["stage"],
                            context={"file": raw_file, "folder": folder},
                        )

                    current_orientation = decoded["orientation"]
                    entry["orientation"] = current_orientation

                    ct = decoded["capture_time"]
                    entry["capture_time"] = ct.isoformat() if ct is not None else ""

                    stage_ctx["stage"] = "compute_similarity"

                    preserved_row = errored_rows_by_filename.get(raw_file)

                    def _coerce_float(v, default):
                        try:
                            f = float(v)
                            return f if f == f else default  # NaN check
                        except (TypeError, ValueError):
                            return default

                    preserved_feat_sim = _coerce_float(preserved_row.get("feature_similarity"), -1.0) if preserved_row else -1.0
                    preserved_was_similar = bool(preserved_row.get("similar", False)) if preserved_row else False
                    preserved_sim_valid = bool(
                        preserved_row is not None
                        and (preserved_feat_sim >= 0.0 or preserved_was_similar)
                    )

                    if preserved_sim_valid:
                        # Reuse similarity from the prior (errored) attempt. The
                        # previous_image AKAZE was originally computed against is
                        # gone, but the saved values are still authoritative —
                        # the source images haven't changed.
                        preserved_scene = int(_coerce_float(preserved_row.get("scene_count"), scene_count))
                        entry.update(
                            {
                                "feature_similarity": preserved_feat_sim,
                                "feature_confidence": _coerce_float(preserved_row.get("feature_confidence"), -1.0),
                                "color_similarity": _coerce_float(preserved_row.get("color_similarity"), -1.0),
                                "color_confidence": _coerce_float(preserved_row.get("color_confidence"), -1.0),
                                "scene_count": preserved_scene,
                                "similar": preserved_was_similar,
                            }
                        )
                        # Keep the running scene counter monotonic so subsequent
                        # genuinely-new scenes get unique IDs.
                        scene_count = max(scene_count, preserved_scene)
                    else:
                        # No usable preserved values. If this is a retry image,
                        # reset previous_image to its alphabetical predecessor
                        # so AKAZE compares against the right neighbor instead
                        # of whatever rolled in from the last DB row.
                        if preserved_row is not None:
                            prev_idx = files_idx.get(raw_file, -1) - 1
                            if prev_idx >= 0:
                                pred_filename = files[prev_idx]
                                pred_path = os.path.join(folder, pred_filename)
                                if os.path.exists(pred_path):
                                    try:
                                        pred_img = read_image(pred_path)
                                    except Exception:
                                        pred_img = None
                                    if pred_img is not None:
                                        previous_image = pred_img
                                        previous_image_path = pred_path
                                        previous_orientation = self._get_image_orientation(pred_img)
                            else:
                                previous_image = None
                                previous_image_path = None
                                previous_orientation = None

                        timestamp_similar = None
                        try:
                            timestamp_similar = compute_similarity_timestamp(
                                previous_image_path, image_path,
                                threshold_seconds=scene_time_threshold
                            ) if previous_image_path else None
                        except Exception as e:
                            log_warning(
                                self._log_path,
                                f"Timestamp similarity check failed: {e}",
                                stage=stage_ctx["stage"],
                                context={"file": raw_file, "folder": folder},
                            )

                        orientation_changed = (
                            previous_orientation is not None
                            and current_orientation != "unknown"
                            and previous_orientation != "unknown"
                            and current_orientation != previous_orientation
                        )

                        if orientation_changed:
                            scene_count += 1
                            entry.update(
                                {
                                    "feature_similarity": -1.0,
                                    "feature_confidence": -1.0,
                                    "color_similarity": -1.0,
                                    "color_confidence": -1.0,
                                    "scene_count": scene_count,
                                    "similar": False,
                                }
                            )
                        elif timestamp_similar is True:
                            # Images captured within the same second — treat as similar, skip AKAZE
                            entry.update(
                                {
                                    "feature_similarity": -1.0,
                                    "feature_confidence": -1.0,
                                    "color_similarity": -1.0,
                                    "color_confidence": -1.0,
                                    "scene_count": scene_count,
                                    "similar": True,
                                }
                            )
                        else:
                            similarity = compute_image_similarity_akaze(previous_image, img)
                            if not similarity["similar"]:
                                scene_count += 1
                            entry.update(
                                {
                                    "feature_similarity": similarity["feature_similarity"],
                                    "feature_confidence": similarity["feature_confidence"],
                                    "color_similarity": similarity["color_similarity"],
                                    "color_confidence": similarity["color_confidence"],
                                    "scene_count": scene_count,
                                    "similar": similarity["similar"],
                                }
                            )
                    # Hold the previous image by reference, not by copy. The next
                    # iteration rebinds ``img`` to a fresh decoded array, so the
                    # old array stays alive for AKAZE via this reference. The
                    # per-detection patch-and-restore in ``process_subject_items``
                    # keeps ``img`` pristine across the rest of this iteration.
                    # Skipping a 45 MP uint8 copy (~135 MB memcpy) here closes
                    # a significant slice of the gap between "bird crop shown"
                    # and "next image thumbnail shown".
                    previous_image = img
                    previous_image_path = image_path
                    previous_orientation = current_orientation

                    stage_ctx["stage"] = "export_image"
                    export_path = os.path.join(export_dir, f"{os.path.splitext(raw_file)[0]}_export.jpg")
                    # Thumbnail must be free of baked-in exposure correction so the
                    # browser's live correction (driven by entry.exposure_correction)
                    # does not double-apply the meter_scale component. For RAW,
                    # render directly from noauto_linear (no meter scale). For
                    # non-RAW, img already has no correction applied.
                    #
                    # Downsize FIRST in linear float space, THEN apply the sRGB
                    # curve. This was the biggest single cost in the gap between
                    # "bird thumbnails shown for image N" and "thumbnail shown
                    # for image N+1" — the old code ran the full gamma LUT on
                    # the entire 45 MP linear frame before shrinking to ~1 MP,
                    # which is ~40x more work than necessary. Downsizing in
                    # linear space with INTER_AREA is also more physically
                    # correct for photographs (standard practice in any serious
                    # image pipeline — avoids edge/highlight artifacts that
                    # come from box-filtering gamma-compressed values).
                    if noauto_linear is not None:
                        src_h, src_w = noauto_linear.shape[:2]
                        _tgt_w = min(thumbnail_max_width, int(src_w))
                        _tgt_h = max(1, int(_tgt_w * src_h / src_w))
                        linear_small = cv2.resize(
                            noauto_linear, (_tgt_w, _tgt_h),
                            interpolation=cv2.INTER_AREA,
                        )
                        img_small = linear_to_srgb_u8(linear_small)
                    else:
                        src_h, src_w = img.shape[:2]
                        _tgt_w = min(thumbnail_max_width, int(src_w))
                        _tgt_h = max(1, int(_tgt_w * src_h / src_w))
                        img_small = cv2.resize(img, (_tgt_w, _tgt_h), interpolation=cv2.INTER_AREA)
                    _write_jpeg(
                        export_path,
                        cv2.cvtColor(img_small, cv2.COLOR_RGB2BGR),
                        jpeg_params,
                    )
                    # Store relative path for cross-platform compatibility
                    export_path_rel = os.path.relpath(export_path, folder)
                    entry.update({"export_path": export_path_rel})
                    if thumbnail_cb:
                        thumbnail_cb({"filename": raw_file, "thumbnail": img_small, "export_path": export_path_rel})

                    stage_ctx["stage"] = "detection_segmentation"
                    # SpeciesNet + SAM-HQ inference can take many seconds. Pause semantics are
                    # handled at the start of each image loop so we do not check
                    # repeatedly inside the image processing path.
                    # mask_threshold is accepted for API compatibility; SAM-HQ ignores it (see settings tooltip).
                    masks, pred_boxes, pred_class, pred_score = self.sn_sam.get_prediction(
                        img,
                        image_path,
                        wildlife_enabled=wildlife_enabled,
                        threshold=detection_threshold,
                        mask_threshold=mask_threshold,
                    )
                    if masks is None or len(masks) == 0:
                        if detection_cb:
                            detection_cb(
                                {
                                    "filename": raw_file,
                                    "overlay": self._create_mask_overlay(img_small, None, None),
                                    "bird_count": 0,
                                }
                            )
                        if crops_cb:
                            crops_cb({"filename": raw_file, "crops": [], "confidences": []})
                        if quality_cb:
                            quality_cb({"filename": raw_file, "results": []})
                        if species_cb:
                            species_cb({"filename": raw_file, "results": []})
                        if status_cb:
                            status_cb(f"No detections in {raw_file}")
                        stage_ctx["stage"] = "write_crop"
                        crop_path = os.path.join(crop_dir, f"{os.path.splitext(raw_file)[0]}_crop_0.jpg")
                        _write_jpeg(
                            crop_path,
                            cv2.cvtColor(img_small, cv2.COLOR_RGB2BGR),
                            jpeg_params,
                        )
                        # Store relative path for cross-platform compatibility
                        crop_path_rel = os.path.relpath(crop_path, folder)
                        h, w = img.shape[:2]
                        fallback_crop = {
                            "crop_index": 0,
                            "crop_path": crop_path_rel,
                            "detection_index": -1,
                            "detection_confidence": 0.0,
                            "species": "Unknown",
                            "species_confidence": 0.0,
                            "family": "Unknown",
                            "family_confidence": 0.0,
                            "quality": -1.0,
                            "rating": 0,
                            "exposure_correction": 0.0,
                            "exposure_pipeline": entry.get("exposure_pipeline", "legacy_auto_bright_v1"),
                            "exposure_subject_stops": 0.0,
                            "exposure_meter_scale": float(entry.get("exposure_meter_scale", 1.0)),
                            "bbox": {
                                "x_min": 0,
                                "x_max": int(w),
                                "y_min": 0,
                                "y_max": int(h),
                                "width": int(w),
                                "height": int(h),
                                "x_min_norm": 0.0,
                                "x_max_norm": 1.0,
                                "y_min_norm": 0.0,
                                "y_max_norm": 1.0,
                                "x_center_norm": 0.5,
                                "y_center_norm": 0.5,
                            },
                        }
                        entry.update(
                            {
                                "crop_path": crop_path_rel,
                                "crops_json": json.dumps([fallback_crop]),
                                "primary_crop_index": 0,
                            }
                        )
                        stage_ctx["stage"] = "save_database"
                        database = pd.concat([database, pd.DataFrame([entry])], ignore_index=True)
                        save_database(database, db_path)
                        if image_cb:
                            image_cb(entry)
                        if progress_cb:
                            progress_cb(idx + processed_count, total)
                        continue

                    # Top-5 SpeciesNet ensemble scores (per retained detection)
                    entry["detection_scores"] = json.dumps([float(s) for s in sorted(pred_score, reverse=True)[:5]])

                    bird_indices = [i for i, c in enumerate(pred_class) if c == "bird"]
                    subject_indices = list(range(len(pred_class)))

                    overlay_indices = subject_indices
                    if detection_cb:
                        detection_cb(
                            {
                                "filename": raw_file,
                                "overlay": self._create_mask_overlay(img_small, masks, overlay_indices),
                                "bird_count": len(bird_indices),
                            }
                        )

                    def process_subject_items(indices):
                        stage_ctx["stage"] = "process_subjects"
                        items = []
                        for i in indices:
                            crop_bbox = self.sn_sam.get_square_crop_box(masks[i])
                            if noauto_linear is not None:
                                # RAW numpy path — no additional rawpy calls.
                                stops = self._compute_stops_numpy(
                                    noauto_linear, masks[i], raw_meter_scale, exposure_quality
                                )
                                total_scale = float(raw_meter_scale) * (2.0 ** float(stops))
                                bbox_tuple = (
                                    crop_bbox["y_min"], crop_bbox["x_min"],
                                    crop_bbox["y_max"], crop_bbox["x_max"],
                                )
                                corrected_crop = self._apply_crop_numpy(noauto_linear, bbox_tuple, total_scale)
                                # Patch the exposure-corrected crop in place on ``img``,
                                # run the two downstream crop extractions that need
                                # full-image coordinates, then restore the original
                                # pixels before the next detection runs. This replaces
                                # a full-frame ``img.copy()`` per detection (which was
                                # ~140 MB on 45 MP RAW and was the visible lag between
                                # per-bird thumbnails in the UI) with two tiny
                                # crop-sized copies that are effectively free.
                                y0, y1 = crop_bbox["y_min"], crop_bbox["y_max"]
                                x0, x1 = crop_bbox["x_min"], crop_bbox["x_max"]
                                original_patch = img[y0:y1, x0:x1].copy()
                                img[y0:y1, x0:x1] = corrected_crop
                                try:
                                    species_crop = self.sn_sam.get_species_crop(pred_boxes[i], img)
                                    quality_crop, quality_mask = self.sn_sam.get_square_crop(masks[i], img, resize=True)
                                finally:
                                    img[y0:y1, x0:x1] = original_patch
                                total_stops = ec_compose_total_stops(stops, raw_meter_scale)
                                meter_scale = float(raw_meter_scale)
                                pipeline_mode = "numpy_linear_v2"
                            else:
                                # Non-RAW / JPEG path — no exposure correction, so no
                                # patching needed. Use the metered image directly.
                                stops = 0.0
                                total_stops = 0.0
                                meter_scale = 1.0
                                pipeline_mode = "legacy_auto_bright_v1"
                                species_crop = self.sn_sam.get_species_crop(pred_boxes[i], img)
                                quality_crop, quality_mask = self.sn_sam.get_square_crop(masks[i], img, resize=True)
                            items.append(
                                {
                                    "index": i,
                                    "confidence": float(pred_score[i]),
                                    "species_crop": species_crop,
                                    "quality_crop": quality_crop,
                                    "quality_mask": quality_mask,
                                    "stops": stops,
                                    "total_stops": total_stops,
                                    "exposure_pipeline": pipeline_mode,
                                    "exposure_subject_stops": round(float(stops), 4),
                                    "exposure_meter_scale": round(meter_scale, 6),
                                    "crop_bbox": crop_bbox,
                                }
                            )
                        if crops_cb:
                            crops_cb(
                                {
                                    "filename": raw_file,
                                    "crops": [i["quality_crop"] for i in items],
                                    "confidences": [i["confidence"] for i in items],
                                }
                            )
                        for item in items:
                            i = item["index"]
                            if pred_class[i] == "bird":
                                if species_detection_enabled:
                                    species_result = self.species_clf.classify(item["species_crop"])
                                    item["species"] = (
                                        species_result["top_species_labels"][0]
                                        if len(species_result["top_species_labels"])
                                        else "Unknown"
                                    )
                                    item["species_confidence"] = (
                                        float(species_result["top_species_scores"][0])
                                        if len(species_result["top_species_scores"])
                                        else 0.0
                                    )
                                    item["family"] = (
                                        species_result["top_family_labels"][0]
                                        if len(species_result["top_family_labels"])
                                        else "Unknown"
                                    )
                                    item["family_confidence"] = (
                                        float(species_result["top_family_scores"][0])
                                        if len(species_result["top_family_scores"])
                                        else 0.0
                                    )
                                else:
                                    item["species"] = "Unknown"
                                    item["species_confidence"] = 0.0
                                    item["family"] = "Unknown"
                                    item["family_confidence"] = 0.0
                            else:
                                item["species"] = pred_class[i]
                                item["species_confidence"] = float(pred_score[i])
                                item["family"] = "N/A"
                                item["family_confidence"] = 0.0
                            item["exposure_correction"] = round(
                                float(item.get("total_stops", item["stops"])),
                                4,
                            )
                            stage_ctx["stage"] = "quality_score"
                            quality_score = self.quality_clf.classify(item["quality_crop"], item["quality_mask"])
                            item["quality"] = quality_score
                            item["rating"] = quality_to_rating(quality_score, rating_thresholds)
                        if quality_cb:
                            quality_cb(
                                {
                                    "filename": raw_file,
                                    "results": [
                                        {"quality": i["quality"], "rating": i["rating"]} for i in items
                                    ],
                                }
                            )
                        if species_cb:
                            species_cb(
                                {
                                    "filename": raw_file,
                                    "results": [
                                        {
                                            "species": i["species"],
                                            "species_confidence": i["species_confidence"],
                                            "family": i["family"],
                                            "family_confidence": i["family_confidence"],
                                        }
                                        for i in items
                                    ],
                                }
                            )
                        return items

                    crop_items_for_write = []
                    primary_crop_index = 0
                    img_h, img_w = img.shape[:2]

                    if subject_indices:
                        subject_items = process_subject_items(subject_indices)
                        crop_data = [
                            {
                                "index": i["index"],
                                "confidence": i["confidence"],
                                "species": i["species"],
                                "species_confidence": i["species_confidence"],
                                "family": i["family"],
                                "family_confidence": i["family_confidence"],
                                "quality": i["quality"],
                                "rating": i["rating"],
                                "quality_crop": i["quality_crop"],
                                "exposure_correction": i.get("exposure_correction", 0.0),
                                "exposure_pipeline": i.get("exposure_pipeline", "legacy_auto_bright_v1"),
                                "exposure_subject_stops": i.get("exposure_subject_stops", 0.0),
                                "exposure_meter_scale": i.get("exposure_meter_scale", 1.0),
                                "crop_bbox": i.get("crop_bbox"),
                            }
                            for i in subject_items
                        ]
                        primary_crop_index = int(np.argmax([b["quality"] for b in crop_data]))
                        primary_row = crop_data[primary_crop_index]
                        entry.update(
                            {
                                "species": primary_row["species"],
                                "species_confidence": primary_row["species_confidence"],
                                "family": primary_row["family"],
                                "family_confidence": primary_row["family_confidence"],
                                "quality": primary_row["quality"],
                                "exposure_correction": primary_row["exposure_correction"],
                                "exposure_pipeline": primary_row["exposure_pipeline"],
                                "exposure_subject_stops": primary_row["exposure_subject_stops"],
                                "exposure_meter_scale": primary_row["exposure_meter_scale"],
                            }
                        )
                        all_species = [b["species"] for b in crop_data]
                        all_species_conf = [float(b["species_confidence"]) for b in crop_data]
                        all_families = [b["family"] for b in crop_data]
                        all_family_conf = [float(b["family_confidence"]) for b in crop_data]
                        entry.update(
                            {
                                "secondary_species_list": json.dumps(all_species),
                                "secondary_species_scores": json.dumps(all_species_conf),
                                "secondary_family_list": json.dumps(all_families),
                                "secondary_family_scores": json.dumps(all_family_conf),
                            }
                        )
                        crop_items_for_write = crop_data
                        # Release full-res buffers now that all crops are extracted as
                        # self-contained uint8 arrays.  img_h/img_w are already scalars;
                        # img_small is a separate 1200px array and is unaffected.
                        noauto_linear = None
                        img = np.zeros((1, 1, 3), dtype=np.uint8)
                    else:
                        if crops_cb:
                            crops_cb({"filename": raw_file, "crops": [], "confidences": []})
                        if quality_cb:
                            quality_cb({"filename": raw_file, "results": []})
                        if species_cb:
                            species_cb({"filename": raw_file, "results": []})
                        crop_items_for_write = [
                            {
                                "index": -1,
                                "confidence": 0.0,
                                "species": entry.get("species", "Unknown"),
                                "species_confidence": entry.get("species_confidence", 0.0),
                                "family": entry.get("family", "Unknown"),
                                "family_confidence": entry.get("family_confidence", 0.0),
                                "quality": entry.get("quality", -1.0),
                                "rating": 0,
                                "quality_crop": img_small,
                                "exposure_correction": entry.get("exposure_correction", 0.0),
                                "exposure_pipeline": entry.get("exposure_pipeline", "legacy_auto_bright_v1"),
                                "exposure_subject_stops": entry.get("exposure_subject_stops", 0.0),
                                "exposure_meter_scale": entry.get("exposure_meter_scale", 1.0),
                                "crop_bbox": {
                                    "x_min": 0,
                                    "x_max": int(img_w),
                                    "y_min": 0,
                                    "y_max": int(img_h),
                                    "width": int(img_w),
                                    "height": int(img_h),
                                    "x_min_norm": 0.0,
                                    "x_max_norm": 1.0,
                                    "y_min_norm": 0.0,
                                    "y_max_norm": 1.0,
                                    "x_center_norm": 0.5,
                                    "y_center_norm": 0.5,
                                },
                            }
                        ]
                        primary_crop_index = 0

                    stage_ctx["stage"] = "write_crop"
                    serialized_crops = []
                    base_name = os.path.splitext(raw_file)[0]
                    for crop_idx, crop_item in enumerate(crop_items_for_write):
                        crop_img = crop_item.get("quality_crop")
                        if crop_img is None:
                            continue
                        crop_path = os.path.join(crop_dir, f"{base_name}_crop_{crop_idx}.jpg")
                        _write_jpeg(
                            crop_path,
                            cv2.cvtColor(crop_img, cv2.COLOR_RGB2BGR),
                            jpeg_params,
                        )
                        crop_path_rel = os.path.relpath(crop_path, folder)
                        bbox = crop_item.get("crop_bbox") or {
                            "x_min": 0,
                            "x_max": int(img_w),
                            "y_min": 0,
                            "y_max": int(img_h),
                            "width": int(img_w),
                            "height": int(img_h),
                            "x_min_norm": 0.0,
                            "x_max_norm": 1.0,
                            "y_min_norm": 0.0,
                            "y_max_norm": 1.0,
                            "x_center_norm": 0.5,
                            "y_center_norm": 0.5,
                        }
                        serialized_crops.append(
                            {
                                "crop_index": int(crop_idx),
                                "crop_path": crop_path_rel,
                                "detection_index": int(crop_item.get("index", -1)),
                                "detection_confidence": float(crop_item.get("confidence", 0.0)),
                                "species": str(crop_item.get("species", "Unknown") or "Unknown"),
                                "species_confidence": float(crop_item.get("species_confidence", 0.0)),
                                "family": str(crop_item.get("family", "Unknown") or "Unknown"),
                                "family_confidence": float(crop_item.get("family_confidence", 0.0)),
                                "quality": float(crop_item.get("quality", -1.0)),
                                "rating": int(crop_item.get("rating", 0)),
                                "exposure_correction": float(crop_item.get("exposure_correction", 0.0)),
                                "exposure_pipeline": str(crop_item.get("exposure_pipeline", "legacy_auto_bright_v1") or "legacy_auto_bright_v1"),
                                "exposure_subject_stops": float(crop_item.get("exposure_subject_stops", 0.0)),
                                "exposure_meter_scale": float(crop_item.get("exposure_meter_scale", 1.0)),
                                "bbox": {
                                    "x_min": int(bbox.get("x_min", 0)),
                                    "x_max": int(bbox.get("x_max", img_w)),
                                    "y_min": int(bbox.get("y_min", 0)),
                                    "y_max": int(bbox.get("y_max", img_h)),
                                    "width": int(bbox.get("width", img_w)),
                                    "height": int(bbox.get("height", img_h)),
                                    "x_min_norm": float(bbox.get("x_min_norm", 0.0)),
                                    "x_max_norm": float(bbox.get("x_max_norm", 1.0)),
                                    "y_min_norm": float(bbox.get("y_min_norm", 0.0)),
                                    "y_max_norm": float(bbox.get("y_max_norm", 1.0)),
                                    "x_center_norm": float(bbox.get("x_center_norm", 0.5)),
                                    "y_center_norm": float(bbox.get("y_center_norm", 0.5)),
                                },
                            }
                        )

                    if not serialized_crops:
                        raise RuntimeError("No crop records generated for image")

                    primary_crop_index = int(np.clip(primary_crop_index, 0, len(serialized_crops) - 1))
                    primary_crop = serialized_crops[primary_crop_index]
                    entry.update(
                        {
                            "species": primary_crop["species"],
                            "species_confidence": primary_crop["species_confidence"],
                            "family": primary_crop["family"],
                            "family_confidence": primary_crop["family_confidence"],
                            "quality": primary_crop["quality"],
                            "exposure_correction": primary_crop["exposure_correction"],
                            "exposure_pipeline": primary_crop.get("exposure_pipeline", "legacy_auto_bright_v1"),
                            "exposure_subject_stops": primary_crop.get("exposure_subject_stops", 0.0),
                            "exposure_meter_scale": primary_crop.get("exposure_meter_scale", 1.0),
                            "crop_path": primary_crop["crop_path"],
                            "crops_json": json.dumps(serialized_crops),
                            "primary_crop_index": primary_crop_index,
                        }
                    )

                    stage_ctx["stage"] = "save_database"
                    database = pd.concat([database, pd.DataFrame([entry])], ignore_index=True)
                    save_database(database, db_path)

                    if image_cb:
                        image_cb(entry)

                    if status_cb:
                        _q = entry.get('quality', -1)
                        _display_q = f"{float(_q):.3f}" if _q not in (None, 'N/A', -1) else '—'
                        status_cb(
                            f"Processed {raw_file}: {entry['species']} Q={_display_q}"
                            f" ({idx + processed_count}/{total})"
                        )
                except Exception as e:
                    log_exception(
                        self._log_path,
                        e,
                        stage=stage_ctx["stage"],
                        context={
                            "file": raw_file,
                            "folder": folder,
                            "image_path": image_path,
                            "analyzer": analyzer_name,
                        },
                    )
                    if error_cb:
                        error_cb(raw_file, e)
                    if status_cb:
                        status_cb(f"Error {raw_file}: {e}")
                    entry["scene_count"] = scene_count
                    entry["species"] = "Error"
                    entry["similar"] = False
                    database = pd.concat([database, pd.DataFrame([entry])], ignore_index=True)
                    save_database(database, db_path)
                    time.sleep(2)

                if progress_cb:
                    progress_cb(idx + processed_count, total)

                # Explicitly clear large temporary variables after each image
                # so that pausing between images doesn't retain large buffers.
                # raw_obj is owned by _decode_image (closed there); not present here.
                try:
                    try: del masks
                    except Exception: pass
                    try: del pred_boxes
                    except Exception: pass
                    try: del pred_class
                    except Exception: pass
                    try: del pred_score
                    except Exception: pass
                    try: del img
                    except Exception: pass
                    try: del crop_img
                    except Exception: pass
                    try: del items
                    except Exception: pass
                    try: del subject_items
                    except Exception: pass
                except Exception:
                    pass

            # === Post-analysis: persist database + audit-trail metadata + scene grouping ===
            stage_ctx["stage"] = "post_analysis_normalization"
            try:
                if not database.empty and "quality" in database.columns:
                    save_database(database, db_path)

                    # Update kestrel_metadata.json with the analysis-run audit trail
                    # (render mode, settings snapshot). The frontend reads
                    # exposure_render_mode / exposure_pipeline_version to pick the
                    # correct RAW preview path; the rest is a record of which
                    # parameters produced the cached results, intended for users
                    # who later want to see what settings produced this folder.
                    metadata_path = os.path.join(kestrel_dir, METADATA_FILENAME)
                    try:
                        import json as _json
                        from datetime import datetime as _dt, timezone as _tz
                        if os.path.exists(metadata_path):
                            with open(metadata_path, "r", encoding="utf-8") as mf:
                                _meta = _json.load(mf)
                        else:
                            _meta = {"version": VERSION, "analyzer": analyzer_name}

                        pipeline_modes = set()
                        if "exposure_pipeline" in database.columns:
                            for _mode in database["exposure_pipeline"].tolist():
                                _mode_txt = str(_mode or "").strip().lower()
                                if _mode_txt:
                                    pipeline_modes.add(_mode_txt)
                        if pipeline_modes == {"numpy_linear_v2"}:
                            render_mode_meta = "numpy_linear_v2"
                        elif pipeline_modes == {"legacy_auto_bright_v1"}:
                            render_mode_meta = "legacy_auto_bright_v1"
                        elif pipeline_modes:
                            render_mode_meta = "mixed_per_row_v1"
                        else:
                            render_mode_meta = "legacy_auto_bright_v1"

                        _meta["exposure_pipeline_version"] = 3
                        _meta["exposure_render_mode"] = render_mode_meta
                        _meta["exposure_quality"] = exposure_quality

                        _meta["analyzed_utc"] = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                        _meta["kestrel_version"] = VERSION
                        _meta["analysis_settings"] = {
                            "detector_name": str(getattr(self, "detector_name", "") or ""),
                            "use_gpu": bool(getattr(self, "use_gpu", False)),
                            "wildlife_enabled": bool(wildlife_enabled),
                            "species_detection_enabled": bool(species_detection_enabled),
                            "detection_threshold": float(detection_threshold),
                            "scene_time_threshold": float(scene_time_threshold),
                            "mask_threshold": float(mask_threshold),
                            "max_bird_crops": int(max_bird_crops),
                            "parallel_prefetch": int(decode_workers),
                            "rating_profile": rating_profile,
                            "exposure_quality": exposure_quality,
                            "exposure_render_mode": render_mode_meta,
                            "exposure_pipeline_version": 3,
                            "thumbnail_max_width": int(thumbnail_max_width),
                            "thumbnail_jpeg_compression": float(round(thumbnail_jpeg_compression, 4)),
                            "thumbnail_jpeg_quality": int(thumbnail_jpeg_quality),
                        }
                        with open(metadata_path, "w", encoding="utf-8") as mf:
                            _json.dump(_meta, mf, indent=2)
                    except Exception as _meta_e:
                        log_warning(
                            self._log_path,
                            f"Failed to write analysis metadata: {_meta_e}",
                            stage="post_analysis_normalization",
                        )

                    # Create or update kestrel_scenedata.json
                    try:
                        existing_scenedata = load_scenedata(kestrel_dir)
                        if not existing_scenedata.get("scenes"):
                            new_scenedata = build_scenedata_from_database(database)
                            save_scenedata(new_scenedata, kestrel_dir)
                        else:
                            update_scenedata_with_database(existing_scenedata, database)
                            save_scenedata(existing_scenedata, kestrel_dir)
                    except Exception as _sd_e:
                        log_warning(
                            self._log_path,
                            f"Failed to create/update kestrel_scenedata.json: {_sd_e}",
                            stage="post_analysis_normalization",
                        )

                log_event(
                    self._log_path,
                    {
                        "level": "info",
                        "event": "analysis_complete",
                        "folder": folder,
                        "total_files": len(database),
                    },
                )
                if status_cb:
                    status_cb("Analysis complete.")
            except Exception as _post_e:
                log_warning(
                    self._log_path,
                    f"Post-analysis normalization failed: {_post_e}",
                    stage="post_analysis_normalization",
                )

        except Exception as e:
            log_exception(
                self._log_path,
                e,
                stage=stage_ctx["stage"],
                context={"folder": folder, "analyzer": analyzer_name},
            )
            if status_cb:
                status_cb(f"Fatal error: {e}")
            if error_cb:
                error_cb("fatal", e)
        finally:
            warnings.showwarning = original_showwarning
