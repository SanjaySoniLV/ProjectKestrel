"""Persisted settings I/O and general utility functions for the Kestrel visualizer."""

from __future__ import annotations

import glob
import json
import math
import os
import shutil
import sys
import tempfile
import threading
import time
from typing import Any

# Serializes ``save_persisted_settings`` so the read-existing / monotonic-guard
# / refresh-.bak / os.replace sequence is atomic with respect to other saves
# in this process. Concurrent callers include the JS bridge (UI setting
# changes), the queue worker (impact-counter bumps), and the visualizer
# startup path — they all hit the same file and previously raced on the
# shared ``settings.json.tmp`` name (symptom: WinError 2 "cannot find the
# file specified" on os.replace when the other thread deleted our tmp).
_SAVE_LOCK = threading.RLock()

# Prefix/suffix for ``tempfile.mkstemp`` tmp files we emit. The glob
# ``settings.json.*.tmp`` uniquely identifies orphans left by a crashed save.
_TMP_FILE_PREFIX = 'settings.json.'
_TMP_FILE_SUFFIX = '.tmp'

# Forward-compatible / unknown keys (e.g. tutorial flags from newer builds) — size limits only.
_PASSTHROUGH_MAX_STR = 16384
_PASSTHROUGH_MAX_LIST_LEN = 512
_PASSTHROUGH_MAX_DICT_KEYS = 256
_PASSTHROUGH_MAX_DEPTH = 8

SETTINGS_FILENAME = 'settings.json'
_MAX_PATH_CHARS = 4096
_MAX_TEXT_CHARS = 4096

_ALLOWED_EDITORS = {
    'system', 'darktable', 'lightroom', 'photoshop', 'capture_one',
    'affinity', 'gimp', 'rawtherapee', 'luminar', 'dxo', 'on1',
    'acdsee', 'paintshop', 'faststone', 'xnview', 'irfanview', 'custom',
}
_ALLOWED_RATING_PROFILES = {'very_strict', 'strict', 'balanced', 'lenient', 'very_lenient'}
_ALLOWED_EXPOSURE_QUALITY = {'lenient', 'balanced', 'aggressive'}
_ALLOWED_WILDLIFE_MODEL_MODES = {'fast', 'accurate'}
_ALLOWED_QUEUE_DETECTOR_NAMES = {'mdv5a', 'mdv1000-cedar'}
# Biogeographic region codes for the species combobox. Sourced from the
# ``bird_catalog`` module so the data layer owns the vocabulary and we only
# import the tuple here for validation.
try:
    from kestrel_analyzer.bird_catalog import ALLOWED_REGION_CODES as _BIRD_REGION_CODES
except ImportError:  # pragma: no cover - import only fails in odd test sandboxes
    try:
        from analyzer.kestrel_analyzer.bird_catalog import ALLOWED_REGION_CODES as _BIRD_REGION_CODES
    except ImportError:
        _BIRD_REGION_CODES = ('NA',)
_ALLOWED_BIRD_REGIONS = set(_BIRD_REGION_CODES)
_DEFAULT_BIRD_REGIONS: list[str] = ['NA']
# Legacy detector names → current names. Applied silently at load to migrate
# stored settings from older builds. UI semantics ("Fast"/"Accurate") are
# preserved, so users never see a change.
_LEGACY_DETECTOR_NAME_MIGRATIONS = {'mdv6-e': 'mdv1000-cedar'}


def _migrate_legacy_detector_name(value):
    """Remap a stored detector name through ``_LEGACY_DETECTOR_NAME_MIGRATIONS``.

    Non-string or unknown values pass through unchanged so that the downstream
    ``_coerce_enum`` allowlist check handles them.
    """
    if not isinstance(value, str):
        return value
    norm = value.strip().lower()
    return _LEGACY_DETECTOR_NAME_MIGRATIONS.get(norm, value)
_ALLOWED_QUEUE_ITEM_STATUSES = {'pending', 'running', 'done', 'error', 'cancelled'}

# Telemetry — failsafe import (never blocks startup)
try:
    import kestrel_telemetry as _telemetry
except ImportError:
    try:
        from analyzer import kestrel_telemetry as _telemetry
    except ImportError:
        _telemetry = None  # type: ignore[assignment]


def _get_user_data_dir() -> str:
    # Use a unified application folder name for Project Kestrel
    if sys.platform.startswith('win'):
        base = os.environ.get('LOCALAPPDATA') or os.environ.get('APPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'ProjectKestrel')
    if sys.platform == 'darwin':
        return os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'ProjectKestrel')
    base = os.environ.get('XDG_DATA_HOME') or os.path.join(os.path.expanduser('~'), '.local', 'share')
    return os.path.join(base, 'project-kestrel')


