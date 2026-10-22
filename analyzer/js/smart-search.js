    // ── Smart search (SigLIP 2, pipeline 3.0) ───────────────────────────────
    // Free-text search over what is happening in the photos ("bird in flight",
    // "perched on a wire", "blurry", "two birds") with species detection in the
    // query: bird species / families / wildlife named in the query ("heron in a
    // marsh", "dove pair") become removable filter chips, and the rest is scored
    // with SigLIP against each photo's crop AND whole-frame embeddings. Opt-in
    // via the ✨ toggle next to the box. aggregateScenes() hands the scene list
    // to smartSearchApply(), which keeps the matching scenes in score order and
    // points each card at its best-matching image.
    const SMART_SEARCH_LIMIT = 48;
    const SMART_SEARCH_DEBOUNCE_MS = 400;
    const SMART_SEARCH_PLACEHOLDER = '✨ e.g. "heron in a marsh", "dove pair", "bird in flight"';
    const SMART_SEARCH_EXAMPLES = ['bird in flight', 'dove pair', 'heron in a marsh', 'perched on a wire', 'two birds', 'blurry'];
    const SMART_CHIP_KIND_LABEL = {
      species: 'species', family: 'family', wildlife: 'wildlife', group: 'species group',
    };
    const _smart = {
      on: false,
      query: '',          // query the current scores belong to
      seq: 0,             // bumps per request so stale replies are dropped
      scores: null,       // Map(rootPath\0filename -> score) or null
      pending: false,
      matchCount: 0,
      needsIndex: [],     // [{root, indexed, total, upgrade}]
      reanalyze: [],      // [{root, reason}] folders too old to upgrade
      chips: [],          // detected filters [{kind, label, text, labels, hits}]
      disabled: new Set(),// chip texts the user removed (sent back as plain text)
      semantic: '',
      mode: '',
      unavailable: false,
      error: '',
      indexing: null,     // {current, total, root} while an upgrade runs
      defaultPlaceholder: '',
    };

    function _smartKey(rp, fn) { return String(rp || '') + '\u0000' + String(fn || ''); }

    function _smartRoots() {
      const out = new Set();
      for (const r of rows) {
        const rp = r.__rootPath || rootPath || '';
        if (rp) out.add(rp);
      }
      return Array.from(out);
    }

    /** True while the smart-search toggle is on (the #search text is then a smart query). */
    function smartSearchOwnsQuery() { return _smart.on; }

    /** True when smart search owns the #search query (toggle on, non-empty query). */
    function smartSearchIsActive() {
      return _smart.on && !!_smart.scores && !!(el('#search')?.value || '').trim();
    }

    /** Score of one image in the current smart search, or undefined (used to highlight matches). */
    function smartSearchImageScore(row) {
      if (!smartSearchIsActive() || !row) return undefined;
      return _smart.scores.get(_smartKey(row.__rootPath || rootPath || '', row.filename));
    }

    /** Called by aggregateScenes(): matching scenes, best first, with smartScore/smartRank. */
    function smartSearchApply(list) {
      const out = [];
      for (const s of list) {
        let best = -Infinity;
        let bestRow = null;
        let n = 0;
        for (const r of s.images || []) {
          const v = _smart.scores.get(_smartKey(r.__rootPath || rootPath || '', r.filename));
          if (v === undefined) continue;
          n += 1;
          if (v > best) { best = v; bestRow = r; }
        }
        if (best > -Infinity) {
          s.smartScore = best;
          s.smartMatchCount = n;
          // Show the best-matching photo on the card, not the scene's usual pick.
          if (bestRow) s.representative = bestRow;
          out.push(s);
        }
      }
      out.sort((a, b) => b.smartScore - a.smartScore);
      const top = out.slice(0, SMART_SEARCH_LIMIT);
      top.forEach((s, i) => { s.smartRank = i + 1; });
      _smart.matchCount = top.length;
      return top;
    }

    function _smartFolderName(rp) {
      return (typeof folderBaseName === 'function' ? folderBaseName(rp) : '') || rp;
    }

    function _smartRenderChips() {
      const box = el('#smartSearchChips');
      if (!box) return;
      box.textContent = '';
      const show = _smart.on && _smart.chips.length > 0;
      box.classList.toggle('hidden', !show);
      if (!show) return;
      for (const c of _smart.chips) {
        const chip = document.createElement('span');
        chip.className = `smart-chip smart-chip--${c.kind}`;
        const kindLabel = SMART_CHIP_KIND_LABEL[c.kind] || c.kind;
        const lbl = c.kind === 'group' ? `${c.text} (${(c.labels || []).join(', ')})` : c.label;
        chip.title = `Detected ${kindLabel} in your query: only photos tagged “${(c.labels || [c.label]).join('”, “')}”`
          + (c.scientific ? ` (${c.scientific})` : '')
          + (c.fuzzy ? ' — matched despite a typo' : '')
          + `. ${c.hits || 0} photo${c.hits === 1 ? '' : 's'} in the open folders. Click × to search the words “${c.text}” as plain text instead.`;
        const name = document.createElement('span');
        name.textContent = lbl;
        chip.appendChild(name);
        if (!c.hits) {
          const none = document.createElement('span');
          none.className = 'smart-chip-none';
          none.textContent = ' · none here';
          chip.appendChild(none);
        }
        const x = document.createElement('button');
        x.type = 'button';
        x.className = 'smart-chip-x';
        x.setAttribute('aria-label', `Remove filter ${lbl}`);
        x.textContent = '×';
        x.addEventListener('click', (ev) => {
          ev.preventDefault();
          _smart.disabled.add(c.text);
          _smartRun();
        });
        chip.appendChild(x);
        box.appendChild(chip);
      }
    }

    function _smartUpdateUi() {
      const bar = el('#smartSearchBar');
      const hint = el('#smartSearchHint');
      const spin = el('#smartSearchSpinner');
      const idxBtn = el('#smartSearchIndexBtn');
      const clearBtn = el('#smartSearchClear');
      const toggle = el('#smartSearchToggle');
      const input = el('#search');
      if (!bar || !hint) return;
      if (toggle) toggle.setAttribute('aria-pressed', _smart.on ? 'true' : 'false');
      if (input) {
        input.classList.toggle('tfb-search--smart', _smart.on);
        input.placeholder = _smart.on ? SMART_SEARCH_PLACEHOLDER : _smart.defaultPlaceholder;
      }
      _smartRenderChips();
      const q = (input?.value || '').trim();
      // Visible while smart search is on, and during an upgrade started from
      // the Folder Actions menu (so its progress is never invisible).
      const show = _smart.on || !!_smart.indexing;
      bar.classList.toggle('hidden', !show);
      if (!show) return;
      spin?.classList.toggle('hidden', !(_smart.pending || _smart.indexing));
      clearBtn?.classList.toggle('hidden', !q);
      const upgradable = _smart.needsIndex.filter(n => n.upgrade !== 'reanalyze');
      idxBtn?.classList.toggle('hidden', !!_smart.indexing || _smart.unavailable || upgradable.length === 0);
      if (idxBtn) {
        idxBtn.textContent = upgradable.length > 1 ? `Upgrade ${upgradable.length} folders` : 'Upgrade folder';
        idxBtn.title = 'Upgrade to analysis pipeline 3.0 using the crops Kestrel already saved: new species IDs, the SigLIP quality score and the smart-search index. No re-analysis, never changes your ratings, culls or tags. Runs on this computer only.';
      }

      hint.textContent = '';
      const add = (text, cls) => {
        const span = document.createElement('span');
        if (cls) span.className = cls;
        span.textContent = text;
        hint.appendChild(span);
        return span;
      };
      if (_smart.unavailable) { add('Smart search is not available in this build (model files missing).'); return; }
      if (_smart.indexing) {
        const { current, total, root, jobId } = _smart.indexing;
        add(`Upgrading ${_smartFolderName(root)} to pipeline 3… ${current}/${total || '?'} `);
        if (jobId && !_smart.cancelUpgrade) {
          const c = add('Cancel', 'smart-search-example');
          c.tabIndex = 0;
          c.setAttribute('role', 'button');
          c.title = 'Stop the upgrade. Progress so far is kept; running it again resumes where it stopped.';
          const stop = () => {
            _smart.cancelUpgrade = true;
            try { window.pywebview?.api?.cancel_smart_search_index(jobId); } catch (_) {}
            _smartUpdateUi();
          };
          c.addEventListener('click', stop);
          c.addEventListener('keydown', (e) => { if (e.key === 'Enter') stop(); });
        } else if (_smart.cancelUpgrade) {
          add('(stopping…)', 'smart-search-muted');
        }
        return;
      }
      if (_smart.error) { add(`Smart search failed: ${_smart.error}`); return; }
      if (!q) {
        // Empty state: what you can type.
        add('Try: ');
        SMART_SEARCH_EXAMPLES.forEach((ex, i) => {
          const a = add(ex, 'smart-search-example');
          a.setAttribute('role', 'button');
          a.tabIndex = 0;
          const go = () => { if (input) { input.value = ex; _smartRun(); } };
          a.addEventListener('click', go);
          a.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
          if (i < SMART_SEARCH_EXAMPLES.length - 1) add(' · ');
        });
        add(' — bird names become filter chips: ', 'smart-search-muted');
        for (const [kind, label] of [['species', 'species'], ['family', 'family'], ['wildlife', 'wildlife']]) {
          add(label, `smart-chip smart-chip--${kind} smart-chip--legend`);
          add(' ');
        }
      }
      const totalIndexed = _smart.needsIndex.reduce((a, n) => a + (n.indexed || 0), 0);
      const nothingIndexed = q && _smart.scores && _smart.scores.size === 0 && _smart.needsIndex.length > 0 && totalIndexed === 0;
      if (nothingIndexed && !_smart.chips.length) {
        add('These folders were analysed before pipeline 3.0 and aren’t indexed for smart search yet. Upgrading reads the crops Kestrel already saved — no re-analysis.');
        return;
      }
      if (_smart.pending && !_smart.scores) { add('Searching…'); return; }
      if (q) {
        add(`${_smart.matchCount} scene${_smart.matchCount === 1 ? '' : 's'}`);
        if (_smart.mode === 'filter') add(' · filtered by the detected bird, sorted by quality');
        else if (_smart.semantic && _smart.chips.length) add(` · “${_smart.semantic}” within the detected bird`);
        else if (_smart.semantic) add(` for “${_smart.semantic}”`);
      }
      if (_smart.needsIndex.length) {
        const missing = _smart.needsIndex.reduce((a, n) => a + Math.max(0, (n.total || 0) - (n.indexed || 0)), 0);
        if (missing > 0) add(` · ${missing} photo${missing === 1 ? '' : 's'} need the pipeline 3 upgrade`);
      }
      if (_smart.reanalyze.length) {
        const r = _smart.reanalyze[0];
        add(` · ${_smartFolderName(r.root)}${_smart.reanalyze.length > 1 ? ` (+${_smart.reanalyze.length - 1})` : ''}: re-analyse to use smart search`, 'smart-search-muted')
          .title = r.reason || '';
      }
    }

    async function _smartRefreshStatus() {
      const api = window.pywebview?.api;
      if (!api?.smart_search_status) return;
      const needs = [];
      const reanalyze = [];
      for (const rp of _smartRoots()) {
        let st;
        try { st = await api.smart_search_status(rp); } catch (_) { st = null; }
        if (!st?.success) continue;
        const up = st.upgrade?.status || '';
        if (up === 'reanalyze') { reanalyze.push({ root: rp, reason: st.upgrade?.reason || '' }); continue; }
        if (st.needs_index || up === 'upgradable') {
          needs.push({
            root: rp, upgrade: up,
            indexed: Math.min(st.indexed_images || 0, st.frame_indexed_images || 0),
            total: Math.max(st.indexable_images || 0, st.frame_indexable_images || 0),
          });
        }
      }
      _smart.needsIndex = needs;
      _smart.reanalyze = reanalyze;
    }

    async function _smartRun() {
      const input = el('#search');
      const q = (input?.value || '').trim();
      const my = ++_smart.seq;
      if (!_smart.on) return;
      if (!q) {
        Object.assign(_smart, { query: '', scores: null, pending: false, error: '', chips: [], semantic: '', mode: '' });
        _smart.disabled.clear();
        await _smartRefreshStatus();
        if (my !== _smart.seq) return;
        _smartUpdateUi();
        renderScenes();
        return;
      }
      // Forget un-chipped words that are no longer in the query.
      for (const t of Array.from(_smart.disabled)) {
        if (!q.toLowerCase().includes(String(t).toLowerCase())) _smart.disabled.delete(t);
      }
      const api = window.pywebview?.api;
      if (!api?.smart_search) return;
      _smart.pending = true;
      _smartUpdateUi();
      const scores = new Map();
      const needs = [];
      const chipsByKey = new Map();
      let unavailable = false;
      let err = '';
      let semantic = '';
      let mode = '';
      const options = { disabled_terms: Array.from(_smart.disabled) };
      for (const rp of _smartRoots()) {
        let res;
        try { res = await api.smart_search(rp, q, SMART_SEARCH_LIMIT, options); } catch (e) { err = String(e?.message || e); continue; }
        if (my !== _smart.seq) return;
        if (!res?.success) {
          if (res?.error === 'smart_search_unavailable') unavailable = true;
          else err = String(res?.error || 'unknown error');
          continue;
        }
        semantic = semantic || res.semantic_query || '';
        mode = mode || res.mode || '';
        for (const c of res.chips || []) {
          const k = `${c.kind}\u0000${c.text}`;
          const prev = chipsByKey.get(k);
          if (prev) prev.hits = (prev.hits || 0) + (c.hits || 0);
          else chipsByKey.set(k, { ...c });
        }
        if (res.needs_index) {
          needs.push({
            root: rp, upgrade: 'upgradable',
            indexed: Math.min(res.indexed_images || 0, res.frame_indexed_images || 0),
            total: Math.max(res.indexable_images || 0, res.frame_indexable_images || 0),
          });
        }
        for (const im of res.images || []) scores.set(_smartKey(rp, im.filename), Number(im.score));
      }
      if (my !== _smart.seq) return;
      Object.assign(_smart, {
        pending: false, query: q, scores, needsIndex: needs, unavailable, semantic, mode,
        chips: Array.from(chipsByKey.values()),
        error: scores.size || chipsByKey.size ? '' : err,
      });
      await renderScenes();
      _smartUpdateUi();
    }

    async function _smartIndexMissing() {
      const api = window.pywebview?.api;
      const start_fn = api?.upgrade_folder_v3 || api?.smart_search_index_folder;
      if (!start_fn || _smart.indexing) return;
      const targets = _smart.needsIndex.filter(n => n.upgrade !== 'reanalyze').map(n => n.root);
      let upgradedAny = false;
      _smart.cancelUpgrade = false;
      for (const rp of targets) {
        if (_smart.cancelUpgrade) break;
        _smart.indexing = { current: 0, total: 0, root: rp };
        _smartUpdateUi();
        let start;
        try { start = await start_fn.call(api, rp); } catch (e) { start = { success: false, error: String(e) }; }
        if (!start?.success) {
          _smart.indexing = null;
          const why = start?.error === 'analysis_running'
            ? 'this folder is being analyzed; upgrade it once analysis finishes.'
            : start?.error === 'reanalyze_required'
              ? (start.reason || 're-analyse this folder to use smart search.')
              : String(start?.error || 'unknown error');
          showToast(`Couldn’t upgrade ${_smartFolderName(rp)}: ${why}`, 6000);
          continue;
        }
        for (;;) {
          await new Promise(r => setTimeout(r, 500));
          let p;
          try { p = (await api.get_smart_search_index_progress(start.job_id))?.progress; } catch (_) { p = null; }
          if (!p) break;
          _smart.indexing = { current: p.current || 0, total: p.total || 0, root: rp, jobId: start.job_id };
          _smartUpdateUi();
          if (p.done) {
            if (p.phase === 'error') showToast(`Upgrade failed: ${p.error || 'unknown error'}`, 6000);
            else if (p.phase === 'stopped') showToast('Upgrade paused because analysis started. Try again when it finishes.', 6000);
            else if (p.phase === 'canceled') { showToast(`Upgrade of ${_smartFolderName(rp)} stopped — run it again to resume.`, 5000); upgradedAny = true; }
            else if (p.phase === 'complete') upgradedAny = true;
            break;
          }
        }
      }
      _smart.indexing = null;
      _smart.cancelUpgrade = false;
      _smart.needsIndex = [];
      _smartUpdateUi();
      // The upgrade rewrote species / quality columns: reload what is open so
      // the grid shows them (and so a later save cannot write the old values back).
      if (upgradedAny) {
        try {
          if (dirty && typeof saveCsv === 'function') await saveCsv();
          if (typeof reloadCurrentFolders === 'function') await reloadCurrentFolders();
        } catch (e) { console.warn('[smart-search] reload after upgrade failed', e); }
        showToast('Upgraded to pipeline 3.0 — species, quality and smart search are up to date.', 5000);
      }
      await _smartRun();
    }

    /** Folder action (sidebar / menus): upgrade one folder to pipeline 3. */
    async function smartSearchUpgradeFolder(rp) {
      if (!rp) return;
      _smart.needsIndex = [{ root: rp, upgrade: 'upgradable', indexed: 0, total: 0 }];
      await _smartIndexMissing();
    }
    window.smartSearchUpgradeFolder = smartSearchUpgradeFolder;

    function _smartSetOn(on) {
      _smart.on = !!on;
      if (!_smart.on) {
        _smart.seq++;
        Object.assign(_smart, { scores: null, query: '', pending: false, needsIndex: [], reanalyze: [], error: '',
          unavailable: false, chips: [], semantic: '', mode: '' });
        _smart.disabled.clear();
        _smartUpdateUi();
        renderScenes();
        return;
      }
      _smartUpdateUi();
      el('#search')?.focus();
      _smartRun();
    }

    (function _wireSmartSearch() {
      const input = el('#search');
      const toggle = el('#smartSearchToggle');
      if (!input || !toggle) return;
      _smart.defaultPlaceholder = input.placeholder;
      toggle.addEventListener('click', () => _smartSetOn(!_smart.on));
      input.addEventListener('input', debounce(() => { if (_smart.on) _smartRun(); }, SMART_SEARCH_DEBOUNCE_MS));
      el('#smartSearchClear')?.addEventListener('click', () => {
        input.value = '';
        _smartRun();
        input.focus();
      });
      el('#smartSearchIndexBtn')?.addEventListener('click', () => { _smartIndexMissing(); });
    })();
