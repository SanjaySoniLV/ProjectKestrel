"""Analysis queue manager for Project Kestrel.

Provides the QueueManager class which manages a thread-safe sequential queue
for folder analysis, and the _QueueItem dataclass used internally.
"""

from __future__ import annotations

import os
import sys
import threading
import time as _time_mod
from datetime import datetime

from settings_utils import load_persisted_settings, save_persisted_settings, log

# Telemetry — failsafe import (never blocks startup)
try:
    import kestrel_telemetry as _telemetry
except ImportError:
    try:
        from analyzer import kestrel_telemetry as _telemetry
    except ImportError:
        _telemetry = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Optional: AnalysisPipeline from the sibling analyzer module.
# We do a lightweight directory check at import time but defer the actual
# pipeline/ML import until analysis is first requested, so the visualizer
# starts quickly when the user only wants to browse already-analyzed photos.
# ---------------------------------------------------------------------------
_pipeline_import_error = ''
_AnalysisPipeline = None   # populated lazily on first use
_DEFAULT_DETECTOR_NAME = 'mdv5a'
_ALLOWED_DETECTOR_NAMES = {'mdv5a', 'mdv6-e'}


def _coerce_detector_name(value) -> str:
    name = str(value or _DEFAULT_DETECTOR_NAME).strip().lower()
    return name if name in _ALLOWED_DETECTOR_NAMES else _DEFAULT_DETECTOR_NAME


def _utc_timestamp() -> str:
    return datetime.utcnow().isoformat() + 'Z'


