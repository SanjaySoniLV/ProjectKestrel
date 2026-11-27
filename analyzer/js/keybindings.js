// Keyboard shortcuts for Cull mode and the scene viewer, and the user's own
// bindings for them (Settings > Keyboard). Both key handlers ask resolve()
// which action a key press means instead of matching keys themselves.
//
// Three kinds of action:
//   action  the user's keys replace the defaults (up to MAX_KEYS)
//   nav     the built-in key always works; the user may add extra keys
//   chord   a Ctrl/Cmd shortcut the handler matches itself; the user may add
//           plain extra keys so it is reachable without holding two keys
// Shift and Ctrl are never part of a binding. Handlers read them off the event:
// Shift widens or reverses an action (accept -> accept the whole scene, next
// scene -> previous), and Ctrl only reaches actions marked ctrlOk.
//
// Saved as the `keybindings` setting: { actionId: [keyId, ...] } holding only
// the actions the user changed, so later default changes still reach them.
(function () {
  const SETTING = 'keybindings';
  const MAX_KEYS = 2;
  const BOTH = ['cull', 'viewer'];
  const CULL = ['cull'];
  const VIEWER = ['viewer'];

  // group: where the row sits in Settings. ctx: where the key works.
  const ACTIONS = [
    { id: 'prevPhoto', kind: 'nav', group: 'nav', ctx: BOTH, keys: ['ArrowLeft'], ctrlOk: true, label: 'Previous photo' },
    { id: 'nextPhoto', kind: 'nav', group: 'nav', ctx: BOTH, keys: ['ArrowRight'], ctrlOk: true, label: 'Next photo' },
    { id: 'firstPhoto', kind: 'nav', group: 'nav', ctx: BOTH, keys: ['Home'], label: 'First photo in scene' },
    { id: 'lastPhoto', kind: 'nav', group: 'nav', ctx: BOTH, keys: ['End'], label: 'Last photo in scene' },
    { id: 'nextScene', kind: 'nav', group: 'nav', ctx: BOTH, keys: ['Tab'], ctrlOk: true, label: 'Next scene', note: 'Shift for previous' },
    { id: 'prevScene', kind: 'nav', group: 'nav', ctx: BOTH, keys: [], fixedLabel: 'Shift+Tab', label: 'Previous scene' },
    { id: 'prevBird', kind: 'nav', group: 'nav', ctx: BOTH, keys: ['ArrowUp'], label: 'Previous bird' },
    { id: 'nextBird', kind: 'nav', group: 'nav', ctx: BOTH, keys: ['ArrowDown'], label: 'Next bird' },
    { id: 'close', kind: 'nav', group: 'nav', ctx: BOTH, keys: ['Escape'], label: 'Close or end zoom' },

    { id: 'accept', kind: 'action', group: 'both', ctx: BOTH, keys: ['z', '7'], label: 'Accept', note: 'Shift accepts the whole scene in Cull mode' },
    { id: 'reject', kind: 'action', group: 'both', ctx: BOTH, keys: ['c', '9'], label: 'Reject', note: 'Shift rejects the whole scene in Cull mode' },
    { id: 'favorite', kind: 'action', group: 'both', ctx: BOTH, keys: ['f'], label: 'Favorite' },
    { id: 'focusBird', kind: 'action', group: 'both', ctx: BOTH, keys: ['s'], label: 'Focus the scene on this bird' },

    { id: 'flip', kind: 'action', group: 'cull', ctx: CULL, keys: ['Space'], label: 'Flip accept / reject' },
    { id: 'confirmNext', kind: 'action', group: 'cull', ctx: CULL, keys: ['Enter'], label: 'Confirm and next scene', note: 'Shift makes the bird the main bird' },
    { id: 'acceptAll', kind: 'action', group: 'cull', ctx: CULL, keys: [], fixedLabel: 'Shift+Accept', label: 'Accept whole scene' },
    { id: 'rejectAll', kind: 'action', group: 'cull', ctx: CULL, keys: [], fixedLabel: 'Shift+Reject', label: 'Reject whole scene' },
    { id: 'cullMainBird', kind: 'action', group: 'cull', ctx: CULL, keys: [], fixedLabel: 'Shift+Enter', label: 'Make this the main bird' },

    { id: 'clear', kind: 'action', group: 'viewer', ctx: VIEWER, keys: ['x', '8'], label: 'Undecided' },
    { id: 'rate1', kind: 'action', group: 'viewer', ctx: VIEWER, keys: ['1'], label: '1 star' },
    { id: 'rate2', kind: 'action', group: 'viewer', ctx: VIEWER, keys: ['2'], label: '2 stars' },
    { id: 'rate3', kind: 'action', group: 'viewer', ctx: VIEWER, keys: ['3'], label: '3 stars' },
    { id: 'rate4', kind: 'action', group: 'viewer', ctx: VIEWER, keys: ['4'], label: '4 stars' },
    { id: 'rate5', kind: 'action', group: 'viewer', ctx: VIEWER, keys: ['5'], label: '5 stars' },
    { id: 'openEditor', kind: 'action', group: 'viewer', ctx: VIEWER, keys: ['Space'], label: 'Open in editor' },
    { id: 'mainBird', kind: 'action', group: 'viewer', ctx: VIEWER, keys: ['Enter'], label: 'Make this the main bird' },
    { id: 'birdNumbers', kind: 'action', group: 'viewer', ctx: VIEWER, keys: ['b'], label: 'Show / hide bird numbers' },
    { id: 'confirmSpecies', kind: 'chord', group: 'viewer', ctx: VIEWER, keys: [], fixedLabel: 'Ctrl+R', label: 'Confirm species' },
    { id: 'speciesTag', kind: 'chord', group: 'viewer', ctx: VIEWER, keys: [], fixedLabel: 'Ctrl+T', label: 'Add species tag' },
    { id: 'familyTag', kind: 'chord', group: 'viewer', ctx: VIEWER, keys: [], fixedLabel: 'Ctrl+Shift+T', label: 'Add family tag' },
    { id: 'clearTags', kind: 'chord', group: 'viewer', ctx: VIEWER, keys: [], fixedLabel: 'Ctrl+Shift+C', label: 'Clear all tags' },
    { id: 'resetTags', kind: 'chord', group: 'viewer', ctx: VIEWER, keys: [], fixedLabel: 'Ctrl+Shift+R', label: 'Reset and reassign tags' },
  ];
  const BY_ID = new Map(ACTIONS.map(a => [a.id, a]));

  const GROUPS = [
    { id: 'nav', label: 'Moving around', hint: 'These keys always work. Add an extra key for any of them.' },
    { id: 'both', label: 'Cull mode and scene viewer' },
    { id: 'cull', label: 'Cull mode' },
    { id: 'viewer', label: 'Scene viewer' },
  ];

  // Layouts are starting points: each is a full set of overrides that the user
  // can then change row by row. Keys are chosen so neither hand has to cross
  // over to the other side of the keyboard; Num keys only exist on a numpad,
  // so each layout also works without one.
  const PRESETS = [
    { id: 'default', label: 'Default', overrides: {} },
    { id: 'right', label: 'Right hand on the keyboard', hint: 'Mouse in your left hand. Decisions on M , . and the numpad.',
      overrides: {
        accept: ['m', 'Num7'], clear: [',', 'Num8'], reject: ['.', 'Num9'],
        favorite: ['j'], focusBird: ['l'], birdNumbers: ['n'], flip: ['Space', 'Num0'],
        nextScene: [']', 'Num+'], prevScene: ['[', 'Num-'], close: ['Backspace'],
        confirmSpecies: ['p'],
      } },
    { id: 'left', label: 'Left hand on the keyboard', hint: 'Mouse in your right hand. Browse with A D Q E, confirm with G.',
      overrides: {
        prevPhoto: ['a'], nextPhoto: ['d'], prevBird: ['q'], nextBird: ['e'],
        confirmNext: ['Enter', 'g'], mainBird: ['Enter', 'g'], confirmSpecies: ['r'],
      } },
  ];

  const LABELS = {
    ArrowLeft: '←', ArrowRight: '→', ArrowUp: '↑', ArrowDown: '↓', Escape: 'Esc', Space: 'Space',
    Enter: 'Enter', Tab: 'Tab', Backspace: 'Backspace', Delete: 'Delete', Home: 'Home', End: 'End',
    PageUp: 'Page Up', PageDown: 'Page Down', Insert: 'Insert',
  };
  // Keys that can never be bound: modifiers on their own, and keys the
  // window or OS keeps for itself.
  const UNBINDABLE = new Set(['Shift', 'Control', 'Alt', 'Meta', 'AltGraph', 'CapsLock', 'NumLock',
    'ScrollLock', 'ContextMenu', 'Fn', 'OS', 'Dead', 'Unidentified', 'Process', 'F5', 'F11', 'F12']);
  // Punctuation is read by physical position so Shift does not change it.
  const PUNCT_CODES = { Comma: ',', Period: '.', Slash: '/', Semicolon: ';', Quote: "'", BracketLeft: '[',
    BracketRight: ']', Minus: '-', Equal: '=', Backquote: '`', Backslash: '\\', IntlBackslash: '\\' };
  const NUMPAD_OPS = { NumpadAdd: '+', NumpadSubtract: '-', NumpadMultiply: '*', NumpadDivide: '/', NumpadDecimal: '.' };

  /** Key ids for an event, most specific first: ['Num7', '7'] for numpad 7, ['z'] for Z. */
  function keyIds(e) {
    const code = e.code || '';
    const key = e.key || '';
    const out = [];
    const np = /^Numpad(\d)$/.exec(code);
    if (np) out.push('Num' + np[1]);
    else if (NUMPAD_OPS[code]) out.push('Num' + NUMPAD_OPS[code]);
    let generic;
    if (key === ' ' || code === 'Space') generic = 'Space';
    else if (code === 'NumpadEnter') generic = 'Enter';
    else if (/^Digit\d$/.test(code)) generic = code.slice(5);
    else if (key.length === 1 && /\p{L}/u.test(key)) generic = key.toLowerCase();
    else if (PUNCT_CODES[code] && !np) generic = PUNCT_CODES[code];
    else if (key.length === 1) generic = key.toLowerCase();
    else generic = key;
    if (generic && !UNBINDABLE.has(generic) && !out.includes(generic)) out.push(generic);
    return out.filter(k => !UNBINDABLE.has(k));
  }

  function label(keyId) {
    if (LABELS[keyId]) return LABELS[keyId];
    if (/^Num./.test(keyId)) return 'Num ' + keyId.slice(3);
    return keyId.length === 1 ? keyId.toUpperCase() : keyId;
  }

  // ── Stored overrides ───────────────────────────────────────────────────────
  let _overrides = null;
  let _maps = null;

  /** Keep only known actions and well-formed key lists. */
  function clean(raw) {
    const out = {};
    if (!raw || typeof raw !== 'object') return out;
    for (const [id, keys] of Object.entries(raw)) {
      if (!BY_ID.has(id) || !Array.isArray(keys)) continue;
      const list = [];
      for (const k of keys) {
        if (typeof k === 'string' && k && k.length <= 16 && !UNBINDABLE.has(k) && !list.includes(k)) list.push(k);
      }
      out[id] = list.slice(0, MAX_KEYS);
    }
    return out;
  }

  function overrides() {
    if (_overrides === null) {
      _overrides = clean(typeof getSetting === 'function' ? getSetting(SETTING, {}) : {});
    }
    return _overrides;
  }

  /** Re-read the saved bindings (after Settings are loaded from disk). */
  function reload() { _overrides = null; _maps = null; }

  /**
   * Use a full set of overrides straight away and return the cleaned set.
   * persist: false leaves writing the setting to the caller (Settings saves
   * all of its values in one write).
   */
  function save(next, { persist = true } = {}) {
    const cleaned = clean(next);
    for (const [id, keys] of Object.entries(cleaned)) {
      if (sameKeys(keys, userKeys(id, {}))) delete cleaned[id];
    }
    _overrides = cleaned;
    _maps = null;
    if (persist && typeof mergeSetting === 'function') mergeSetting(SETTING, cleaned);
    document.dispatchEvent(new CustomEvent('kestrel:keybindings-changed'));
    return cleaned;
  }

  function sameKeys(a, b) { return a.length === b.length && a.every((k, i) => k === b[i]); }

  /** The keys the user controls for an action (replacements or extras). */
  function userKeys(id, ov = overrides()) {
    const a = BY_ID.get(id);
    if (!a) return [];
    if (Object.prototype.hasOwnProperty.call(ov, id)) return ov[id].slice();
    return a.kind === 'action' ? a.keys.slice() : [];
  }

  /** Every key that triggers an action: built-in keys of nav actions plus the user's keys. */
  function keysFor(id, ov = overrides()) {
    const a = BY_ID.get(id);
    if (!a) return [];
    const fixed = a.kind === 'nav' ? a.keys : [];
    return [...fixed, ...userKeys(id, ov).filter(k => !fixed.includes(k))];
  }

  function fixedKeys(id) {
    const a = BY_ID.get(id);
    return a && a.kind === 'nav' ? a.keys.slice() : [];
  }

  function buildMaps(ov) {
    const maps = { cull: new Map(), viewer: new Map() };
    for (const a of ACTIONS) {
      for (const k of keysFor(a.id, ov)) {
        for (const c of a.ctx) if (!maps[c].has(k)) maps[c].set(k, a.id);
      }
    }
    return maps;
  }

  /**
   * The action a key press means in a context ('cull' or 'viewer'), or null.
   * Returns { id, shift, ctrl }. Alt is never ours; Ctrl/Cmd reaches only
   * ctrlOk actions so system and app chords pass through.
   */
  function resolve(ctx, e) {
    if (e.altKey) return null;
    if (!_maps) _maps = buildMaps(overrides());
    const map = _maps[ctx];
    if (!map) return null;
    for (const k of keyIds(e)) {
      const id = map.get(k);
      if (!id) continue;
      const ctrl = !!(e.ctrlKey || e.metaKey);
      if (ctrl && !BY_ID.get(id).ctrlOk) return null;
      return { id, shift: !!e.shiftKey, ctrl };
    }
    return null;
  }

  /**
   * Who else uses `key` in any context `id` works in, under overrides `ov`.
   * Returns { id, fixed } or null. fixed means it is a built-in key that
   * cannot be moved.
   */
  function conflict(id, key, ov = overrides()) {
    const a = BY_ID.get(id);
    if (!a) return null;
    for (const b of ACTIONS) {
      if (b.id === id || !b.ctx.some(c => a.ctx.includes(c))) continue;
      if (fixedKeys(b.id).includes(key)) return { id: b.id, fixed: true };
      if (keysFor(b.id, ov).includes(key)) return { id: b.id, fixed: false };
    }
    return null;
  }

  /** The preset the overrides match, or null for a custom set. */
  function presetOf(ov = overrides()) {
    const norm = (o) => {
      const out = {};
      for (const a of ACTIONS) out[a.id] = userKeys(a.id, o).join('\u0001');
      return JSON.stringify(out);
    };
    const mine = norm(ov);
    const p = PRESETS.find(x => norm(clean(x.overrides)) === mine);
    return p ? p.id : null;
  }

  // ── Showing keys in the UI ─────────────────────────────────────────────────
  function escapeText(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  /** ' (F)' for a tooltip: the action's first key, or '' when it has none. */
  function titleSuffix(id) {
    const k = primaryKey(id);
    return k ? ` (${label(k)})` : '';
  }

  /**
   * The one key to show where there is room for one: the user's own key when
   * they added one (an extra key on a nav action is the one they reach for),
   * else the first built-in key.
   */
  function primaryKey(id) {
    return userKeys(id)[0] || keysFor(id)[0] || '';
  }

  /** `<kbd>` markup for an action's keys, joined with "or"; '' when it has none. */
  function hint(id, { prefix = '', first = false } = {}) {
    let keys = keysFor(id);
    if (first) keys = keys.length ? [primaryKey(id)] : [];
    const pre = prefix ? `<kbd>${escapeText(prefix)}</kbd>+` : '';
    return keys.map(k => `${pre}<kbd>${escapeText(label(k))}</kbd>`).join(' or ');
  }

  /**
   * Fill every element under root that has data-keys="actionId" with that
   * action's keys. data-keys-prefix adds a modifier (Shift) and data-keys-first
   * shows only the first key, for tight button labels.
   */
  function paint(root = document) {
    root.querySelectorAll('[data-keys]').forEach(node => {
      node.innerHTML = hint(node.dataset.keys, {
        prefix: node.dataset.keysPrefix || '',
        first: node.hasAttribute('data-keys-first'),
      });
    });
    // Extra keys after a fixed one ("Ctrl+R or P"); empty when there are none.
    root.querySelectorAll('[data-keys-or]').forEach(node => {
      const extra = hint(node.dataset.keysOr);
      node.innerHTML = extra ? ` or ${extra}` : '';
    });
  }

  // The scene viewer's shortcut legend, drawn from the current bindings.
  const LEGEND = [
    { title: 'Browse', items: [
      ['prevPhoto|nextPhoto', 'Previous / next photo'],
      ['firstPhoto|lastPhoto', 'First / last photo', 'or <kbd>Ctrl</kbd>/<kbd>Cmd</kbd>+<kbd>←</kbd><kbd>→</kbd>'],
      ['nextScene', 'Next scene'],
      ['', 'Previous scene', '<kbd>Shift</kbd>+<kbd>Tab</kbd>', 'prevScene'],
      ['prevBird|nextBird', 'Switch bird crop'],
      ['', 'Scroll on the preview: zoom into the RAW'],
      ['close', 'Close'],
    ] },
    { title: 'Decide', items: [
      ['accept', 'Accept'], ['clear', 'Undecided'], ['reject', 'Reject'],
      ['rate1|rate5', 'Star rating', '', '', '–'],
      ['', 'Confirm species', '<kbd>Ctrl</kbd>/<kbd>Cmd</kbd>+<kbd>R</kbd>', 'confirmSpecies'],
      ['favorite', 'Favorite'], ['openEditor', 'Open in editor'],
    ] },
    { title: 'Birds and tags', items: [
      ['mainBird', 'Make the active crop the main bird'],
      ['focusBird', 'focus the scene on it (again to undo)', 'or <kbd>Shift</kbd>+click a bird:'],
      ['birdNumbers', 'Show / hide bird numbers'],
      ['', 'Add species tag', '<kbd>Ctrl</kbd>/<kbd>Cmd</kbd>+<kbd>T</kbd>', 'speciesTag'],
      ['', 'Add family tag', '<kbd>Ctrl</kbd>/<kbd>Cmd</kbd>+<kbd>Shift</kbd>+<kbd>T</kbd>', 'familyTag'],
      ['', 'Clear all tags', '<kbd>Ctrl</kbd>/<kbd>Cmd</kbd>+<kbd>Shift</kbd>+<kbd>C</kbd>', 'clearTags'],
      ['', 'Reset and reassign tags', '<kbd>Ctrl</kbd>/<kbd>Cmd</kbd>+<kbd>Shift</kbd>+<kbd>R</kbd>', 'resetTags'],
      ['', 'Change any of these in Settings, Keyboard.'],
    ] },
  ];

  /**
   * Fill the scene viewer legend. Each item is [actions, text, fixedHtml,
   * extraAction, joiner]: actions are '|'-separated ids whose keys lead the
   * line, fixedHtml is static key markup, extraAction adds the user's extra
   * keys after it. Text here is our own constant, never user input.
   */
  function renderLegend(box) {
    box.innerHTML = LEGEND.map(col => {
      const lines = col.items.map(([ids, text, fixed = '', extra = '', join = ' / ']) => {
        const keys = ids ? ids.split('|').map(id => hint(id)).join(join) : '';
        const more = extra ? hint(extra) : '';
        const lead = [keys, fixed].filter(Boolean).join(' ') + (more ? ` or ${more}` : '');
        return `<span>${lead ? lead + ' ' : ''}${escapeText(text)}</span>`;
      }).join('');
      return `<div class="ssl-col"><h4 class="k-section-title">${escapeText(col.title)}</h4>${lines}</div>`;
    }).join('');
  }

  document.addEventListener('kestrel:keybindings-changed', () => paint());

  window.KestrelKeys = {
    ACTIONS, GROUPS, PRESETS, MAX_KEYS,
    get: (id) => BY_ID.get(id),
    keyIds, label, clean, overrides, reload, save, userKeys, keysFor, fixedKeys,
    resolve, conflict, presetOf, hint, paint, titleSuffix, renderLegend,
  };
})();
