// ── i18n engine ─────────────────────────────────────────────────────────────
// Display-language support for Project Kestrel. Two layers, two independent
// settings:
//
//   UI strings  — dictionaries at analyzer/i18n/<tag>.json (tag = BCP-47,
//                 e.g. 'de', 'pt-BR', 'zh-Hans'). en.json is the source of
//                 truth; a language overrides keys on top of it, walking the
//                 fallback chain pt-BR -> pt -> en. i18n/index.json lists the
//                 shipped languages. The interface language is the
//                 'language' setting.
//
//   Bird names  — species labels stay stored in canonical English everywhere
//                 (CSV, scene JSON, XMP, search). trSpecies() resolves a label
//                 to the active language at render time, falling back to
//                 English for names the language file lacks. The bird-name
//                 language is the separate 'bird_name_language' setting; the
//                 maps are built by tools/build_bird_names_i18n.py from
//                 Multiling IOC and served through get_bird_name_translations().
//
// ESCAPING RULE (see docs/i18n.md). Translations are untrusted text: they
// come from outside contributors and machine translation.
//
//   t(key, vars)        -> plain text. Use with textContent, title,
//                          setAttribute, showToast, confirm(). Never HTML.
//   tHtml(key, vars)    -> the same text HTML-escaped, for the rare template
//                          literal that is assigned to innerHTML.
//   tRich(el, key, vars)-> renders into an element with a tiny allowlist of
//                          inline tags (<b> <strong> <em> <i> <code> <br>, no
//                          attributes) built as DOM nodes; everything else is
//                          shown as literal text. {name} placeholders can be
//                          filled by child elements marked data-i18n-slot.
//
// Static markup uses data-i18n (text), data-i18n-rich (allowlisted inline
// tags + slots), data-i18n-title / -aria / -placeholder / -alt / -data-tip. There is
// no raw-HTML path; tools/i18n_lint.py fails the build on one.
//
// Messages may use ICU-style plurals and selects:
//   "{count, plural, one {# scene} other {# scenes}}"
//   "{kind, select, folder {Folder} other {Item}}"
// Plural categories come from Intl.PluralRules for the active language.
//
// This file must stay load-order independent: it is included before
// settings.js/state.js in visualizer.html and in culling.html, so it reads
// settings itself and every hook guards with `typeof` where it touches other
// modules' globals.
(function () {
  'use strict';

  const SETTINGS_KEY = 'kestrel-webviz-settings-v1';
  const SOURCE_LANG = 'en';

  function _readSetting(key, def) {
    try {
      const s = JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {};
      return (key in s) ? s[key] : def;
    } catch (_) { return def; }
  }

  async function _writeSetting(key, value) {
    let s = {};
    try { s = JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {}; } catch (_) { }
    s[key] = value;
    try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(s)); } catch (_) { }
    if (typeof hasPywebviewApi !== 'undefined' && hasPywebviewApi && window.pywebview?.api?.save_settings_data) {
      try { await window.pywebview.api.save_settings_data(s); } catch (_) { }
    }
  }

  function _log(...args) {
    if (typeof kdebug === 'function') kdebug(...args);
  }

  function _dispatch(name, detail) {
    try { document.dispatchEvent(new CustomEvent(name, { detail })); } catch (_) { }
  }

  // ── BCP-47 language tags ──────────────────────────────────────────────────
  // Mirror of analyzer/kestrel_analyzer/language_codes.py; the unit tests
  // run both against one table. Accepted subset: language[-Script][-REGION].
  const _TAG_RE = /^([A-Za-z]{2,3})(?:-([A-Za-z]{4}))?(?:-([A-Za-z]{2}|[0-9]{3}))?$/;
  const _IMPLIED_SCRIPT = {
    'zh-TW': 'Hant', 'zh-HK': 'Hant', 'zh-MO': 'Hant',
    'zh-CN': 'Hans', 'zh-SG': 'Hans', 'zh-MY': 'Hans',
  };
  const _DEFAULT_SCRIPT = { zh: 'Hans' };
  const _RTL_SCRIPTS = new Set(['Arab', 'Hebr', 'Thaa', 'Syrc', 'Nkoo', 'Adlm', 'Rohg']);
  const _RTL_LANGUAGES = new Set(['ar', 'he', 'fa', 'ur', 'ps', 'yi', 'dv', 'ckb', 'sd', 'ug']);

  /** Canonical form of a BCP-47 tag ('pt-br' -> 'pt-BR'), or null. */
  function canonicalizeLang(tag) {
    if (typeof tag !== 'string') return null;
    const s = tag.trim().replace(/_/g, '-');
    if (!s || s.length > 16) return null;
    const m = _TAG_RE.exec(s);
    if (!m) return null;
    let out = m[1].toLowerCase();
    if (m[2]) out += '-' + m[2][0].toUpperCase() + m[2].slice(1).toLowerCase();
    if (m[3]) out += '-' + m[3].toUpperCase();
    return out;
  }

  /** Lookup order for a tag, most specific first, always ending in 'en'. */
  function fallbackChain(tag) {
    const canon = canonicalizeLang(tag);
    if (!canon) return [SOURCE_LANG];
    const parts = canon.split('-');
    const lang = parts[0];
    let script = parts.slice(1).find(p => p.length === 4) || null;
    const region = parts.slice(1).find(p => p.length !== 4) || null;
    if (!script && region) script = _IMPLIED_SCRIPT[`${lang}-${region}`] || null;
    const chain = [];
    const add = (t) => { if (!chain.includes(t)) chain.push(t); };
    if (script && region) add(`${lang}-${script}-${region}`);
    if (region && !script) add(`${lang}-${region}`);
    if (script) add(`${lang}-${script}`);
    else if (_DEFAULT_SCRIPT[lang]) add(`${lang}-${_DEFAULT_SCRIPT[lang]}`);
    add(lang);
    add(SOURCE_LANG);
    return chain;
  }

  /** 'rtl' for right-to-left languages/scripts, else 'ltr'. */
  function textDirection(tag) {
    const canon = canonicalizeLang(tag);
    if (!canon) return 'ltr';
    const parts = canon.split('-');
    const script = parts.slice(1).find(p => p.length === 4);
    if (script) return _RTL_SCRIPTS.has(script) ? 'rtl' : 'ltr';
    return _RTL_LANGUAGES.has(parts[0]) ? 'rtl' : 'ltr';
  }

  // ── Shipped interface languages (i18n/index.json) ─────────────────────────
  let _manifest = [{ code: SOURCE_LANG, name_native: 'English' }];
  let _manifestLoaded = false;

  async function _loadManifest() {
    if (_manifestLoaded) return _manifest;
    try {
      const resp = await fetch('i18n/index.json', { cache: 'no-store' });
      if (resp.ok) {
        const data = await resp.json();
        const langs = Array.isArray(data && data.languages) ? data.languages : [];
        const clean = langs.filter(l => l && canonicalizeLang(l.code) === l.code);
        if (clean.some(l => l.code === SOURCE_LANG)) _manifest = clean;
      }
    } catch (_) { /* English only */ }
    _manifestLoaded = true;
    return _manifest;
  }

  /** Shipped UI languages: [{code, name_native, machine_translated, reviewed}]. */
  function getUiLanguages() {
    return _manifest.map(l => Object.assign({}, l));
  }

  // ── UI-string dictionaries ────────────────────────────────────────────────
  let _dicts = [];          // most specific first; always ends with en
  let _uiLang = SOURCE_LANG;
  let _dictGen = 0;
  const _dictCache = new Map();

  async function _fetchDict(lang) {
    if (_dictCache.has(lang)) return _dictCache.get(lang);
    let out = {};
    try {
      const resp = await fetch(`i18n/${encodeURIComponent(lang)}.json`, { cache: 'no-store' });
      if (resp.ok) {
        const data = await resp.json();
        if (data && typeof data === 'object') {
          // Only string values are messages; '_meta' and anything else is not.
          for (const [k, v] of Object.entries(data)) {
            if (typeof v === 'string' && k[0] !== '_') out[k] = v;
          }
        }
      }
    } catch (_) { out = {}; }
    _dictCache.set(lang, out);
    return out;
  }

  /** Load the dictionaries for ``lang``. Returns false if a later call won. */
  async function _loadDicts(lang) {
    const gen = ++_dictGen;
    await _loadManifest();
    const shipped = new Set(_manifest.map(l => l.code));
    const chain = fallbackChain(lang).filter(c => shipped.has(c) || c === SOURCE_LANG);
    const dicts = [];
    for (const code of chain) dicts.push(await _fetchDict(code));
    if (gen !== _dictGen) return false;   // superseded by a newer language switch
    _dicts = dicts;
    // The language actually shown: the most specific tag with a dictionary
    // (a setting for an unshipped language renders, and is tagged, as English).
    _uiLang = chain[0] || SOURCE_LANG;
    return true;
  }

  function _lookup(key) {
    for (const d of _dicts) {
      const v = d[key];
      if (typeof v === 'string') return v;
    }
    return null;
  }

  // ── Message formatting (ICU subset: {x}, plural, select) ─────────────────
  const _pluralRulesCache = new Map();
  function _pluralCategory(lang, n) {
    let pr = _pluralRulesCache.get(lang);
    if (!pr) {
      try { pr = new Intl.PluralRules(lang); } catch (_) { pr = new Intl.PluralRules(SOURCE_LANG); }
      _pluralRulesCache.set(lang, pr);
    }
    return pr.select(n);
  }

  function _formatNumber(lang, n) {
    try { return new Intl.NumberFormat(lang).format(n); } catch (_) { return String(n); }
  }

  /** Index of the '}' matching the '{' at ``open`` (or -1). */
  function _matchBrace(s, open) {
    let depth = 0;
    for (let i = open; i < s.length; i++) {
      if (s[i] === '{') depth++;
      else if (s[i] === '}') { depth--; if (depth === 0) return i; }
    }
    return -1;
  }

  /** Parse "one {…} other {…}" option lists into {selector: body}. */
  function _parseOptions(s) {
    const opts = {};
    let i = 0;
    while (i < s.length) {
      while (i < s.length && /\s/.test(s[i])) i++;
      const m = /^(=\d+|[A-Za-z_][\w-]*)\s*\{/.exec(s.slice(i));
      if (!m) break;
      const open = i + m[0].length - 1;
      const close = _matchBrace(s, open);
      if (close < 0) break;
      opts[m[1]] = s.slice(open + 1, close);
      i = close + 1;
    }
    return opts;
  }

  /**
   * Format an ICU-subset message. ``hashValue`` fills '#' inside plurals.
   * Simple placeholders named in ``keep`` (a Set) are left as "{name}" for
   * tRich to fill as DOM nodes; their values still drive plural/select.
   */
  function formatMessage(msg, vars, lang, hashValue, keep) {
    const v = (vars && typeof vars === 'object') ? vars : {};
    const loc = lang || _uiLang;
    let out = '';
    let i = 0;
    while (i < msg.length) {
      const ch = msg[i];
      if (ch === '#' && hashValue !== undefined) {
        out += _formatNumber(loc, hashValue);
        i++;
        continue;
      }
      if (ch !== '{') { out += ch; i++; continue; }
      const close = _matchBrace(msg, i);
      if (close < 0) { out += msg.slice(i); break; }
      const inner = msg.slice(i + 1, close);
      const m = /^\s*(\w+)\s*(?:,\s*(plural|select)\s*,([\s\S]*))?$/.exec(inner);
      if (!m) {
        out += msg.slice(i, close + 1);   // not a placeholder: keep literally
      } else if (!m[2]) {
        out += (keep && keep.has(m[1])) ? `{${m[1]}}`
          : (v[m[1]] != null) ? String(v[m[1]]) : `{${m[1]}}`;
      } else {
        const opts = _parseOptions(m[3]);
        const val = v[m[1]];
        let body;
        if (m[2] === 'plural') {
          const n = Number(val);
          if (Number.isFinite(n)) {
            body = opts[`=${n}`];
            if (body == null) body = opts[_pluralCategory(loc, n)];
          }
          if (body == null) body = opts.other;
          out += formatMessage(body == null ? '' : body, v, loc, Number.isFinite(Number(val)) ? Number(val) : undefined, keep);
        } else {
          body = opts[String(val)];
          if (body == null) body = opts.other;
          out += formatMessage(body == null ? '' : body, v, loc, hashValue, keep);
        }
      }
      i = close + 1;
    }
    return out;
  }

  /** Translate a UI-string key to PLAIN TEXT (never HTML). */
  function t(key, vars) {
    let s = _lookup(key);
    if (s == null) {
      _log(`[i18n] missing key: ${key}`);
      s = key;
    }
    return formatMessage(s, vars, _uiLang);
  }

  const _HTML_ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', '\'': '&#39;' };
  function _escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => _HTML_ESC[c]); }

  /** Translate and HTML-escape, for a template literal assigned to innerHTML. */
  function tHtml(key, vars) {
    return _escapeHtml(t(key, vars));
  }

  // ── Rich text: allowlisted inline tags + slots, built as DOM nodes ────────
  const RICH_TAGS = new Set(['b', 'strong', 'em', 'i', 'code', 'br']);
  const _RICH_TAG_RE = /<(\/?)([a-z]+)\s*(\/?)>/y;

  /**
   * Tokenize a rich message into a tree:
   *   {type:'text', text} | {type:'slot', name} | {type:'el', tag, children}
   * Only RICH_TAGS with no attributes become elements. Any other '<' is
   * text. {name} tokens become slots; plural/select must already be
   * formatted away (tRich formats first, protecting slots).
   */
  function parseRich(str) {
    const root = { type: 'el', tag: null, children: [] };
    const stack = [root];
    let text = '';
    const flush = () => {
      if (text) { stack[stack.length - 1].children.push({ type: 'text', text }); text = ''; }
    };
    let i = 0;
    while (i < str.length) {
      const ch = str[i];
      if (ch === '<') {
        _RICH_TAG_RE.lastIndex = i;
        const m = _RICH_TAG_RE.exec(str);
        if (m && RICH_TAGS.has(m[2])) {
          flush();
          const [whole, closing, tag, selfClose] = m;
          if (tag === 'br') {
            if (!closing) stack[stack.length - 1].children.push({ type: 'el', tag: 'br', children: [] });
          } else if (closing) {
            // Close the nearest matching open tag; a stray closer is dropped.
            for (let d = stack.length - 1; d > 0; d--) {
              if (stack[d].tag === tag) { stack.length = d; break; }
            }
          } else if (!selfClose) {
            const node = { type: 'el', tag, children: [] };
            stack[stack.length - 1].children.push(node);
            stack.push(node);
          }
          i += whole.length;
          continue;
        }
        text += ch; i++;
        continue;
      }
      if (ch === '{') {
        const m = /^\{(\w+)\}/.exec(str.slice(i));
        if (m) {
          flush();
          stack[stack.length - 1].children.push({ type: 'slot', name: m[1] });
          i += m[0].length;
          continue;
        }
      }
      text += ch; i++;
    }
    flush();
    return root.children;
  }

  function _buildRich(parent, nodes, slots, vars) {
    for (const n of nodes) {
      if (n.type === 'text') {
        parent.appendChild(document.createTextNode(n.text));
      } else if (n.type === 'slot') {
        if (slots[n.name]) {
          parent.appendChild(slots[n.name]);
          delete slots[n.name];
        } else if (vars && vars[n.name] != null) {
          parent.appendChild(document.createTextNode(String(vars[n.name])));
        } else {
          parent.appendChild(document.createTextNode(`{${n.name}}`));
        }
      } else {
        const el = document.createElement(n.tag);
        _buildRich(el, n.children, slots, vars);
        parent.appendChild(el);
      }
    }
  }

  function _ownSlots(node) {
    const slots = {};
    node.querySelectorAll('[data-i18n-slot]').forEach((s) => {
      // A slot belongs to its nearest rich ancestor only.
      let p = s.parentElement;
      while (p && p !== node && !p.hasAttribute('data-i18n-rich')) p = p.parentElement;
      if (p === node) slots[s.getAttribute('data-i18n-slot')] = s;
    });
    return slots;
  }

  /**
   * Render translation ``key`` into ``node`` with allowlisted inline markup.
   * Child elements marked data-i18n-slot="name" are moved (not recreated) to
   * where {name} appears, so ids and listeners on them survive. ``vars`` fill
   * plain {name} placeholders as text.
   */
  function tRich(node, key, vars) {
    if (!node) return;
    const slots = _ownSlots(node);
    const textVars = Object.assign({}, vars || {});
    // Format plurals/selects first, but leave every simple placeholder as
    // "{name}" so values are inserted as text nodes, never parsed as markup.
    const keep = new Set([...Object.keys(slots), ...Object.keys(textVars)]);
    let s = _lookup(key);
    if (s == null) { _log(`[i18n] missing key: ${key}`); s = key; }
    const formatted = formatMessage(s, textVars, _uiLang, undefined, keep);
    const tree = parseRich(formatted);
    for (const sl of Object.values(slots)) sl.remove();
    while (node.firstChild) node.removeChild(node.firstChild);
    _buildRich(node, tree, slots, textVars);
    // A slot the translation forgot is appended so its element (and id)
    // never disappears from the DOM; the parity test should prevent this.
    for (const sl of Object.values(slots)) {
      node.appendChild(document.createTextNode(' '));
      node.appendChild(sl);
    }
  }

  /** Apply data-i18n* attributes under ``root`` (default: whole document). */
  function applyStaticI18n(root) {
    const scope = root || document;
    if (!scope.querySelectorAll) return;
    scope.querySelectorAll('[data-i18n]').forEach((node) => {
      const v = t(node.getAttribute('data-i18n'));
      if (node.textContent !== v) node.textContent = v;
    });
    scope.querySelectorAll('[data-i18n-rich]').forEach((node) => {
      tRich(node, node.getAttribute('data-i18n-rich'));
    });
    scope.querySelectorAll('[data-i18n-title]').forEach((node) => {
      node.title = t(node.getAttribute('data-i18n-title'));
    });
    scope.querySelectorAll('[data-i18n-aria]').forEach((node) => {
      node.setAttribute('aria-label', t(node.getAttribute('data-i18n-aria')));
    });
    scope.querySelectorAll('[data-i18n-placeholder]').forEach((node) => {
      node.setAttribute('placeholder', t(node.getAttribute('data-i18n-placeholder')));
    });
    scope.querySelectorAll('[data-i18n-alt]').forEach((node) => {
      node.setAttribute('alt', t(node.getAttribute('data-i18n-alt')));
    });
    // Custom tooltip attribute used by .help-tip elements.
    scope.querySelectorAll('[data-i18n-data-tip]').forEach((node) => {
      node.setAttribute('data-tip', t(node.getAttribute('data-i18n-data-tip')));
    });
  }

  /** Set <html lang> and dir for the active interface language. */
  function _applyDocumentLang(lang) {
    try {
      const html = document.documentElement;
      if (!html) return;
      html.setAttribute('lang', lang);
      html.setAttribute('dir', textDirection(lang));
    } catch (_) { }
  }

  // ── Bird-name translations ────────────────────────────────────────────────
  let _speciesMap = null;   // null = identity (English / not loaded yet)
  let _speciesLang = SOURCE_LANG;
  let _speciesGen = 0;
  let _speciesInflight = null;   // { lang, promise } for the newest request

  /** Sync label resolver used by render paths. Identity before load. */
  function trSpecies(name) {
    if (!_speciesMap) return name;
    return _speciesMap[name] || name;
  }

  async function _fetchSpeciesMap(lang) {
    if (!lang || lang === SOURCE_LANG) return null;
    try {
      if (typeof hasPywebviewApi === 'undefined' || !hasPywebviewApi ||
          !window.pywebview?.api?.get_bird_name_translations) {
        console.warn(`[i18n] no species translations for '${lang}': bridge method ` +
          'get_bird_name_translations is missing — the app is running code from before i18n. ' +
          'Restart from the updated build.');
        return null;
      }
      const res = await window.pywebview.api.get_bird_name_translations(lang);
      if (res && res.success && res.map && typeof res.map === 'object') return res.map;
      console.warn(`[i18n] species translations request failed for '${lang}':`,
        (res && res.error) || 'empty response');
    } catch (e) { console.warn('[i18n] species translations load failed:', e); }
    return null;
  }

  /**
   * Load (or clear) the species map for ``lang``. Concurrent calls for the
   * same language share one request; a call for a different language
   * supersedes any in-flight one, whose result is then discarded so a slow
   * earlier response can never overwrite a newer choice. Resolves to true
   * when this call's map was applied (and 'kestrel:species-lang-ready' fired).
   */
  function loadSpeciesTranslations(lang) {
    const code = canonicalizeLang(lang || SOURCE_LANG) || SOURCE_LANG;
    if (_speciesInflight && _speciesInflight.lang === code) return _speciesInflight.promise;
    const gen = ++_speciesGen;
    const promise = (async () => {
      const map = await _fetchSpeciesMap(code);
      if (gen !== _speciesGen) return false;
      _speciesMap = map;
      _speciesLang = code;
      _speciesInflight = null;
      _dispatch('kestrel:species-lang-ready', { lang: code });
      return true;
    })();
    _speciesInflight = { lang: code, promise };
    return promise;
  }

  // ── Language state ─────────────────────────────────────────────────────────
  // Two independent settings: 'language' = interface (UI strings),
  // 'bird_name_language' = species labels.

  /** Active interface language (canonical BCP-47; 'en' when unset/invalid). */
  function getLanguage() {
    return canonicalizeLang(_readSetting('language', SOURCE_LANG)) || SOURCE_LANG;
  }

  /** Active bird-name language (canonical BCP-47; 'en' when unset/invalid). */
  function getBirdNameLanguage() {
    return canonicalizeLang(_readSetting('bird_name_language', SOURCE_LANG)) || SOURCE_LANG;
  }

  async function _persistLanguageSetting(key, code) {
    if (typeof mergeSetting === 'function') {
      mergeSetting(key, code);
    } else {
      await _writeSetting(key, code);
    }
  }

  async function _applyUiLanguage(code) {
    if (!(await _loadDicts(code))) return false;
    _applyDocumentLang(_uiLang);
    applyStaticI18n();
    _dispatch('kestrel:language-changed', { lang: code });
    return true;
  }

  /** Called once after settings hydrate: load dictionaries + species map. */
  async function initI18n() {
    await _applyUiLanguage(getLanguage());
    await loadSpeciesTranslations(getBirdNameLanguage());
  }

  /** Change the interface language: persist, reload, re-apply, notify. */
  async function setLanguage(lang) {
    const code = canonicalizeLang(lang);
    if (!code) return;
    await _persistLanguageSetting('language', code);
    await _applyUiLanguage(code);
  }

  /** Change the bird-name language: persist, reload the map, notify. */
  async function setBirdNameLanguage(lang) {
    const code = canonicalizeLang(lang);
    if (!code) return;
    await _persistLanguageSetting('bird_name_language', code);
    if (await loadSpeciesTranslations(code)) {
      _dispatch('kestrel:species-lang-changed', { lang: code });
    }
  }

  // Expose the small public surface as globals, matching the other modules.
  window.t = t;
  window.tHtml = tHtml;
  window.tRich = tRich;
  window.applyStaticI18n = applyStaticI18n;
  window.trSpecies = trSpecies;
  window.getLanguage = getLanguage;
  window.setLanguage = setLanguage;
  window.getBirdNameLanguage = getBirdNameLanguage;
  window.setBirdNameLanguage = setBirdNameLanguage;
  window.getUiLanguages = getUiLanguages;
  window.initI18n = initI18n;
  window.loadSpeciesTranslations = loadSpeciesTranslations;
  window.canonicalizeLang = canonicalizeLang;
  window.langFallbackChain = fallbackChain;
  window.langTextDirection = textDirection;
  // Pure helpers, exposed for the engine unit tests.
  window.__kestrelI18n = {
    formatMessage, parseRich, canonicalizeLang, fallbackChain, textDirection, RICH_TAGS,
    activeUiLanguage: () => _uiLang,
  };
})();
