"""Reviewed scene tags must reach the files the metadata writer touches.

A species correction is stored on the SCENE, as
``scenes[id].user_tags = {species, families, finalized: true}`` in
kestrel_scenedata.json. Nothing writes it back onto the per-image ``species`` /
``family`` columns of kestrel_database.csv. Every place the app *displays* a
reviewed scene substitutes the reviewed tags for the model's prediction (the
rule in ``js/scenes.js``), but both XMP payload builders read the row columns
directly -- so a user who fixed a misidentified bird and ticked Reviewed saw
the correction in Kestrel while their file's keywords and IPTC description
were written with the original prediction.

``js/reviewed-tags.js`` is the one resolver both builders now go through.

Two layers here, matching the repo's existing approach to frontend tests
(``tests/test_security_visualizer_js_xss.py``): source-level lints that always
run, plus behavioural tests driven through node when a runtime is available.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap

import pytest

pytestmark = pytest.mark.unit

_ANALYZER_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RESOLVER = os.path.join(_ANALYZER_DIR, "js", "reviewed-tags.js")
_CULLING_JS = os.path.join(_ANALYZER_DIR, "js", "culling.js")
_CULLING_HTML = os.path.join(_ANALYZER_DIR, "culling.html")
_VISUALIZER_HTML = os.path.join(_ANALYZER_DIR, "visualizer.html")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------------------
# Source-level lints (no JS runtime required)
# ---------------------------------------------------------------------------


def test_resolver_exists_and_is_dependency_free():
    src = _read(_RESOLVER)
    assert "KestrelReviewedTags" in src
    # It is loaded by a standalone page too, so it must not reach for any of
    # the main window's shared helpers. Comments are stripped first so prose
    # naming kestrel_scenedata.json does not read as a reference to the
    # main window's ``_scenedata`` state.
    code = re.sub(r"//[^\n]*", "", src)
    for symbol in ("parseNumber(", "_scenedata[", "escapeHtml(", "getSetting("):
        assert symbol not in code, f"resolver must stay self-contained: {symbol}"


@pytest.mark.parametrize("page", [_VISUALIZER_HTML, _CULLING_HTML])
def test_both_pages_load_the_resolver(page):
    assert 'src="js/reviewed-tags.js"' in _read(page), os.path.basename(page)


def test_xmp_payload_builders_do_not_read_the_row_columns_directly():
    """The regression guard: a payload built off ``r.species`` is the bug."""
    for path, marker in ((_CULLING_JS, "write_xmp_metadata"), (_CULLING_HTML, "xmpPayload")):
        src = _read(path)
        assert marker in src, f"{path} no longer builds an XMP payload?"
        assert "species: r.species" not in src, (
            f"{os.path.basename(path)} builds its XMP payload from the model's "
            f"per-image species instead of the reviewed scene tags"
        )
        assert "family: r.family" not in src, os.path.basename(path)
        assert "KestrelReviewedTags.resolveRowTags" in src, os.path.basename(path)


# ---------------------------------------------------------------------------
# Behavioural tests through node
# ---------------------------------------------------------------------------

_NODE = shutil.which("node")

_SCENES = {
    # Reviewed, one species: the reported case.
    "11": {"user_tags": {"species": ["Least Grebe"], "families": ["Podicipedidae"], "finalized": True}},
    # Reviewed, several species: which one applies to a given image is unknown.
    "12": {"user_tags": {"species": ["Least Grebe", "Cinnamon Teal"], "families": [], "finalized": True}},
    # Edited but not ticked Reviewed.
    "13": {"user_tags": {"species": ["Least Grebe"], "families": [], "finalized": False}},
}


def _resolve(row):
    """Run ``resolveRowTags`` for ``row`` against ``_SCENES`` under node."""
    script = textwrap.dedent(
        """
        const fs = require('fs');
        const g = {};
        // node -e drops the script itself from argv: argv[1] is the first
        // argument passed after the inline script.
        new Function('window', fs.readFileSync(process.argv[1], 'utf8'))(g);
        const out = g.KestrelReviewedTags.resolveRowTags(
            JSON.parse(process.argv[2]), JSON.parse(process.argv[3]));
        process.stdout.write(JSON.stringify(out));
        """
    )
    proc = subprocess.run(
        [_NODE, "-e", script, _RESOLVER, json.dumps(_SCENES), json.dumps(row)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


@pytest.mark.skipif(_NODE is None, reason="no node runtime available")
@pytest.mark.parametrize(
    "row,expected",
    [
        # The report: species corrected to Least Grebe, file still said the
        # model's original guess.
        (
            {"species": "Cinnamon Teal", "family": "Anatidae", "scene_count": "11"},
            {"species": "Least Grebe", "family": "Podicipedidae"},
        ),
        # Prediction already agrees with the review — keep it.
        (
            {"species": "Least Grebe", "family": "Podicipedidae", "scene_count": "11"},
            {"species": "Least Grebe", "family": "Podicipedidae"},
        ),
        # scene_count arrives as a number from some call sites.
        (
            {"species": "Cinnamon Teal", "family": "Anatidae", "scene_count": 11},
            {"species": "Least Grebe", "family": "Podicipedidae"},
        ),
        # Several reviewed species and the prediction is none of them: leave the
        # prediction alone rather than guess which image got which bird.
        (
            {"species": "Great Egret", "family": "", "scene_count": "12"},
            {"species": "Great Egret", "family": ""},
        ),
        # Several reviewed species and the prediction is one of them: keep it.
        (
            {"species": "Cinnamon Teal", "family": "", "scene_count": "12"},
            {"species": "Cinnamon Teal", "family": ""},
        ),
        # Not ticked Reviewed: nothing is asserted, so nothing is substituted.
        (
            {"species": "Cinnamon Teal", "family": "Anatidae", "scene_count": "13"},
            {"species": "Cinnamon Teal", "family": "Anatidae"},
        ),
        # Scene the scenedata has never heard of.
        (
            {"species": "Cinnamon Teal", "family": "Anatidae", "scene_count": "99"},
            {"species": "Cinnamon Teal", "family": "Anatidae"},
        ),
    ],
)
def test_resolve_row_tags(row, expected):
    assert _resolve(row) == expected
