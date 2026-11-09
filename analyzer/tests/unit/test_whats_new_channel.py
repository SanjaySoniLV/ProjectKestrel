"""What's New leaves out the Cloud Compute pricing bullet on the Mac App Store build,
which hides in-app prices, and shows it on every other build. Runs the real
``welcome.js`` in a Node ``vm`` with a stubbed page and bridge (skipped without Node).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ANALYZER = Path(__file__).resolve().parents[2]
WELCOME_JS = str(ANALYZER / "js" / "welcome.js")

pytestmark = pytest.mark.unit

NODE = shutil.which("node")

_HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const [path, channel] = process.argv.slice(1);
const banner = { innerHTML: '', classList: { remove() {}, add() {} }, querySelector: () => null };
const ctx = vm.createContext({
  console,
  hasPywebviewApi: true,
  window: { pywebview: { api: {
    get_settings: async () => ({ success: true, settings: {} }),
    get_dist_channel: async () => ({ success: true, channel }),
  } } },
  document: { getElementById: (id) => (id === 'welcomeWhatsNew' ? banner : null) },
});
ctx.window.document = ctx.document;
// The sanitizer needs a real DOM; this test only checks which bullets are included.
const src = fs.readFileSync(path, 'utf8').replace('var sr = sanitizeRichText;', 'var sr = function (h) { return String(h); };');
if (!src.includes('var sr = function')) throw new Error('sanitizer hook not found');
try { vm.runInContext(src, ctx); } catch (_) { /* later page widgets need more DOM */ }
setTimeout(() => process.stdout.write(JSON.stringify({ html: banner.innerHTML })), 50);
"""

PRICING = "timing and pricing</b> options are now available in the app"


def _render(channel):
    out = subprocess.run([NODE, "-e", _HARNESS, WELCOME_JS, channel],
                         capture_output=True, text=True, check=True, encoding="utf-8")
    html = json.loads(out.stdout)["html"]
    assert "analysis version 3.0" in html  # the note rendered
    return html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_pricing_bullet_shown_on_direct_builds():
    assert PRICING in _render("direct")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_pricing_bullet_hidden_on_the_mac_app_store_build():
    html = _render("appstore")
    assert PRICING not in html
    assert "[object Object]" not in html
