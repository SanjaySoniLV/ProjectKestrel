"""js/util.js: the shared escaping and formatting helpers, run under Node."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

UTIL_JS = str(Path(__file__).resolve().parents[2] / "js" / "util.js")
NODE = shutil.which("node")

pytestmark = [pytest.mark.unit, pytest.mark.skipif(NODE is None, reason="node not installed")]

_HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const ctx = vm.createContext({ window: {} });
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), ctx);
const U = ctx.window.KestrelUtil;
const calls = JSON.parse(process.argv[2]);
process.stdout.write(JSON.stringify(calls.map(([fn, arg]) => U[fn](arg))));
"""


def _run(calls):
    out = subprocess.run([NODE, "-e", _HARNESS, UTIL_JS, json.dumps(calls)],
                         capture_output=True, text=True, check=True, encoding="utf-8")
    return json.loads(out.stdout)


def test_escape_html_covers_content_and_attributes():
    got = _run([["escapeHtml", "<a href=\"x\" onclick='y'>&</a>"], ["escapeHtml", None], ["escapeHtml", 42]])
    assert got == ["&lt;a href=&quot;x&quot; onclick=&#39;y&#39;&gt;&amp;&lt;/a&gt;", "", "42"]


@pytest.mark.parametrize("secs,want", [
    (0, "0s"), (38.4, "38s"), (60, "1m"), (252, "4m 12s"), (3600, "1h"), (3900, "1h 5m"),
    (-1, "—"), (None, "—"),
])
def test_format_duration(secs, want):
    assert _run([["formatDuration", secs]]) == [want]


@pytest.mark.parametrize("n,want", [
    (0, "0 B"), (512, "512 B"), (1536, "1.5 KB"), (3.4 * 1024 ** 2, "3.4 MB"),
    (150 * 1024 ** 3, "150 GB"), ("bad", "0 B"),
])
def test_format_bytes(n, want):
    assert _run([["formatBytes", n]]) == [want]
