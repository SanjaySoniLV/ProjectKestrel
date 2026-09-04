    // ── Folder Tree ──────────────────────────────────────────────────────────────
    // Multi-root state (Phase 2): the sidebar tree can host N independent root
    // folders side by side. The user adds roots via "+ 📂 Load Folders…" and
    // each root scans its own subtree. Scene/data aggregation downstream is
    // already multi-root via __rootPath / __folderSlot row tagging.
    let folderTreeRootNodes = new Map();   // Map<rootPath, syntheticRootNode>
    let folderTreeRootOrder = [];          // insertion order — render order
    let treeExpandedPaths = new Set();
    let treeActivePath = null;             // visual highlight only (last clicked)
    let checkedFolderPaths = new Set();    // folders checked for multi-load
    let _checkedFolderPathSnapshot = new Map(); // normalized path -> original path
    let queuedFolderPaths = new Set();     // folders queued for analysis (dialog selection)
    let _treeFlatOrder = [];               // flat ordered list of visible tree paths for range-select
    let _appVersion = '';                  // current app version, fetched once
    let _isFrozenApp = false;              // whether running as frozen (PyInstaller) build
    let _appPlatform = 'windows';          // 'windows' | 'macos' | 'linux'

    // ── Multi-root accessors ─────────────────────────────────────────────────────
    function _hasAnyRoots() {
      return folderTreeRootOrder.length > 0;
    }
    function _getAllRoots() {
      return folderTreeRootOrder.map(p => folderTreeRootNodes.get(p)).filter(Boolean);
    }
    function _getFirstRoot() {
      return _hasAnyRoots() ? folderTreeRootNodes.get(folderTreeRootOrder[0]) : null;
    }
    // Find a node by path across ALL loaded roots. Returns the matching node or null.
    function findNodeInAnyRoot(targetPath) {
      for (const root of _getAllRoots()) {
        const found = _findNodeInSubtree(root, targetPath);
        if (found) return found;
      }
      return null;
    }
    function _findNodeInSubtree(node, targetPath) {
      if (!node) return null;
      if (node.path === targetPath) return node;
      if (node.children) {
        for (const c of node.children) {
          const f = _findNodeInSubtree(c, targetPath);
          if (f) return f;
        }
      }
      return null;
    }
    // Return the root node that contains (or equals) `path`, or null.
    function _findRootContaining(targetPath) {
      const norm = p => (p || '').replace(/\\/g, '/').replace(/\/+$/, '');
      const t = norm(targetPath);
      for (const root of _getAllRoots()) {
        const r = norm(root.path);
        if (t === r || t.startsWith(r + '/')) return root;
      }
      return null;
    }

    // Normalize a path for dedup/lookup (forward slashes, no trailing slash).
    function _normRoot(p) {
      return (p || '').replace(/\\/g, '/').replace(/\/+$/, '');
    }

    // Walk a node's subtree: auto-expand any folder that contains analyzed
    // descendants (so the user sees the analyzed work without clicking) and
    // auto-check any folder that itself has .kestrel data.
    //
    // The expand condition uses `descendantHas` (NOT `subtreeHas`) because
    // expanding an analyzed leaf to show its unanalyzed subdirs (e.g. a
    // `_KESTREL_Rejects` or `darktable_exported` folder) is pure noise. We
    // only expand when there's something useful below to reveal.
    function _autoExpandAndCheckAnalyzedDescendants(node) {
      if (!node) return;
      function walk(n) {
        if (!n) return false;
        const selfHasKestrel = !!n.has_kestrel;
        let descendantHas = false;
        if (n.children) {
          for (const c of n.children) {
            if (walk(c)) descendantHas = true;
          }
        }
        if (descendantHas && n.children && n.children.length > 0) {
          treeExpandedPaths.add(n.path);
        }
        if (selfHasKestrel) {
          checkedFolderPaths.add(n.path);
        }
        return selfHasKestrel || descendantHas;
      }
      walk(node);
    }

    // Add a folder root to the tree. Additive: existing roots stay. If the
    // root is already loaded, briefly flash its row (caller can show feedback)
    // and return {added: false, alreadyLoaded: true}. On successful scan,
    // auto-expands + auto-checks analyzed descendants, re-renders, and returns
    // {added: true, rootHasKestrel: bool, node: syntheticNode}.
    async function addFolderRoot(rootPath) {
      if (!hasPywebviewApi || !window.pywebview?.api?.list_subfolders) return { added: false, error: 'no-bridge' };
      if (!rootPath) return { added: false, error: 'no-path' };

      const norm = _normRoot(rootPath);
      // Dedup #1: exact match against existing roots.
      if (folderTreeRootNodes.has(norm)) {
        return { added: false, alreadyLoaded: true, reason: 'duplicate', node: folderTreeRootNodes.get(norm) };
      }
      // Dedup #2: the picked path is a SUBDIRECTORY of an already-loaded root.
      // Reject — the user can already see/check it under the existing root.
      for (const existing of folderTreeRootOrder) {
        if (norm.startsWith(existing + '/')) {
          setStatus(`Already in tree under ${existing.split('/').pop()}`);
          return { added: false, alreadyLoaded: true, reason: 'subdir-of', containingRoot: existing };
        }
      }
      // Dedup #3: the picked path is an ANCESTOR of one or more already-loaded
      // roots. Remove those child roots first — the new ancestor will contain
      // them, and everything gets re-loaded from the ancestor's scan anyway.
      const childRootsToReplace = folderTreeRootOrder.filter(existing => existing.startsWith(norm + '/'));
      for (const child of childRootsToReplace) {
        removeFolderRoot(child);
      }

      // Fetch app version / frozen status / platform once (legacy behavior)
      if (!_appVersion && window.pywebview?.api?.get_app_version) {
        try {
          const vr = await window.pywebview.api.get_app_version();
          if (vr && vr.success) _appVersion = vr.version || '';
        } catch (e) { /* ignore */ }
      }
      if (!_isFrozenApp && window.pywebview?.api?.is_frozen_app) {
        try {
          const fr = await window.pywebview.api.is_frozen_app();
          _isFrozenApp = !!(fr && fr.frozen);
        } catch (e) { /* ignore */ }
      }
      if (_appPlatform === 'windows') {
        try {
          _appPlatform = await getPlatformInfo();
        } catch (e) { /* ignore */ }
      }

      const depth = getSetting('treeScanDepth', 3);
      setStatus('Scanning folder tree…');
      try {
        const result = await window.pywebview.api.list_subfolders(norm, depth);
        if (!result.success) {
          console.warn('[tree] list_subfolders failed:', result.error);
          return { added: false, error: result.error || 'scan-failed' };
        }
        const rootHasKestrel = !!result.root_has_kestrel;
        const rootName = norm.split('/').filter(Boolean).pop() || norm;
        const node = {
          name: rootName,
          path: norm,
          has_kestrel: rootHasKestrel,
          kestrel_version: result.root_kestrel_version || '',
          children: result.tree || [],
        };
        folderTreeRootNodes.set(norm, node);
        folderTreeRootOrder.push(norm);
        // Auto-expand this root + walk for analyzed descendants
        treeExpandedPaths.add(norm);
        _autoExpandAndCheckAnalyzedDescendants(node);
        renderFolderTree();
        if (typeof updateSelectToggleVisibility === 'function') updateSelectToggleVisibility();
        if (typeof updateEmptyHintCopy === 'function') updateEmptyHintCopy();
        // Re-render recents so the chip for this just-loaded folder disappears
        // (loaded roots are filtered out of the chip row).
        if (typeof renderFolderRecentsChips === 'function') renderFolderRecentsChips();
        // Drop the empty-state class once at least one root exists
        const treeWrap = document.getElementById('folderTreeWrap');
        if (treeWrap) {
          treeWrap.classList.remove('folder-tree-empty');
          treeWrap.querySelectorAll('button[disabled]').forEach(b => b.removeAttribute('disabled'));
        }
        return { added: true, rootHasKestrel, node };
      } catch (e) {
        console.error('[tree] addFolderRoot error:', e);
        return { added: false, error: String(e) };
      }
    }

    // Remove a single root from the tree (also drops any checked/expanded paths
    // under that root). Returns true if removed, false if it wasn't loaded.
    function removeFolderRoot(rootPath) {
      const norm = _normRoot(rootPath);
      if (!folderTreeRootNodes.has(norm)) return false;
      folderTreeRootNodes.delete(norm);
      folderTreeRootOrder = folderTreeRootOrder.filter(p => p !== norm);
      // Drop checks/expansions whose paths fall under this root
      const prefix = norm + '/';
      for (const p of Array.from(checkedFolderPaths)) {
        if (p === norm || _normRoot(p).startsWith(prefix)) checkedFolderPaths.delete(p);
      }
      for (const p of Array.from(treeExpandedPaths)) {
        if (p === norm || _normRoot(p).startsWith(prefix)) treeExpandedPaths.delete(p);
      }
      if (treeActivePath && (treeActivePath === norm || _normRoot(treeActivePath).startsWith(prefix))) {
        treeActivePath = null;
      }
      renderFolderTree();
      // A removed root re-becomes a candidate for the recents chip row.
      if (typeof renderFolderRecentsChips === 'function') renderFolderRecentsChips();
      return true;
    }

    // Unload every root and reset tree state. Returns the tree to its empty
    // visual state (welcome panel + recents chips visible). Also bumps
    // _loadFoldersVersion so any in-flight loadMultipleFolders call hits its
    // version check at the next await and aborts — otherwise scenes from a
    // mid-flight read_csv would still appear AFTER the user clicked Clear.
    function clearAllFolderRoots() {
      try { ++_loadFoldersVersion; } catch (e) { /* ignore */ }
      folderTreeRootNodes.clear();
      folderTreeRootOrder = [];
      checkedFolderPaths.clear();
      _checkedFolderPathSnapshot.clear();
      treeExpandedPaths.clear();
      treeActivePath = null;
      renderFolderTree();
      if (typeof updateSelectToggleVisibility === 'function') updateSelectToggleVisibility();
      if (typeof updateEmptyHintCopy === 'function') updateEmptyHintCopy();
      // Recents come back into view once their folders are no longer loaded.
      if (typeof renderFolderRecentsChips === 'function') renderFolderRecentsChips();
      const treeWrap = document.getElementById('folderTreeWrap');
      if (treeWrap) treeWrap.classList.add('folder-tree-empty');
    }

    // Legacy alias. Existing callers that historically called scanFolderTree
    // get the new additive behavior — if a caller truly wants "replace tree"
    // it should call clearAllFolderRoots() first. The Phase 2 plan documents
    // each migrated caller; this alias keeps any stragglers compiling.
    async function scanFolderTree(rootPath) {
      const res = await addFolderRoot(rootPath);
      return !!res.added;
    }

    /** Compare two semver strings. Returns -1 if a < b, 0 if equal, 1 if a > b. */
    function compareVersions(a, b) {
      if (!a || !b) return 0;
      const pa = a.split('.').map(Number), pb = b.split('.').map(Number);
      for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
        const na = pa[i] || 0, nb = pb[i] || 0;
        if (na < nb) return -1;
        if (na > nb) return 1;
      }
      return 0;
    }

    /** Check if a node's kestrel_version is older than the current app version. */
    function isVersionOutdated(node) {
      if (!node || !node.has_kestrel || !node.kestrel_version || !_appVersion) return false;
      return compareVersions(node.kestrel_version, _appVersion) < 0;
    }

    /** Show a custom context menu at (x, y) with given items. */
    function showContextMenu(x, y, items) {
      dismissContextMenu();
      const menu = document.createElement('div');
      menu.className = 'kestrel-ctx-menu';
      menu.id = '_kestrelCtxMenu';
      for (const item of items) {
        const el = document.createElement('div');
        el.className = 'kestrel-ctx-menu-item' + (item.danger ? ' danger' : '');
        el.textContent = item.label;
        el.addEventListener('click', (e) => {
          e.stopPropagation();
          dismissContextMenu();
          item.action();
        });
        menu.appendChild(el);
      }
      menu.style.left = x + 'px';
      menu.style.top = y + 'px';
      document.body.appendChild(menu);
      // Adjust if off-screen
      const rect = menu.getBoundingClientRect();
      if (rect.right > window.innerWidth) menu.style.left = (window.innerWidth - rect.width - 8) + 'px';
      if (rect.bottom > window.innerHeight) menu.style.top = (window.innerHeight - rect.height - 8) + 'px';
      // Dismiss on any click outside
      setTimeout(() => document.addEventListener('click', dismissContextMenu, { once: true }), 0);
    }

    function dismissContextMenu() {
      const old = document.getElementById('_kestrelCtxMenu');
      if (old) old.remove();
    }

    /** Clear kestrel analysis data for a folder. Surfaces a styled WebView
     *  confirmation dialog (#clearKestrelConfirmDlg) so the destructive flow
     *  is visually consistent with the rest of the app and harder to misclick
     *  than the previous native confirm()/alert() prompts. */
    async function clearKestrelDataForFolder(folderPath, folderName, refreshCallback) {
      const dlg = document.getElementById('clearKestrelConfirmDlg');
      const cancelBtn = document.getElementById('clearKestrelCancel');
      const proceedBtn = document.getElementById('clearKestrelProceed');
      const nameEl = document.getElementById('clearKestrelFolderName');
      const pathEl = document.getElementById('clearKestrelFolderPath');
      if (!dlg || !cancelBtn || !proceedBtn) {
        // Fallback for environments where the modal markup is missing.
        console.error('[clearKestrelDataForFolder] missing #clearKestrelConfirmDlg markup');
        return;
      }

      if (nameEl) nameEl.textContent = folderName || folderPath || 'this folder';
      if (pathEl) pathEl.textContent = folderPath || '';

      // Bind one-shot handlers so we don't accumulate listeners across calls.
      const close = () => { try { dlg.close(); } catch (_) {} };
      const onCancel = () => { cleanup(); close(); };
      const onProceed = async () => {
        cleanup();
        close();
        try {
          const result = await window.pywebview.api.clear_kestrel_data(folderPath);
          if (result && result.success) {
            if (typeof showToast === 'function') {
              showToast('Kestrel analysis data cleared for ' + (folderName || folderPath));
            }
            if (refreshCallback) refreshCallback();
          } else if (typeof showToast === 'function') {
            showToast('Failed to clear analysis data: ' + (result?.error || 'Unknown error'));
          }
        } catch (e) {
          if (typeof showToast === 'function') {
            showToast('Failed to clear analysis data: ' + (e.message || e));
          }
        }
      };
      function cleanup() {
        cancelBtn.removeEventListener('click', onCancel);
        proceedBtn.removeEventListener('click', onProceed);
        dlg.removeEventListener('cancel', onCancel);
      }
      cancelBtn.addEventListener('click', onCancel);
      proceedBtn.addEventListener('click', onProceed);
      dlg.addEventListener('cancel', onCancel);

      try { dlg.showModal(); } catch (_) { dlg.show(); }
      // Default-focus the Cancel button so Enter doesn't auto-confirm.
      try { cancelBtn.focus(); } catch (_) {}
    }

    function renderFolderTree() {
      const container = document.getElementById('folderTree');
      if (!container) return;
      // Rebuild the flat visible order for shift-range selection
      _treeFlatOrder = [];
      container.innerHTML = '';
      if (!_hasAnyRoots()) return;
      // Each loaded root renders as its own top-level node. Visual spacing
      // between roots is handled by CSS (.tree-node + .tree-node margin).
      for (const root of _getAllRoots()) {
        container.appendChild(buildTreeNode(root, _treeFlatOrder));
      }
      // Rows were just rebuilt, so re-apply the drift badges from the last
      // diff. Cheap DOM pass, no rescan.
      try { if (typeof syncRepairIndicators === 'function') syncRepairIndicators(); } catch (_) { }
      // Note: counts are only populated for the Analyze dialog tree.
    }

    /** Update a single main-folder-tree row for `path` without re-rendering whole tree.
     *  Makes the node appear as having kestrel data (icon + checkbox) but does not
     *  change selection or checked state. This avoids disturbing the user's view.
     */
    function updateFolderTreeNode(path) {
      try {
        const norm = p => (p || '').replace(/\\/g, '/');
        const target = norm(path);
        // Find rows in the main folder tree matching this path
        const rows = Array.from(document.querySelectorAll('#folderTree .tree-node-row'));
        for (const row of rows) {
          const rp = norm(row.dataset.path || '');
          if (rp !== target) continue;
          // Update classes
          row.classList.remove('no-kestrel');
          row.classList.add('has-kestrel');
          // Persist a transient marker so future rescans don't immediately clear it
          try { _tempKestrelPaths.add(norm(path)); } catch (e) { }
          // Update icon
          const icon = row.querySelector('.tree-icon');
          if (icon) icon.textContent = '📂';
          // Ensure checkbox exists (do not auto-check it)
          if (!row.querySelector('.tree-cb')) {
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.className = 'tree-cb';
            cb.title = 'Include in multi-folder view';
            cb.checked = _isPathChecked(row.dataset.path);
            cb.addEventListener('change', (e) => {
              e.stopPropagation();
              if (cb.checked) checkedFolderPaths.add(row.dataset.path);
              else checkedFolderPaths.delete(row.dataset.path);
              debouncedAutoLoad();
            });
            // Insert before the icon element if present
            if (icon && icon.parentNode) icon.parentNode.insertBefore(cb, icon);
            else row.insertBefore(cb, row.firstChild);
          }
        }
      } catch (e) { /* failsafe */ }
    }

    // Build a single tree node DOM element.
    // flatOrder is mutated to collect visible paths in order (for range-select).
    // ancestorContinueFlags: array of booleans, one per ancestor depth ≥ 1.
    //   Element i = true if the ancestor at depth i+1 has more siblings after the
    //   current branch (the vertical line at that ancestor's column continues
    //   downward past this row). null means "this is the root" — no rail at all.
    // isLastSibling: whether this node is the last among its own siblings.
    //   Determines elbow shape (└─ vs ├─).
    function buildTreeNode(node, flatOrder, ancestorContinueFlags = null, isLastSibling = true) {
      flatOrder.push(node.path);

      const wrap = document.createElement('div');
      wrap.className = 'tree-node';

      const row = document.createElement('div');

      function subtreeHasKestrel(n) {
        if (!n) return false;
        if (n.has_kestrel) return true;
        if (!n.children) return false;
        for (const c of n.children) if (subtreeHasKestrel(c)) return true;
        return false;
      }

      const norm = p => (p || '').replace(/\\/g, '/');
      const normPath = norm(node.path);
      const isInProgress = _inProgressFolderPaths.has(normPath)
        || (window._ccInProgressFolderPaths && window._ccInProgressFolderPaths.has(normPath));

      const effectiveHasKestrel = subtreeHasKestrel(node) || isInProgress; // Show checkbox for in-progress too
      const outdated = isVersionOutdated(node);
      row.className = 'tree-node-row ' + (effectiveHasKestrel ? 'has-kestrel' : 'no-kestrel') + (outdated ? ' version-outdated' : '') + (isInProgress ? ' in-progress' : '');
      if (node.path === treeActivePath) row.classList.add('active');
      // Path is always visible on hover; in-progress / outdated states append context.
      if (isInProgress) row.title = `Currently analyzing…\n${node.path}`;
      else if (outdated) row.title = `Analyzed on Kestrel v${node.kestrel_version} (current: v${_appVersion})\n${node.path}`;
      else row.title = node.path;

      // ── Rail: ancestor continuation lines + own elbow ──────────────────────
      // Root (ancestorContinueFlags === null) has no rail. Every non-root row
      // gets one slot per ancestor depth ≥ 1 (vert if branch continues, blank
      // otherwise), then a final elbow slot at its own depth.
      const isRoot = ancestorContinueFlags === null;
      if (!isRoot) {
        for (let i = 0; i < ancestorContinueFlags.length; i++) {
          const rail = document.createElement('span');
          rail.className = ancestorContinueFlags[i] ? 'tree-rail tree-rail-vert' : 'tree-rail tree-rail-blank';
          row.appendChild(rail);
        }
        const elbow = document.createElement('span');
        elbow.className = isLastSibling ? 'tree-rail tree-rail-elbow-last' : 'tree-rail tree-rail-elbow-mid';
        row.appendChild(elbow);
      }

      // Arrow toggle
      const arrow = document.createElement('span');
      arrow.className = 'tree-arrow';
      const hasChildren = node.children && node.children.length > 0;
      if (hasChildren) {
        arrow.textContent = '▶';
        if (treeExpandedPaths.has(node.path)) arrow.classList.add('open');
      } else {
        arrow.classList.add('leaf');
        arrow.textContent = '▶';
      }

      // Folder icon
      const icon = document.createElement('span');
      icon.className = 'tree-icon' + (node.has_perch_link ? ' has-perch' : '');
      icon.textContent = node.has_kestrel ? '📂' : '📁';

      // Feather overlay — surfaces "this folder is on Perch" at a glance.
      const perchFeather = node.has_perch_link ? document.createElement('span') : null;
      if (perchFeather) {
        perchFeather.className = 'tree-perch-feather';
        perchFeather.textContent = '\u{1FAB6}';
        perchFeather.title = 'Published to Perch';
      }

      // Hourglass overlay — surfaces "this folder is being analyzed" at a glance.
      const inProgressHourglass = isInProgress ? document.createElement('span') : null;
      if (inProgressHourglass) {
        inProgressHourglass.className = 'tree-in-progress-hourglass';
        inProgressHourglass.textContent = '⏳';
        inProgressHourglass.title = 'Analysis in progress';
      }

      // Label
      const label = document.createElement('span');
      label.className = 'tree-label';
      label.textContent = node.name;

      // Attach path to the row for async inspection
      row.dataset.path = node.path;

      // Count placeholder (populated asynchronously)
      const countSpan = document.createElement('span');
      countSpan.className = 'tree-count';
      countSpan.textContent = '';

      // ── Right-side checkbox column ─────────────────────────────────────────
      // Always present (placeholder if no checkbox) so the right edge stays
      // aligned across analyzed and unanalyzed rows. Reserved for future
      // per-row actions (quick-analyze, etc.) in the same column.
      const cbCol = document.createElement('span');
      cbCol.className = 'tree-cb-col';
      let loadCheckbox = null;
      if (node.has_kestrel || isInProgress) {
        loadCheckbox = document.createElement('input');
        loadCheckbox.type = 'checkbox';
        loadCheckbox.className = 'tree-cb';
        loadCheckbox.title = isInProgress ? 'Include in multi-folder view (analyzing now)' : 'Include in multi-folder view';
        loadCheckbox.checked = _isPathChecked(node.path);
        loadCheckbox.addEventListener('change', (e) => {
          e.stopPropagation();
          if (loadCheckbox.checked) checkedFolderPaths.add(node.path);
          else checkedFolderPaths.delete(node.path);
          _updateAutoRefreshTimers();
          if (typeof updateSelectToggleVisibility === 'function') updateSelectToggleVisibility();
          debouncedAutoLoad();
        });
        cbCol.appendChild(loadCheckbox);
      }

      row.appendChild(arrow);
      row.appendChild(icon);
      row.appendChild(label);
      row.appendChild(countSpan);
      // Status emojis sit on the right edge, adjacent to the checkbox, so they
      // don't crowd the folder name on the left.
      if (perchFeather) row.appendChild(perchFeather);
      if (inProgressHourglass) row.appendChild(inProgressHourglass);
      row.appendChild(cbCol);
      wrap.appendChild(row);

      // Children container
      let childWrap = null;
      if (hasChildren) {
        childWrap = document.createElement('div');
        childWrap.className = 'tree-children';
        if (!treeExpandedPaths.has(node.path)) childWrap.classList.add('hidden');
        // Children at depth (this.depth + 1). Their ancestorContinueFlags = our
        // flags + a new entry at our own depth saying "this row continues if I
        // have more siblings after me". Root passes [] (children start fresh).
        const childAncestors = isRoot ? [] : [...ancestorContinueFlags, !isLastSibling];
        const childCount = node.children.length;
        node.children.forEach((child, idx) => {
          const childIsLast = idx === childCount - 1;
          childWrap.appendChild(buildTreeNode(child, flatOrder, childAncestors, childIsLast));
        });
        wrap.appendChild(childWrap);

        arrow.addEventListener('click', (e) => {
          e.stopPropagation();
          const open = treeExpandedPaths.has(node.path);
          if (open) {
            treeExpandedPaths.delete(node.path);
            arrow.classList.remove('open');
            childWrap.classList.add('hidden');
          } else {
            treeExpandedPaths.add(node.path);
            arrow.classList.add('open');
            childWrap.classList.remove('hidden');
          }
        });
      }

      // Label/icon click toggles expand/collapse (same as clicking the chevron).
      // Loading/unloading a folder is reserved for the checkbox per Phase 2
      // decision — clicking the label is non-destructive.
      const toggleExpand = (e) => {
        e.stopPropagation();
        treeActivePath = node.path;
        if (!hasChildren) {
          renderFolderTree();
          return;
        }
        const open = treeExpandedPaths.has(node.path);
        if (open) {
          treeExpandedPaths.delete(node.path);
          arrow.classList.remove('open');
          if (childWrap) childWrap.classList.add('hidden');
        } else {
          treeExpandedPaths.add(node.path);
          arrow.classList.add('open');
          if (childWrap) childWrap.classList.remove('hidden');
        }
        // Update the active highlight without a full re-render.
        document.querySelectorAll('#folderTree .tree-node-row.active').forEach(r => r.classList.remove('active'));
        row.classList.add('active');
      };
      label.addEventListener('click', toggleExpand);
      icon.addEventListener('click', toggleExpand);

      // (Right-click "Clear Kestrel Analysis Data" context menu removed —
      // the action now lives in the per-folder Folder Actions dropdown in
      // the scene-grid header. Discoverable, single entry point.)

      return wrap;
    }

    // Phase 3: populateAnalyzeFolderCounts removed. The Phase 3 Analyze
    // Folders dialog has its own independent inspection cache + render
    // pipeline (analyze-dialog.js:_inspectAndRenderAnalyzeDlg) using
    // pure-CSS state pills. The legacy 6-class color coding it set
    // (analyzed-full / analyzed-partial / analyzed-none / no-photos-deep /
    // no-photos-shallow / has-errored-images) is no longer used.

    function _normRootPath(p) {
      return (p || '').replace(/\\/g, '/').replace(/\/+$/, '');
    }

    function ellipsizeRecentsPath(path, maxLen = 28) {
      if (!path) return '';
      const s = path.replace(/\\/g, '/');
      if (s.length <= maxLen) return s;
      const parts = s.split('/').filter(Boolean);
      if (parts.length <= 2) return s.slice(0, maxLen - 1) + '…';
      const t = `${parts[0]}/…/${parts[parts.length - 1]}`;
      return t.length <= maxLen ? t : (t.slice(0, maxLen - 1) + '…');
    }

    /** Build recents chip (+ optional × dismiss). Returns a .folder-recents-chip-wrap. */
    function buildFolderRecentsChip(path, onClick, onDismiss) {
      const wrap = document.createElement('span');
      wrap.className = 'folder-recents-chip-wrap';
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'folder-recents-chip';
      chip.title = path;
      chip.dataset.path = path;
      const plus = document.createElement('span');
      plus.className = 'folder-recents-chip-plus';
      plus.textContent = '+ \u{1F4C2}';
      const label = document.createElement('span');
      label.textContent = ' ' + ellipsizeRecentsPath(path);
      chip.appendChild(plus);
      chip.appendChild(label);
      chip.addEventListener('click', onClick);
      wrap.appendChild(chip);
      if (onDismiss) {
        const x = document.createElement('button');
        x.type = 'button';
        x.className = 'folder-recents-chip-dismiss';
        x.setAttribute('aria-label', 'Remove from recents');
        x.textContent = '\u00d7';
        x.addEventListener('click', (e) => {
          e.stopPropagation();
          e.preventDefault();
          onDismiss(path);
        });
        wrap.appendChild(x);
      }
      return wrap;
    }

    async function persistFolderRecentsBump(path) {
      const np = _normRootPath(path);
      if (!np) return;
      try {
        const s = loadSettings();
        const existing = Array.isArray(s.folder_recents) ? s.folder_recents : [];
        const filtered = existing.filter(p => _normRootPath(p) !== np);
        s.folder_recents = [np, ...filtered].slice(0, 8);
        saveSettings(s);
        if (hasPywebviewApi && window.pywebview?.api?.save_settings_data) {
          try { await window.pywebview.api.save_settings_data(s); } catch (_) { }
        }
        if (typeof renderFolderRecentsChips === 'function') renderFolderRecentsChips();
      } catch (_) { /* best-effort */ }
    }

    async function persistFolderRecentsRemove(path) {
      const np = _normRootPath(path);
      if (!np) return;
      try {
        const s = loadSettings();
        const existing = Array.isArray(s.folder_recents) ? s.folder_recents : [];
        s.folder_recents = existing.filter(p => _normRootPath(p) !== np);
        saveSettings(s);
        if (hasPywebviewApi && window.pywebview?.api?.save_settings_data) {
          try { await window.pywebview.api.save_settings_data(s); } catch (_) { }
        }
        if (typeof renderFolderRecentsChips === 'function') renderFolderRecentsChips();
      } catch (_) { }
    }

    async function persistAnalyzeRecentsRemove(path) {
      const np = _normRootPath(path);
      if (!np) return;
      try {
        const s = loadSettings();
        const existing = Array.isArray(s.analyze_recents) ? s.analyze_recents : [];
        s.analyze_recents = existing.filter(e => e && _normRootPath(e.path) !== np);
        saveSettings(s);
        if (hasPywebviewApi && window.pywebview?.api?.save_settings_data) {
          try { await window.pywebview.api.save_settings_data(s); } catch (_) { }
        }
        if (typeof renderAnalyzeDlgRecentsChips === 'function') {
          renderAnalyzeDlgRecentsChips().catch(() => {});
        }
      } catch (_) { }
    }

    /**
     * When the sidebar tree is empty, add the analyzing folder as a root and
     * auto-check + auto-load it. Used by local queue and cloud submit paths.
     */
    async function autoLoadFolderWhenTreeEmpty(folderPath) {
      if (!folderPath) return;
      if (_hasAnyRoots()) return;
      if (!hasPywebviewApi || !window.pywebview?.api?.list_subfolders) return;
      try {
        const r = await addFolderRoot(folderPath);
        if (!r || (!r.added && !r.alreadyLoaded)) return;
        const loadPath = (r.node && r.node.path) ? r.node.path : _normRootPath(folderPath);
        checkedFolderPaths.add(loadPath);
        renderFolderTree();
        if (typeof updateInProgressFoldersInTree === 'function') updateInProgressFoldersInTree();
        if (typeof debouncedAutoLoad === 'function') await debouncedAutoLoad();
        if (typeof setStatus === 'function') {
          setStatus('Auto-loaded folder (tree was empty)');
        }
      } catch (e) {
        console.warn('[tree] autoLoadFolderWhenTreeEmpty failed:', e);
      }
    }

    // ── End Folder Tree ───────────────────────────────────────────────────────────

