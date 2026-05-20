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
        const subtreeHas = selfHasKestrel || descendantHas;
        if (subtreeHas && n.children && n.children.length > 0) {
          treeExpandedPaths.add(n.path);
        }
        if (selfHasKestrel) {
          checkedFolderPaths.add(n.path);
        }
        return subtreeHas;
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
      // Dedup against existing roots
      if (folderTreeRootNodes.has(norm)) {
        return { added: false, alreadyLoaded: true, node: folderTreeRootNodes.get(norm) };
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
      return true;
    }

    // Unload every root and reset tree state. Returns the tree to its empty
    // visual state (welcome panel + recents chips visible).
    function clearAllFolderRoots() {
      folderTreeRootNodes.clear();
      folderTreeRootOrder = [];
      checkedFolderPaths.clear();
      _checkedFolderPathSnapshot.clear();
      treeExpandedPaths.clear();
      treeActivePath = null;
      renderFolderTree();
      if (typeof updateSelectToggleVisibility === 'function') updateSelectToggleVisibility();
      if (typeof updateEmptyHintCopy === 'function') updateEmptyHintCopy();
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

    /** Clear kestrel analysis data for a folder (with confirmation). */
    async function clearKestrelDataForFolder(folderPath, folderName, refreshCallback) {
      const confirmed = confirm(
        `Are you sure you want to delete all Kestrel analysis data for "${folderName}"?\n\n` +
        `This will permanently remove the .kestrel folder and all its contents (database, exports, thumbnails) ` +
        `in:\n${folderPath}\n\nThis action cannot be undone.`
      );
      if (!confirmed) return;
      try {
        const result = await window.pywebview.api.clear_kestrel_data(folderPath);
        if (result && result.success) {
          showToast('Kestrel analysis data cleared for ' + folderName);
          if (refreshCallback) refreshCallback();
        } else {
          alert('Failed to clear analysis data:\n\n' + (result?.error || 'Unknown error'));
        }
      } catch (e) {
        alert('Failed to clear analysis data:\n\n' + (e.message || e));
      }
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
      const isInProgress = _inProgressFolderPaths.has(normPath);

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
      icon.className = 'tree-icon';
      icon.textContent = node.has_kestrel ? '📂' : '📁';

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

      // Right-click context menu for clearing analysis data (still gated to
      // .kestrel folders since "clear analysis data" only applies there).
      if (node.has_kestrel) {
        row.addEventListener('contextmenu', (e) => {
          e.preventDefault();
          e.stopPropagation();
          showContextMenu(e.clientX, e.clientY, [
            {
              label: '🗑 Clear Kestrel Analysis Data',
              danger: true,
              action: () => {
                clearKestrelDataForFolder(node.path, node.name, () => {
                  node.has_kestrel = false;
                  node.kestrel_version = '';
                  renderFolderTree();
                });
              }
            }
          ]);
        });
      }

      return wrap;
    }

    // Populate folder counts ONLY for the Analyze Folders dialog tree.
    // Two-pass approach: (1) inspect all folders, (2) apply subtree-aware fading & colors.
    // Colors: green=finished, purple=started-not-finished, blue=not-started-but-has-images.
    // Fading: no-photos-deep = folder+all descendants have 0 images (full row faded).
    //         no-photos-shallow = folder has 0 images but some descendant has images (checkbox faded only).
    async function populateAnalyzeFolderCounts() {
      if (!hasPywebviewApi || !window.pywebview?.api?.inspect_folder) return;
      try {
        // Query ONLY the Analyze dialog tree nodes
        const rows = Array.from(document.querySelectorAll('#analyzeDlgTree .adlg-node-row'));
        const norm = p => (p || '').replace(/\\/g, '/');
        const pathToRows = new Map(); // normalized path → [row, ...]
        const normToOriginal = new Map();
        const paths = [];

        for (const row of rows) {
          const origPath = row.dataset.path;
          if (!origPath) continue;
          const np = norm(origPath);
          if (!pathToRows.has(np)) {
            pathToRows.set(np, []);
            normToOriginal.set(np, origPath);
            paths.push(np);
          }
          pathToRows.get(np).push(row);
          const span = row.querySelector('.tree-count');
          if (span) span.textContent = '';
        }
        if (paths.length === 0) return;

        const uniq = Array.from(new Set(paths));
        uniq.sort((a, b) => {
          const da = a.split('/').length, db = b.split('/').length;
          return da !== db ? da - db : a.length - b.length;
        });

        const total = uniq.length;
        let completed = 0;
        // Store inspection results for the second pass
        const inspectionMap = new Map(); // normalized path → { total, processed } | null

        const dlgProgWrap = document.getElementById('analyzeScanProgress');
        const dlgProgFill = document.getElementById('analyzeScanFill');
        const dlgProgLabel = document.getElementById('analyzeScanLabel');
        if (dlgProgWrap) dlgProgWrap.classList.remove('hidden');

        // ── Pass 1: Inspect all folders concurrently ──
        const concurrency = Math.min(8, Math.max(2, Math.ceil(total / 8)));
        let idx = 0;

        async function worker() {
          while (true) {
            const i = idx++;
            if (i >= total) break;
            const np = uniq[i];
            const origPath = normToOriginal.get(np) || np;
            try {
              for (const r of (pathToRows.get(np) || [])) {
                const s = r.querySelector('.tree-count');
                if (s) s.textContent = ' …';
              }
              const res = await window.pywebview.api.inspect_folder(origPath);
              const info = res && res.success ? res.info : null;
              inspectionMap.set(np, info ? { total: info.total || 0, processed: info.processed || 0 } : null);
            } catch (e) {
              console.warn('[populateAnalyzeFolderCounts] error for', origPath, e);
              inspectionMap.set(np, null);
            }
            completed++;
            const pct = Math.round((completed / total) * 100);
            if (dlgProgFill) dlgProgFill.style.width = pct + '%';
            if (dlgProgLabel) dlgProgLabel.textContent = `Scanning folders… (${completed}/${total})`;
          }
        }

        const workers = [];
        for (let w = 0; w < concurrency; w++) workers.push(worker());
        await Promise.all(workers);

        // ── Pass 2: Apply colors and subtree-aware fading ──
        // Helper: does any descendant of `prefix` have images?
        function subtreeHasImages(prefix) {
          const pfx = prefix.endsWith('/') ? prefix : prefix + '/';
          for (const [p, info] of inspectionMap) {
            if (p !== prefix && p.startsWith(pfx) && info && info.total > 0) return true;
          }
          return false;
        }

        // Helper: look up kestrel_version from any loaded root by path.
        function findNodeVersionAcrossRoots(targetPath) {
          const node = findNodeInAnyRoot(targetPath);
          return node ? (node.kestrel_version || '') : '';
        }

        for (const np of uniq) {
          const info = inspectionMap.get(np);
          const related = pathToRows.get(np) || [];
          for (const row of related) {
            const span = row.querySelector('.tree-count');
            row.classList.remove('analyzed-full', 'analyzed-partial', 'analyzed-none',
                                 'no-photos', 'no-photos-deep', 'no-photos-shallow', 'version-outdated',
                                 'has-errored-images');
            row.title = '';
            if (span) { span.title = ''; span.textContent = ''; }

            if (!info) continue;

            const totalImgs = info.total;
            const processedImgs = info.processed;
            const erroredImgs = info.errored || 0;

            if (totalImgs > 0) {
              const countText = erroredImgs > 0
                ? ` ${processedImgs}/${totalImgs} (${erroredImgs} errored)`
                : ` ${processedImgs}/${totalImgs}`;
              if (span) span.textContent = countText;
              if (erroredImgs > 0) {
                row.classList.add('has-errored-images');
              }
              if (processedImgs >= totalImgs && erroredImgs === 0) {
                row.classList.add('analyzed-full');          // green: finished, no errors
                // Check if analyzed on an outdated version
                const origPath = normToOriginal.get(np) || np;
                const nodeVer = findNodeVersionAcrossRoots(origPath);
                if (nodeVer && _appVersion && compareVersions(nodeVer, _appVersion) < 0) {
                  row.classList.add('version-outdated');
                  row.title = `Analyzed on Kestrel v${nodeVer} (current: v${_appVersion}). Consider re-analyzing.`;
                }
              } else if (processedImgs > 0) {
                row.classList.add('analyzed-partial');       // purple: started not finished, OR has errors
              } else {
                row.classList.add('analyzed-none');          // blue: has images, not started
              }
              if (erroredImgs > 0) {
                row.title = `${erroredImgs} image(s) errored during the previous analysis. Tick "Re-attempt errored images" before queuing to retry just those.`;
              }
            } else {
              // This folder has 0 images — determine deep vs shallow fading
              const hasDescendantImages = subtreeHasImages(np);
              if (hasDescendantImages) {
                // Shallow: only fade the checkbox, not the name/arrow (descendant has images)
                row.classList.add('no-photos-shallow');
              } else {
                // Deep: entire row faded (no images anywhere in subtree)
                row.classList.add('no-photos-deep');
              }
              const cb = row.querySelector('.adlg-cb');
              if (cb) { cb.disabled = true; cb.checked = false; }
              const tip = hasDescendantImages
                ? 'No photos in this folder, but subfolders contain images.'
                : 'No supported photos found in this folder or any subfolder.';
              if (span) span.title = tip;
              row.title = tip;
            }
          }
        }

        // Hide progress after brief delay
        setTimeout(() => {
          if (dlgProgWrap) dlgProgWrap.classList.add('hidden');
          if (dlgProgFill) dlgProgFill.style.width = '0%';
          if (dlgProgLabel) dlgProgLabel.textContent = 'Scanning folders…';
        }, 400);
      } catch (e) {
        console.warn('[populateAnalyzeFolderCounts] failed', e);
        const dlgWrap = document.getElementById('analyzeScanProgress');
        if (dlgWrap) dlgWrap.classList.add('hidden');
      }
    }

    // ── End Folder Tree ───────────────────────────────────────────────────────────

