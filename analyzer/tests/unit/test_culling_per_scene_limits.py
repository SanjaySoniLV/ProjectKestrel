"""Tests for the Culling Assistant's per-scene accept limits.

"Min accepted per scene" and "Max accepted per scene" used to be sliders with
hard ceilings of 5 and 20, and the settings loader threw away any stored value
above those. That is fine for burst-sized scenes and useless for a photographer
shooting 100-frame bursts, so the numbers are now click-to-type and accept any
non-negative value, with the slider track growing to fit.

The implementation is inline in ``culling.html`` and the repo has no JS test
runner, so these are source-level lints in the spirit of
``test_culling_quality_cutoff.py``. Behavioural coverage of ``setPerSceneValue``
— growing and shrinking the track, clamping, and the garbage-input cases — was
verified by evaluating the real source in a Node harness (18 assertions); see
the PR description.
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


def _read(*parts: str) -> str:
    with open(os.path.join(_ANALYZER_DIR, *parts), "r", encoding="utf-8") as f:
        return f.read()


class TestOldCeilingsAreGone(unittest.TestCase):
    """The specific limits that made this unusable for big bursts."""

    def setUp(self):
        self.src = _read("culling.html")

    def test_min_slider_no_longer_caps_at_five(self):
        self.assertNotIn('id="minPerSceneSlider" min="0" max="5"', self.src)

    def test_max_slider_no_longer_caps_at_twenty(self):
        self.assertNotIn('id="maxPerSceneSlider" min="0" max="20"', self.src)

    def test_loader_no_longer_discards_large_stored_values(self):
        # The old guards silently dropped anything above the slider ceiling, so
        # a typed value would not survive a reload.
        self.assertNotIn("minPS   <= 5", self.src)
        self.assertNotIn("maxPS   <= 20", self.src)

    def test_loader_accepts_any_non_negative_value(self):
        self.assertIn("setPerSceneValue('min', minPS)", self.src)
        self.assertIn("setPerSceneValue('max', maxPS)", self.src)


class TestClickToType(unittest.TestCase):
    """The number itself is an editable control."""

    def setUp(self):
        self.src = _read("culling.html")

    def test_both_values_are_marked_editable_and_focusable(self):
        for span_id in ("minPerSceneVal", "maxPerSceneVal"):
            block = self.src.split(f'id="{span_id}"', 1)[0][-320:] + \
                self.src.split(f'id="{span_id}"', 1)[1][:320]
            self.assertIn("slider-val--editable", block, span_id)
            self.assertIn('tabindex="0"', block, span_id)

    def test_edit_inputs_exist_for_both(self):
        self.assertIn('id="minPerSceneEdit"', self.src)
        self.assertIn('id="maxPerSceneEdit"', self.src)

    def test_editors_are_wired(self):
        self.assertIn("_wirePerSceneEditor('min')", self.src)
        self.assertIn("_wirePerSceneEditor('max')", self.src)

    def test_keyboard_reachable(self):
        body = self.src.split("function _wirePerSceneEditor", 1)[1].split(
            "\n    let _autoRulesLoaded", 1)[0]
        self.assertIn("'Enter'", body)
        self.assertIn("'Escape'", body)
        self.assertIn("blur", body)

    def test_escape_cancels_without_committing(self):
        body = self.src.split("function _wirePerSceneEditor", 1)[1].split(
            "\n    let _autoRulesLoaded", 1)[0]
        self.assertRegex(body, r"Escape[\s\S]{0,120}_closePerSceneEditor\(which, false\)")

    def test_keystrokes_do_not_reach_the_page_cull_shortcuts(self):
        body = self.src.split("function _wirePerSceneEditor", 1)[1].split(
            "\n    let _autoRulesLoaded", 1)[0]
        self.assertIn("stopPropagation", body)

    def test_label_refresh_skips_a_field_being_typed_into(self):
        # updateSliderLabels runs on every slider input; writing to the open
        # editor would fight the user mid-keystroke.
        body = self.src.split("function updateSliderLabels", 1)[1].split(
            "\n    function ", 1)[0]
        self.assertIn("_editingPerScene !== 'min'", body)
        self.assertIn("_editingPerScene !== 'max'", body)


class TestSetPerSceneValue(unittest.TestCase):
    """The single writer for both counts."""

    def setUp(self):
        self.src = _read("culling.html")
        self.body = self.src.split("function setPerSceneValue", 1)[1].split(
            "\n    function ", 1)[0]

    def test_grows_the_track_to_fit_a_typed_value(self):
        self.assertIn("Math.max(PER_SCENE_BASE_MAX[which], n)", self.body)

    def test_clamps_negatives_to_zero(self):
        self.assertIn("n < 0", self.body)

    def test_has_a_sanity_ceiling(self):
        self.assertIn("PER_SCENE_HARD_MAX", self.body)

    def test_base_maxima_are_defined_for_both(self):
        self.assertRegex(self.src, r"PER_SCENE_BASE_MAX\s*=\s*\{\s*min:\s*\d+,\s*max:\s*\d+\s*\}")


class TestZeroStillMeansNoCap(unittest.TestCase):
    """0 on the max slider is 'no cap', and that must survive the change."""

    def setUp(self):
        self.src = _read("culling.html")

    def test_get_slider_values_still_maps_zero_to_infinity(self):
        body = self.src.split("function getSliderValues", 1)[1].split(
            "\n    let _autoRulesLoaded", 1)[0]
        self.assertIn("maxRaw === 0 ? Infinity : maxRaw", body)

    def test_label_still_renders_the_infinity_glyph(self):
        body = self.src.split("function updateSliderLabels", 1)[1].split(
            "\n    function ", 1)[0]
        self.assertIn("\\u221e", body)

    def test_max_control_documents_the_zero_meaning(self):
        self.assertRegex(self.src, r'id="maxPerSceneVal"[\s\S]{0,200}0 = no cap')


class TestAutoCategorizeUnchanged(unittest.TestCase):
    """Raising the ceilings must not alter how the rules are applied."""

    def setUp(self):
        self.src = _read("culling.html")
        self.body = self.src.split("function applyAutoCategorize", 1)[1].split(
            "\n    // ---- Initialize culling state ----", 1)[0]

    def test_min_still_wins_over_max(self):
        self.assertIn("Math.max(maxKeep, minKeep)", self.body)

    def test_manual_decisions_still_preserved_by_default(self):
        self.assertIn("if (preserveManual && isProtectedCull(r)) continue;", self.body)

    def test_still_applied_per_scene(self):
        self.assertIn("for (const scene of scenes)", self.body)


if __name__ == "__main__":
    unittest.main()
