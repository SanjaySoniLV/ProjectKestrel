// Unit tests for analyzer/js/i18n.js, run with plain Node (no npm deps):
//
//   node analyzer/tests/js/i18n_engine.test.js [path/to/i18n.js]
//   node analyzer/tests/js/i18n_engine.test.js --lang-table   (JSON for the Python parity test)
//
// analyzer/tests/unit/test_ui_i18n.py runs this file. It loads i18n.js in a
// vm sandbox with a minimal fake DOM, fetch and pywebview bridge, so the
// escaping, rich-text, plural, fallback and race behaviour is tested against
// the real engine code.
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const args = process.argv.slice(2);
const LANG_TABLE = args.includes('--lang-table');
const ENGINE = path.resolve(args.find(a => !a.startsWith('--')) ||
  path.join(__dirname, '..', '..', 'js', 'i18n.js'));

// ── Minimal DOM ───────────────────────────────────────────────────────────
class Node_ {
  constructor() { this.childNodes = []; this.parentNode = null; }
  get firstChild() { return this.childNodes[0] || null; }
  get parentElement() { return this.parentNode instanceof Element_ ? this.parentNode : null; }
  appendChild(c) {
    if (c.parentNode) c.parentNode.removeChild(c);
    this.childNodes.push(c); c.parentNode = this; return c;
  }
  removeChild(c) {
    const i = this.childNodes.indexOf(c);
    if (i >= 0) this.childNodes.splice(i, 1);
    c.parentNode = null; return c;
  }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  get textContent() { return this.childNodes.map(c => c.textContent).join(''); }
  set textContent(v) {
    this.childNodes.forEach(c => { c.parentNode = null; });
    this.childNodes = [];
    if (v !== '') this.appendChild(new Text_(String(v)));
  }
}
class Text_ extends Node_ {
  constructor(t) { super(); this.data = t; }
  get textContent() { return this.data; }
  set textContent(v) { this.data = String(v); }
}
class Element_ extends Node_ {
  constructor(tag) { super(); this.tagName = tag.toUpperCase(); this.attrs = {}; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  set title(v) { this.attrs.title = String(v); }
  get title() { return this.attrs.title || ''; }
  _walk(fn) { for (const c of this.childNodes) if (c instanceof Element_) { fn(c); c._walk(fn); } }
  querySelectorAll(sel) {
    const m = /^\[([\w-]+)\]$/.exec(sel);
    if (!m) throw new Error('fake DOM supports [attr] selectors only: ' + sel);
    const out = [];
    this._walk(e => { if (e.hasAttribute(m[1])) out.push(e); });
    return out;
  }
  // Serialize: elements as <tag attr="">, text escaped — what a browser's
  // innerHTML getter would show, so tests can assert on structure.
  get outerHTML() {
    const tag = this.tagName.toLowerCase();
    const attrs = Object.entries(this.attrs).map(([k, v]) => ` ${k}="${v}"`).join('');
    if (tag === 'br') return '<br>';
    return `<${tag}${attrs}>${this.innerHTML}</${tag}>`;
  }
  get innerHTML() {
    return this.childNodes.map(c => c instanceof Element_ ? c.outerHTML
      : c.data.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')).join('');
  }
}

function el(tag, attrs, ...kids) {
  const e = new Element_(tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v);
  for (const k of kids) e.appendChild(typeof k === 'string' ? new Text_(k) : k);
  return e;
}

// ── Sandbox ───────────────────────────────────────────────────────────────
function makeSandbox({ files, settings, bridge }) {
  const html = new Element_('html');
  const body = new Element_('body');
  html.appendChild(body);
  const events = [];
  const store = { 'kestrel-webviz-settings-v1': JSON.stringify(settings || {}) };
  const document = {
    documentElement: html,
    body,
    createElement: (t) => new Element_(t),
    createTextNode: (t) => new Text_(t),
    querySelectorAll: (sel) => html.querySelectorAll(sel),
    dispatchEvent: (e) => { events.push(e); return true; },
    addEventListener: () => { },
  };
  const sandbox = {
    console, Intl, Promise, setTimeout, JSON, Object, Array, String, Number, Map, Set, RegExp, Error,
    document,
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
    },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
    fetch: async (url) => {
      const name = decodeURIComponent(String(url).replace(/^i18n\//, ''));
      const f = files[name];
      if (f === undefined) return { ok: false, json: async () => ({}) };
      if (f && f.delay) await new Promise(r => setTimeout(r, f.delay));
      const data = f && f.delay ? f.data : f;
      return { ok: true, json: async () => JSON.parse(JSON.stringify(data)) };
    },
    hasPywebviewApi: !!bridge,
  };
  sandbox.window = sandbox;
  if (bridge) sandbox.pywebview = { api: bridge };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(ENGINE, 'utf8'), sandbox, { filename: ENGINE });
  sandbox.__events = events;
  return sandbox;
}

const MANIFEST = {
  languages: [
    { code: 'en', name_native: 'English' },
    { code: 'de', name_native: 'Deutsch' },
    { code: 'ru', name_native: 'Русский' },
    { code: 'pt', name_native: 'Português' },
    { code: 'pt-BR', name_native: 'Português (Brasil)' },
    { code: 'ar', name_native: 'العربية' },
  ],
};

// ── Language-table mode (Python parity) ───────────────────────────────────
if (LANG_TABLE) {
  const sb = makeSandbox({ files: {} });
  const tags = JSON.parse(fs.readFileSync(0, 'utf8'));
  const out = {};
  for (const tag of tags) {
    out[tag] = {
      canonical: sb.canonicalizeLang(tag),
      chain: sb.langFallbackChain(tag),
      dir: sb.langTextDirection(tag),
    };
  }
  process.stdout.write(JSON.stringify(out));
  process.exit(0);
}

// ── Tests ─────────────────────────────────────────────────────────────────
const tests = [];
function test(name, fn) { tests.push({ name, fn }); }
function eq(a, b, msg) {
  const sa = JSON.stringify(a), sb = JSON.stringify(b);
  if (sa !== sb) throw new Error(`${msg || 'not equal'}\n  got:      ${sa}\n  expected: ${sb}`);
}
function ok(c, msg) { if (!c) throw new Error(msg || 'assertion failed'); }

const HOSTILE = '<img src=x onerror="alert(1)"><script>alert(2)</script>';

test('t() returns plain text; tHtml() escapes translation and vars', async () => {
  const sb = makeSandbox({ files: { 'index.json': MANIFEST, 'en.json': { k: 'Hi {name}', evil: HOSTILE } } });
  await sb.initI18n();
  eq(sb.t('evil'), HOSTILE, 't() must not alter text');
  ok(typeof sb.tHtml === 'function', 'tHtml() must exist');
  const h = sb.tHtml('evil');
  ok(!h.includes('<'), `tHtml() leaked markup: ${h}`);
  eq(sb.tHtml('k', { name: '<b>x</b>' }), 'Hi &lt;b&gt;x&lt;/b&gt;');
});

test('parseRich keeps only allowlisted, attribute-free tags', async () => {
  const sb = makeSandbox({ files: {} });
  const P = sb.__kestrelI18n.parseRich;
  const tree = P('<b>bold</b> <a href="javascript:x">link</a> <b class="x">c</b> ' + HOSTILE + ' <br/>');
  const tags = [];
  (function walk(ns) { for (const n of ns) if (n.type === 'el') { tags.push(n.tag); walk(n.children); } })(tree);
  eq(tags, ['b', 'br'], 'only <b> and <br> should become elements');
  const text = JSON.stringify(tree);
  ok(text.includes('<a href=\\"javascript:x\\">'), 'a non-allowlisted tag must survive as literal text');
});

test('data-i18n-rich renders allowlisted tags as DOM, everything else as text', async () => {
  const sb = makeSandbox({
    files: {
      'index.json': MANIFEST,
      'en.json': { r: 'Click <b>Load</b> now', bad: 'x ' + HOSTILE + ' <strong>y</strong>' },
    },
  });
  const a = el('div', { 'data-i18n-rich': 'r' }, 'old');
  const b = el('div', { 'data-i18n-rich': 'bad' });
  sb.document.body.appendChild(a); sb.document.body.appendChild(b);
  await sb.initI18n();
  eq(a.innerHTML, 'Click <b>Load</b> now');
  ok(!/<img|<script/.test(b.innerHTML), `hostile markup became elements: ${b.innerHTML}`);
  ok(b.innerHTML.includes('&lt;img'), 'hostile markup should be visible as text');
  ok(b.innerHTML.endsWith('<strong>y</strong>'));
});

test('rich slots move existing elements (ids survive) and can be reordered', async () => {
  const sb = makeSandbox({
    files: {
      'index.json': MANIFEST,
      'en.json': { cap: 'Captured at {time}. Check {link}.', lnk: 'here' },
      'de.json': { cap: 'Prüfe {link}: Aufnahme um {time}.', lnk: 'hier' },
    },
    settings: { language: 'de' },
  });
  const time = el('strong', { id: 'firstTime', 'data-i18n-slot': 'time' }, '12:00');
  const link = el('a', { href: '#', 'data-i18n-slot': 'link', 'data-i18n': 'lnk' }, 'here');
  const host = el('div', { 'data-i18n-rich': 'cap' }, 'Captured at ', time, '. Check ', link, '.');
  sb.document.body.appendChild(host);
  await sb.initI18n();
  eq(host.innerHTML, 'Prüfe <a href="#" data-i18n-slot="link" data-i18n="lnk">hier</a>: Aufnahme um ' +
    '<strong id="firstTime" data-i18n-slot="time">12:00</strong>.');
  ok(host.childNodes.includes(time), 'slot element must be the same node');
});

test('tRich() inserts vars as text, never markup', async () => {
  const sb = makeSandbox({ files: { 'index.json': MANIFEST, 'en.json': { m: 'Folder <b>{name}</b>: {count, plural, one {# file} other {# files}}' } } });
  await sb.initI18n();
  const host = el('p', {});
  sb.tRich(host, 'm', { name: '<img src=x onerror=1>', count: 2 });
  eq(host.innerHTML, 'Folder <b>&lt;img src=x onerror=1&gt;</b>: 2 files');
});

test('there is no raw-HTML attribute path (data-i18n-html renders nothing)', async () => {
  const sb = makeSandbox({ files: { 'index.json': MANIFEST, 'en.json': { h: HOSTILE } } });
  const node = el('div', { 'data-i18n-html': 'h' }, 'orig');
  sb.document.body.appendChild(node);
  await sb.initI18n();
  eq(node.innerHTML, 'orig');
});

test('plurals follow Intl.PluralRules (Russian one/few/many) and # is localized', async () => {
  const sb = makeSandbox({
    files: {
      'index.json': MANIFEST,
      'en.json': { n: '{count, plural, one {# scene} other {# scenes}}' },
      'ru.json': { n: '{count, plural, one {# сцена} few {# сцены} many {# сцен} other {# сцены}}' },
    },
    settings: { language: 'ru' },
  });
  await sb.initI18n();
  eq([1, 2, 5, 21, 1.5].map(n => sb.t('n', { count: n })),
    ['1 сцена', '2 сцены', '5 сцен', '21 сцена', '1,5 сцены']);
  eq(sb.t('n', { count: 12345 }), '12 345 сцен');
});

test('plural =N exact match, select, nested placeholders, missing other', async () => {
  const sb = makeSandbox({
    files: {
      'index.json': MANIFEST,
      'en.json': {
        z: '{count, plural, =0 {No folders} one {# folder} other {# folders}} in {where}',
        s: '{kind, select, folder {Folder {name}} other {Item}}',
      },
    },
  });
  await sb.initI18n();
  eq(sb.t('z', { count: 0, where: 'A' }), 'No folders in A');
  eq(sb.t('z', { count: 1, where: 'A' }), '1 folder in A');
  eq(sb.t('z', { count: 1000, where: 'A' }), '1,000 folders in A');
  eq(sb.t('s', { kind: 'folder', name: 'X' }), 'Folder X');
  eq(sb.t('s', { kind: 'zzz' }), 'Item');
});

test('fallback chain pt-BR -> pt -> en, _meta ignored, missing key -> key', async () => {
  const sb = makeSandbox({
    files: {
      'index.json': MANIFEST,
      'en.json': { a: 'A-en', b: 'B-en', c: 'C-en' },
      'pt.json': { _meta: { machine_translated: true }, a: 'A-pt', b: 'B-pt' },
      'pt-BR.json': { _meta: { machine_translated: true }, a: 'A-br' },
    },
    settings: { language: 'pt-br' },
  });
  await sb.initI18n();
  eq([sb.t('a'), sb.t('b'), sb.t('c'), sb.t('_meta'), sb.t('nope')], ['A-br', 'B-pt', 'C-en', '_meta', 'nope']);
  eq(sb.document.documentElement.getAttribute('lang'), 'pt-BR');
});

test('<html lang/dir> follow the shown language (RTL for Arabic; unshipped -> en)', async () => {
  const sb = makeSandbox({ files: { 'index.json': MANIFEST, 'en.json': { a: 'A' }, 'ar.json': { a: 'أ' } }, settings: { language: 'ar' } });
  await sb.initI18n();
  eq(sb.document.documentElement.getAttribute('lang'), 'ar');
  eq(sb.document.documentElement.getAttribute('dir'), 'rtl');
  await sb.setLanguage('ja');   // not in the manifest: shown in English
  eq(sb.document.documentElement.getAttribute('lang'), 'en');
  eq(sb.document.documentElement.getAttribute('dir'), 'ltr');
  eq(sb.t('a'), 'A');
});

test('switching UI language mid-load: the last choice wins', async () => {
  const sb = makeSandbox({
    files: {
      'index.json': MANIFEST,
      'en.json': { a: 'A' },
      'de.json': { delay: 60, data: { a: 'A-de' } },
      'ru.json': { delay: 5, data: { a: 'A-ru' } },
    },
  });
  await sb.initI18n();
  const p1 = sb.setLanguage('de');
  const p2 = sb.setLanguage('ru');
  await Promise.all([p1, p2]);
  eq(sb.t('a'), 'A-ru');
  eq(sb.document.documentElement.getAttribute('lang'), 'ru');
});

test('loadSpeciesTranslations: dedupes same language, last language wins', async () => {
  const calls = [];
  const bridge = {
    get_bird_name_translations: (lang) => {
      calls.push(lang);
      const delay = lang === 'de' ? 60 : 5;
      return new Promise(r => setTimeout(() => r({ success: true, lang, map: { 'Common Ostrich': `Ostrich-${lang}` } }), delay));
    },
  };
  const sb = makeSandbox({ files: { 'index.json': MANIFEST, 'en.json': {} }, bridge });
  const a = sb.loadSpeciesTranslations('de');
  const b = sb.loadSpeciesTranslations('de');
  eq(calls, ['de'], 'same-language loads must share one request');
  const c = sb.loadSpeciesTranslations('fr');
  const results = await Promise.all([a, b, c]);
  eq(results, [false, false, true], 'superseded load must report not-applied');
  eq(sb.trSpecies('Common Ostrich'), 'Ostrich-fr', 'slow earlier response must not overwrite');
  await sb.setBirdNameLanguage('en');
  eq(sb.trSpecies('Common Ostrich'), 'Common Ostrich');
});

test('setBirdNameLanguage only announces a change that was applied', async () => {
  const bridge = {
    get_bird_name_translations: (lang) => new Promise(r => setTimeout(() =>
      r({ success: true, lang, map: { X: lang } }), lang === 'de' ? 50 : 5)),
  };
  const sb = makeSandbox({ files: { 'index.json': MANIFEST, 'en.json': {} }, bridge });
  await Promise.all([sb.setBirdNameLanguage('de'), sb.setBirdNameLanguage('fr')]);
  const changed = sb.__events.filter(e => e.type === 'kestrel:species-lang-changed').map(e => e.detail.lang);
  eq(changed, ['fr']);
  eq(sb.trSpecies('X'), 'fr');
});

test('BCP-47 canonicalization and fallback chains', async () => {
  const sb = makeSandbox({ files: {} });
  eq(sb.canonicalizeLang('pt-br'), 'pt-BR');
  eq(sb.canonicalizeLang('ZH_hant'), 'zh-Hant');
  eq(sb.canonicalizeLang('es-419'), 'es-419');
  eq(sb.canonicalizeLang('../en'), null);
  eq(sb.langFallbackChain('zh-TW'), ['zh-Hant-TW', 'zh-Hant', 'zh', 'en']);
  eq(sb.langFallbackChain('zh'), ['zh-Hans', 'zh', 'en']);
  eq(sb.langFallbackChain('pt-BR'), ['pt-BR', 'pt', 'en']);
  eq(sb.langTextDirection('he'), 'rtl');
  eq(sb.langTextDirection('az-Arab'), 'rtl');
  eq(sb.langTextDirection('sr-Latn'), 'ltr');
});

(async () => {
  let failed = 0;
  for (const { name, fn } of tests) {
    try {
      await fn();
      console.log(`ok   ${name}`);
    } catch (e) {
      failed++;
      console.log(`FAIL ${name}\n     ${String(e && e.message || e).split('\n').join('\n     ')}`);
    }
  }
  console.log(`${tests.length - failed}/${tests.length} passed`);
  process.exit(failed ? 1 : 0);
})();
