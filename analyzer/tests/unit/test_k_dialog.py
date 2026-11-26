"""js/k-dialog.js: message lookup and the styled confirm / notice flow, run under Node
with a tiny DOM stub (skipped without Node)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

K_DIALOG_JS = str(Path(__file__).resolve().parents[2] / "js" / "k-dialog.js")
NODE = shutil.which("node")

pytestmark = [pytest.mark.unit, pytest.mark.skipif(NODE is None, reason="node not installed")]

_HARNESS = r"""
const vm = require('vm'), fs = require('fs');
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.listeners = {}; this.textContent = ''; this.className = ''; this.attrs = {}; }
  append(...c) { this.children.push(...c); }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  get firstElementChild() { return this.children[0]; }
  focus() { state.focused = this; }
  remove() { state.removed = true; }
  showModal() { state.open = this; }
  close(v) { this.returnValue = v || ''; (this.listeners.close || []).forEach(f => f()); }
  click() { (this.listeners.click || []).forEach(f => f()); }
}
const state = {};
const document = { createElement: t => new El(t), body: new El('body') };
const ctx = vm.createContext({ window: {}, document });
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const D = ctx.window.KestrelDialog;
const buttons = () => state.open.children[1].children;
const find = (n, cls) => n.children.find(c => c.className === cls);
(async () => {
  const out = {};
  out.filled = D.text('resetAllDecisions', { name: '<b>Marsh</b>' });
  out.plain = D.text('upgradeFoldersSkipped', { count: 2 });
  try { D.text('nope'); } catch (e) { out.unknown = e.message; }

  let p = D.confirm('resetAllDecisions', { name: 'Marsh' });
  out.labels = buttons().map(b => [b.textContent, b.className]);
  out.focusedCancel = state.focused === buttons()[0];
  out.title = find(state.open.children[0], 'k-dialog-title').textContent;
  buttons()[1].click();
  out.confirmed = await p;

  p = D.confirm('revertChanges');
  state.open.close();            // Escape closes with an empty returnValue
  out.escaped = await p;

  p = D.notice('mergeDifferentFolders');
  out.noticeButtons = buttons().map(b => b.textContent);
  buttons()[0].click();
  out.noticeResolved = (await p) === undefined;
  out.removed = !!state.removed;
  process.stdout.write(JSON.stringify(out));
})();
"""


def test_confirm_and_notice():
    res = subprocess.run([NODE, "-e", _HARNESS, K_DIALOG_JS], capture_output=True, text=True,
                         check=True, encoding="utf-8")
    out = json.loads(res.stdout)
    # Values are filled as plain text; the dialog sets them with textContent.
    assert "“<b>Marsh</b>”" in out["filled"]["body"]
    assert out["filled"]["danger"] is True
    assert out["plain"].strip().startswith("2 other folder")
    assert "unknown message" in out["unknown"]

    assert out["labels"] == [["Cancel", "k-btn--quiet"], ["Reset all", "k-btn--danger-solid"]]
    assert out["focusedCancel"] is True  # destructive: Enter never confirms
    assert out["title"] == "Reset all decisions?"
    assert out["confirmed"] is True
    assert out["escaped"] is False
    assert out["noticeButtons"] == ["OK"]
    assert out["noticeResolved"] is True
    assert out["removed"] is True


def test_every_message_has_a_title_and_body():
    res = subprocess.run([NODE, "-e", (
        "const vm=require('vm'),fs=require('fs');const ctx=vm.createContext({window:{}});"
        "vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),ctx);"
        "process.stdout.write(JSON.stringify(ctx.window.KestrelDialog.MESSAGES));"), K_DIALOG_JS],
        capture_output=True, text=True, check=True, encoding="utf-8")
    for key, msg in json.loads(res.stdout).items():
        if isinstance(msg, dict):
            assert msg.get("title") and msg.get("body"), key
