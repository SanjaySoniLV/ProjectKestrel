"""Integration test for the pywebview JS<->Python bridge via --api-probe.

Spawns ``analyzer/visualizer.py --api-probe`` as a subprocess, waits for it
to write a result JSON, and asserts that the bridge round-trip worked end-
to-end (JS reached Python via ``pywebviewready``-triggered API call).

Marked ``@pytest.mark.ui`` so it can be excluded from headless CI lanes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]
VISUALIZER_PATH = REPO_ROOT / "analyzer" / "visualizer.py"


pytestmark = pytest.mark.ui


def _wait_for_file(path: Path, proc: subprocess.Popen, deadline_s: float) -> bool:
    """Poll for ``path`` to appear OR for ``proc`` to exit, whichever happens first.

    Returns True if the file appeared, False on timeout.
    """
    deadline = time.monotonic() + float(deadline_s)
    while time.monotonic() < deadline:
        if path.exists():
            return True
        if proc.poll() is not None:
            # Subprocess exited; the file may have been written just before exit
            # — give the OS a moment to flush, then check once more.
            time.sleep(0.2)
            return path.exists()
        time.sleep(0.2)
    return False


def _kill_subprocess(proc: subprocess.Popen) -> None:
    """Escalating-kill helper: terminate -> kill -> taskkill /T (Windows)."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
    except Exception:
        pass
    try:
        proc.kill()
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            pass
    except Exception:
        pass
    if os.name == "nt":
        # WebView2 spawns helper processes; /T kills the whole tree.
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                check=False,
                timeout=10,
            )
        except Exception:
            pass


def test_api_probe_writes_result_json(tmp_path):
    """End-to-end: subprocess --api-probe -> JS -> Api.report_bridge_ready -> JSON."""
    probe_out = tmp_path / "probe.json"
    cmd = [
        sys.executable,
        str(VISUALIZER_PATH),
        "--api-probe",
        "--probe-output", str(probe_out),
        "--probe-timeout", "15",
        "--port", "8799",
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        appeared = _wait_for_file(probe_out, proc, deadline_s=30.0)
        stdout, stderr = b"", b""
        try:
            stdout, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        assert appeared, (
            f"probe output not written within 30s. rc={proc.returncode}\n"
            f"stdout:\n{stdout.decode(errors='replace')}\n"
            f"stderr:\n{stderr.decode(errors='replace')}"
        )
        payload = json.loads(probe_out.read_text(encoding="utf-8"))
        assert payload.get("ok") is True, f"probe reported failure: {payload!r}"

        # version should match what api_bridge.Api.get_app_version reads.
        sys.path.insert(0, str(REPO_ROOT / "analyzer"))
        from kestrel_analyzer.config import VERSION
        assert payload.get("version") == VERSION, (
            f"version mismatch: probe={payload.get('version')!r}, "
            f"config.VERSION={VERSION!r}"
        )

        # frozen is platform-dependent but must always be a bool.
        assert isinstance(payload.get("frozen"), bool), (
            f"'frozen' is {type(payload.get('frozen')).__name__}, expected bool"
        )

        # Subprocess should have exited cleanly with rc=0.
        assert proc.returncode == 0, (
            f"probe subprocess exited with rc={proc.returncode}\n"
            f"stderr:\n{stderr.decode(errors='replace')}"
        )
    finally:
        _kill_subprocess(proc)
