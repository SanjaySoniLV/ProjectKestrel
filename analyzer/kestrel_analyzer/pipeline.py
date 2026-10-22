import concurrent.futures
import json
import os
import queue
import threading
import time
import traceback
import warnings
from typing import Callable, Dict, Generator, List, Optional

import cv2
import numpy as np
import pandas as pd

from .config import (
    DEFAULT_DETECTOR_NAME,
    BIOCLIP_MODELS_DIR,
    PIPELINE_MAJOR,
    LEGACY_QUALITY_MODEL_ID,
    QUALITYCLASSIFIER_PATH,
    QUALITY_NORMALIZATION_DATA_PATH,
    MODELS_DIR,
    KESTREL_DIR_NAME,
    METADATA_FILENAME,
    VERSION,
    select_camera_images,
)
from .database import (
    load_database,
    save_database,
    finalize_scenedata_after_analysis,
    parse_file_size,
    DATABASE_SCHEMA_VERSION,
)
from .exposure_compensation import (
    apply_exposure_crop_numpy,
    build_metered_detection_image,
    compose_total_stops as ec_compose_total_stops,
    compute_stops_numpy_solver,
    linear_to_srgb_u8,
)
from .image_utils import decode_embedded_preview, read_image, read_image_for_pipeline
from .ratings import quality_to_rating, resolve_thresholds
from .similarity import compute_image_similarity_akaze, compute_similarity_timestamp
from .raw_exif import get_capture_time
from .logging_utils import (
    get_log_path,
    log_event,
    log_exception,
    log_warning,
    make_logged_showwarning,
)

try:
    from ..settings_utils import load_persisted_settings, debug as _debug, info as _info, error as _error
except (ImportError, ValueError):
    # Top-level package case: cli.py adds analyzer/ to sys.path and imports
    # ``kestrel_analyzer`` as a root package, so the relative ``..`` walks
    # beyond it. The bare-name fallback works because ``settings_utils`` is
    # then directly importable from sys.path.
    try:
        from settings_utils import load_persisted_settings, debug as _debug, info as _info, error as _error  # type: ignore
    except ImportError:
        load_persisted_settings = None
        def _debug(*_a, **_kw): pass
        def _info(*_a, **_kw): pass
        def _error(*_a, **_kw): pass
