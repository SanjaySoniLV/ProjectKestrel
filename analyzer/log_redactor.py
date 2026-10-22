"""
Project Kestrel — Log Redactor

Best-effort redaction of the local OS username from log/traceback strings
before they leave the device (feedback + crash report payloads).

DESIGN
------
We replace the *username segment* of recognised home-directory path prefixes
with ``[REDACTED]``:

    C:\\Users\\Sanja\\Pictures\\IMG.CR2   ->  C:\\Users\\[REDACTED]\\Pictures\\IMG.CR2
    /Users/sanja/Photos/IMG.jpg          ->  /Users/[REDACTED]/Photos/IMG.jpg
    /home/sanja/photos/img.jpg           ->  /home/[REDACTED]/photos/img.jpg

The rest of the path is preserved so that ML stack frames (which typically
live under home, e.g. ``...\\AppData\\...\\site-packages\\tensorflow\\__init__.py``)
remain readable in crash reports.

We also do a literal-substring sweep for the result of
``os.path.expanduser('~')`` to cover unusual home directories (mapped drives,
custom ``$HOME``) that don't match the ``\\Users\\`` / ``/home/`` patterns.

Every public function is failsafe — it never raises, and returns the input
unchanged if the username can't be resolved.
"""

import os
import re
from typing import Any, Optional, Tuple

_REDACTED = '[REDACTED]'


def _resolve_username() -> str:
    """Return the current OS username, or '' if it can't be determined."""
    try:
        home = os.path.expanduser('~')
        if home and home != '~':
            # Split on BOTH separators rather than os.path.basename: the home may
            # be a Windows-style path even when running on POSIX (or vice-versa),
            # and basename only understands the host OS's separator -- so
            # basename(r'C:\Users\x') wrongly returns the whole string on
            # macOS/Linux instead of 'x'.
            name = re.split(r'[\\/]', home.rstrip('\\/'))[-1]
            if name:
                return name
    except Exception:
        pass
    for env_var in ('USERNAME', 'USER', 'LOGNAME'):
        try:
            name = os.environ.get(env_var, '').strip()
            if name:
                return name
        except Exception:
            continue
    return ''


def _resolve_home() -> str:
    """Return the absolute home directory, or '' if it can't be resolved."""
    try:
        home = os.path.expanduser('~')
        if home and home != '~':
            return home
    except Exception:
        pass
    return ''


_cache_key: Tuple[str, str] = ('', '')
_cache_username_pattern: Optional[re.Pattern] = None
_cache_home_pattern: Optional[re.Pattern] = None

# Lookahead prevents partial matches against longer usernames or sibling
# directories (e.g. user 'San' must NOT redact '\Users\Sanjay\...', and home
# 'Z:\profiles\sanja' must NOT redact 'Z:\profiles\sanja_backup'). End-of-
# string and common delimiters are accepted terminators.
_BOUNDARY = r"(?=[\\/]|$|[\"'\s:;,)\]])"


def _build_username_pattern(username: str) -> Optional[re.Pattern]:
    if not username:
        return None
    u = re.escape(username)
    parts = [
        r"(?i)\\Users\\" + u + _BOUNDARY,    # Windows
        r"/Users/" + u + _BOUNDARY,            # macOS
        r"/home/" + u + _BOUNDARY,             # Linux
    ]
    return re.compile('|'.join(parts))


def _is_standard_home(home: str) -> bool:
    """True if ``home`` already matches one of the patterns the username
    regex covers (so a separate home pattern is redundant)."""
    if not home:
        return False
    h = home.replace('/', '\\')
    if re.match(r"(?i)^[A-Za-z]:\\Users\\[^\\]+$", h):
        return True
    if home.startswith('/Users/') and home.count('/') == 2:
        return True
    if home.startswith('/home/') and home.count('/') == 2:
        return True
    return False


def _build_home_pattern(home: str) -> Optional[re.Pattern]:
    """Compile a boundary-aware pattern for an exotic home directory
    (mapped drives, custom $HOME). Returns None for standard shapes."""
    if not home or _is_standard_home(home):
        return None
    return re.compile(re.escape(home) + _BOUNDARY)


def _get_compiled() -> Tuple[Optional[re.Pattern], Optional[re.Pattern]]:
    """Return (username-pattern, home-pattern), each possibly None.

    Cached across calls; rebuilt if the resolved username or home changes
    (e.g. tests that monkeypatch env vars or expanduser).
    """
    global _cache_key, _cache_username_pattern, _cache_home_pattern
    try:
        username = _resolve_username()
        home = _resolve_home()
    except Exception:
        return None, None
    key = (username, home)
    if key != _cache_key:
        _cache_key = key
        _cache_username_pattern = _build_username_pattern(username)
        _cache_home_pattern = _build_home_pattern(home)
    return _cache_username_pattern, _cache_home_pattern


def _redact_username_match(m: re.Match) -> str:
    """Substitution callback: keep '<sep>...<sep>' prefix, drop the username."""
    matched = m.group(0)
    for sep in ('\\', '/'):
        idx = matched.rfind(sep)
        if idx >= 0:
            return matched[: idx + 1] + _REDACTED
    return _REDACTED


def redact_user_paths(text: str) -> str:
    """Replace home-directory username occurrences with ``[REDACTED]``.

    Failsafe — returns the input unchanged on any error.
    """
    if not isinstance(text, str) or not text:
        return text
    try:
        username_pat, home_pat = _get_compiled()
        out = text
        if username_pat is not None:
            out = username_pat.sub(_redact_username_match, out)
        if home_pat is not None:
            out = home_pat.sub(_REDACTED, out)
        return out
    except Exception:
        return text


def redact_user_paths_in_obj(obj: Any) -> Any:
    """Recursively walk dict/list/tuple structures and redact string values.

    Returns a new structure; does not mutate the input. Non-string scalars
    (int, float, bool, None) pass through unchanged. Failsafe — returns the
    input on any error.
    """
    try:
        if isinstance(obj, str):
            return redact_user_paths(obj)
        if isinstance(obj, dict):
            return {k: redact_user_paths_in_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [redact_user_paths_in_obj(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(redact_user_paths_in_obj(v) for v in obj)
        return obj
    except Exception:
        return obj
