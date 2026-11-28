"""Tests for scene review state: thumbnail choice and the review-state filter.

The scene thumbnail is picked from the user's own accept/reject decisions
(best accepted, else best non-rejected, else best overall) rather than from raw
quality alone, and the grid can be filtered to reviewed or unreviewed scenes.

Both halves live in ``analyzer/js`` and the repo has no JS test runner, so these
are source-level lints in the spirit of ``test_raw_warn_banner.py`` and
``test_culling_quality_cutoff.py``. Behavioural coverage — the full tier
priority table, auto-cull rows being ignored, tie-breaking, the cull counts and
the reviewed/unreviewed predicate — was verified by evaluating the real
``scenes.js`` source in a Node harness; see the PR description.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ANALYZER_DIR = os.path.dirname(os.path.dirname(_THIS_DIR))

_SCENES_JS = os.path.join(_ANALYZER_DIR, 'js', 'scenes.js')
_SCENE_GRID_JS = os.path.join(_ANALYZER_DIR, 'js', 'scene-grid.js')
_CSV_INIT_JS = os.path.join(_ANALYZER_DIR, 'js', 'csv-init.js')
_VISUALIZER_HTML = os.path.join(_ANALYZER_DIR, 'visualizer.html')


def _view_filter_src() -> str:
    """Body of sceneMatchesViewFilters in scenes.js."""
    src = _read(_SCENES_JS)
    start = src.index('function sceneMatchesViewFilters')
    return src[start:src.index('function isManuallyReviewedScene', start)]


def _read(path: str) -> str:
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


class TestRepresentativeSelection(unittest.TestCase):
    """The thumbnail must reflect the user's decisions, in priority order."""

    def setUp(self):
        self.src = _read(_SCENES_JS)

    def test_helper_exists_and_is_used_by_aggregate(self):
        self.assertIn('function _pickSceneRepresentative', self.src)
        self.assertIn('_pickSceneRepresentative(arr)', self.src)

    def test_no_longer_picks_purely_by_quality(self):
        # The superseded implementation walked every row comparing quality and
        # ignored cull state entirely.
        self.assertNotIn(
            "for (const r of arr) if (parseNumber(r.quality) > parseNumber(rep.quality)) rep = r;",
            self.src,
        )

    def test_tiers_are_ordered_accepted_then_non_rejected(self):
        body = self.src.split('function _pickSceneRepresentative', 1)[1]
        body = body.split('\n    }', 1)[0]
        accept_at = body.find("=== 'accept'")
        reject_at = body.find("!== 'reject'")
        self.assertGreater(accept_at, -1, 'no accepted tier')
        self.assertGreater(reject_at, -1, 'no non-rejected tier')
        self.assertLess(
            accept_at, reject_at,
            'accepted photos must be preferred over merely non-rejected ones',
        )

    def test_uses_manual_only_cull_status(self):
        # getCullStatus() returns '' for auto-culled rows, so an untouched
        # scene keeps the representative it always had. Using the raw column
        # would let the auto-culler change thumbnails behind the user's back.
        body = self.src.split('function _pickSceneRepresentative', 1)[1]
        body = body.split('\n    }', 1)[0]
        self.assertIn('getCullStatus(r)', body)
        self.assertNotIn('getRawCullStatus', body)
        self.assertNotIn('r.culled ===', body)

    def test_scene_exposes_cull_counts(self):
        self.assertIn('function _cullCounts', self.src)
        self.assertIn('cullCounts,', self.src)


class TestReviewStateFilter(unittest.TestCase):
    """All / reviewed / unreviewed, with the legacy boolean migrated."""

    def test_markup_offers_three_states(self):
        html = _read(_VISUALIZER_HTML)
        self.assertIn('id="filterSceneReviewState"', html)
        for value in ('"all"', '"reviewed"', '"unreviewed"'):
            self.assertIn(f'<option value={value}', html)

    def test_superseded_checkbox_is_gone(self):
        html = _read(_VISUALIZER_HTML)
        self.assertNotIn('id="filterScenesManualRated"', html)
        # ...and nothing still reads it.
        self.assertNotIn('filterScenesManualRated', _read(_SCENE_GRID_JS))
        self.assertNotIn('filterScenesManualRated', _read(_CSV_INIT_JS))

    def test_grid_filters_both_directions(self):
        # The grid and scene-to-scene navigation share one predicate in scenes.js.
        self.assertIn("scenes.filter(sceneMatchesViewFilters)", _read(_SCENE_GRID_JS))
        src = _view_filter_src()
        self.assertIn("review === 'reviewed' && !isManuallyReviewedScene(scene)", src)
        self.assertIn("review === 'unreviewed' && isManuallyReviewedScene(scene)", src)

    def test_unreviewed_is_the_exact_complement(self):
        """Both branches must go through the same predicate.

        'Unreviewed' means no user-assigned trait anywhere on the scene --
        across its photos and its scene tags. Reimplementing that test instead
        of negating isManuallyReviewedScene would let the two drift apart.
        """
        self.assertEqual(2, _view_filter_src().count('isManuallyReviewedScene'))

    def test_legacy_setting_is_migrated(self):
        src = _read(_CSV_INIT_JS)
        self.assertIn('sceneReviewFilter', src)
        # An install that had the old checkbox ticked must land on 'reviewed'.
        self.assertIn("getSetting('onlyManualRatedScenes', false) ? 'reviewed' : 'all'", src)

    def test_filter_value_is_validated_before_use(self):
        src = _read(_CSV_INIT_JS)
        self.assertIn("VALID = ['all', 'reviewed', 'unreviewed']", src)
        self.assertIn('VALID.includes(t.value)', src)


