"""Unit tests for session-exit classification.

Covers the pure helper ``_classify_prior_session`` and the legacy-settings
migration path. These tests avoid any pywebview / PyQt import side effects
by skipping cleanly if the visualizer module cannot be imported in the test
environment (e.g. CI without GUI dependencies).
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    from visualizer import (
        EXIT_REASON_KEY,
        _classify_prior_session,
        _should_flag_unclean,
    )
except Exception as e:  # pragma: no cover - environment-dependent
    pytest.skip(f'visualizer module not importable in this env: {e}', allow_module_level=True)


pytestmark = pytest.mark.unit


class TestClassifyPriorSession:
    """``_classify_prior_session`` reads a settings dict and returns one of
    the four canonical exit reasons. It must be pure (no I/O) and must not
    mutate its input."""

    def test_clean_passthrough(self):
        assert _classify_prior_session({EXIT_REASON_KEY: 'clean'}) == 'clean'

    def test_os_shutdown_passthrough(self):
        assert _classify_prior_session({EXIT_REASON_KEY: 'os_shutdown'}) == 'os_shutdown'

    def test_crash_passthrough(self):
        assert _classify_prior_session({EXIT_REASON_KEY: 'crash'}) == 'crash'

    def test_unknown_passthrough(self):
        assert _classify_prior_session({EXIT_REASON_KEY: 'unknown'}) == 'unknown'

    def test_case_insensitive(self):
        assert _classify_prior_session({EXIT_REASON_KEY: 'CRASH'}) == 'crash'
        assert _classify_prior_session({EXIT_REASON_KEY: '  Clean  '}) == 'clean'

    def test_garbage_value_falls_back_to_legacy_path(self):
        # Bogus exit_reason → fall through to legacy migration. With no
        # legacy markers either, classify as 'clean'.
        assert _classify_prior_session({EXIT_REASON_KEY: 'lol'}) == 'clean'

    def test_does_not_mutate_input(self):
        settings = {EXIT_REASON_KEY: 'crash', 'other_key': 42}
        snapshot = dict(settings)
        _classify_prior_session(settings)
        assert settings == snapshot


class TestLegacyMigration:
    """Pre-upgrade installs lack ``app_session_exit_reason`` and only have
    the old ``app_session_closed_cleanly`` boolean. Make sure we map those
    sensibly so a single dirty legacy session doesn't trigger a crash
    dialog every time the user upgrades."""

    def test_legacy_clean_install(self):
        # Old install that had a clean prior exit → new schema sees 'clean'.
        settings = {'app_session_closed_cleanly': True}
        assert _classify_prior_session(settings) == 'clean'

    def test_legacy_no_session_at_all(self):
        # First-ever launch: no settings at all. Classify as clean (no dialog).
        assert _classify_prior_session({}) == 'clean'

    def test_legacy_dirty_session(self):
        # Upgrade with a dirty legacy flag from before exit_reason existed.
        # Must be 'unknown' (soft prompt), not 'crash' (alarming).
        settings = {
            'app_session_closed_cleanly': False,
            'app_session_started_utc': '2026-05-09T12:00:00Z',
        }
        assert _classify_prior_session(settings) == 'unknown'

    def test_legacy_dirty_flag_without_started_utc(self):
        # Defensive: dirty flag but no start time means we have no evidence
        # of an actual prior session — don't pester the user.
        settings = {'app_session_closed_cleanly': False}
        assert _classify_prior_session(settings) == 'clean'

    def test_new_schema_overrides_legacy_flag(self):
        # If both old and new keys are present, the new key wins.
        settings = {
            EXIT_REASON_KEY: 'os_shutdown',
            'app_session_closed_cleanly': False,
            'app_session_started_utc': '2026-05-09T12:00:00Z',
        }
        assert _classify_prior_session(settings) == 'os_shutdown'


class TestShouldFlagUnclean:
    """``_should_flag_unclean`` turns a classified prior session plus the
    liveness of its recorded pid into the "show the recovery dialog"
    decision. Pure function; ``prev_pid_alive`` is ``None`` when liveness
    could not be determined."""

    STARTED = '2026-05-09T12:00:00Z'
    OURS = 4242
    THEIRS = 1717

    def test_clean_never_flags(self):
        assert not _should_flag_unclean('clean', self.STARTED, self.THEIRS, False, self.OURS)

    def test_os_shutdown_never_flags(self):
        # Reboot / logoff is a normal exit, not a crash.
        assert not _should_flag_unclean('os_shutdown', self.STARTED, self.THEIRS, False, self.OURS)

    def test_no_start_time_never_flags(self):
        # No evidence a prior session ever ran.
        assert not _should_flag_unclean('unknown', '', self.THEIRS, False, self.OURS)

    def test_unknown_with_dead_pid_flags(self):
        # The real unclean shutdown: process is gone and never recorded why.
        assert _should_flag_unclean('unknown', self.STARTED, self.THEIRS, False, self.OURS)

    def test_unknown_with_undeterminable_pid_flags(self):
        # Liveness unknown → fall back to the previous behaviour and prompt.
        assert _should_flag_unclean('unknown', self.STARTED, self.THEIRS, None, self.OURS)

    def test_unknown_with_no_recorded_pid_flags(self):
        # Installs upgrading from a build that never wrote app_session_pid.
        assert _should_flag_unclean('unknown', self.STARTED, 0, None, self.OURS)

    def test_unknown_with_live_other_pid_does_not_flag(self):
        # The false positive this guard exists for: a second instance reading
        # the still-running first instance's in-flight 'unknown' marker.
        assert not _should_flag_unclean('unknown', self.STARTED, self.THEIRS, True, self.OURS)

    def test_live_pid_equal_to_ours_still_flags(self):
        # A recycled pid that happens to equal our own is not evidence of a
        # concurrent instance — our own process cannot be the prior session.
        assert _should_flag_unclean('unknown', self.STARTED, self.OURS, True, self.OURS)

    def test_crash_flags_even_with_live_pid(self):
        # A recorded 'crash' means the top-level handler actually caught an
        # exception. Surface it regardless of what else is running.
        assert _should_flag_unclean('crash', self.STARTED, self.THEIRS, True, self.OURS)

    def test_crash_with_dead_pid_flags(self):
        assert _should_flag_unclean('crash', self.STARTED, self.THEIRS, False, self.OURS)
