"""Quality-feedback sort dialog: list + large preview layout.

No JS runner: source-level lints, same shape as ``test_scene_filmstrip_order.py``.
They pin the parts of ``js/quality-feedback.js`` that must not drift: the
retired enlarge button stays gone, the ranking list has a clear sharpest and
blurriest end, a hover-driven preview exists, and the submit payload keeps
exactly the fields ``api_bridge.submit_quality_feedback`` reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ANALYZER = Path(__file__).resolve().parents[2]
QF_JS = (ANALYZER / "js" / "quality-feedback.js").read_text(encoding="utf-8")
QF_CSS = (ANALYZER / "css" / "dialogs" / "quality-feedback.css").read_text(encoding="utf-8")
HTML = (ANALYZER / "visualizer.html").read_text(encoding="utf-8")

pytestmark = pytest.mark.unit


def _sort_renderer() -> str:
    start = QF_JS.index("function _qfRenderSort(")
    return QF_JS[start:QF_JS.index("function _qfFinishSort(")]


def test_enlarge_button_is_gone():
    for src in (QF_JS, QF_CSS, HTML):
        assert "qf-zoom" not in src
        assert "_qfZoom" not in src
    assert "Enlarge" not in QF_JS


def test_sort_step_has_a_sharpest_to_blurriest_scale():
    body = _sort_renderer()
    assert "Sharpest" in body and "Blurriest" in body
    # The rank numbers are positions in the list, sharpest = 1.
    assert "String(pos + 1)" in body


def test_hover_focus_and_click_drive_the_large_preview():
    body = _sort_renderer()
    for event in ("mouseenter", "focus", "click"):
        assert f"addEventListener('{event}', () => _qfPreview(it.index))" in body
    assert "qfPreviewImg" in body
    # The preview reuses the frame data already sent to the dialog; it must not
    # ask Python for file paths or names.
    start = QF_JS.index("function _qfPreview(")
    preview = QF_JS[start:QF_JS.index("function _qfRenderSort(")]
    assert "pywebview" not in preview


def test_drag_and_keyboard_reorder_are_kept():
    body = _sort_renderer()
    for event in ("dragstart", "dragover", "drop", "keydown"):
        assert f"'{event}'" in body
    assert "ArrowUp" in body and "ArrowDown" in body


def test_submit_payload_fields_are_unchanged():
    start = QF_JS.index("async function _qfSubmit(")
    body = QF_JS[start:QF_JS.index("window.onQualityFeedbackResult = ", start)]
    block = re.search(r"const payload = \{(.*?)\};", body, re.S).group(1)
    keys = re.findall(r"^\s*(\w+):", block, re.M)
    assert keys == ["token", "ranking", "thumbs", "duration_ms", "consent"]
    # The ranking is still one single-index group per photo, in list order.
    assert "_qf.items.map(it => [it.index])" in QF_JS


def test_sort_dialog_takes_most_of_the_window_but_shrinks_to_fit():
    sort_rule = re.search(r'qf-dlg\[data-step="sort"\] \{(.*?)\}', QF_CSS, re.S).group(1)
    assert "90vw" in sort_rule and "90vh" in sort_rule
    assert "calc(100vw - 32px)" in sort_rule and "calc(100vh - 32px)" in sort_rule
    # A small window stacks the preview above the ranking strip.
    assert "@media (max-width: 760px)" in QF_CSS
