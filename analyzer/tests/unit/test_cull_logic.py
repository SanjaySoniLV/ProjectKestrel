"""Cull mode's starting picks and strip order (js/cull-logic.js), run under node."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.skipif(shutil.which("node") is None, reason="no node runtime")]

_LOGIC = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "js", "cull-logic.js")


def _run(body: str):
    script = textwrap.dedent(
        """
        const fs = require('fs');
        global.window = global;
        eval(fs.readFileSync(process.argv[1], 'utf8'));
        const L = window.KestrelCullLogic;
        const P = (key, q, extra = {}) => ({ key, q, manual: null, fav: false, time: NaN, ...extra });
        const plan = (...a) => { const r = L.planScene(...a); return { ...r, decisions: Object.fromEntries(r.decisions) }; };
        const out = (() => {
        """
    ) + textwrap.dedent(body) + "\n})();\nconsole.log(JSON.stringify(out));\n"
    res = subprocess.run([shutil.which("node"), "-e", script, _LOGIC], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


RULES = "{ threshold: 0.5, min_per_scene: 1, no_bird: 'reject' }"


def test_threshold_splits_the_pool_and_orders_the_strip():
    out = _run(f"""
        return plan([P('a', 0.3), P('b', 0.9), P('c', 0.6), P('d', null)], {RULES}, null, 'quality');
    """)
    assert out["decisions"] == {"b": "accept", "c": "accept", "a": "reject", "d": "reject"}
    assert out["order"] == ["b", "c", "a", "d"]
    assert out["divider"] == 2


def test_accept_at_least_tops_up_sharpest_first():
    out = _run(f"return plan([P('a', 0.2), P('b', 0.3)], {RULES}, null, 'quality');")
    assert out["decisions"] == {"b": "accept", "a": "reject"}


def test_user_decisions_are_kept_and_count_toward_the_minimum():
    out = _run(f"""
        return plan([P('a', 0.2, {{ manual: 'accept' }}), P('b', 0.3), P('c', 0.95, {{ manual: 'reject' }})],
                    {RULES}, null, 'quality');
    """)
    assert out["decisions"] == {"b": "reject"}
    assert out["order"] == ["a", "b", "c"]
    assert out["divider"] == 1


def test_favorites_always_start_accepted():
    out = _run(f"return plan([P('a', 0.9), P('b', 0.1, {{ fav: true }})], {RULES}, 0, 'quality');")
    assert out["decisions"] == {"b": "accept", "a": "reject"}


def test_cut_overrides_threshold_and_no_bird_rule_applies():
    out = _run("""
        return plan([P('a', 0.9), P('b', 0.8), P('c', 0.7), P('d', null)],
                    { threshold: 0.5, min_per_scene: 1, no_bird: 'accept' }, 1, 'quality');
    """)
    assert out["decisions"] == {"a": "accept", "b": "reject", "c": "reject", "d": "accept"}


def test_capture_order_has_no_divider():
    out = _run(f"return plan([P('b', 0.9, {{ time: 2 }}), P('a', 0.1, {{ time: 1 }})], {RULES}, null, 'time');")
    assert out["order"] == ["a", "b"] and out["divider"] is None


def test_next_threshold_and_snapping_use_star_edges():
    out = _run("""
        const e = { two: 0.15, three: 0.4, four: 0.6, five: 0.85 };
        return [L.nextThreshold(0.5, e), L.nextThreshold(0.9, e), L.snapToEdge(0.47, e), L.starFor(0.61, e),
                L.cutFromLeftKeys([P('a', 0.9), P('b', 0.5), P('c', 0.1)], ['a', 'b'])];
    """)
    assert out == [0.6, 0.95, 0.4, 4, 2]


def test_strip_layout_fits_three_rows_then_scrolls():
    out = _run("return [L.stripLayout(6, 900, 80, 140, 8), L.stripLayout(60, 900, 80, 140, 8)];")
    assert out[0]["rows"] == 1 and out[0]["scroll"] is False and out[0]["w"] == 140
    assert out[1]["scroll"] is True and out[1]["w"] == 80
