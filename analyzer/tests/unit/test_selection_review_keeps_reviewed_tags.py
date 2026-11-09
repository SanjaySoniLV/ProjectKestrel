"""The selection bar's "Mark as Reviewed" leaves already-reviewed scenes alone.

``_finalizeSceneReview`` rebuilds a scene's tags from the model's visible tags,
so running it on a scene the user had already reviewed (possibly after
correcting its species) replaced their tags. ``executeSelectionReview`` now
skips finalized scenes. Behaviour is checked by running the real function from
``scene-grid.js`` in a Node ``vm`` with stubbed globals (skipped without Node).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ANALYZER = Path(__file__).resolve().parents[2]
SCENE_GRID_JS = (ANALYZER / "js" / "scene-grid.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.unit

NODE = shutil.which("node")


def _body(src, start, end):
    s = src.index(start)
    return src[s:src.index(end, s)]


SRC = _body(SCENE_GRID_JS, "function executeSelectionReview()", "/** Selection bar: reject the undecided")

_HARNESS = r"""
const vm = require('vm');
const [src, payload] = process.argv.slice(1);
const { selected, finalized } = JSON.parse(payload);
const finalizedCalls = [], status = [];
const ctx = vm.createContext({
  selectedSceneIds: new Set(selected),
  _getSceneScenedataEntry: (id) => ({ user_tags: { finalized: finalized.includes(id) } }),
  _finalizeSceneReview: (id) => { finalizedCalls.push(id); return true; },
  setStatus: (m) => status.push(m),
  renderScenes: () => {},
});
vm.runInContext(src + '\nexecuteSelectionReview();', ctx);
process.stdout.write(JSON.stringify({ finalizedCalls, status }));
"""


def _run(selected, finalized):
    out = subprocess.run([NODE, "-e", _HARNESS, SRC, json.dumps({"selected": selected, "finalized": finalized})],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_already_reviewed_scenes_are_not_re_finalized():
    res = _run(["3", "5", "8"], finalized=["5"])
    assert res["finalizedCalls"] == ["3", "8"]
    assert res["status"] == ["Marked 2 scenes as reviewed."]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_all_reviewed_selection_changes_nothing():
    res = _run(["5"], finalized=["5"])
    assert res["finalizedCalls"] == []
    assert res["status"] == []
