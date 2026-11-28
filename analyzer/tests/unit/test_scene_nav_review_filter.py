"""Scene-to-scene navigation in the scene viewer honours the grid's filters
(review state, and View > Only scenes with favorites).

With "Unreviewed only" selected the grid hides reviewed scenes, but Tab (and
arrowing past a scene's last image) used to step through the full scene list,
landing on scenes the grid had hidden. ``navigateToScene`` now skips scenes
outside the filter.

Behaviour is checked by running the real ``_sceneReviewFilterValue`` +
``navigateToScene`` source from ``scene-dialog.js``, plus the shared
``sceneMatchesViewFilters`` predicate from ``scenes.js``, in a Node ``vm`` with a few
stubbed globals (skipped when Node is not installed).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ANALYZER = Path(__file__).resolve().parents[2]
SCENE_DIALOG_JS = (ANALYZER / "js" / "scene-dialog.js").read_text(encoding="utf-8")
SCENES_JS = (ANALYZER / "js" / "scenes.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.unit

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


def _body(src, start, end):
    s = src.index(start)
    return src[s:src.index(end, s)]


NAV_SRC = (_body(SCENES_JS, "/** True when any photo in the scene is a favorite", "function isManuallyReviewedScene")
           + _body(SCENE_DIALOG_JS, "/** The grid's review-state filter", "// ── Review-flow shortcut helpers"))


def test_arrow_left_wrap_lets_navigate_pick_the_last_image():
    # The old call computed the adjacent scene's last index, which is wrong
    # once navigation can skip hidden scenes.
    assert "navigateToScene(-1, -1)" in SCENE_DIALOG_JS
    assert "navigateToScene(-1, prevScene.images.length - 1)" not in SCENE_DIALOG_JS


_HARNESS = r"""
const vm = require('vm');
const [src, payload] = process.argv.slice(1);
const { filter, favOnly, scenes, current, direction, startIndex, splitMode } = JSON.parse(payload);
const opened = [], toasts = [];
const ctx = vm.createContext({
  console, JSON, Array, String, Math,
  scenes,
  _currentScene: scenes.find(s => s.id === current),
  _splitMode: !!splitMode,
  _sceneEditDraft: null, _sceneEditMode: false,
  document: {
    getElementById: (id) => (id === 'filterSceneReviewState' ? { value: filter }
      : id === 'onlyFavoriteScenes' ? { checked: !!favOnly } : null),
    removeEventListener: () => {},
  },
  getSetting: (_k, d) => d,
  isManuallyReviewedScene: (s) => !!s.reviewed,
  KestrelFavorites: { isFavorite: (r) => !!(r && r.favorite === '1') },
  showToast: (m) => toasts.push(m),
  _flashSplitSceneButton: () => {},
  _sceneKeyHandler: () => {},
  sceneDlg: { close: () => {} },
  openSceneDialog: (id, idx) => opened.push([id, idx]),
});
vm.runInContext(src, ctx);
ctx.__args = [direction, startIndex];
vm.runInContext('navigateToScene(...__args)', ctx);
process.stdout.write(JSON.stringify({ opened, toasts }));
"""


def _scenes():
    # a and c reviewed; b, d unreviewed; images counts differ so last-index is checkable.
    # c and d have a favorite photo.
    fav = {"favorite": "1"}
    return [
        {"id": "a", "reviewed": True, "images": [{}, {}]},
        {"id": "b", "reviewed": False, "images": [{}, {}, {}]},
        {"id": "c", "reviewed": True, "images": [fav]},
        {"id": "d", "reviewed": False, "images": [{}, {}, fav, {}]},
    ]


def _nav(filter, current, direction, start=0, split=False, fav_only=False):
    payload = json.dumps({
        "filter": filter, "favOnly": fav_only, "scenes": _scenes(), "current": current,
        "direction": direction, "startIndex": start, "splitMode": split,
    })
    out = subprocess.run(
        [NODE, "-e", _HARNESS, NAV_SRC, payload],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return json.loads(out.stdout)


@needs_node
def test_unreviewed_filter_skips_reviewed_scenes_forward():
    assert _nav("unreviewed", "b", 1)["opened"] == [["d", 0]]


@needs_node
def test_current_scene_just_marked_reviewed_still_steps_from_its_position():
    # Starting on reviewed scene "a" (e.g. just marked) goes to the next unreviewed one.
    assert _nav("unreviewed", "a", 1)["opened"] == [["b", 0]]


@needs_node
def test_unreviewed_filter_backward_to_last_image():
    assert _nav("unreviewed", "d", -1, -1)["opened"] == [["b", 2]]


@needs_node
def test_no_more_unreviewed_stays_put_and_says_so():
    res = _nav("unreviewed", "d", 1)
    assert res["opened"] == []
    assert res["toasts"] and "unreviewed" in res["toasts"][0]


@needs_node
def test_reviewed_filter_skips_unreviewed():
    assert _nav("reviewed", "a", 1)["opened"] == [["c", 0]]


@needs_node
def test_all_filter_is_unchanged_adjacent_step():
    assert _nav("all", "a", 1)["opened"] == [["b", 0]]
    assert _nav("all", "c", -1, -1)["opened"] == [["b", 2]]
    assert _nav("all", "d", 1)["opened"] == []


@needs_node
def test_split_mode_still_blocks_navigation():
    assert _nav("unreviewed", "b", 1, split=True)["opened"] == []


@needs_node
def test_favorites_only_skips_scenes_without_a_favorite():
    assert _nav("all", "a", 1, fav_only=True)["opened"] == [["c", 0]]
    assert _nav("all", "d", -1, -1, fav_only=True)["opened"] == [["c", 0]]


@needs_node
def test_favorites_only_combines_with_the_review_filter():
    # Unreviewed + favorites: only d qualifies.
    assert _nav("unreviewed", "a", 1, fav_only=True)["opened"] == [["d", 0]]
    assert _nav("reviewed", "c", 1, fav_only=True)["opened"] == []
