"""Time clustering for the scene grid timeline (js/scene-grid-parts.js), run through Node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ANALYZER = Path(__file__).resolve().parents[2]
PARTS = ANALYZER / "js" / "scene-grid-parts.js"

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("node") is None, reason="node not installed"),
]

MIN = 60 * 1000


def _run(expr: str):
    script = f"""
const vm = require('vm');
const ctx = {{ window: {{}} }};
vm.createContext(ctx);
vm.runInContext(require('fs').readFileSync({json.dumps(str(PARTS))}, 'utf8'), ctx);
const P = ctx.window.SceneGridParts;
process.stdout.write(JSON.stringify((function () {{ return {expr}; }})()));
"""
    res = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(res.stdout)


def _scenes(times):
    return json.dumps([{"id": i, "captureTimeMs": t} for i, t in enumerate(times)])


def test_gap_uses_fallback_with_too_few_timed_scenes():
    assert _run(f"P.computeDynamicClusterGapMs({_scenes([0, MIN])})") == 3 * MIN


def test_gap_is_ten_times_the_median_within_floor_and_ceiling():
    # 20 s median gap -> 200 s threshold.
    assert _run(f"P.computeDynamicClusterGapMs({_scenes([0, 20000, 40000, 60000])})") == 200000
    # 1 s bursts clamp to the 45 s floor; hour gaps clamp to the 10 min ceiling.
    assert _run(f"P.computeDynamicClusterGapMs({_scenes([0, 1000, 2000, 3000])})") == 45000
    assert _run(f"P.computeDynamicClusterGapMs({_scenes([0, 60 * MIN, 120 * MIN])})") == 10 * MIN


def test_clusters_split_on_gaps_and_keep_untimed_scenes_last():
    scenes = json.dumps([
        {"id": "a", "captureTimeMs": 5 * MIN},
        {"id": "u"},
        {"id": "b", "captureTimeMs": 0},
        {"id": "c", "captureTimeMs": 30 * MIN},
    ])
    out = _run(f"P.buildTimeClusters({scenes}, 10 * 60 * 1000).map(c => "
               "({{ids: c.scenes.map(s => s.id), start: c.startMs, end: c.endMs, untimed: c.untimed}}))"
               .replace("{{", "{").replace("}}", "}"))
    assert out == [
        {"ids": ["b", "a"], "start": 0, "end": 5 * MIN, "untimed": False},
        {"ids": ["c"], "start": 30 * MIN, "end": 30 * MIN, "untimed": False},
        {"ids": ["u"], "start": None, "end": None, "untimed": True},
    ]
