"""Regression tests for ON1 Photo RAW discovery on Windows.

ON1 installs each yearly release as ``ON1\\ON1 Photo RAW <year>\\ON1 Photo RAW
<year>.exe``. The registry used to look for ``ON1 Photo RAW.exe`` inside the
versioned folder, so choosing ON1 always logged "on1 not found on Windows,
falling back to system default".
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import editor_launch


pytestmark = pytest.mark.unit


def _make_on1(root: Path, folder: str, exe: str) -> Path:
    d = root / "ON1" / folder
    d.mkdir(parents=True, exist_ok=True)
    p = d / exe
    p.write_bytes(b"")
    return p


@pytest.fixture
def program_files(monkeypatch, tmp_path):
    pf = tmp_path / "Program Files"
    pf.mkdir()
    monkeypatch.setenv("ProgramFiles", str(pf))
    monkeypatch.delenv("ProgramW6432", raising=False)
    monkeypatch.delenv("ProgramFiles(x86)", raising=False)
    return pf


def test_find_on1_prefers_newest_versioned_exe(program_files):
    _make_on1(program_files, "ON1 Photo RAW 2024", "ON1 Photo RAW 2024.exe")
    newest = _make_on1(program_files, "ON1 Photo RAW 2026", "ON1 Photo RAW 2026.exe")
    _make_on1(program_files, "ON1 Photo RAW 2026", "unins000.exe")

    found = editor_launch._find_on1_exes()

    assert found[0] == str(newest)
    assert not any("unins" in f.lower() for f in found)
    # Unversioned layout stays as the last-resort candidate.
    assert found[-1].endswith(str(Path("ON1") / "ON1 Photo RAW" / "ON1 Photo RAW.exe"))


def test_launch_on1_uses_versioned_exe_on_windows(monkeypatch, program_files, tmp_path):
    exe = _make_on1(program_files, "ON1 Photo RAW 2025", "ON1 Photo RAW 2025.exe")
    target = tmp_path / "IMG_0001.CR3"
    target.write_bytes(b"")

    launched: list[list[str]] = []
    started: list[str] = []
    monkeypatch.setattr(editor_launch, "load_persisted_settings", lambda: {})
    monkeypatch.setattr(editor_launch.sys, "platform", "win32")
    monkeypatch.setattr(
        editor_launch.subprocess, "Popen",
        lambda argv, *a, **kw: launched.append(list(argv)) or None,
    )
    monkeypatch.setattr(editor_launch.os, "startfile", lambda p: started.append(p), raising=False)

    editor_launch.launch(str(target), "on1")

    assert launched == [[str(exe), str(target)]]
    assert started == [], "fell back to the system default instead of ON1"
