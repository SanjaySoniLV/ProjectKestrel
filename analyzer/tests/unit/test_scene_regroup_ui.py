"""Tests for the Regroup Scenes folder action.

The grouping rules themselves live in ``kestrel_analyzer/regroup.py`` and are
covered behaviourally in ``test_regroup.py``. What is left is the wiring: the
bridge methods that serve the dialog, and the frontend that renders it and
writes the result back.

The frontend half lives in ``analyzer/js/scene-regroup.js`` and the repo has no
JS test runner, so those are source-level lints in the spirit of
``test_scene_review_state.py``. The behavioural coverage that matters most —
the scenedata migration, which decides whether a scene's verified tags follow
its photos — was verified by evaluating the real source in a Node harness
(21 assertions, including that renumbering never hands one scene's verified
tags to a different scene); see the PR description.
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

pytestmark = pytest.mark.unit

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ANALYZER_DIR = os.path.dirname(os.path.dirname(_THIS_DIR))


def _read(*parts: str) -> str:
    with open(os.path.join(_ANALYZER_DIR, *parts), "r", encoding="utf-8") as f:
        return f.read()


class TestBridgeMethods(unittest.TestCase):
    """The dialog's three bridge calls exist and keep their contract."""

    def setUp(self):
        self.src = _read("api_bridge.py")

    def test_methods_exist(self):
        for name in ("regroup_scenes_prepare", "regroup_scenes_preview",
                     "regroup_scenes_release"):
            self.assertIn(f"def {name}(", self.src)

    def test_rules_come_from_the_regroup_module_not_a_copy(self):
        body = self.src.split("def regroup_scenes_preview(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("from kestrel_analyzer.regroup import plan_regroup", body)

    def test_preview_omits_the_assignment_unless_asked(self):
        # The per-image map is the bulk of the payload and the live preview
        # never reads it; shipping it on every slider tick would make the
        # dialog crawl on a large folder.
        body = self.src.split("def regroup_scenes_preview(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("if not include_assignment", body)
        self.assertIn("plan.pop('assignment', None)", body)

    def test_preview_rejects_a_session_for_another_folder(self):
        body = self.src.split("def regroup_scenes_preview(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("normcase", body)
        self.assertIn("different folder", body)

    def test_prepare_caps_the_working_set(self):
        body = self.src.split("def regroup_scenes_prepare(", 1)[1].split("\n    @staticmethod", 1)[0]
        self.assertIn("_REGROUP_MAX_IMAGES", body)

    def test_release_clears_the_cache(self):
        body = self.src.split("def regroup_scenes_release(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("self._regroup_cache = None", body)

    def test_analyzed_settings_are_read_from_metadata(self):
        body = self.src.split("def _read_analyzed_scene_settings(", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("kestrel_metadata.json", body)
        self.assertIn("scene_time_threshold", body)
        self.assertIn("scene_break_gap_seconds", body)


class TestDialogWiring(unittest.TestCase):
    """The dialog is registered everywhere the app expects to find it."""

    def setUp(self):
        self.html = _read("visualizer.html")
        self.grid = _read("js", "scene-grid.js")

    def test_script_and_stylesheet_are_loaded(self):
        self.assertIn('<script src="js/scene-regroup.js"></script>', self.html)
        self.assertIn('href="css/dialogs/regroup.css"', self.html)

    def test_script_loads_after_its_dependencies(self):
        # scene-regroup.js calls helpers defined in crop-data.js (the scenedata
        # helpers) and scene-grid.js (renderScenes); classic scripts share one
        # scope, but only in load order. Match the <script> tags specifically —
        # the file also names these paths in prose.
        def tag_at(name: str) -> int:
            tag = f'<script src="js/{name}"></script>'
            self.assertIn(tag, self.html, f"{name} is not loaded")
            return self.html.index(tag)

        pos = tag_at('scene-regroup.js')
        for dep in ('crop-data.js', 'scene-grid.js'):
            self.assertLess(tag_at(dep), pos, f"{dep} must load first")

    def test_dialog_markup_exists_with_the_three_controls(self):
        self.assertIn('id="regroupDlg"', self.html)
        for control in ("regroupGroupWithin", "regroupSplitAfter", "regroupSimilarity"):
            self.assertIn(f'id="{control}"', self.html)

    def test_dialog_has_both_preview_columns(self):
        self.assertIn('id="regroupCurrentList"', self.html)
        self.assertIn('id="regroupProposedList"', self.html)

    def test_folder_actions_menu_offers_it(self):
        menu = self.grid.split("function _buildFolderActionsMenu", 1)[1].split("\n    async function", 1)[0]
        self.assertIn("Regroup Scenes", menu)
        self.assertIn("openRegroupDialog(folderPath)", menu)

    def test_apply_button_starts_disabled(self):
        # Nothing to apply until a preview says the grouping would change.
        self.assertRegex(self.html, r'id="regroupApplyBtn"[^>]*disabled')

    def test_module_self_check_lists_it(self):
        self.assertIn("'scene-regroup.js',   'openRegroupDialog'", self.html)


class TestRegroupFrontend(unittest.TestCase):
    """Behaviour the dialog must not lose."""

    def setUp(self):
        self.src = _read("js", "scene-regroup.js")

    def test_ships_only_the_columns_the_rules_need(self):
        # The in-memory rows carry crops_json / detection_scores blobs; sending
        # whole rows would bloat the bridge payload by an order of magnitude.
        decl = self.src.split("const REGROUP_COLUMNS = [", 1)[1].split("];", 1)[0]
        listed = set(re.findall(r"'([a-z_]+)'", decl))
        self.assertEqual(
            listed,
            {"filename", "capture_time", "orientation",
             "feature_similarity", "feature_confidence",
             "color_similarity", "color_confidence", "scene_count"},
        )

    def test_payload_is_built_from_that_column_list_only(self):
        body = self.src.split("const payload = folderRows.map", 1)[1].split("});", 1)[0]
        self.assertIn("for (const col of REGROUP_COLUMNS)", body)
        # A spread of the whole row would silently reintroduce the blobs.
        self.assertNotIn("...r", body)

    def test_preview_is_debounced_and_ordered(self):
        # Dragging a slider fires many previews; a slow early response must not
        # land on top of a newer one.
        self.assertIn("_regroupPreviewSeq", self.src)
        self.assertIn("if (seq !== _regroupPreviewSeq) return;", self.src)
        self.assertIn("setTimeout", self.src)

    def test_scenedata_is_migrated_before_rows_move(self):
        body = self.src.split("async function applyRegroup", 1)[1]
        migrate_at = body.index("_regroupMigrateScenedata(")
        move_at = body.index("r.scene_count = newScene")
        self.assertLess(
            migrate_at, move_at,
            "migration needs both the old and the new membership, so it must "
            "run before scene_count is overwritten",
        )

    def test_migration_resets_a_scene_built_from_several_old_ones(self):
        body = self.src.split("function _regroupMigrateScenedata", 1)[1].split(
            "\n    async function", 1)[0]
        self.assertIn("contributors.size === 1", body)
        self.assertIn("finalized: false", body)

    def test_migration_deep_copies_the_source_entry(self):
        # Two halves of a split both inherit from one entry; sharing the object
        # would make pruning one prune the other.
        body = self.src.split("function _regroupMigrateScenedata", 1)[1].split(
            "\n    async function", 1)[0]
        self.assertIn("JSON.parse(JSON.stringify(source))", body)

    def test_migration_prunes_tags_on_a_strict_subset(self):
        body = self.src.split("function _regroupMigrateScenedata", 1)[1].split(
            "\n    async function", 1)[0]
        self.assertIn("_pruneFinalizedSceneTagsAfterRemoval", body)

    def test_apply_marks_dirty_so_the_autosave_persists_it(self):
        body = self.src.split("async function applyRegroup", 1)[1]
        self.assertIn("markDirty(folderPath)", body)

    def test_apply_requests_the_assignment(self):
        body = self.src.split("async function applyRegroup", 1)[1]
        self.assertIn("regroup_scenes_preview(folderPath, opts, true)", body)

    def test_closing_releases_the_cached_working_set(self):
        self.assertIn("regroup_scenes_release", self.src)

    def test_scene_list_render_is_bounded(self):
        # A folder can regroup into thousands of scenes and this reruns on
        # every slider tick.
        body = self.src.split("function _regroupRenderSceneList", 1)[1].split(
            "\n    function ", 1)[0]
        self.assertIn("LIMIT", body)
        self.assertIn("slice(0, LIMIT)", body)

    def test_filenames_are_escaped(self):
        body = self.src.split("function _regroupRenderSceneList", 1)[1].split(
            "\n    function ", 1)[0]
        self.assertIn("escapeHtml(s.first_filename", body)
        self.assertIn("escapeHtml(s.last_filename", body)

    def test_opens_on_the_settings_the_folder_was_analyzed_with(self):
        # So the first preview reproduces the current grouping and any
        # difference the user then sees is one they asked for.
        self.assertIn("analyzed_group_within", self.src)
        self.assertIn("analyzed_split_after", self.src)

    def test_tells_the_user_how_many_pairs_the_similarity_reaches(self):
        # Inside a burst the pipeline never scored the pair, so the similarity
        # control genuinely cannot act there. Saying so beats a silent no-op.
        self.assertIn("regroupScoredNote", self.src)
        self.assertIn("scored_pairs", self.src)


if __name__ == "__main__":
    unittest.main()
