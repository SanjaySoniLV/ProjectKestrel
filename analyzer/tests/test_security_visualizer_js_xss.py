r"""Static regression tests for FINDING-01: stored DOM-XSS via sceneName.

These tests are deliberately written as source-level lints against
``analyzer/js/*.js`` (the split-out frontend modules, previously a single
``visualizer.js``) and ``analyzer/culling.html`` (inline assistant script)
so the vulnerable pattern cannot silently re-appear. They don't require a
JS runtime.

What is forbidden
-----------------
1. ``decodeEntities(escapeHtml(...))`` anywhere — the original bug combined
   these two in series, which *undoes* the escape immediately before an
   ``innerHTML`` assignment.
2. ``X.innerHTML = \`...\\${...scene_name...}...\``` — user-controlled scene
   names interpolated into ``.innerHTML`` template literals without an
   explicit escape.  After the fix, the sceneName site must use ``textContent``
   or explicit DOM construction. Concatenated template literals on the same
   assignment are scanned too (``culling.html`` builds scene labels that way).

What remains permitted
----------------------
* Building text nodes via ``document.createElement`` + ``textContent``.
* Using ``escapeHtml`` on its own (without a later ``decodeEntities``).

Run with::

    cd analyzer
    python -m unittest tests.test_security_visualizer_js_xss
"""

from __future__ import annotations

import glob
import os
import re
import unittest

import pytest


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ANALYZER_DIR = os.path.dirname(_THIS_DIR)

pytestmark = pytest.mark.unit


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


_INNERHTML_ASSIGN = re.compile(r"\.innerHTML\s*=\s*")


def _collect_sources() -> list[tuple[str, str]]:
    sources: list[tuple[str, str]] = []
    js_dir = os.path.join(_ANALYZER_DIR, "js")
    for path in sorted(glob.glob(os.path.join(js_dir, "*.js"))):
        sources.append((os.path.join("js", os.path.basename(path)), _read(path)))
    culling = os.path.join(_ANALYZER_DIR, "culling.html")
    sources.append(("culling.html", _read(culling)))
    return sources


def _rhs_end(source: str, start: int) -> int:
    """Index of the statement semicolon, skipping ``;`` inside quotes or templates.

    A naive ``.*?``-to-semicolon cut stops at inline CSS such as
    ``style="color:red;font-size:11px"`` and would miss a later
    ``${scene.sceneName}`` on the same assignment.
    """
    quote: str | None = None
    escape = False
    i = start
    n = len(source)
    while i < n:
        c = source[i]
        if escape:
            escape = False
            i += 1
            continue
        if quote is not None:
            if c == "\\":
                escape = True
            elif c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"`":
            quote = c
            i += 1
            continue
        if c == ";":
            return i
        i += 1
    return n


def _iter_innerhtml_rhs(source: str):
    for m in _INNERHTML_ASSIGN.finditer(source):
        start = m.end()
        end = _rhs_end(source, start)
        yield m.start(), source[start:end]


def _scene_name_innerhtml_offenders(
    sources: list[tuple[str, str]],
) -> list[tuple[str, int, str]]:
    offenders: list[tuple[str, int, str]] = []
    for name, source in sources:
        for assign_at, rhs in _iter_innerhtml_rhs(source):
            if "sceneName" not in rhs and "scene_name" not in rhs:
                continue
            for tmpl in re.finditer(r"`([^`]*)`", rhs):
                body = tmpl.group(1)
                if "sceneName" not in body and "scene_name" not in body:
                    continue
                for expr_match in re.finditer(r"\$\{([^{}]+)\}", body):
                    expr = expr_match.group(1)
                    if "sceneName" not in expr and "scene_name" not in expr:
                        continue
                    if "decodeEntities" in expr or "escapeHtml" not in expr:
                        ln, e = _line_info(source, assign_at, expr)
                        offenders.append((name, ln, e))
    return offenders


