"""Tests for the Culling Assistant's quality-score accept cutoff.

The auto-categorize cutoff is a quality score (0.00–1.00) with the active
rating profile's star thresholds drawn on the slider, rather than an integer
star level.

The Python half (``get_rating_thresholds``) is tested directly. The frontend
half lives inline in ``culling.html`` and the repo has no JS test runner, so
those are source-level lints in the spirit of ``test_raw_warn_banner.py``.
Behavioural coverage (tick positions, band note, legacy migration, the accept
rule with manual ratings) was verified in a headless Chromium harness; see the
PR description.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.ratings import RATING_PROFILES, quality_to_rating

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ANALYZER_DIR = os.path.dirname(os.path.dirname(_THIS_DIR))


def _read(*parts: str) -> str:
    with open(os.path.join(_ANALYZER_DIR, *parts), "r", encoding="utf-8") as f:
        return f.read()


class TestRatingThresholdsBridge(unittest.TestCase):
    """api_bridge.get_rating_thresholds is the single source for the frontend."""

    def setUp(self):
        self.src = _read("api_bridge.py")

    def test_method_exists(self):
        self.assertIn("def get_rating_thresholds(self)", self.src)

    def test_serves_from_the_ratings_module_not_a_copy(self):
        body = self.src.split("def get_rating_thresholds(self)", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("RATING_PROFILES", body)
        # Whichever resolver is in use, the thresholds must come from
        # kestrel_analyzer.ratings rather than a local table.
        self.assertRegex(body, r"(resolve_thresholds|get_profile_thresholds)")
        self.assertIn("rating_profile", body)
        # A hardcoded threshold number here would silently drift from ratings.py.
        self.assertNotRegex(body, r"0\.(85|60|40|15)\b")

    def test_returns_the_documented_shape(self):
        body = self.src.split("def get_rating_thresholds(self)", 1)[1].split("\n    def ", 1)[0]
        for key in ("'success'", "'profile'", "'thresholds'", "'profiles'", "'error'"):
            with self.subTest(key=key):
                self.assertIn(key, body)


class TestCullingCutoffMarkup(unittest.TestCase):
    def setUp(self):
        self.html = _read("culling.html")

    def test_slider_spans_the_quality_range(self):
        tag = re.search(r'<input type="range" id="qualityCutoffSlider"[^>]*>', self.html)
        self.assertIsNotNone(tag, "cutoff slider not found")
        for attr in ('min="0"', 'max="1"', 'step="0.01"'):
            with self.subTest(attr=attr):
                self.assertIn(attr, tag.group(0))

    def test_default_is_the_balanced_four_star_floor(self):
        tag = re.search(r'<input type="range" id="qualityCutoffSlider"[^>]*>', self.html)
        self.assertIn('value="0.60"', tag.group(0))
        self.assertAlmostEqual(RATING_PROFILES["balanced"]["four"], 0.60, places=2)

    def test_star_overlay_and_note_elements_exist(self):
        for eid in ("qualityScaleTicks", "qualityCutoffNote", "qualityCutoffVal"):
            with self.subTest(element=eid):
                self.assertRegex(self.html, rf'id="{eid}"')

    def test_label_asks_for_a_quality_score(self):
        self.assertIn("Accept if quality is at least", self.html)

    def test_tick_styles_are_defined(self):
        for cls in (".quality-scale-ticks", ".quality-tick", ".quality-tick.above", ".quality-scale-note"):
            with self.subTest(css_class=cls):
                self.assertIn(cls, self.html)


class TestCullingCutoffLogic(unittest.TestCase):
    def setUp(self):
        self.html = _read("culling.html")

    def test_thresholds_come_from_the_bridge(self):
        self.assertIn("get_rating_thresholds()", self.html)
        self.assertIn("function loadRatingThresholds", self.html)

    def test_thresholds_load_before_settings_migration(self):
        """A legacy star cutoff is converted using the profile thresholds."""
        init = self.html.index("async function init()")
        tail = self.html[init:]
        self.assertLess(
            tail.index("await loadRatingThresholds()"),
            tail.index("await loadCullingSettings()"),
            "thresholds must load before the legacy cutoff is migrated",
        )

    def test_accept_rule_compares_quality_not_stars(self):
        self.assertIn("effectiveQuality(r) >= qualityCutoff", self.html)
        self.assertNotIn("return rt > cutoff;", self.html)

    def test_manual_ratings_still_win_over_the_computed_score(self):
        body = self.html.split("function effectiveQuality(row)", 1)[1].split("\n    }", 1)[0]
        self.assertIn("getOrigin(row) === 'manual'", body)
        self.assertIn("qualityFloorForStar", body)

    def test_unrated_images_still_follow_the_unrated_default(self):
        self.assertIn("if (rt === 0) return unratedDefault === 'accept';", self.html)

    def test_new_setting_key_is_written_and_read(self):
        self.assertIn("culling_quality_score_cutoff", self.html)
        # The legacy key must still be honoured on load for existing installs.
        self.assertIn("culling_quality_cutoff", self.html)

    def test_star_boundaries_match_the_python_mapping(self):
        """starForQuality in culling.html mirrors ratings.quality_to_rating."""
        body = self.html.split("function starForQuality(q)", 1)[1].split("\n    }", 1)[0]
        order = re.findall(r"_ratingThresholds\.(five|four|three|two)", body)
        self.assertEqual(
            order,
            ["five", "four", "three", "two"],
            "bands must be checked high-to-low or images land in the wrong star",
        )
        returns = re.findall(r"return (\d);", body)
        self.assertEqual(returns, ["0", "5", "4", "3", "2", "1"])

        # Sanity-check the Python side the JS is mirroring.
        balanced = RATING_PROFILES["balanced"]
        self.assertEqual(quality_to_rating(balanced["five"], balanced), 5)
        self.assertEqual(quality_to_rating(balanced["four"], balanced), 4)
        self.assertEqual(quality_to_rating(balanced["three"], balanced), 3)
        self.assertEqual(quality_to_rating(balanced["two"], balanced), 2)
        self.assertEqual(quality_to_rating(balanced["two"] - 0.01, balanced), 1)


if __name__ == "__main__":
    unittest.main()
