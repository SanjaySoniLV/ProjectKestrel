"""Unit tests for log_redactor.py — best-effort username PII stripper."""

import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import log_redactor


pytestmark = pytest.mark.unit


@pytest.fixture
def fake_user(monkeypatch):
    """Pin the resolved username to 'Sanja' with a Windows-style home dir.

    Tests that need a different username override via ``set_user``.
    """
    state = {'home': r'C:\Users\Sanja'}

    def _expanduser(path):
        if path == '~' or path.startswith('~'):
            return state['home'] + path[1:]
        return path

    monkeypatch.setattr(log_redactor.os.path, 'expanduser', _expanduser)
    monkeypatch.setenv('USERNAME', 'Sanja')
    monkeypatch.setenv('USER', 'Sanja')
    log_redactor._cache_key = ('', '')  # invalidate cache
    log_redactor._cache_username_pattern = None
    log_redactor._cache_home_pattern = None
    yield state
    log_redactor._cache_key = ('', '')


def _set_user(state, home, username, monkeypatch):
    """Reconfigure the fake_user fixture mid-test."""
    state['home'] = home
    monkeypatch.setenv('USERNAME', username)
    monkeypatch.setenv('USER', username)
    log_redactor._cache_key = ('', '')
    log_redactor._cache_username_pattern = None
    log_redactor._cache_home_pattern = None


class TestRedactUserPaths:
    """Covers redact_user_paths()."""

    def test_windows_user_path_redacted(self, fake_user):
        result = log_redactor.redact_user_paths(
            r'C:\Users\Sanja\Pictures\IMG_3333.CR2'
        )
        assert result == r'C:\Users\[REDACTED]\Pictures\IMG_3333.CR2'

    def test_windows_case_insensitive_lower(self, fake_user):
        result = log_redactor.redact_user_paths(
            r'C:\users\sanja\foo\bar.txt'
        )
        assert result == r'C:\users\[REDACTED]\foo\bar.txt'

    def test_windows_case_insensitive_upper(self, fake_user):
        result = log_redactor.redact_user_paths(
            r'C:\USERS\SANJA\foo\bar.txt'
        )
        assert result == r'C:\USERS\[REDACTED]\foo\bar.txt'

    def test_macos_user_path_redacted(self, fake_user, monkeypatch):
        _set_user(fake_user, '/Users/sanja', 'sanja', monkeypatch)
        result = log_redactor.redact_user_paths('/Users/sanja/Photos/IMG.jpg')
        assert result == '/Users/[REDACTED]/Photos/IMG.jpg'

    def test_linux_home_path_redacted(self, fake_user, monkeypatch):
        _set_user(fake_user, '/home/sanja', 'sanja', monkeypatch)
        result = log_redactor.redact_user_paths('/home/sanja/photos/img.jpg')
        assert result == '/home/[REDACTED]/photos/img.jpg'

    def test_ml_library_traceback_kept_readable(self, fake_user):
        tb = (
            'Traceback (most recent call last):\n'
            '  File "C:\\Users\\Sanja\\AppData\\Local\\Programs\\Python\\'
            'Python311\\Lib\\site-packages\\tensorflow\\__init__.py", '
            'line 42, in <module>\n'
            '    from tensorflow.python import keras\n'
            'ImportError: bad magic number\n'
        )
        out = log_redactor.redact_user_paths(tb)
        # ML library context preserved:
        assert 'site-packages\\tensorflow\\__init__.py' in out
        assert 'line 42, in <module>' in out
        assert 'ImportError: bad magic number' in out
        # Username gone:
        assert 'Sanja' not in out
        assert '[REDACTED]' in out

    def test_path_not_under_home_unchanged(self, fake_user):
        samples = [
            r'C:\Data\Code\ProjectKestrel\analyzer\foo.py',
            r'C:\Windows\System32\drivers\etc\hosts',
            r'D:\backup\IMG.CR2',
            '/usr/local/lib/python3.11/site-packages/numpy/__init__.py',
            '/opt/homebrew/bin/python3',
        ]
        for s in samples:
            assert log_redactor.redact_user_paths(s) == s

    def test_username_with_regex_special_chars(self, fake_user, monkeypatch):
        _set_user(fake_user, r'C:\Users\a.b+c', 'a.b+c', monkeypatch)
        # Exact match redacted:
        assert log_redactor.redact_user_paths(r'C:\Users\a.b+c\foo') == \
            r'C:\Users\[REDACTED]\foo'
        # Regex special chars must not match as regex (a.b+c shouldn't
        # match 'axbxc' or 'abbbc'):
        assert log_redactor.redact_user_paths(r'C:\Users\axbxc\foo') == \
            r'C:\Users\axbxc\foo'

    def test_longer_username_not_partial_match(self, fake_user, monkeypatch):
        _set_user(fake_user, r'C:\Users\San', 'San', monkeypatch)
        # 'Sanjay' starts with 'San' — must NOT redact.
        result = log_redactor.redact_user_paths(r'C:\Users\Sanjay\foo.txt')
        assert result == r'C:\Users\Sanjay\foo.txt'
        # Exact match still redacts.
        assert log_redactor.redact_user_paths(r'C:\Users\San\foo.txt') == \
            r'C:\Users\[REDACTED]\foo.txt'

    def test_expanded_home_substring(self, fake_user, monkeypatch):
        # Mapped drive home that doesn't match \Users\ or /home/ patterns.
        _set_user(fake_user, r'Z:\profiles\sanja', 'sanja', monkeypatch)
        result = log_redactor.redact_user_paths(
            r'opened Z:\profiles\sanja\settings.json successfully'
        )
        assert result == r'opened [REDACTED]\settings.json successfully'

    def test_multiple_paths_one_string(self, fake_user):
        text = (
            r'A: C:\Users\Sanja\a.txt, '
            r'B: C:\Users\Sanja\b.txt, '
            r'C: C:\Users\Sanja\c.txt'
        )
        out = log_redactor.redact_user_paths(text)
        assert out.count('[REDACTED]') == 3
        assert 'Sanja' not in out

    def test_path_at_end_of_string(self, fake_user):
        out = log_redactor.redact_user_paths(r'home is C:\Users\Sanja')
        assert out == r'home is C:\Users\[REDACTED]'

    def test_path_followed_by_quote(self, fake_user):
        out = log_redactor.redact_user_paths(
            'File "C:\\Users\\Sanja\\x.py", line 1'
        )
        assert 'Sanja' not in out
        assert '[REDACTED]' in out
        assert 'x.py' in out

    def test_empty_string(self, fake_user):
        assert log_redactor.redact_user_paths('') == ''

    def test_non_string_input(self, fake_user):
        # Defensive: pass-through for unexpected types.
        assert log_redactor.redact_user_paths(None) is None  # type: ignore[arg-type]
        assert log_redactor.redact_user_paths(42) == 42  # type: ignore[arg-type]

    def test_idempotent(self, fake_user):
        text = r'C:\Users\Sanja\Pictures\IMG.CR2 and /home/sanja/photo.jpg'
        once = log_redactor.redact_user_paths(text)
        twice = log_redactor.redact_user_paths(once)
        assert once == twice

    def test_failsafe_on_unresolvable_username(self, monkeypatch):
        # No home, no env vars — redactor must no-op without raising.
        monkeypatch.setattr(
            log_redactor.os.path, 'expanduser', lambda p: '~'
        )
        monkeypatch.delenv('USERNAME', raising=False)
        monkeypatch.delenv('USER', raising=False)
        monkeypatch.delenv('LOGNAME', raising=False)
        log_redactor._cache_key = ('', '')
        log_redactor._cache_pattern = None
        log_redactor._cache_home = ''
        sample = r'C:\Users\SomeoneElse\file.txt'
        assert log_redactor.redact_user_paths(sample) == sample


