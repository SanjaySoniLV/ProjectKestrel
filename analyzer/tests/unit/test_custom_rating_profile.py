"""Tests for the user-defined ('custom') rating profile.

The custom profile's cutoffs live in the ``rating_thresholds_custom`` setting
rather than ``RATING_PROFILES``. ``normalize_custom_thresholds`` is the guard
that keeps them usable: whatever it is handed, it must return four cutoffs in
[0, 1] that are strictly descending, because two cutoffs collapsing onto the
same value makes a whole star band unreachable.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.ratings import (
    CUSTOM_PROFILE,
    MIN_THRESHOLD_GAP,
    RATING_PROFILES,
    THRESHOLD_KEYS,
    normalize_custom_thresholds,
    quality_to_rating,
    resolve_thresholds,
)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ANALYZER_DIR = os.path.dirname(os.path.dirname(_THIS_DIR))


def _read(*parts: str) -> str:
    with open(os.path.join(_ANALYZER_DIR, *parts), "r", encoding="utf-8") as f:
        return f.read()


def _descending(t: dict) -> bool:
    vals = [t[k] for k in THRESHOLD_KEYS]
    return all(vals[i] > vals[i + 1] for i in range(len(vals) - 1))


class TestNormalizeCustomThresholds(unittest.TestCase):
    def test_none_falls_back_to_balanced(self):
        self.assertEqual(normalize_custom_thresholds(None), RATING_PROFILES["balanced"])

    def test_a_valid_set_is_returned_unchanged(self):
        t = {"five": 0.9, "four": 0.7, "three": 0.5, "two": 0.3}
        self.assertEqual(normalize_custom_thresholds(t), t)

    def test_missing_keys_fall_back_per_key(self):
        out = normalize_custom_thresholds({"five": 0.95})
        self.assertEqual(out["five"], 0.95)
        for key in ("four", "three", "two"):
            with self.subTest(key=key):
                self.assertEqual(out[key], RATING_PROFILES["balanced"][key])

    def test_out_of_order_input_is_forced_descending(self):
        out = normalize_custom_thresholds({"five": 0.3, "four": 0.7, "three": 0.5, "two": 0.9})
        self.assertTrue(_descending(out), out)

    def test_collapsed_input_keeps_every_band_reachable(self):
        out = normalize_custom_thresholds({k: 0.5 for k in THRESHOLD_KEYS})
        self.assertTrue(_descending(out), out)
        vals = [out[k] for k in THRESHOLD_KEYS]
        for a, b in zip(vals, vals[1:]):
            self.assertGreaterEqual(round(a - b, 4), MIN_THRESHOLD_GAP)

    def test_all_zero_input_lifts_off_the_floor(self):
        out = normalize_custom_thresholds({k: 0 for k in THRESHOLD_KEYS})
        self.assertTrue(_descending(out), out)
        self.assertGreaterEqual(min(out.values()), 0.0)

    def test_values_are_clamped_into_range(self):
        out = normalize_custom_thresholds({"five": 5, "four": 0.7, "three": 0.5, "two": -3})
        self.assertLessEqual(out["five"], 1.0)
        self.assertGreaterEqual(out["two"], 0.0)

    def test_junk_values_fall_back(self):
        out = normalize_custom_thresholds(
            {"five": "abc", "four": None, "three": float("nan"), "two": [1]}
        )
        self.assertTrue(_descending(out), out)
        self.assertEqual(out["five"], RATING_PROFILES["balanced"]["five"])

    def test_booleans_are_not_thresholds(self):
        """float(True) is 1.0; accepting it would silently set a 1.00 cutoff.

        The frontend normalizer rejects booleans, so this side must too or the
        editor would show different bands than the pipeline computes.
        """
        out = normalize_custom_thresholds({"five": True, "four": False, "three": 0.5, "two": 0.3})
        self.assertEqual(out["five"], RATING_PROFILES["balanced"]["five"])
        self.assertEqual(out["four"], RATING_PROFILES["balanced"]["four"])

    def test_numeric_strings_are_accepted(self):
        out = normalize_custom_thresholds({"five": "0.9", "four": "0.7", "three": "0.5", "two": "0.3"})
        self.assertEqual(out, {"five": 0.9, "four": 0.7, "three": 0.5, "two": 0.3})

    def test_non_dict_input_falls_back(self):
        for junk in ("nope", 42, [], None, object()):
            with self.subTest(value=repr(junk)):
                self.assertEqual(normalize_custom_thresholds(junk), RATING_PROFILES["balanced"])

    def test_output_is_always_valid_over_a_fuzz_sweep(self):
        import random

        random.seed(1234)
        choices = [
            lambda: random.random(),
            lambda: random.uniform(-5, 5),
            lambda: random.choice(["x", "", "0.5", None, True, False]),
            lambda: float("nan"),
        ]
        for _ in range(3000):
            raw = {k: random.choice(choices)() for k in THRESHOLD_KEYS}
            out = normalize_custom_thresholds(raw)
            self.assertTrue(_descending(out), (raw, out))
            self.assertTrue(all(0.0 <= v <= 1.0 for v in out.values()), (raw, out))

    def test_every_star_band_stays_reachable(self):
        """A quality score exists that lands in each of the five bands."""
        out = normalize_custom_thresholds({k: 0.5 for k in THRESHOLD_KEYS})
        seen = {quality_to_rating(q / 1000, out) for q in range(0, 1001)}
        self.assertEqual(seen, {1, 2, 3, 4, 5}, out)


class TestResolveThresholds(unittest.TestCase):
    def test_named_profiles_ignore_the_custom_payload(self):
        self.assertEqual(
            resolve_thresholds("strict", {"five": 0.1, "four": 0.09, "three": 0.08, "two": 0.07}),
            RATING_PROFILES["strict"],
        )

    def test_custom_profile_uses_the_supplied_thresholds(self):
        t = {"five": 0.5, "four": 0.4, "three": 0.3, "two": 0.2}
        self.assertEqual(resolve_thresholds(CUSTOM_PROFILE, t), t)

    def test_custom_profile_without_thresholds_falls_back(self):
        self.assertEqual(resolve_thresholds(CUSTOM_PROFILE, None), RATING_PROFILES["balanced"])

    def test_unknown_profile_falls_back_to_balanced(self):
        self.assertEqual(resolve_thresholds("nonsense"), RATING_PROFILES["balanced"])

    def test_profile_name_is_case_insensitive(self):
        t = {"five": 0.5, "four": 0.4, "three": 0.3, "two": 0.2}
        self.assertEqual(resolve_thresholds("CUSTOM", t), t)


class TestSettingsPlumbing(unittest.TestCase):
    def setUp(self):
        self.src = _read("settings_utils.py")

    def test_custom_is_an_allowed_profile(self):
        block = re.search(r"_ALLOWED_RATING_PROFILES = \{(.*?)\}", self.src, re.S)
        self.assertIsNotNone(block)
        self.assertIn("'custom'", block.group(1))

    def test_saved_thresholds_are_normalized_not_trusted(self):
        self.assertIn("rating_thresholds_custom", self.src)
        self.assertIn("normalize_custom_thresholds", self.src)


class TestResolutionSitesUseTheCustomProfile(unittest.TestCase):
    """Every place that turns a profile name into cutoffs must handle 'custom'.

    A site still calling get_profile_thresholds would silently fall back to
    balanced, so a custom profile would apply in some views but not others.
    """

    def test_api_bridge_resolves_custom(self):
        src = _read("api_bridge.py")
        self.assertNotIn("get_profile_thresholds", src)
        self.assertIn("resolve_thresholds", src)
        # apply_normalization and get_rating_thresholds are the two sites.
        self.assertGreaterEqual(src.count("resolve_thresholds("), 2)

    def test_pipeline_resolves_custom(self):
        src = _read("kestrel_analyzer", "pipeline.py")
        self.assertNotIn("get_profile_thresholds", src)
        self.assertIn("rating_thresholds_custom", src)


class TestCustomEditorMarkup(unittest.TestCase):
    def setUp(self):
        self.html = _read("visualizer.html")
        self.js = _read("js", "settings.js")
        self.css = _read("css", "dialogs", "settings.css")

    def test_custom_option_is_offered(self):
        self.assertRegex(self.html, r'<option value="custom">')

    def test_editor_elements_exist(self):
        for eid in (
            "ratingCustomEditor",
            "ratingCustomTrack",
            "ratingCustomBands",
            "ratingCustomHandles",
            "ratingCustomReadout",
            "ratingCustomReset",
        ):
            with self.subTest(element=eid):
                self.assertRegex(self.html, rf'id="{eid}"')

    def test_editor_starts_hidden(self):
        tag = re.search(r'<div class="([^"]*)" id="ratingCustomEditor"', self.html)
        self.assertIsNotNone(tag)
        self.assertIn("hidden", tag.group(1))

    def test_editor_styles_are_defined(self):
        for cls in (".rating-custom-editor", ".rating-custom-band", ".rating-custom-handle"):
            with self.subTest(css_class=cls):
                self.assertIn(cls, self.css)

    def test_js_gap_matches_the_python_constant(self):
        m = re.search(r"const RATING_MIN_GAP = ([\d.]+);", self.js)
        self.assertIsNotNone(m, "RATING_MIN_GAP not found")
        self.assertAlmostEqual(float(m.group(1)), MIN_THRESHOLD_GAP, places=6)

    def test_js_keys_match_the_python_order(self):
        m = re.search(r"const RATING_THRESHOLD_KEYS = \[(.*?)\];", self.js)
        self.assertIsNotNone(m)
        keys = tuple(re.findall(r"'(\w+)'", m.group(1)))
        self.assertEqual(keys, THRESHOLD_KEYS)

    def test_thresholds_are_persisted_and_reapplied(self):
        self.assertIn("rating_thresholds_custom: normalizeCustomThresholds(_customThresholds)", self.js)
        # Editing cutoffs changes every auto rating without changing the
        # profile name, so the re-apply check must compare the values.
        self.assertIn("nextCustom !== prevCustom", self.js)

    def test_handles_are_keyboard_operable(self):
        self.assertIn("ArrowLeft", self.js)
        self.assertIn("aria-valuenow", self.js)
        self.assertIn("role', 'slider'", self.js)


if __name__ == "__main__":
    unittest.main()
