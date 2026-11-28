"""Settings, enhanced GPU and bird-region methods of the JS bridge.

``api_bridge.Api`` inherits these mixins, so the method names the frontend
calls (``window.pywebview.api.get_settings`` and so on) are unchanged. Each
mixin holds only bridge glue; the logic lives in ``settings_utils``,
``kestrel_analyzer.ml.enhanced_gpu`` and ``kestrel_analyzer.bird_catalog``.
"""
from __future__ import annotations

import json
import threading

from settings_utils import load_persisted_settings, save_persisted_settings, error
from queue_manager import _queue_manager

try:
    import kestrel_telemetry as _telemetry
except ImportError:  # telemetry is optional; settings work without it
    _telemetry = None  # type: ignore[assignment]

# The quality-feedback install id is managed by Python only (see the Quality
# feedback section of Api); it never goes to or comes from the frontend.
_QF_INSTALL_ID_KEY = 'quality_feedback_install_id'

# Session / shutdown bookkeeping owned by visualizer._mark_session_start,
# the clean-exit path and clear_recovery_state. The frontend caches a full
# get_settings() snapshot in localStorage at startup and sends it back whole
# on many later saves (folder recents, mergeSetting, the settings dialog), so
# without this filter a stale copy re-adds last_unclean_shutdown_utc after the
# user has dealt with the recovery prompt, and the same unclean shutdown is
# offered for reporting again on a later launch.
_SESSION_OWNED_SETTINGS_KEYS = frozenset({
    'last_unclean_shutdown_utc',
    'last_exit_reason',
    'app_session_exit_reason',
    'exit_reason_migrated_v1',
    'app_session_started_utc',
    'app_session_closed_cleanly',
    'app_session_pid',
    'last_session_closed_utc',
})
# Written only by Python: enhanced GPU setup turns it on, and the Settings
# switch calls enhanced_gpu_set_enabled the moment it changes. Whole-object
# frontend saves (mergeSetting, the settings dialog) carry a cached copy that
# can predate setup, so they must not write it back.
_ENHANCED_GPU_ENABLED_KEY = 'enhanced_gpu_enabled'


class SettingsBridgeMixin:
    """App settings: read, save (with the Python-owned keys filtered out), rating profiles."""

    def get_settings(self):
        """Return persisted settings, ensuring machine_id and version exist."""
        try:
            settings = load_persisted_settings()
            if _telemetry is not None:
                _telemetry.get_machine_id(settings)
            if _telemetry is not None:
                settings['version'] = _telemetry._read_version()
            save_persisted_settings(settings)
            # The quality-feedback install id is managed by Python only (see
            # the Quality feedback section); keep it out of the frontend's
            # localStorage so a stale settings snapshot can't resurrect a
            # forgotten id.
            settings = {k: v for k, v in settings.items() if k != _QF_INSTALL_ID_KEY}
            return {'success': True, 'settings': settings}
        except Exception as e:
            error(f'[API] get_settings error: {e}')
            return {'success': False, 'error': str(e), 'settings': {}}

    def get_rating_thresholds(self):
        """Return the active rating profile's quality-score cutoffs for each star.

        The frontend needs these to draw star positions on a quality-score
        number line (Culling Assistant) and to convert a manual star rating
        back into the quality band it represents. Served from
        ``kestrel_analyzer.ratings`` so the table has exactly one definition.

        Returns:
            {
              'success': bool,
              'profile': str,                # active rating_profile setting
              'thresholds': {'five': float, 'four': float, 'three': float, 'two': float},
              'profiles': {name: thresholds, ...},   # every built-in profile
              'error': str,
            }
        """
        try:
            try:
                from kestrel_analyzer.ratings import RATING_PROFILES, resolve_thresholds
            except ImportError:
                from analyzer.kestrel_analyzer.ratings import (  # type: ignore
                    RATING_PROFILES,
                    resolve_thresholds,
                )

            settings = load_persisted_settings()
            profile = str(settings.get('rating_profile', 'balanced') or 'balanced').lower()
            thresholds = resolve_thresholds(profile, settings.get('rating_thresholds_custom'))
            return {
                'success': True,
                'profile': profile,
                'thresholds': dict(thresholds),
                'profiles': {k: dict(v) for k, v in RATING_PROFILES.items()},
                'error': '',
            }
        except Exception as e:
            error(f'[API] get_rating_thresholds error: {e}')
            return {'success': False, 'error': str(e), 'profile': '', 'thresholds': {}, 'profiles': {}}

    def save_settings_data(self, settings_dict):
        """Persist settings from JavaScript (wraps save_persisted_settings)."""
        try:
            if not isinstance(settings_dict, dict):
                return {'success': False, 'error': 'Invalid settings'}
            # Merge into existing persisted settings so stale/minimal frontend
            # payloads cannot drop unrelated keys (for example legal consent flags).
            existing = load_persisted_settings()
            if not isinstance(existing, dict):
                existing = {}
            # Python-owned keys the frontend must never overwrite.
            settings_dict = {
                k: v for k, v in settings_dict.items()
                if k != _QF_INSTALL_ID_KEY and k != _ENHANCED_GPU_ENABLED_KEY
                and k not in _SESSION_OWNED_SETTINGS_KEYS
            }
            merged = {**existing, **settings_dict}

            # Keep cumulative impact counters monotonic so stale UI payloads cannot
            # accidentally reset totals to a lower value.
            def _coerce_number(v):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None

            prev_files = _coerce_number(existing.get('kestrel_impact_total_files'))
            new_files = _coerce_number(merged.get('kestrel_impact_total_files'))
            if prev_files is not None and (new_files is None or new_files < prev_files):
                merged['kestrel_impact_total_files'] = int(prev_files)

            prev_secs = _coerce_number(existing.get('kestrel_impact_total_seconds'))
            new_secs = _coerce_number(merged.get('kestrel_impact_total_seconds'))
            if prev_secs is not None and (new_secs is None or new_secs < prev_secs):
                merged['kestrel_impact_total_seconds'] = prev_secs

            save_persisted_settings(merged)
            return {'success': True}
        except Exception as e:
            error(f'[API] save_settings_data error: {e}')
            return {'success': False, 'error': str(e)}


