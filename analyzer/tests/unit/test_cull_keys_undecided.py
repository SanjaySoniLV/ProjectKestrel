"""Ctrl+C/Z/X must not cull, and explicit Undecided keeps a manual origin (WP-26).

The scene-dialog Z/X/C keys (and 7/8/9) write accept / empty / reject. Those
letters are also the browser's copy / undo / cut chords. The handler already
skips input fields and handles Ctrl+Shift+C before the switch; it must also
leave Ctrl/Cmd+Z, Ctrl/Cmd+X, and Ctrl/Cmd+C alone so preventDefault does not
eat the browser shortcut.

Explicit Undecided is the X key (and the filmstrip "none" toggle): culled is
empty and origin is manual.

Key resolution runs through js/keybindings.js under node.
"""

from __future__ import annotations

import json
import shutil
import subprocess
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


KEYBINDINGS = ANALYZER / "js" / "keybindings.js"
_NODE = shutil.which("node")

_RESOLVE = r"""
const fs = require('fs');
global.window = global;
global.document = { addEventListener() {}, dispatchEvent() {}, querySelectorAll: () => [] };
global.getSetting = (_k, d) => d;
eval(fs.readFileSync(process.argv[1], 'utf8'));
const out = {};
for (const [key, code] of [['z', 'KeyZ'], ['x', 'KeyX'], ['c', 'KeyC'], ['7', 'Digit7'], ['8', 'Digit8'], ['9', 'Digit9']]) {
  const hit = (mods) => (KestrelKeys.resolve('viewer', { key, code, ...mods }) || {}).id || null;
  out[key] = { plain: hit({}), ctrl: hit({ ctrlKey: true }), meta: hit({ metaKey: true }) };
}
console.log(JSON.stringify(out));
"""


def _viewer_keys() -> dict:
    res = subprocess.run([_NODE, "-e", _RESOLVE, str(KEYBINDINGS)], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_ctrl_shift_c_is_handled_before_the_key_bindings():
    handler = _fn_body(_read(SCENE_DIALOG), "function _sceneKeyHandler(e)")
    ctrl_shift_c = handler.find("ctrlShift && lowerKey === 'c'")
    resolve_at = handler.find("KestrelKeys.resolve('viewer', e)")
    assert ctrl_shift_c != -1
    assert resolve_at != -1
    assert ctrl_shift_c < resolve_at


@pytest.mark.skipif(_NODE is None, reason="no node runtime available")
def test_cull_letter_keys_yield_when_ctrl_or_meta_is_held():
    """Ctrl/Cmd+Z/X/C resolve to nothing, so the handler leaves the browser chord alone."""
    for key, hits in _viewer_keys().items():
        assert hits["ctrl"] is None, key
        assert hits["meta"] is None, key


@pytest.mark.skipif(_NODE is None, reason="no node runtime available")
def test_plain_zxc_still_map_to_accept_undecided_reject():
    keys = _viewer_keys()
    assert {k: keys[k]["plain"] for k in keys} == {
        "z": "accept", "x": "clear", "c": "reject", "7": "accept", "8": "clear", "9": "reject",
    }


def test_clearing_cull_is_a_manual_origin():
    """X / none is explicit Undecided, not 'never touched'."""
    body = _fn_body(_read(SCENE_DIALOG), "function setCullStatus(row, status)")
    assert "row.culled = status || '';" in body
    assert "row.culled_origin = 'manual';" in body
    assert "status ? 'manual' : ''" not in body
