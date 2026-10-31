"""Search-box fast path for species/family names (js/smart-search.js).

On a large library a plain species query ("purple gallinule") could take tens
of seconds: while the name was being typed, the debounced input classified the
prefix ("purple") as a smart search and started a ``smart_search`` call per
folder over the whole library, ahead of the full name. Now a query that is a
loaded species/family tag filters with no bridge call, and a query that is
still the start of one shows the instant tag filter and holds the classifier
until typing settles.

Driven through node with the DOM and bridge stubbed, matching the repo's other
frontend behavioural tests; skipped when no node runtime is available.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap

import pytest

pytestmark = pytest.mark.unit

_ANALYZER_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SMART_JS = os.path.join(_ANALYZER_DIR, "js", "smart-search.js")
_NODE = shutil.which("node")

_HARNESS = textwrap.dedent(r"""
    const fs = require('fs'), vm = require('vm');
    const src = fs.readFileSync(process.argv[2], 'utf8');
    const input = { value: '', addEventListener() {}, closest() { return null; }, focus() {} };
    const calls = { classify: [], smart: [], renders: 0 };
    const ctx = {
      console, setTimeout, Promise, Map, Set, Array, String, Number, Math, JSON, Object,
      requestAnimationFrame: () => 0,
      document: { activeElement: null, createElement: () => ({ appendChild() {}, classList: { add() {}, remove() {} }, addEventListener() {} }), createTextNode: () => ({}) },
      el: (sel) => (sel === '#search' ? input : null),
      rows: [{ __rootPath: 'A', filename: 'x' }], rootPath: 'A',
      renderScenes: async () => { calls.renders++; },
    };
    ctx.window = { pywebview: { api: {
      classify_search_query: async (q) => { calls.classify.push(q); return { success: true, kind: /gallinule/i.test(q) ? 'filter' : 'semantic', labels: [] }; },
      smart_search: async (rp, q) => { calls.smart.push(q); return { success: true, images: [], chips: [] }; },
    } } };
    vm.createContext(ctx);
    vm.runInContext(src + '\n;this.__api = { smartSearchOnInput, smartSearchRememberTags, smartSearchFilterLabels, smartSearchOwnsQuery };', ctx);
    const A = ctx.__api;
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    const reset = () => { calls.classify = []; calls.smart = []; calls.renders = 0; };
    (async () => {
      const out = {};
      A.smartSearchRememberTags([{ species: ['Purple Gallinule', 'Song Sparrow'], families: ['Rails'] }]);
      input.value = 'Purple  Gallinule'; await A.smartSearchOnInput();
      out.exact = { classify: calls.classify, smart: calls.smart, renders: calls.renders,
                    labels: A.smartSearchFilterLabels('Purple  Gallinule') };
      reset();
      input.value = 'purple'; const pending = A.smartSearchOnInput();
      await sleep(50);
      out.prefixNow = { renders: calls.renders, classify: calls.classify.length, owns: A.smartSearchOwnsQuery() };
      input.value = 'purple gallinule'; await A.smartSearchOnInput(); await pending; await sleep(1100);
      out.typedThrough = { classify: calls.classify, smart: calls.smart };
      reset();
      input.value = 'purple'; await A.smartSearchOnInput();
      out.settled = { classify: calls.classify, smart: calls.smart };
      reset();
      input.value = 'bird in flight'; await A.smartSearchOnInput();
      out.semantic = { classify: calls.classify, smart: calls.smart };
      console.log(JSON.stringify(out));
    })().catch(e => { console.error(e); process.exit(1); });
""")


@pytest.fixture(scope="module")
def run(tmp_path_factory):
    if _NODE is None:
        pytest.skip("no node runtime available")
    harness = tmp_path_factory.mktemp("smartjs") / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run([_NODE, str(harness), _SMART_JS], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


def test_exact_tag_filters_without_a_bridge_call(run):
    assert run["exact"]["classify"] == [] and run["exact"]["smart"] == []
    assert run["exact"]["renders"] == 1
    assert run["exact"]["labels"] == ["purple gallinule"]


def test_tag_prefix_shows_the_filter_at_once(run):
    assert run["prefixNow"] == {"renders": 1, "classify": 0, "owns": False}


def test_typing_through_a_prefix_never_starts_a_smart_search(run):
    """The regression: 'purple' used to queue a whole-library search ahead of the full name."""
    assert run["typedThrough"] == {"classify": [], "smart": []}


def test_a_settled_prefix_still_reaches_smart_search(run):
    assert run["settled"] == {"classify": ["purple"], "smart": ["purple"]}


def test_other_text_goes_straight_to_the_classifier(run):
    assert run["semantic"] == {"classify": ["bird in flight"], "smart": ["bird in flight"]}
