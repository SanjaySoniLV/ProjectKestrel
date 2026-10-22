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

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ANALYZER_DIR = os.path.dirname(os.path.dirname(_THIS_DIR))

_SCENES_JS = os.path.join(_ANALYZER_DIR, 'js', 'scenes.js')
_SCENE_GRID_JS = os.path.join(_ANALYZER_DIR, 'js', 'scene-grid.js')
_CSV_INIT_JS = os.path.join(_ANALYZER_DIR, 'js', 'csv-init.js')
_VISUALIZER_HTML = os.path.join(_ANALYZER_DIR, 'visualizer.html')


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
        src = _read(_SCENE_GRID_JS)
        self.assertIn("=== 'reviewed' ? scenes.filter(isManuallyReviewedScene)", src)
        self.assertIn("=== 'unreviewed' ? scenes.filter(s => !isManuallyReviewedScene(s))", src)

    def test_unreviewed_is_the_exact_complement(self):
        """Both branches must go through the same predicate.

        'Unreviewed' means no user-assigned trait anywhere on the scene --
        across its photos and its scene tags. Reimplementing that test instead
        of negating isManuallyReviewedScene would let the two drift apart.
        """
        src = _read(_SCENE_GRID_JS)
        self.assertEqual(2, src.count('isManuallyReviewedScene'))

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
