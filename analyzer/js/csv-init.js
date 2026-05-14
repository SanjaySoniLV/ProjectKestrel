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
    hydrateSettingsFromServer();

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

    // Wire "Change root…" button in the tree panel
    const treeChangeRootBtn = document.getElementById('treeChangeRoot');
    if (treeChangeRootBtn) {
      treeChangeRootBtn.addEventListener('click', async () => {
        if (!hasPywebviewApi) { const ready = await waitForPywebview(); if (!ready) return; }
        if (!window.pywebview?.api?.choose_directory) return;
        setStatus('Opening folder picker…');
        const folderPath = await window.pywebview.api.choose_directory();
        if (folderPath) {
          treeExpandedPaths.clear();
          checkedFolderPaths.clear();
          const treeScanned = await scanFolderTree(folderPath);
          if (treeScanned && !folderTreeRootHasKestrel) {
            setStatus('Select a folder from the tree below to load its scenes');
          } else {
            await loadFolderFromPath(folderPath);
          }
        } else {
          setStatus('Folder selection cancelled');
        }
      });
    }

    // Wire "Check all / Check none" buttons
    const treeCheckAllBtn = document.getElementById('treeCheckAll');
    if (treeCheckAllBtn) treeCheckAllBtn.addEventListener('click', checkAllTreeFolders);
    const treeCheckNoneBtn = document.getElementById('treeCheckNone');
    if (treeCheckNoneBtn) treeCheckNoneBtn.addEventListener('click', checkNoneTreeFolders);

    // Wire "Load checked" button (removed from HTML; kept as no-op guard)
    const treeLoadSelectedBtn = document.getElementById('treeLoadSelected');
    if (treeLoadSelectedBtn) {
      treeLoadSelectedBtn.addEventListener('click', async () => {
        if (checkedFolderPaths.size === 0) return;
        await loadMultipleFolders(Array.from(checkedFolderPaths));
      });
    }

