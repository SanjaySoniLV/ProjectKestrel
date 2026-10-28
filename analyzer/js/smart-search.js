    // ── Unified search box: species/family filter or smart search (SigLIP 2) ──
    // One box. classify_search_query (query_parser.classify_query) routes each
    // query: a query that is only a species / family name ("Roseate Spoonbill",
    // "spoonbills") keeps the classic timeline filter; anything else ("spoonbill
    // wading", "bird in flight at sunset") is a smart search over the SigLIP 2
    // crop AND whole-frame embeddings, with detected bird names shown as
    // removable filter chips. aggregateScenes() hands the scene list to
    // smartSearchApply(), which keeps the matching scenes in score order and
    // points each card at its best-matching image. Folders without embeddings
    // show the existing "upgrade folder" prompt in the bar below the filters.
    const SMART_SEARCH_LIMIT = 48;
    const SMART_SEARCH_EXAMPLES = ['bird in flight', 'dove pair', 'heron in a marsh', 'perched on a wire', 'two birds', 'blurry'];
    const SMART_CHIP_KIND_LABEL = {
      species: 'species', family: 'family', wildlife: 'wildlife', group: 'species group',
    };
    const _smart = {
      route: { query: '', kind: 'empty', labels: [] }, // how the current query was classified
      tags: { species: [], families: [] },             // tags in the loaded folders (classifier hint)
      query: '',          // query the current scores belong to
      seq: 0,             // bumps per request so stale replies are dropped
      scores: null,       // Map(rootPath\0filename -> score) or null
      percents: new Map(),// rootPath\0filename -> 0-100 match percent (semantic results only)
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
      indexing: false,    // true while _smartIndexMissing() is starting/awaiting upgrades
                          // (their progress is shown in the analysis-queue panel)
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

    function _smartQueryNow() { return (el('#search')?.value || '').trim(); }

    /** True when the current query is a smart (non-filter) query. */
    function smartSearchOwnsQuery() {
      const q = _smartQueryNow();
      return !!q && _smart.route.kind === 'semantic' && _smart.route.query === q;
    }

    /** True when smart search owns the #search query and has a result set. */
    function smartSearchIsActive() {
      return smartSearchOwnsQuery() && !!_smart.scores;
    }

    /** Lower-cased tag values the species/family filter should also match for this query
     *  (plural / typo / scientific-name forms of a pure filter query), or []. */
    function smartSearchFilterLabels(q) {
      const r = _smart.route;
      if (r.kind !== 'filter' || r.query !== String(q || '').trim()) return [];
      return r.labels.map(l => String(l).toLowerCase());
    }

    /** Remember which species / family tags the loaded folders hold (helps classify "robin"). */
    function smartSearchRememberTags(list) {
      const sp = new Set(), fa = new Set();
      for (const s of list) {
        for (const x of s.species || []) sp.add(x);
        for (const x of s.families || []) fa.add(x);
      }
      _smart.tags = { species: Array.from(sp), families: Array.from(fa) };
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
        let bestKey = '';
        let n = 0;
        for (const r of s.images || []) {
          const k = _smartKey(r.__rootPath || rootPath || '', r.filename);
          const v = _smart.scores.get(k);
          if (v === undefined) continue;
          n += 1;
          if (v > best) { best = v; bestRow = r; bestKey = k; }
        }
        if (best > -Infinity) {
          s.smartScore = best;
          s.smartMatchCount = n;
          s.smartPercent = _smart.percents.has(bestKey) ? _smart.percents.get(bestKey) : null;
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
      const show = smartSearchOwnsQuery() && _smart.chips.length > 0;
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

    /** Display names of the active filter chips, OR-joined ("Roseate Spoonbill or Snowy Egret"). */
    function _smartChipNames() {
      const names = [];
      for (const c of _smart.chips) {
        const n = c.kind === 'group' ? (c.text || c.label) : (c.label || c.text);
        if (n && !names.includes(n)) names.push(n);
      }
      if (names.length <= 1) return names[0] || '';
      return names.slice(0, -1).join(', ') + ' or ' + names[names.length - 1];
    }

    /** True while any pipeline-3 upgrade runs (reported by the queue panel's poll). */
    function _smartUpgradeRunning() {
      if (_smart.indexing) return true;
      try { return (window.queueUpgradeRunningPaths?.().size || 0) > 0; } catch (_) { return false; }
    }

    function _smartUpdateUi() {
      const bar = el('#smartSearchBar');
      const hint = el('#smartSearchHint');
      const spin = el('#smartSearchSpinner');
      const idxBtn = el('#smartSearchIndexBtn');
      const clearBtn = el('#smartSearchClear');
      const input = el('#search');
      if (!bar || !hint) return;
      _smartRenderChips();
      const q = (input?.value || '').trim();
      const on = smartSearchOwnsQuery();
      // Upgrade progress lives in the analysis-queue panel, not here.
      bar.classList.toggle('hidden', !on);
      if (!on) return;
      spin?.classList.toggle('hidden', !_smart.pending);
      clearBtn?.classList.toggle('hidden', !q);
      const upgradable = _smart.needsIndex.filter(n => n.upgrade !== 'reanalyze');
      const upgrading = _smartUpgradeRunning();
      idxBtn?.classList.toggle('hidden', upgrading || _smart.unavailable || upgradable.length === 0);
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
      if (_smart.error) { add(`Smart search failed: ${_smart.error}`); return; }
      const totalIndexed = _smart.needsIndex.reduce((a, n) => a + (n.indexed || 0), 0);
      const nothingIndexed = q && _smart.scores && _smart.scores.size === 0 && _smart.needsIndex.length > 0 && totalIndexed === 0;
      if (nothingIndexed && !_smart.chips.length) {
        add('Describing a scene needs smart search, and these folders aren’t indexed for it yet (they were analysed before pipeline 3.0). Upgrading reads the crops Kestrel already saved — no re-analysis. Species and family names still filter normally.');
        return;
      }
      if (_smart.pending && !_smart.scores) { add('Searching…'); return; }
      if (q) {
        const scenes = `${_smart.matchCount} scene${_smart.matchCount === 1 ? '' : 's'}`;
        const filters = _smartChipNames();
        if (_smart.mode === 'filter' && filters) add(`${scenes} of ${filters}, sorted by quality`);
        else if (_smart.semantic && filters) add(`${scenes} for “${_smart.semantic}”, filtered to ${filters}`);
        else if (_smart.semantic) add(`${scenes} for “${_smart.semantic}”`);
        else add(scenes);
      }
      if (_smart.needsIndex.length) {
        const missing = _smart.needsIndex.reduce((a, n) => a + Math.max(0, (n.total || 0) - (n.indexed || 0)), 0);
        if (missing > 0) {
          add(` · ${missing} photo${missing === 1 ? '' : 's'} need the pipeline 3 upgrade`);
          if (upgrading) add(' (upgrading — see the analysis queue)', 'smart-search-muted');
        }
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

    /** Classify the current query, then show either the filtered timeline or smart-search results.
     *  Called (debounced) on every edit of the search box. */
    async function smartSearchOnInput() {
      const q = _smartQueryNow();
      const my = ++_smart.seq;
      if (!q) {
        Object.assign(_smart, { route: { query: '', kind: 'empty', labels: [] }, query: '', scores: null,
          percents: new Map(), pending: false, error: '', chips: [], semantic: '', mode: '',
          needsIndex: [], reanalyze: [], unavailable: false });
        _smart.disabled.clear();
        _smartUpdateUi();
        await renderScenes();
        return;
      }
      const api = window.pywebview?.api;
      let route = { query: q, kind: 'filter', labels: [] };
      if (api?.classify_search_query) {
        let res = null;
        try { res = await api.classify_search_query(q, _smart.tags.species, _smart.tags.families); } catch (_) { res = null; }
        if (my !== _smart.seq) return;
        // On any failure fall back to the classic filter: it never needs a model.
        if (res?.success && (res.kind === 'semantic' || res.kind === 'filter')) {
          route = { query: q, kind: res.kind, labels: res.labels || [] };
        }
      }
      _smart.route = route;
      if (route.kind !== 'semantic') {
        Object.assign(_smart, { scores: null, percents: new Map(), pending: false, error: '', chips: [],
          semantic: '', mode: '', needsIndex: [], unavailable: false });
        _smart.disabled.clear();
        _smartUpdateUi();
        renderScenes();
        return;
      }
      await _smartRun();
    }
    window.smartSearchOnInput = smartSearchOnInput;

    async function _smartRun() {
      const input = el('#search');
      const q = (input?.value || '').trim();
      const my = ++_smart.seq;
      if (!q || !smartSearchOwnsQuery()) return;
      // Forget un-chipped words that are no longer in the query.
      for (const t of Array.from(_smart.disabled)) {
        if (!q.toLowerCase().includes(String(t).toLowerCase())) _smart.disabled.delete(t);
      }
      const api = window.pywebview?.api;
      if (!api?.smart_search) return;
      _smart.pending = true;
      _smartUpdateUi();
      const scores = new Map();
      const percents = new Map();
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
        for (const im of res.images || []) {
          const k = _smartKey(rp, im.filename);
          scores.set(k, Number(im.score));
          if (im.percent != null) percents.set(k, Number(im.percent));
        }
      }
      if (my !== _smart.seq) return;
      Object.assign(_smart, {
        pending: false, query: q, scores, percents, needsIndex: needs, unavailable, semantic, mode,
        chips: Array.from(chipsByKey.values()),
        error: scores.size || chipsByKey.size ? '' : err,
      });
      await renderScenes();
      _smartUpdateUi();
    }

    /** Why an upgrade request was refused, as toast text. */
    function _smartUpgradeRefusal(res) {
      if (res?.error === 'analysis_running') return 'this folder is being analyzed; upgrade it once analysis finishes.';
      if (res?.error === 'reanalyze_required') return res.reason || 're-analyse this folder to use smart search.';
      return String(res?.error || 'unknown error');
    }

    /** Queue pipeline-3 upgrades for `roots`. The worker runs them one at a
     *  time in this order; progress, Cancel, the folder-tree hourglass and the
     *  end-of-job toast + reload are the analysis-queue panel's job (queue.js
     *  renders get_queue_status().upgrades and calls
     *  smartSearchOnUpgradeFinished). Returns the number of folders queued. */
    async function smartSearchQueueUpgrades(roots, { quietSkips = false } = {}) {
      const api = window.pywebview?.api;
      const list = [...new Set((roots || []).filter(Boolean))];
      if (!api || !list.length) return 0;
      let results = null;
      if (api.upgrade_folders_v3) {
        try { results = (await api.upgrade_folders_v3(list))?.results || null; } catch (_) { results = null; }
      }
      if (!results) {
        const start_fn = api.upgrade_folder_v3 || api.smart_search_index_folder;
        if (!start_fn) return 0;
        results = [];
        for (const rp of list) {
          let r;
          try { r = await start_fn.call(api, rp); } catch (e) { r = { success: false, error: String(e) }; }
          results.push({ path: rp, ...r });
        }
      }
      let queued = 0;
      for (const r of results) {
        if (r?.success && r.job_id) {
          queued += 1;
          if (typeof window.queueTrackUpgradeJob === 'function') window.queueTrackUpgradeJob(r.job_id);
        } else if (!(quietSkips && (r?.error === 'current' || r?.error === 'not_analyzed'))) {
          const why = r?.error === 'current' ? 'already on pipeline 3.0.' : _smartUpgradeRefusal(r);
          showToast(`Couldn’t upgrade ${_smartFolderName(r?.path || '')}: ${why}`, 6000);
        }
      }
      return queued;
    }
    window.smartSearchQueueUpgrades = smartSearchQueueUpgrades;

    async function _smartIndexMissing() {
      if (_smart.indexing) return;
      const targets = _smart.needsIndex.filter(n => n.upgrade !== 'reanalyze').map(n => n.root);
      _smart.indexing = true;
      _smartUpdateUi();
      try {
        await smartSearchQueueUpgrades(targets);
      } finally {
        _smart.indexing = false;
        _smart.needsIndex = [];
        _smartUpdateUi();
      }
    }

    let _smartReloadChain = Promise.resolve();

    /** Called by queue.js once per upgrade job when it leaves 'running'. */
    function smartSearchOnUpgradeFinished(u) {
      try { window.invalidateFolderPipelineStatus?.(); } catch (_) { }
      const name = u?.name || _smartFolderName(u?.path || '');
      if (u.status === 'error') showToast(`Upgrade of ${name} failed: ${u.error || 'unknown error'}`, 6000);
      else if (u.status === 'stopped') showToast(`Upgrade of ${name} paused because analysis started. Try again when it finishes.`, 6000);
      else if (u.status === 'cancelled') showToast(`Upgrade of ${name} stopped — run it again to resume.`, 5000);
      else if (u.status === 'done') showToast(`Upgraded ${name} to pipeline 3.0 — species, quality and smart search are up to date.`, 5000);
      if (u.status !== 'done' && u.status !== 'cancelled') { _smartUpdateUi(); return; }
      // The upgrade rewrote species / quality columns: reload what is open so
      // the grid shows them (and so a later save cannot write the old values back).
      _smartReloadChain = _smartReloadChain.then(async () => {
        try {
          if (dirty && typeof saveCsv === 'function') await saveCsv();
          if (typeof reloadCurrentFolders === 'function') await reloadCurrentFolders();
        } catch (e) { console.warn('[smart-search] reload after upgrade failed', e); }
        if (!_smart.indexing) await _smartRun();
      });
    }
    window.smartSearchOnUpgradeFinished = smartSearchOnUpgradeFinished;
    window.smartSearchOnUpgradesChanged = () => {
      try { _smartUpdateUi(); } catch (_) { }
      try { window.syncFolderUpgradeButtons?.(); } catch (_) { }
    };

    /** Folder action (sidebar / menus): queue one folder's pipeline-3 upgrade.
     *  Safe while other upgrades run: it waits its turn in the queue panel. */
    async function smartSearchUpgradeFolder(rp) {
      if (!rp) return;
      await smartSearchQueueUpgrades([rp]);
    }
    window.smartSearchUpgradeFolder = smartSearchUpgradeFolder;

    (function _wireSmartSearch() {
      const input = el('#search');
      if (!input) return;
      el('#smartSearchClear')?.addEventListener('click', () => {
        input.value = '';
        smartSearchOnInput();
        input.focus();
      });
      el('#smartSearchIndexBtn')?.addEventListener('click', () => { _smartIndexMissing(); });

      // Example queries: shown under the bar while it is focused and empty.
      const box = el('#smartSearchExamples');
      if (!box) return;
      const lead = document.createElement('span');
      lead.className = 'smart-search-muted';
      lead.textContent = 'Try:';
      box.appendChild(lead);
      for (const ex of SMART_SEARCH_EXAMPLES) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'smart-example-chip';
        b.textContent = ex;
        b.addEventListener('mousedown', (e) => e.preventDefault()); // keep focus, avoid blur race
        b.addEventListener('click', () => {
          input.value = ex;
          box.classList.add('hidden');
          smartSearchOnInput();
        });
        box.appendChild(b);
      }
      const sync = () => box.classList.toggle('hidden', !(document.activeElement === input && !input.value.trim()));
      input.addEventListener('focus', sync);
      input.addEventListener('input', sync);
      input.addEventListener('blur', () => box.classList.add('hidden'));
    })();
