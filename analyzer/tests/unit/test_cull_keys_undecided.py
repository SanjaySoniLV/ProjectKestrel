"""Ctrl+C/Z/X must not cull; auto-categorize must keep explicit Undecided (WP-26).

The scene-dialog Z/X/C keys (and 7/8/9) write accept / empty / reject. Those
letters are also the browser's copy / undo / cut chords. The handler already
skips input fields and handles Ctrl+Shift+C before the switch; it must also
leave Ctrl/Cmd+Z, Ctrl/Cmd+X, and Ctrl/Cmd+C alone so preventDefault does not
eat the browser shortcut.

Explicit Undecided is the X key (and the filmstrip "none" toggle): culled is
empty and origin is manual. ``applyAutoCategorize(true)`` already skips
``isProtectedCull``, which is origin-based, so that empty+manual row stays
off both assistant piles. Untouched rows (empty cull, empty origin) are still
backfilled on init.

No JS runner: source-level lints, same shape as ``test_culling_quality_cutoff.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ANALYZER = Path(__file__).resolve().parents[2]
SCENE_DIALOG = ANALYZER / "js" / "scene-dialog.js"
CULLING_HTML = ANALYZER / "culling.html"


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


def test_protected_cull_is_origin_only():
    """Empty+manual must be protected; do not require accept/reject."""
    html = _read(CULLING_HTML)
    body = _fn_body(html, "function isProtectedCull(row)")
    assert "origin === 'manual'" in body
    assert "origin === 'verified'" in body
    assert "hasAnyCull" not in body


def test_normalize_keeps_manual_origin_without_accept_or_reject():
    body = _fn_body(_read(CULLING_HTML), "function normalizeCullOrigin(row)")
    raw_ok = body.find("raw === 'manual'")
    status_fallback = body.find("if (status) return 'manual';")
    empty = body.find("return '';")
    assert raw_ok != -1
    assert status_fallback != -1
    assert empty != -1
    assert raw_ok < status_fallback < empty


def test_apply_auto_preserve_skips_protected_not_empty_without_origin():
    """Init still backfills untouched rows; explicit Undecided is origin=manual."""
    body = _fn_body(_read(CULLING_HTML), "function applyAutoCategorize(preserveManual = true)")
    write = body[body.find("// Write results"):]
    assert "if (preserveManual && isProtectedCull(r)) continue;" in write
    assert "!hasAnyCull(r)" not in write
    assert "setCullState(r, state, 'auto')" in write


def test_init_backfill_uses_the_preserve_path():
    body = _fn_body(_read(CULLING_HTML), "function initCullingState()")
    assert "applyAutoCategorize(true)" in body


def test_reset_still_overwrites_including_undecided():
    html = _read(CULLING_HTML)
    assert "applyAutoCategorize(false)" in html
    assert "el('#resetConfirmOk')" in html
