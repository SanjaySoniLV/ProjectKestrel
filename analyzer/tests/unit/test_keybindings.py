"""Keyboard bindings (js/keybindings.js) and their settings sanitizer.

The JS module runs under node with a small stub for the settings helpers,
matching the repo's other frontend tests.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap

import pytest

import settings_utils

pytestmark = pytest.mark.unit

_ANALYZER_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_KEYS = os.path.join(_ANALYZER_DIR, "js", "keybindings.js")
_NODE = shutil.which("node")

_PRELUDE = textwrap.dedent(
    """
    const fs = require('fs');
    global.window = global;
    global.document = { addEventListener() {}, dispatchEvent() {}, querySelectorAll: () => [] };
    global.CustomEvent = class { constructor(t) { this.type = t; } };
    global.__settings = { keybindings: JSON.parse(process.argv[2] || '{}') };
    global.getSetting = (k, d) => (k in __settings ? __settings[k] : d);
    global.mergeSetting = (k, v) => { __settings[k] = v; };
    eval(fs.readFileSync(process.argv[1], 'utf8'));
    const K = window.KestrelKeys;
    const ev = (key, code, mods = {}) => ({ key, code, ...mods });
    const out = {};
    """
)


def _run(body: str, overrides: dict | None = None) -> dict:
    script = _PRELUDE + textwrap.dedent(body) + "\nconsole.log(JSON.stringify(out));"
    res = subprocess.run(
        [_NODE, "-e", script, _KEYS, json.dumps(overrides or {})],
        capture_output=True, text=True, timeout=30,
    )
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


needs_node = pytest.mark.skipif(_NODE is None, reason="no node runtime available")


@needs_node
def test_key_ids_are_stable_under_shift_and_split_out_the_numpad():
    out = _run(
        """
        out.z = K.keyIds(ev('Z', 'KeyZ', { shiftKey: true }));
        out.digit = K.keyIds(ev('!', 'Digit1', { shiftKey: true }));
        out.num7 = K.keyIds(ev('7', 'Numpad7'));
        out.numHome = K.keyIds(ev('Home', 'Numpad7'));
        out.space = K.keyIds(ev(' ', 'Space'));
        out.comma = K.keyIds(ev('<', 'Comma', { shiftKey: true }));
        out.shift = K.keyIds(ev('Shift', 'ShiftLeft'));
        """
    )
    assert out["z"] == ["z"]
    assert out["digit"] == ["1"]
    assert out["num7"] == ["Num7", "7"]
    assert out["numHome"] == ["Num7", "Home"]
    assert out["space"] == ["Space"]
    assert out["comma"] == [","]
    assert out["shift"] == []


@needs_node
def test_defaults_resolve_per_screen_and_ctrl_only_reaches_navigation():
    out = _run(
        """
        const r = (ctx, e) => { const h = K.resolve(ctx, e); return h && h.id; };
        out.spaceCull = r('cull', ev(' ', 'Space'));
        out.spaceViewer = r('viewer', ev(' ', 'Space'));
        out.num7 = r('viewer', ev('7', 'Numpad7'));
        out.ctrlZ = r('viewer', ev('z', 'KeyZ', { ctrlKey: true }));
        out.ctrlRight = r('cull', ev('ArrowRight', 'ArrowRight', { ctrlKey: true }));
        out.shiftZ = K.resolve('cull', ev('Z', 'KeyZ', { shiftKey: true }));
        out.alt = r('cull', ev('z', 'KeyZ', { altKey: true }));
        """
    )
    assert out["spaceCull"] == "flip"
    assert out["spaceViewer"] == "openEditor"
    assert out["num7"] == "accept"
    assert out["ctrlZ"] is None
    assert out["ctrlRight"] == "nextPhoto"
    assert out["shiftZ"] == {"id": "accept", "shift": True, "ctrl": False}
    assert out["alt"] is None


@needs_node
def test_user_keys_replace_actions_but_only_add_to_navigation():
    out = _run(
        """
        const r = (ctx, e) => { const h = K.resolve(ctx, e); return h && h.id; };
        out.m = r('cull', ev('m', 'KeyM'));
        out.z = r('cull', ev('z', 'KeyZ'));
        out.j = r('viewer', ev('j', 'KeyJ'));
        out.left = r('viewer', ev('ArrowLeft', 'ArrowLeft'));
        out.backspace = r('cull', ev('Backspace', 'Backspace'));
        out.esc = r('cull', ev('Escape', 'Escape'));
        """,
        {"accept": ["m"], "prevPhoto": ["j"], "close": ["Backspace"]},
    )
    assert out["m"] == "accept"
    assert out["z"] is None
    assert out["j"] == "prevPhoto"
    assert out["left"] == "prevPhoto"
    assert out["backspace"] == "close"
    assert out["esc"] == "close"


@needs_node
def test_conflicts_only_count_where_both_actions_work():
    out = _run(
        """
        out.zForFavorite = K.conflict('favorite', 'z');
        out.spaceForEditor = K.conflict('openEditor', 'Space');
        out.leftForAccept = K.conflict('accept', 'ArrowLeft');
        out.free = K.conflict('accept', 'q');
        """
    )
    assert out["zForFavorite"] == {"id": "accept", "fixed": False}
    # Space flips in Cull mode and opens the editor in the viewer: no clash.
    assert out["spaceForEditor"] is None
    assert out["leftForAccept"] == {"id": "prevPhoto", "fixed": True}
    assert out["free"] is None


@needs_node
def test_every_layout_is_free_of_conflicts_and_recognised():
    out = _run(
        """
        out.layouts = {};
        for (const p of K.PRESETS) {
          const ov = K.clean(p.overrides);
          const clashes = [];
          for (const ctx of ['cull', 'viewer']) {
            const seen = new Map();
            for (const a of K.ACTIONS.filter(x => x.ctx.includes(ctx))) {
              for (const k of K.keysFor(a.id, ov)) {
                if (seen.has(k)) clashes.push(`${ctx}:${k}:${seen.get(k)}/${a.id}`);
                seen.set(k, a.id);
              }
            }
          }
          out.layouts[p.id] = { clashes, recognised: K.presetOf(ov) };
        }
        """
    )
    for pid, res in out["layouts"].items():
        assert res["clashes"] == [], pid
        assert res["recognised"] == pid


@needs_node
def test_save_keeps_only_changes_from_the_defaults():
    out = _run(
        """
        K.save({ accept: ['z', '7'], reject: ['q'], bogus: ['x'], favorite: ['a', 'b', 'c'] });
        out.saved = __settings.keybindings;
        """
    )
    assert out["saved"] == {"reject": ["q"], "favorite": ["a", "b"]}


@needs_node
def test_settings_allowlist_matches_the_js_actions():
    out = _run("out.ids = K.ACTIONS.map(a => a.id);")
    assert set(out["ids"]) == settings_utils._ALLOWED_KEYBINDING_ACTIONS


def test_sanitizer_drops_unknown_actions_and_bad_keys():
    out = settings_utils._sanitize_settings_payload(
        {"keybindings": {"accept": ["m", "Num7", "x"], "bogus": ["a"], "close": ["\x01", "Backspace"], "flip": "a"}}
    )
    assert out["keybindings"] == {"accept": ["m", "Num7"], "close": ["Backspace"]}
    assert settings_utils._sanitize_settings_payload({"keybindings": "nope"})["keybindings"] == {}
    assert all(re.match(r"^\S{1,16}$", k) for keys in out["keybindings"].values() for k in keys)
