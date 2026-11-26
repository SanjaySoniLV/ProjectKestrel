"""Unit tests for the interface (UI-string) translations.

* Every key the HTML/JS uses exists in ``analyzer/i18n/en.json`` and every
  en.json key is used.
* ``analyzer/i18n/index.json`` lists exactly the shipped dictionaries, with
  canonical BCP-47 tags and honest machine-translation flags.
* Each shipped dictionary keeps key, placeholder, plural and markup parity
  with English, and uses only the plural categories its language has.
* ``tools/i18n_lint.py``: no unsafe translation sinks, and no hard-coded
  English beyond the ratcheting baseline.
* The JavaScript engine (``analyzer/js/i18n.js``) passes its Node tests, and
  its BCP-47 rules match ``kestrel_analyzer.language_codes``.

See docs/i18n.md for the rules these tests enforce.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ANALYZER_DIR = Path(__file__).parent.parent.parent
REPO_ROOT = ANALYZER_DIR.parent
I18N_DIR = ANALYZER_DIR / 'i18n'
sys.path.insert(0, str(ANALYZER_DIR))
sys.path.insert(0, str(REPO_ROOT / 'tools'))

import i18n_lint  # noqa: E402
from kestrel_analyzer import language_codes  # noqa: E402

pytestmark = pytest.mark.unit

NON_DICT_FILES = {'index.json'}


def _load(name):
    return json.loads((I18N_DIR / name).read_text(encoding='utf-8'))


EN = _load('en.json')
MANIFEST = _load('index.json')
SHIPPED = [l['code'] for l in MANIFEST['languages'] if l['code'] != 'en']


# ----------------------------------------------------------------------------
# Message parsing helpers (mirror the ICU subset in i18n.js)
# ----------------------------------------------------------------------------

def _match_brace(s, i):
    depth = 0
    for j in range(i, len(s)):
        if s[j] == '{':
            depth += 1
        elif s[j] == '}':
            depth -= 1
            if depth == 0:
                return j
    return -1


_ARG_RE = re.compile(r'^\s*(\w+)\s*(?:,\s*(plural|select)\s*,([\s\S]*))?$')
_OPT_RE = re.compile(r'\s*(=\d+|[A-Za-z_][\w-]*)\s*\{')


def _parse_options(body):
    opts, i = {}, 0
    while i < len(body):
        m = _OPT_RE.match(body, i)
        if not m:
            if body[i:].strip():
                raise ValueError(f'bad plural/select options: {body[i:][:40]!r}')
            break
        open_ = m.end() - 1
        close = _match_brace(body, open_)
        if close < 0:
            raise ValueError('unbalanced braces in options')
        opts[m.group(1)] = body[open_ + 1:close]
        i = close + 1
    return opts


def analyze_message(msg):
    """Return (simple_args, {arg: [(kind, {selector,...})]}) or raise ValueError."""
    simple, complex_ = set(), {}

    def walk(s):
        i = 0
        while i < len(s):
            if s[i] == '}':
                raise ValueError('unbalanced }')
            if s[i] != '{':
                i += 1
                continue
            close = _match_brace(s, i)
            if close < 0:
                raise ValueError('unbalanced {')
            m = _ARG_RE.match(s[i + 1:close])
            if not m:
                raise ValueError(f'not a placeholder: {s[i:close + 1][:40]!r}')
            if m.group(2):
                opts = _parse_options(m.group(3))
                if 'other' not in opts:
                    raise ValueError(f'{m.group(1)}: {m.group(2)} needs an "other" branch')
                complex_.setdefault(m.group(1), []).append((m.group(2), set(opts)))
                for body in opts.values():
                    walk(body)
            else:
                simple.add(m.group(1))
            i = close + 1

    walk(msg)
    return simple, complex_


_RICH_TAG_RE = re.compile(r'</?([a-zA-Z]+)[^<>]*>')
_ANY_TAG_RE = re.compile(r'<\s*/?\s*[a-zA-Z][^<>]*>')
RICH_TAGS = {'b', 'strong', 'em', 'i', 'code', 'br'}

# CLDR cardinal categories (integers + decimals) per base language. A
# translation may use only these; REQUIRED must be present whenever the
# English message pluralizes. Languages not listed only need "other".
PLURAL_ALLOWED = {
    'en': {'one', 'other'}, 'de': {'one', 'other'}, 'nl': {'one', 'other'},
    'fr': {'one', 'many', 'other'}, 'es': {'one', 'many', 'other'},
    'pt': {'one', 'many', 'other'}, 'it': {'one', 'many', 'other'},
    'ru': {'one', 'few', 'many', 'other'}, 'uk': {'one', 'few', 'many', 'other'},
    'pl': {'one', 'few', 'many', 'other'},
    'ja': {'other'}, 'zh': {'other'}, 'ko': {'other'},
    'ar': {'zero', 'one', 'two', 'few', 'many', 'other'},
}
PLURAL_REQUIRED = {
    'en': {'one', 'other'}, 'de': {'one', 'other'}, 'fr': {'one', 'other'},
    'es': {'one', 'other'}, 'pt': {'one', 'other'}, 'it': {'one', 'other'},
    'ru': {'one', 'few', 'many', 'other'}, 'uk': {'one', 'few', 'many', 'other'},
    'pl': {'one', 'few', 'many', 'other'},
}


# ----------------------------------------------------------------------------
# Key usage
# ----------------------------------------------------------------------------

_MARKUP_ATTRS = ('data-i18n', 'data-i18n-rich', 'data-i18n-title', 'data-i18n-aria',
                 'data-i18n-placeholder', 'data-i18n-alt', 'data-i18n-data-tip')
_KEY = r'[a-z][a-zA-Z0-9_.]*'
_T_CALL_RE = re.compile(r'(?<![.\w$])(?:t|tHtml)\(\s*[\'"](' + _KEY + r')[\'"]')
_TRICH_RE = re.compile(r'(?<![.\w$])tRich\(\s*[^,]+,\s*[\'"](' + _KEY + r')[\'"]')


def _html_sources():
    return [(ANALYZER_DIR / f).read_text(encoding='utf-8') for f in i18n_lint.HTML_FILES]


def _js_sources():
    return [p.read_text(encoding='utf-8') for p in (ANALYZER_DIR / 'js').glob('*.js')
            if p.name != 'i18n.js']


def used_keys():
    used, rich = set(), set()
    for html in _html_sources():
        for attr in _MARKUP_ATTRS:
            keys = set(re.findall(r'\s' + attr + r'="([^"]+)"', html))
            used |= keys
            if attr == 'data-i18n-rich':
                rich |= keys
    for src in _js_sources():
        used |= set(_T_CALL_RE.findall(src))
        r = set(_TRICH_RE.findall(src))
        used |= r
        rich |= r
    return used, rich


USED, RICH_KEYS = used_keys()


class TestKeyUsage:

    def test_used_keys_exist_in_en(self):
        missing = sorted(USED - set(EN))
        assert not missing, f'keys used but missing from i18n/en.json: {missing}'

    def test_no_orphan_keys_in_en(self):
        orphan = sorted(k for k in EN if not k.startswith('_') and k not in USED)
        assert not orphan, f'en.json keys not used anywhere: {orphan}'

    def test_en_values_are_strings(self):
        bad = sorted(k for k, v in EN.items() if not k.startswith('_') and not isinstance(v, str))
        assert not bad

    def test_en_messages_parse(self):
        bad = []
        for k, v in EN.items():
            if k.startswith('_'):
                continue
            try:
                analyze_message(v)
            except ValueError as e:
                bad.append(f'{k}: {e}')
        assert not bad, bad

    def test_markup_only_in_rich_keys(self):
        # Tags in a message are only rendered by the rich path, and only the
        # allowlisted ones. Anywhere else a '<b>' would show up literally.
        bad = []
        for k, v in EN.items():
            if k.startswith('_'):
                continue
            tags = _ANY_TAG_RE.findall(v)
            if tags and k not in RICH_KEYS:
                bad.append(f'{k}: markup in a plain-text key')
            for tag in tags:
                m = re.fullmatch(r'<\s*/?\s*([a-zA-Z]+)\s*/?\s*>', tag)
                if not m or m.group(1).lower() not in RICH_TAGS:
                    bad.append(f'{k}: {tag} is not an allowlisted, attribute-free tag')
        assert not bad, bad


# ----------------------------------------------------------------------------
# Manifest + dictionaries
# ----------------------------------------------------------------------------

class TestManifest:

    def test_lists_exactly_the_shipped_files(self):
        files = {p.stem for p in I18N_DIR.glob('*.json') if p.name not in NON_DICT_FILES}
        codes = [l['code'] for l in MANIFEST['languages']]
        assert len(codes) == len(set(codes))
        assert set(codes) == files

    def test_codes_are_canonical_bcp47(self):
        bad = [l['code'] for l in MANIFEST['languages']
               if not language_codes.is_valid_language_tag(l['code'])]
        assert not bad

    def test_english_first(self):
        assert MANIFEST['languages'][0]['code'] == 'en'

    def test_flags_match_dictionary_meta(self):
        for l in MANIFEST['languages']:
            if l['code'] == 'en':
                continue
            meta = _load(f"{l['code']}.json").get('_meta', {})
            assert isinstance(meta.get('machine_translated'), bool), l['code']
            assert isinstance(meta.get('reviewed'), bool), l['code']
            assert meta['machine_translated'] == l.get('machine_translated'), l['code']
            assert meta['reviewed'] == l.get('reviewed'), l['code']
            assert meta.get('language') == l['code'], l['code']

    def test_dir_matches_language(self):
        for l in MANIFEST['languages']:
            assert l.get('dir', 'ltr') == language_codes.text_direction(l['code']), l['code']


@pytest.fixture(scope='module', params=SHIPPED)
def lang_dict(request):
    return request.param, _load(f'{request.param}.json')


class TestShippedDictionaries:

    def test_key_parity(self, lang_dict):
        lang, d = lang_dict
        keys = {k for k in d if not k.startswith('_')}
        en_keys = {k for k in EN if not k.startswith('_')}
        assert keys == en_keys, (
            f'{lang}.json key drift: missing={sorted(en_keys - keys)[:8]} '
            f'extra={sorted(keys - en_keys)[:8]}')

    def test_values_nonempty_strings(self, lang_dict):
        lang, d = lang_dict
        bad = sorted(k for k, v in d.items()
                     if not k.startswith('_') and (not isinstance(v, str) or not v.strip()))
        assert not bad, f'{lang}.json empty/non-string values: {bad[:8]}'

    def test_placeholders_match_en(self, lang_dict):
        lang, d = lang_dict
        bad = []
        for k, v in d.items():
            if k.startswith('_') or k not in EN:
                continue
            try:
                s_en, c_en = analyze_message(EN[k])
                s_tr, c_tr = analyze_message(v)
            except ValueError as e:
                bad.append(f'{k}: {e}')
                continue
            # Every English argument must still be referenced (as a simple
            # placeholder or a plural/select); no new arguments may appear.
            if (s_en | set(c_en)) != (s_tr | set(c_tr)):
                bad.append(f'{k}: args {sorted(s_en | set(c_en))} vs {sorted(s_tr | set(c_tr))}')
            # A translation may drop a plural (e.g. Japanese) but must not
            # change a select into a plural or vice versa.
            for arg, uses in c_tr.items():
                kinds_en = {kind for kind, _ in c_en.get(arg, [])}
                if kinds_en and any(kind not in kinds_en for kind, _ in uses):
                    bad.append(f'{k}: {arg} kind changed')
        assert not bad, f'{lang}.json placeholder drift: {bad[:8]}'

    def test_plural_categories(self, lang_dict):
        lang, d = lang_dict
        base = lang.split('-')[0]
        allowed = PLURAL_ALLOWED.get(base)
        required = PLURAL_REQUIRED.get(base, {'other'})
        bad = []
        for k, v in d.items():
            if k.startswith('_') or k not in EN:
                continue
            _, c_en = analyze_message(EN[k])
            _, c_tr = analyze_message(v)
            for arg, uses in c_tr.items():
                for kind, cats in uses:
                    if kind != 'plural':
                        continue
                    named = {c for c in cats if not c.startswith('=')}
                    if allowed is not None and not named <= allowed:
                        bad.append(f'{k}: {sorted(named - allowed)} not a {base} plural category')
                    en_cats = set().union(*(c for kd, c in c_en.get(arg, []) if kd == 'plural')) \
                        if arg in c_en else set()
                    if {'one', 'other'} <= en_cats and not required <= named:
                        bad.append(f'{k}: needs {sorted(required - named)} for {base}')
        assert not bad, f'{lang}.json plural problems: {bad[:8]}'

    def test_markup_matches_en(self, lang_dict):
        lang, d = lang_dict
        bad = []
        for k, v in d.items():
            if k.startswith('_') or k not in EN:
                continue
            en_tags = sorted(t.lower() for t in _RICH_TAG_RE.findall(EN[k]))
            tr_tags = sorted(t.lower() for t in _RICH_TAG_RE.findall(v))
            if en_tags != tr_tags:
                bad.append(k)
        assert not bad, f'{lang}.json markup drift: {bad[:8]}'

    def test_meta_block(self, lang_dict):
        lang, d = lang_dict
        meta = d.get('_meta')
        assert isinstance(meta, dict), f'{lang}.json needs a _meta block'
        assert meta.get('language') == lang
        if meta.get('machine_translated'):
            assert meta.get('reviewed') is False or meta.get('reviewed_by_native_speaker'), lang


# ----------------------------------------------------------------------------
# Lint
# ----------------------------------------------------------------------------

@pytest.fixture(scope='module')
def scan():
    return i18n_lint.collect()


class TestLint:

    def test_no_unsafe_translation_sinks(self, scan):
        _found, unsafe = scan
        flat = [f'{rel}: {u}' for rel, items in sorted(unsafe.items()) for u in items]
        assert not flat, 'translated text reaches an HTML sink unescaped:\n' + '\n'.join(flat)

    def test_no_new_hardcoded_strings(self, scan):
        found, _ = scan
        new, _stale = i18n_lint.diff_against_baseline(found, i18n_lint.load_baseline())
        assert not new, (
            'New hard-coded English UI strings. Add an i18n key (see docs/i18n.md), or '
            'mark deliberate non-translatable text translate="no":\n' + '\n'.join(new[:40]))

    def test_baseline_has_no_stale_entries(self, scan):
        found, _ = scan
        _new, stale = i18n_lint.diff_against_baseline(found, i18n_lint.load_baseline())
        assert not stale, (
            'These strings were converted; shrink the baseline with '
            '`python tools/i18n_lint.py --update-baseline`:\n' + '\n'.join(stale[:40]))

    def test_lint_detects_unsafe_patterns(self, tmp_path):
        js = tmp_path / 'js'
        js.mkdir()
        (js / 'bad.js').write_text(
            "el.innerHTML = `<b>${t('a.b')}</b>`;\n"
            "el.innerHTML = t('a.b');\n"
            "el.textContent = 'Hello world';\n"
            "ok.innerHTML = `<b>${tHtml('a.b')}</b>`;\n"
            "ok.title = `${t('a.b')} · ${t('c.d')}`;\n", encoding='utf-8')
        (tmp_path / 'visualizer.html').write_text(
            '<div data-i18n-html="x">Raw</div><p>Plain English</p>'
            '<p data-i18n="k">Done</p><span translate="no">Brand Name</span>', encoding='utf-8')
        found, unsafe = i18n_lint.collect(tmp_path)
        assert len(unsafe['js/bad.js']) == 2
        assert len(unsafe['visualizer.html']) == 1
        assert found['js/bad.js'] == {'textContent\tHello world': 1}
        assert set(found['visualizer.html']) == {'text\tRaw', 'text\tPlain English'}


# ----------------------------------------------------------------------------
# JavaScript engine (Node)
# ----------------------------------------------------------------------------

NODE = shutil.which('node')
ENGINE_TEST = ANALYZER_DIR / 'tests' / 'js' / 'i18n_engine.test.js'


@pytest.mark.skipif(NODE is None, reason='node is not installed')
class TestJsEngine:

    def test_engine_suite(self):
        r = subprocess.run([NODE, str(ENGINE_TEST)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr

    def test_language_rules_match_python(self):
        tags = ['en', 'EN', 'pt-br', 'pt_BR', 'pt-PT', 'zh', 'zh-TW', 'zh-hk', 'zh-CN', 'zh-Hans',
                'zh-hant-tw', 'sr-Latn', 'es-419', 'ar', 'he-IL', 'fa', 'az-Arab', 'ur-PK',
                'de-DE', 'x', '', '../en', 'en-US-x-private', 'abcd', 'fil-PH']
        r = subprocess.run([NODE, str(ENGINE_TEST), '--lang-table'], input=json.dumps(tags),
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        js = json.loads(r.stdout)
        for tag in tags:
            py = {
                'canonical': language_codes.canonicalize_language_tag(tag),
                'chain': language_codes.language_fallback_chain(tag),
                'dir': language_codes.text_direction(tag),
            }
            assert js[tag] == py, f'{tag!r}: js={js[tag]} py={py}'
