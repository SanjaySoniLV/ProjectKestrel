"""Lint the desktop UI for i18n regressions.

Two checks, both run by ``analyzer/tests/unit/test_ui_i18n.py``:

1. **Unsafe translation sinks (hard failures, never baselined).** Translated
   text is untrusted, so it must never reach an HTML parser unescaped:

   * ``data-i18n-html`` in an HTML file (use ``data-i18n-rich``);
   * ``${t(...)}`` or ``${trSpecies(...)}`` inside a template literal that
     contains HTML tags (use ``${tHtml(...)}`` / ``escapeHtml(trSpecies(...))``);
   * ``innerHTML = t(...)``, ``innerHTML += ... t(...)``, or
     ``insertAdjacentHTML(..., t(...))``.

2. **Hard-coded English (ratcheting baseline).** User-visible English that
   is not routed through the dictionaries:

   * HTML (``visualizer.html``, ``culling.html``): text nodes outside an
     element carrying ``data-i18n`` / ``data-i18n-rich``, and ``title`` /
     ``placeholder`` / ``aria-label`` / ``alt`` / ``data-tip`` attributes
     without the matching ``data-i18n-*`` attribute.
   * JS (``analyzer/js/*.js``): string literals assigned to ``textContent`` /
     ``innerText`` / ``title`` / ``placeholder`` / ``innerHTML``, passed to
     ``setAttribute('title'|'aria-label'|'placeholder', …)``, or to
     ``showToast`` / ``setStatus`` / ``confirm`` / ``alert``.

   Everything found today is recorded in
   ``tools/i18n_untranslated_baseline.json``. The test fails when a string
   appears that is not in the baseline (new hard-coded English) and when a
   baseline entry no longer occurs (the baseline must shrink as strings are
   converted). Regenerate after converting strings with:

       python tools/i18n_lint.py --update-baseline

   Mark an element ``translate="no"`` (or ``data-i18n-skip``) for text that
   is deliberately not translated (brand names, file names, code).

Run ``python tools/i18n_lint.py`` for a report.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ANALYZER = REPO_ROOT / 'analyzer'
HTML_FILES = ('visualizer.html', 'culling.html')
JS_DIR = ANALYZER / 'js'
BASELINE_PATH = REPO_ROOT / 'tools' / 'i18n_untranslated_baseline.json'

_WORD_RE = re.compile(r'[A-Za-z]{2,}')
_WS_RE = re.compile(r'\s+')

# Attribute -> the data-i18n-* attribute that translates it.
_TRANSLATABLE_ATTRS = {
    'title': 'data-i18n-title',
    'placeholder': 'data-i18n-placeholder',
    'aria-label': 'data-i18n-aria',
    'alt': 'data-i18n-alt',
    'data-tip': 'data-i18n-data-tip',
}
_TEXT_I18N_ATTRS = ('data-i18n', 'data-i18n-rich')
_SKIP_TAGS = {'script', 'style'}
_VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link',
              'meta', 'param', 'source', 'track', 'wbr'}


def _norm(s: str) -> str:
    return _WS_RE.sub(' ', s).strip()


def _is_text(s: str) -> bool:
    return bool(_WORD_RE.search(s))


# ----------------------------------------------------------------------------
# HTML
# ----------------------------------------------------------------------------

class _HtmlScanner(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []   # (tag, covered)
        self.found: list[tuple[str, str]] = []
        self.unsafe: list[str] = []

    def _covered(self) -> bool:
        return bool(self.stack) and self.stack[-1][1]

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if 'data-i18n-html' in a:
            self.unsafe.append(f'data-i18n-html="{a["data-i18n-html"]}" (use data-i18n-rich)')
        parent_covered = self._covered()
        skip = tag in _SKIP_TAGS or a.get('translate') == 'no' or 'data-i18n-skip' in a
        covered = parent_covered or skip or any(k in a for k in _TEXT_I18N_ATTRS)
        if not (parent_covered or skip):
            for attr, i18n_attr in _TRANSLATABLE_ATTRS.items():
                v = a.get(attr)
                if v and _is_text(v) and i18n_attr not in a:
                    self.found.append((attr, _norm(v)))
        if tag not in _VOID_TAGS:
            self.stack.append((tag, covered))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID_TAGS and self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self._covered():
            return
        text = _norm(data)
        if text and _is_text(text):
            self.found.append(('text', text))


def scan_html(path: Path):
    p = _HtmlScanner()
    p.feed(path.read_text(encoding='utf-8'))
    p.close()
    return p.found, p.unsafe


# ----------------------------------------------------------------------------
# JS
# ----------------------------------------------------------------------------

_LIT = r"""(?:'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"|`(?:[^`\\]|\\.)*`)"""
_JS_PATTERNS = [
    ('textContent', re.compile(r'\.(?:textContent|innerText)\s*\+?=\s*(' + _LIT + ')')),
    ('title', re.compile(r'\.title\s*=\s*(' + _LIT + ')')),
    ('placeholder', re.compile(r'\.placeholder\s*=\s*(' + _LIT + ')')),
    ('innerHTML', re.compile(r'\.innerHTML\s*\+?=\s*(' + _LIT + ')')),
    ('attr', re.compile(r"setAttribute\(\s*['\"](?:title|aria-label|placeholder)['\"]\s*,\s*(" + _LIT + ')')),
    ('call', re.compile(r'(?<![\w.$])(?:showToast|setStatus|confirm|alert)\(\s*(' + _LIT + ')')),
]
_INTERP_RE = re.compile(r'\$\{(?:[^{}]|\{[^{}]*\})*\}')
_TAG_RE = re.compile(r'<[^<>]*>')
_ENTITY_RE = re.compile(r'&[a-zA-Z#0-9]+;')


def _literal_text(lit: str) -> str:
    body = lit[1:-1]
    if lit[0] == '`':
        body = _INTERP_RE.sub(' ', body)
    body = _TAG_RE.sub(' ', body)
    body = _ENTITY_RE.sub(' ', body)
    body = body.replace('\\n', ' ')
    return _norm(body)


def _template_literals(src: str):
    """Yield (start, end) spans of template literals, honouring ${...} nesting."""
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in '\'"':
            j = i + 1
            while j < n and src[j] != c and src[j] != '\n':
                j += 2 if src[j] == '\\' else 1
            i = j + 1
            continue
        if c == '/' and i + 1 < n and src[i + 1] == '/':
            j = src.find('\n', i)
            i = n if j < 0 else j + 1
            continue
        if c == '/' and i + 1 < n and src[i + 1] == '*':
            j = src.find('*/', i + 2)
            i = n if j < 0 else j + 2
            continue
        if c == '`':
            j, depth = i + 1, 0
            while j < n:
                if src[j] == '\\':
                    j += 2
                    continue
                if depth == 0 and src[j] == '`':
                    break
                if src.startswith('${', j):
                    depth += 1
                    j += 2
                    continue
                if depth and src[j] == '}':
                    depth -= 1
                elif depth and src[j] == '{':
                    depth += 1
                j += 1
            yield i, j + 1
            i = j + 1
            continue
        i += 1


_HTML_TEMPLATE_RE = re.compile(r'<[a-zA-Z/][^<>]*>')
_RAW_T_IN_TEMPLATE = re.compile(r'\$\{\s*(?:t|trSpecies)\(')
_UNSAFE_JS = [
    re.compile(r'\.innerHTML\s*\+?=\s*(?:t|trSpecies)\('),
    re.compile(r'\.innerHTML\s*\+?=[^;\n]*[+]\s*(?:t|trSpecies)\('),
    re.compile(r'insertAdjacentHTML\([^;\n]*[,+]\s*(?:t|trSpecies)\('),
]


def scan_js(path: Path):
    src = path.read_text(encoding='utf-8')
    found: list[tuple[str, str]] = []
    unsafe: list[str] = []
    for kind, rx in _JS_PATTERNS:
        for m in rx.finditer(src):
            text = _literal_text(m.group(1))
            if text and _is_text(text):
                found.append((kind, text))
    for a, b in _template_literals(src):
        lit = src[a:b]
        if _HTML_TEMPLATE_RE.search(lit) and _RAW_T_IN_TEMPLATE.search(lit):
            line = src.count('\n', 0, a) + 1
            unsafe.append(f'line {line}: ${{t(...)}} inside an HTML template literal (use ${{tHtml(...)}})')
    for rx in _UNSAFE_JS:
        for m in rx.finditer(src):
            line = src.count('\n', 0, m.start()) + 1
            unsafe.append(f'line {line}: translated text assigned as HTML: {m.group(0)[:60]}')
    return found, unsafe


# ----------------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------------

def collect(root: Path = ANALYZER):
    """Return (found, unsafe): found = {file: Counter('kind\\ttext')}."""
    found: dict[str, Counter] = {}
    unsafe: dict[str, list[str]] = {}
    targets = [root / f for f in HTML_FILES] + sorted((root / 'js').glob('*.js'))
    for path in targets:
        if not path.exists():
            continue
        rel = path.relative_to(root).as_posix()
        if path.suffix == '.html':
            f, u = scan_html(path)
        else:
            if path.name == 'i18n.js' or path.name.startswith('vendor'):
                continue
            f, u = scan_js(path)
        if f:
            found[rel] = Counter(f'{k}\t{t}' for k, t in f)
        if u:
            unsafe[rel] = u
    return found, unsafe


def load_baseline(path: Path = BASELINE_PATH) -> dict[str, Counter]:
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    out = {}
    for rel, entries in raw.get('files', {}).items():
        c = Counter()
        for e in entries:
            c[e] += 1
        out[rel] = c
    return out


def write_baseline(found: dict[str, Counter], path: Path = BASELINE_PATH) -> None:
    data = {
        '_comment': ('Hard-coded English still to be converted to i18n keys. Generated by '
                     'tools/i18n_lint.py --update-baseline; it may only shrink. Format: '
                     '"<kind>\\t<text>", one entry per occurrence.'),
        'files': {rel: sorted(c.elements()) for rel, c in sorted(found.items())},
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')


def diff_against_baseline(found, baseline):
    """Return (new, stale): lists of 'file: kind\\ttext' strings."""
    new, stale = [], []
    for rel in sorted(set(found) | set(baseline)):
        cur = found.get(rel, Counter())
        base = baseline.get(rel, Counter())
        for e, n in (cur - base).items():
            new.extend([f'{rel}: {e}'] * n)
        for e, n in (base - cur).items():
            stale.extend([f'{rel}: {e}'] * n)
    return new, stale


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--update-baseline', action='store_true',
                    help='Rewrite tools/i18n_untranslated_baseline.json from the current tree.')
    args = ap.parse_args(argv)
    found, unsafe = collect()
    rc = 0
    for rel, items in sorted(unsafe.items()):
        for u in items:
            print(f'UNSAFE {rel}: {u}')
            rc = 1
    if args.update_baseline:
        write_baseline(found)
        total = sum(sum(c.values()) for c in found.values())
        print(f'Wrote {BASELINE_PATH.relative_to(REPO_ROOT)} ({total} entries)')
        return rc
    new, stale = diff_against_baseline(found, load_baseline())
    for e in new:
        print(f'NEW hard-coded string  {e}')
    for e in stale:
        print(f'STALE baseline entry   {e}')
    if new or stale:
        rc = 1
    total = sum(sum(c.values()) for c in found.values())
    print(f'{total} hard-coded strings remain (baseline); {len(new)} new, {len(stale)} stale, '
          f'{sum(len(v) for v in unsafe.values())} unsafe sinks')
    return rc


if __name__ == '__main__':
    sys.exit(main())
