"""Unit tests for kestrel_telemetry.py — log payload redaction integration."""

import json
import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import kestrel_telemetry
import log_redactor


pytestmark = pytest.mark.unit


@pytest.fixture
def pinned_user(monkeypatch):
    """Pin the resolved username to 'Sanja' with a Windows-style home dir."""
    state = {'home': r'C:\Users\Sanja'}

    def _expanduser(path):
        if path == '~' or path.startswith('~'):
            return state['home'] + path[1:]
        return path

    monkeypatch.setattr(log_redactor.os.path, 'expanduser', _expanduser)
    monkeypatch.setattr(
        kestrel_telemetry.os.path, 'expanduser', _expanduser
    )
    monkeypatch.setenv('USERNAME', 'Sanja')
    monkeypatch.setenv('USER', 'Sanja')
    log_redactor._cache_key = ('', '')
    log_redactor._cache_pattern = None
    log_redactor._cache_home = ''
    yield state
    log_redactor._cache_key = ('', '')


class TestGetRecentLogTailRedaction:
    """Integration: planted log files should come back through
    get_recent_log_tail() with username PII stripped."""

    def test_redacts_username_in_payload(self, pinned_user, tmp_path):
        kestrel_dir = tmp_path / '.kestrel'
        kestrel_dir.mkdir()

        # Planted structured analysis log.
        analysis_log = kestrel_dir / 'kestrel_error_20260516T000000Z.json'
        entries = [
            {
                'timestamp_utc': '2026-05-16T00:00:00Z',
                'level': 'error',
                'stage': 'pipeline',
                'context': {'folder': r'C:\Users\Sanja\Pictures\Wildlife'},
                'exception_type': 'FileNotFoundError',
                'exception_message': (
                    r"[Errno 2] No such file or directory: "
                    r"'C:\Users\Sanja\Pictures\IMG_3333.CR2'"
                ),
                'traceback': (
                    'Traceback (most recent call last):\n'
                    '  File "C:\\Users\\Sanja\\AppData\\Local\\Programs\\'
                    'Python\\Python311\\Lib\\site-packages\\tensorflow\\'
                    '__init__.py", line 42, in <module>\n'
                ),
            }
        ]
        analysis_log.write_text(json.dumps(entries, indent=2), encoding='utf-8')

        # Planted runtime stdout/stderr tail.
        logs_dir = kestrel_dir / 'logs'
        logs_dir.mkdir()
        runtime_log = logs_dir / 'kestrel_runtime_20260516T000000Z.log'
        runtime_log.write_text(
            'starting analysis on C:\\Users\\Sanja\\Pictures\\Wildlife\n'
            'wrote output to C:\\Users\\Sanja\\Pictures\\Wildlife\\.kestrel\n'
            'all done\n',
            encoding='utf-8',
        )

        result = kestrel_telemetry.get_recent_log_tail(
            folder=str(tmp_path), runtime_log_files=1
        )
        assert result, 'expected a non-empty JSON payload'

        # The serialised payload must not leak the username anywhere.
        assert 'Sanja' not in result
        assert '[REDACTED]' in result

        payload = json.loads(result)

        # Structured entry: ML traceback context preserved, username gone.
        entry = payload['analysis_entries'][0]
        assert 'site-packages\\tensorflow\\__init__.py' in entry['traceback']
        assert 'FileNotFoundError' == entry['exception_type']
        assert 'Sanja' not in entry['exception_message']
        assert '[REDACTED]' in entry['exception_message']
        assert 'Sanja' not in entry['context']['folder']
        # Non-string fields untouched.
        assert entry['level'] == 'error'
        assert entry['stage'] == 'pipeline'

        # Runtime tail: username gone, structural content kept.
        runtime_tails = payload['runtime_output_tails']
        assert len(runtime_tails) == 1
        tail_text = runtime_tails[0]['tail']
        assert 'Sanja' not in tail_text
        assert 'starting analysis on' in tail_text
        assert 'all done' in tail_text
        # Filename preserved in the runtime entry.
        assert runtime_tails[0]['file'] == 'kestrel_runtime_20260516T000000Z.log'
