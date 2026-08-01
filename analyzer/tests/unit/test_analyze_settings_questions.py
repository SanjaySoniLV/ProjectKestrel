"""Static wiring tests for the question-and-answer analysis settings column.

The critical settings block in the Analyze Folders dialog is phrased as
questions about the shoot. Each answer pair is the visible control for a
hidden checkbox (``#analyzeWildlife`` / ``#analyzeSpeciesDetection``) that
``event-wiring.js`` reads when Start is clicked.

The repo has no JS test runner, so these are source-level lints in the same
spirit as ``test_raw_warn_banner.py``. They catch the failure modes that would
silently analyze with the wrong scope: a renamed radio id, a dropped hidden
checkbox, or the mirror wiring being moved back behind the dialog-open path.

Behavioural coverage (mirroring in both directions, defaults, responsive
wrapping) was verified in a headless Chromium harness; see the PR description.
"""

from __future__ import annotations

import os
import re
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ANALYZER_DIR = os.path.dirname(os.path.dirname(_THIS_DIR))

# Radio id -> the hidden checkbox it mirrors onto.
ANSWER_MIRRORS = {
    "adlgSubjectScopeWildlife": "analyzeWildlife",
    "adlgSpeciesScopeYes": "analyzeSpeciesDetection",
}
# The "negative" half of each pair; present so the group has two answers.
ANSWER_ALTERNATES = ("adlgSubjectScopeBirds", "adlgSpeciesScopeNo")

QUESTIONS = (
    "What subjects does this shoot contain?",
    "Does this shoot contain North American birds?",
)

# The Start handler in event-wiring.js reads these; they must survive the
# rephrasing or analysis silently runs with the wrong options.
START_HANDLER_IDS = ("analyzeWildlife", "analyzeSpeciesDetection", "adlgWildlifeModelMode")


def _read(*parts: str) -> str:
    with open(os.path.join(_ANALYZER_DIR, *parts), "r", encoding="utf-8") as f:
        return f.read()


class TestQuestionMarkup(unittest.TestCase):
    def setUp(self):
        self.html = _read("visualizer.html")

    def test_questions_are_present(self):
        for q in QUESTIONS:
            with self.subTest(question=q):
                self.assertIn(q, self.html)

    def test_species_help_text_states_the_north_american_limit(self):
        self.assertIn("only recognizes North American birds", self.html)

    def test_every_answer_radio_exists(self):
        for rid in list(ANSWER_MIRRORS) + list(ANSWER_ALTERNATES):
            with self.subTest(radio=rid):
                self.assertRegex(self.html, rf'id="{rid}"')

    def test_answer_pairs_share_a_radio_group_name(self):
        for name, count in (("adlgSubjectScope", 2), ("adlgSpeciesScope", 2)):
            with self.subTest(group=name):
                self.assertEqual(len(re.findall(rf'name="{name}"', self.html)), count)

    def test_start_handler_ids_survive(self):
        for eid in START_HANDLER_IDS:
            with self.subTest(element=eid):
                self.assertRegex(self.html, rf'id="{eid}"')

    def test_mirrored_checkboxes_are_hidden_and_unfocusable(self):
        """A visible duplicate control would let the two drift apart."""
        for box in ANSWER_MIRRORS.values():
            with self.subTest(checkbox=box):
                tag = re.search(rf'<input id="{box}"[^>]*>', self.html)
                self.assertIsNotNone(tag, f"{box} input tag not found")
                self.assertIn('class="hidden"', tag.group(0))
                self.assertIn('tabindex="-1"', tag.group(0))

    def test_experimental_badge_is_retired(self):
        """The owner asked for the badge to go when wildlife became an answer."""
        critical = re.search(
            r'<div class="analyze-dlg-settings-critical">(.*?)\n          </div>',
            self.html,
            re.S,
        )
        self.assertIsNotNone(critical, "critical settings block not found")
        self.assertNotIn("adlg-tag", critical.group(1))
        self.assertNotIn("experimental", critical.group(1).lower())


class TestDetectionModelIsBuried(unittest.TestCase):
    def setUp(self):
        self.html = _read("visualizer.html")

    def test_old_fast_accurate_radio_pair_is_gone(self):
        for rid in ("adlgWildlifeModelModeAccurate", "adlgWildlifeModelModeFast"):
            with self.subTest(radio=rid):
                self.assertNotIn(rid, self.html)

    def test_model_select_defaults_to_accurate(self):
        sel = re.search(r'<select id="adlgWildlifeModelMode".*?</select>', self.html, re.S)
        self.assertIsNotNone(sel, "model select not found")
        self.assertRegex(sel.group(0), r'<option value="accurate"[^>]*selected')

    def test_model_select_is_visible_and_inside_more_options(self):
        sel = re.search(r'<select id="adlgWildlifeModelMode"[^>]*>', self.html)
        self.assertIsNotNone(sel)
        self.assertNotIn("hidden", sel.group(0))

        more = self.html.index('<details class="analyze-dlg-more-options"')
        self.assertGreater(
            self.html.index('id="adlgWildlifeModelMode"'),
            more,
            "detection model should sit inside More options, not the critical block",
        )


class TestMirrorWiring(unittest.TestCase):
    def setUp(self):
        self.js = _read("js", "analyze-dialog.js")

    def test_pairs_table_maps_each_radio_to_its_checkbox(self):
        table = re.search(r"const _ANSWER_PAIRS = \[(.*?)\];", self.js, re.S)
        self.assertIsNotNone(table, "_ANSWER_PAIRS table not found")
        for radio, box in ANSWER_MIRRORS.items():
            with self.subTest(radio=radio):
                self.assertRegex(table.group(1), rf"'{radio}'.*?'{box}'")

    def test_mirrors_are_wired_at_load_not_on_dialog_open(self):
        """Wiring inside openAnalyzeDialog would break if that path throws first."""
        self.assertIn("DOMContentLoaded", self.js)
        wire_at = self.js.index("wireAnalyzeAnswerMirrors()")
        open_at = self.js.index("async function openAnalyzeDialog()")
        self.assertLess(wire_at, open_at, "mirror wiring must run before/independently of dialog open")

    def test_dialog_open_still_pushes_saved_settings_into_the_answers(self):
        self.assertIn("syncAnalyzeAnswersFromSettings()", self.js)

    def test_stale_radio_sync_removed(self):
        self.assertNotIn("_syncModeRadios", self.js)


class TestQuestionStyles(unittest.TestCase):
    def setUp(self):
        self.css = _read("css", "dialogs", "analyze.css")

    def test_question_classes_are_styled(self):
        for cls in (".adlg-question", ".adlg-question-text", ".adlg-answer-pair", ".adlg-question-help"):
            with self.subTest(css_class=cls):
                self.assertIn(cls, self.css)

    def test_answers_collapse_to_one_column_when_narrow(self):
        """Both answer labels cannot sit side by side in a narrow dialog."""
        self.assertRegex(self.css, r"@media \(max-width: \d+px\)\s*\{\s*\.adlg-answer-pair")


if __name__ == "__main__":
    unittest.main()
