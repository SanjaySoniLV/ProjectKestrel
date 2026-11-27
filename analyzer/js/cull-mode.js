// Cull mode: a full-screen mode of the main window for a fast accept / reject
// decision on every photo of one folder. Entered from the folder action
// "Enter Cull mode"; Exit (top right) returns to the library.
//
// Decisions live where the rest of the app reads them: `culled` and
// `culled_origin` on the shared `rows`. Kestrel's suggestions are origin
// 'auto' (dotted ring), the user's own decisions 'manual' and confirmed
// suggestions 'verified' (solid ring). Pass state (starting picks, per-scene
// cuts, confirmed scenes, finished passes) is in .kestrel/cull_session.json
// through the bridge; js/cull-flow.js owns the pass dialogs, js/cull-logic.js
// the rules.
(function () {
  const TEXT = {
    title: 'Cull mode',
    passBadge: 'Pass {n} ▾',
    passBadgeView: 'Pass {n} · read-only ▾',
    passDoneAt: 'Pass {n} finished {time}',
    scenesDone: '{d} of {t} scenes done',
    bySpecies: 'By species',
    byTime: 'By time',
    allScenes: 'All scenes',
    unconfirmed: 'Unconfirmed',
    favorites: 'Favorites',
    favoritesSub: 'across the folder',
    favoritesEmpty: 'Press F on any photo to add it here.',
    startingPicks: 'Starting picks',
    history: 'Culling history',
    finish: 'Finish…',
    exit: 'Exit',
    full: 'Full image',
    crop: 'Bird crop',
    holdToZoom: 'Click and hold to zoom',
    rawZoom: 'RAW Zoom',
    exposure: 'Exposure Adjustment',
    clipped: 'clipped',
    clippedTitle: 'Share of the uncorrected image with blown highlights. Click to show the clipping mask.',
    sharpest: 'Sharpest first',
    capture: 'Capture order',
    accept: 'Accept',
    reject: 'Reject',
    acceptAll: 'Accept all',
    rejectAll: 'Reject all',
    favorite: 'Favorite',
    savedAsYouGo: 'Saved as you go',
    skip: 'Skip',
    confirmNext: 'Confirm and next',
    finishShort: 'Finish',
    readOnlyFoot: 'Read-only: this pass is finished',
    noBird: 'No bird found',
    noBirdBody: "Kestrel didn't detect a bird here, so it has no Quality or stars. Your starting picks {rule} these; check it and decide.",
    yours: 'Your decision',
    confirmed: 'Confirmed',
    suggestion: "Kestrel's suggestion",
    inRejects: 'In Kestrel rejects',
    birds: '{n} birds',
    allConfirmed: 'Every scene is confirmed. Press Finish.',
    readOnlyToast: 'Pass {n} is read-only. Go back to pass {cur} to change decisions.',
    cutMoved: "Moved Kestrel's cut for this scene",
    cutTitle: "Kestrel's cut. Drag to move it.",
    everySceneDone: 'Every scene is confirmed. Finish when you are ready.',
    noLinks: 'No bird links for this photo yet (needs an analysis version 3.0 folder)',
    focusOn: 'Focused on Bird {bird} in {n} photos',
    focusOff: 'Focus on Bird {bird} undone',
    mainBird: 'Main bird set to {i} of {n}',
    unknownSpecies: 'Unknown species',
    keys: {
      photo: 'photo', firstLast: 'first / last', flip: 'accept / reject', fav: 'favorite',
      prevScene: 'previous scene', all: 'whole scene', switchBird: 'switch bird', focus: 'focus bird',
    },
  };
  const fill = (s, p) => String(s).replace(/\{(\w+)\}/g, (_, k) => (p && k in p ? String(p[k]) : ''));
  const L = window.KestrelCullLogic;
  const STRIP_GAP = 12;

  const S = {
    active: false, root: '', el: null,
    session: null, edges: { ...L.DEFAULT_EDGES },
    scope: null,            // Set of file names in play this pass, or null = all
    scenes: [], cur: null, sel: null,
    group: 'species', filter: 'all', order: 'quality', thumb: 'frame',
    favView: false, view: null, notice: '',
    snaps: new Map(), drag: false, zoom: null, rawZoom: 5,
    focus: null, saveTimer: null, sessionTimer: null,
  };

  const el = (sel) => S.el?.querySelector(sel);
  const confirmedSet = () => new Set(S.session?.confirmed || []);
  const viewPass = () => (S.view ? S.session?.passes?.[S.view - 1] || null : null);

  // ── Rows and scenes ────────────────────────────────────────────────────────

  function isNoBird(r) { return !(parseNumber(r.quality) >= 0); }
  function rowTime(r) { return parseCaptureTimeMs(r.capture_time); }

  function liveRows() { return rows.filter(r => r.__rootPath === S.root); }

  /** Rows shown: the live rows in scope, or a finished pass's photos when viewing it. */
  function shownRows() {
    const v = viewPass();
    if (!v) return liveRows().filter(r => !S.scope || S.scope.has(r.filename));
    const live = liveRows().filter(r => r.filename in v.decisions);
    const have = new Set(live.map(r => r.filename));
    const moved = (v.moved_rows || []).filter(m => !have.has(m.filename) && m.filename in v.decisions)
      .map(m => ({ ...m, __rootPath: S.root, __moved: true, crops_json: '' }));
    return live.concat(moved);
  }

  function decisionOf(r) {
    const v = viewPass();
    if (v) return String(v.decisions[r.filename] || '').split(':')[0];
    return r.culled === 'accept' || r.culled === 'reject' ? r.culled : '';
  }
  function originOf(r) { return normalizeCullOrigin(r); }
  function isFirm(r) { return !!viewPass() || ['manual', 'verified'].includes(originOf(r)); }
  function isManual(r) { return originOf(r) === 'manual' && !!decisionOf(r); }

  function photoOf(r) {
    return {
      key: r.filename,
      q: isNoBird(r) ? null : parseNumber(r.quality),
      manual: isManual(r) ? decisionOf(r) : null,
      fav: KestrelFavorites.isFavorite(r),
      time: rowTime(r),
    };
  }

  function sceneSpecies(list) {
    const sd = _scenedata[S.root]?.scenes?.[String(list[0]?.scene_count ?? '')];
    if (sd?.user_tags?.finalized && sd.user_tags.species?.length) return sd.user_tags.species[0];
    const counts = new Map();
    for (const r of list) {
      if (isNoBird(r)) continue;
      const sp = getRowDisplayLabels(r).species;
      if (!sp || sp === 'Unknown' || /^no bird/i.test(sp)) continue;
      counts.set(sp, (counts.get(sp) || 0) + 1);
    }
    let best = '', n = 0;
    for (const [sp, c] of counts) if (c > n) { best = sp; n = c; }
    return best || (list.every(isNoBird) ? TEXT.noBird : TEXT.unknownSpecies);
  }

  function buildScenes() {
    const groups = new Map();
    for (const r of shownRows()) {
      const id = String(r.scene_count ?? '');
      if (!groups.has(id)) groups.set(id, []);
      groups.get(id).push(r);
    }
    const list = [];
    for (const [id, rs] of groups) {
      const times = rs.map(rowTime).filter(Number.isFinite);
      list.push({ id, rows: rs, species: sceneSpecies(rs), time: times.length ? Math.min(...times) : Infinity });
    }
    list.sort((a, b) => a.time - b.time || String(a.id).localeCompare(String(b.id), undefined, { numeric: true }));
    S.scenes = list;
  }

  /** Species groups in first-seen order, with "No bird found" after all of them. */
  function speciesOrder() {
    return [...new Set(S.scenes.map(s => s.species))].sort((a, b) => (a === TEXT.noBird) - (b === TEXT.noBird));
  }

  function orderedScenes() {
    if (S.group === 'time') return S.scenes;
    return speciesOrder().flatMap(sp => S.scenes.filter(s => s.species === sp));
  }
  function openScenes() {
    const done = confirmedSet();
    return orderedScenes().filter(s => !done.has(s.id));
  }

  function timeLabel(ms) {
    if (!Number.isFinite(ms)) return '—';
    return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  // ── Starting picks applied to rows ─────────────────────────────────────────

  function rules() { return S.session.rules; }

  function snapshot(scene) {
    const cut = S.session.cuts?.[scene.id];
    const key = [S.session.current, rules().threshold, rules().min_per_scene, rules().max_per_scene, rules().no_bird, cut ?? '', S.order].join('|');
    const old = S.snaps.get(scene.id);
    if (old && old.key === key && old.n === scene.rows.length) return old;
    const plan = L.planScene(scene.rows.map(photoOf), rules(), cut ?? null, S.order);
    const snap = { key, n: scene.rows.length, order: plan.order, divider: plan.divider };
    S.snaps.set(scene.id, snap);
    return snap;
  }

  function stripRows(scene) {
    if (viewPass()) {
      const q = (r) => (isNoBird(r) ? -1 : parseNumber(r.quality));
      return scene.rows.slice().sort((a, b) => (decisionOf(a) === decisionOf(b) ? q(b) - q(a) : decisionOf(a) === 'accept' ? -1 : 1));
    }
    const byKey = new Map(scene.rows.map(r => [r.filename, r]));
    return snapshot(scene).order.map(k => byKey.get(k)).filter(Boolean);
  }

  /** Re-apply the starting picks to one unconfirmed scene. */
  function applyPicks(scene) {
    if (confirmedSet().has(scene.id)) return;
    const plan = L.planScene(scene.rows.map(photoOf), rules(), S.session.cuts?.[scene.id] ?? null, S.order);
    for (const r of scene.rows) {
      const dec = plan.decisions.get(r.filename);
      if (!dec || isManual(r)) continue;
      r.culled = dec;
      r.culled_origin = 'auto';
    }
  }

  function applyPicksAll() {
    ensureRatingColumns();
    for (const s of S.scenes) applyPicks(s);
    S.snaps.clear();
    queueSave();
  }

  function unconfirm(scene) {
    const done = confirmedSet();
    if (!done.has(scene.id)) return;
    done.delete(scene.id);
    S.session.confirmed = [...done];
    for (const r of scene.rows) if (originOf(r) === 'verified') r.culled_origin = 'auto';
  }

  function counts(list) {
    let a = 0, r = 0, nb = 0;
    for (const row of list) {
      const d = decisionOf(row);
      if (d === 'accept') a++; else r++;
      if (isNoBird(row) && !isFirm(row)) nb++;
    }
    return { a, r, nb };
  }

  // ── Saving ─────────────────────────────────────────────────────────────────

  /** Save the folder's rows soon, whatever the auto-save setting (Cull mode is "saved as you go"). */
  function queueSave() {
    markDirty(S.root);
    clearTimeout(S.saveTimer);
    S.saveTimer = setTimeout(() => { saveCsv().catch(e => console.warn('[cull] save failed', e)); }, 1200);
    queueSessionSave();
  }

  function queueSessionSave() {
    clearTimeout(S.sessionTimer);
    S.sessionTimer = setTimeout(saveSessionNow, 600);
  }

  async function saveSessionNow() {
    clearTimeout(S.sessionTimer);
    if (!S.root || !S.session) return;
    try {
      const res = await window.pywebview.api.save_cull_session(S.root, S.session);
      if (!res?.success) console.warn('[cull] session save failed', res?.error);
    } catch (e) { console.warn('[cull] session save error', e); }
  }

  async function flushSaves() {
    clearTimeout(S.saveTimer);
    if (dirty) await saveCsv();
    await saveSessionNow();
  }

  // ── Decisions ──────────────────────────────────────────────────────────────

  function setDecision(r, state) {
    if (!r) return;
    if (viewPass()) { showToast(fill(TEXT.readOnlyToast, { n: S.view, cur: S.session.current }), 2600); return; }
    ensureRatingColumns();
    r.culled = state;
    r.culled_origin = 'manual';
    queueSave();
    renderAfterDecision(r);
  }
  /** Shift+Z / Shift+C: one decision for every photo in the current scene. */
  function setScene(state) {
    if (S.favView || !S.cur) return;
    if (viewPass()) { showToast(fill(TEXT.readOnlyToast, { n: S.view, cur: S.session.current }), 2600); return; }
    ensureRatingColumns();
    for (const r of stripRows(S.cur)) {
      if (r.__moved) continue;
      r.culled = state;
      r.culled_origin = 'manual';
    }
    queueSave();
    render();
  }
  function flip(r) { if (r) setDecision(r, decisionOf(r) === 'accept' ? 'reject' : 'accept'); }

  function toggleFavorite(r) {
    if (!r || r.__moved) return;
    KestrelFavorites.toggle(r);
    queueSave();
    const card = S.el.querySelector(`.cm-card[data-key="${CSS.escape(r.filename)}"]`);
    const heart = card?.querySelector('.fav-heart');
    if (card) card.classList.toggle('is-favorite', KestrelFavorites.isFavorite(r));
    if (heart) KestrelFavorites.syncButton(heart, r);
    renderAfterDecision(r);
    renderRail();
  }

  function confirmScene(scene) {
    const done = confirmedSet();
    for (const r of scene.rows) if (originOf(r) === 'auto') r.culled_origin = 'verified';
    done.add(scene.id);
    S.session.confirmed = [...done];
    queueSave();
  }

  /** "Confirm all as suggested": confirm every scene still open. */
  function confirmAll() {
    for (const s of openScenes()) confirmScene(s);
    S.notice = '';
  }

  // ── Navigation ─────────────────────────────────────────────────────────────

  function currentList() {
    if (S.favView) return shownRows().filter(r => KestrelFavorites.isFavorite(r));
    return S.cur ? stripRows(S.cur) : [];
  }

  function go(scene, at = 'first') {
    S.favView = false;
    S.cur = scene || null;
    S.zoom = null;
    const list = currentList();
    S.sel = (at === 'last' ? list[list.length - 1] : list[0]) || null;
    render();
    S.el?.querySelector('.cm-rail .is-cur')?.scrollIntoView({ block: 'nearest' });
  }

  function step(n, at = 'first') {
    const ord = S.filter === 'open' ? openScenes() : orderedScenes();
    if (!ord.length) return;
    let i = ord.indexOf(S.cur);
    if (i < 0) i = n > 0 ? -1 : 0;
    go(ord[(i + n + ord.length) % ord.length], at);
  }

  function move(dx, jump) {
    const list = currentList();
    if (!list.length) return;
    const i = list.indexOf(S.sel);
    // Arrows stop at the ends of the scene; only Enter or Tab leaves it.
    const j = jump ? (dx < 0 ? 0 : list.length - 1) : Math.max(0, Math.min(list.length - 1, i + dx));
    if (list[j] === S.sel) return;
    S.sel = list[j];
    refreshSel();
  }

  function isLastScene() {
    if (S.favView || !S.cur) return false;
    const done = confirmedSet();
    const ord = orderedScenes();
    return ord.every(s => s === S.cur || done.has(s.id)) || (S.filter === 'all' && ord[ord.length - 1] === S.cur);
  }

  function confirmNext() {
    if (S.favView || viewPass() || !S.cur) return;
    const wasLast = isLastScene();
    confirmScene(S.cur);
    if (wasLast) { render(); KestrelCullFlow.finish(); return; }
    const ord = orderedScenes();
    const done = confirmedSet();
    const i = ord.indexOf(S.cur);
    const next = ord.slice(i + 1).find(s => !done.has(s.id)) || ord.find(s => !done.has(s.id)) || ord[(i + 1) % ord.length];
    if (S.filter === 'open' && !openScenes().length) S.notice = '';
    go(next);
    if (done.size === S.scenes.length) showToast(TEXT.everySceneDone, 2600);
  }

  // ── Birds ──────────────────────────────────────────────────────────────────

  function switchBird(d) {
    const r = S.sel;
    if (!r || r.__moved) return;
    const st = getRowActiveCropState(r);
    if (st.crops.length < 2) return;
    setRowActiveCropIndex(r, (st.activeIndex + d + st.crops.length) % st.crops.length, st.crops);
    refreshSel();
  }

  function makeMainBird() {
    const r = S.sel;
    if (!r || r.__moved || viewPass()) return;
    const st = getRowActiveCropState(r);
    if (st.crops.length < 2) return;
    if (promoteActiveCropToPrimary(r)) queueSave();
    showToast(fill(TEXT.mainBird, { i: st.activeIndex + 1, n: st.crops.length }), 1800);
    refreshSel();
  }

  /** S: make the active bird the main bird in every photo of the scene it was found in; again to undo. */
  async function focusBird() {
    const r = S.sel;
    if (!r || r.__moved || viewPass() || !S.cur || typeof _psBirdOf !== 'function') return;
    await _psLoadLinks(r.__rootPath);
    const b = _psBirdOf(r, getRowActiveCropState(r).activeIndex);
    if (!b) { showToast(TEXT.noLinks, 2600); return; }
    if (S.focus && S.focus.sceneId === S.cur.id && S.focus.key === b.key) {
      for (const row of S.cur.rows) {
        const prev = S.focus.prev.get(row.filename);
        if (prev == null) continue;
        setRowActiveCropIndex(row, getRowCropRecords(row).findIndex(c => String(c.crop_index) === prev));
        promoteActiveCropToPrimary(row);
      }
      showToast(fill(TEXT.focusOff, { bird: b.bird }), 1800);
      S.focus = null;
    } else {
      const prev = new Map();
      for (const row of S.cur.rows) {
        const hit = _psFindBird(row, b.key);
        if (!hit) continue;
        const primary = getRowCropRecords(row)[getRowPrimaryCropIndex(row)];
        prev.set(row.filename, String(primary?.crop_index ?? ''));
        setRowActiveCropIndex(row, hit.pos);
        promoteActiveCropToPrimary(row);
      }
      S.focus = { sceneId: S.cur.id, key: b.key, prev };
      showToast(fill(TEXT.focusOn, { bird: b.bird, n: prev.size }), 2000);
    }
    refocusScene(S.cur.id);
  }

  /**
   * The focused bird sets each photo's Quality and species, so the scene's
   * starting picks are worked out again (its cut and confirmation are
   * dropped) and it moves to the new bird's species group.
   */
  function refocusScene(sceneId) {
    const scene = S.scenes.find(s => s.id === sceneId);
    if (scene) {
      unconfirm(scene);
      if (S.session.cuts) delete S.session.cuts[sceneId];
      applyPicks(scene);
    }
    const sel = S.sel;
    rebuild();
    S.cur = S.scenes.find(s => s.id === sceneId) || S.cur;
    S.sel = S.cur && S.cur.rows.includes(sel) ? sel : (S.cur ? stripRows(S.cur)[0] : null);
    queueSave();
    render();
  }

  /** Click on a bird box on the full image: make that bird the active one. */
  function pickBird(idx) {
    const r = S.sel;
    if (!r || r.__moved) return;
    const st = getRowActiveCropState(r);
    if (idx < 0 || idx >= st.crops.length || idx === st.activeIndex) return;
    setRowActiveCropIndex(r, idx, st.crops);
    refreshSel();
  }

  // ── Rendering ──────────────────────────────────────────────────────────────

  const SKELETON = `
    <div class="cm-top" role="toolbar">
      <span class="cm-title"></span><span class="cm-folder"></span>
      <button type="button" class="cm-pass" data-act="passmenu"></button>
      <div class="cm-prog"><span class="cm-prog-label"></span><div class="cm-track"><div class="cm-fill"></div></div></div>
      <span class="cm-tally"></span>
      <button type="button" class="k-btn--quiet" data-act="picks"></button>
      <button type="button" class="k-btn--quiet" data-act="history"></button>
      <button type="button" data-act="finish"></button>
      <button type="button" class="k-btn--quiet" data-act="exit"></button>
    </div>
    <aside class="cm-rail">
      <div class="cm-rail-head">
        <div class="k-seg" role="group" data-seg="group"><button type="button" data-v="species"></button><button type="button" data-v="time"></button></div>
        <div class="k-seg" role="group" data-seg="filter"><button type="button" data-v="all"></button><button type="button" data-v="open"></button></div>
      </div>
      <div class="cm-rail-list"></div>
    </aside>
    <div class="cm-main">
      <div class="cm-viewbar hidden"></div>
      <div class="cm-head"></div>
      <div class="cm-stage">
        <div class="cm-pv cm-pv--full" data-hold="1"><span class="cm-lbl"></span><span class="cm-hint"></span></div>
        <div class="cm-pv cm-pv--crop"><span class="cm-lbl"></span></div>
      </div>
      <div class="cm-zbar">
        <label class="cm-zctl"><span class="cm-zlbl"></span><input type="range" class="cm-rawzoom" min="2" max="12" step="0.5"></label>
        <label class="cm-zctl"><span class="cm-zlbl cm-explbl"></span><input type="range" class="cm-exp" min="0" max="100" step="5"><span class="cm-expval"></span></label>
        <button type="button" class="cm-clip" data-act="clip"></button>
      </div>
      <div class="cm-info"></div>
      <div class="cm-stripbar"><span class="cm-stripcount"></span>
        <span class="cm-allbtns"><button type="button" class="k-btn--quiet" data-act="all" data-v="accept"></button><button type="button" class="k-btn--quiet" data-act="all" data-v="reject"></button></span>
        <span class="cm-stripctl">
          <div class="k-seg" role="group" data-seg="order"><button type="button" data-v="quality"></button><button type="button" data-v="time"></button></div>
          <div class="k-seg" role="group" data-seg="thumb"><button type="button" data-v="frame"></button><button type="button" data-v="bird"></button></div>
        </span>
      </div>
      <div class="cm-strip"></div>
    </div>
    <footer class="cm-foot"><div class="cm-keys"></div><span class="cm-saved"></span><span class="cm-footact"></span></footer>`;

  function buildShell() {
    const root = document.createElement('div');
    root.id = 'cullMode';
    root.className = 'cm';
    root.setAttribute('role', 'application');
    root.innerHTML = SKELETON;
    S.el = root;
    el('.cm-title').textContent = TEXT.title;
    el('[data-act="picks"]').innerHTML = `${escapeHtml(TEXT.startingPicks)} ▾`;
    el('[data-act="history"]').textContent = TEXT.history;
    el('[data-act="finish"]').textContent = TEXT.finish;
    el('[data-act="exit"]').innerHTML = `${KestrelIcon('x')} ${escapeHtml(TEXT.exit)}`;
    const segText = { group: [TEXT.bySpecies, TEXT.byTime], filter: [TEXT.allScenes, TEXT.unconfirmed],
      order: [TEXT.sharpest, TEXT.capture], thumb: [TEXT.full, TEXT.crop] };
    for (const [seg, labels] of Object.entries(segText)) {
      el(`[data-seg="${seg}"]`).querySelectorAll('button').forEach((b, i) => { b.textContent = labels[i]; });
    }
    el('.cm-pv--full .cm-lbl').textContent = TEXT.full;
    el('.cm-pv--full .cm-hint').textContent = TEXT.holdToZoom;
    el('.cm-pv--crop .cm-lbl').textContent = TEXT.crop;
    el('.cm-zctl .cm-zlbl').textContent = TEXT.rawZoom;
    el('.cm-clip').title = TEXT.clippedTitle;
    el('.cm-saved').textContent = TEXT.savedAsYouGo;
    el('[data-act="all"][data-v="accept"]').innerHTML = `${escapeHtml(TEXT.acceptAll)} <kbd>Shift</kbd>+<kbd>Z</kbd>`;
    el('[data-act="all"][data-v="reject"]').innerHTML = `${escapeHtml(TEXT.rejectAll)} <kbd>Shift</kbd>+<kbd>C</kbd>`;
    document.body.appendChild(root);
    wireEvents();
  }

  function render() {
    if (!S.el) return;
    renderTop();
    renderRail();
    renderMain();
  }

  function renderTop() {
    const v = viewPass();
    const done = confirmedSet().size;
    const total = S.scenes.length;
    const passes = S.session.passes || [];
    const finished = v || (S.session.ended ? passes[passes.length - 1] : null);
    el('.cm-pass').textContent = fill(v ? TEXT.passBadgeView : TEXT.passBadge, { n: S.view || S.session.current });
    el('.cm-folder').textContent = folderBaseName(S.root);
    el('.cm-prog-label').textContent = finished
      ? fill(TEXT.passDoneAt, { n: finished.number, time: KestrelCullFlow.formatTime(finished.finished_at) })
      : fill(TEXT.scenesDone, { d: done, t: total });
    el('.cm-fill').style.width = `${total ? (100 * (finished ? total : done)) / total : 0}%`;
    const c = counts(shownRows());
    const tally = el('.cm-tally');
    tally.innerHTML = `<span class="cm-a">${c.a}</span> accepted · <span class="cm-r">${c.r}</span> rejected`
      + (c.nb ? ` · <span class="cm-warn" title="${escapeHtml(TEXT.noBird)}">${KestrelIcon('warning')} ${c.nb}</span>` : '');
    el('[data-act="finish"]').disabled = !!v;
    el('[data-act="picks"]').disabled = !!v;
  }

  function railMini(scene) {
    const wrap = document.createElement('div');
    wrap.className = 'cm-mini';
    const list = stripRows(scene);
    for (const r of list.slice(0, 11)) {
      const i = document.createElement('img');
      i.alt = '';
      i.className = `cm-mini-${decisionOf(r) || 'none'}${isNoBird(r) ? ' is-nobird' : ''}`;
      lazyLoadImg(i, () => getBlobUrlForPath(getRowFallbackCropPath(r) || r.export_path, r.__rootPath));
      wrap.appendChild(i);
    }
    if (list.length > 11) {
      const more = document.createElement('span');
      more.className = 'cm-more';
      more.textContent = `+${list.length - 11}`;
      wrap.appendChild(more);
    }
    return wrap;
  }

  function railScene(scene, showSpecies) {
    const done = confirmedSet();
    const row = document.createElement('div');
    row.className = 'cm-sc' + (!S.favView && S.cur === scene ? ' is-cur' : '');
    row.dataset.scene = scene.id;
    const l1 = document.createElement('div');
    l1.className = 'cm-sc-l1';
    l1.textContent = `${timeLabel(scene.time)} · ${scene.rows.length} photos`;
    if (showSpecies) {
      const sp = document.createElement('span');
      sp.className = 'cm-sc-sp';
      sp.textContent = ` · ${scene.species}`;
      l1.appendChild(sp);
    }
    const nb = scene.rows.filter(r => isNoBird(r) && !isFirm(r)).length;
    if (nb) l1.insertAdjacentHTML('beforeend', ` <span class="cm-warn" title="${escapeHtml(TEXT.noBird)}">${KestrelIcon('warning')} ${nb}</span>`);
    if (done.has(scene.id)) l1.insertAdjacentHTML('beforeend', `<span class="cm-ok">${KestrelIcon('check')}</span>`);
    row.append(l1, railMini(scene));
    return row;
  }

  function renderRail() {
    el('[data-seg="group"]').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.v === S.group));
    const filt = el('[data-seg="filter"]');
    filt.querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.v === S.filter));
    const openN = viewPass() ? 0 : openScenes().length;
    filt.querySelector('[data-v="open"]').innerHTML = `${escapeHtml(TEXT.unconfirmed)} <span class="cm-n">${openN}</span>`;

    const list = el('.cm-rail-list');
    list.replaceChildren();
    const done = confirmedSet();
    if (S.notice && !viewPass()) list.appendChild(KestrelCullFlow.noticeEl(S.notice));

    const fav = document.createElement('div');
    fav.className = 'cm-favrow' + (S.favView ? ' is-on' : '');
    fav.dataset.act = 'favview';
    fav.innerHTML = `${KestrelIcon('heart-fill')}<span>${escapeHtml(TEXT.favorites)}</span><span class="cm-n">${KestrelFavorites.count(shownRows())}</span>`;
    list.appendChild(fav);

    const visible = (s) => S.filter !== 'open' || !done.has(s.id) || s === S.cur;
    if (S.group === 'species') {
      for (const sp of speciesOrder()) {
        const ss = S.scenes.filter(s => s.species === sp);
        if (!ss.some(visible)) continue;
        const d = ss.filter(s => done.has(s.id)).length;
        const isCur = !S.favView && S.cur && S.cur.species === sp;
        const head = document.createElement('div');
        head.className = 'cm-sp' + (isCur ? ' is-cur' : '');
        head.dataset.species = sp;
        const th = document.createElement('img');
        th.className = 'cm-sp-th';
        th.alt = '';
        const rep = ss[0].rows.find(r => !isNoBird(r)) || ss[0].rows[0];
        lazyLoadImg(th, () => getBlobUrlForPath(getRowFallbackCropPath(rep) || rep.export_path, rep.__rootPath));
        const txt = document.createElement('div');
        const nm = document.createElement('div');
        nm.className = 'cm-sp-nm';
        nm.textContent = sp;
        const sub = document.createElement('div');
        sub.className = 'cm-sp-sub';
        sub.textContent = `${ss.length} scene${ss.length === 1 ? '' : 's'} · ${ss.reduce((a, s) => a + s.rows.length, 0)} photos`;
        txt.append(nm, sub);
        const ct = document.createElement('span');
        ct.className = 'cm-sp-ct' + (d === ss.length ? ' is-done' : '');
        ct.textContent = `${d}/${ss.length}`;
        head.append(th, txt, ct);
        list.appendChild(head);
        if (isCur || S.filter === 'open') ss.filter(visible).forEach(s => list.appendChild(railScene(s, false)));
      }
    } else {
      orderedScenes().filter(visible).forEach(s => list.appendChild(railScene(s, true)));
    }
    if (S.filter === 'open' && !openScenes().length) {
      const m = document.createElement('div');
      m.className = 'cm-muted cm-pad';
      m.textContent = TEXT.allConfirmed;
      list.appendChild(m);
    }
  }

  function renderMain() {
    const v = viewPass();
    const vb = el('.cm-viewbar');
    vb.classList.toggle('hidden', !v);
    if (v) KestrelCullFlow.fillViewBar(vb, v, S.view, S.session.current);
    renderHead();
    renderStripBar();
    renderStrip();
    refreshSel();
    renderFoot();
  }

  function renderHead() {
    const h = el('.cm-head');
    h.replaceChildren();
    const t = document.createElement('span');
    t.className = 'cm-h';
    const s = document.createElement('span');
    s.className = 'cm-s';
    if (S.favView) {
      t.textContent = TEXT.favorites;
      s.textContent = TEXT.favoritesSub;
    } else if (S.cur) {
      const same = S.scenes.filter(x => x.species === S.cur.species);
      t.textContent = S.cur.species;
      s.textContent = (S.group === 'species' ? `Scene ${same.indexOf(S.cur) + 1} of ${same.length} for this bird · ` : '')
        + `${S.cur.rows.length} photos · ${timeLabel(S.cur.time)}`;
    }
    h.append(t, s);
  }

  function renderStripBar() {
    el('[data-seg="order"]').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.v === S.order));
    el('[data-seg="thumb"]').querySelectorAll('button').forEach(b => b.classList.toggle('on', b.dataset.v === S.thumb));
    S.el.querySelectorAll('[data-act="all"]').forEach((b) => { b.disabled = !!(viewPass() || S.favView || !S.cur); });
    const c = el('.cm-stripcount');
    if (S.favView) { c.textContent = `${currentList().length} ${TEXT.favorites.toLowerCase()}`; return; }
    if (!S.cur) { c.textContent = ''; return; }
    const list = currentList();
    const k = counts(list);
    const nb = list.filter(isNoBird).length;
    c.innerHTML = `${list.length} photos · <span class="cm-a">${k.a} accepted</span> · <span class="cm-r">${k.r} rejected</span>`
      + (nb ? ` · <span class="cm-warn">${KestrelIcon('warning')} ${nb} with no bird</span>` : '');
    const hidden = viewPass() || !S.scope ? 0 : liveRows().filter(r => String(r.scene_count) === S.cur.id && !S.scope.has(r.filename)).length;
    if (hidden) c.insertAdjacentText('beforeend', ` · ${hidden} rejected in pass ${S.session.current - 1}, hidden`);
  }

  function thumbPath(r) {
    if (S.thumb === 'bird' && !isNoBird(r)) {
      const st = r.__moved ? null : getRowActiveCropState(r);
      return st?.crops?.[st.activeIndex]?.crop_path || getRowFallbackCropPath(r) || r.crop_path || r.export_path;
    }
    return r.export_path || r.crop_path;
  }

  function buildCard(r, w) {
    const d = decisionOf(r);
    const card = document.createElement('div');
    card.className = `cm-card cm-thumb is-${d || 'none'}${isFirm(r) ? ' is-firm' : ''}${isNoBird(r) ? ' is-nobird' : ''}${S.sel === r ? ' is-sel' : ''}`;
    card.dataset.key = r.filename;
    card.style.width = `${w}px`;
    const img = document.createElement('img');
    img.alt = r.filename || '';
    img.style.height = `${S.thumb === 'frame' ? Math.round((w * 2) / 3) : w}px`;
    applyThumbnailExposureToImg(img, r);
    lazyLoadImg(img, () => getBlobUrlForPath(thumbPath(r), r.__rootPath));
    card.appendChild(img);
    const meta = document.createElement('div');
    meta.className = 'cm-meta';
    if (isNoBird(r)) {
      meta.innerHTML = `<span class="cm-warn">${KestrelIcon('warning')} ${escapeHtml(TEXT.noBird)}</span>`;
    } else {
      const stars = getRating(r);
      meta.innerHTML = `<span class="cm-stars">${'★'.repeat(stars)}<s>${'★'.repeat(5 - stars)}</s></span><span>${parseNumber(r.quality).toFixed(2)}</span>`;
    }
    card.appendChild(meta);
    if (isNoBird(r)) card.insertAdjacentHTML('beforeend', `<span class="cm-nbtag">${KestrelIcon('warning')}</span>`);
    const birds = r.__moved ? 0 : getRowCropRecords(r).length;
    if (birds > 1) {
      const tag = document.createElement('span');
      tag.className = 'cm-tag';
      tag.textContent = fill(TEXT.birds, { n: birds });
      card.appendChild(tag);
    }
    const v = viewPass();
    if (r.__moved && v && !v.move_undone) {
      const tag = document.createElement('span');
      tag.className = 'cm-tag cm-tag--rej';
      tag.textContent = TEXT.inRejects;
      card.appendChild(tag);
    }
    if (!r.__moved) KestrelFavorites.decorateCard(card, r, () => { queueSave(); renderInfo(); renderRail(); });
    return card;
  }

  function renderStrip() {
    const strip = el('.cm-strip');
    const top = strip.scrollTop;
    strip.replaceChildren();
    const list = currentList();
    if (!list.length) {
      const m = document.createElement('span');
      m.className = 'cm-muted cm-pad';
      m.textContent = S.favView ? TEXT.favoritesEmpty : '';
      strip.appendChild(m);
      return;
    }
    const width = Math.max(320, (strip.clientWidth || (S.el.clientWidth - 320)) - 22);
    const minW = S.thumb === 'frame' ? 96 : 76, maxW = S.thumb === 'frame' ? 176 : 128;
    const lay = L.stripLayout(list.length, width, minW, maxW, STRIP_GAP);
    strip.style.gridTemplateColumns = `repeat(auto-fill, ${lay.w}px)`;
    const cardH = (S.thumb === 'frame' ? Math.round((lay.w * 2) / 3) : lay.w) + 22;
    strip.style.maxHeight = `${3 * cardH + 2 * STRIP_GAP + 25}px`;
    for (const r of list) strip.appendChild(buildCard(r, lay.w));
    if (!S.favView && !viewPass() && S.cur && snapshot(S.cur).divider != null) {
      const div = document.createElement('div');
      div.className = 'cm-divider';
      div.title = TEXT.cutTitle;
      strip.appendChild(div);
      requestAnimationFrame(placeDivider);
    }
    strip.scrollTop = top;
  }

  /** The divider sits in the gap between two cards, so the grid never shifts. */
  function placeDivider() {
    const d = el('.cm-divider');
    if (!d || !S.cur) return;
    const cards = [...S.el.querySelectorAll('.cm-strip .cm-card')];
    const at = snapshot(S.cur).divider;
    if (!cards.length || at == null) { d.remove(); return; }
    let x, c;
    if (at < cards.length) { c = cards[at]; x = c.offsetLeft - STRIP_GAP / 2 - 1; }
    else { c = cards[cards.length - 1]; x = c.offsetLeft + c.offsetWidth + STRIP_GAP / 2 - 1; }
    d.style.left = `${x}px`;
    d.style.top = `${c.offsetTop - 2}px`;
    d.style.height = `${c.offsetHeight + 4}px`;
  }

  function renderAfterDecision(r) {
    const card = S.el.querySelector(`.cm-card[data-key="${CSS.escape(r.filename)}"]`);
    if (card) {
      const d = decisionOf(r);
      card.classList.remove('is-accept', 'is-reject', 'is-none');
      card.classList.add(`is-${d || 'none'}`);
      card.classList.toggle('is-firm', isFirm(r));
      card.classList.toggle('is-nobird', isNoBird(r) && !isFirm(r));
      card.classList.add('is-pop');
      setTimeout(() => card.classList.remove('is-pop'), 240);
    }
    renderTop();
    renderStripBar();
    renderInfo();
    const sc = S.el.querySelector(`.cm-sc[data-scene="${CSS.escape(String(r.scene_count))}"] .cm-mini`);
    if (sc && S.cur) sc.replaceWith(railMini(S.cur));
  }

  /** The full image carries the same accept/reject border as its strip card. */
  function syncPreviewBorder(r) {
    const pv = el('.cm-pv--full');
    const d = r ? decisionOf(r) : '';
    pv.classList.toggle('is-accept', d === 'accept');
    pv.classList.toggle('is-reject', d === 'reject');
    pv.classList.toggle('is-firm', !!r && isFirm(r));
  }

  function renderInfo() {
    const box = el('.cm-info');
    box.replaceChildren();
    const r = S.sel;
    syncPreviewBorder(r);
    if (!r) return;
    const list = currentList();
    const fn = document.createElement('span');
    fn.className = 'cm-fn';
    fn.textContent = r.filename;
    box.appendChild(fn);
    if (!isNoBird(r)) {
      const st = getRating(r);
      box.insertAdjacentHTML('beforeend', `<span class="cm-stars">${'★'.repeat(st)}<s>${'★'.repeat(5 - st)}</s></span>`);
    }
    const q = document.createElement('span');
    q.className = 'cm-q';
    q.textContent = `${isNoBird(r) ? '' : `Quality ${parseNumber(r.quality).toFixed(2)} · `}${list.indexOf(r) + 1} of ${list.length} · ${timeLabel(rowTime(r))}`;
    box.appendChild(q);
    if (!r.__moved) {
      const fav = document.createElement('button');
      fav.type = 'button';
      fav.className = 'scene-info-fav-btn';
      KestrelFavorites.syncButton(fav, r, true);
      fav.addEventListener('click', () => toggleFavorite(r));
      box.appendChild(fav);
    }
    const status = document.createElement('span');
    status.className = 'cm-status';
    if (isFirm(r)) status.textContent = isManual(r) ? TEXT.yours : TEXT.confirmed;
    else if (isNoBird(r)) status.innerHTML = `<span class="cm-warn">${KestrelIcon('warning')} ${escapeHtml(TEXT.noBird)}</span>`;
    else status.textContent = TEXT.suggestion;
    box.appendChild(status);
    const d = decisionOf(r);
    const seg = document.createElement('div');
    seg.className = 'k-seg cm-dec';
    seg.innerHTML = `<button type="button" data-act="set" data-v="accept" class="${d === 'accept' ? 'on accept' : ''}">${KestrelIcon('check')} ${escapeHtml(TEXT.accept)}<kbd>Z</kbd></button>`
      + `<button type="button" data-act="set" data-v="reject" class="${d === 'reject' ? 'on reject' : ''}">${KestrelIcon('x')} ${escapeHtml(TEXT.reject)}<kbd>C</kbd></button>`;
    box.appendChild(seg);
  }

  function renderFoot() {
    const v = viewPass();
    const act = el('.cm-footact');
    if (v) { act.innerHTML = `<span class="cm-muted">${escapeHtml(TEXT.readOnlyFoot)}</span>`; return; }
    act.innerHTML = `<button type="button" class="k-btn--quiet" data-act="skip">${escapeHtml(TEXT.skip)} <kbd>Tab</kbd></button>`
      + `<button type="button" class="primary" data-act="next">${escapeHtml(isLastScene() ? TEXT.finishShort : TEXT.confirmNext)} <kbd>Enter</kbd></button>`;
  }

  function renderKeys() {
    const k = TEXT.keys;
    const kb = (s) => `<kbd>${s}</kbd>`;
    let h = `<span>${kb('←')}${kb('→')} ${k.photo}</span><span>${kb('Ctrl')}+${kb('←')}${kb('→')} ${k.firstLast}</span>`
      + `<span>${kb('Space')} ${k.flip}</span><span>${kb('Shift')}+${kb('Z')}${kb('C')} ${k.all}</span><span>${kb('F')} ${k.fav}</span><span>${kb('Shift')}+${kb('Tab')} ${k.prevScene}</span>`;
    const r = S.sel;
    if (r && !r.__moved && getRowCropRecords(r).length > 1) {
      h += `<span class="cm-birdkeys">${kb('↑')}${kb('↓')} ${k.switchBird}</span><span class="cm-birdkeys">${kb('S')} ${k.focus}</span>`;
    }
    el('.cm-keys').innerHTML = h;
  }

  /** Light update for hover and arrow keys: stage, info, selection ring, keys. */
  function refreshSel() {
    if (!S.el) return;
    S.el.querySelectorAll('.cm-card.is-sel').forEach(c => c.classList.remove('is-sel'));
    const r = S.sel;
    if (r) {
      const card = S.el.querySelector(`.cm-card[data-key="${CSS.escape(r.filename)}"]`);
      if (card) { card.classList.add('is-sel'); keepVisible(card); }
    }
    renderInfo();
    renderKeys();
    KestrelCullStage.show(S, r);
  }

  function keepVisible(card) {
    const strip = el('.cm-strip');
    if (card.offsetTop < strip.scrollTop) strip.scrollTop = card.offsetTop - 6;
    else if (card.offsetTop + card.offsetHeight > strip.scrollTop + strip.clientHeight) {
      strip.scrollTop = card.offsetTop + card.offsetHeight - strip.clientHeight + 6;
    }
  }

  // ── Events ─────────────────────────────────────────────────────────────────

  function rowByKey(key) {
    return currentList().find(r => r.filename === key) || null;
  }

  function onClick(e) {
    if (!e.target.closest('.cm-passmenu, [data-act="passmenu"]')) S.el.querySelector('.cm-passmenu')?.remove();
    const fav = e.target.closest('.fav-heart');
    if (fav) return;
    const card = e.target.closest('.cm-strip .cm-card');
    if (card) { const r = rowByKey(card.dataset.key); S.sel = r; flip(r); refreshSel(); return; }
    const seg = e.target.closest('[data-seg] > button');
    if (seg) { onSeg(seg.parentElement.dataset.seg, seg.dataset.v); return; }
    const sc = e.target.closest('.cm-sc');
    if (sc) { go(S.scenes.find(s => s.id === sc.dataset.scene)); return; }
    const sp = e.target.closest('.cm-sp');
    if (sp) {
      const done = confirmedSet();
      const ss = S.scenes.filter(s => s.species === sp.dataset.species);
      go(ss.find(s => !done.has(s.id)) || ss[0]);
      return;
    }
    const a = e.target.closest('[data-act]');
    if (!a) return;
    const act = a.dataset.act;
    if (act === 'next') confirmNext();
    else if (act === 'skip') step(1);
    else if (act === 'exit') exit();
    else if (act === 'set') setDecision(S.sel, a.dataset.v);
    else if (act === 'all') setScene(a.dataset.v);
    else if (act === 'favview') {
      S.favView = !S.favView;
      S.sel = currentList()[0] || (S.favView ? null : S.sel);
      if (!S.favView && S.cur) S.sel = stripRows(S.cur)[0] || null;
      render();
    } else if (act === 'clip') KestrelCullStage.toggleClip(S);
    else KestrelCullFlow.action(act, a);
  }

  function onSeg(seg, v) {
    if (seg === 'group') S.group = v;
    else if (seg === 'filter') { S.filter = v; if (v === 'all') S.notice = ''; }
    else if (seg === 'order') { S.order = v; S.snaps.clear(); }
    else if (seg === 'thumb') S.thumb = v;
    if (seg === 'order' || seg === 'thumb') {
      try { const st = loadSettings(); st[`cull_mode_${seg}`] = v; saveSettings(st); } catch (e) { console.warn('[cull] could not remember', seg, e); }
    }
    render();
  }

  function onKey(e) {
    if (!S.active) return;
    if (document.querySelector('dialog[open]')) return;
    if (e.target && /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) && e.target.type !== 'range') return;
    if (e.metaKey || e.altKey) return;
    const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;
    const ctrl = e.ctrlKey;
    const r = S.sel;
    const map = {
      ArrowRight: () => move(1, ctrl), ArrowLeft: () => move(-1, ctrl),
      Home: () => move(-1, true), End: () => move(1, true),
      ArrowDown: () => switchBird(1), ArrowUp: () => switchBird(-1),
      ' ': () => flip(r),
      z: () => (e.shiftKey ? setScene('accept') : setDecision(r, 'accept')),
      c: () => (e.shiftKey ? setScene('reject') : setDecision(r, 'reject')),
      f: () => toggleFavorite(r), s: () => { focusBird(); },
      Enter: () => (e.shiftKey ? makeMainBird() : confirmNext()),
      Tab: () => step(e.shiftKey ? -1 : 1),
      Escape: () => KestrelCullStage.endZoom(S),
    };
    const fn = map[k];
    if (!fn || (ctrl && !k.startsWith('Arrow'))) return;
    e.preventDefault();
    e.stopPropagation();
    fn();
  }

  function onMouseOver(e) {
    const card = e.target.closest('.cm-strip .cm-card');
    if (!card || S.drag || S.zoom) return;
    const r = rowByKey(card.dataset.key);
    if (r && r !== S.sel) { S.sel = r; refreshSel(); }
  }

  function onMouseDown(e) {
    if (e.target.closest('.cm-divider')) { S.drag = true; e.preventDefault(); return; }
    if (e.button === 0 && e.target.closest('[data-hold]') && S.sel) {
      e.preventDefault();
      KestrelCullStage.startZoom(S, S.sel, e);
    }
  }

  function onMouseMove(e) {
    if (!S.drag || !S.cur) return;
    const photos = S.cur.rows.map(photoOf);
    const left = [];
    for (const card of S.el.querySelectorAll('.cm-strip .cm-card')) {
      const rc = card.getBoundingClientRect();
      const below = e.clientY > rc.bottom + 4;
      const inRow = e.clientY >= rc.top - 4 && e.clientY <= rc.bottom + 4;
      if (below || (inRow && e.clientX > rc.left + rc.width / 2)) left.push(card.dataset.key);
    }
    const k = L.cutFromLeftKeys(photos, left);
    if (S.session.cuts[S.cur.id] === k) return;
    S.session.cuts[S.cur.id] = k;
    unconfirm(S.cur);
    applyPicks(S.cur);
    queueSave();
    renderStrip();
    renderStripBar();
    renderTop();
  }

  function onMouseUp() {
    if (!S.drag) return;
    S.drag = false;
    showToast(TEXT.cutMoved, 1800);
    renderRail();
  }

  function onResize() { if (S.active) renderStrip(); }

  function wireEvents() {
    S.el.addEventListener('click', onClick);
    S.el.addEventListener('mouseover', onMouseOver);
    S.el.addEventListener('mousedown', onMouseDown);
    window.addEventListener('mousemove', onMouseMove);
    window.addEventListener('mouseup', onMouseUp);
    window.addEventListener('resize', onResize);
    document.addEventListener('keydown', onKey, true);
    KestrelCullStage.wire(S);
  }

  function unwireEvents() {
    window.removeEventListener('mousemove', onMouseMove);
    window.removeEventListener('mouseup', onMouseUp);
    window.removeEventListener('resize', onResize);
    document.removeEventListener('keydown', onKey, true);
  }

  // ── Enter / exit ───────────────────────────────────────────────────────────

  async function loadEdges() {
    try {
      const res = await window.pywebview.api.get_rating_thresholds();
      const t = res?.success ? res.thresholds : null;
      if (t && ['two', 'three', 'four', 'five'].every(k => Number.isFinite(Number(t[k])))) {
        S.edges = { two: Number(t.two), three: Number(t.three), four: Number(t.four), five: Number(t.five) };
      }
    } catch (e) { console.warn('[cull] rating thresholds unavailable', e); }
  }

  /** Recompute scope and scenes from the current rows and session (after enter, reload or a pass change). */
  function rebuild() {
    const passes = S.session.passes || [];
    const prev = S.session.ended ? passes[passes.length - 2] : passes[passes.length - 1];
    S.scope = prev ? new Set(Object.keys(prev.decisions).filter(fn => prev.decisions[fn].startsWith('accept'))) : null;
    if (S.scope) for (const r of liveRows()) if (!(r.filename in prev.decisions)) S.scope.add(r.filename);
    S.snaps.clear();
    buildScenes();
    if (!S.scenes.includes(S.cur)) S.cur = S.scenes.find(s => s.id === S.cur?.id) || orderedScenes()[0] || null;
    if (S.cur && (!S.sel || !S.cur.rows.includes(S.sel))) S.sel = stripRows(S.cur)[0] || null;
  }

  async function loadSession() {
    let session = null;
    try {
      const res = await window.pywebview.api.get_cull_session(S.root);
      if (res?.success) session = res.session;
      else if (res?.error) console.warn('[cull] session load failed', res.error);
    } catch (e) { console.warn('[cull] session load error', e); }
    S.session = session || KestrelCullFlow.newSession(S.edges);
    S.session.cuts = S.session.cuts || {};
    S.session.confirmed = S.session.confirmed || [];
  }

  async function enter(rootPath) {
    if (!window.pywebview?.api?.get_cull_session) { showToast('Cull mode needs the desktop app', 4000); return; }
    if (!rootPath) return;
    if (S.active) await exit();
    if (typeof closeSceneDialog === 'function' && document.getElementById('sceneDlg')?.open) closeSceneDialog();
    S.root = rootPath;
    if (!liveRows().length) { showToast('No photos loaded for this folder', 3000); return; }
    S.active = true;
    S.view = null; S.favView = false; S.filter = 'all'; S.notice = ''; S.cur = null; S.sel = null;
    S.order = getSetting('cull_mode_order', 'quality') === 'time' ? 'time' : 'quality';
    S.thumb = getSetting('cull_mode_thumb', 'frame') === 'bird' ? 'bird' : 'frame';
    ensureRatingColumns();
    await Promise.all([loadEdges(), loadSession()]);
    buildShell();
    document.body.classList.add('cull-mode-on');
    rebuild();
    go(S.cur);
    KestrelCullFlow.onEnter();
  }

  /**
   * Reload the folder and session from disk after the bridge changed them
   * (undo, revert, reset). Callers flush first; pending saves are dropped here
   * so stale in-memory state can't overwrite what the bridge just wrote.
   */
  async function reload() {
    clearTimeout(S.saveTimer);
    clearTimeout(S.sessionTimer);
    await reloadCurrentFolders();
    await loadSession();
    rebuild();
    render();
  }

  async function exit() {
    if (!S.active) return;
    KestrelCullStage.endZoom(S);
    await flushSaves();
    unwireEvents();
    S.el?.remove();
    S.el = null;
    S.active = false;
    document.body.classList.remove('cull-mode-on');
    try { await renderScenes(); } catch (e) { console.warn('[cull] grid refresh failed', e); }
  }

  window.KestrelCullMode = {
    enter, exit, reload, render, rebuild, go, step, flushSaves, queueSave, queueSessionSave, pickBird,
    applyPicksAll, confirmAll, liveRows, shownRows, decisionOf, isNoBird, openScenes, orderedScenes, counts,
    get state() { return S; },
    TEXT,
  };
})();
