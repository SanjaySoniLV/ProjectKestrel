// Cull mode passes: starting picks, Finish, the pass menu and Culling history.
//
// Every pass opens on its starting picks. Finish sends the user through any
// scenes still unconfirmed, then offers to move the rejects and write
// metadata, and to finish culling or start the next pass (which looks again
// at this pass's accepts). Culling history lists finished passes with View
// pass, Undo move (newest move only), Revert to this pass (only while every
// photo of that pass is still on disk) and Reset folder. File moves and the
// session file go through the bridge (cull_session.py).
(function () {
  const TEXT = {
    startTitle: 'Start pass {n}: confirm your starting picks',
    picksTitle: 'Starting picks · pass {n}',
    picksSub1: "Kestrel accepts photos at or above this Quality to start with. You'll confirm or change every one, scene by scene.",
    picksSubN: 'Pass {n} looks again at the {kept} photos you kept, with a higher bar. Your own decisions stay.',
    thrLine: 'Accept Quality <b>{t}</b> and up <span class="cm-muted">(inside the ★{star} band)</span>',
    snapHint: 'Hold <kbd>Shift</kbd> to snap to a star edge',
    projection: '<b>{a}</b> of {total} photos accepted ({pct}%)',
    atLeast: 'Accept at least',
    atLeastSub: 'per scene, sharpest first',
    atMost: 'Keep at most',
    atMostSub: 'per scene; favorites are always kept',
    noLimit: 'No limit',
    noBird: 'Photos with no bird found',
    noBirdSub: '{n} in this folder. They stay marked so you can check them.',
    accept: 'Accept',
    reject: 'Reject',
    startPass: 'Start pass {n}',
    done: 'Done',
    backToLibrary: 'Back to the library',
    started: 'Pass {n} started. Enter confirms a scene; Tab skips it.',
    unconfirmedNotice: "{n} scene{s} still {have} suggestions you haven't confirmed. They're listed here; go through them to finish.",
    confirmAll: 'Confirm all as suggested',
    dismiss: 'Dismiss',
    finishTitle: 'Finish pass {n}',
    finishSub: '{folder} · every scene confirmed',
    accepted: 'accepted',
    rejectedThisPass: 'rejected in this pass',
    favorites: 'favorites',
    moveRejects: 'Move rejected photos to _KESTREL_Rejects',
    moveRejectsSub: 'Undo it any time from Culling history.',
    writeMeta: 'Write photo metadata',
    writeMetaSub: 'Accept / reject labels and star ratings',
    anotherPass: '<b>Another pass?</b> Pass {next} looks again at the {a} photos you kept, with stricter starting picks. You can also start it later.',
    nothingDeleted: 'Nothing is deleted.',
    keepCulling: 'Keep culling',
    finishCulling: 'Finish culling',
    working: 'Working…',
    savingStep: 'Saving decisions',
    metaStep: 'Writing photo metadata',
    moveStep: 'Moving rejects to _KESTREL_Rejects',
    metaSkipped: '{n} photos already had metadata from another app and were skipped. Use Write photo metadata to overwrite them.',
    moveFailed: 'Some rejects could not be moved: {error}',
    finishFailed: 'Finishing the pass failed: {error}',
    endedTitle: 'Pass {n} finished',
    history: 'Culling history',
    historySub: '{folder} · what each pass did',
    historyEmpty: 'No finished passes yet.',
    passFinished: 'finished {time}',
    inProgress: 'in progress',
    scenesConfirmed: '{d} of {t} scenes confirmed',
    passLine: '{a} accepted · {r} rejected',
    movedThere: '{n} rejects in _KESTREL_Rejects · all {n} still there',
    movedMissing: '{n} rejects in _KESTREL_Rejects · some are no longer on disk',
    movedBack: '{n} rejects moved back into the folder',
    leftInPlace: 'Rejects left in place',
    metaWritten: 'metadata written to {n} photos',
    noMeta: 'no metadata written',
    viewPass: 'View pass',
    undoMove: 'Undo move',
    revert: 'Revert to this pass',
    revertTitle: 'Offered while every photo from this pass is still in the folder or in _KESTREL_Rejects',
    reset: 'Reset folder',
    close: 'Close',
    cancel: 'Cancel',
    resetTitle: 'Reset this folder?',
    resetBody: 'Moves all {n} photos back from _KESTREL_Rejects and clears every culling decision. Favorites stay. You start again from pass 1\'s starting picks.',
    resetBodyNone: 'Clears every culling decision. Favorites stay. You start again from pass 1\'s starting picks.',
    revertTitleQ: 'Revert to pass {k}?',
    revertMoves: 'Moves the {n} photos that later passes put in _KESTREL_Rejects back into the folder, and sets every decision to where pass {k} ended.',
    revertNoMoves: 'Sets every decision to where pass {k} ended.',
    revertDiscard: 'The work in {what} is discarded.',
    reverted: 'Reverted to the end of pass {k}',
    resetDone: 'Folder reset. Every photo is back and no decisions are left.',
    undoDone: 'Moved {n} photos back from _KESTREL_Rejects. Their reject decisions stay.',
    failed: 'That did not work: {error}',
    menuFinished: 'Finished {time} · {line}',
    menuView: 'View, read-only',
    menuCurrent: 'In progress · {d} of {t} scenes done',
    menuNone: 'Finished passes appear here.',
    menuHistory: 'Culling history…',
    viewing: '<b>Viewing pass {n}</b> · read-only. {where}',
    viewingMoved: 'Its {n} rejects are in _KESTREL_Rejects.',
    viewingBack: 'Its rejects were moved back.',
    viewingLeft: 'Its rejects were left in place.',
    backToPass: 'Back to pass {n}',
  };
  const fill = (s, p) => String(s).replace(/\{(\w+)\}/g, (_, k) => (p && k in p ? escapeHtml(String(p[k])) : ''));
  const L = window.KestrelCullLogic;
  const CM = () => window.KestrelCullMode;
  const S = () => CM().state;
  // Moved rejects leave the CSV; the pass record keeps enough of each to show it read-only.
  const MOVED_ROW_FIELDS = ['scene_count', 'quality', 'export_path', 'crop_path', 'capture_time', 'species'];
  let dlg = null;
  let pendingRevert = null;

  // ── Session helpers ────────────────────────────────────────────────────────

  function newSession(edges) {
    return {
      version: 1, current: 1, ended: false, started: false,
      rules: { threshold: Number(edges?.three ?? 0.4), min_per_scene: 1, max_per_scene: 0, no_bird: 'reject' },
      cuts: {}, confirmed: [], passes: [],
    };
  }

  function formatTime(iso) {
    const d = new Date(iso || '');
    if (!Number.isFinite(d.getTime())) return '';
    const sameDay = d.toDateString() === new Date().toDateString();
    const t = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    return sameDay ? `at ${t}` : `${d.toLocaleDateString([], { month: 'short', day: 'numeric' })}, ${t}`;
  }

  function decisionCode(r) {
    const d = r.culled === 'accept' || r.culled === 'reject' ? r.culled : '';
    if (!d) return '';
    return normalizeCullOrigin(r) === 'manual' ? `${d}:manual` : d;
  }

  // ── Dialog shell ───────────────────────────────────────────────────────────

  function closeDialog() {
    if (!dlg) return;
    const d = dlg;
    dlg = null;
    try { d.close(); } catch (e) { console.warn('[cull] dialog close', e); }
    d.remove();
  }

  /** Open one Cull mode dialog (closing any other). ``sticky`` ignores Esc. */
  function openDialog(size, html, onClick, sticky = false) {
    closeDialog();
    const d = document.createElement('dialog');
    d.className = `kdlg kdlg--${size} cmd`;
    d.innerHTML = html;
    d.addEventListener('click', (e) => {
      const a = e.target.closest('[data-do]');
      if (a && !a.disabled) onClick(a.dataset.do, a, e);
    });
    d.addEventListener('cancel', (e) => { if (sticky) e.preventDefault(); });
    d.addEventListener('close', () => { if (dlg === d) { dlg = null; d.remove(); } });
    document.body.appendChild(d);
    d.showModal();
    dlg = d;
    return d;
  }

  // ── Starting picks ─────────────────────────────────────────────────────────

  function inScopeBirdQs() {
    return CM().shownRows().filter(r => !CM().isNoBird(r)).map(r => parseNumber(r.quality));
  }

  function histogramSvg(thr, edges) {
    const bins = L.histogram(inScopeBirdQs());
    const max = Math.max(1, ...bins);
    let svg = '<svg viewBox="0 0 500 120" preserveAspectRatio="none" class="cmd-histo" aria-hidden="true">';
    for (const e of L.edgesList(edges)) svg += `<line class="cmd-edge" x1="${e.at * 500}" x2="${e.at * 500}" y1="0" y2="120"/>`;
    bins.forEach((n, i) => {
      const h = (108 * n) / max;
      const on = (i + 0.5) / bins.length >= thr;
      svg += `<rect class="${on ? 'cmd-bar-on' : 'cmd-bar-off'}" x="${i * 25 + 2}" y="${118 - h}" width="21" height="${h}" rx="3"/>`;
    });
    return `${svg}<line class="cmd-thr" x1="${thr * 500}" x2="${thr * 500}" y1="0" y2="120"/></svg>`;
  }

  function picksHtml(start) {
    const st = S();
    const ses = st.session;
    const t = ses.rules.threshold;
    const c = CM().counts(CM().shownRows());
    const total = c.a + c.r;
    const nb = CM().shownRows().filter(r => CM().isNoBird(r)).length;
    const n = ses.current;
    const bands = L.edgesList(st.edges).map(e => `<span style="left:${e.at * 100}%">★${e.star}</span>`).join('');
    const seg = (v) => `<button type="button" data-do="nobird" data-v="${v}" class="${ses.rules.no_bird === v ? 'on' : ''}">${escapeHtml(TEXT[v])}</button>`;
    return `
      <div class="cmd-body">
        <h3 class="k-dialog-title">${fill(start ? TEXT.startTitle : TEXT.picksTitle, { n })}</h3>
        <p class="cmd-sub">${n === 1 ? escapeHtml(TEXT.picksSub1) : fill(TEXT.picksSubN, { n, kept: total })}</p>
        <div class="cmd-histo-wrap">${histogramSvg(t, st.edges)}</div>
        <div class="cmd-bands">${bands}</div>
        <input type="range" class="cmd-thr-range" min="0.05" max="0.95" step="0.01" value="${t}" aria-label="Quality threshold">
        <div class="cmd-thrline"><span>${TEXT.thrLine.replace('{t}', t.toFixed(2)).replace('{star}', L.starFor(t, st.edges))}</span>
          <span class="cmd-hint">${TEXT.snapHint}</span></div>
        <div class="cmd-proj">${TEXT.projection.replace('{a}', c.a).replace('{total}', total).replace('{pct}', total ? Math.round((100 * c.a) / total) : 0)}</div>
        <div class="k-row"><div class="k-row-label">${escapeHtml(TEXT.atLeast)}<span class="k-row-hint">${escapeHtml(TEXT.atLeastSub)}</span></div>
          <div class="cmd-stepper"><button type="button" class="k-btn--sm" data-do="min" data-v="-1" aria-label="Fewer">−</button>
          <span>${ses.rules.min_per_scene}</span><button type="button" class="k-btn--sm" data-do="min" data-v="1" aria-label="More">+</button></div></div>
        <div class="k-row"><div class="k-row-label">${escapeHtml(TEXT.atMost)}<span class="k-row-hint">${escapeHtml(TEXT.atMostSub)}</span></div>
          <div class="cmd-stepper"><button type="button" class="k-btn--sm" data-do="max" data-v="-1" aria-label="Fewer">−</button>
          <span>${ses.rules.max_per_scene ? ses.rules.max_per_scene : escapeHtml(TEXT.noLimit)}</span><button type="button" class="k-btn--sm" data-do="max" data-v="1" aria-label="More">+</button></div></div>
        <div class="k-row"><div class="k-row-label">${KestrelIcon('warning')} ${escapeHtml(TEXT.noBird)}<span class="k-row-hint">${fill(TEXT.noBirdSub, { n: nb })}</span></div>
          <div class="k-seg" role="group">${seg('accept')}${seg('reject')}</div></div>
      </div>
      <div class="kdlg-footer">
        ${start ? `<button type="button" class="k-btn--quiet" data-do="exit">${escapeHtml(TEXT.backToLibrary)}</button>
        <button type="button" class="primary" data-do="start">${fill(TEXT.startPass, { n })} <kbd>Enter</kbd></button>`
        : `<button type="button" class="primary" data-do="close">${escapeHtml(TEXT.done)}</button>`}
      </div>`;
  }

  let picksRaf = 0;
  function refreshPicks(start) {
    cancelAnimationFrame(picksRaf);
    picksRaf = requestAnimationFrame(() => {
      CM().applyPicksAll();
      CM().render();
      if (!dlg) return;
      patchPicks(start);
    });
  }

  /**
   * Swap in the re-rendered dialog piece by piece, leaving the range input in
   * the DOM: detaching it, even briefly, ends a drag after its first step.
   */
  function patchPicks(start) {
    const tmp = document.createElement('div');
    tmp.innerHTML = picksHtml(start);
    const oldKids = [...(dlg.querySelector('.cmd-body')?.children || [])];
    const newKids = [...tmp.querySelector('.cmd-body').children];
    if (oldKids.length !== newKids.length) { dlg.innerHTML = tmp.innerHTML; wirePicks(start); return; }
    oldKids.forEach((o, i) => { if (!o.classList.contains('cmd-thr-range')) o.replaceWith(newKids[i]); });
    dlg.querySelector('.kdlg-footer')?.replaceWith(tmp.querySelector('.kdlg-footer'));
  }

  function wirePicks(start) {
    let shift = false;
    const range = dlg.querySelector('.cmd-thr-range');
    range.addEventListener('keydown', (e) => { shift = e.shiftKey; });
    range.addEventListener('pointerdown', (e) => { shift = e.shiftKey; });
    range.addEventListener('pointermove', (e) => { shift = e.shiftKey; });
    range.addEventListener('input', () => {
      let v = Number(range.value);
      if (shift) { v = L.snapToEdge(v, S().edges); range.value = String(v); }
      S().session.rules.threshold = Math.round(v * 100) / 100;
      S().session.cuts = {};
      refreshPicks(start);
    });
  }

  /**
   * Accept at least runs 0-5; Keep at most runs No limit (0), then 1-50. The
   * two never cross: moving one past the other carries the other with it.
   */
  function stepLimit(rules, which, d) {
    const max0 = rules.max_per_scene || 0;
    if (which === 'min') {
      rules.min_per_scene = Math.max(0, Math.min(5, rules.min_per_scene + d));
      if (max0 && max0 < rules.min_per_scene) rules.max_per_scene = rules.min_per_scene;
      return;
    }
    let max = max0 + d;
    if (max <= 0) max = d < 0 ? 0 : 1;
    max = Math.min(50, max);
    if (max && max < rules.min_per_scene) rules.min_per_scene = max;
    rules.max_per_scene = max;
  }

  function openPicks(start) {
    openDialog('md', picksHtml(start), (act, a) => {
      const ses = S().session;
      if (act === 'min' || act === 'max') { stepLimit(ses.rules, act, Number(a.dataset.v)); refreshPicks(start); }
      else if (act === 'nobird') { ses.rules.no_bird = a.dataset.v; refreshPicks(start); }
      else if (act === 'start') startPass();
      else if (act === 'close') closeDialog();
      else if (act === 'exit') { closeDialog(); CM().exit(); }
    }, start);
    dlg.addEventListener('keydown', (e) => {
      if (start && e.key === 'Enter' && !e.target.closest('button')) { e.preventDefault(); startPass(); }
    });
    wirePicks(start);
  }

  function startPass() {
    const ses = S().session;
    ses.started = true;
    closeDialog();
    CM().applyPicksAll();
    CM().queueSessionSave();
    CM().go(CM().orderedScenes()[0]);
    showToast(fill(TEXT.started, { n: ses.current }), 2600);
  }

  // ── Finish ─────────────────────────────────────────────────────────────────

  function noticeEl(text) {
    const box = document.createElement('div');
    box.className = 'cm-notice';
    box.innerHTML = `${KestrelIcon('warning')} `;
    box.appendChild(document.createTextNode(text));
    const acts = document.createElement('div');
    acts.className = 'cm-notice-acts';
    acts.innerHTML = `<button type="button" class="k-btn--sm" data-act="confirmall">${escapeHtml(TEXT.confirmAll)}</button>`
      + `<button type="button" class="k-btn--icon k-btn--quiet" data-act="dismiss" aria-label="${escapeHtml(TEXT.dismiss)}">${KestrelIcon('x')}</button>`;
    box.appendChild(acts);
    return box;
  }

  function finish() {
    const st = S();
    if (st.view) { st.view = null; CM().rebuild(); }
    const open = CM().openScenes();
    if (open.length) {
      st.filter = 'open';
      st.notice = fill(TEXT.unconfirmedNotice, { n: open.length, s: open.length > 1 ? 's' : '', have: open.length > 1 ? 'have' : 'has' });
      CM().go(open[0]);
      return;
    }
    st.notice = '';
    CM().render();
    openFinish();
  }

  function finishHtml() {
    const st = S();
    const ses = st.session;
    const shown = CM().shownRows();
    const c = CM().counts(shown);
    const favs = KestrelFavorites.count(shown);
    const sw = (key, on) => `<input type="checkbox" class="k-switch" data-opt="${key}" ${on ? 'checked' : ''} aria-label="${key}">`;
    return `
      <div class="cmd-body">
        <h3 class="k-dialog-title">${fill(TEXT.finishTitle, { n: ses.current })}</h3>
        <p class="cmd-sub">${fill(TEXT.finishSub, { folder: folderBaseName(st.root) })}</p>
        <div class="cmd-tiles">
          <div><b class="cm-a">${c.a}</b><span>${escapeHtml(TEXT.accepted)}</span></div>
          <div><b class="cm-r">${c.r}</b><span>${escapeHtml(TEXT.rejectedThisPass)}</span></div>
          <div><b class="cm-fav">${favs}</b><span>${escapeHtml(TEXT.favorites)}</span></div>
        </div>
        <div class="k-row"><div class="k-row-label">${escapeHtml(TEXT.moveRejects)}<span class="k-row-hint">${escapeHtml(TEXT.moveRejectsSub)}</span></div>${sw('move', getSetting('cull_finish_move', true))}</div>
        <div class="k-row"><div class="k-row-label">${escapeHtml(TEXT.writeMeta)}<span class="k-row-hint">${escapeHtml(TEXT.writeMetaSub)}</span></div>${sw('meta', getSetting('cull_finish_metadata', false))}</div>
        <div class="cmd-passbox">${TEXT.anotherPass.replace('{next}', ses.current + 1).replace('{a}', c.a)}</div>
        <ul class="cmd-steps hidden"></ul>
      </div>
      <div class="kdlg-footer">
        <span class="cm-muted cmd-footnote">${escapeHtml(TEXT.nothingDeleted)}</span>
        <button type="button" class="k-btn--quiet" data-do="close">${escapeHtml(TEXT.keepCulling)}</button>
        <button type="button" data-do="end">${escapeHtml(TEXT.finishCulling)}</button>
        <button type="button" class="primary" data-do="next">${fill(TEXT.startPass, { n: ses.current + 1 })}</button>
      </div>`;
  }

  function openFinish() {
    openDialog('md', finishHtml(), (act) => {
      if (act === 'close') closeDialog();
      else if (act === 'end' || act === 'next') endPass(act === 'next');
    });
  }

  function rememberOption(key, value) {
    try { const s = loadSettings(); s[key] = value; saveSettings(s); } catch (e) { console.warn('[cull] remember option', e); }
    window.pywebview?.api?.save_settings_data?.({ [key]: value })?.catch?.(e => console.warn('[cull] save option', e));
  }

  function step(text, state) {
    const ul = dlg?.querySelector('.cmd-steps');
    if (!ul) return null;
    ul.classList.remove('hidden');
    const li = document.createElement('li');
    li.className = `cmd-step is-${state}`;
    li.textContent = text;
    ul.appendChild(li);
    return li;
  }
  const setStep = (li, state) => { if (li) li.className = `cmd-step is-${state}`; };

  function xmpPayload(list) {
    const tags = _scenedata[S().root]?.scenes || {};
    return list.map(r => {
      const t = window.KestrelReviewedTags.resolveRowTags(tags, r);
      return { filename: r.filename, rating: getRating(r), culled: r.culled || '', culled_origin: normalizeCullOrigin(r),
        species: t.species, family: t.family, quality: r.quality != null ? r.quality : null };
    });
  }

  function dropMovedRows(moved) {
    const st = S();
    const set = new Set(moved);
    rows = rows.filter(r => !(r.__rootPath === st.root && set.has(r.filename)));
    const sd = _scenedata[st.root];
    if (sd?.image_ratings) for (const fn of set) delete sd.image_ratings[fn];
    for (const [id, sc] of Object.entries(sd?.scenes || {})) {
      if (!Array.isArray(sc.image_filenames)) continue;
      sc.image_filenames = sc.image_filenames.filter(f => !set.has(f));
      if (!sc.image_filenames.length) delete sd.scenes[id];
    }
  }

  async function endPass(next) {
    const st = S();
    const ses = st.session;
    const api = window.pywebview.api;
    const doMove = !!dlg?.querySelector('[data-opt="move"]')?.checked;
    const doMeta = !!dlg?.querySelector('[data-opt="meta"]')?.checked;
    rememberOption('cull_finish_move', doMove);
    rememberOption('cull_finish_metadata', doMeta);
    dlg?.querySelectorAll('.kdlg-footer button').forEach(b => { b.disabled = true; });
    const shown = CM().shownRows();
    const c = CM().counts(shown);
    const all = CM().liveRows();
    const record = {
      finished_at: new Date().toISOString(), rules: { ...ses.rules },
      decisions: {}, accepted: c.a, rejected: c.r, moved: 0, history_id: '',
      move_undone: false, metadata_written: false, moved_rows: [],
    };
    for (const r of all) { const code = decisionCode(r); if (code) record.decisions[r.filename] = code; }
    try {
      const s1 = step(TEXT.savingStep, 'running');
      await CM().flushSaves();
      setStep(s1, 'done');
      if (doMeta) {
        const s2 = step(TEXT.metaStep, 'running');
        const fields = getSetting('xmp_fields', null) || undefined;
        const res = await api.write_xmp_metadata(st.root, xmpPayload(all), false, false, fields, false);
        setStep(s2, res?.success ? 'done' : 'failed');
        record.metadata_written = !!res?.success;
        if (res?.skipped_conflicts?.length) showToast(fill(TEXT.metaSkipped, { n: res.skipped_conflicts.length }), 6000);
      }
      if (doMove) {
        const rejects = all.filter(r => r.culled === 'reject').map(r => r.filename);
        if (rejects.length) {
          const s3 = step(TEXT.moveStep, 'running');
          const bk = await api.backup_kestrel_db(st.root);
          if (!bk?.success) throw new Error(bk?.error || 'backup failed');
          const mv = await api.move_rejects_to_folder(st.root, rejects);
          if (!mv?.success) throw new Error(mv?.error || 'move failed');
          const moved = Array.isArray(mv.moved_requested) ? mv.moved_requested
            : rejects.filter(fn => new Set(mv.moved_filenames || []).has(fn));
          const byName = new Map(all.map(r => [r.filename, r]));
          record.moved_rows = moved.map(fn => {
            const r = byName.get(fn) || {};
            const kept = Object.fromEntries(MOVED_ROW_FIELDS.map(k => [k, r[k]]));
            return { ...kept, filename: fn, rating: getRating(r), favorite: r.favorite || '' };
          });
          record.moved = moved.length;
          dropMovedRows(moved);
          markDirty(st.root);
          await saveCsv();
          if (moved.length) {
            const rec = await api.record_cull_pass(st.root, moved);
            if (rec?.success && rec.recorded) record.history_id = rec.pass_id;
            else console.warn('[cull] record_cull_pass failed', rec?.error);
          }
          setStep(s3, mv.all_moved === false ? 'failed' : 'done');
          if (mv.all_moved === false) showToast(fill(TEXT.moveFailed, { error: (mv.skipped_requested || []).length + ' skipped' }), 6000);
        }
      }
    } catch (e) {
      console.error('[cull] finish failed', e);
      showToast(fill(TEXT.finishFailed, { error: e.message || e }), 6000);
      dlg?.querySelectorAll('.kdlg-footer button').forEach(b => { b.disabled = false; });
      return;
    }
    ses.passes.push(record);
    ses.ended = true;
    ses.started = false;
    ses.current = ses.passes.length;
    ses.confirmed = [];
    ses.cuts = {};
    st.view = null;
    await CM().flushSaves();
    CM().rebuild();
    CM().render();
    closeDialog();
    if (next) startNextPass(); else openEnded();
  }

  function startNextPass() {
    const st = S();
    const ses = st.session;
    ses.ended = false;
    ses.started = false;
    ses.current = ses.passes.length + 1;
    ses.confirmed = [];
    ses.cuts = {};
    const last = ses.passes[ses.passes.length - 1];
    ses.rules = { ...ses.rules, threshold: L.nextThreshold(last?.rules?.threshold ?? ses.rules.threshold, st.edges) };
    st.filter = 'all';
    st.notice = '';
    st.view = null;
    CM().rebuild();
    for (const r of CM().shownRows()) if (normalizeCullOrigin(r) === 'verified') r.culled_origin = 'auto';
    CM().applyPicksAll();
    CM().go(CM().orderedScenes()[0]);
    openPicks(true);
  }

  function passLine(p) {
    let s = fill(TEXT.passLine, { a: p.accepted, r: p.rejected });
    if (p.moved) s += ` · ${p.move_undone ? fill(TEXT.movedBack, { n: p.moved }) : `${p.moved} in _KESTREL_Rejects`}`;
    return s;
  }

  function openEnded() {
    const st = S();
    const ses = st.session;
    const p = ses.passes[ses.passes.length - 1];
    if (!p) return;
    openDialog('sm', `
      <div class="cmd-body">
        <h3 class="k-dialog-title">${fill(TEXT.endedTitle, { n: ses.passes.length })}</h3>
        <p class="cmd-sub">${escapeHtml(folderBaseName(st.root))}</p>
        <p>${passLine(p)}${p.metadata_written ? ' · metadata written' : ''}.</p>
      </div>
      <div class="kdlg-footer">
        <button type="button" class="k-btn--quiet" data-do="history">${escapeHtml(TEXT.history)}</button>
        <button type="button" data-do="next">${fill(TEXT.startPass, { n: ses.passes.length + 1 })}</button>
        <button type="button" class="primary" data-do="exit">${escapeHtml(TEXT.backToLibrary)}</button>
      </div>`, (act) => {
      if (act === 'history') openHistory();
      else if (act === 'next') { closeDialog(); startNextPass(); }
      else if (act === 'exit') { closeDialog(); CM().exit(); }
    });
  }

  // ── Pass menu and read-only view ───────────────────────────────────────────

  function closeMenu() { S().el?.querySelector('.cm-passmenu')?.remove(); }

  function togglePassMenu(anchor) {
    const st = S();
    if (st.el.querySelector('.cm-passmenu')) { closeMenu(); return; }
    const ses = st.session;
    const menu = document.createElement('div');
    menu.className = 'cm-passmenu';
    const item = (n, title, sub, hint, on) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'cm-pm' + (on ? ' is-on' : '');
      b.dataset.act = 'viewpass';
      b.dataset.v = n;
      const t = document.createElement('b'); t.textContent = title;
      const s = document.createElement('span'); s.textContent = sub;
      b.append(t, s);
      if (hint) { const i = document.createElement('i'); i.textContent = hint; b.appendChild(i); }
      return b;
    };
    ses.passes.forEach((p, i) => {
      menu.appendChild(item(i + 1, `Pass ${i + 1}`,
        fill(TEXT.menuFinished, { time: formatTime(p.finished_at), line: passLine(p) }), TEXT.menuView, st.view === i + 1));
    });
    if (!ses.ended) {
      menu.appendChild(item('cur', `Pass ${ses.current}`,
        fill(TEXT.menuCurrent, { d: ses.confirmed.length, t: st.scenes.length }), '', !st.view));
    }
    if (!ses.passes.length) {
      const m = document.createElement('div'); m.className = 'cm-muted cm-pad'; m.textContent = TEXT.menuNone; menu.appendChild(m);
    }
    const h = document.createElement('button');
    h.type = 'button'; h.className = 'cm-pm cm-pm--link'; h.dataset.act = 'history'; h.textContent = TEXT.menuHistory;
    menu.appendChild(h);
    const r = anchor.getBoundingClientRect();
    menu.style.left = `${r.left}px`;
    menu.style.top = `${r.bottom + 6}px`;
    st.el.appendChild(menu);
  }

  function viewPass(v) {
    const st = S();
    closeMenu();
    st.view = v === 'cur' ? null : Number(v);
    st.favView = false;
    CM().rebuild();
    CM().go(CM().orderedScenes()[0]);
  }

  function fillViewBar(bar, p, n, cur) {
    const where = p.moved ? (p.move_undone ? TEXT.viewingBack : fill(TEXT.viewingMoved, { n: p.moved })) : TEXT.viewingLeft;
    const back = `<button type="button" class="k-btn--sm" data-act="viewpass" data-v="cur">${fill(TEXT.backToPass, { n: cur })}</button>`;
    bar.innerHTML = `<span>${TEXT.viewing.replace('{n}', n).replace('{where}', escapeHtml(where))}</span>${back}`;
  }

  // ── Culling history, Undo move, Revert, Reset ──────────────────────────────

  async function freshSession() {
    const st = S();
    try {
      const res = await window.pywebview.api.get_cull_session(st.root);
      if (res?.success && res.session) {
        // Keep the live pass state; take the disk view of finished passes.
        st.session.passes = res.session.passes;
      }
    } catch (e) { console.warn('[cull] history refresh failed', e); }
  }

  function historyRow(p, i, newestMoved, ses) {
    const n = i + 1;
    let detail = !p.moved ? TEXT.leftInPlace
      : p.move_undone ? fill(TEXT.movedBack, { n: p.moved })
        : (p.on_disk === false ? fill(TEXT.movedMissing, { n: p.moved }) : fill(TEXT.movedThere, { n: p.moved }));
    detail += ` · ${p.metadata_written ? fill(TEXT.metaWritten, { n: p.file_count ?? Object.keys(p.decisions || {}).length }) : escapeHtml(TEXT.noMeta)}`;
    const canRevert = p.on_disk !== false && (n < ses.passes.length || !ses.ended);
    return `<div class="cmd-hrow">
        <div class="cmd-hl"><b>Pass ${n}</b> <span class="cm-muted">${fill(TEXT.passFinished, { time: formatTime(p.finished_at) })}</span>
          <div>${fill(TEXT.passLine, { a: p.accepted, r: p.rejected })}</div><div class="cm-muted">${detail}</div></div>
        <div class="cmd-ha">
          <button type="button" class="k-btn--sm k-btn--quiet" data-do="view" data-v="${n}">${escapeHtml(TEXT.viewPass)}</button>
          ${newestMoved === i ? `<button type="button" class="k-btn--sm k-btn--quiet" data-do="undo">${escapeHtml(TEXT.undoMove)}</button>` : ''}
          ${canRevert ? `<button type="button" class="k-btn--sm" data-do="askrevert" data-v="${n}" title="${escapeHtml(TEXT.revertTitle)}">${escapeHtml(TEXT.revert)}</button>` : ''}
        </div></div>`;
  }

  function historyHtml() {
    const st = S();
    const ses = st.session;
    let newestMoved = -1;
    ses.passes.forEach((p, i) => { if (p.history_id && !p.move_undone) newestMoved = i; });
    const rowsHtml = ses.passes.map((p, i) => historyRow(p, i, newestMoved, ses)).join('')
      || `<div class="cm-muted cmd-pad">${escapeHtml(TEXT.historyEmpty)}</div>`;
    const cur = ses.ended ? '' : `<div class="cmd-hrow is-cur"><div class="cmd-hl"><b>Pass ${ses.current}</b> <span class="cm-muted">${escapeHtml(TEXT.inProgress)}</span>
      <div>${fill(TEXT.scenesConfirmed, { d: ses.confirmed.length, t: st.scenes.length })}</div></div></div>`;
    return `
      <div class="cmd-body">
        <h3 class="k-dialog-title">${escapeHtml(TEXT.history)}</h3>
        <p class="cmd-sub">${fill(TEXT.historySub, { folder: folderBaseName(st.root) })}</p>
        ${rowsHtml}${cur}
      </div>
      <div class="kdlg-footer">
        <button type="button" class="k-btn--danger cmd-left" data-do="askrevert" data-v="0">${escapeHtml(TEXT.reset)}</button>
        <button type="button" class="primary" data-do="close">${escapeHtml(TEXT.close)}</button>
      </div>`;
  }

  function revertHtml(k) {
    const ses = S().session;
    const live = (list) => list.reduce((a, p) => a + (p.move_undone ? 0 : p.moved), 0);
    if (k === 0) {
      const n = live(ses.passes);
      return `<div class="cmd-body"><h3 class="k-dialog-title">${escapeHtml(TEXT.resetTitle)}</h3>
        <p class="cmd-sub">${escapeHtml(folderBaseName(S().root))}</p>
        <p>${n ? fill(TEXT.resetBody, { n }) : escapeHtml(TEXT.resetBodyNone)}</p></div>
        <div class="kdlg-footer"><button type="button" class="k-btn--quiet" data-do="history">${escapeHtml(TEXT.cancel)}</button>
        <button type="button" class="k-btn--danger-solid" data-do="revert">${escapeHtml(TEXT.reset)}</button></div>`;
    }
    const mv = live(ses.passes.slice(k));
    const last = ses.ended ? ses.passes.length : ses.current;
    const what = last > k + 1 ? `passes ${k + 1} to ${last}` : `pass ${k + 1}`;
    return `<div class="cmd-body"><h3 class="k-dialog-title">${fill(TEXT.revertTitleQ, { k })}</h3>
      <p class="cmd-sub">${escapeHtml(folderBaseName(S().root))}</p>
      <p>${mv ? fill(TEXT.revertMoves, { n: mv, k }) : fill(TEXT.revertNoMoves, { k })} ${fill(TEXT.revertDiscard, { what })}</p></div>
      <div class="kdlg-footer"><button type="button" class="k-btn--quiet" data-do="history">${escapeHtml(TEXT.cancel)}</button>
      <button type="button" class="primary" data-do="revert">${fill(TEXT.revertTitleQ, { k }).replace('?', '')}</button></div>`;
  }

  async function openHistory() {
    await CM().flushSaves();
    await freshSession();
    openDialog('md', historyHtml(), onHistory);
  }

  async function onHistory(act, a) {
    if (act === 'close') closeDialog();
    else if (act === 'history') { dlg.innerHTML = historyHtml(); }
    else if (act === 'view') { closeDialog(); viewPass(a.dataset.v); }
    else if (act === 'askrevert') { pendingRevert = Number(a.dataset.v); dlg.innerHTML = revertHtml(pendingRevert); }
    else if (act === 'revert') await runRevert(pendingRevert);
    else if (act === 'undo') await runUndo();
  }

  async function runUndo() {
    dlg?.querySelectorAll('button').forEach(b => { b.disabled = true; });
    await CM().flushSaves();
    const res = await window.pywebview.api.undo_cull_move(S().root);
    if (!res?.success) { showToast(fill(TEXT.failed, { error: res?.error || '' }), 5000); closeDialog(); return; }
    await CM().reload();
    showToast(fill(TEXT.undoDone, { n: res.restored || 0 }), 4000);
    await freshSession();
    if (dlg) dlg.innerHTML = historyHtml();
  }

  async function runRevert(k) {
    dlg?.querySelectorAll('button').forEach(b => { b.disabled = true; });
    const st = S();
    await CM().flushSaves();
    const res = await window.pywebview.api.revert_cull_session(st.root, k);
    closeDialog();
    if (!res?.success) { showToast(fill(TEXT.failed, { error: res?.error || '' }), 6000); return; }
    st.view = null;
    st.filter = 'all';
    st.notice = '';
    await CM().reload();
    if (k === 0) {
      CM().applyPicksAll();
      CM().go(CM().orderedScenes()[0]);
      openPicks(true);
      showToast(TEXT.resetDone, 3500);
    } else {
      CM().go(CM().orderedScenes()[0]);
      openEnded();
      showToast(fill(TEXT.reverted, { k }), 3000);
    }
  }

  // ── Entry points from cull-mode.js ─────────────────────────────────────────

  function onEnter() {
    const ses = S().session;
    if (ses.ended) openEnded();
    else if (!ses.started) { CM().applyPicksAll(); CM().render(); openPicks(true); }
  }

  function action(act, a) {
    if (act === 'picks') openPicks(false);
    else if (act === 'history') { closeMenu(); openHistory(); }
    else if (act === 'finish') finish();
    else if (act === 'passmenu') togglePassMenu(a);
    else if (act === 'viewpass') viewPass(a.dataset.v);
    else if (act === 'confirmall') { CM().confirmAll(); finish(); }
    else if (act === 'dismiss') { S().notice = ''; CM().render(); }
  }

  window.KestrelCullFlow = { newSession, formatTime, onEnter, action, finish, noticeEl, fillViewBar, closeDialog };
})();
