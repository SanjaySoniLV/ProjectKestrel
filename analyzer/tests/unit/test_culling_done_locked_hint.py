"""A disabled "Done Culling" button must say why.

While an earlier reject move can still be undone (``moveCompleted``), the
Culling Assistant keeps "Done Culling, Proceed to Review" disabled. The only
explanation used to be a one-off status-bar line from ``syncUndoStateFromDisk``
that ``updateStats`` overwrote on the next accept/reject, so the button looked
broken. Every place that locks or unlocks the button now goes through
``setDoneLocked``, which sets a tooltip, and ``updateStats`` keeps the reason
in the status bar.

No JS runner: source-level lints, same shape as ``test_cull_keys_undecided.py``.
"""

from __future__ import annotations

import re
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


def test_set_done_locked_sets_disabled_and_tooltip():
    body = _fn_body(_src(), "function setDoneLocked(locked)")
    assert "doneBtn.disabled = locked" in body
    assert re.search(r"doneBtn\.title\s*=\s*locked\s*\?\s*DONE_LOCKED_HINT\s*:\s*''", body)


def test_hint_names_the_reason_and_the_folder():
    m = re.search(r"const DONE_LOCKED_HINT = '([^']+)'", _src())
    assert m, "DONE_LOCKED_HINT missing"
    assert "undo" in m.group(1).lower()
    assert "_KESTREL_Rejects" in m.group(1)


def test_update_stats_keeps_the_reason_in_the_status_bar():
    body = _fn_body(_src(), "function updateStats()")
    assert "moveCompleted ?" in body and "DONE_LOCKED_HINT" in body
    assert "setDoneLocked(moveCompleted)" in body


def test_move_lock_paths_use_the_helper():
    src = _src()
    sync = _fn_body(src, "async function syncUndoStateFromDisk()")
    assert "setDoneLocked(true)" in sync
    assert "doneBtn.disabled = true" not in sync
    undo = _fn_body(src, "async function undoMove()")
    assert "setDoneLocked(false)" in undo
