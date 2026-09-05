"""auth.json fallback must never be world-readable, even briefly.

``_keyring_save`` used ``open(path, 'w')`` then ``chmod(0o600)``. Between
those calls the JWT sat at the umask default (typically 0644). The dest
must be created owner-only and must not be truncated in place.
"""

from __future__ import annotations

import builtins
import json
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import api_bridge
import kestrel_analyzer.database as _dbmod


pytestmark = pytest.mark.unit

_BUNDLE = {"access_token": "test-jwt", "refresh_token": "test-refresh"}


def _force_file_fallback(monkeypatch: pytest.MonkeyPatch, dest: Path) -> None:
    monkeypatch.setattr(api_bridge, "_get_auth_fallback_path", lambda: str(dest))
    fake = types.ModuleType("keyring")

    def _too_large(*_a, **_k):
        raise OSError("credential too large")

    fake.set_password = _too_large
    fake.delete_password = lambda *_a, **_k: None
    monkeypatch.setitem(sys.modules, "keyring", fake)


def _auth_path(tmp_path: Path) -> Path:
    return tmp_path / "kestrel" / "auth.json"


def _tmp_leftovers(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if p.name.startswith(".kestrel_atomic_")]


class TestAuthJsonFallbackPerms:
    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_new_auth_json_is_owner_only(self, tmp_path, monkeypatch):
        dest = _auth_path(tmp_path)
        _force_file_fallback(monkeypatch, dest)
        old_umask = os.umask(0o022)
        try:
            api_bridge._keyring_save(_BUNDLE)
        finally:
            os.umask(old_umask)

        assert json.loads(dest.read_text(encoding="utf-8")) == _BUNDLE
        assert dest.stat().st_mode & 0o777 == 0o600
        assert dest.parent.stat().st_mode & 0o777 == 0o700

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
    def test_existing_world_readable_auth_json_is_tightened(self, tmp_path, monkeypatch):
        dest = _auth_path(tmp_path)
        dest.parent.mkdir()
        dest.write_text("{}", encoding="utf-8")
        os.chmod(dest, 0o644)
        _force_file_fallback(monkeypatch, dest)

        api_bridge._keyring_save(_BUNDLE)

        assert json.loads(dest.read_text(encoding="utf-8")) == _BUNDLE
        assert dest.stat().st_mode & 0o777 == 0o600

    def test_dest_is_never_opened_for_write(self, tmp_path, monkeypatch):
        dest = _auth_path(tmp_path)
        dest_abs = dest.resolve()
        _force_file_fallback(monkeypatch, dest)
        write_opens: list[str] = []
        real_open = builtins.open

        def spy_open(file, mode="r", *a, **k):
            try:
                opened = Path(file).resolve()
            except (TypeError, OSError, ValueError):
                opened = None
            if opened == dest_abs and any(c in str(mode) for c in "wxa+"):
                write_opens.append(str(mode))
            return real_open(file, mode, *a, **k)

        monkeypatch.setattr(builtins, "open", spy_open)
        api_bridge._keyring_save(_BUNDLE)

        assert json.loads(dest.read_text(encoding="utf-8")) == _BUNDLE
        assert write_opens == [], (
            "auth.json was opened for write in place; that is the 0644 window"
        )

    def test_os_open_on_dest_uses_owner_only_mode(self, tmp_path, monkeypatch):
        dest = _auth_path(tmp_path)
        dest_abs = dest.resolve()
        _force_file_fallback(monkeypatch, dest)
        modes: list[int] = []
        real_os_open = os.open

        def spy_os_open(path, flags, mode=0o777, *a, **k):
            try:
                opened = Path(path).resolve()
            except (TypeError, OSError, ValueError):
                opened = None
            if opened == dest_abs and flags & os.O_CREAT:
                modes.append(mode & 0o777)
                assert (mode & 0o777) == 0o600
            return real_os_open(path, flags, mode, *a, **k)

        monkeypatch.setattr(os, "open", spy_os_open)
        monkeypatch.setattr(api_bridge.os, "open", spy_os_open)
        monkeypatch.setattr(_dbmod.os, "open", spy_os_open)
        api_bridge._keyring_save(_BUNDLE)

        assert dest.exists()
        assert all(m == 0o600 for m in modes)

    def test_failed_replace_leaves_existing_auth_json(self, tmp_path, monkeypatch):
        dest = _auth_path(tmp_path)
        dest.parent.mkdir()
        original = b'{"access_token":"old-jwt"}'
        dest.write_bytes(original)
        _force_file_fallback(monkeypatch, dest)

        def boom(*_a, **_k):
            raise OSError("simulated crash during replace")

        monkeypatch.setattr(_dbmod.os, "replace", boom)
        with pytest.raises(OSError):
            api_bridge._keyring_save(_BUNDLE)

        assert dest.read_bytes() == original
        assert _tmp_leftovers(dest.parent) == []

    def test_chmod_failure_does_not_fail_the_save(self, tmp_path, monkeypatch):
        dest = _auth_path(tmp_path)
        _force_file_fallback(monkeypatch, dest)
        real_chmod = os.chmod

        def wrapping_chmod(path, mode, *a, **k):
            if Path(path).name == "auth.json":
                raise OSError("chmod unsupported on this filesystem")
            return real_chmod(path, mode, *a, **k)

        monkeypatch.setattr(os, "chmod", wrapping_chmod)
        monkeypatch.setattr(api_bridge.os, "chmod", wrapping_chmod)
        api_bridge._keyring_save(_BUNDLE)

        assert json.loads(dest.read_text(encoding="utf-8")) == _BUNDLE
        assert _tmp_leftovers(dest.parent) == []
