"""Unit tests for the clean/unclean shutdown tracker's instrumentation.

The recovery dialog fires on the *next* launch, so when it fires wrongly the
only evidence is whatever the previous session wrote to its log. These tests
pin the behaviour of that instrumentation: every state transition is logged
with its old and new value plus the writer that caused it, every write is
read back, and a lost write is reported loudly rather than silently.

They also pin the invariant that the logging is observability only — adding
it must not change what the tracker persists.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    import visualizer
    from visualizer import (
        EXIT_REASON_KEY,
        _log_shutdown_state,
        _mark_session_clean_exit,
        _mark_session_exit_reason,
        _parse_session_timestamp,
        _pid_is_alive,
        _prior_session_still_running,
        _settings_file_hint,
        _utc_now_iso,
        _verify_exit_reason_persisted,
    )
except Exception as e:  # pragma: no cover - environment-dependent
    pytest.skip(f'visualizer module not importable in this env: {e}', allow_module_level=True)


pytestmark = pytest.mark.unit


@pytest.fixture
def fake_settings(monkeypatch):
    """Back load/save_persisted_settings with an in-memory dict."""
    store = {'data': {}}

    def _load():
        return dict(store['data'])

    def _save(payload):
        store['data'] = dict(payload)

    monkeypatch.setattr(visualizer, 'load_persisted_settings', _load)
    monkeypatch.setattr(visualizer, 'save_persisted_settings', _save)
    return store


@pytest.fixture
def shutdown_log(capsys):
    """Collect the [shutdown] lines emitted during a test."""
    def _read():
        err = capsys.readouterr().err
        return [ln for ln in err.splitlines() if '[shutdown]' in ln]
    return _read


class TestPidLiveness:
    """``_pid_is_alive`` is how we detect a second instance clobbering the
    first one's bookkeeping — the leading hypothesis for the false
    positives. It must never raise, whatever it is handed."""

    def test_current_process_is_alive(self):
        assert _pid_is_alive(os.getpid()) is True

    def test_zero_and_negative_are_undeterminable(self):
        assert _pid_is_alive(0) is None
        assert _pid_is_alive(-1) is None

    def test_unclaimed_pid_reports_not_alive_or_unknown(self):
        # Never assert False outright: a recycled pid could legitimately be
        # live on a busy machine. The contract is only "returns a tri-state
        # without raising".
        assert _pid_is_alive(2_000_000_000) in (True, False, None)

    def test_garbage_input_does_not_raise(self):
        assert _pid_is_alive(None) is None


class TestPriorSessionStillRunning:
    """A second instance launched while the first is open must not report
    the first one — still on screen — as an unclean shutdown."""

    def test_live_pid_from_a_recent_session_is_still_running(self):
        assert _prior_session_still_running(os.getpid(), _utc_now_iso()) is True

    def test_dead_pid_is_not_still_running(self, monkeypatch):
        monkeypatch.setattr(visualizer, '_pid_is_alive', lambda _pid: False)
        assert _prior_session_still_running(4242, _utc_now_iso()) is False

    def test_undeterminable_pid_is_not_still_running(self, monkeypatch):
        monkeypatch.setattr(visualizer, '_pid_is_alive', lambda _pid: None)
        assert _prior_session_still_running(4242, _utc_now_iso()) is False

    def test_live_pid_from_a_stale_session_reads_as_pid_reuse(self, monkeypatch):
        """Pids are recycled; a weeks-old session matching a live pid is far
        more likely an unrelated process than the same app still running."""
        monkeypatch.setattr(visualizer, '_pid_is_alive', lambda _pid: True)
        assert _prior_session_still_running(4242, '2026-07-01T00:00:00Z') is False

    def test_missing_or_unparseable_timestamp_fails_closed(self, monkeypatch):
        monkeypatch.setattr(visualizer, '_pid_is_alive', lambda _pid: True)
        assert _prior_session_still_running(4242, '') is False
        assert _prior_session_still_running(4242, 'not-a-timestamp') is False

    def test_small_clock_skew_is_tolerated(self, monkeypatch):
        from datetime import timedelta
        monkeypatch.setattr(visualizer, '_pid_is_alive', lambda _pid: True)
        just_ahead = (visualizer.datetime.utcnow() + timedelta(seconds=5)).isoformat() + 'Z'
        assert _prior_session_still_running(4242, just_ahead) is True


class TestParseSessionTimestamp:
    def test_round_trips_utc_now_iso(self):
        assert _parse_session_timestamp(_utc_now_iso()) is not None

    def test_empty_and_garbage_return_none(self):
        assert _parse_session_timestamp('') is None
        assert _parse_session_timestamp(None) is None
        assert _parse_session_timestamp('yesterday') is None


class TestLogHelpers:
    def test_log_shutdown_state_emits_tagged_line(self, shutdown_log):
        _log_shutdown_state('unit_test', alpha='one', beta=2)
        lines = shutdown_log()
        assert len(lines) == 1
        assert 'unit_test' in lines[0]
        assert 'alpha=one' in lines[0]
        assert 'beta=2' in lines[0]

    def test_log_shutdown_state_omits_none_fields(self, shutdown_log):
        _log_shutdown_state('unit_test', present='yes', absent=None)
        line = shutdown_log()[0]
        assert 'present=yes' in line
        assert 'absent' not in line

    def test_log_shutdown_state_never_raises(self):
        class Exploding:
            def __repr__(self):
                raise RuntimeError('boom')
        # Must be swallowed — this runs on shutdown paths.
        _log_shutdown_state('unit_test', bad=Exploding())

    def test_settings_file_hint_leaks_no_home_directory(self):
        hint = _settings_file_hint()
        if hint is None:
            pytest.skip('settings path unavailable in this env')
        # One parent dir + filename only; never an absolute path, which on
        # Windows/macOS embeds the username and would land in crash reports.
        assert not os.path.isabs(hint)
        assert hint.count(os.sep) <= 1


class TestWriteVerification:
    def test_matching_value_logs_persisted(self, fake_settings, shutdown_log):
        fake_settings['data'] = {EXIT_REASON_KEY: 'clean'}
        _verify_exit_reason_persisted('unit', 'clean')
        assert any('persisted' in ln for ln in shutdown_log())

    def test_lost_write_is_reported_loudly(self, fake_settings, shutdown_log):
        fake_settings['data'] = {EXIT_REASON_KEY: 'unknown'}
        _verify_exit_reason_persisted('unit', 'clean')
        lines = shutdown_log()
        assert any('WRITE DID NOT STICK' in ln for ln in lines)
        assert any("expected='clean'" in ln and "on_disk='unknown'" in ln for ln in lines)

    def test_read_failure_is_reported_not_raised(self, monkeypatch, shutdown_log):
        def _boom():
            raise OSError('disk gone')
        monkeypatch.setattr(visualizer, 'load_persisted_settings', _boom)
        _verify_exit_reason_persisted('unit', 'clean')
        assert any('verification read failed' in ln for ln in shutdown_log())


class TestExitReasonTransitions:
    def test_transition_logs_old_and_new_and_source(self, fake_settings, shutdown_log):
        fake_settings['data'] = {EXIT_REASON_KEY: 'unknown'}
        _mark_session_exit_reason('crash', source='unit-test')
        line = next(ln for ln in shutdown_log() if 'transition' in ln)
        assert "previous='unknown'" in line
        assert 'new=crash' in line
        assert 'source=unit-test' in line

    def test_transition_persists_the_value(self, fake_settings):
        fake_settings['data'] = {EXIT_REASON_KEY: 'unknown'}
        _mark_session_exit_reason('os_shutdown', source='unit-test')
        assert fake_settings['data'][EXIT_REASON_KEY] == 'os_shutdown'

    def test_clean_reason_clears_the_unclean_flag(self, fake_settings):
        fake_settings['data'] = {
            EXIT_REASON_KEY: 'unknown',
            'last_unclean_shutdown_utc': '2026-07-01T00:00:00Z',
        }
        _mark_session_exit_reason('clean', source='unit-test')
        assert 'last_unclean_shutdown_utc' not in fake_settings['data']
        assert fake_settings['data']['app_session_closed_cleanly'] is True

    def test_invalid_reason_is_rejected_and_logged(self, fake_settings, shutdown_log):
        fake_settings['data'] = {EXIT_REASON_KEY: 'unknown'}
        _mark_session_exit_reason('bogus', source='unit-test')
        assert any('REJECTED' in ln for ln in shutdown_log())
        # State untouched.
        assert fake_settings['data'][EXIT_REASON_KEY] == 'unknown'

    def test_save_failure_is_logged_not_swallowed(self, monkeypatch, fake_settings, shutdown_log):
        def _boom(_payload):
            raise OSError('read-only filesystem')
        monkeypatch.setattr(visualizer, 'save_persisted_settings', _boom)
        _mark_session_exit_reason('crash', source='unit-test')
        assert any('FAILED to record' in ln for ln in shutdown_log())

    def test_default_source_is_recorded(self, fake_settings, shutdown_log):
        fake_settings['data'] = {EXIT_REASON_KEY: 'unknown'}
        _mark_session_exit_reason('crash')
        assert any('source=unspecified' in ln for ln in shutdown_log())


class TestCleanExit:
    def test_marks_clean_and_logs_entry_with_source(self, fake_settings, shutdown_log):
        fake_settings['data'] = {EXIT_REASON_KEY: 'unknown'}
        _mark_session_clean_exit(source='main:finally')
        lines = shutdown_log()
        assert any('clean_exit: entered' in ln and 'source=main:finally' in ln for ln in lines)
        assert any('marking clean' in ln for ln in lines)
        assert fake_settings['data'][EXIT_REASON_KEY] == 'clean'

    @pytest.mark.parametrize('preserved', ['crash', 'os_shutdown'])
    def test_does_not_downgrade_an_earlier_verdict(self, fake_settings, shutdown_log, preserved):
        fake_settings['data'] = {EXIT_REASON_KEY: preserved}
        _mark_session_clean_exit(source='main:finally')
        assert fake_settings['data'][EXIT_REASON_KEY] == preserved
        assert any(f'preserved={preserved}' in ln for ln in shutdown_log())

    def test_clears_stale_unclean_flag(self, fake_settings):
        fake_settings['data'] = {
            EXIT_REASON_KEY: 'unknown',
            'last_unclean_shutdown_utc': '2026-07-01T00:00:00Z',
        }
        _mark_session_clean_exit(source='unit-test')
        assert 'last_unclean_shutdown_utc' not in fake_settings['data']

    def test_entry_is_logged_even_when_the_write_fails(
        self, monkeypatch, fake_settings, shutdown_log
    ):
        """The whole point: distinguish 'never ran' from 'ran and lost'."""
        def _boom(_payload):
            raise OSError('disk full')
        monkeypatch.setattr(visualizer, 'save_persisted_settings', _boom)
        _mark_session_clean_exit(source='main:finally')
        lines = shutdown_log()
        assert any('clean_exit: entered' in ln for ln in lines)
        assert any('FAILED to mark clean exit' in ln for ln in lines)


class TestSessionStartLogging:
    def test_logs_classification_inputs_and_flags_unclean(self, fake_settings, shutdown_log):
        fake_settings['data'] = {
            EXIT_REASON_KEY: 'unknown',
            'app_session_started_utc': '2026-07-01T00:00:00Z',
            'app_session_pid': 4242,
        }
        visualizer._mark_session_start()
        lines = shutdown_log()
        assert any('classifying prior session' in ln for ln in lines)
        assert any('FLAGGING prior session unclean' in ln for ln in lines)
        assert any('prev_pid=4242' in ln for ln in lines)
        assert fake_settings['data']['last_unclean_shutdown_utc'] == '2026-07-01T00:00:00Z'

    def test_clean_prior_session_is_not_flagged(self, fake_settings, shutdown_log):
        fake_settings['data'] = {
            EXIT_REASON_KEY: 'clean',
            'app_session_started_utc': '2026-07-01T00:00:00Z',
        }
        visualizer._mark_session_start()
        lines = shutdown_log()
        assert any('prior session OK' in ln for ln in lines)
        assert 'last_unclean_shutdown_utc' not in fake_settings['data']

    def test_still_running_prior_session_is_not_flagged(self, fake_settings, shutdown_log):
        """The concurrent-instance case: the first window is still open, so
        its 'unknown' means "has not exited yet", not "crashed"."""
        fake_settings['data'] = {
            EXIT_REASON_KEY: 'unknown',
            'app_session_started_utc': _utc_now_iso(),
            'app_session_pid': os.getpid(),
        }
        visualizer._mark_session_start()
        lines = shutdown_log()
        assert any('prior session still running' in ln for ln in lines)
        assert not any('FLAGGING prior session unclean' in ln for ln in lines)
        assert 'last_unclean_shutdown_utc' not in fake_settings['data']

    def test_still_running_does_not_clear_an_earlier_pending_flag(self, fake_settings):
        """Suppressing this prompt must not swallow a real one already queued."""
        fake_settings['data'] = {
            EXIT_REASON_KEY: 'unknown',
            'app_session_started_utc': _utc_now_iso(),
            'app_session_pid': os.getpid(),
            'last_unclean_shutdown_utc': '2026-07-01T00:00:00Z',
        }
        visualizer._mark_session_start()
        assert fake_settings['data']['last_unclean_shutdown_utc'] == '2026-07-01T00:00:00Z'

    def test_dead_prior_session_is_still_flagged(self, monkeypatch, fake_settings, shutdown_log):
        """The guard is narrow: a prior session that really did die still
        raises the recovery prompt, however recent it was."""
        monkeypatch.setattr(visualizer, '_pid_is_alive', lambda _pid: False)
        started = _utc_now_iso()
        fake_settings['data'] = {
            EXIT_REASON_KEY: 'unknown',
            'app_session_started_utc': started,
            'app_session_pid': 4242,
        }
        visualizer._mark_session_start()
        assert any('FLAGGING prior session unclean' in ln for ln in shutdown_log())
        assert fake_settings['data']['last_unclean_shutdown_utc'] == started

    def test_opens_new_session_as_unknown(self, fake_settings, shutdown_log):
        fake_settings['data'] = {EXIT_REASON_KEY: 'clean'}
        visualizer._mark_session_start()
        assert fake_settings['data'][EXIT_REASON_KEY] == 'unknown'
        assert fake_settings['data']['app_session_pid'] == os.getpid()
        assert any('opening new session' in ln for ln in shutdown_log())


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
