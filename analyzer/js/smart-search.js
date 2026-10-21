    // ── Smart search (experimental) ──────────────────────────────────────────
    // Free-text search over what is happening in the photos ("in flight",
    // "eating", "two birds", "blurry", "hidden in branches") instead of the
    // species/family name match the #search box does by default. Opt-in via the
    // ✨ toggle next to the box. When on, the typed query goes to the bridge's
    // smart_search() for every loaded folder (CLIP embeddings of each crop,
    // built at analysis time or by "Index this folder"), and aggregateScenes()
    // hands the scene list to smartSearchApply(), which keeps the matching
    // scenes in score order. Species/family filtering is untouched when off.
    const SMART_SEARCH_LIMIT = 48;
    const SMART_SEARCH_DEBOUNCE_MS = 400;
    const SMART_SEARCH_PLACEHOLDER = '✨ Describe the moment, e.g. in flight, eating, two birds, blurry, hidden in branches';
    const _smart = {
      on: false,
      query: '',          // query the current scores belong to
      seq: 0,             // bumps per request so stale replies are dropped
      scores: null,       // Map(rootPath\0filename -> score) or null
      pending: false,
      matchCount: 0,
      needsIndex: [],     // [{root, indexed, total}]
      unavailable: false,
      error: '',
      indexing: null,     // {current, total, root} while a backfill runs
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

    /** Called by aggregateScenes(): matching scenes, best first, with smartScore/smartRank. */
    function smartSearchApply(list) {
      const out = [];
      for (const s of list) {
        let best = -Infinity;
        for (const r of s.images || []) {
          const v = _smart.scores.get(_smartKey(r.__rootPath || rootPath || '', r.filename));
          if (v !== undefined && v > best) best = v;
        }
        if (best > -Infinity) { s.smartScore = best; out.push(s); }
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
      const q = (input?.value || '').trim();
      const show = _smart.on && (!!q || !!_smart.indexing || _smart.needsIndex.length > 0);
      bar.classList.toggle('hidden', !show);
      if (!show) return;
      spin?.classList.toggle('hidden', !(_smart.pending || _smart.indexing));
      clearBtn?.classList.toggle('hidden', !q);
      idxBtn?.classList.toggle('hidden', !!_smart.indexing || _smart.unavailable || _smart.needsIndex.length === 0);

      hint.textContent = '';
      const add = (text, cls) => {
        const span = document.createElement('span');
        if (cls) span.className = cls;
        span.textContent = text;
        hint.appendChild(span);
      };
      if (_smart.unavailable) { add('Smart search is not available in this build (model files missing).'); return; }
      if (_smart.indexing) {
        const { current, total, root } = _smart.indexing;
        add(`Indexing ${_smartFolderName(root)} for smart search… ${current}/${total || '?'}`);
        return;
      }
      if (_smart.error) { add(`Smart search failed: ${_smart.error}`); return; }
      const totalIndexed = _smart.needsIndex.reduce((a, n) => a + (n.indexed || 0), 0);
      const nothingIndexed = _smart.scores && _smart.scores.size === 0 && _smart.needsIndex.length > 0 && totalIndexed === 0;
      if (nothingIndexed) {
        add('This folder hasn’t been indexed for smart search yet. Indexing reads the bird crops Kestrel already saved; no re-analysis needed.');
        return;
      }
      if (_smart.pending && !_smart.scores) { add('Searching…'); return; }
      if (q) {
        add(`${_smart.matchCount} match${_smart.matchCount === 1 ? '' : 'es'} for “${q}” · `);
        add('experimental', 'smart-search-exp');
      }
      if (_smart.needsIndex.length) {
        const missing = _smart.needsIndex.reduce((a, n) => a + Math.max(0, (n.total || 0) - (n.indexed || 0)), 0);
        add(` · ${missing} photo${missing === 1 ? '' : 's'} not indexed yet`);
      }
    }

    async function _smartRun() {
      const input = el('#search');
      const q = (input?.value || '').trim();
      const my = ++_smart.seq;
      if (!_smart.on) return;
      if (!q) {
        _smart.query = '';
        _smart.scores = null;
        _smart.pending = false;
        _smart.error = '';
        _smartUpdateUi();
        renderScenes();
        return;
      }
      const api = window.pywebview?.api;
      if (!api?.smart_search) return;
      _smart.pending = true;
      _smartUpdateUi();
      const scores = new Map();
      const needs = [];
      let unavailable = false;
      let err = '';
      for (const rp of _smartRoots()) {
        let res;
        try { res = await api.smart_search(rp, q, SMART_SEARCH_LIMIT); } catch (e) { err = String(e?.message || e); continue; }
        if (my !== _smart.seq) return;
        if (!res?.success) {
          if (res?.error === 'smart_search_unavailable') unavailable = true;
          else err = String(res?.error || 'unknown error');
          continue;
        }
        if (res.needs_index) needs.push({ root: rp, indexed: res.indexed_images || 0, total: res.indexable_images || 0 });
        for (const im of res.images || []) scores.set(_smartKey(rp, im.filename), Number(im.score));
      }
      if (my !== _smart.seq) return;
      Object.assign(_smart, { pending: false, query: q, scores, needsIndex: needs, unavailable, error: scores.size ? '' : err });
      await renderScenes();
      _smartUpdateUi();
    }

    async function _smartIndexMissing() {
      const api = window.pywebview?.api;
      if (!api?.smart_search_index_folder || _smart.indexing) return;
      const targets = _smart.needsIndex.map(n => n.root);
      for (const rp of targets) {
        _smart.indexing = { current: 0, total: 0, root: rp };
        _smartUpdateUi();
        let start;
        try { start = await api.smart_search_index_folder(rp); } catch (e) { start = { success: false, error: String(e) }; }
        if (!start?.success) {
          _smart.indexing = null;
          const why = start?.error === 'analysis_running'
            ? 'this folder is being analyzed; index it once analysis finishes.'
            : String(start?.error || 'unknown error');
          showToast(`Couldn’t index ${_smartFolderName(rp)}: ${why}`, 6000);
          continue;
        }
        // Poll like the Perch share progress.
        for (;;) {
          await new Promise(r => setTimeout(r, 500));
          let p;
          try { p = (await api.get_smart_search_index_progress(start.job_id))?.progress; } catch (_) { p = null; }
          if (!p) break;
          _smart.indexing = { current: p.current || 0, total: p.total || 0, root: rp };
          _smartUpdateUi();
          if (p.done) {
            if (p.phase === 'error') showToast(`Indexing failed: ${p.error || 'unknown error'}`, 6000);
            else if (p.phase === 'stopped') showToast('Indexing paused because analysis started. Try again when it finishes.', 6000);
            break;
          }
        }
      }
      _smart.indexing = null;
      _smart.needsIndex = [];
      _smartUpdateUi();
      await _smartRun();
    }

    function _smartSetOn(on) {
      _smart.on = !!on;
      if (!_smart.on) {
        _smart.seq++;
        Object.assign(_smart, { scores: null, query: '', pending: false, needsIndex: [], error: '', unavailable: false });
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
