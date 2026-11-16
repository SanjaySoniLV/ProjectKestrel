// ── i18n engine ─────────────────────────────────────────────────────────────
// Display-language support for Project Kestrel. Two layers:
//
//   UI strings  — static chrome marked with data-i18n / data-i18n-title /
//                 data-i18n-aria attributes, translated by applyStaticI18n()
//                 from dictionaries at analyzer/i18n/<lang>.json. 'en' is the
//                 source of truth; other languages override keys on top of it.
//                 Only English ships today — adding a UI language is a pure
//                 data drop (one JSON file), no code change.
//
//   Bird names  — species labels stay stored in canonical English everywhere
//                 (CSV, scene JSON, XMP, search). trSpecies() resolves a label
//                 to the active language at render time, falling back to
//                 English for names the language file lacks. The maps are
//                 built by tools/build_bird_names_i18n.py from Multiling IOC
//                 and served through get_bird_name_translations().
//
// This file must stay load-order independent: it is included before
// settings.js/state.js in visualizer.html and in culling.html (where only the
// identity fallbacks are used), so it reads settings itself and every hook
// guards with `typeof` where it touches other modules' globals.
(function () {
  'use strict';

  const SETTINGS_KEY = 'kestrel-webviz-settings-v1';

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

  // ── UI-string dictionaries ────────────────────────────────────────────────
  let _enDict = {};        // English source of truth
  let _activeDict = {};    // Active language ({} when English)
  let _dictLang = null;    // Which language _activeDict is for

  async function _fetchDict(lang) {
    try {
      const resp = await fetch(`i18n/${encodeURIComponent(lang)}.json`, { cache: 'no-store' });
      if (!resp.ok) return {};
      const data = await resp.json();
      return (data && typeof data === 'object') ? data : {};
    } catch (_) { return {}; }
  }

  async function _loadDicts(lang) {
    _enDict = await _fetchDict('en');
    _activeDict = lang && lang !== 'en' ? await _fetchDict(lang) : {};
    _dictLang = lang || 'en';
  }

  /** Translate a UI-string key; ``vars`` interpolates {name} placeholders. */
  function t(key, vars) {
    let s = _activeDict[key] ?? _enDict[key];
    if (s == null) {
      _log(`[i18n] missing key: ${key}`);
      s = key;
    }
    if (vars && typeof vars === 'object') {
      s = String(s).replace(/\{(\w+)\}/g, (m, name) =>
        (vars[name] != null) ? String(vars[name]) : m);
    }
    return s;
  }

  /** Apply data-i18n* attributes under ``root`` (default: whole document). */
  function applyStaticI18n(root) {
    const scope = root || document;
    if (!scope.querySelectorAll) return;
    scope.querySelectorAll('[data-i18n]').forEach((node) => {
      const v = t(node.getAttribute('data-i18n'));
      if (node.textContent !== v) node.textContent = v;
    });
    scope.querySelectorAll('[data-i18n-title]').forEach((node) => {
      node.title = t(node.getAttribute('data-i18n-title'));
    });
    scope.querySelectorAll('[data-i18n-aria]').forEach((node) => {
      node.setAttribute('aria-label', t(node.getAttribute('data-i18n-aria')));
    });
  }

  // ── Bird-name translations ────────────────────────────────────────────────
  let _speciesMap = null;   // null = identity (English / not loaded yet)
  let _speciesLang = 'en';
  let _speciesLoadPromise = null;

  /** Sync label resolver used by render paths. Identity before load. */
  function trSpecies(name) {
    if (!_speciesMap) return name;
    return _speciesMap[name] || name;
  }

  async function _fetchSpeciesMap(lang) {
    if (!lang || lang === 'en') return null;
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

  /** Load (or clear) the species map for ``lang``; fires 'kestrel:species-lang-ready'. */
  function loadSpeciesTranslations(lang) {
    const code = (lang || 'en').toString().trim().toLowerCase();
    if (_speciesLoadPromise) return _speciesLoadPromise;
    _speciesLoadPromise = (async () => {
      const map = await _fetchSpeciesMap(code);
      _speciesMap = map;
      _speciesLang = code;
      _speciesLoadPromise = null;
      try {
        document.dispatchEvent(new CustomEvent('kestrel:species-lang-ready', { detail: { lang: code } }));
      } catch (_) { }
    })();
    return _speciesLoadPromise;
  }

  // ── Language state ─────────────────────────────────────────────────────────

  /** Active display language ('en' when unset/invalid). */
  function getLanguage() {
    const lang = _readSetting('language', 'en');
    return (typeof lang === 'string' && /^[a-z]{2}(-[a-z]{2})?$/.test(lang)) ? lang : 'en';
  }

  /** Called once after settings hydrate: load dictionaries + species map. */
  async function initI18n() {
    const lang = getLanguage();
    await _loadDicts(lang);
    applyStaticI18n();
    await loadSpeciesTranslations(lang);
  }

  /** Change the display language: persist, reload, re-apply, notify. */
  async function setLanguage(lang) {
    const code = (lang || 'en').toString().trim().toLowerCase();
    if (!/^[a-z]{2}(-[a-z]{2})?$/.test(code)) return;
    if (typeof mergeSetting === 'function') {
      mergeSetting('language', code);
    } else {
      await _writeSetting('language', code);
    }
    await _loadDicts(code);
    applyStaticI18n();
    await loadSpeciesTranslations(code);
    try {
      document.dispatchEvent(new CustomEvent('kestrel:language-changed', { detail: { lang: code } }));
    } catch (_) { }
  }

  // Expose the small public surface as globals, matching the other modules.
  window.t = t;
  window.applyStaticI18n = applyStaticI18n;
  window.trSpecies = trSpecies;
  window.getLanguage = getLanguage;
  window.setLanguage = setLanguage;
  window.initI18n = initI18n;
  window.loadSpeciesTranslations = loadSpeciesTranslations;
})();
