    (function initSortBy() {
      const sortSel = document.getElementById('sortBy');
      if (!sortSel) return;
      try { sortSel.value = getSetting('sortBy', 'captureTime'); } catch { sortSel.value = 'captureTime'; }
    })();

    // Apply initial auto-save visibility from cached localStorage settings
    (function initAutoSaveVisibility() {
      _autoSaveEnabled = getSetting('auto_save_enabled', true) !== false;
      if (!_autoSaveEnabled) {
        clearTimeout(_autoSaveTimer);
        _autoSaveTimer = null;
      }
      _updateSaveRevertVisibility();
    })();

    // Group-by-folder toggle
    (function initGroupByFolder() {
      const t = document.getElementById('groupByFolder');
      if (!t) return;
      try { t.checked = getSetting('groupByFolder', true); } catch { }
      t.addEventListener('change', () => {
        const s = loadSettings(); s.groupByFolder = !!t.checked; saveSettings(s); renderScenes();
      });
    })();

    // Group-by-capture-time toggle
    (function initGroupByTime() {
      const t = document.getElementById('groupByTime');
      if (!t) return;
      try { t.checked = getSetting('groupByTime', true); } catch { }
      t.addEventListener('change', () => {
        const s = loadSettings(); s.groupByTime = !!t.checked; saveSettings(s); renderScenes();
      });
    })();

    // Show bird thumbnails toggle
    (function initShowBirdThumbs() {
      const t = document.getElementById('showBirdThumbs');
      if (!t) return;
      try { t.checked = getSetting('showBirdThumbs', false); } catch { }
      t.addEventListener('change', () => {
        const s = loadSettings(); s.showBirdThumbs = !!t.checked; saveSettings(s); renderScenes();
      });
    })();

    // Scroll position indicator — shows current folder/time-group while scrolling
    (function initScrollPositionIndicator() {
      const mainEl = document.querySelector('main');
      const indicator = document.getElementById('scrollPositionIndicator');
      if (!mainEl || !indicator) return;
      let hideTimer = null;
      mainEl.addEventListener('scroll', () => {
        // Track both folder headers and timeline day banners
        const headers = [...sceneGrid.querySelectorAll('.folder-group-header, .timeline-day-banner')];
        if (!headers.length) { indicator.style.opacity = '0'; return; }
        const mainRect = mainEl.getBoundingClientRect();
        const thresholdY = mainRect.top + mainRect.height * 0.25;
        let bestHeader = null;
        for (const h of headers) {
          const r = h.getBoundingClientRect();
          if (r.top <= thresholdY) bestHeader = h;
          else if (!bestHeader) { bestHeader = h; break; }
        }
        if (!bestHeader) { indicator.style.opacity = '0'; return; }
        const nameEl = bestHeader.querySelector('.folder-group-name');
        const text = nameEl ? nameEl.textContent.trim() : bestHeader.textContent.trim();
        if (!text) { indicator.style.opacity = '0'; return; }
        indicator.textContent = text;
        indicator.style.opacity = '1';
        clearTimeout(hideTimer);
        hideTimer = setTimeout(() => { indicator.style.opacity = '0'; }, 1800);
      }, { passive: true });
    })();

    // Multi-select merge action bar
    const selectMergeBtn = document.getElementById('selectMergeBtn');
    if (selectMergeBtn) selectMergeBtn.addEventListener('click', executeSelectionMerge);
    const selectClearBtn = document.getElementById('selectClearBtn');
    if (selectClearBtn) selectClearBtn.addEventListener('click', () => { selectedSceneIds.clear(); _lastSelectedIdx = -1; updateSelectionUI(); });
    document.addEventListener('keydown', ev => { if (ev.key === 'Escape' && !document.querySelector('dialog[open]')) { if (selectedSceneIds.size > 0) { selectedSceneIds.clear(); _lastSelectedIdx = -1; updateSelectionUI(); } _clearGridFocus(); } });
    // Revert button
    const revertBtn = el('#revertCsv');
    if (revertBtn) revertBtn.addEventListener('click', () => {
      if (!_cleanSnapshot) return;
      if (!confirm('Discard all unsaved changes and revert to the last saved state?')) return;
      applySnapshot();
    });

    const zoomInBtn = el('#zoomIn');
    const zoomOutBtn = el('#zoomOut');
    if (zoomInBtn) zoomInBtn.addEventListener('click', () => { uiZoom = Math.min(1.4, uiZoom + 0.1); applyZoom(); });
    if (zoomOutBtn) zoomOutBtn.addEventListener('click', () => { uiZoom = Math.max(0.7, uiZoom - 0.1); applyZoom(); });

    // Initialize toolbar toggle for scene-level manual-reviewed filter
    (function initScenesManualFilter() {
      const t = document.getElementById('filterScenesManualRated');
      if (!t) return;
      try { t.checked = !!getSetting('onlyManualRatedScenes', false); } catch { }
      t.addEventListener('change', () => {
        const s = loadSettings(); s.onlyManualRatedScenes = !!t.checked; saveSettings(s);
        renderScenes();
      });
    })();

    // Initialize secondary species/families inclusion toggle
    (function initIncludeSecondary() {
      const t = document.getElementById('includeSecondarySpecies');
      if (!t) return;
      try { t.checked = getSetting('includeSecondarySpecies', false); } catch { }
      t.addEventListener('change', () => {
        const s = loadSettings(); s.includeSecondarySpecies = !!t.checked; saveSettings(s); renderScenes();
      });
    })();

    // Merge scenes feature
    function computeAllScenesForMerge() {
      // Group rows by scene_count; keep simple stats and representative
      const groups = new Map();
      for (const r of rows) {
        const id = r.scene_count;
        if (!groups.has(id)) groups.set(id, []);
        groups.get(id).push(r);
      }
      const list = [];
      for (const [id, arr] of groups) {
        // pick representative by max quality
        let rep = arr[0];
        for (const r of arr) if (parseNumber(r.quality) > parseNumber(rep.quality)) rep = r;
        const maxQ = Math.max(...arr.map(a => parseNumber(a.quality)));
        const rowRp = arr[0]?.__rootPath || rootPath || '';
        const sdScene = rowRp ? _scenedata[rowRp]?.scenes?.[id] : null;
        const name = sdScene?.name || (arr.find(a => (a.scene_name || '').trim().length)?.scene_name || '').trim();
        list.push({ id, imageCount: arr.length, maxQuality: maxQ, sceneName: name, repPath: (rep.export_path || rep.crop_path || ''), repFilename: rep.filename || '' });
      }
      // Sort numerically by id where possible
      return list.sort((a, b) => parseNumber(a.id) - parseNumber(b.id));
    }

    function openMergeDialog() {
      const dlg = document.getElementById('mergeDlg');
      const listEl = document.getElementById('mergeList');
      const summary = document.getElementById('mergeSummary');
      const applyBtn = document.getElementById('mergeApply');
      const targetInput = document.getElementById('mergeTargetId');
      const modeRadios = Array.from(document.querySelectorAll('input[name="mergeTargetMode"]'));

      const sceneList = computeAllScenesForMerge();
      listEl.innerHTML = '';

      const sel = new Set();

      function updateSummary() {
        const ids = Array.from(sel);
        const n = ids.length;
        const targetMode = modeRadios.find(r => r.checked)?.value || 'min';
        const targetId = targetMode === 'manual' && targetInput.value ? String(targetInput.value) : (n ? String(ids.map(x => parseNumber(x)).sort((a, b) => a - b)[0]) : '');
        const totalImgs = sceneList.filter(s => ids.includes(String(s.id))).reduce((acc, s) => acc + s.imageCount, 0);
        summary.textContent = n < 2 ? 'Select at least two scenes to merge.' : `Merging ${n} scenes into Scene ${targetId} (${totalImgs} images).`;
        applyBtn.disabled = n < 2 || !targetId;
      }

      // Build rows: [thumb] [checkbox + title] [count]
      for (const s of sceneList) {
        const row = document.createElement('div');
        row.style.display = 'contents';

        // Thumb cell
        const cThumb = document.createElement('div');
        const thumb = document.createElement('div'); thumb.className = 'thumb'; thumb.style.aspectRatio = '16/10';
        const img = document.createElement('img'); img.alt = s.repFilename || 'No preview'; img.loading = 'lazy';
        (async () => { const url = await getBlobUrlForPath(s.repPath); if (url) img.src = url; })();
        thumb.appendChild(img); cThumb.appendChild(thumb);

        // Title + checkbox cell
        const cTitle = document.createElement('div');
        const cb = document.createElement('input'); cb.type = 'checkbox'; cb.dataset.id = String(s.id); cb.style.marginRight = '8px';
        cb.addEventListener('change', () => { if (cb.checked) sel.add(cb.dataset.id); else sel.delete(cb.dataset.id); updateSummary(); });
        const title = document.createElement('span'); title.textContent = `Scene ${s.id}${s.sceneName ? ` — ${s.sceneName}` : ''}`; title.title = title.textContent;
        cTitle.appendChild(cb); cTitle.appendChild(title);

        // Count cell
        const cCount = document.createElement('div'); cCount.className = 'muted'; cCount.style.textAlign = 'right'; cCount.textContent = `${s.imageCount} images`;

        row.appendChild(cThumb); row.appendChild(cTitle); row.appendChild(cCount);
        listEl.appendChild(row);
      }

      // Wire radios
      modeRadios.forEach(r => r.onchange = updateSummary);
      targetInput.oninput = updateSummary;

      document.getElementById('mergeCancel').onclick = () => dlg.close();
      document.getElementById('mergeApply').onclick = () => {
        const ids = Array.from(sel).map(String);
        if (ids.length < 2) return;
        const targetMode = modeRadios.find(r => r.checked)?.value || 'min';
        let targetId = targetMode === 'manual' && targetInput.value ? String(targetInput.value) : String(ids.map(x => parseNumber(x)).sort((a, b) => a - b)[0]);
        if (!targetId) return;
        let changed = 0;
        for (const r of rows) {
          const idStr = String(r.scene_count);
          if (ids.includes(idStr) && idStr !== targetId) { r.scene_count = targetId; changed++; }
        }
        // Update scenedata: move filenames from non-target scenes into target scene
        let rpForMerge = '';
        if (hasPywebviewApi && changed > 0) {
          const rowSample = rows.find(r => ids.includes(String(r.scene_count)));
          rpForMerge = rowSample?.__rootPath || rootPath || '';
          if (rpForMerge) {
            const sd = _initScenedata(rpForMerge);
            const allMovedFiles = new Set();
            for (const id of ids) {
              if (id !== targetId && sd.scenes[id]) {
                for (const f of sd.scenes[id].image_filenames || []) allMovedFiles.add(f);
                delete sd.scenes[id];
              }
            }
            if (!sd.scenes[targetId]) {
              sd.scenes[targetId] = { scene_id: targetId, image_filenames: [], name: '', status: 'pending', user_tags: { species: [], families: [], finalized: false } };
            }
            for (const f of allMovedFiles) {
              if (!sd.scenes[targetId].image_filenames.includes(f)) sd.scenes[targetId].image_filenames.push(f);
            }
          }
        }
        if (changed) { markDirty(rpForMerge); setStatus(`Merged scenes into ${targetId}. ${changed} rows updated.`); }
        renderScenes();
        dlg.close();
      };

      updateSummary();
      dlg.showModal();
    }

    // Init
    loadVersionBadge();
    initPerchAuth();
    setStatus('Open your photo folder (the one that contains .kestrel) or select kestrel_database.csv');
    // Hydrate from backend, then re-render recents chips. Must waitForPywebview
    // first — at script-parse time the bridge isn't ready yet, so calling
    // hydrateSettingsFromServer() directly bails (its first guard checks
    // window.pywebview?.api?.get_settings) and the recents render sees an
    // empty localStorage. Wait for the bridge, THEN hydrate + render.
    //
    // Cold-start retry: hydrateSettingsFromServer silently no-ops on any
    // failure (bridge not fully wired yet, slow first get_settings call).
    // When that happens localStorage stays empty, renderFolderRecentsChips
    // sees no recents, and the row hides until the next app launch. One
    // retry catches the transient case without spamming on the steady-state
    // empty-recents path.
    (async () => {
      try {
        await waitForPywebview();
        await hydrateSettingsFromServer();
        if (typeof renderFolderRecentsChips === 'function') renderFolderRecentsChips();
        const hasRecents = (() => {
          try {
            const s = (typeof loadSettings === 'function') ? loadSettings() : {};
            return Array.isArray(s.folder_recents) && s.folder_recents.length > 0;
          } catch { return false; }
        })();
        if (!hasRecents) {
          await new Promise(r => setTimeout(r, 1500));
          await hydrateSettingsFromServer();
          if (typeof renderFolderRecentsChips === 'function') renderFolderRecentsChips();
        }
      } catch (e) { console.warn('[recents] init chain failed:', e); }
    })();

    // If a queue was running before this page loaded (e.g. page refresh), re-attach the polling
    (async () => {
      try {
        const status = await apiGetQueueStatus();
        if (status && (status.items || []).length > 0) {
          renderQueuePanel(status);
          if (status.running) startPollingQueue();
        }
      } catch (_) { }
      try {
        await maybeHandleStartupRecovery();
      } catch (_) { }
    })();

    // Legacy "Change root…" button — the element is now permanently hidden in
    // HTML (kept only so this handler can't NPE on a missing element). The
    // user-facing replacement is "+ 📂 Load Folders…" which is additive.
    // No handler wired; intentional no-op.

    // Wire "Select All / Select None" toggle (one button is always hidden via
    // .hidden — see updateSelectToggleVisibility in multi-folder.js).
    const treeCheckAllBtn = document.getElementById('treeCheckAll');
    if (treeCheckAllBtn) treeCheckAllBtn.addEventListener('click', checkAllTreeFolders);
    const treeCheckNoneBtn = document.getElementById('treeCheckNone');
    if (treeCheckNoneBtn) treeCheckNoneBtn.addEventListener('click', checkNoneTreeFolders);

    // Wire Clear button — unload every root, reset scenes, return to welcome
    // panel. Recents in settings are preserved so the user can re-click them.
    const treeClearBtn = document.getElementById('treeClear');
    console.log('[clear] wiring treeClear button — element found:', !!treeClearBtn, 'disabled:', treeClearBtn?.disabled);
    if (treeClearBtn) {
      treeClearBtn.addEventListener('click', () => {
        console.log('[clear] click — roots before:', folderTreeRootOrder.length, 'checked before:', checkedFolderPaths.size);
        clearAllFolderRoots();
        console.log('[clear] after clearAllFolderRoots — roots:', folderTreeRootOrder.length, 'checked:', checkedFolderPaths.size);
        // Reset loaded scenes too so the welcome panel returns.
        try {
          rows = [];
          header = [];
          if (typeof renderScenes === 'function') renderScenes();
        } catch (e) { console.warn('[clear] renderScenes error', e); }
        setStatus('Idle');
      });
    }

    // Wire "Load checked" button (removed from HTML; kept as no-op guard)
    const treeLoadSelectedBtn = document.getElementById('treeLoadSelected');
    if (treeLoadSelectedBtn) {
      treeLoadSelectedBtn.addEventListener('click', async () => {
        if (checkedFolderPaths.size === 0) return;
        await loadMultipleFolders(Array.from(checkedFolderPaths));
      });
    }

    // ── Recents chips (2H) + empty hint context (2I) ──────────────────────────
    // Read folder_recents from settings on startup, probe existence via
    // inspect_folders, render chips into #folderRecentsRow. Each chip click
    // calls addFolderRoot for that specific path (same flow as + Load Folders).
    function _ellipsizePathMiddle(path, maxLen = 28) {
      if (!path) return '';
      const p = path.replace(/\\/g, '/');
      if (p.length <= maxLen) return p;
      // Keep the drive/first segment + the last segment, ellipsis in the middle.
      const parts = p.split('/').filter(Boolean);
      if (parts.length <= 2) return p.slice(0, maxLen - 1) + '…';
      const first = parts[0];
      const last = parts[parts.length - 1];
      const truncated = `${first}/…/${last}`;
      return truncated.length <= maxLen ? truncated : (truncated.slice(0, maxLen - 1) + '…');
    }

    async function renderFolderRecentsChips() {
      const row = document.getElementById('folderRecentsRow');
      if (!row) { console.log('[recents] no row element'); return; }
      const s = (typeof loadSettings === 'function') ? loadSettings() : {};
      const recents = Array.isArray(s.folder_recents) ? s.folder_recents.slice(0, 8) : [];
      console.log('[recents] render — settings.folder_recents:', s.folder_recents, '-> recents:', recents);
      if (recents.length === 0) {
        console.log('[recents] empty list — hiding row');
        row.classList.add('hidden');
        row.innerHTML = '';
        updateEmptyHintCopy();
        return;
      }
      // Probe existence via inspect_folders so missing-drive paths drop out.
      // Note: inspect_folders returns success=true ONLY if every input path is
      // a valid existing folder (require_exists=True in _validate_root_dir).
      // The results dict is keyed by os.path.realpath which may not match the
      // original strings (case canonicalization etc.), so we DON'T do per-path
      // result lookup — instead we treat success=true as "all exist, show all"
      // and only filter when the backend gives us an explicit invalid_paths list.
      let available = recents;
      try {
        if (hasPywebviewApi && window.pywebview?.api?.inspect_folders) {
          const res = await window.pywebview.api.inspect_folders(recents);
          console.log('[recents] inspect_folders result:', res);
          if (res && res.success) {
            available = recents; // all valid
          } else if (res && Array.isArray(res.invalid_paths)) {
            const normRoot = q => (q || '').replace(/\\/g, '/').replace(/\/+$/, '');
            const invalid = new Set(res.invalid_paths.map(normRoot));
            available = recents.filter(p => !invalid.has(normRoot(p)));
          }
        }
      } catch (e) { console.warn('[recents] inspect_folders threw:', e); }
      // Hide chips for paths that are ALREADY loaded as roots in the tree —
      // no point showing a "click to load" chip for a folder you already see.
      // Comparison uses the same normalization as addFolderRoot's _normRoot.
      try {
        const normRoot = q => (q || '').replace(/\\/g, '/').replace(/\/+$/, '');
        const loaded = new Set(folderTreeRootOrder.map(normRoot));
        available = available.filter(p => !loaded.has(normRoot(p)));
      } catch (e) { /* if folderTreeRootOrder isn't accessible, show all */ }
      console.log('[recents] available after probe + loaded-filter:', available);
      if (available.length === 0) {
        console.log('[recents] all filtered out — hiding row');
        row.classList.add('hidden');
        row.innerHTML = '';
        updateEmptyHintCopy();
        return;
      }
      row.innerHTML = '';
      // "Recents" header + a divider (CSS) separating the chips from the tree.
      const header = document.createElement('div');
      header.className = 'folder-recents-header';
      header.textContent = 'Recents';
      row.appendChild(header);
      const chips = document.createElement('div');
      chips.className = 'folder-recents-chips';
      for (const path of available) {
        const wrap = buildFolderRecentsChip(
          path,
          async () => {
            const chipBtn = wrap.querySelector('.folder-recents-chip');
            if (chipBtn) chipBtn.disabled = true;
            try {
              const r = await addFolderRoot(path);
              if (r && r.added) {
                debouncedAutoLoad();
                if (typeof persistFolderRecentsBump === 'function') {
                  await persistFolderRecentsBump(path);
                }
              }
            } finally {
              const chipBtn = wrap.querySelector('.folder-recents-chip');
              if (chipBtn) chipBtn.disabled = false;
            }
          },
          (p) => { if (typeof persistFolderRecentsRemove === 'function') persistFolderRecentsRemove(p); },
        );
        chips.appendChild(wrap);
      }
      row.appendChild(chips);
      row.classList.remove('hidden');
      updateEmptyHintCopy();
    }

    function updateEmptyHintCopy() {
      const hint = document.getElementById('folderTreeEmptyHint');
      if (!hint) return;
      const row = document.getElementById('folderRecentsRow');
      const hasRecents = row && !row.classList.contains('hidden') && row.children.length > 0;
      hint.innerHTML = hasRecents
        ? 'Click a recent above, or <b>+ 📂 Load Folders…</b> to pick a new one.'
        : 'No folders loaded.<br />Click <b>+ 📂 Load Folders…</b> to get started.';
    }

    // Initial render of recents on startup.
    renderFolderRecentsChips();

    // ── Zoom slider (2J) ──────────────────────────────────────────────────────
    // Drives the existing uiZoom / applyZoom pipeline (blob-zoom.js) so behavior
    // matches the +/- buttons. Slider range 70..140 mirrors the button clamps
    // (Math.min(1.4, …) / Math.max(0.7, …) in the existing wiring above).
    const _zoomSlider = document.getElementById('zoomSlider');
    if (_zoomSlider) {
      // Reflect the current uiZoom into the slider on init.
      try { _zoomSlider.value = String(Math.round(uiZoom * 100)); } catch (e) { /* ignore */ }
      _zoomSlider.addEventListener('input', (e) => {
        const pct = parseInt(e.target.value, 10) || 100;
        uiZoom = Math.max(0.7, Math.min(1.4, pct / 100));
        applyZoom();
      });
      // Sync slider when the existing +/- buttons fire (they update uiZoom
      // directly, so we listen on them post-click).
      const _syncSlider = () => {
        try { _zoomSlider.value = String(Math.round(uiZoom * 100)); } catch (e) { /* ignore */ }
      };
      document.getElementById('zoomIn')?.addEventListener('click', _syncSlider);
      document.getElementById('zoomOut')?.addEventListener('click', _syncSlider);
    }

