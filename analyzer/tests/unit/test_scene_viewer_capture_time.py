"""Scene viewer shows each image's capture time.

The info bar under the preview and the filmstrip tooltips show the camera's
capture time. ``formatCaptureTime`` is run for real under node (skipped when
node is absent); the call sites are source lints, as in the other scene
viewer tests (there is no JS runner).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ANALYZER = Path(__file__).resolve().parents[2]
SCENE_DIALOG_JS = (ANALYZER / "js" / "scene-dialog.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.unit


def _helper_source() -> str:
    m = re.search(r"    function formatCaptureTime\(v\) \{.*?\n    \}\n", SCENE_DIALOG_JS, re.S)
    assert m, "formatCaptureTime not found in scene-dialog.js"
    return m.group(0)


CASES = {
    "2026-11-01T07:32:10": "2026-11-01 07:32:10",
    "2026-11-01 07:32:10": "2026-11-01 07:32:10",
    "2026-11-01T07:32:10.500000": "2026-11-01 07:32:10",
    "2026-11-01T23:59": "2026-11-01 23:59",
    "": "",
    "nan": "",
    "garbage": "",
}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_format_capture_time_keeps_camera_wall_clock():
    # Run in a timezone far from UTC: going through Date would shift the hour.
    script = _helper_source() + (
        "const cases = %s;\n"
        "const out = {};\n"
        "for (const k of Object.keys(cases)) out[k] = formatCaptureTime(k);\n"
        "out['<null>'] = formatCaptureTime(null);\n"
        "out['<undef>'] = formatCaptureTime(undefined);\n"
        "console.log(JSON.stringify(out));\n" % json.dumps(CASES)
    )
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True,
                         env={**os.environ, "TZ": "Pacific/Kiritimati"},
                         check=True)
    out = json.loads(res.stdout)
    for raw, want in CASES.items():
        assert out[raw] == want, raw
    assert out["<null>"] == "" and out["<undef>"] == ""


def test_info_bar_appends_capture_time_to_position():
    start = SCENE_DIALOG_JS.index("const metaTextEl = el('#sceneInfoMetaText');")
    body = SCENE_DIALOG_JS[start:start + 900]
    assert "formatCaptureTime(r.capture_time)" in body
    # Shown both with labels and with labels hidden, via `position`.
    assert body.count("position") >= 3


def test_both_filmstrip_tooltips_show_capture_time():
    assert SCENE_DIALOG_JS.count("`Captured: ${formatCaptureTime(r.capture_time)}`") == 2
