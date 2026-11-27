"""Ctrl+C/Z/X must not cull, and explicit Undecided keeps a manual origin (WP-26).

The scene-dialog Z/X/C keys (and 7/8/9) write accept / empty / reject. Those
letters are also the browser's copy / undo / cut chords. The handler already
skips input fields and handles Ctrl+Shift+C before the switch; it must also
leave Ctrl/Cmd+Z, Ctrl/Cmd+X, and Ctrl/Cmd+C alone so preventDefault does not
eat the browser shortcut.

Explicit Undecided is the X key (and the filmstrip "none" toggle): culled is
empty and origin is manual.

No JS runner: source-level lints.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ANALYZER = Path(__file__).resolve().parents[2]
SCENE_DIALOG = ANALYZER / "js" / "scene-dialog.js"


pytestmark = pytest.mark.unit


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _fn_body(src: str, header: str) -> str:
    start = src.find(header)
    assert start != -1, f"missing {header!r}"
    candidates = [
        src.find(marker, start + len(header))
        for marker in ("\n    function ", "\n    async function ")
    ]
    nxt = min(i for i in candidates if i != -1)
    return src[start:nxt]


def _cull_key_cases(src: str) -> str:
    start = src.find("// Cull decisions.")
    assert start != -1, "cull-decision comment missing"
    end = src.find("case '1':", start)
    assert end != -1, "rating-key cases missing after cull keys"
    return src[start:end]


def test_ctrl_shift_c_is_handled_before_the_cull_reject_case():
    src = _read(SCENE_DIALOG)
    handler = _fn_body(src, "function _sceneKeyHandler(e)")
    ctrl_shift_c = handler.find("ctrlShift && lowerKey === 'c'")
    switch_at = handler.find("switch (e.key)")
    cull_c = handler.find("case 'c':")
    assert ctrl_shift_c != -1
    assert switch_at != -1
    assert cull_c != -1
    assert ctrl_shift_c < switch_at < cull_c


def test_cull_letter_keys_yield_when_ctrl_or_meta_is_held():
    """Ctrl/Cmd+Z/X/C must not preventDefault or call setCullStatus."""
    block = _cull_key_cases(_read(SCENE_DIALOG))
    groups = [
        block[block.find("case 'z':"):block.find("case 'x':")],
        block[block.find("case 'x':"):block.find("case 'c':")],
        block[block.find("case 'c':"):],
    ]
    assert all(g.strip() for g in groups)
    for group in groups:
        guard = group.find("if (hasSceneModifier) break;")
        prevent = group.find("e.preventDefault();")
        set_cull = group.find("setCullStatus(")
        assert guard != -1, group
        assert prevent != -1, group
        assert set_cull != -1, group
        assert guard < prevent < set_cull


def test_plain_zxc_still_map_to_accept_undecided_reject():
    block = _cull_key_cases(_read(SCENE_DIALOG))
    assert "setCullStatus(images[currentImageIndex], 'accept')" in block
    assert "setCullStatus(images[currentImageIndex], '')" in block
    assert "setCullStatus(images[currentImageIndex], 'reject')" in block


def test_clearing_cull_is_a_manual_origin():
    """X / none is explicit Undecided, not 'never touched'."""
    body = _fn_body(_read(SCENE_DIALOG), "function setCullStatus(row, status)")
    assert "row.culled = status || '';" in body
    assert "row.culled_origin = 'manual';" in body
    assert "status ? 'manual' : ''" not in body