def _get_settings_path() -> str:
    return os.path.join(_get_user_data_dir(), SETTINGS_FILENAME)


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {'1', 'true', 'yes', 'on'}:
            return True
        if low in {'0', 'false', 'no', 'off'}:
            return False
    return bool(default)


def _coerce_optional_bool(value: Any, default: bool | None = None) -> bool | None:
    if value is None:
        return default
    return _coerce_bool(value, default=False)


def _coerce_int(value: Any, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        if isinstance(value, bool):
            n = int(value)
        else:
            n = int(float(value))
    except (TypeError, ValueError):
        n = int(default)
    if min_value is not None and n < min_value:
        n = min_value
    if max_value is not None and n > max_value:
        n = max_value
    return n


def _coerce_float(value: Any, default: float, min_value: float | None = None, max_value: float | None = None) -> float:
    try:
        if isinstance(value, bool):
            n = float(int(value))
        else:
            n = float(value)
    except (TypeError, ValueError):
        n = float(default)
    if min_value is not None and n < min_value:
        n = min_value
    if max_value is not None and n > max_value:
        n = max_value
    return n


def _coerce_string(value: Any, default: str = '', max_len: int = _MAX_TEXT_CHARS) -> str:
    if value is None:
        return default
    s = str(value).strip()
    if not s:
        return default
    if len(s) > max_len:
        s = s[:max_len]
    return s


def _coerce_enum(value: Any, allowed: set[str], default: str) -> str:
    s = _coerce_string(value, default=default, max_len=64).lower()
    if s in allowed:
        return s
    return default


def _coerce_path(value: Any, default: str = '') -> str:
    s = _coerce_string(value, default=default, max_len=_MAX_PATH_CHARS)
    if not s:
        return ''
    return _normalize(s)


def _sanitize_path_list(value: Any, max_items: int = 256) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        p = _coerce_path(item)
        if not p or p in seen:
            continue
        seen.add(p)
        out.append(p)
        if len(out) >= max_items:
            break
    return out


def _sanitize_analyze_recents(value: Any, max_items: int = 8) -> list[dict]:
    """Validate the analyze_recents settings field — list of recently-queued
    folders that haven't been fully analyzed yet. Each entry is a dict
    ``{'path': str, 'timestamp': str}``. Deduped by normalized path
    (most recent timestamp wins on duplicate). Capped at ``max_items``.

    Designed for the Phase 3 Analyze Folders dialog's chip row, which is
    intentionally independent from the main tree's ``folder_recents``.
    """
    if not isinstance(value, list):
        return []
    # Build dedup map first so the most-recent timestamp wins, then preserve
    # the input order (most-recent-first from the frontend).
    by_path: dict[str, dict] = {}
    order: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        p = _coerce_path(item.get('path'))
        if not p:
            continue
        ts = _coerce_string(item.get('timestamp'), default='', max_len=32)
        if p not in by_path:
            order.append(p)
        # Keep the first occurrence's timestamp (input is expected to be
        # most-recent-first; first occurrence == most recent).
        by_path.setdefault(p, {'path': p, 'timestamp': ts})
    out: list[dict] = []
    for p in order:
        out.append(by_path[p])
        if len(out) >= max_items:
            break
    return out


def _sanitize_perf_samples(value: Any, max_items: int = 50) -> list[dict]:
    """Validate the perf_samples_gpu / perf_samples_cpu lists used by the
    Analyze Folders dialog's local-rate time estimate. Each entry is a dict
    ``{'imgs': int, 'secs': float, 'ts': str}``. Drops malformed entries
    silently. Returns the last ``max_items`` valid entries (the worker only
    appends, so trailing entries are the most recent)."""
    if not isinstance(value, list):
        return []
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        imgs = _coerce_int(item.get('imgs'), 0, min_value=0, max_value=1_000_000)
        secs = _coerce_float(item.get('secs'), 0.0, min_value=0.0, max_value=10_000_000.0)
        if imgs <= 0 or secs <= 0.0:
            continue
        ts = _coerce_string(item.get('ts'), default='', max_len=32)
        out.append({'imgs': imgs, 'secs': round(secs, 1), 'ts': ts})
    return out[-max_items:]


def _sanitize_int_list(value: Any, max_items: int = 128, min_value: int = 0, max_value: int = 1_000_000_000) -> list[int]:
    if not isinstance(value, list):
        return []
    out: list[int] = []
    seen: set[int] = set()
    for item in value:
        try:
            n = int(float(item))
        except (TypeError, ValueError):
            continue
        if n < min_value or n > max_value or n in seen:
            continue
        seen.add(n)
        out.append(n)
        if len(out) >= max_items:
            break
    return out


def _sanitize_pending_analytics(value: Any) -> dict | None:
    if not isinstance(value, dict):
        return None

    payload: dict[str, Any] = {
        'folder_path': _coerce_path(value.get('folder_path', '')),
        'files_analyzed': _coerce_int(value.get('files_analyzed', 0), 0, min_value=0, max_value=1_000_000_000),
        'total_files': _coerce_int(value.get('total_files', 0), 0, min_value=0, max_value=1_000_000_000),
        'active_compute_time_s': round(
            _coerce_float(value.get('active_compute_time_s', 0.0), 0.0, min_value=0.0, max_value=31_536_000.0),
            3,
        ),
        'was_cancelled': _coerce_bool(value.get('was_cancelled', False), default=False),
        'machine_id': _coerce_string(value.get('machine_id', ''), max_len=128),
        'version': _coerce_string(value.get('version', ''), max_len=64),
    }

    file_sizes: list[float] = []
    if isinstance(value.get('file_sizes_kb'), list):
        for raw in value.get('file_sizes_kb', []):
            try:
                f = float(raw)
            except (TypeError, ValueError):
                continue
            if f < 0:
                continue
            file_sizes.append(round(min(f, 1_000_000_000.0), 3))
            if len(file_sizes) >= 20000:
                break
    payload['file_sizes_kb'] = file_sizes

    file_formats: dict[str, int] = {}
    if isinstance(value.get('file_formats'), dict):
        for raw_k, raw_v in value.get('file_formats', {}).items():
            k = _coerce_string(raw_k, max_len=32).lower()
            if not k:
                continue
            file_formats[k] = _coerce_int(raw_v, 0, min_value=0, max_value=1_000_000_000)
            if len(file_formats) >= 128:
                break
    payload['file_formats'] = file_formats

    return payload


# Phase 3: _sanitize_queue_recovery_state removed — queue-restore feature
# replaced by the analyze dialog's analyze_recents chip row. Existing user
# settings files may still contain a 'queue_recovery_state' key; it falls
# through to the passthrough handler at the end of _sanitize_settings_payload
# and is silently dropped on next save since the explicit allowlist no
# longer mentions it.


def _passthrough_setting_value(value: Any, depth: int = 0) -> Any | None:
    """Copy a JSON-like value for persisting unknown settings keys.

    Only bool, None, numbers, strings, lists, and dicts are allowed (same as JSON).
    Used so newer app versions can add keys without updating this module, and older
    builds preserve them on load/save instead of dropping them as 'unsupported'.
    """
    if depth > _PASSTHROUGH_MAX_DEPTH:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > 9_007_199_254_740_991:  # practical JS-safe integer range
            return None
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    if isinstance(value, str):
        if len(value) > _PASSTHROUGH_MAX_STR:
            return value[:_PASSTHROUGH_MAX_STR]
        return value
    if isinstance(value, list):
        out: list[Any] = []
        for item in value[:_PASSTHROUGH_MAX_LIST_LEN]:
            pv = _passthrough_setting_value(item, depth + 1)
            if pv is not None:
                out.append(pv)
        return out
    if isinstance(value, dict):
        out_d: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= _PASSTHROUGH_MAX_DICT_KEYS:
                break
            if not isinstance(k, str):
                continue
            ks = k[:256] if len(k) > 256 else k
            pv = _passthrough_setting_value(v, depth + 1)
            if pv is not None:
                out_d[ks] = pv
        return out_d
    return None


def _merge_forward_compatible_keys(out: dict[str, Any], data: dict, emit_log: bool) -> None:
    """Attach unknown keys from *data* onto *out* (keys not already set by core sanitization)."""
    skipped: list[str] = []
    for k, v in data.items():
        if k in out:
            continue
        pv = _passthrough_setting_value(v)
        if pv is not None:
            out[k] = pv
        else:
            skipped.append(k)
    if emit_log and skipped:
        sample = ', '.join(skipped[:12])
        suffix = ' ...' if len(skipped) > 12 else ''
        warn(f'[settings] Could not preserve {len(skipped)} key(s) (unsupported type): {sample}{suffix}')


def _sanitize_settings_payload(data: dict, emit_log: bool = False) -> dict:
    if not isinstance(data, dict):
        return {}

    out: dict[str, Any] = {}

    def _set_bool(key: str, default: bool) -> None:
        if key in data:
            out[key] = _coerce_bool(data.get(key), default=default)

    def _set_opt_bool(key: str, default: bool | None = None) -> None:
        if key in data:
            out[key] = _coerce_optional_bool(data.get(key), default=default)

    def _set_int(key: str, default: int, min_value: int | None = None, max_value: int | None = None) -> None:
        if key in data:
            out[key] = _coerce_int(data.get(key), default=default, min_value=min_value, max_value=max_value)

    def _set_float(key: str, default: float, min_value: float | None = None, max_value: float | None = None, digits: int | None = None) -> None:
        if key in data:
            val = _coerce_float(data.get(key), default=default, min_value=min_value, max_value=max_value)
            out[key] = round(val, digits) if digits is not None else val

    def _set_str(key: str, default: str = '', max_len: int = _MAX_TEXT_CHARS) -> None:
        if key in data:
            out[key] = _coerce_string(data.get(key), default=default, max_len=max_len)

    def _set_path(key: str, default: str = '') -> None:
        if key in data:
            out[key] = _coerce_path(data.get(key), default=default)

    if 'editor' in data:
        out['editor'] = _coerce_enum(data.get('editor'), _ALLOWED_EDITORS, default='darktable')
    _set_path('customEditorPath')
    _set_int('treeScanDepth', default=3, min_value=1, max_value=6)

    _set_opt_bool('analytics_opted_in', default=None)
    _set_bool('analytics_consent_shown', default=False)
    # Crash reports default ON; users can opt out via Settings. Gated only for
    # the two automatic sends in visualizer.py — the user-confirmed recovery
    # report ignores this flag (the dialog is its own consent).
    _set_bool('crash_reports_enabled', default=True)

    if 'rating_profile' in data:
        out['rating_profile'] = _coerce_enum(data.get('rating_profile'), _ALLOWED_RATING_PROFILES, default='balanced')
    _set_float('detection_threshold', default=0.25, min_value=0.1, max_value=0.99, digits=4)
    _set_float('scene_time_threshold', default=1.0, min_value=0.0, max_value=60.0, digits=4)
    _set_float('mask_threshold', default=0.5, min_value=0.5, max_value=0.95, digits=4)
    _set_int('max_bird_crops', default=10, min_value=1, max_value=20)
    _set_int('parallel_prefetch', default=3, min_value=1, max_value=5)
    _set_bool('exposure_corrected_thumbs', default=True)
    # Analysis-mode feature toggles persisted so cloud-compute settings snapshot
    # (_cc_analysis_settings_snapshot) can read them without needing the UI to
    # pass them as runtime parameters.
    _set_bool('wildlife_enabled', default=False)
    _set_bool('species_detection_enabled', default=True)
    # ONNX provider resilience (auto GPU↔CPU fallback). Advanced/triage knobs;
    # not exposed in the UI. ``gpu_resilience_enabled=False`` reverts to the
    # pre-resilience behavior (single GPU session, no recovery).
    _set_bool('gpu_resilience_enabled', default=True)
    _set_bool('gpu_aggressive_recreate', default=False)
    if 'wildlife_model_mode' in data:
        out['wildlife_model_mode'] = _coerce_enum(
            data.get('wildlife_model_mode'),
            _ALLOWED_WILDLIFE_MODEL_MODES,
            default='accurate',
        )
    if 'detector_name' in data:
        out['detector_name'] = _coerce_enum(
            _migrate_legacy_detector_name(data.get('detector_name')),
            _ALLOWED_QUEUE_DETECTOR_NAMES,
            default='mdv5a',
        )

    if 'exposure_quality' in data:
        out['exposure_quality'] = _coerce_enum(
            data.get('exposure_quality'),
            _ALLOWED_EXPOSURE_QUALITY,
            default='balanced',
        )

    _set_bool('raw_preview_cache_enabled', default=True)
    _set_bool('raw_preview_debug_logging_enabled', default=True)
    _set_bool('auto_save_enabled', default=True)
    _set_bool('raw_exposure_correction_disabled', default=False)

    # Analysis-time thumbnail generation (cached export JPEGs + crops). Exposed
    # as user settings so photographers can trade storage for fidelity.
    # Width in pixels along the long edge; JPEG quality is cv2's 0–100 param.
    _set_int('thumbnail_max_width', default=1200, min_value=400, max_value=2400)
    _set_float('thumbnail_jpeg_compression', default=0.75, min_value=0.5, max_value=1.0, digits=4)
    _set_int('thumbnail_jpeg_quality', default=75, min_value=50, max_value=100)

    # Bird-catalog region selection (multi-select). The list is sanitised here
    # so a stale or malformed payload from a future/older build cannot bypass
    # the allowlist downstream in the combobox query path.
    if 'bird_regions' in data:
        raw = data.get('bird_regions')
        if isinstance(raw, list):
            cleaned: list[str] = []
            seen: set[str] = set()
            for item in raw:
                if not isinstance(item, str):
                    continue
                token = item.strip()
                if token in _ALLOWED_BIRD_REGIONS and token not in seen:
                    cleaned.append(token)
                    seen.add(token)
            out['bird_regions'] = cleaned if cleaned else list(_DEFAULT_BIRD_REGIONS)
        else:
            out['bird_regions'] = list(_DEFAULT_BIRD_REGIONS)
    _set_bool('show_scientific_names', default=False)

    _set_bool('includeSecondarySpecies', default=False)
    _set_bool('groupByFolder', default=True)
    _set_bool('groupByTime', default=True)
    _set_bool('showBirdThumbs', default=False)
    _set_bool('onlyManualRatedScenes', default=False)
    _set_float('scene_preview_split_ratio', default=0.68, min_value=0.25, max_value=0.85, digits=4)

    if 'sortBy' in data:
        sort_by = _coerce_string(data.get('sortBy'), default='captureTime', max_len=64)
        if sort_by and not sort_by.replace('_', '').isalnum():
            sort_by = 'captureTime'
        out['sortBy'] = sort_by

    _set_path('rootHint')
    # Phase 3: lastQueueState removed — was used by the dialog's "Restore Queue?"
    # button to persist the user's most-recent selection. Replaced by the
    # analyze_recents settings key (separate timestamp-based history per the
    # Phase 3 dialog redesign). Existing values fall through to passthrough.
    if 'folder_recents' in data:
        # Persistent most-recently-loaded root folders. Cap matches the visible
        # chip count in the sidebar so backend and frontend stay aligned.
        out['folder_recents'] = _sanitize_path_list(data.get('folder_recents'), max_items=8)
    if 'analyze_recents' in data:
        # Phase 3: Analyze Folders dialog's own recents (list of {path, timestamp}).
        # Separate from folder_recents — the dialog's mental model is "recently
        # queued but not yet fully analyzed", independent of the main tree.
        out['analyze_recents'] = _sanitize_analyze_recents(data.get('analyze_recents'), max_items=8)

    # Local performance samples for the Analyze Folders dialog's time estimate.
    # The queue worker appends one entry per completed folder run; the dialog
    # averages them to display per-machine time estimates instead of hardcoded
    # baselines. GPU and CPU runs are bucketed separately because rates differ
    # 2-3×. See plan: Est. Time System.
    if 'perf_samples_gpu' in data:
        out['perf_samples_gpu'] = _sanitize_perf_samples(data.get('perf_samples_gpu'), max_items=50)
    if 'perf_samples_cpu' in data:
        out['perf_samples_cpu'] = _sanitize_perf_samples(data.get('perf_samples_cpu'), max_items=50)

    _set_bool('main_tutorial_seen', default=False)
    if 'kestrel_donate_thresholds_shown' in data:
        out['kestrel_donate_thresholds_shown'] = _sanitize_int_list(
            data.get('kestrel_donate_thresholds_shown'),
            max_items=128,
            min_value=0,
            max_value=1_000_000_000,
        )

    _set_int('kestrel_impact_total_files', default=0, min_value=0, max_value=1_000_000_000_000)
    _set_float('kestrel_impact_total_seconds', default=0.0, min_value=0.0, max_value=31_536_000_000.0, digits=1)

    _set_str('machine_id', max_len=128)
    _set_str('version', max_len=64)
    _set_str('legal_agreed_version', max_len=64)
    _set_str('legal_agreed_date', max_len=32)
    _set_str('last_open_ping_utc', max_len=32)
    _set_bool('installed_telemetry_sent', default=False)
    _set_bool('legal_upgrade_self_heal_2026_03', default=False)

    _set_path('active_analysis_path')
    # Override the cloud-compute Worker URL (handy for staging / wrangler dev).
    # Empty string falls back to the env var KESTREL_CC_API_BASE then the
    # hardcoded default in cloud_compute_client.default_api_base().
    _set_str('cloud_compute_api_base', default='', max_len=256)
    _set_str('auth_api_base', default='', max_len=256)
    # Cloud-compute analysis settings are NOT a separate override block —
    # they're projected from the same advanced-analysis settings the local
    # queue uses (detection_threshold, detector_name, etc.) at submit time.
    # See api_bridge._cc_build_analysis_settings_from_local. One source of
    # truth across local + cloud; the wire allowlist lives on both the
    # client (cloud_compute_client.ANALYSIS_SETTINGS_ALLOWLIST) and the
    # Worker / Modal sides (defence in depth).
    _set_str('app_session_started_utc', max_len=64)
    _set_bool('app_session_closed_cleanly', default=True)
    _set_int('app_session_pid', default=0, min_value=0, max_value=2_147_483_647)
    _set_str('last_session_closed_utc', max_len=64)
    _set_str('last_unclean_shutdown_utc', max_len=64)

    # Phase 3: queue_recovery_state allowlist entry removed. Any pre-existing
    # value in user settings files falls through to passthrough and is dropped
    # on the next save (the analyze_recents mechanism replaces this feature).

    if 'pending_analytics' in data:
        pending = _sanitize_pending_analytics(data.get('pending_analytics'))
        if pending is not None:
            out['pending_analytics'] = pending

    # Forward compatibility: preserve unknown keys (e.g. settings added by a
    # newer build) so an older Kestrel doesn't drop them on the round-trip.
    # ``_merge_forward_compatible_keys`` copies JSON-safe values only and logs
    # any keys whose structure was unrecoverable.
    _merge_forward_compatible_keys(out, data, emit_log=emit_log)

    return out


# --- Cumulative counters that must never regress across save/load cycles ---
# Each entry is (key, coerce_fn). The coerce_fn returns None on unparseable input.
def _coerce_counter_int(v: Any) -> int | None:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _coerce_counter_float(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


_MONOTONIC_COUNTERS: tuple[tuple[str, Any], ...] = (
    ('kestrel_impact_total_files', _coerce_counter_int),
    ('kestrel_impact_total_seconds', _coerce_counter_float),
)


def _load_settings_raw() -> tuple[dict | None, str]:
    """Read ``settings.json`` verbatim, returning ``(data, status)``.

    ``status`` is one of:
      * ``'ok'``      — file parsed, ``data`` is the raw dict.
      * ``'missing'`` — file does not exist; ``data`` is ``None``.
      * ``'corrupt'`` — file exists but failed to parse as a JSON object.

    This is the single source of truth for distinguishing "new install" from
    "existing file the app cannot read". Callers that need to overwrite the
    file use ``'corrupt'`` to bail out instead of silently clobbering data
    a user may still be able to recover manually.
    """
    path = _get_settings_path()
    if not os.path.exists(path):
        return None, 'missing'
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        warn(f'[settings] could not parse {path}: {exc}')
        return None, 'corrupt'
    if not isinstance(data, dict):
        return None, 'corrupt'
    return data, 'ok'


def _load_backup_if_valid() -> dict | None:
    """Return the ``.bak`` sidecar contents if present and parseable, else None."""
    path = _get_settings_path()
    bak = path + '.bak'
    if not os.path.exists(bak):
        return None
    try:
        with open(bak, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        warn(f'[settings] .bak is also unreadable: {exc}')
        return None
    return data if isinstance(data, dict) else None


def _quarantine_corrupt_settings(path: str) -> str | None:
    """Move the corrupt ``settings.json`` aside for manual recovery, so the
    next save can proceed with a fresh file. Returns the quarantine path or
    ``None`` on failure.
    """
    try:
        ts = time.strftime('%Y%m%d-%H%M%S')
        quarantine = f'{path}.corrupt-{ts}'
        # Avoid clobbering a previous quarantine from the same second.
        attempt = 0
        while os.path.exists(quarantine):
            attempt += 1
            quarantine = f'{path}.corrupt-{ts}-{attempt}'
        shutil.copy2(path, quarantine)
        warn(f'[settings] Quarantined corrupt settings file to {quarantine}')
        return quarantine
    except OSError as exc:
        error(f'[settings] Failed to quarantine corrupt settings: {exc}')
        return None


def _apply_monotonic_guard(incoming: dict, existing: dict | None) -> dict:
    """Return a copy of ``incoming`` with cumulative counters clamped to at
    least the value in ``existing``. Protects against data loss when a stale
    caller (e.g. a race between queue_manager and the settings dialog) would
    otherwise regress a counter.

    If ``incoming`` OMITS a counter key entirely but ``existing`` has one,
    the existing value is resurrected into the output. Callers that mutate
    only a subset of settings (the UI does this on every save) must not be
    able to silently zero a counter by virtue of not sending it.
    """
    if not isinstance(existing, dict):
        return dict(incoming)
    out = dict(incoming)
    for key, coerce in _MONOTONIC_COUNTERS:
        prev = coerce(existing.get(key))
        if prev is None:
            continue
        if key not in out:
            out[key] = prev
            continue
        new = coerce(out.get(key))
        if new is None or new < prev:
            out[key] = prev
    return out


def load_persisted_settings() -> dict:
    """Load ``settings.json`` from the user data directory.

    Core keys are validated/coerced; any additional keys present in the file are
    preserved when JSON-safe (forward compatibility across app versions).

    If the main file is unreadable, transparently falls back to the ``.bak``
    sidecar written by the last successful save, so a partial write or disk
    glitch does not silently wipe cumulative state like the impact counter.
    """
    data, status = _load_settings_raw()
    if status == 'ok':
        return _sanitize_settings_payload(data, emit_log=False)
    if status == 'corrupt':
        bak_data = _load_backup_if_valid()
        if bak_data is not None:
            warn('[settings] Main settings file is corrupt; serving from .bak.')
            return _sanitize_settings_payload(bak_data, emit_log=False)
    return {}


def _reap_orphan_tmp_files(directory: str) -> None:
    """Best-effort cleanup of ``settings.json.*.tmp`` orphans from crashed
    or racing saves. Silent on any error — these files are harmless; they
    just waste a few bytes until next startup.
    """
    try:
        pattern = os.path.join(directory, _TMP_FILE_PREFIX + '*' + _TMP_FILE_SUFFIX)
        for orphan in glob.glob(pattern):
            try:
                os.remove(orphan)
            except OSError:
                pass
    except Exception:
        pass


def save_persisted_settings(data: dict) -> None:
    """Atomically persist settings with corruption-aware integrity guards.

    Behaviour:
      * Serialized in-process by ``_SAVE_LOCK`` so concurrent callers (JS
        bridge, queue worker, startup) cannot race on the temp file.
      * Each save writes to a **unique** temp file via ``tempfile.mkstemp``
        so two racing saves never overwrite or delete each other's tmp —
        this is the fix for the ``WinError 2`` we hit when the old code
        hardcoded ``settings.json.tmp``.
      * If the on-disk file is corrupt and a valid ``.bak`` exists, the corrupt
        file is quarantined to ``<path>.corrupt-<ts>`` and the save proceeds,
        using ``.bak`` for the monotonic-counter guard so impact totals etc.
        are not lost in the recovery.
      * If the on-disk file is corrupt and ``.bak`` is also unusable, the save
        is **refused** — the corrupt file is preserved verbatim so the user
        can examine or restore it manually. Running app state stays in memory.
      * On success, the previous good ``settings.json`` is promoted to
        ``settings.json.bak`` before the atomic ``os.replace`` of the new file.
      * The previous non-atomic fallback (a direct write on ``os.replace``
        failure) has been removed — it was the root cause of partial writes
        that in turn caused the data-loss symptom being guarded against here.
    """
    if not isinstance(data, dict):
        raise ValueError('Settings payload must be an object')

    with _SAVE_LOCK:
        path = _get_settings_path()
        directory = os.path.dirname(path)
        existing_raw, status = _load_settings_raw()

        if status == 'corrupt':
            bak_data = _load_backup_if_valid()
            if bak_data is None:
                error(
                    f'[settings] REFUSING to save over unreadable {path}; '
                    f'no valid .bak to recover from. '
                    f'Remove or repair the file manually, then retry.'
                )
                return
            # Move the corrupt file aside so the atomic write below has a clean slate.
            _quarantine_corrupt_settings(path)
            try:
                os.remove(path)
            except OSError:
                # Not fatal — os.replace below will try to overwrite regardless.
                pass
            existing_raw = bak_data
            status = 'missing'

        data = _apply_monotonic_guard(data, existing_raw)

        # Re-sanitize while preserving forward-compatible keys so merges from the UI
        # cannot strip unknown keys that were loaded from disk.
        data = _sanitize_settings_payload(data, emit_log=True)

        # --- Flush pending analytics on consent ---
        if data.get('analytics_consent_shown', False) and 'pending_analytics' in data:
            pending = data.pop('pending_analytics')
            if data.get('analytics_opted_in', False) and _telemetry is not None:
                try:
                    _telemetry.send_folder_analytics(**pending)
                    info('[analytics] Flushed pending detailed analytics after opt-in.')
                except Exception as e:
                    warn(f'[analytics] Failed to flush pending analytics: {e}')
        # ------------------------------------------

        os.makedirs(directory, exist_ok=True)

        # Unique temp file per save. ``mkstemp`` atomically creates and opens
        # the file with O_EXCL so no two calls can ever produce the same path.
        try:
            tmp_fd, tmp = tempfile.mkstemp(
                prefix=_TMP_FILE_PREFIX,
                suffix=_TMP_FILE_SUFFIX,
                dir=directory,
            )
        except OSError as exc:
            error(
                f'[settings] FAILED to create temp file for atomic save ({exc}); '
                f'existing file left unchanged. Check disk space and permissions '
                f'on {directory}.'
            )
            return

        try:
            try:
                with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, sort_keys=True)
                    try:
                        f.flush()
                        os.fsync(f.fileno())
                    except OSError:
                        # fsync can legitimately fail on some network filesystems;
                        # proceed with the atomic replace anyway.
                        pass
            except Exception:
                # fdopen took ownership of the fd; if the with-block raised,
                # the fd is already closed. Just make sure we clean up the
                # orphan temp file in the outer except.
                raise

            # Promote the previous good file to .bak before replacement so that
            # a future corrupt-main scenario can auto-recover.
            if status == 'ok':
                try:
                    shutil.copy2(path, path + '.bak')
                except OSError as exc:
                    warn(f'[settings] could not refresh .bak: {exc}')

            os.replace(tmp, path)
        except OSError as exc:
            # Do NOT fall back to a non-atomic direct write — that path is how
            # partial writes corrupt settings.json in the first place. Leave the
            # existing (possibly older) file in place and log loudly.
            error(
                f'[settings] FAILED to atomically save settings ({exc}); '
                f'existing file left unchanged. Check disk space, permissions, '
                f'and antivirus locks on {path}.'
            )
            try:
                os.remove(tmp)
            except OSError:
                pass
        else:
            # Successful save — reap any orphans left by prior crashed saves.
            _reap_orphan_tmp_files(directory)


# ---------------------------------------------------------------------------
# Leveled logging
# ---------------------------------------------------------------------------
# All output goes to stderr (captured by ``_TeeStream`` in visualizer.py into
# ~/.kestrel/logs/kestrel_runtime_*.log). Threshold is read per-emit from the
# ``KESTREL_LOG_LEVEL`` env var; default INFO. Setting it to DEBUG re-enables
# the per-image / per-detection / per-batch traces that are normally silenced.
#
# Line format: ``[serve][LEVEL] <message>``. Callers preserve their existing
# ``[area]`` tag inside the message (e.g. ``[settings] ...``, ``[culling] ...``)
# for grep-filtering.

_LEVELS = ('DEBUG', 'INFO', 'WARN', 'ERROR')
_LEVEL_VALUES = {name: idx for idx, name in enumerate(_LEVELS)}


def _current_log_threshold() -> str:
    raw = os.environ.get('KESTREL_LOG_LEVEL', 'INFO').upper()
    return raw if raw in _LEVEL_VALUES else 'INFO'


def _emit_log(level: str, args: tuple) -> None:
    if _LEVEL_VALUES[level] < _LEVEL_VALUES[_current_log_threshold()]:
        return
    msg = ' '.join(str(a) for a in args)
    print(f'[serve][{level}] {msg}', file=sys.stderr, flush=True)


# The level helpers accept and discard arbitrary kwargs so they're a
# drop-in replacement for ``print(..., flush=True)`` call sites without
# requiring every conversion to also strip the keyword arguments.

def debug(*args, **_kwargs) -> None:
    _emit_log('DEBUG', args)


def info(*args, **_kwargs) -> None:
    _emit_log('INFO', args)


def warn(*args, **_kwargs) -> None:
    _emit_log('WARN', args)


def error(*args, **_kwargs) -> None:
    _emit_log('ERROR', args)


# Backwards-compat alias. Existing ``log(...)`` call sites stay valid and
# emit at INFO level; we retag the ones that should be WARN/ERROR/DEBUG
# individually.
log = info


def _normalize(p: str) -> str:
    if not p:
        return ''
    p = os.path.expanduser(p)
    if (p.startswith('"') and p.endswith('"')) or (p.startswith("'") and p.endswith("'")):
        p = p[1:-1]
    return os.path.normpath(p)