class TestGridRefreshAfterCullChange(unittest.TestCase):
    """A cull decision now changes the thumbnail, so the grid owes a repaint."""

    def test_close_handler_repaints_once(self):
        src = _read(os.path.join(_ANALYZER_DIR, 'js', 'scene-dialog.js'))
        self.assertIn('_sceneCullDecisionsChanged = true;', src)
        self.assertIn("sceneDlg?.addEventListener('close'", src)

    def test_navigation_between_scenes_does_not_drop_the_refresh(self):
        """close fires on scene-to-scene navigation too; the flag must survive.

        The guard has to return BEFORE clearing the flag, or navigating away
        from a scene you just culled would swallow the pending repaint.
        """
        src = _read(os.path.join(_ANALYZER_DIR, 'js', 'scene-dialog.js'))
        handler = src.split("sceneDlg?.addEventListener('close'", 1)[1]
        handler = handler.split('});', 1)[0]
        open_guard = handler.find('if (sceneDlg.open) return;')
        clear = handler.find('_sceneCullDecisionsChanged = false;')
        self.assertGreater(open_guard, -1, 'no reopen guard')
        self.assertGreater(clear, -1, 'flag is never cleared')
        self.assertLess(open_guard, clear, 'flag cleared before the reopen guard')


if __name__ == '__main__':
    unittest.main()


# ── Behaviour, run through Node ──────────────────────────────────────────────
# What counts as a reviewed scene, and the recent-folder chip labels, checked by
# evaluating the real functions in a Node vm (skipped without Node).

_NODE = shutil.which('node')
_needs_node = pytest.mark.skipif(_NODE is None, reason='node not installed')

_VM_HARNESS = r"""
const vm = require('vm');
const [src, expr] = process.argv.slice(1);
const ctx = vm.createContext({});
vm.runInContext(src, ctx);
process.stdout.write(JSON.stringify(vm.runInContext(expr, ctx)));
"""


def _run_js(src: str, expr: str):
    out = subprocess.run([_NODE, '-e', _VM_HARNESS, src, expr],
                         capture_output=True, text=True, check=True, timeout=30)
    return json.loads(out.stdout)


def _reviewed_src() -> str:
    src = _read(_SCENES_JS)
    head = src[:src.index('/** Highest-quality row')]
    # Rows carry their own rating / origin / cull status in this harness.
    stubs = ('function getRating(r) { return r.rating || 0; }\n'
             'function getOrigin(r) { return r.origin || ""; }\n'
             'function getCullStatus(r) { return r.cull || ""; }\n')
    return stubs + head


@pytest.mark.unit
@_needs_node
def test_reviewed_means_a_decision_or_a_star_the_user_set():
    src = _reviewed_src()
    cases = {
        'accepted': {'images': [{'cull': 'accept'}]},
        'rejected': {'images': [{'cull': 'reject'}]},
        'manual_star': {'images': [{'rating': 3, 'origin': 'manual'}]},
        'auto_star_only': {'images': [{'rating': 4, 'origin': 'auto'}]},
        'species_confirmed_only': {'isApproved': True, 'images': [{}]},
        'named_only': {'sceneName': 'Egret fishing', 'images': [{}]},
        'untouched': {'images': [{}, {}]},
    }
    got = _run_js(src, 'Object.fromEntries(Object.entries(%s).map(([k, s]) => [k, isManuallyReviewedScene(s)]))'
                  % json.dumps(cases))
    assert got == {
        'accepted': True, 'rejected': True, 'manual_star': True,
        'auto_star_only': False, 'species_confirmed_only': False,
        'named_only': False, 'untouched': False,
    }


@pytest.mark.unit
@_needs_node
def test_recent_chip_labels_are_folder_names_unless_two_share_one():
    src = _read(os.path.join(_ANALYZER_DIR, 'js', 'folder-tree.js'))
    start = src.index('    function recentsChipLabels(')
    fn = src[start:src.index('\n    }\n', start) + 6]
    paths = ['/Users/a/Pictures/Costa Rica', 'E:\\DCIM\\105NZ_8', '/Volumes/SD2/DCIM2/105NZ_8', '/Users/a/Birds/']
    got = _run_js(fn, 'recentsChipLabels(%s)' % json.dumps(paths))
    assert got == ['Costa Rica', 'DCIM/105NZ_8', 'DCIM2/105NZ_8', 'Birds']
