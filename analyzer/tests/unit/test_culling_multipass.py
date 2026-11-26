"""The Culling Assistant can run more than once on the same folder.

It used to keep "Done Culling, Proceed to Review" disabled for as long as an
earlier reject move could be undone, because a second move would overwrite the
single backup Undo restored from. Every move is now a recorded pass
(``record_cull_pass``), Undo walks the passes back newest first
(``undo_last_cull_pass``), and the Undo button's tooltip lists each pass and
what can still be recovered from it (``get_cull_history``).

No JS runner: source-level lints, same shape as ``test_cull_keys_undecided.py``.
The bridge side is covered by ``test_cull_history.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

CULLING_HTML = Path(__file__).resolve().parents[2] / "culling.html"

pytestmark = pytest.mark.unit


def _src() -> str:
    return CULLING_HTML.read_text(encoding="utf-8")


def _fn_body(src: str, header: str) -> str:
    start = src.find(header)
    assert start != -1, f"missing {header!r}"
    candidates = [
        src.find(marker, start + len(header))
        for marker in ("\n    function ", "\n    async function ")
    ]
    nxt = min(i for i in candidates if i != -1)
    return src[start:nxt]


def test_done_button_is_never_locked_by_an_earlier_move():
    src = _src()
    assert "moveCompleted" not in src
    assert "setDoneLocked" not in src
    body = _fn_body(src, "function updateStats()")
    assert "el('#doneCullingBtn').disabled = false" in body


def test_move_records_a_pass_and_refreshes_history():
    body = _fn_body(_src(), "async function executeActions()")
    rec = body.find("record_cull_pass(rootPath, movedMains)")
    assert rec != -1
    assert body.find("move_rejects_to_folder") < rec
    assert body.find("await syncUndoStateFromDisk()", rec) != -1


def test_undo_prefers_the_pass_stack_and_keeps_a_legacy_fallback():
    body = _fn_body(_src(), "async function undoMove()")
    assert "undo_last_cull_pass(rootPath)" in body
    assert "undo_reject_move(rootPath, rejectFilenames)" in body
    assert "restore_kestrel_db_backup(rootPath)" in body
    assert "await syncUndoStateFromDisk()" in body


def test_history_tooltip_says_what_is_recoverable():
    body = _fn_body(_src(), "function renderUndoState()")
    assert "Culling history" in body
    assert "not recoverable" in body
    assert "recoverable" in body
    assert "newest pass first" in body


def test_sync_uses_history_when_the_bridge_has_it():
    body = _fn_body(_src(), "async function syncUndoStateFromDisk()")
    assert "get_cull_history(rootPath)" in body
    assert "get_reject_restore_state(rootPath)" in body
