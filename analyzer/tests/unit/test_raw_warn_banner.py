"""Static wiring tests for the unsupported-RAW warning banner.

The banner is pure frontend (``js/raw-warn.js`` + ``css/raw-warn-banner.css``
+ markup in ``visualizer.html``), and the repo has no JS test runner, so
these are source-level lints in the same spirit as
``tests/test_security_visualizer_js_xss.py``. They catch the failure modes
that would silently disable the banner: a renamed element id, a dropped
stylesheet/script tag, a changed settings key, or a broken Learn More URL.

Behavioural coverage (detector output, dismissal, persistence, stacking
under the legal banner) was verified in a headless Chromium harness when
the feature landed; see the PR description.
"""

from __future__ import annotations

import os
import re
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ANALYZER_DIR = os.path.dirname(os.path.dirname(_THIS_DIR))

LEARN_MORE_URL = "https://www.projectkestrel.org/notes/nikon-unsupported-raws"
SUPPRESS_KEY = "raw_unsupported_warn_suppressed"
FALLBACK_MARKER = "embedded_preview_jpeg"

# Element ids the JS looks up; all must exist in the markup.
ELEMENT_IDS = (
    "rawWarnNotice",
    "rawWarnMsg",
    "rawWarnLearnMore",
    "rawWarnDontShowBtn",
    "rawWarnCloseBtn",
)


def _read(*parts: str) -> str:
    with open(os.path.join(_ANALYZER_DIR, *parts), "r", encoding="utf-8") as f:
        return f.read()


class TestRawWarnBannerWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.html = _read("visualizer.html")
        self.js = _read("js", "raw-warn.js")
        self.css = _read("css", "raw-warn-banner.css")

    def test_markup_defines_every_element_the_js_queries(self) -> None:
        for el_id in ELEMENT_IDS:
            with self.subTest(element=el_id):
                self.assertIn(
                    f'id="{el_id}"', self.html,
                    f"visualizer.html is missing #{el_id}",
                )
                self.assertIn(
                    f"'{el_id}'", self.js,
                    f"raw-warn.js never references #{el_id}",
                )

    def test_stylesheet_and_script_are_loaded(self) -> None:
        self.assertIn('href="css/raw-warn-banner.css"', self.html)
        self.assertIn('src="js/raw-warn.js"', self.html)

    def test_script_loads_before_its_callers(self) -> None:
        """raw-warn.js must be parsed before the modules that call into it."""
        pos = self.html.index('src="js/raw-warn.js"')
        for caller in ("js/multi-folder.js", "js/csv-init.js", "js/event-wiring.js"):
            with self.subTest(caller=caller):
                self.assertLess(
                    pos, self.html.index(f'src="{caller}"'),
                    f"raw-warn.js must be loaded before {caller}",
                )

    def test_banner_starts_hidden(self) -> None:
        banner = re.search(r'<div id="rawWarnNotice"[^>]*>', self.html)
        self.assertIsNotNone(banner, "rawWarnNotice div not found")
        self.assertIn("hidden", banner.group(0))

    def test_learn_more_points_at_the_published_note(self) -> None:
        self.assertIn(LEARN_MORE_URL, self.html)
        self.assertIn(LEARN_MORE_URL, self.js)

    def test_detector_keys_off_the_pipeline_fallback_marker(self) -> None:
        """The marker must match what pipeline.py writes to the CSV."""
        self.assertIn(FALLBACK_MARKER, self.js)
        pipeline = _read("kestrel_analyzer", "pipeline.py")
        self.assertIn(
            f'"{FALLBACK_MARKER}"', pipeline,
            "pipeline.py no longer emits the exposure_pipeline value the "
            "banner detects; the banner would never fire",
        )

    def test_suppression_key_is_persisted_to_the_backend(self) -> None:
        self.assertIn(SUPPRESS_KEY, self.js)
        self.assertIn("save_settings_data", self.js)

    def test_close_button_is_labelled_for_screen_readers(self) -> None:
        close = re.search(r'<button id="rawWarnCloseBtn"[^>]*>', self.html, re.S)
        self.assertIsNotNone(close)
        self.assertIn("aria-label", close.group(0))

    def test_css_defines_the_classes_the_markup_uses(self) -> None:
        for cls in (
            "raw-warn-banner", "raw-warn-msg", "raw-warn-actions",
            "raw-warn-link", "raw-warn-btn", "raw-warn-close",
        ):
            with self.subTest(cls=cls):
                self.assertIn(f".{cls}", self.css)
                self.assertIn(cls, self.html)

    def test_banner_has_a_hidden_rule(self) -> None:
        self.assertRegex(self.css, r"\.raw-warn-banner\.hidden\s*\{[^}]*display:\s*none")

    def test_stacked_offset_is_driven_by_the_measured_variable(self) -> None:
        """CSS must consume the custom property the JS sets, or the banner
        overlaps the legal notice when that one wraps to two lines."""
        self.assertIn("--raw-warn-offset", self.css)
        self.assertIn("--raw-warn-offset", self.js)

    def test_learn_more_routes_through_open_url(self) -> None:
        """target=_blank alone opens a dead window under pywebview."""
        self.assertIn("open_url", self.js)

    def test_callers_refresh_the_banner_after_loading_rows(self) -> None:
        for module in ("multi-folder.js", "queue.js"):
            with self.subTest(module=module):
                self.assertIn(
                    "refreshRawWarnBanner", _read("js", module),
                    f"{module} loads rows but never refreshes the banner",
                )


if __name__ == "__main__":
    unittest.main()
