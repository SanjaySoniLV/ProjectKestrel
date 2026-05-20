"""Static regression tests for FINDING-01: stored DOM-XSS via sceneName.

These tests are deliberately written as source-level lints against
``analyzer/js/*.js`` (the split-out frontend modules, previously a single
``visualizer.js``) so the vulnerable pattern cannot silently re-appear.
They don't require a JS runtime.

What is forbidden
-----------------
1. ``decodeEntities(escapeHtml(...))`` anywhere — the original bug combined
   these two in series, which *undoes* the escape immediately before an
   ``innerHTML`` assignment.
2. ``X.innerHTML = \`...\\${...scene_name...}...\``` — user-controlled scene
   names interpolated into ``.innerHTML`` template literals without an
   explicit escape.  After the fix, the sceneName site must use ``textContent``
   or explicit DOM construction.

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


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ANALYZER_DIR = os.path.dirname(_THIS_DIR)


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


class TestVisualizerJsXssRegression(unittest.TestCase):
    JS_DIR = os.path.join(_ANALYZER_DIR, "js")

    def setUp(self) -> None:
        self.assertTrue(
            os.path.isdir(self.JS_DIR),
            f"Missing directory: {self.JS_DIR}",
        )
        # Concatenate all js/*.js files (sorted for stable line attribution).
        # `self.sources` is a list of (filename, content) for per-file reporting,
        # and `self.source` is the flat concat used by the legacy regex checks.
        self.sources: list[tuple[str, str]] = []
        for path in sorted(glob.glob(os.path.join(self.JS_DIR, "*.js"))):
            self.sources.append((os.path.basename(path), _read(path)))
        self.assertTrue(
            self.sources,
            f"No .js files found under {self.JS_DIR}",
        )
        self.source = "\n".join(content for _name, content in self.sources)

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
            + "\n".join(f"  js/{name}:{ln}: {text.strip()}" for name, ln, text in hits),
        )

    def test_scene_name_not_interpolated_into_innerHTML(self) -> None:
        """Block the broader class of the bug: any ``.innerHTML`` assignment
        whose right-hand side is a template literal that references
        ``sceneName`` (or ``.scene_name``) without going through a safe
        wrapper.

        The permitted wrapper is ``escapeHtml(...)`` with no outer
        ``decodeEntities(...)`` — that case is already guarded by
        ``test_no_decodeEntities_of_escapeHtml``.  We flag *any* bare
        ``${...sceneName...}`` / ``${...scene_name...}`` inside an innerHTML
        template literal so we catch future regressions early.
        """
        # Match one logical statement: ``foo.innerHTML = `...`;`` possibly spanning
        # up to a few lines.  We capture the template body and scan it.
        stmt_re = re.compile(
            r"\.innerHTML\s*=\s*`([^`]*)`",
            re.DOTALL,
        )
        offenders: list[tuple[str, int, str]] = []
        for name, source in self.sources:
            for m in stmt_re.finditer(source):
                body = m.group(1)
                if "sceneName" not in body and "scene_name" not in body:
                    continue
                # Inspect each ${...} expression that references sceneName/scene_name.
                for expr_match in re.finditer(r"\$\{([^{}]+)\}", body):
                    expr = expr_match.group(1)
                    if "sceneName" not in expr and "scene_name" not in expr:
                        continue
                    # Permit only escapeHtml(...) with no outer decodeEntities().
                    if "decodeEntities" in expr:
                        ln, e = _line_info(source, m.start(), expr)
                        offenders.append((name, ln, e))
                        continue
                    if "escapeHtml" not in expr:
                        ln, e = _line_info(source, m.start(), expr)
                        offenders.append((name, ln, e))
        self.assertFalse(
            offenders,
            "sceneName interpolated into innerHTML without safe escaping:\n"
            + "\n".join(
                f"  js/{name}:{ln}: ${{{expr}}}" for name, ln, expr in offenders
            ),
        )

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
            + "\n".join(f"  js/{name}:{ln}: {text}" for name, ln, text in hits),
        )


def _line_info(source: str, offset: int, expr: str) -> tuple[int, str]:
    line_no = source.count("\n", 0, offset) + 1
    return line_no, expr.strip()


if __name__ == "__main__":
    unittest.main()