class TestRedactUserPathsInObj:
    """Covers redact_user_paths_in_obj() — recursive walker."""

    def test_walks_dict(self, fake_user):
        obj = {
            'a': r'C:\Users\Sanja\x',
            'b': 'no path here',
            'c': 42,
            'd': True,
            'e': None,
        }
        out = log_redactor.redact_user_paths_in_obj(obj)
        assert out['a'] == r'C:\Users\[REDACTED]\x'
        assert out['b'] == 'no path here'
        assert out['c'] == 42
        assert out['d'] is True
        assert out['e'] is None

    def test_walks_nested(self, fake_user, monkeypatch):
        # Use a posix-style home for variety.
        _set_user(fake_user, '/home/sanja', 'sanja', monkeypatch)
        obj = {
            'outer': [
                {'inner': '/home/sanja/photo.jpg'},
                {'inner': 'plain'},
            ],
            'meta': {'path': '/home/sanja/data'},
        }
        out = log_redactor.redact_user_paths_in_obj(obj)
        assert out['outer'][0]['inner'] == '/home/[REDACTED]/photo.jpg'
        assert out['outer'][1]['inner'] == 'plain'
        assert out['meta']['path'] == '/home/[REDACTED]/data'

    def test_preserves_tuple_type(self, fake_user):
        obj = (r'C:\Users\Sanja\x', 'plain', 42)
        out = log_redactor.redact_user_paths_in_obj(obj)
        assert isinstance(out, tuple)
        assert out == (r'C:\Users\[REDACTED]\x', 'plain', 42)

    def test_does_not_mutate_input(self, fake_user):
        obj = {'a': r'C:\Users\Sanja\x', 'b': [r'C:\Users\Sanja\y']}
        snapshot = {'a': r'C:\Users\Sanja\x', 'b': [r'C:\Users\Sanja\y']}
        _ = log_redactor.redact_user_paths_in_obj(obj)
        assert obj == snapshot

    def test_none_input(self, fake_user):
        assert log_redactor.redact_user_paths_in_obj(None) is None

    def test_string_input(self, fake_user):
        assert log_redactor.redact_user_paths_in_obj(
            r'C:\Users\Sanja\x'
        ) == r'C:\Users\[REDACTED]\x'