from .ml import GPU_EP
from .ml.speciesnet_sam_hq import DetectorSAMHQWrapper
from .ml.provider_coordinator import ResilienceConfig
from .ml.bioclip_species import BioClipSpeciesClassifier, MODEL_ID as SPECIES_MODEL_ID
from .ml.animal_gate import AnimalGate, GATE_METHOD
from .ml import siglip
from .ml import providers as provider_diag
from .bird_catalog import resolve_species_regions
from . import embedding_store
from . import quality_model as quality_model_mod
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
    # Class-level defaults so instances built with ``__new__`` (tests) work.
    siglip_img = None
    siglip_quality = None
    animal_gate = None

    def __init__(self, use_gpu: bool, detector_name: str = DEFAULT_DETECTOR_NAME):
        self.use_gpu = use_gpu
        self.detector_name = str(detector_name)
        self.sn_sam: Optional[DetectorSAMHQWrapper] = None
        self.species_clf: Optional[BioClipSpeciesClassifier] = None
        self.quality_clf: Optional[QualityClassifier] = None
        # Pipeline 3.0.0: SigLIP 2 image tower (search embeddings + default
        # quality) and the BioCLIP false-positive gate. Both best-effort: a
        # missing asset logs a warning and analysis carries on (quality then
        # falls back to the legacy CNN, every MegaDetector box is kept).
        self.siglip_img: Optional[siglip.SiglipImageEmbedder] = None
        self.siglip_quality: Optional[siglip.SiglipQualityScorer] = None
        self.animal_gate: Optional[AnimalGate] = None
        self._log_path: Optional[str] = None
        # Per-stage wall time (seconds) + call counts for tools/bench_pipeline.py.
        self.stage_times: Dict[str, float] = {}
        self.stage_counts: Dict[str, int] = {}
        # load_models() can be reached from the queue worker and from the
        # v3-upgrade job (which borrows this pipeline); serialise it so the
        # models are built once.
        self._load_lock = threading.RLock()

    def _timing_add(self, stage: str, t0: float) -> None:
        if "stage_times" not in self.__dict__:
            self.stage_times, self.stage_counts = {}, {}
        self.stage_times[stage] = self.stage_times.get(stage, 0.0) + (time.perf_counter() - t0)
        self.stage_counts[stage] = self.stage_counts.get(stage, 0) + 1

    def reset_timings(self) -> None:
        self.stage_times = {}
        self.stage_counts = {}

    def _load_v3_extras(self, coord) -> None:
        """SigLIP image tower + quality scorer + animal gate (each best-effort)."""
        if self.siglip_img is None:
            if siglip.image_assets_available():
                try:
                    self.siglip_img = siglip.SiglipImageEmbedder(siglip.default_assets_dir(), coord)
                except Exception as e:
                    log_warning(self._log_path, f"[siglip] image tower failed to load: {type(e).__name__}: {e}",
                                stage="load_models")
            else:
                log_warning(self._log_path, "[siglip] image tower asset missing; quality falls back to the legacy CNN",
                            stage="load_models")
        if self.siglip_quality is None and self.siglip_img is not None:
            try:
                self.siglip_quality = siglip.SiglipQualityScorer()
            except Exception as e:
                log_warning(self._log_path, f"[siglip] quality calibration failed to load: {type(e).__name__}: {e}",
                            stage="load_models")
        if self.animal_gate is None:
            try:
                self.animal_gate = AnimalGate()
            except Exception as e:
                log_warning(self._log_path, f"[gate] animal gate unavailable, keeping every detection: {e}",
                            stage="load_models")

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
        stop_event = threading.Event()

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
                    if stop_event.is_set():
                        break
                    semaphore.acquire()
                    if stop_event.is_set():
                        # Consumer abandoned the generator (cancel / fatal error)
                        # while we waited for a free buffer slot; stop submitting
                        # so the pool can drain and shut down.
                        break
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

        try:
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
        finally:
            # If the consumer abandons this generator early -- an outer cancel or
            # a fatal error makes process_folder return, which raises
            # GeneratorExit at the yield above -- the loop stops releasing the
            # semaphore. Signal the producer to stop and free one slot so it
            # unblocks from semaphore.acquire(), lets its ThreadPoolExecutor
            # drain, and exits. Without this the decode-worker threads (and their
            # buffered full-res images) leak for the rest of the process.
            stop_event.set()
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

    def _save_database_soft(self, database, db_path: str, stage: str) -> bool:
        """``save_database`` whose failure skips the save instead of ending the run.

        ``save_database`` re-reads the CSV to recover the user's ``culled`` marks
        before overwriting it, and raises if that read fails. Raising is the right
        contract -- writing without the merge would discard those marks -- but the
        failure it raises on is a Windows sharing violation that
        ``retry_on_file_lock`` documents as routine, and this runs after *every*
        image. Letting it propagate meant one transient lock could end an entire
        run, and worse: the per-image handler caught it, recorded the photo as
        ``species="Error"`` though the photo decoded fine, then called save again
        from inside its own ``except`` where nothing was left to catch the second
        raise.

        Skipping is safe because ``database`` is cumulative and every save writes
        it whole -- the next image's save persists whatever this one missed. Only
        the on-disk copy lags, by a photo or two, until a save succeeds.

        The post-analysis save deliberately does NOT come through here: it is the
        last one, has no successor to cover for it, and must surface.

        Returns True if the save landed.
        """
        try:
            save_database(database, db_path)
            return True
        except Exception as exc:
            log_exception(
                self._log_path,
                exc,
                stage=stage,
                context={"db_path": db_path, "soft": True},
            )
            _error(
                f"[pipeline] save_database skipped at stage={stage}: "
                f"{type(exc).__name__}: {exc}. Results are held in memory and "
                f"will be written by the next successful save."
            )
            return False

    def _log_resolved_providers(self) -> None:
        """Log the execution providers ONNX Runtime actually chose.

        ``use_gpu`` on ``analysis_start`` records what was *requested*; this
        records what was *granted*. They differ whenever a GPU provider fails
        to initialise and the coordinator falls back to CPU, and that gap is
        the thing crash reports currently cannot show. Purely diagnostic —
        every lookup is defensive so a missing attribute can never fail a run.
        """
        providers = {}
        for name, holder, attr in (
            ("quality", self.quality_clf, "providers_used"),
            ("species", self.species_clf, "providers_used"),
            ("siglip", self.siglip_img, "providers_used"),
        ):
            try:
                value = getattr(holder, attr, None)
                if value:
                    providers[name] = list(value)
            except Exception:
                continue
        try:
            log_event(
                self._log_path,
                {
                    "level": "info",
                    "event": "models_loaded",
                    "use_gpu": bool(self.use_gpu),
                    "gpu_ep": GPU_EP,
                    "providers": providers,
                    # Requested vs actual providers + per-EP node placement for
                    # every session built so far (see ml/providers.py).
                    "placement": provider_diag.placement_summary(),
                },
            )
        except Exception:
            pass

    def load_models(
        self,
        status_cb: Optional[Callable[[str], None]] = None,
        max_bird_crops: int = 5,
    ) -> None:
        lock = getattr(self, "_load_lock", None)
        if lock is None:
            lock = self._load_lock = threading.RLock()
        with lock:
            self._load_models_locked(status_cb, max_bird_crops)

    def _load_models_locked(
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
            self._load_v3_extras(self.sn_sam.coord)
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
            self.sn_sam = DetectorSAMHQWrapper(
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
            self.siglip_img = None
        # Share the wrapper's coordinator with the species/quality classifiers
        # so demote/promote rebuilds every session in the run together. Without
        # this, a DML/CoreML failure recovers the wrapper but leaves these two
        # sessions on the dead provider — every later image errors at the
        # species step and the run looks "stuck on error".
        coord = self.sn_sam.coord
        if not self.species_clf:
            self.species_clf = BioClipSpeciesClassifier(str(BIOCLIP_MODELS_DIR), coord)
        if not self.quality_clf:
            self.quality_clf = QualityClassifier(
                str(QUALITYCLASSIFIER_PATH),
                normalization_data_path=str(QUALITY_NORMALIZATION_DATA_PATH),
                coord=coord,
            )
        self._load_v3_extras(coord)
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
        species_regions: Optional[List[str]] = None,
        smart_search_enabled: Optional[bool] = None,
        quality_model: Optional[str] = None,
    ) -> None:
        """Analyse ``folder``.

        Pipeline 3.0.0 always stores the BioCLIP and SigLIP crop embeddings
        (the SigLIP one is also the default quality score).
        ``smart_search_enabled`` additionally embeds the downscaled whole
        frame with SigLIP for context queries ("bird on a wire", "wide
        landscape"); ``None`` falls back to the persisted setting (default on).

        ``quality_model`` (``"siglip"`` | ``"legacy"``) picks which stored
        score becomes the effective ``quality``; ``None`` = the persisted
        ``quality_model`` setting (default ``"siglip"``).

        ``species_regions`` geofences the bird species classifier: a list of
        ``bird_catalog.ALLOWED_REGION_CODES``, or ``["global"]`` for every IOC
        world species. ``None`` falls back to the persisted ``bird_regions``
        setting (default ``["NA"]``).
        """
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
                rating_thresholds = resolve_thresholds(
                    rating_profile, sett.get('rating_thresholds_custom')
                )
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
        if species_regions is None and callable(load_persisted_settings):
            try:
                species_regions = (load_persisted_settings() or {}).get("bird_regions")
            except Exception:
                species_regions = None
        species_regions = resolve_species_regions(species_regions)
        if smart_search_enabled is None:
            smart_search_enabled = True
            if callable(load_persisted_settings):
                try:
                    smart_search_enabled = bool(
                        (load_persisted_settings() or {}).get("smart_search_enabled", True)
                    )
                except Exception:
                    smart_search_enabled = True
        smart_search_enabled = bool(smart_search_enabled)
        quality_model = (
            quality_model_mod.resolve(quality_model)
            if quality_model is not None
            else quality_model_mod.settings_quality_model()
        )
        thumbnail_jpeg_compression = max(0.5, min(1.0, thumbnail_jpeg_compression))
        thumbnail_jpeg_quality = int(round(thumbnail_jpeg_compression * 100.0))
        thumbnail_jpeg_quality = max(50, min(100, thumbnail_jpeg_quality))
        jpeg_params = [int(cv2.IMWRITE_JPEG_QUALITY), thumbnail_jpeg_quality]

        self._log_path = get_log_path(folder)
        stage_ctx = {"stage": "startup", "file": None}

        def _mark_stage(stage: str) -> None:
            """Record a one-time setup stage, and persist it to the analysis log.

            A native-level process death (segfault/abort inside onnxruntime,
            LibRaw, OpenCV) never unwinds to an ``except`` block, so the
            ``stage_ctx`` that ``log_exception`` reports is lost in exactly the
            crashes hardest to diagnose. Writing each transition means the last
            record in the log names the stage the process died in.

            Only the ONCE-PER-RUN setup stages call this. ``log_event`` reads,
            re-serialises and rewrites the whole JSON log on every call, so
            marking the per-file stages (``read_image``, ``compute_similarity``)
            would make analysis O(n^2) in file count. Those stages still update
            ``stage_ctx`` for exception reporting, they just are not persisted.
            """
            stage_ctx["stage"] = stage
            try:
                log_event(
                    self._log_path,
                    {"level": "debug", "event": "stage", "stage": stage},
                )
            except Exception:
                # Instrumentation must never break an analysis run.
                pass

        original_showwarning = warnings.showwarning
        warnings.showwarning = make_logged_showwarning(
            self._log_path,
            stage_ctx,
            folder=folder,
            original_showwarning=original_showwarning,
        )

        try:
            _mark_stage("list_files")
            # scandir rather than listdir + isfile: identical syscall count on
            # POSIX and strictly fewer on Windows (the stat data comes back with
            # the directory enumeration), and it yields each file's size, which
            # the skip test below needs to tell a re-shot IMG_0005.CR3 from the
            # IMG_0005.CR3 already in the database.
            disk_sizes: Dict[str, int] = {}
            with os.scandir(folder) as _it:
                for _entry in _it:
                    try:
                        if not _entry.is_file():
                            continue
                        disk_sizes[_entry.name] = _entry.stat().st_size
                    except OSError:
                        # Present but unmeasurable — keep it, size unknown, so
                        # it is still analysed rather than silently dropped.
                        disk_sizes[_entry.name] = -1
            files = select_camera_images(list(disk_sizes.keys()))
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
                    "use_gpu": bool(self.use_gpu),
                    "gpu_ep": GPU_EP,
                },
            )

            _mark_stage("create_kestrel_dirs")
            kestrel_dir = os.path.join(folder, KESTREL_DIR_NAME)
            export_dir = os.path.join(kestrel_dir, "export")
            crop_dir = os.path.join(kestrel_dir, "crop")
            os.makedirs(export_dir, exist_ok=True)
            os.makedirs(crop_dir, exist_ok=True)

            _mark_stage("load_database")
            database, db_path = load_database(kestrel_dir, analyzer_name, log_path=self._log_path)
            emb_store = embedding_store.EmbeddingStore(
                kestrel_dir, model=SPECIES_MODEL_ID, log_path=self._log_path
            )
            # SigLIP slots: crop embeddings (always) and whole-frame embeddings
            # (smart search). The frame slot is opened when enabled, or when it
            # already exists so re-analysed / dropped files still get their
            # stale rows removed while the feature is switched off.
            sig_crop_store = siglip.open_store(kestrel_dir, slot=siglip.MODEL_ID, log_path=self._log_path)
            frame_store = None
            if smart_search_enabled or os.path.isdir(siglip.store_dir(kestrel_dir, siglip.FRAME_SLOT)):
                frame_store = siglip.open_store(kestrel_dir, slot=siglip.FRAME_SLOT, log_path=self._log_path)
            _all_stores = [st for st in (emb_store, sig_crop_store, frame_store) if st is not None]

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
                    self._save_database_soft(database, db_path, "retry_errored_prune")
                    for _st in _all_stores:
                        _st.remove(errored_rows_by_filename.keys())

            # ── Identity is (filename, file_size), not filename alone ────────
            # Keying only on filename made the pipeline skip genuinely new
            # photos: empty an SD card, shoot again, and the recycled
            # IMG_0005.CR3 matched the old row, so it was never analysed while
            # the UI kept showing the previous shoot's metadata for it.
            #
            # An unknown size on EITHER side falls back to filename-only
            # matching, so rows written before this column existed (stored '')
            # and files whose stat failed (disk -1) behave exactly as before.
            stored_sizes: Dict[str, "int | None"] = {}
            _has_size_col = "file_size" in database.columns
            for _i, _fn in enumerate(database["filename"].values):
                _raw = database["file_size"].values[_i] if _has_size_col else None
                stored_sizes[str(_fn)] = parse_file_size(_raw)

            # Same name on disk, provably different bytes: a different photo
            # wearing a recycled filename. Its stale row is dropped below so the
            # re-analysis appends a clean one instead of duplicating the name.
            superseded: List[str] = []
            new_files: List[str] = []
            for f in files:
                if f not in stored_sizes:
                    new_files.append(f)
                    continue
                _stored = stored_sizes[f]
                _disk = disk_sizes.get(f, -1)
                if _stored is not None and _disk >= 0 and _stored != _disk:
                    superseded.append(f)
                    new_files.append(f)

            if superseded:
                if status_cb:
                    status_cb(
                        f"{len(superseded)} file(s) changed since the last analysis "
                        f"— re-analyzing."
                    )
                log_event(
                    self._log_path,
                    {
                        "level": "info",
                        "event": "superseded_rows_dropped",
                        "folder": folder,
                        "count": len(superseded),
                    },
                )
                _sup = set(superseded)
                database = database.loc[
                    ~database["filename"].astype(str).isin(_sup)
                ].copy()
                self._save_database_soft(database, db_path, "superseded_prune")
                for _st in _all_stores:
                    _st.remove(_sup)
                stored_sizes = {
                    k: v for k, v in stored_sizes.items() if k not in _sup
                }

            # Backfill file_size for rows that predate the column. This is the
            # one place a legacy folder's identity gets upgraded, and it is safe
            # to write here because analysis is explicitly user-initiated and
            # the folder is already being enumerated — a passive background scan
            # must never rewrite a database just to add a column.
            _backfilled = 0
            if not database.empty:
                _sizes_out = []
                for _fn in database["filename"].astype(str).values:
                    _cur = stored_sizes.get(_fn)
                    if _cur is None:
                        _disk = disk_sizes.get(_fn, -1)
                        if _disk >= 0:
                            _backfilled += 1
                            _sizes_out.append(_disk)
                            continue
                        _sizes_out.append("")
                    else:
                        _sizes_out.append(_cur)
                if _backfilled:
                    database["file_size"] = _sizes_out
                    self._save_database_soft(database, db_path, "file_size_backfill")
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

            _mark_stage("load_models")
            self.load_models(status_cb=status_cb, max_bird_crops=max_bird_crops)
            siglip_errors = [0]
            gate_stats = {"dropped": 0}
            if self.siglip_img is None:
                frame_store_effective = None
            else:
                frame_store_effective = frame_store if smart_search_enabled else None
            self._log_resolved_providers()
            if species_detection_enabled and self.species_clf is not None:
                self.species_clf.set_regions(species_regions)
                log_event(
                    self._log_path,
                    {
                        "level": "info",
                        "event": "species_geofence",
                        "regions": list(self.species_clf.regions),
                        "species_count": int(self.species_clf.allowed_count),
                    },
                )

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
                    "quality_siglip": -1.0,
                    "quality_legacy": -1.0,
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
                    # Half of the row's identity. Blank when the entry could not
                    # be measured at listing time, which keeps it "unknown"
                    # rather than asserting a size that was never observed.
                    "file_size": (
                        disk_sizes.get(raw_file, -1)
                        if disk_sizes.get(raw_file, -1) >= 0
                        else ""
                    ),
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
                    # Live-status visibility for the embedded-preview fallback
                    # (Nikon HE NEFs etc.). The "Processed" message below
                    # overwrites this almost immediately for normal files;
                    # showing it here surfaces the warning in the Live
                    # dialog's status line during heavy ML work for the same
                    # file, where the user is most likely to notice it.
                    used_preview_fallback = (
                        decoded.get("exposure_pipeline") == "embedded_preview_jpeg"
                    )
                    if decoded.get("meter_warning"):
                        log_warning(
                            self._log_path,
                            decoded["meter_warning"],
                            stage=stage_ctx["stage"],
                            context={"file": raw_file, "folder": folder},
                        )
                        if used_preview_fallback and status_cb:
                            status_cb(
                                f"⚠ {raw_file}: LibRaw can't decompress "
                                "sensor data; using embedded JPEG preview"
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

                    # Smart search: one SigLIP embedding of the downscaled whole
                    # frame (the export JPEG's pixels), for context queries.
                    frame_emb = None
                    if frame_store_effective is not None:
                        stage_ctx["stage"] = "siglip_frame"
                        _t = time.perf_counter()
                        try:
                            frame_emb = self.sn_sam.run_with_resilience(
                                lambda: self.siglip_img.embed(img_small)[0]
                            )
                        except Exception as _fe:
                            siglip_errors[0] += 1
                            if siglip_errors[0] <= 3:
                                log_warning(
                                    self._log_path,
                                    f"[siglip] frame embed failed for {raw_file}: {type(_fe).__name__}: {_fe}",
                                    stage="siglip_frame",
                                )
                        self._timing_add("siglip_frame", _t)

                    stage_ctx["stage"] = "detection_segmentation"
                    # MegaDetector + SAM-HQ. Every returned box is an "animal"
                    # proposal at/above the detection threshold; the BioCLIP
                    # gate + routing below decide what it is (pipeline 3.0.0).
                    # mask_threshold is accepted for API compatibility; SAM-HQ ignores it (see settings tooltip).
                    _t_stage = time.perf_counter()
                    masks, pred_boxes, pred_class, pred_score = self.sn_sam.get_prediction(
                        img,
                        image_path,
                        wildlife_enabled=wildlife_enabled,
                        threshold=detection_threshold,
                        mask_threshold=mask_threshold,
                    )
                    self._timing_add("detector_sam", _t_stage)

                    def extract_crop(i):
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
                            # take the square crop, then restore the original pixels
                            # before the next detection runs (two crop-sized copies
                            # instead of a full-frame ``img.copy()`` per detection).
                            y0, y1 = crop_bbox["y_min"], crop_bbox["y_max"]
                            x0, x1 = crop_bbox["x_min"], crop_bbox["x_max"]
                            original_patch = img[y0:y1, x0:x1].copy()
                            img[y0:y1, x0:x1] = corrected_crop
                            try:
                                quality_crop, quality_mask = self.sn_sam.get_square_crop(masks[i], img, resize=True)
                            finally:
                                img[y0:y1, x0:x1] = original_patch
                            total_stops = ec_compose_total_stops(stops, raw_meter_scale)
                            meter_scale = float(raw_meter_scale)
                            pipeline_mode = "numpy_linear_v2"
                        else:
                            # Non-RAW / JPEG path — no exposure correction.
                            stops = 0.0
                            total_stops = 0.0
                            meter_scale = 1.0
                            pipeline_mode = "legacy_auto_bright_v1"
                            quality_crop, quality_mask = self.sn_sam.get_square_crop(masks[i], img, resize=True)
                        return {
                            "index": i,
                            "confidence": float(pred_score[i]),
                            "quality_crop": quality_crop,
                            "quality_mask": quality_mask,
                            "stops": stops,
                            "total_stops": total_stops,
                            "exposure_pipeline": pipeline_mode,
                            "exposure_subject_stops": round(float(stops), 4),
                            "exposure_meter_scale": round(meter_scale, 6),
                            "crop_bbox": crop_bbox,
                        }

                    def gate_and_route(indices):
                        """Crop + BioCLIP-embed every proposal once, then (from that one
                        embedding) drop false positives and route bird vs wildlife.
                        Applies the per-class ``max_bird_crops`` cap in detector-
                        confidence order. Birds first, then wildlife (as before)."""
                        stage_ctx["stage"] = "process_subjects"
                        items = [extract_crop(i) for i in indices]
                        birds, wild = [], []
                        for it in items:
                            stage_ctx["stage"] = "bioclip_embed"
                            _t = time.perf_counter()
                            it["embedding"] = self.sn_sam.run_with_resilience(
                                lambda _c=it["quality_crop"]: self.species_clf.embed(_c)[0]
                            )
                            self._timing_add("bioclip", _t)
                            stage_ctx["stage"] = "gate_route"
                            _t = time.perf_counter()
                            score = self.animal_gate.animal_score(it["embedding"]) if self.animal_gate else 1.0
                            it["animal_score"] = float(score)
                            if self.animal_gate is not None and not self.animal_gate.keep_score(score, detection_threshold):
                                gate_stats["dropped"] += 1
                                self._timing_add("gate_route", _t)
                                continue
                            it["route"] = self.species_clf.route(it["embedding"])
                            self._timing_add("gate_route", _t)
                            if it["route"]["route"] == "wildlife":
                                if wildlife_enabled and len(wild) < max_bird_crops:
                                    wild.append(it)
                            elif len(birds) < max_bird_crops:
                                birds.append(it)
                        return birds + wild

                    subject_items = []
                    if masks is not None and len(masks) > 0:
                        subject_items = gate_and_route(list(range(len(pred_class))))

                    if not subject_items:
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
                        _t_stage = time.perf_counter()
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
                        self._save_database_soft(database, db_path, "save_database")
                        if frame_store_effective is not None and frame_emb is not None:
                            frame_store.put(raw_file, {0: frame_emb})
                        self._timing_add("writes", _t_stage)
                        if image_cb:
                            image_cb(entry)
                        if progress_cb:
                            progress_cb(idx + processed_count, total)
                        continue

                    # Detector confidences of the kept subjects (top 5).
                    entry["detection_scores"] = json.dumps(
                        [float(s) for s in sorted((it["confidence"] for it in subject_items), reverse=True)[:5]]
                    )
                    n_birds = sum(1 for it in subject_items if it["route"]["route"] == "bird")
                    if detection_cb:
                        detection_cb(
                            {
                                "filename": raw_file,
                                "overlay": self._create_mask_overlay(
                                    img_small, masks, [it["index"] for it in subject_items]
                                ),
                                "bird_count": n_birds,
                            }
                        )

                    def process_subject_items(items):
                        stage_ctx["stage"] = "process_subjects"
                        if crops_cb:
                            crops_cb(
                                {
                                    "filename": raw_file,
                                    "crops": [i["quality_crop"] for i in items],
                                    "confidences": [i["confidence"] for i in items],
                                }
                            )
                        for item in items:
                            route = item["route"]
                            if route["route"] == "bird":
                                if species_detection_enabled:
                                    species_result = self.species_clf.classify_embedding(item["embedding"])
                                    labels = species_result["top_species_labels"]
                                    scores = species_result["top_species_scores"]
                                    fams = species_result["top_family_labels"]
                                    fscores = species_result["top_family_scores"]
                                    item["species"] = labels[0] if len(labels) else "Unknown"
                                    item["species_confidence"] = float(scores[0]) if len(scores) else 0.0
                                    item["family"] = fams[0] if len(fams) else "Unknown"
                                    item["family_confidence"] = float(fscores[0]) if len(fscores) else 0.0
                                else:
                                    item["species"] = "Unknown"
                                    item["species_confidence"] = 0.0
                                    item["family"] = "Unknown"
                                    item["family_confidence"] = 0.0
                            else:
                                # Same CSV semantics as the SpeciesNet path: title-cased
                                # common name, family "N/A" (wildlife families are never
                                # scene tags); the taxonomy goes in the crop record.
                                item["species"] = route["wildlife_label"] or "Unknown"
                                item["species_confidence"] = float(route["wildlife_confidence"])
                                item["family"] = "N/A"
                                item["family_confidence"] = 0.0
                            item["exposure_correction"] = round(
                                float(item.get("total_stops", item["stops"])),
                                4,
                            )
                            q_siglip = -1.0
                            if self.siglip_img is not None:
                                stage_ctx["stage"] = "siglip_crop"
                                _t = time.perf_counter()
                                try:
                                    sig = self.sn_sam.run_with_resilience(
                                        lambda _c=item["quality_crop"]: self.siglip_img.embed(_c)[0]
                                    )
                                    item["siglip_embedding"] = sig
                                    if self.siglip_quality is not None:
                                        q_siglip = float(self.siglip_quality.score(sig))
                                except Exception as _sig_e:
                                    siglip_errors[0] += 1
                                    if siglip_errors[0] <= 3:
                                        log_warning(
                                            self._log_path,
                                            f"[siglip] crop embed failed for {raw_file}: "
                                            f"{type(_sig_e).__name__}: {_sig_e}",
                                            stage="siglip_crop",
                                        )
                                self._timing_add("siglip_crop", _t)
                            stage_ctx["stage"] = "quality_score"
                            _t = time.perf_counter()
                            q_legacy = float(self.quality_clf.classify(item["quality_crop"], item["quality_mask"]))
                            self._timing_add("legacy_quality", _t)
                            item["quality_siglip"] = round(q_siglip, 6) if q_siglip >= 0 else -1.0
                            item["quality_legacy"] = round(q_legacy, 6) if q_legacy >= 0 else -1.0
                            quality_score = quality_model_mod.effective(q_siglip, q_legacy, quality_model)
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

                    if subject_items:
                        subject_items = process_subject_items(subject_items)
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
                                "embedding": i.get("embedding"),
                                "siglip_embedding": i.get("siglip_embedding"),
                                "quality_siglip": i.get("quality_siglip", -1.0),
                                "quality_legacy": i.get("quality_legacy", -1.0),
                                "animal_score": i.get("animal_score"),
                                "route": i.get("route"),
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
                    _t_writes = time.perf_counter()
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
                                "quality_siglip": float(crop_item.get("quality_siglip", -1.0)),
                                "quality_legacy": float(crop_item.get("quality_legacy", -1.0)),
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
                        _route = crop_item.get("route") or {}
                        if crop_item.get("animal_score") is not None:
                            serialized_crops[-1]["animal_score"] = round(float(crop_item["animal_score"]), 4)
                        if _route:
                            serialized_crops[-1]["subject"] = str(_route.get("route", "bird"))
                            serialized_crops[-1]["bird_prob"] = round(float(_route.get("bird_prob", 1.0)), 4)
                            if _route.get("route") == "wildlife":
                                serialized_crops[-1]["wildlife_scientific"] = str(_route.get("wildlife_scientific") or "")
                                serialized_crops[-1]["wildlife_family"] = str(_route.get("wildlife_family") or "")
                                serialized_crops[-1]["wildlife_class"] = str(_route.get("wildlife_class") or "")

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
                            "quality_siglip": primary_crop.get("quality_siglip", -1.0),
                            "quality_legacy": primary_crop.get("quality_legacy", -1.0),
                        }
                    )

                    stage_ctx["stage"] = "save_database"
                    database = pd.concat([database, pd.DataFrame([entry])], ignore_index=True)
                    self._save_database_soft(database, db_path, "save_database")
                    # Never raises: embeddings are a best-effort sidecar.
                    emb_store.put(
                        raw_file,
                        {
                            int(c): it.get("embedding")
                            for c, it in enumerate(crop_items_for_write)
                            if it.get("embedding") is not None
                        },
                    )
                    sig_crop_store.put(
                        raw_file,
                        {
                            int(c): it.get("siglip_embedding")
                            for c, it in enumerate(crop_items_for_write)
                            if it.get("siglip_embedding") is not None
                        },
                    )
                    if frame_store_effective is not None and frame_emb is not None:
                        frame_store.put(raw_file, {0: frame_emb})
                    self._timing_add("writes", _t_writes)

                    if image_cb:
                        image_cb(entry)

                    if status_cb:
                        _q = entry.get('quality', -1)
                        _display_q = f"{float(_q):.3f}" if _q not in (None, 'N/A', -1) else '—'
                        _suffix = " [⚠ preview fallback]" if used_preview_fallback else ""
                        status_cb(
                            f"Processed {raw_file}: {entry['species']} Q={_display_q}"
                            f" ({idx + processed_count}/{total}){_suffix}"
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
                    # Mirror the full traceback into the runtime stderr log.
                    # ``log_exception`` already records it in the per-folder
                    # JSON, but bug reports usually attach only the runtime
                    # tail — without this line the user-visible payload is
                    # just ``str(e)`` and the originating library/call is
                    # invisible.
                    _error(
                        f"[queue:{os.path.basename(folder)}] {raw_file}"
                        f" stage={stage_ctx['stage']}"
                        f" {type(e).__name__}: {e}\n{traceback.format_exc()}"
                    )
                    if error_cb:
                        error_cb(raw_file, e)
                    if status_cb:
                        status_cb(f"Error {raw_file}: {e}")
                    entry["scene_count"] = scene_count
                    entry["species"] = "Error"
                    entry["similar"] = False
                    database = pd.concat([database, pd.DataFrame([entry])], ignore_index=True)
                    # Must not raise: this is already the error path, and an
                    # escaping exception here has no handler left and would end
                    # the run while reporting the wrong cause.
                    self._save_database_soft(database, db_path, "save_error_entry")
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
                    # Deliberately strict, unlike the per-image saves above: no
                    # later save can cover for this one, so a failure here must
                    # surface rather than leave the run looking complete.
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
                        # Stamped on every run, not just on folder creation, so
                        # a folder analysed by an older build records its new
                        # schema as soon as this build touches it. The app
                        # version above cannot stand in for this: it says which
                        # build wrote the folder, not which columns it wrote.
                        _meta["schema_version"] = DATABASE_SCHEMA_VERSION
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
                            "species_model": SPECIES_MODEL_ID,
                            "species_regions": list(species_regions),
                            "quality_model": quality_model,
                            "animal_gate": GATE_METHOD if self.animal_gate is not None else "off",
                            "smart_search_frames": bool(frame_store_effective is not None),
                        }
                        # Top-level too, so the browse UI / Perch can show the
                        # label space a folder was classified against without
                        # digging into the settings snapshot. Additive keys.
                        _meta["species_model"] = SPECIES_MODEL_ID
                        _meta["species_regions"] = list(species_regions)
                        # Pipeline 3.0.0 provenance (see config.PIPELINE_MAJOR).
                        # A folder that still has pre-3.0 rows (analysed before,
                        # topped up now) keeps pipeline_major < 3 until it is
                        # upgraded, so the upgrade offer stays visible.
                        try:
                            _prior_major = int(_meta.get("pipeline_major") or 0)
                        except (TypeError, ValueError):
                            _prior_major = 0
                        if _prior_major >= PIPELINE_MAJOR or len(new_files) >= len(database):
                            _meta["pipeline_major"] = PIPELINE_MAJOR
                        _meta["models"] = {
                            "detector": str(getattr(self, "detector_name", "") or ""),
                            "species": SPECIES_MODEL_ID,
                            "siglip": siglip.MODEL_ID if self.siglip_img is not None else None,
                            "legacy_quality": LEGACY_QUALITY_MODEL_ID,
                            "animal_gate": GATE_METHOD if self.animal_gate is not None else None,
                        }
                        _meta[quality_model_mod.METADATA_KEY] = quality_model
                        with open(metadata_path, "w", encoding="utf-8") as mf:
                            _json.dump(_meta, mf, indent=2)
                    except Exception as _meta_e:
                        log_warning(
                            self._log_path,
                            f"Failed to write analysis metadata: {_meta_e}",
                            stage="post_analysis_normalization",
                        )

                    # Drop embeddings for files no longer in the database and
                    # rewrite the sidecar compactly (best-effort).
                    _live = database["filename"].astype(str).tolist()
                    for _st in _all_stores:
                        _st.compact(_live)
                    # The CLIP prototype's slot (exp/semantic-search) is dead data
                    # since 3.0.0; SigLIP replaces it.
                    _stale = os.path.join(kestrel_dir, embedding_store.EMBEDDINGS_DIRNAME, "clip-b16")
                    if os.path.isdir(_stale):
                        import shutil as _sh
                        _sh.rmtree(_stale, ignore_errors=True)
                    if gate_stats["dropped"]:
                        log_event(self._log_path, {"level": "info", "event": "animal_gate_summary",
                                                   "dropped": int(gate_stats["dropped"]),
                                                   "method": GATE_METHOD})

                    # Create or update kestrel_scenedata.json. Reload+merge
                    # UI fields immediately before save so ratings/names/tags
                    # written during the run are not overwritten (S1-08).
                    try:
                        finalize_scenedata_after_analysis(kestrel_dir, database)
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