class EnhancedGpuBridgeMixin:
    """Opt-in vendor GPU acceleration (Windows only): setup worker, status, on/off, report text."""

    # Setup runs on a worker thread; the UI polls enhanced_gpu_status() for progress. Nothing here
    # touches Windows ML unless the user starts setup, so macOS and unsupported Windows are unaffected.
    _eg_lock = threading.Lock()
    _eg_job: dict = {'running': False, 'stage': '', 'message': '', 'cancel': False, 'result': None}

    def _eg_module(self):
        from kestrel_analyzer.ml import enhanced_gpu
        return enhanced_gpu

    def enhanced_gpu_status(self):
        """Settings-dialog state: availability, whether set up / enabled, per-model results, setup progress."""
        try:
            eg = self._eg_module()
            st = eg.status()
            st['runtime'] = dict(eg.runtime_summary(), cpu_components=[f.get('kind') for f in _queue_manager.cpu_fallbacks()])
            with self._eg_lock:
                job = {k: v for k, v in self._eg_job.items() if k != 'cancel'}
            st['job'] = job
            return {'success': True, 'status': st}
        except Exception as e:
            return {'success': False, 'error': str(e), 'status': {'available': False}}

    def enhanced_gpu_start_setup(self):
        """Start the set-up worker (platform check, EP download, validation). Returns at once."""
        try:
            eg = self._eg_module()
            if not eg.platform_status().get('available'):
                return {'success': False, 'error': eg.platform_status().get('reason', 'Not available')}
            with self._eg_lock:
                if self._eg_job.get('running'):
                    return {'success': False, 'error': 'Setup is already running.'}
                self._eg_job.update(running=True, stage='platform', message='Starting...', cancel=False, result=None)

            def _progress(stage, message):
                with self._eg_lock:
                    self._eg_job.update(stage=stage, message=message)

            def _work():
                result = None
                try:
                    result = eg.run_setup(_progress, lambda: bool(self._eg_job.get('cancel')))
                    if result.get('ok'):
                        # Turn the mode on once it has been validated; the user can switch it off.
                        s = load_persisted_settings()
                        s['enhanced_gpu_enabled'] = True
                        save_persisted_settings(s)
                        eg.reset_cache()
                except Exception as e:  # run_setup should not raise, but never leave 'running' stuck
                    result = {'ok': False, 'reason': f'{type(e).__name__}: {e}'}
                finally:
                    with self._eg_lock:
                        self._eg_job.update(running=False, stage='done', message='',
                                            result={'ok': bool(result and result.get('ok')),
                                                    'reason': (result or {}).get('reason', '')})

            threading.Thread(target=_work, name='enhanced-gpu-setup', daemon=True).start()
            return {'success': True}
        except Exception as e:
            error(f'[API] enhanced_gpu_start_setup error: {e}')
            return {'success': False, 'error': str(e)}

    def enhanced_gpu_cancel_setup(self):
        with self._eg_lock:
            self._eg_job['cancel'] = True
        return {'success': True}

    def enhanced_gpu_set_enabled(self, enabled):
        """Turn the mode on or off (off returns every model to DirectML on the next analysis)."""
        try:
            s = load_persisted_settings()
            s['enhanced_gpu_enabled'] = bool(enabled)
            save_persisted_settings(s)
            self._eg_module().reset_cache()
            return {'success': True}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    def enhanced_gpu_report_text(self):
        """Plain-text summary for a bug report about the setup (the log tail is attached separately)."""
        try:
            eg = self._eg_module()
            st = eg.load_state() or {}
            lines = ['Enhanced GPU performance setup did not work.', '',
                     f"Result: {st.get('reason') or 'see log'}",
                     f"Vendor EP tried: {st.get('ep_name') or 'none'}",
                     f"Models: {json.dumps(st.get('models', {}))}",
                     f"Hardware: {json.dumps(st.get('hardware', {}))}"]
            return {'success': True, 'text': '\n'.join(lines)[:6000]}
        except Exception as e:
            return {'success': False, 'error': str(e), 'text': ''}

    def gpu_mode_report_text(self):
        """Plain-text summary for a bug report about the GPU mode (enhanced / standard / CPU fallback)."""
        try:
            return {'success': True, 'text': self._eg_module().report_text(_queue_manager.cpu_fallbacks())}
        except Exception as e:
            return {'success': False, 'error': str(e), 'text': ''}
