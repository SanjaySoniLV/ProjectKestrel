"""Distribution channel of this build.

    'direct'   -> website download: notarized Developer ID DMG (macOS) or the
                  Windows installer / Microsoft Store bundle.
    'appstore' -> the sandboxed Mac App Store build.

The value is baked into the bundle at build time as ``dist_channel.txt`` (each
PyInstaller spec writes the channel it targets). At runtime we read that file
from the frozen bundle. For source / dev runs (no baked file) we fall back to
the ``KESTREL_DIST_CHANNEL`` environment variable, then default to ``'direct'``.

Kept dependency-free and import-safe so every entry point (Windows, DMG, App
Store, CLI, tests) can import it unconditionally.

This is deliberately INDEPENDENT of sandbox detection
(``mac_sandbox.is_sandboxed``). Channel describes how a build is *distributed*,
not whether it is sandboxed: the direct DMG could adopt the sandbox later while
remaining the 'direct' channel, and Windows has a channel but no sandbox at all.
Use ``mac_sandbox.is_sandboxed()`` for "am I inside the macOS sandbox right now"
and this module for "which store/download did this build ship through".
"""

from __future__ import annotations

import os
import sys

_VALID = ("direct", "appstore")
_DEFAULT = "direct"
_ENV_VAR = "KESTREL_DIST_CHANNEL"
_BAKED_FILENAME = "dist_channel.txt"

_cached: str | None = None


def _candidate_paths() -> list[str]:
    """Locations the baked ``dist_channel.txt`` may live, most-specific first."""
    paths: list[str] = []
    # Frozen bundle: PyInstaller extracts bundled datas under _MEIPASS
    # (onedir: the app's _internal directory).
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        paths.append(os.path.join(meipass, _BAKED_FILENAME))
    # Fallback: alongside the executable (onedir root).
    if getattr(sys, "frozen", False):
        paths.append(os.path.join(os.path.dirname(sys.executable), _BAKED_FILENAME))
    # Source / dev: next to this module.
    paths.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), _BAKED_FILENAME))
    return paths


def _read_baked() -> str | None:
    for p in _candidate_paths():
        try:
            with open(p, "r", encoding="utf-8") as f:
                v = f.read().strip().lower()
        except OSError:
            continue
        if v in _VALID:
            return v
    return None


def get_channel() -> str:
    """Return the distribution channel: ``'direct'`` or ``'appstore'``.

    Result is cached after the first call. Baked file wins over the env var,
    which wins over the ``'direct'`` default.
    """
    global _cached
    if _cached is not None:
        return _cached
    v = _read_baked()
    if v is None:
        env = os.environ.get(_ENV_VAR, "").strip().lower()
        v = env if env in _VALID else _DEFAULT
    _cached = v
    return v


def is_appstore() -> bool:
    return get_channel() == "appstore"


def is_direct() -> bool:
    return get_channel() == "direct"