def _ensure_pipeline_path() -> bool:
    """Insert the analyzer package directory into sys.path if present."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    for candidate in [
        os.path.join(script_dir, '..', 'analyzer'),
        os.path.join(script_dir, 'analyzer'),
        script_dir,
        os.path.join(script_dir, '..'),
    ]:
        candidate = os.path.normpath(candidate)
        if os.path.isdir(os.path.join(candidate, 'kestrel_analyzer')):
            if candidate not in sys.path:
                sys.path.insert(0, candidate)
            return True
    return False


# Lightweight check: just look for the kestrel_analyzer directory (no ML imports)
_PIPELINE_AVAILABLE = _ensure_pipeline_path()
if not _PIPELINE_AVAILABLE:
    _pipeline_import_error = 'kestrel_analyzer package not found alongside the visualizer (expected kestrel_analyzer in repo)'


def _get_pipeline_class():
    """Import and cache AnalysisPipeline on first call (deferred ML import)."""
    global _AnalysisPipeline, _PIPELINE_AVAILABLE, _pipeline_import_error
    log("_get_pipeline_class() called, available:", _PIPELINE_AVAILABLE)
    if _AnalysisPipeline is not None:
        return _AnalysisPipeline
    try:
        log("Importing AnalysisPipeline from kestrel_analyzer.pipeline...")
        from kestrel_analyzer.pipeline import AnalysisPipeline  # type: ignore  # noqa: PLC0415
        log("AnalysisPipeline imported successfully.")
        _AnalysisPipeline = AnalysisPipeline
        _PIPELINE_AVAILABLE = True
        return _AnalysisPipeline
    except Exception as exc:
        _pipeline_import_error = str(exc)
        _PIPELINE_AVAILABLE = False
        return None


def _quality_to_raw_rating(quality: float) -> int:
    """Map raw quality score to fixed-threshold stars (1-5, or 0 for invalid)."""
    try:
        q = float(quality)
    except (TypeError, ValueError):
        return 0
    if q < 0:
        return 0
    if q < 0.15:
        return 1
    if q < 0.3:
        return 2
    if q < 0.6:
        return 3
    if q < 0.9:
        return 4
    return 5


class _QueueItem:
    __slots__ = ('path', 'name', 'status', 'processed', 'total', 'error',
                 'start_time', 'end_time', 'paused_duration', 'pause_start_time',
                 'current_filename', 'current_export_path', 'current_status_msg',
                 'current_overlay_rel', 'current_crops_rel', 'current_detections',
                 'current_quality_results', 'current_species_results',
                 'initial_processed')

    def __init__(self, path: str, name: str):
        self.path = path
        self.name = name
        self.status = 'pending'   # pending | running | done | error | cancelled
        self.processed = 0
        self.total = 0
        self.error = ''
        self.start_time: float | None = None
        self.end_time: float | None = None
        self.paused_duration: float = 0.0
        self.pause_start_time: float | None = None
        self.current_filename: str = ''
        self.current_export_path: str = ''
        self.current_status_msg: str = ''
        self.current_overlay_rel: str = ''
        self.current_crops_rel: list = []
        self.current_detections: list = []
        self.current_quality_results: list = []
        self.current_species_results: list = []
        self.initial_processed: int = 0  # files already done before this session

    def to_dict(self) -> dict:
        elapsed = 0.0
        if self.start_time is not None:
            end = self.end_time if self.end_time is not None else _time_mod.time()
            raw = end - self.start_time
            paused = self.paused_duration
            if self.pause_start_time is not None:
                paused += _time_mod.time() - self.pause_start_time
            elapsed = max(0.0, raw - paused)
        return {
            'path': self.path,
            'name': self.name,
            'status': self.status,
            'processed': self.processed,
            'total': self.total,
            'error': self.error,
            'elapsed_seconds': round(elapsed, 1),
            'is_paused': self.pause_start_time is not None,
            'current_filename': self.current_filename,
            'current_export_path': self.current_export_path,
            'current_status_msg': self.current_status_msg,
            'current_overlay_rel': self.current_overlay_rel,
            'current_crops_rel': list(self.current_crops_rel),
            'current_detections': list(self.current_detections),
            'current_quality_results': list(self.current_quality_results),
            'current_species_results': list(self.current_species_results),
        }


class QueueManager:
    """Thread-safe manager for the sequential folder-analysis queue."""

    def __init__(self):
        self._lock = threading.Lock()
        self._items: list = []          # list[_QueueItem]
        self._pause_event = threading.Event()
        self._pause_event.set()         # set = NOT paused
        self._cancel_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pipeline = None
        self._use_gpu = True
        self._wildlife_enabled = True
        self._detection_threshold = 0.25
        self._scene_time_threshold = 1.0
        self._mask_threshold = 0.5
        self._max_bird_crops = 10
        self._parallel_prefetch = 3
        self._detector_name = _DEFAULT_DETECTOR_NAME
        self._retry_errored = False

    def _collect_restore_paths_locked(self) -> list:
        restore_statuses = {'pending', 'running', 'cancelled'}
        seen = set()
        restore_paths = []
        for it in self._items:
            if it.status not in restore_statuses:
                continue
            p = str(it.path or '').strip()
            if not p or p in seen:
                continue
            seen.add(p)
            restore_paths.append(p)
        return restore_paths

    def _build_recovery_state_locked(self) -> dict:
        return {
            'updated_utc': _utc_timestamp(),
            'running': self.is_running,
            'paused': self.is_paused,
            'restore_paths': self._collect_restore_paths_locked(),
            'options': {
                'use_gpu': bool(self._use_gpu),
                'wildlife_enabled': bool(self._wildlife_enabled),
                'detector_name': str(self._detector_name),
                'detection_threshold': float(self._detection_threshold),
                'scene_time_threshold': float(self._scene_time_threshold),
                'mask_threshold': float(self._mask_threshold),
                'max_bird_crops': int(self._max_bird_crops),
                'parallel_prefetch': int(self._parallel_prefetch),
            },
            'items': [
                {
                    'path': it.path,
                    'name': it.name,
                    'status': it.status,
                    'processed': int(it.processed or 0),
                    'total': int(it.total or 0),
                }
                for it in self._items
            ],
        }

    def _persist_recovery_state(self) -> None:
        try:
            with self._lock:
                state = self._build_recovery_state_locked()
            settings = load_persisted_settings()
            if state.get('restore_paths'):
                settings['queue_recovery_state'] = state
            else:
                settings.pop('queue_recovery_state', None)
            save_persisted_settings(settings)
        except Exception:
            pass

    def get_persisted_recovery_state(self) -> dict:
        try:
            settings = load_persisted_settings()
            state = settings.get('queue_recovery_state')
            return state if isinstance(state, dict) else {}
        except Exception:
            return {}

    def restore_from_persisted_state(self) -> dict:
        if self.is_running:
            return {'success': False, 'error': 'Queue is already running'}

        def _safe_float(val, default):
            try:
                return float(val)
            except (TypeError, ValueError):
                return float(default)

        def _safe_int(val, default, min_value=1, max_value=20):
            try:
                num = int(float(val))
            except (TypeError, ValueError):
                num = int(default)
            if num < min_value:
                return min_value
            if num > max_value:
                return max_value
            return num

        state = self.get_persisted_recovery_state()
        if not state:
            return {'success': False, 'error': 'No persisted queue recovery state found'}

        raw_paths = state.get('restore_paths')
        if not isinstance(raw_paths, list):
            return {'success': False, 'error': 'Persisted queue recovery state is invalid'}

        restore_paths = []
        missing_paths = []
        seen = set()
        for p in raw_paths:
            path = str(p or '').strip()
            if not path or path in seen:
                continue
            seen.add(path)
            if os.path.isdir(path):
                restore_paths.append(path)
            else:
                missing_paths.append(path)

        if not restore_paths:
            return {
                'success': False,
                'error': 'No valid folders found for queue recovery',
                'missing_paths': missing_paths,
            }

        options = state.get('options') if isinstance(state.get('options'), dict) else {}
        result = self.enqueue(
            restore_paths,
            use_gpu=bool(options.get('use_gpu', True)),
            wildlife_enabled=bool(options.get('wildlife_enabled', True)),
            detector_name=_coerce_detector_name(options.get('detector_name', _DEFAULT_DETECTOR_NAME)),
            detection_threshold=_safe_float(options.get('detection_threshold', 0.25), 0.25),
            scene_time_threshold=_safe_float(options.get('scene_time_threshold', 1.0), 1.0),
            mask_threshold=_safe_float(options.get('mask_threshold', 0.5), 0.5),
            max_bird_crops=_safe_int(options.get('max_bird_crops', 10), 10),
            parallel_prefetch=_safe_int(options.get('parallel_prefetch', 3), 3, min_value=1, max_value=5),
        )
        if result.get('success'):
            result['restored'] = len(restore_paths)
            if missing_paths:
                result['missing_paths'] = missing_paths
        return result

    def clear_persisted_recovery_state(self) -> dict:
        try:
            settings = load_persisted_settings()
            settings.pop('queue_recovery_state', None)
            save_persisted_settings(settings)
            return {'success': True}
        except Exception as exc:
            return {'success': False, 'error': str(exc)}

    # ---- public read-only properties ----

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    def get_status(self) -> dict:
        with self._lock:
            return {
                'available': _PIPELINE_AVAILABLE,
                'unavailable_reason': _pipeline_import_error if not _PIPELINE_AVAILABLE else '',
                'running': self.is_running,
                'paused': self.is_paused,
                'items': [it.to_dict() for it in self._items],
            }

    # ---- control ----

    def enqueue(
        self,
        paths: list,
        use_gpu: bool = True,
        wildlife_enabled: bool = True,
        detector_name: str = _DEFAULT_DETECTOR_NAME,
        detection_threshold: float = 0.25,
        scene_time_threshold: float = 1.0,
        mask_threshold: float = 0.5,
        max_bird_crops: int = 10,
        parallel_prefetch: int = 3,
        retry_errored: bool = False,
    ) -> dict:
        if not _PIPELINE_AVAILABLE:
            return {'success': False, 'error': f'Analyzer unavailable: {_pipeline_import_error}'}
        with self._lock:
            path_to_item = {it.path: it for it in self._items}
            added = 0
            for p in paths:
                existing_item = path_to_item.get(p)
                if existing_item is not None:
                    if existing_item.status in ('done', 'error', 'cancelled'):
                        # Reset finalized item so it can be re-processed
                        existing_item.status = 'pending'
                        existing_item.processed = 0
                        existing_item.total = 0
                        existing_item.error = ''
                        existing_item.start_time = None
                        existing_item.end_time = None
                        existing_item.paused_duration = 0.0
                        existing_item.pause_start_time = None
                        existing_item.current_filename = ''
                        existing_item.current_export_path = ''
                        existing_item.current_status_msg = ''
                        existing_item.current_overlay_rel = ''
                        existing_item.current_crops_rel = []
                        existing_item.current_detections = []
                        existing_item.current_quality_results = []
                        existing_item.current_species_results = []
                        existing_item.initial_processed = 0
                        added += 1
                    # If already pending/running, leave it alone
                else:
                    name = os.path.basename(p.rstrip('/\\')) or p
                    new_item = _QueueItem(p, name)
                    self._items.append(new_item)
                    added += 1
        if not self.is_running:
            self._cancel_event.clear()
            self._pause_event.set()
            self._use_gpu = use_gpu
            self._wildlife_enabled = wildlife_enabled
            self._detection_threshold = float(detection_threshold)
            self._scene_time_threshold = float(scene_time_threshold)
            self._mask_threshold = float(mask_threshold)
            self._detector_name = _coerce_detector_name(detector_name)
            try:
                max_bird_crops_num = int(float(max_bird_crops))
            except (TypeError, ValueError):
                max_bird_crops_num = 10
            self._max_bird_crops = max(1, min(20, max_bird_crops_num))
            try:
                parallel_prefetch_num = int(float(parallel_prefetch))
            except (TypeError, ValueError):
                parallel_prefetch_num = 3
            self._parallel_prefetch = max(1, min(5, parallel_prefetch_num))
            self._retry_errored = bool(retry_errored)
            self._thread = threading.Thread(target=self._run, daemon=True, name='kestrel-queue')
            self._thread.start()
        self._persist_recovery_state()
        return {'success': True, 'added': added}

    def pause(self) -> dict:
        self._pause_event.clear()
        with self._lock:
            running = next((it for it in self._items if it.status == 'running'), None)
            if running is not None and running.pause_start_time is None:
                running.pause_start_time = _time_mod.time()
        self._persist_recovery_state()
        return {'success': True, 'paused': True}

    def resume(self) -> dict:
        with self._lock:
            running = next((it for it in self._items if it.status == 'running'), None)
            if running is not None and running.pause_start_time is not None:
                running.paused_duration += _time_mod.time() - running.pause_start_time
                running.pause_start_time = None
        self._pause_event.set()
        self._persist_recovery_state()
        return {'success': True, 'paused': False}

    def cancel(self) -> dict:
        self._cancel_event.set()
        self._pause_event.set()
        with self._lock:
            for it in self._items:
                if it.status == 'pending':
                    it.status = 'cancelled'
            running = next((it for it in self._items if it.status == 'running'), None)
            if running is not None:
                running.current_status_msg = 'Cancelling\u2026'
        self._persist_recovery_state()
        return {'success': True}

    def join_worker(self, timeout: float | None = 120.0) -> bool:
        """Block until the analysis worker thread stops (e.g. after :meth:`cancel`).

        Returns True if no worker is running or the thread joined within *timeout*.
        Returns False if the thread is still alive after waiting (caller may exit anyway).
        """
        t = self._thread
        if t is None:
            return True
        if not t.is_alive():
            return True
        t.join(timeout=timeout)
        return not t.is_alive()

    def clear_done(self) -> dict:
        with self._lock:
            self._items = [it for it in self._items if it.status not in ('done', 'error', 'cancelled')]
        self._persist_recovery_state()
        return {'success': True}

    def remove_pending_item(self, path: str) -> dict:
        """Remove a single pending item from the queue by path."""
        with self._lock:
            idx = next((i for i, it in enumerate(self._items)
                        if it.path == path and it.status == 'pending'), None)
            if idx is None:
                return {'success': False, 'error': 'Item not found or not pending'}
            self._items.pop(idx)
        self._persist_recovery_state()
        return {'success': True}

    def reorder_pending(self, ordered_paths: list) -> dict:
        """Reorder pending items to match the given path order."""
        with self._lock:
            pending = [it for it in self._items if it.status == 'pending']
            non_pending = [it for it in self._items if it.status != 'pending']
            path_to_item = {it.path: it for it in pending}
            reordered = []
            for p in ordered_paths:
                if p in path_to_item:
                    reordered.append(path_to_item.pop(p))
            for it in pending:
                if it.path in path_to_item:
                    reordered.append(it)
            self._items = non_pending + reordered
        self._persist_recovery_state()
        return {'success': True}

    # ---- internal ----

    def _run(self):
        if self._pipeline is None:
            cls = _get_pipeline_class()
            if cls is None:
                with self._lock:
                    for it in self._items:
                        if it.status in ('pending', 'running'):
                            it.status = 'error'
                            it.error = f'Pipeline unavailable: {_pipeline_import_error}'
                log('[queue] Pipeline unavailable, aborting:', _pipeline_import_error)
                self._persist_recovery_state()
                return
            self._pipeline = cls(use_gpu=self._use_gpu, detector_name=self._detector_name)
        else:
            try:
                self._pipeline.detector_name = self._detector_name
            except Exception:
                pass

        while not self._cancel_event.is_set():
            with self._lock:
                item = next((it for it in self._items if it.status == 'pending'), None)
            if item is None:
                break

            with self._lock:
                item.status = 'running'
                item.start_time = _time_mod.time()
                item.initial_processed = 0
            self._persist_recovery_state()

            try:
                current_settings = load_persisted_settings()
                if current_settings.get('active_analysis_path') != item.path:
                    current_settings['active_analysis_path'] = item.path
                    save_persisted_settings(current_settings)
            except Exception:
                pass

            try:
                def _on_progress(processed, total, _it=item):
                    with self._lock:
                        if _it.initial_processed == 0 and processed > 0 and _it.processed == 0:
                            _it.initial_processed = processed
                        _it.processed = processed
                        _it.total = total

                def _on_status(msg, _it=item):
                    with self._lock:
                        _it.current_status_msg = msg
                    log(f'[queue:{_it.name}]', msg)

                def _on_thumbnail(data, _it=item):
                    with self._lock:
                        _it.current_filename = data.get('filename', '')
                        export_rel = data.get('export_path', '')
                        _it.current_export_path = export_rel.replace('\\', '/')
                        _it.current_overlay_rel = ''
                        _it.current_crops_rel = []
                        _it.current_detections = []
                        _it.current_quality_results = []
                        _it.current_species_results = []

                def _on_detection(data, _it=item):
                    import cv2 as _cv2
                    overlay_np = data.get('overlay')
                    rel = ''
                    if overlay_np is not None:
                        overlay_path = os.path.join(_it.path, '.kestrel', 'export',
                                                     '__live_overlay.jpg')
                        try:
                            os.makedirs(os.path.dirname(overlay_path), exist_ok=True)
                            _cv2.imwrite(overlay_path,
                                         _cv2.cvtColor(overlay_np, _cv2.COLOR_RGB2BGR))
                            rel = os.path.relpath(overlay_path, _it.path).replace('\\', '/')
                        except Exception:
                            pass
                    with self._lock:
                        _it.current_overlay_rel = rel

                def _on_crops(data, _it=item):
                    import cv2 as _cv2
                    crops = data.get('crops') or []
                    confidences = data.get('confidences') or []
                    saved_rels = []
                    export_dir = os.path.join(_it.path, '.kestrel', 'export')
                    try:
                        os.makedirs(export_dir, exist_ok=True)
                    except Exception:
                        pass
                    for idx, crop in enumerate(crops):
                        if crop is None:
                            continue
                        cp = os.path.join(export_dir, f'__live_crop_{idx}.jpg')
                        try:
                            _cv2.imwrite(cp,
                                         _cv2.cvtColor(crop, _cv2.COLOR_RGB2BGR))
                            saved_rels.append(
                                os.path.relpath(cp, _it.path).replace('\\', '/'))
                        except Exception:
                            pass
                    with self._lock:
                        _it.current_crops_rel = saved_rels
                        _it.current_detections = [
                            {'confidence': float(c)} for c in confidences]

                def _on_quality(data, _it=item):
                    incoming = list(data.get('results') or [])
                    normalized = []
                    for entry in incoming:
                        if not isinstance(entry, dict):
                            continue
                        raw_quality = entry.get('quality', -1)
                        try:
                            quality = float(raw_quality)
                        except (TypeError, ValueError):
                            quality = -1.0
                        raw_rating = _quality_to_raw_rating(quality)
                        normalized.append({
                            'quality': quality,
                            'raw_rating': raw_rating,
                            # Keep legacy field for compatibility with existing UI readers.
                            'rating': raw_rating,
                        })
                    with self._lock:
                        _it.current_quality_results = normalized

                def _on_species(data, _it=item):
                    with self._lock:
                        _it.current_species_results = list(data.get('results') or [])

                self._pipeline.process_folder(
                    item.path,
                    pause_event=self._pause_event,
                    cancel_event=self._cancel_event,
                    callbacks={
                        'on_status': _on_status,
                        'on_progress': _on_progress,
                        'on_thumbnail': _on_thumbnail,
                        'on_detection': _on_detection,
                        'on_crops': _on_crops,
                        'on_quality': _on_quality,
                        'on_species': _on_species,
                    },
                    analyzer_name='visualizer-queue',
                    wildlife_enabled=self._wildlife_enabled,
                    detection_threshold=self._detection_threshold,
                    scene_time_threshold=self._scene_time_threshold,
                    mask_threshold=self._mask_threshold,
                    max_bird_crops=self._max_bird_crops,
                    parallel_prefetch=self._parallel_prefetch,
                    retry_errored=self._retry_errored,
                )
                with self._lock:
                    if self._cancel_event.is_set():
                        item.status = 'cancelled'
                        item.end_time = _time_mod.time()
                    else:
                        item.status = 'done'
                        item.end_time = _time_mod.time()
                        if item.total > 0:
                            item.processed = item.total
                self._persist_recovery_state()
                self._send_folder_analytics(item)
            except Exception as exc:
                log(f'[queue] Error processing {item.path!r}:', exc)
                with self._lock:
                    item.status = 'error'
                    item.end_time = _time_mod.time()
                    item.error = str(exc)
                self._persist_recovery_state()
                self._send_folder_analytics(item)

        log('[queue] Run thread finished.')
        self._persist_recovery_state()

    def _send_folder_analytics(self, item):
        """Send per-folder analytics (if opted-in) and completion telemetry (non-optional)."""
        try:
            if _telemetry is None:
                return
            settings = load_persisted_settings()
            machine_id = _telemetry.get_machine_id(settings)
            version = _telemetry._read_version()

            files_this_session = max(0, item.processed - item.initial_processed)

            elapsed = 0.0
            if item.start_time is not None:
                end = item.end_time if item.end_time is not None else _time_mod.time()
                elapsed = max(0.0, (end - item.start_time) - item.paused_duration)

            avg_time_per_file_s = elapsed / files_this_session if files_this_session > 0 else 0.0
            _telemetry.send_analysis_completion_telemetry(
                files_analyzed=files_this_session,
                machine_id=machine_id,
                version=version,
                avg_time_per_file_s=avg_time_per_file_s,
            )

            settings['kestrel_impact_total_files'] = settings.get('kestrel_impact_total_files', 0) + files_this_session
            settings['kestrel_impact_total_seconds'] = round(
                settings.get('kestrel_impact_total_seconds', 0.0) + elapsed, 1
            )
            save_persisted_settings(settings)

            was_cancelled = (item.status == 'cancelled')
            stats = _telemetry.collect_folder_stats(
                item.path, files_this_session, item.total
            )

            analytics_payload = {
                'folder_path': item.path,
                'files_analyzed': files_this_session,
                'total_files': item.total,
                'active_compute_time_s': elapsed,
                'file_sizes_kb': stats.get('file_sizes_kb', []),
                'file_formats': stats.get('file_formats', {}),
                'was_cancelled': was_cancelled,
                'machine_id': machine_id,
                'version': version
            }

            if not settings.get('analytics_consent_shown', False):
                settings['pending_analytics'] = analytics_payload
                save_persisted_settings(settings)
            elif settings.get('analytics_opted_in', False):
                _telemetry.send_folder_analytics(**analytics_payload)
        except Exception:
            pass  # failsafe — never disrupt queue operation


# Module-level singleton — shared across Api and Handler
_queue_manager = QueueManager()
