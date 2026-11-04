"""Scene viewer filmstrip order toggle (Best quality first / Capture order).

No JS runner: source-level lints, same shape as ``test_cull_keys_undecided.py``,
plus a real check of the settings sanitizer.

The order is applied to ``scene.images`` itself, so the arrow-key handler, the
filmstrip cards and the preview all share one visible order.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import settings_utils

ANALYZER = Path(__file__).resolve().parents[2]
SCENES_JS = (ANALYZER / "js" / "scenes.js").read_text(encoding="utf-8")
SCENE_DIALOG_JS = (ANALYZER / "js" / "scene-dialog.js").read_text(encoding="utf-8")
STATE_JS = (ANALYZER / "js" / "state.js").read_text(encoding="utf-8")
HTML = (ANALYZER / "visualizer.html").read_text(encoding="utf-8")

pytestmark = pytest.mark.unit


def test_setting_key_is_defined_once_and_used_by_the_dialog():
    assert "SCENE_FILMSTRIP_ORDER_KEY = 'scene_filmstrip_order'" in STATE_JS
    assert "mergeSetting(SCENE_FILMSTRIP_ORDER_KEY, order)" in SCENE_DIALOG_JS


def test_capture_order_uses_capture_time_then_filename():
    start = SCENES_JS.index("function sortSceneImages(")
    body = SCENES_JS[start:SCENES_JS.index("function aggregateScenes(")]
    assert "parseCaptureTimeMs(a.capture_time)" in body
    assert "localeCompare" in body  # filename fallback / tie-break
    assert "parseNumber(b.quality) - parseNumber(a.quality)" in body  # quality default


def test_scenes_are_built_in_the_saved_order():
    assert "images: sortSceneImages(arr, getSceneFilmstripOrder())" in SCENES_JS


def test_toggle_sorts_in_place_and_keeps_the_selected_image():
    start = SCENE_DIALOG_JS.index("async function _setFilmstripOrder(")
    body = SCENE_DIALOG_JS[start:SCENE_DIALOG_JS.index("async function _resortAndFocusSceneImage(")]
    assert "imgs.splice(0, imgs.length, ...sortSceneImages(imgs, order))" in body
    assert "imgs.indexOf(selected)" in body
    assert "renderFilmstrip(_currentScene)" in body


def test_toggle_buttons_exist_in_the_filmstrip_bar():
    bar = HTML[HTML.index('id="sceneFilmstripBar"'):HTML.index('class="scene-filmstrip-wrap"')]
    assert 'data-order="quality"' in bar and 'data-order="time"' in bar
    for ident in ("sceneQf", "sceneRejectUndecidedBtn", "splitSceneBtn"):
        assert f'id="{ident}"' in bar


def test_sanitizer_accepts_only_known_orders():
    ok = settings_utils._sanitize_settings_payload({"scene_filmstrip_order": "time"})
    assert ok["scene_filmstrip_order"] == "time"
    bad = settings_utils._sanitize_settings_payload({"scene_filmstrip_order": "zigzag"})
    assert bad["scene_filmstrip_order"] == "quality"
