    // Folder Actions dropdown — module-scoped state so only one menu is open
    // at a time across multiple folder groups. The trigger element is tracked
    // so re-clicking the same trigger closes (toggle behavior).
    let _openFolderActionsMenu = null;
    let _openFolderActionsTrigger = null;

    function _closeFolderActionsMenu() {
      if (_openFolderActionsMenu && _openFolderActionsMenu.parentNode) {
        _openFolderActionsMenu.parentNode.removeChild(_openFolderActionsMenu);
      }
      _openFolderActionsMenu = null;
      _openFolderActionsTrigger = null;
    }

    // One-time global listeners for outside-click and Escape.
    document.addEventListener('mousedown', (ev) => {
      if (!_openFolderActionsMenu) return;
      if (_openFolderActionsMenu.contains(ev.target)) return;
      if (_openFolderActionsTrigger && _openFolderActionsTrigger.contains(ev.target)) return;
      _closeFolderActionsMenu();
    });
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && _openFolderActionsMenu) _closeFolderActionsMenu();
    });

    /** True when a folder can be upgraded in place and is not on pipeline 3.x yet. */
    function _isUpgradableTo3(up) {
      return up?.status === 'upgradable' && (Number(up.pipeline_major) || 0) < 3;
    }

    /** ``pipelineStatus`` is the folder's upgrade status (folderPipelineStatus);
     *  the Upgrade entry is only listed while ``_isUpgradableTo3`` holds. */
    function _buildFolderActionsMenu(folderPath, refreshCallback, pipelineStatus = null) {
      const menu = document.createElement('div');
      menu.className = 'folder-group-actions-menu';

      const items = [
        { icon: 'folder-open', label: 'Open in File Explorer',
          run: () => window.pywebview.api.open_file_explorer(folderPath) },
        { icon: 'file-text', label: 'Write Photo Metadata',
          run: () => writeMetadataForFolder(folderPath) },
        { icon: 'gear', label: 'Folder settings…',
          run: () => openFolderSettingsDialog(folderPath) },
        ...(_isUpgradableTo3(pipelineStatus) ? [
          { icon: 'sparkle', label: 'Upgrade to Analysis Version 3.0…',
            run: () => upgradeFolderToV3(folderPath) },
        ] : []),
        { divider: true },
        { icon: 'trash', label: 'Clear Kestrel Analysis Data', danger: true,
          run: () => {
            const folderName = (folderPath || '').split(/[\\/]/).filter(Boolean).pop() || folderPath;
            clearKestrelDataForFolder(folderPath, folderName, refreshCallback);
          } },
      ];

      for (const it of items) {
        if (it.divider) {
          const d = document.createElement('div');
          d.className = 'folder-group-actions-divider';
          menu.appendChild(d);
          continue;
        }
        const row = document.createElement('div');
        row.className = 'folder-group-actions-item' + (it.danger ? ' folder-group-actions-item--danger' : '');
        row.innerHTML = `${KestrelIcon(it.icon)} ${it.label}`;
        row.addEventListener('click', (ev) => {
          ev.stopPropagation();
          _closeFolderActionsMenu();
          try { it.run(); } catch (e) { console.error('[folder-actions]', it.label, e); }
        });
        menu.appendChild(row);
      }
      return menu;
    }

    /** Folder action: upgrade an analysed folder to pipeline 3.0 from its stored
     *  crops (species re-ID, SigLIP quality, smart-search index). Local only. */
    async function upgradeFolderToV3(folderPath) {
      const api = window.pywebview?.api;
      if (!api?.smart_search_status) return;
      let st = null;
      try { st = await api.smart_search_status(folderPath); } catch (_) { st = null; }
      const up = st?.upgrade || {};
      const name = (folderPath || '').split(/[\\/]/).filter(Boolean).pop() || folderPath;
      if (up.status === 'current') {
        showToast(`${name} is already on analysis version 3.0.`, 4000);
        return;
      }
      if (up.status === 'reanalyze' || up.status === 'not_analyzed') {
        showToast(`${name}: ${up.reason || 'analyze this folder again to use analysis version 3.0.'}`, 7000);
        return;
      }
      const ok = await KestrelDialog.confirm('upgradeFolder', { name, skipped: '' });
      if (!ok) return;
      if (typeof smartSearchUpgradeFolder === 'function') await smartSearchUpgradeFolder(folderPath);
    }

    // Per-folder pipeline status for the folder headers (version label + the
    // Upgrade button). smart_search_status reads the folder's CSV, so results
    // are cached briefly; an upgrade finishing clears the cache.
    const _PIPELINE_STATUS_TTL_MS = 30000;
    const _pipelineStatusCache = new Map(); // folderPath -> {at, promise}

    function folderPipelineStatus(folderPath) {
      const api = window.pywebview?.api;
      if (!api?.smart_search_status || !folderPath) return Promise.resolve(null);
      const hit = _pipelineStatusCache.get(folderPath);
      if (hit && Date.now() - hit.at < _PIPELINE_STATUS_TTL_MS) return hit.promise;
      const promise = Promise.resolve(api.smart_search_status(folderPath))
        .then(st => (st?.success ? st.upgrade || null : null))
        .catch(() => null);
      _pipelineStatusCache.set(folderPath, { at: Date.now(), promise });
      return promise;
    }

    function invalidateFolderPipelineStatus() { _pipelineStatusCache.clear(); }
    window.invalidateFolderPipelineStatus = invalidateFolderPipelineStatus;

    /** Header label for a folder's analysis pipeline ("Pipeline 2.0.4"). */
    function _pipelineVersionLabel(up) {
      if (!up || up.status === 'not_analyzed') return '';
      if ((Number(up.pipeline_major) || 0) >= 3) return 'Analysis version 3.0';
      return up.version ? `Analysis version ${up.version}` : 'Analysis version < 2.0';
    }

    /** Show/hide every header Upgrade button from its folder's status and the
     *  queue panel's queued/running upgrades. Called after render and whenever
     *  the set of queued/running upgrades changes. */
    function syncFolderUpgradeButtons() {
      let busy = new Set();
      try { busy = window.queueUpgradeRunningPaths?.() || new Set(); } catch (_) { }
      for (const btn of document.querySelectorAll('.folder-upgrade-btn[data-folder-path]')) {
        const fp = btn.dataset.folderPath;
        const queued = busy.has(String(fp).replace(/\\/g, '/'));
        btn.disabled = queued;
        btn.innerHTML = queued ? `${KestrelIcon('hourglass')} In upgrade queue` : `${KestrelIcon('sparkle')} Upgrade to 3.0`;
        btn.classList.toggle('hidden', !(queued || btn.dataset.upgradable === '1'));
      }
      syncUpgradeAllButton();
    }
    window.syncFolderUpgradeButtons = syncFolderUpgradeButtons;

    /** Show the left panel's "Upgrade All to 3.0" row only while a loaded folder can be upgraded. */
    async function syncUpgradeAllButton() {
      const row = document.getElementById('treeUpgradeAllRow');
      if (!row) return;
      const loaded = [...new Set((rows || []).map(r => r.__rootPath).filter(Boolean))];
      const statuses = await Promise.all(loaded.map(fp => folderPipelineStatus(fp)));
      row.classList.toggle('hidden', !statuses.some(_isUpgradableTo3));
    }
    window.syncUpgradeAllButton = syncUpgradeAllButton;
    document.getElementById('treeUpgradeAll')?.addEventListener('click', () => { upgradeAllLoadedFoldersToV3(); });

    /** Folder action: queue every loaded folder that can be upgraded. */
    async function upgradeAllLoadedFoldersToV3() {
      const loaded = [...new Set((rows || []).map(r => r.__rootPath).filter(Boolean))];
      if (!loaded.length) return;
      invalidateFolderPipelineStatus();
      const statuses = await Promise.all(loaded.map(fp => folderPipelineStatus(fp)));
      const targets = loaded.filter((fp, i) => statuses[i]?.status === 'upgradable');
      const needReanalysis = statuses.filter(u => u?.status === 'reanalyze').length;
      if (!targets.length) {
        showToast(needReanalysis
          ? `No loaded folder can be upgraded in place; ${needReanalysis} need${needReanalysis === 1 ? 's' : ''} to be analyzed again.`
          : 'All loaded folders are already on analysis version 3.0.', 6000);
        return;
      }
      const skipped = needReanalysis ? KestrelDialog.text('upgradeFoldersSkipped', { count: needReanalysis }) : '';
      const ok = targets.length === 1
        ? await KestrelDialog.confirm('upgradeFolder', { name: folderBaseName(targets[0]), skipped })
        : await KestrelDialog.confirm('upgradeFolders', { count: targets.length, skipped });
      if (!ok) return;
      if (typeof smartSearchQueueUpgrades === 'function') {
        await smartSearchQueueUpgrades(targets, { quietSkips: true });
      }
      syncFolderUpgradeButtons();
    }

    async function renderScenes() {
      const myVer = ++_renderScenesVersion;
      const minC = parseFloat(el('#speciesConf').value) || 0;
      const search = el('#search').value;
      const sortBy = el('#sortBy').value;
      // Smart-search results are shown flat, in score order (no folder/time grouping).
      const _searchOn = !!String(search || '').trim();
      const _smartActive = typeof smartSearchIsActive === 'function' && smartSearchIsActive();
      const groupByFolder = !_smartActive && (document.getElementById('groupByFolder')?.checked ?? getSetting('groupByFolder', true));
      const groupByTime = !_smartActive && (document.getElementById('groupByTime')?.checked ?? getSetting('groupByTime', true));
      const showBirdThumbs = document.getElementById('showBirdThumbs')?.checked ?? getSetting('showBirdThumbs', false);
      // Widen each card's grid track when bird-crop thumbs are shown, so the
      // main thumb keeps its "no-crop" height and the crop slots into the
      // extra width rather than stealing height from the main image.
      sceneGrid.classList.toggle('grid--bird-thumbs', !!showBirdThumbs);
      const includeSecondaryCheckbox = document.getElementById('includeSecondarySpecies');
      const includeSecondary = includeSecondaryCheckbox ? includeSecondaryCheckbox.checked : !!getSetting('includeSecondarySpecies', false);
      const includeFamilies = true;
      scenes = aggregateScenes(minC, search, sortBy, includeSecondary, includeFamilies);
      syncUpgradeAllButton(); // left-panel "Upgrade All to 3.0" row follows the loaded folders

      // Re-resolve _currentScene so the open scene dialog keeps working
      // after the scenes array is regenerated with new objects.
      if (_currentScene) {
        const openId = String(_currentScene.id);
        const refreshed = scenes.find(s => String(s.id) === openId);
        if (refreshed) _currentScene = refreshed;
      }

      // Apply the filter bar's scene filters (review state, favorites only)
      // without mutating global scenes.
      // Reviewed means any of:
      //  - scene tags finalized by user, or scene renamed by user
      //  - manual/verified accept-reject culling decision on any image
      //  - manual star rating on any image
      // 'unreviewed' is the exact complement: scenes carrying no user-assigned
      // trait at all, i.e. the ones that still need work.
      const visibleScenes = scenes.filter(sceneMatchesViewFilters);

      const _showSciOnCards = _getShowSciNames();
      if (_showSciOnCards) SceneGridParts.hydrateSpeciesRecords(visibleScenes);

      updateStatusBar(visibleScenes);

      // Prevent flash-of-empty-content: lock the grid's current height as a
      // minimum so the page layout doesn't collapse while we rebuild the DOM,
      // and save the scroll position of the main container for restoration.
      const mainEl = document.querySelector('main');
      const savedScrollTop = mainEl ? mainEl.scrollTop : 0;
      const currentHeight = sceneGrid.offsetHeight;
      if (currentHeight > 0) {
        sceneGrid.style.minHeight = currentHeight + 'px';
      }
      sceneGrid.innerHTML = '';

      // Show welcome panel when no data is loaded; hide it once a folder is open.
      // The timeline filter bar is the inverse: shown only when scenes exist.
      const _welcomePanel = document.getElementById('welcomePanel');
      if (_welcomePanel) _welcomePanel.classList.toggle('hidden', rows.length > 0);
      const _tfb = document.getElementById('timelineFilterBar');
      if (_tfb) _tfb.classList.toggle('hidden', rows.length === 0);

      // Flat index for shift-click range selection
      _visibleSceneOrder = visibleScenes.map(s => String(s.id));

      // Build folderMap: folderKey → { folderPath, scenes: [ordered scene list] }
      const folderOrder = [];
      const folderMap = new Map();
      for (const s of visibleScenes) {
        const rp = groupByFolder ? (s.representative?.__rootPath || '') : '';
        const fk = rp || '__single__';
        if (!folderMap.has(fk)) { folderMap.set(fk, { folderPath: rp, scenes: [] }); folderOrder.push(fk); }
        folderMap.get(fk).scenes.push(s);
      }

      const ctx = {
        smartActive: _smartActive,
        searchOn: _searchOn,
        showBirdThumbs,
        showFolderHeaders: groupByFolder,
        showSciOnCards: _showSciOnCards,
        isStale: () => myVer !== _renderScenesVersion,
      };
      for (const fk of folderOrder) {
        const fd = folderMap.get(fk);
        let bodyEl; // receives the timeline or flat grid
        if (ctx.showFolderHeaders && fd.folderPath) {
          const group = SceneGridParts.buildFolderGroup(fd, fk);
          bodyEl = group.bodyEl;
          sceneGrid.appendChild(group.groupEl);
        } else {
          bodyEl = document.createElement('div');
          sceneGrid.appendChild(bodyEl);
        }

        let finished;
        if (groupByTime) {
          finished = SceneGridParts.buildTimeline(fd, bodyEl, ctx);
        } else {
          const gridEl = document.createElement('div');
          gridEl.className = 'folder-group-grid grid';
          bodyEl.appendChild(gridEl);
          finished = SceneGridParts.appendCards(gridEl, fd.scenes, ctx);
        }
        if (!finished) { sceneGrid.style.minHeight = ''; return; }
      }

      // Restore scroll position and release the minimum-height lock now that
      // the grid is rebuilt, preventing flash-of-empty-content.
      sceneGrid.style.minHeight = '';
      if (mainEl && savedScrollTop > 0) {
        mainEl.scrollTop = savedScrollTop;
      }

      // Folder headers were just rebuilt with their Repair buttons hidden;
      // re-apply whatever the last diff found. Cheap DOM toggle, no rescan.
      try { if (typeof syncRepairIndicators === 'function') syncRepairIndicators(); } catch (_) { }
    }

    // Update card highlights and show/hide floating action bar based on current selection
    function updateSelectionUI() {
      const n = selectedSceneIds.size;
      document.querySelectorAll('.card[data-scene-id]').forEach(c => {
        c.classList.toggle('selected', selectedSceneIds.has(c.dataset.sceneId));
      });
      const bar = document.getElementById('selectActionBar');
      if (!bar) return;
      if (n >= 2) {
        bar.classList.remove('hidden');
        const lbl = document.getElementById('selectActionLabel');
        if (lbl) lbl.textContent = `${n} scene${n === 1 ? '' : 's'} selected`;
      } else {
        bar.classList.add('hidden');
      }
    }

    // Scroll to a scene card in the grid and give it keyboard focus
    function _focusGridCard(sceneId) {
      _focusedCardId = String(sceneId);
      document.querySelectorAll('.card.focused').forEach(c => c.classList.remove('focused'));
      const card = sceneGrid.querySelector(`.card[data-scene-id="${CSS.escape(_focusedCardId)}"]`);
      if (card) {
        card.classList.add('focused');
        card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }

    // Right-click menu on a scene card (search results). One menu at a time; closes on
    // any outside mousedown, Escape, scroll or window blur. Reuses the Folder Actions styling.
    let _cardContextMenu = null;
    function _closeCardContextMenu() {
      if (_cardContextMenu && _cardContextMenu.parentNode) _cardContextMenu.parentNode.removeChild(_cardContextMenu);
      _cardContextMenu = null;
    }
    document.addEventListener('mousedown', (ev) => {
      if (_cardContextMenu && !_cardContextMenu.contains(ev.target)) _closeCardContextMenu();
    });
    document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') _closeCardContextMenu(); });
    document.addEventListener('scroll', () => _closeCardContextMenu(), true);
    window.addEventListener('blur', () => _closeCardContextMenu());

    function _openCardContextMenu(x, y, items) {
      _closeCardContextMenu();
      const menu = document.createElement('div');
      menu.className = 'card-context-menu';
      for (const it of items) {
        const row = document.createElement('div');
        row.className = 'folder-group-actions-item';
        row.appendChild(KestrelIcon.el(it.icon));
        row.appendChild(document.createTextNode(' ' + it.label));
        row.addEventListener('click', (ev) => {
          ev.stopPropagation();
          _closeCardContextMenu();
          try { it.run(); } catch (e) { console.error('[card-menu]', it.label, e); }
        });
        menu.appendChild(row);
      }
      document.body.appendChild(menu);
      // Keep the menu inside the window.
      const r = menu.getBoundingClientRect();
      menu.style.left = Math.max(4, Math.min(x, window.innerWidth - r.width - 4)) + 'px';
      menu.style.top = Math.max(4, Math.min(y, window.innerHeight - r.height - 4)) + 'px';
      _cardContextMenu = menu;
    }

    /** Leave the search / filtered view and show a scene at its place in the full timeline:
     *  clears the search box, re-renders, expands its folder group if collapsed, then
     *  scrolls to the card and gives it the keyboard-focus highlight. */
    async function goToSceneInTimeline(sceneId) {
      const sid = String(sceneId);
      const rp = scenes.find(s => String(s.id) === sid)?.representative?.__rootPath || '';
      if (rp) collapsedFolders.delete(rp);
      const input = el('#search');
      if (input) input.value = '';
      if (typeof smartSearchOnInput === 'function') await smartSearchOnInput(); else await renderScenes();
      const card = sceneGrid.querySelector(`.card[data-scene-id="${CSS.escape(sid)}"]`);
      if (!card) {
        showToast('That scene is hidden by the review-state filter.', 4000);
        return;
      }
      _lastSelectedIdx = _visibleSceneOrder.indexOf(sid);
      _focusGridCard(sid);
    }

    // Clear the focused-card highlight
    function _clearGridFocus() {
      _focusedCardId = null;
      document.querySelectorAll('.card.focused').forEach(c => c.classList.remove('focused'));
    }

    // Get all visible card elements in DOM order
    function _getVisibleCards() {
      return Array.from(sceneGrid.querySelectorAll('.card[data-scene-id]'));
    }

    // Grid keyboard navigation: arrow keys move focus, Enter opens scene dialog
    function _gridKeyHandler(e) {
      if (document.querySelector('dialog[open]')) return;
      if (selectedSceneIds.size > 0) return;
      const tag = (e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;

      const isArrow = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key);
      const isEnter = e.key === 'Enter';
      if (!isArrow && !isEnter) return;
      if (!_focusedCardId) return;

      e.preventDefault();

      if (isEnter) {
        // Mirror the mouse path: opening a scene by click also moves the
        // shift-click anchor (see the card click handler), so opening one by
        // keyboard must too, or the anchor is left behind at whichever card
        // was last clicked.
        _lastSelectedIdx = _visibleSceneOrder.indexOf(String(_focusedCardId));
        openSceneDialog(_focusedCardId);
        return;
      }

      const cards = _getVisibleCards();
      if (cards.length === 0) return;
      const curIdx = cards.findIndex(c => c.dataset.sceneId === _focusedCardId);
      if (curIdx < 0) return;
      const curCard = cards[curIdx];

      let nextIdx = -1;
      if (e.key === 'ArrowLeft') {
        nextIdx = curIdx - 1;
      } else if (e.key === 'ArrowRight') {
        nextIdx = curIdx + 1;
      } else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        const curRect = curCard.getBoundingClientRect();
        const curCenterX = curRect.left + curRect.width / 2;
        const dir = e.key === 'ArrowDown' ? 1 : -1;
        let bestIdx = -1, bestDist = Infinity;
        for (let i = 0; i < cards.length; i++) {
          if (i === curIdx) continue;
          const r = cards[i].getBoundingClientRect();
          const rowDiff = dir > 0 ? r.top - curRect.top : curRect.top - r.top;
          if (rowDiff < 10) continue;
          const dist = Math.abs(r.left + r.width / 2 - curCenterX) + rowDiff * 2;
          if (dist < bestDist) { bestDist = dist; bestIdx = i; }
        }
        nextIdx = bestIdx;
      }

      if (nextIdx >= 0 && nextIdx < cards.length) {
        const nextId = cards[nextIdx].dataset.sceneId;
        _focusGridCard(nextId);
        // Keep the shift-click anchor with the focused card. Without this,
        // arrowing across the grid leaves _lastSelectedIdx wherever the last
        // mouse click happened, and the next Shift+Click selects everything
        // between that stale card and the clicked one rather than starting a
        // fresh range from where the user actually is.
        //
        // Safe against clobbering a range selection: this handler returns
        // early while selectedSceneIds is non-empty, so arrow keys never move
        // the anchor mid-selection.
        _lastSelectedIdx = _visibleSceneOrder.indexOf(String(nextId));
      }
    }
    document.addEventListener('keydown', _gridKeyHandler);

    // Merge all currently selected scenes (must all be in same folder)
    /** Selection bar: mark every selected scene as reviewed with the tags it shows. */
    function executeSelectionReview() {
      let n = 0;
      for (const id of selectedSceneIds) {
        // An already-reviewed scene keeps its tags: _finalizeSceneReview rebuilds them
        // from the model's visible tags, which would undo the user's corrections.
        if (_getSceneScenedataEntry(id, false)?.user_tags?.finalized) continue;
        if (_finalizeSceneReview(id)) n += 1;
      }
      if (!n) return;
      setStatus(`Confirmed species on ${n} scene${n === 1 ? '' : 's'}.`);
      renderScenes();
    }

    /** Selection bar: reject the undecided photos of every selected scene. */
    function executeSelectionRejectUndecided() {
      let photos = 0;
      for (const id of selectedSceneIds) photos += rejectUndecidedRows(getSceneRows(id));
      setStatus(photos
        ? `Rejected ${photos} undecided photo${photos === 1 ? '' : 's'} in ${selectedSceneIds.size} scenes.`
        : 'Every photo in the selected scenes already has a decision.');
      if (photos) renderScenes();
    }

    async function executeSelectionMerge() {
      const ids = Array.from(selectedSceneIds);
      if (ids.length < 2) return;
      const parsed = ids.map(id => {
        const parts = String(id).split(':');
        const count = parts.pop();
        const slot = parts.length ? parseInt(parts[0], 10) : 0;
        return { id, slot, count };
      });
      const slots = new Set(parsed.map(p => p.slot));
      if (slots.size > 1) {
        await KestrelDialog.notice('mergeDifferentFolders');
        return;
      }
      const target = parsed.slice().sort((a, b) => parseNumber(a.count) - parseNumber(b.count))[0];
      const slot = target.slot;
      const targetCount = target.count;
      const mergedSceneId = String(slot != null ? slot + ':' + targetCount : targetCount);
      let changed = 0;
      for (const r of rows) {
        if ((r.__folderSlot ?? 0) !== slot) continue;
        if (parsed.some(p => p.count === String(r.scene_count)) && String(r.scene_count) !== targetCount) {
          r.scene_count = targetCount; changed++;
        }
      }
      const rpForMerge = rows.find(r => (r.__folderSlot ?? 0) === slot)?.__rootPath || rootPath || '';
      // Update scenedata: move filenames from non-target scenes into target scene
      if (hasPywebviewApi) {
        if (rpForMerge) {
          const sd = _initScenedata(rpForMerge);
          const allMovedFiles = new Set();
          for (const p of parsed) {
            if (p.count !== targetCount && sd.scenes[p.count]) {
              for (const f of sd.scenes[p.count].image_filenames || []) allMovedFiles.add(f);
              delete sd.scenes[p.count];
            }
          }
          if (!sd.scenes[targetCount]) {
            sd.scenes[targetCount] = { scene_id: targetCount, image_filenames: [], name: '', status: 'pending', user_tags: { species: [], families: [], finalized: false } };
          }
          for (const f of allMovedFiles) {
            if (!sd.scenes[targetCount].image_filenames.includes(f)) sd.scenes[targetCount].image_filenames.push(f);
          }
        }
      }
      if (changed) {
        markDirty(rpForMerge);
        setStatus(`Merged ${ids.length} scenes into #${targetCount}. ${changed} rows updated.`);
      }
      selectedSceneIds.clear();
      _lastSelectedIdx = -1;
      updateSelectionUI();
      await renderScenes();
      // Scroll to the merged scene card; fall back to current scroll position
      const mergedCard = document.querySelector(`.card[data-scene-id="${CSS.escape(mergedSceneId)}"]`);
      if (mergedCard) mergedCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    // Render images inside the scene dialog, honoring the manual-rated filter and stable ordering
