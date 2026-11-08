"""A stale frontend settings snapshot must not undo clear_recovery_state.

The frontend copies the whole get_settings() result into localStorage at
startup (settings.js hydrateSettingsFromServer) and later sends that copy
back whole through save_settings_data (folder recents, mergeSetting, the
settings dialog). If the startup snapshot carried last_unclean_shutdown_utc,
that save re-added the flag after the user had already answered the
recovery prompt, and a later launch offered the same unclean shutdown for
reporting again.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    import api_bridge
    import visualizer
except Exception as e:  # pragma: no cover - environment-dependent
    pytest.skip(f'bridge/visualizer not importable in this env: {e}', allow_module_level=True)


pytestmark = pytest.mark.unit

UNCLEAN = '2026-01-01T05:00:00.000000Z'


@pytest.fixture
def store(monkeypatch):
    data = {}

    def _load():
        return dict(data)

    def _save(d):
        data.clear()
        data.update(d)

    for mod in (api_bridge, visualizer):
        monkeypatch.setattr(mod, 'load_persisted_settings', _load)
        monkeypatch.setattr(mod, 'save_persisted_settings', _save)
    monkeypatch.setattr(visualizer, '_start_native_crash_probe', lambda *a, **k: None)
    return data


def test_stale_snapshot_does_not_resurrect_unclean_flag(store):
    api = api_bridge.Api()
    store.update({
        'last_unclean_shutdown_utc': UNCLEAN,
        'last_exit_reason': 'unknown',
        'editor': 'darktable',
    })
    # Startup: the frontend caches the full settings, flag included.
    snapshot = api.get_settings()['settings']
    assert snapshot['last_unclean_shutdown_utc'] == UNCLEAN

    # The user answers the recovery prompt.
    assert api.clear_recovery_state(False)['success']
    assert 'last_unclean_shutdown_utc' not in store

    # Later, e.g. picking a folder bumps folder_recents and sends the cached copy back.
    snapshot['folder_recents'] = ['/photos/a']
    assert api.save_settings_data(snapshot)['success']

    assert 'last_unclean_shutdown_utc' not in store
    assert store['folder_recents'] == ['/photos/a']
    assert api.get_recovery_status()['unclean_shutdown'] is False


def test_session_keys_owned_by_python(store):
    api = api_bridge.Api()
    store.update({
        'app_session_started_utc': 'NEW',
        'app_session_pid': 222,
        'app_session_exit_reason': 'unknown',
        'last_session_closed_utc': 'LATEST',
    })
    stale = {
        'app_session_started_utc': 'OLD',
        'app_session_pid': 111,
        'app_session_exit_reason': 'clean',
        'last_session_closed_utc': 'EARLIER',
        'last_exit_reason': 'crash',
        'exit_reason_migrated_v1': False,
        'app_session_closed_cleanly': True,
        'last_unclean_shutdown_utc': UNCLEAN,
        'editor': 'gimp',
    }
    assert api.save_settings_data(stale)['success']
    assert store == {
        'app_session_started_utc': 'NEW',
        'app_session_pid': 222,
        'app_session_exit_reason': 'unknown',
        'last_session_closed_utc': 'LATEST',
        'editor': 'gimp',
    }


def test_second_instance_does_not_reprompt_after_clear(store, monkeypatch):
    """The field sequence: launch flags the prior session, the user reports and
    clears, a folder pick saves the stale snapshot, then a second launch finds
    the first one still running and leaves any pending flag in place."""
    api = api_bridge.Api()
    store.update({
        'app_session_exit_reason': 'unknown',
        'exit_reason_migrated_v1': True,
        'app_session_started_utc': UNCLEAN,
        'app_session_pid': 1,
    })
    monkeypatch.setattr(visualizer, '_prior_session_still_running', lambda *a: False)
    monkeypatch.setattr(visualizer, '_prior_session_too_stale_to_prompt', lambda *a: False)
    visualizer._mark_session_start()
    assert store['last_unclean_shutdown_utc'] == UNCLEAN

    snapshot = api.get_settings()['settings']
    assert api.get_recovery_status()['unclean_shutdown'] is True
    api.clear_recovery_state(False)
    snapshot['folder_recents'] = ['/photos/b']
    api.save_settings_data(snapshot)

    monkeypatch.setattr(visualizer, '_prior_session_still_running', lambda *a: True)
    visualizer._mark_session_start()
    assert api.get_recovery_status()['unclean_shutdown'] is False