class TestVisualizerJsXssRegression(unittest.TestCase):
    def setUp(self) -> None:
        self.sources = _collect_sources()
        self.assertTrue(self.sources, "No frontend sources found to lint")
        self.source = "\n".join(content for _name, content in self.sources)

    def test_culling_html_is_in_the_scan(self) -> None:
        names = [name for name, _content in self.sources]
        self.assertIn("culling.html", names)
        self.assertTrue(
            any(name.replace("\\", "/").startswith("js/") for name in names),
            "js/*.js dropped out of the XSS scan",
        )

    def test_no_decodeEntities_of_escapeHtml(self) -> None:
        """Hard ban on the exact vulnerable pattern.  Any caller that needs
        to decode entities after escaping has a bug — the two operations are
        inverses and the result is effectively raw HTML.

        Lines that are single-line comments (``//``) are skipped so we can
        reference the forbidden pattern in prose without tripping the lint.
        """
        pattern = re.compile(r"decodeEntities\s*\(\s*escapeHtml\s*\(")
        hits: list[tuple[str, int, str]] = []
        for name, source in self.sources:
            for i, line in enumerate(source.splitlines()):
                if not pattern.search(line):
                    continue
                stripped = line.lstrip()
                # Ignore ``// ...`` comment lines — they routinely document the
                # forbidden pattern for future maintainers.
                if stripped.startswith("//"):
                    continue
                hits.append((name, i + 1, line))
        self.assertFalse(
            hits,
            "Forbidden pattern decodeEntities(escapeHtml(...)) reintroduces XSS.\n"
            + "\n".join(f"  {name}:{ln}: {text.strip()}" for name, ln, text in hits),
        )

    def test_scene_name_not_interpolated_into_innerHTML(self) -> None:
        """Block the broader class of the bug: any ``.innerHTML`` assignment
        whose right-hand side interpolates ``sceneName`` (or ``.scene_name``)
        without going through a safe wrapper.

        The permitted wrapper is ``escapeHtml(...)`` with no outer
        ``decodeEntities(...)`` — that case is already guarded by
        ``test_no_decodeEntities_of_escapeHtml``.  We flag *any* bare
        ``${...sceneName...}`` / ``${...scene_name...}`` inside an innerHTML
        template literal so we catch future regressions early.

        The RHS is the whole assignment through the next semicolon that is
        not inside a string or template, so concatenated labels and inline
        CSS ``;`` on the same assignment are still scanned.
        """
        offenders = _scene_name_innerhtml_offenders(self.sources)
        self.assertFalse(
            offenders,
            "sceneName interpolated into innerHTML without safe escaping:\n"
            + "\n".join(f"  {name}:{ln}: ${{{expr}}}" for name, ln, expr in offenders),
        )

    def test_innerhtml_scan_does_not_stop_at_semicolon_inside_template(self) -> None:
        src = (
            'el.innerHTML = `<span style="color:red;font-size:11px">'
            "${scene.sceneName}</span>`;\n"
        )
        offenders = _scene_name_innerhtml_offenders([("snip.js", src)])
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn("scene.sceneName", offenders[0][2])

    def test_innerhtml_scan_allows_escaped_scene_name_after_css_semicolon(self) -> None:
        src = (
            'el.innerHTML = `<span style="color:red;font-size:11px">'
            "${escapeHtml(scene.sceneName)}</span>`;\n"
        )
        self.assertEqual(_scene_name_innerhtml_offenders([("snip.js", src)]), [])

    def test_decodeEntities_not_piped_into_innerHTML(self) -> None:
        """``decodeEntities`` is legitimate when feeding ``textContent`` — the
        browser won't parse HTML there.  What's unsafe is piping its result
        into ``.innerHTML`` in the same statement.  Flag only those.
        """
        # Scan each line (and its successor, in case the assignment wraps)
        # for `.innerHTML =` AND `decodeEntities(` appearing together.
        hits: list[tuple[str, int, str]] = []
        for name, source in self.sources:
            lines = source.splitlines()
            for i, line in enumerate(lines):
                window = line + (lines[i + 1] if i + 1 < len(lines) else "")
                if ".innerHTML" in window and "decodeEntities(" in window:
                    hits.append((name, i + 1, line.strip()))
        self.assertFalse(
            hits,
            "decodeEntities() output used in an innerHTML assignment "
            "(re-enables the FINDING-01 XSS class):\n"
            + "\n".join(f"  {name}:{ln}: {text}" for name, ln, text in hits),
        )


def _line_info(source: str, offset: int, expr: str) -> tuple[int, str]:
    line_no = source.count("\n", 0, offset) + 1
    return line_no, expr.strip()


if __name__ == "__main__":
    unittest.main()
