// Settings > Keyboard: pick a layout, change keys one row at a time, swap on
// conflict, reset. Edits a draft that is saved with the rest of Settings
// (applySettings calls apply()), so Cancel throws them away.
(function () {
  const K = window.KestrelKeys;
  const TEXT = {
    title: 'Keyboard',
    intro: 'Keys for Cull mode and the scene viewer. Click a key to change it, then press the new one. '
      + 'Shift and Ctrl still work with your keys: Shift with your Accept key accepts the whole scene.',
    layout: 'Layout',
    restore: 'Restore defaults',
    custom: 'Custom',
    press: 'Press a key…',
    add: 'Add key',
    extra: 'Extra key',
    remove: 'Remove {key}',
    reset: 'Reset',
    resetTitle: 'Back to the default keys for this action',
    fixedTitle: 'Always works',
    usedBy: '{key} is used by {other}.',
    swap: 'Swap',
    swapHint: '{other} gets {old} instead.',
    cancel: 'Cancel',
    fixed: '{key} always means {other}. Pick another key.',
    ok: 'OK',
    layoutHint: 'A starting point. You can change any key afterwards.',
    customHint: 'Your own keys. Pick a layout to start again from it.',
  };
  const fill = (s, p) => String(s).replace(/\{(\w+)\}/g, (_, k) => (k in p ? String(p[k]) : ''));
  const isMac = /Mac/i.test(navigator.platform || '');
  const chordLabel = (s) => (isMac ? s.replace('Ctrl', 'Cmd') : s);

  let draft = {};
  let capture = null;   // { id, slot } slot = index into the user's keys, or 'add'
  let pending = null;   // { id, slot, key, other, fixed }

  const $ = (id) => document.getElementById(id);
  const clone = (o) => JSON.parse(JSON.stringify(o || {}));

  function markDirty() { $('keysList').dispatchEvent(new Event('change', { bubbles: true })); }

  function open() {
    draft = clone(K.overrides());
    stopCapture();
    render();
  }

  /** Use the draft now and return it for Settings to write with everything else. */
  function apply() {
    stopCapture();
    return K.save(draft, { persist: false });
  }

  // ── Editing ──────────────────────────────────────────────────────────────
  function setKeys(id, keys) {
    draft[id] = keys.slice(0, K.MAX_KEYS);
  }

  function assign(id, slot, key) {
    const keys = K.userKeys(id, draft);
    if (slot === 'add') keys.push(key); else keys[slot] = key;
    setKeys(id, keys);
  }

  function swap() {
    const { id, slot, key, other } = pending;
    const old = slot === 'add' ? null : K.userKeys(id, draft)[slot];
    const theirs = K.userKeys(other, draft);
    const at = theirs.indexOf(key);
    assign(id, slot, key);
    // Give them our old key only where it means nothing else to them.
    if (old && !K.conflict(other, old, { ...draft, [other]: theirs.filter(k => k !== key) })) theirs[at] = old;
    else theirs.splice(at, 1);
    setKeys(other, theirs);
    pending = null;
    changed();
  }

  function changed() {
    render();
    markDirty();
  }

  function startCapture(id, slot) {
    pending = null;
    capture = { id, slot };
    document.addEventListener('keydown', onCaptureKey, true);
    render();
  }

  function stopCapture() {
    capture = null;
    document.removeEventListener('keydown', onCaptureKey, true);
  }

  function onCaptureKey(e) {
    if (!capture) return;
    e.preventDefault();
    e.stopPropagation();
    if (e.key === 'Escape') { stopCapture(); render(); return; }
    const key = K.keyIds(e)[0];
    if (!key) return;   // a modifier on its own: keep waiting
    const { id, slot } = capture;
    stopCapture();
    if (K.keysFor(id, draft).includes(key)) { render(); return; }
    const hit = K.conflict(id, key, draft);
    if (hit) pending = { id, slot, key, other: hit.id, fixed: hit.fixed };
    else { assign(id, slot, key); markDirty(); }
    render();
  }

  // ── Drawing ──────────────────────────────────────────────────────────────
  function node(tag, cls, text) {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function button(cls, text, onClick, title) {
    const b = node('button', cls, text);
    b.type = 'button';
    if (title) { b.title = title; b.setAttribute('aria-label', title); }
    b.addEventListener('click', onClick);
    return b;
  }

  function fixedChip(text) {
    const c = node('span', 'keys-chip is-fixed', text);
    c.title = TEXT.fixedTitle;
    return c;
  }

  function isCapturing(id, slot) { return !!capture && capture.id === id && capture.slot === slot; }

  function renderChips(a) {
    const box = node('div', 'keys-chips');
    for (const k of K.fixedKeys(a.id)) box.appendChild(fixedChip(K.label(k)));
    if (a.fixedLabel) box.appendChild(fixedChip(chordLabel(a.fixedLabel)));
    const mine = K.userKeys(a.id, draft);
    mine.forEach((k, i) => {
      const chip = node('span', 'keys-chip' + (isCapturing(a.id, i) ? ' is-capturing' : ''));
      chip.appendChild(button('keys-chip-key', isCapturing(a.id, i) ? TEXT.press : K.label(k), () => startCapture(a.id, i)));
      chip.appendChild(button('keys-chip-x', '×', () => { mine.splice(i, 1); setKeys(a.id, mine); changed(); },
        fill(TEXT.remove, { key: K.label(k) })));
      box.appendChild(chip);
    });
    if (mine.length < K.MAX_KEYS) {
      const adding = isCapturing(a.id, 'add');
      const add = button('keys-add' + (adding ? ' is-capturing' : ''),
        adding ? TEXT.press : `+ ${a.kind === 'action' ? TEXT.add : TEXT.extra}`, () => startCapture(a.id, 'add'));
      box.appendChild(add);
    }
    const isDefault = mine.join('\u0001') === K.userKeys(a.id, {}).join('\u0001');
    if (!isDefault) {
      box.appendChild(button('k-btn--quiet keys-reset', TEXT.reset, () => { delete draft[a.id]; changed(); }, TEXT.resetTitle));
    }
    return box;
  }

  function renderConflict() {
    const p = pending;
    const box = node('div', 'keys-conflict');
    const other = K.get(p.other).label;
    if (p.fixed) {
      box.appendChild(node('span', '', fill(TEXT.fixed, { key: K.label(p.key), other })));
      box.appendChild(button('k-btn--sm', TEXT.ok, () => { pending = null; render(); }));
      return box;
    }
    const old = p.slot === 'add' ? null : K.userKeys(p.id, draft)[p.slot];
    let msg = fill(TEXT.usedBy, { key: K.label(p.key), other });
    if (old) msg += ' ' + fill(TEXT.swapHint, { other, old: K.label(old) });
    box.appendChild(node('span', '', msg));
    box.appendChild(button('k-btn--sm primary', TEXT.swap, swap));
    box.appendChild(button('k-btn--sm k-btn--quiet', TEXT.cancel, () => { pending = null; render(); }));
    return box;
  }

  function renderLayout() {
    const sel = $('keysLayout');
    const current = K.presetOf(draft);
    sel.replaceChildren();
    for (const p of K.PRESETS) sel.appendChild(new Option(p.label, p.id));
    if (!current) sel.appendChild(new Option(TEXT.custom, 'custom'));
    sel.value = current || 'custom';
    const preset = K.PRESETS.find(p => p.id === current);
    $('keysLayoutHint').textContent = current ? (preset.hint || TEXT.layoutHint) : TEXT.customHint;
  }

  function render() {
    const list = $('keysList');
    if (!list) return;
    renderLayout();
    list.replaceChildren();
    for (const g of K.GROUPS) {
      const sec = node('div', 'keys-group');
      sec.appendChild(node('h5', 'k-section-title', g.label));
      if (g.hint) sec.appendChild(node('p', 'k-row-hint', g.hint));
      for (const a of K.ACTIONS.filter(x => x.group === g.id)) {
        const row = node('div', 'k-row keys-row');
        const lbl = node('div', 'k-row-label', a.label);
        if (a.note) lbl.appendChild(node('span', 'k-row-hint', a.note));
        row.append(lbl, renderChips(a));
        sec.appendChild(row);
        if (pending && pending.id === a.id) sec.appendChild(renderConflict());
      }
      list.appendChild(sec);
    }
  }

  // ── Wiring ───────────────────────────────────────────────────────────────
  function buildPane() {
    const pane = $('keysPane');
    if (!pane) return;
    const row = node('div', 'k-row');
    const lbl = node('label', 'k-row-label', TEXT.layout);
    lbl.htmlFor = 'keysLayout';
    const hint = node('span', 'k-row-hint', TEXT.layoutHint);
    hint.id = 'keysLayoutHint';
    lbl.appendChild(hint);
    const sel = node('select');
    sel.id = 'keysLayout';
    row.append(lbl, sel);
    const list = node('div', 'keys-list');
    list.id = 'keysList';
    const foot = node('div', 'keys-foot');
    foot.appendChild(button('k-btn--quiet', TEXT.restore, () => {
      draft = {};
      pending = null;
      stopCapture();
      changed();
    }));
    pane.append(node('h4', 'settings-pane-title', TEXT.title), node('p', 'settings-pane-intro', TEXT.intro), row, list, foot);
  }
  buildPane();

  $('keysLayout')?.addEventListener('change', (e) => {
    const p = K.PRESETS.find(x => x.id === e.target.value);
    if (!p) return;
    draft = clone(p.overrides);
    pending = null;
    stopCapture();
    render();
  });
  $('settingsDlg')?.addEventListener('close', () => { stopCapture(); pending = null; });

  window.KestrelKeyboardSettings = { open, apply };
})();
