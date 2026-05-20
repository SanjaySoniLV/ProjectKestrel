    // ── Analyze Folders Dialog ───────────────────────────────────────────────────

    let _dlgSelected = new Set();
    let _dlgExpandedPaths = new Set();
    let _dlgReanalyze = new Set(); // paths confirmed for re-analysis (fully analyzed folders)

    /** Build a tree node for the Analyze dialog (amber checkboxes, no load-cb). */
    function buildAnalyzeDlgNode(node, selectedSet, onChangeCallback) {
      const wrap = document.createElement('div');
      wrap.className = 'tree-node';

      const row = document.createElement('div');
      const hasChildren = node.children && node.children.length > 0;
      const isExpanded = _dlgExpandedPaths.has(node.path);
      const outdated = isVersionOutdated(node);
      row.className = 'adlg-node-row' + (selectedSet.has(node.path) ? ' queue-sel' : '') + (node.has_kestrel ? ' has-kestrel' : '') + (outdated ? ' version-outdated' : '');
      if (outdated) {
        row.title = `Analyzed on Kestrel v${node.kestrel_version} (current: v${_appVersion}). Consider re-analyzing.`;
      }

      const arrow = document.createElement('span');
      arrow.className = 'tree-arrow' + (hasChildren ? (isExpanded ? ' open' : '') : ' leaf');
      arrow.textContent = hasChildren ? '▶' : '';

      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'adlg-cb';
      cb.checked = selectedSet.has(node.path);
      cb.addEventListener('change', (e) => {
        e.stopPropagation();
        if (cb.checked) {
          // Prompt before re-queuing a fully analyzed folder
          if (row.classList.contains('analyzed-full')) {
            const confirmed = confirm(
              `"${node.name}" has already been fully analyzed.\n\n` +
              `Re-analyzing will delete the existing analysis data (.kestrel folder) and process it again.\n\n` +
              `Continue?`
            );
            if (!confirmed) { cb.checked = false; return; }
            _dlgReanalyze.add(node.path);
          }
          selectedSet.add(node.path);
        } else {
          selectedSet.delete(node.path);
          _dlgReanalyze.delete(node.path);
        }
        row.classList.toggle('queue-sel', cb.checked);
        onChangeCallback();
      });

      const icon = document.createElement('span');
      icon.className = 'tree-icon';
      icon.textContent = node.has_kestrel ? '📂' : '📁';

      const label = document.createElement('span');
      label.className = 'tree-label';
      label.textContent = node.name;
      if (!outdated) label.title = node.path;
      else label.title = `v${node.kestrel_version} → v${_appVersion} (outdated)`;

      // Version badge for outdated folders
      const versionBadge = document.createElement('span');
      if (outdated) {
        versionBadge.style.cssText = 'font-size:10px;color:var(--ok);opacity:0.7;margin-left:4px;font-style:italic;';
        versionBadge.textContent = `v${node.kestrel_version}`;
      }

      // Attach path for async inspection and add count placeholder
      row.dataset.path = node.path;
      const countSpan = document.createElement('span');
      countSpan.className = 'tree-count';
      countSpan.textContent = '';

      row.appendChild(arrow);
      row.appendChild(cb);
      row.appendChild(icon);
      row.appendChild(label);
      if (outdated) row.appendChild(versionBadge);
      row.appendChild(countSpan);

      // Right-click context menu for clearing analysis data
      if (node.has_kestrel) {
        row.addEventListener('contextmenu', (e) => {
          e.preventDefault();
          e.stopPropagation();
          const folderName = node.name;
          showContextMenu(e.clientX, e.clientY, [
            {
              label: '🗑 Clear Kestrel Analysis Data',
              danger: true,
              action: () => {
                clearKestrelDataForFolder(node.path, folderName, () => {
                  // Update the node state in-memory
                  node.has_kestrel = false;
                  node.kestrel_version = '';
                  // Re-render the dialog tree from all loaded roots
                  const treeEl = document.getElementById('analyzeDlgTree');
                  if (treeEl && _hasAnyRoots()) {
                    treeEl.innerHTML = '';
                    for (const root of _getAllRoots()) {
                      treeEl.appendChild(buildAnalyzeDlgNode(root, _dlgSelected, onChangeCallback));
                    }
                    populateAnalyzeFolderCounts();
                  }
                });
              }
            }
          ]);
        });
      }

      wrap.appendChild(row);

      if (hasChildren) {
        const childWrap = document.createElement('div');
        childWrap.className = 'tree-children';
        if (!isExpanded) childWrap.classList.add('hidden');
        node.children.forEach(child => childWrap.appendChild(buildAnalyzeDlgNode(child, selectedSet, onChangeCallback)));
        wrap.appendChild(childWrap);

        arrow.addEventListener('click', (e) => {
          e.stopPropagation();
          const open = _dlgExpandedPaths.has(node.path);
          if (open) { _dlgExpandedPaths.delete(node.path); arrow.classList.remove('open'); childWrap.classList.add('hidden'); }
          else { _dlgExpandedPaths.add(node.path); arrow.classList.add('open'); childWrap.classList.remove('hidden'); }
        });
      }
      return wrap;
    }

    /** Render the right-side queue preview panel in the Analyze dialog.
     *  Shows: running items, pending items (draggable + removable), and "will be added" selection. */
    function _refreshAnalyzeDlgQueuePreview() {
      const runningEl = document.getElementById('adlgQueueRunning');
      const willAddEl = document.getElementById('adlgQueueWillAdd');
      const emptyEl = document.getElementById('adlgQueueEmpty');
      if (!runningEl || !willAddEl || !emptyEl) return;

      runningEl.innerHTML = '';
      willAddEl.innerHTML = '';

      let hasActiveQueue = false;

      try {
        const status = window._lastQueueStatus;
        if (status && status.items && status.items.length > 0) {
          const runningItems = status.items.filter(i => i.status === 'running');
          const pendingItems = status.items.filter(i => i.status === 'pending');

          // ── Running items ──
          if (runningItems.length > 0) {
            hasActiveQueue = true;
            const title = document.createElement('div');
            title.className = 'adlg-queue-section-title';
            title.textContent = '⚙ Analyzing';
            runningEl.appendChild(title);
            for (const item of runningItems) {
              const row = document.createElement('div');
              row.className = 'adlg-queue-item';
              const nameEl = document.createElement('span');
              nameEl.className = 'adlg-qi-name';
              nameEl.textContent = item.name;
              nameEl.title = item.path;
              const statusEl = document.createElement('span');
              statusEl.className = 'adlg-qi-status';
              statusEl.textContent = item.total > 0 ? `${item.processed}/${item.total}` : 'starting…';
              row.appendChild(nameEl);
              row.appendChild(statusEl);
              runningEl.appendChild(row);
            }
          }

          // ── Pending items (drag-to-reorder + cancel) ──
          if (pendingItems.length > 0) {
            hasActiveQueue = true;
            const pendTitle = document.createElement('div');
            pendTitle.className = 'adlg-queue-section-title';
            pendTitle.textContent = `⏳ In Queue (${pendingItems.length})`;
            runningEl.appendChild(pendTitle);

            let _dragSrcPath = null;
            const pendContainer = document.createElement('div');
            pendContainer.dataset.role = 'pending-list';

            for (const item of pendingItems) {
              const row = document.createElement('div');
              row.className = 'adlg-queue-item';
              row.draggable = true;
              row.dataset.queuePath = item.path;

              const grip = document.createElement('span');
              grip.className = 'adlg-qi-grip';
              grip.textContent = '⠿';
              grip.title = 'Drag to reorder';

              const nameEl = document.createElement('span');
              nameEl.className = 'adlg-qi-name';
              nameEl.textContent = item.name;
              nameEl.title = item.path;

              const statusEl = document.createElement('span');
              statusEl.className = 'adlg-qi-status';
              statusEl.textContent = 'pending';

              const removeBtn = document.createElement('button');
              removeBtn.className = 'adlg-qi-remove';
              removeBtn.textContent = '✕';
              removeBtn.title = 'Remove from queue';
              removeBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                if (hasPywebviewApi && window.pywebview?.api?.remove_queue_item) {
                  await window.pywebview.api.remove_queue_item(item.path);
                  const s = await apiGetQueueStatus();
                  renderQueuePanel(s);
                  _refreshAnalyzeDlgQueuePreview();
                }
              });

              // Drag events
              row.addEventListener('dragstart', (e) => {
                _dragSrcPath = item.path;
                row.classList.add('dragging');
                e.dataTransfer.effectAllowed = 'move';
                e.dataTransfer.setData('text/plain', item.path);
              });
              row.addEventListener('dragend', () => {
                _dragSrcPath = null;
                row.classList.remove('dragging');
                pendContainer.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
              });
              row.addEventListener('dragover', (e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                if (row.dataset.queuePath !== _dragSrcPath) {
                  row.classList.add('drag-over');
                }
              });
              row.addEventListener('dragleave', () => {
                row.classList.remove('drag-over');
              });
              row.addEventListener('drop', async (e) => {
                e.preventDefault();
                row.classList.remove('drag-over');
                const srcPath = e.dataTransfer.getData('text/plain');
                if (!srcPath || srcPath === item.path) return;
                const currentOrder = Array.from(pendContainer.querySelectorAll('[data-queue-path]'))
                  .map(el => el.dataset.queuePath);
                const filtered = currentOrder.filter(p => p !== srcPath);
                const targetIdx = filtered.indexOf(item.path);
                filtered.splice(targetIdx, 0, srcPath);
                if (hasPywebviewApi && window.pywebview?.api?.reorder_queue) {
                  await window.pywebview.api.reorder_queue(JSON.stringify(filtered));
                  const s = await apiGetQueueStatus();
                  renderQueuePanel(s);
                  _refreshAnalyzeDlgQueuePreview();
                }
              });

              row.appendChild(grip);
              row.appendChild(nameEl);
              row.appendChild(statusEl);
              row.appendChild(removeBtn);
              pendContainer.appendChild(row);
            }
            runningEl.appendChild(pendContainer);
          }
        }
      } catch (_) { }

      // ── Will be added (selected but not yet queued) ──
      const selected = Array.from(_dlgSelected);
      if (selected.length > 0) {
        const title = document.createElement('div');
        title.className = 'adlg-queue-section-title';
        title.textContent = `➕ Will Be Added (${selected.length})`;
        willAddEl.appendChild(title);
        for (const path of selected) {
          const name = path.replace(/\\/g, '/').split('/').pop() || path;
          const row = document.createElement('div');
          row.className = 'adlg-queue-item';
          const nameEl = document.createElement('span');
          nameEl.className = 'adlg-qi-name';
          nameEl.textContent = name;
          nameEl.title = path;
          const removeBtn = document.createElement('button');
          removeBtn.className = 'adlg-qi-remove';
          removeBtn.textContent = '✕';
          removeBtn.title = 'Remove from selection';
          removeBtn.addEventListener('click', () => {
            _dlgSelected.delete(path);
            _dlgReanalyze.delete(path);
            const treeRows = document.querySelectorAll('#analyzeDlgTree .adlg-node-row');
            for (const r of treeRows) {
              if (r.dataset.path === path) {
                const cb = r.querySelector('.adlg-cb');
                if (cb) cb.checked = false;
                r.classList.remove('queue-sel');
              }
            }
            const countEl = document.getElementById('analyzeDlgCount');
            const addBtn = document.getElementById('analyzeDlgAdd');
            if (countEl) countEl.textContent = _dlgSelected.size + ' folder' + (_dlgSelected.size === 1 ? '' : 's') + ' selected';
            if (addBtn) addBtn.disabled = _dlgSelected.size === 0;
            _refreshAnalyzeDlgQueuePreview();
          });
          row.appendChild(nameEl);
          if (_dlgReanalyze.has(path)) {
            const badge = document.createElement('span');
            badge.className = 'adlg-qi-status';
            badge.style.color = '#f0a040';
            badge.style.fontStyle = 'italic';
            badge.textContent = 'Will be Re-analyzed';
            row.appendChild(badge);
          }
          row.appendChild(removeBtn);
          willAddEl.appendChild(row);
        }
      }

      emptyEl.classList.toggle('hidden', hasActiveQueue || selected.length > 0);
    }

    /** Open the 'Analyze Folders…' dialog. */
    async function openAnalyzeDialog() {
      if (!hasPywebviewApi) {
        alert('Analysis queue is only available in the desktop (pywebview) mode.\n\nRun kestrel_visualizer as a desktop app to use this feature.');
        return;
      }
      // Make sure we have at least one root to browse. If empty, prompt the
      // user to pick one (or several, if multi-select is available).
      if (!_hasAnyRoots()) {
        let pickedPaths = [];
        if (window.pywebview?.api?.choose_directories) {
          const res = await window.pywebview.api.choose_directories();
          if (Array.isArray(res)) pickedPaths = res.filter(Boolean);
          else if (typeof res === 'string' && res) pickedPaths = [res];
        } else {
          const single = await window.pywebview.api.choose_directory();
          if (single) pickedPaths = [single];
        }
        if (pickedPaths.length === 0) return;
        for (const p of pickedPaths) await addFolderRoot(p);
        if (!_hasAnyRoots()) return;
      }
      // GPU is always available: DirectML (Windows) and CoreML (macOS) are bundled
      // with the frozen build, so no platform-specific hiding is needed.
      // Seed the dialog's selected set from any previously-queued paths
      _dlgSelected = new Set(queuedFolderPaths);
      
      // Try to restore last queue state if available
      const savedQueue = getSetting('lastQueueState', null);
      if (savedQueue && Array.isArray(savedQueue) && savedQueue.length > 0) {
        const restoreBtn = document.getElementById('analyzeDlgRestoreQueue');
        if (restoreBtn) {
          restoreBtn.style.display = '';
          restoreBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            _dlgSelected = new Set(savedQueue);
            // Restore queue in settings so it looks restored
            const s = loadSettings();
            delete s.lastQueueState;
            saveSettings(s);
            restoreBtn.style.display = 'none';
            refreshDlg();
          }, { once: true }); // only trigger once per dialog open
        }
      }
      
      _dlgExpandedPaths = new Set(_getAllRoots().map(r => r.path));
      _dlgReanalyze = new Set();

      function refreshDlg() {
        const countEl = document.getElementById('analyzeDlgCount');
        const addBtn = document.getElementById('analyzeDlgAdd');
        if (countEl) countEl.textContent = _dlgSelected.size + ' folder' + (_dlgSelected.size === 1 ? '' : 's') + ' selected';
        if (addBtn) addBtn.disabled = _dlgSelected.size === 0;
        _refreshAnalyzeDlgQueuePreview();
      }

      // Hydrate advanced analysis settings from persisted values
      const _adlgDt = document.getElementById('adlgDetectionThreshold');
      if (_adlgDt) _adlgDt.value = getSetting('detection_threshold', 0.25);
      const _adlgMbc = document.getElementById('adlgMaxBirdCrops');
      if (_adlgMbc) _adlgMbc.value = getSetting('max_bird_crops', 10);
      const _adlgEq = document.getElementById('adlgExposureQuality');
      if (_adlgEq) {
        const savedEq = String(getSetting('exposure_quality', 'balanced') || 'balanced').toLowerCase();
        _adlgEq.value = ['lenient', 'balanced', 'aggressive'].includes(savedEq) ? savedEq : 'balanced';
      }
      const _adlgModelMode = document.getElementById('adlgWildlifeModelMode');
      if (_adlgModelMode) {
        const savedMode = String(getSetting('wildlife_model_mode', 'accurate') || 'accurate').toLowerCase();
        _adlgModelMode.value = (savedMode === 'fast') ? 'fast' : 'accurate';
      }
      const _adlgSt = document.getElementById('adlgSceneTime');
      if (_adlgSt) _adlgSt.value = getSetting('scene_time_threshold', 1.0);
      const _adlgPp = document.getElementById('adlgParallelPrefetch');
      if (_adlgPp) _adlgPp.value = getSetting('parallel_prefetch', 3);
      const _adlgThumbW = document.getElementById('adlgThumbnailMaxWidth');
      if (_adlgThumbW) {
        const savedW = parseInt(getSetting('thumbnail_max_width', 1200), 10);
        _adlgThumbW.value = Math.max(400, Math.min(2400, Number.isFinite(savedW) ? savedW : 1200));
      }
      const _adlgThumbComp = document.getElementById('adlgThumbnailJpegCompression');
      if (_adlgThumbComp) {
        let compression = parseFloat(getSetting('thumbnail_jpeg_compression', Number.NaN));
        if (!Number.isFinite(compression)) {
          const legacyQuality = parseInt(getSetting('thumbnail_jpeg_quality', 75), 10);
          compression = (Number.isFinite(legacyQuality) ? legacyQuality : 75) / 100;
        }
        compression = Math.max(0.5, Math.min(1.0, compression));
        _adlgThumbComp.value = compression.toFixed(2);
      }

      const treeEl = document.getElementById('analyzeDlgTree');
      treeEl.innerHTML = '';
      for (const root of _getAllRoots()) {
        treeEl.appendChild(buildAnalyzeDlgNode(root, _dlgSelected, refreshDlg));
      }
      // Populate counts for dialog nodes with colors and progress bar
      populateAnalyzeFolderCounts();
      refreshDlg();
      document.getElementById('analyzeQueueDlg').showModal();
    }

