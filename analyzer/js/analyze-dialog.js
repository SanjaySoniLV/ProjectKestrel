    // ── Analyze Folders Dialog (Phase 3 — completely independent state) ──────────
    //
    // The dialog tree no longer reads or writes the main sidebar tree's state
    // (folderTreeRootNodes / folderTreeRootOrder / checkedFolderPaths). It has
    // its own Map, its own checked set, its own expanded set, its own recents
    // settings key (analyze_recents). The two trees never share state. This
    // makes "what am I viewing" (main) and "what am I queueing for analysis"
    // (dialog) two cleanly-separable mental models.

    // ── Time estimation constants (Phase 3I) ────────────────────────────────────
    // Hardcoded baseline for now. The runtime "analyzing" badge already shows
    // live ETA during processing; this is just the PRE-queue estimate so the
    // user can gauge how much work they're about to commit. Telemetry-backed
    // local rates are deferred to a follow-up.
    const _EST_SECS_PER_IMG_GPU = 1.2;
    const _EST_SECS_PER_IMG_CPU = 8.0;
    const _EST_MIN_IMAGES_TO_ESTIMATE = 50;

    // ── Dialog-local state (NEVER shared with the main tree) ────────────────────
    let analyzeDlgRootNodes = new Map();      // Map<rootPath, syntheticRootNode>
    let analyzeDlgRootOrder = [];             // insertion order (render order)
    let analyzeDlgExpandedPaths = new Set();
    let analyzeDlgCheckedPaths = new Set();   // queue-builder selections
    let analyzeDlgInspectionCache = new Map();// Map<path, {total, processed, errored, has_kestrel, kestrel_version}>
    let analyzeDlgReanalyzeUnlocked = false;  // gates the "re-analyze fully-analyzed" path
    let _analyzeDlgInspectionVersion = 0;     // bumped on each scan to cancel stale callbacks

    // ── Multi-root accessors (dialog-scoped mirror of folder-tree.js helpers) ────
    function _adlgHasAnyRoots() { return analyzeDlgRootOrder.length > 0; }
    function _adlgGetAllRoots() {
      return analyzeDlgRootOrder.map(p => analyzeDlgRootNodes.get(p)).filter(Boolean);
    }
    function _adlgNormRoot(p) {
      return (p || '').replace(/\\/g, '/').replace(/\/+$/, '');
    }
    function _adlgFindNode(targetPath) {
      const target = _adlgNormRoot(targetPath);
      for (const root of _adlgGetAllRoots()) {
        const found = _adlgFindNodeInSubtree(root, target);
        if (found) return found;
      }
      return null;
    }
    function _adlgFindNodeInSubtree(node, targetNorm) {
      if (!node) return null;
      if (_adlgNormRoot(node.path) === targetNorm) return node;
      if (node.children) {
        for (const c of node.children) {
          const f = _adlgFindNodeInSubtree(c, targetNorm);
          if (f) return f;
        }
      }
      return null;
    }

    // ── Add / remove / clear roots (dialog-local) ───────────────────────────────
    async function addAnalyzeDlgRoot(rootPath) {
      if (!hasPywebviewApi || !window.pywebview?.api?.list_subfolders) {
        return { added: false, error: 'no-bridge' };
      }
      if (!rootPath) return { added: false, error: 'no-path' };
      const norm = _adlgNormRoot(rootPath);
      if (analyzeDlgRootNodes.has(norm)) {
        return { added: false, alreadyLoaded: true, node: analyzeDlgRootNodes.get(norm) };
      }
      // Subdir / ancestor overlap rejection — same policy as the main tree.
      for (const existing of analyzeDlgRootOrder) {
        if (norm.startsWith(existing + '/')) {
          return { added: false, alreadyLoaded: true, reason: 'subdir-of', containingRoot: existing };
        }
      }
      const childRootsToReplace = analyzeDlgRootOrder.filter(existing => existing.startsWith(norm + '/'));
      for (const child of childRootsToReplace) {
        analyzeDlgRootNodes.delete(child);
      }
      analyzeDlgRootOrder = analyzeDlgRootOrder.filter(p => !childRootsToReplace.includes(p));

      const depth = getSetting('treeScanDepth', 3);
      try {
        const result = await window.pywebview.api.list_subfolders(norm, depth);
        if (!result || !result.success) {
          return { added: false, error: (result && result.error) || 'scan-failed' };
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
        analyzeDlgRootNodes.set(norm, node);
        analyzeDlgRootOrder.push(norm);
        analyzeDlgExpandedPaths.add(norm);
        // Expand visible non-greyed subtrees so the user can see what's
        // inside the picked parent — but don't auto-check anything yet
        // (auto-check happens AFTER inspection resolves).
        _adlgAutoExpandVisible(node);
        return { added: true, rootHasKestrel, node };
      } catch (e) {
        console.error('[analyzeDlg] addAnalyzeDlgRoot error', e);
        return { added: false, error: String(e) };
      }
    }

    // Expand any non-greyed descendant that has children. "Greyed" status
    // is determined post-inspection (no-photos folders fade), so during the
    // initial expand we conservatively expand every container — the post-
    // inspection re-render will keep the layout stable.
    function _adlgAutoExpandVisible(node) {
      if (!node || !node.children || node.children.length === 0) return;
      for (const c of node.children) {
        if (c.children && c.children.length > 0) {
          analyzeDlgExpandedPaths.add(c.path);
          _adlgAutoExpandVisible(c);
        }
      }
    }

    function clearAnalyzeDlgRoots() {
      analyzeDlgRootNodes.clear();
      analyzeDlgRootOrder = [];
      analyzeDlgExpandedPaths.clear();
      analyzeDlgCheckedPaths.clear();
      analyzeDlgInspectionCache.clear();
      analyzeDlgReanalyzeUnlocked = false;
      const unlockBox = document.getElementById('analyzeDlgReanalyzeUnlock');
      if (unlockBox) unlockBox.checked = false;
    }

    // ── State pill computation (Phase 3F) ───────────────────────────────────────
    //
    // Two orthogonal axes encode every folder's status:
    //   color = relationship to queue (blue done / orange queued / gray idle / red errored / dim no-photos)
    //   shape = work progress (solid done / half partial / outline not-started / dot no-photos / warn errored)
    //
    // Returns { color, shape } for the given path. `node` is the tree node
    // (for static metadata like has_kestrel/kestrel_version); `info` is the
    // inspection result for that exact path (may be undefined if not yet probed).
    function _computeAnalyzeDlgPill(node, info, isChecked) {
      // Inspection not yet done → neutral outline.
      if (!info) {
        return { color: 'dim', shape: 'outline' };
      }
      const total = info.total || 0;
      const processed = info.processed || 0;
      const errored = info.errored || 0;
      // No photos at all → dim dot (parent folders, exports dirs, etc.)
      if (total === 0) {
        return { color: 'dim', shape: 'dot' };
      }
      // Errored images → red warn (regardless of queue state).
      if (errored > 0) {
        return { color: 'red', shape: 'warn' };
      }
      const isFullyAnalyzed = (processed >= total);
      const isPartial = (processed > 0 && processed < total);
      if (isFullyAnalyzed) {
        return { color: 'blue', shape: 'solid' };
      }
      if (isPartial) {
        return { color: isChecked ? 'orange' : 'gray', shape: 'half' };
      }
      // Not started: outline, orange if queued, gray otherwise.
      return { color: isChecked ? 'orange' : 'gray', shape: 'outline' };
    }

    function _isOutdatedNode(node) {
      // Reuse the main tree's version-compare helper if available.
      if (typeof isVersionOutdated === 'function') {
        try { return isVersionOutdated(node); } catch (e) { /* fall through */ }
      }
      return false;
    }

    // ── Tree rendering (rewritten — uses state pills, dialog-local state) ───────
    function buildAnalyzeDlgNode(node, parentSiblingsLast) {
      const wrap = document.createElement('div');
      wrap.className = 'tree-node';

      const row = document.createElement('div');
      const hasChildren = node.children && node.children.length > 0;
      const isExpanded = analyzeDlgExpandedPaths.has(node.path);
      const isChecked = analyzeDlgCheckedPaths.has(_adlgNormRoot(node.path));
      const info = analyzeDlgInspectionCache.get(_adlgNormRoot(node.path));
      const outdated = _isOutdatedNode(node);
      const noPhotosDeep = info && info.total === 0;

      let cls = 'adlg-node-row';
      if (isChecked) cls += ' queue-sel';
      if (node.has_kestrel) cls += ' has-kestrel';
      if (outdated) cls += ' version-outdated';
      if (noPhotosDeep) cls += ' no-photos-deep';
      row.className = cls;
      row.dataset.path = node.path;
      row.title = outdated
        ? `Analyzed on Kestrel v${node.kestrel_version} (current: v${_appVersion}) — re-analyze to update.\n${node.path}`
        : node.path;

      // Arrow (expand toggle). Leaf rows get a hidden arrow for alignment.
      const arrow = document.createElement('span');
      arrow.className = 'tree-arrow' + (hasChildren ? (isExpanded ? ' open' : '') : ' leaf');
      arrow.textContent = '▶';

      // Checkbox: gated by "has photos to do work on". Empty/already-fully-
      // analyzed folders can still be checked (the worker handles skip-silently
      // and re-analysis-confirmation), but no-photos-deep is disabled.
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'adlg-cb';
      cb.checked = isChecked;
      if (noPhotosDeep) {
        cb.disabled = true;
        cb.title = 'No photos in this folder or any subfolder.';
      }
      cb.addEventListener('change', (e) => {
        e.stopPropagation();
        const norm = _adlgNormRoot(node.path);
        if (cb.checked) analyzeDlgCheckedPaths.add(norm);
        else analyzeDlgCheckedPaths.delete(norm);
        // Live update — refresh just this row's pill + the column-3 summary.
        renderAnalyzeDlgTree();
        refreshAnalyzeDlgSummary();
      });

      // Folder icon
      const icon = document.createElement('span');
      icon.className = 'tree-icon';
      icon.textContent = node.has_kestrel ? '📂' : '📁';

      // Label
      const label = document.createElement('span');
      label.className = 'tree-label';
      label.textContent = node.name;

      // State pill (orthogonal color × shape — Phase 3F)
      const pillSpec = _computeAnalyzeDlgPill(node, info, isChecked);
      const pill = document.createElement('span');
      pill.className = `state-pill state-pill--${pillSpec.color} state-pill--${pillSpec.shape}`;
      pill.setAttribute('aria-hidden', 'true');

      // Count text
      const countSpan = document.createElement('span');
      countSpan.className = 'tree-count';
      if (info && info.total > 0) {
        const erroredFrag = info.errored > 0 ? ` (${info.errored} errored)` : '';
        countSpan.textContent = ` ${info.processed}/${info.total}${erroredFrag}`;
      } else {
        countSpan.textContent = '';
      }

      // Outdated badge (Phase 3F — orthogonal to the pill).
      let outdatedBadge = null;
      if (outdated) {
        outdatedBadge = document.createElement('span');
        outdatedBadge.className = 'adlg-outdated-badge' + (isChecked ? ' warning' : '');
        outdatedBadge.textContent = `v↑ ${node.kestrel_version || ''}`.trim();
        outdatedBadge.title = `Analyzed on v${node.kestrel_version}; current is v${_appVersion}. Will re-analyze (destructive) if checked.`;
      }

      row.appendChild(arrow);
      row.appendChild(cb);
      row.appendChild(pill);
      row.appendChild(icon);
      row.appendChild(label);
      row.appendChild(countSpan);
      if (outdatedBadge) row.appendChild(outdatedBadge);

      // Right-click: clear .kestrel data (preserved from legacy dialog)
      if (node.has_kestrel) {
        row.addEventListener('contextmenu', (e) => {
          e.preventDefault();
          e.stopPropagation();
          showContextMenu(e.clientX, e.clientY, [{
            label: '🗑 Clear Kestrel Analysis Data',
            danger: true,
            action: () => {
              clearKestrelDataForFolder(node.path, node.name, () => {
                node.has_kestrel = false;
                node.kestrel_version = '';
                // Re-inspect and re-render so the pill flips to outline.
                _inspectAndRenderAnalyzeDlg();
              });
            },
          }]);
        });
      }

      wrap.appendChild(row);

      if (hasChildren) {
        const childWrap = document.createElement('div');
        childWrap.className = 'tree-children';
        if (!isExpanded) childWrap.classList.add('hidden');
        node.children.forEach(child => childWrap.appendChild(buildAnalyzeDlgNode(child)));
        wrap.appendChild(childWrap);
        arrow.addEventListener('click', (e) => {
          e.stopPropagation();
          const open = analyzeDlgExpandedPaths.has(node.path);
          if (open) {
            analyzeDlgExpandedPaths.delete(node.path);
            arrow.classList.remove('open');
            childWrap.classList.add('hidden');
          } else {
            analyzeDlgExpandedPaths.add(node.path);
            arrow.classList.add('open');
            childWrap.classList.remove('hidden');
          }
        });
        // Click on label/icon also toggles expand (same as the main tree
        // semantics — non-destructive).
        const toggleExpand = (e) => {
          e.stopPropagation();
          arrow.click();
        };
        label.addEventListener('click', toggleExpand);
        icon.addEventListener('click', toggleExpand);
      }
      return wrap;
    }

    // ── Tree render dispatcher ──────────────────────────────────────────────────
    function renderAnalyzeDlgTree() {
      const treeEl = document.getElementById('analyzeDlgTree');
      const wrap = document.querySelector('.analyze-dlg-tree-wrap');
      const footer = document.getElementById('analyzeDlgFooter');
      if (!treeEl) return;
      treeEl.innerHTML = '';
      if (!_adlgHasAnyRoots()) {
        if (wrap) wrap.classList.remove('has-roots');
        if (footer) footer.classList.add('hidden');
        return;
      }
      if (wrap) wrap.classList.add('has-roots');
      if (footer) footer.classList.remove('hidden');
      for (const root of _adlgGetAllRoots()) {
        treeEl.appendChild(buildAnalyzeDlgNode(root));
      }
    }

    // ── Folder inspection (Phase 3F — populates the pill cache) ─────────────────
    //
    // Walks every visible row in the dialog tree, batch-calls inspect_folders,
    // populates analyzeDlgInspectionCache keyed by normalized path, then
    // re-renders so pills update from "neutral outline" to their real state.
    async function _inspectAndRenderAnalyzeDlg() {
      const myVer = ++_analyzeDlgInspectionVersion;
      renderAnalyzeDlgTree(); // immediate render with whatever cache we have
      if (!hasPywebviewApi || !window.pywebview?.api?.inspect_folders) return;
      // Collect every node path currently in our dialog state.
      const paths = [];
      const seen = new Set();
      function walk(n) {
        if (!n) return;
        const norm = _adlgNormRoot(n.path);
        if (!seen.has(norm)) { seen.add(norm); paths.push(n.path); }
        if (n.children) for (const c of n.children) walk(c);
      }
      for (const root of _adlgGetAllRoots()) walk(root);
      if (paths.length === 0) return;
      const progWrap = document.getElementById('analyzeScanProgress');
      const progFill = document.getElementById('analyzeScanFill');
      const progLabel = document.getElementById('analyzeScanLabel');
      if (progWrap) progWrap.classList.remove('hidden');
      if (progLabel) progLabel.textContent = `Scanning ${paths.length} folder${paths.length === 1 ? '' : 's'}…`;
      if (progFill) progFill.style.width = '10%';
      try {
        const res = await window.pywebview.api.inspect_folders(paths);
        if (myVer !== _analyzeDlgInspectionVersion) return; // cancelled
        if (res && res.success && res.results) {
          // inspect_folders keys results by realpath; we key the cache by
          // normalized-input path. Build a lookup that matches both.
          for (const inputPath of paths) {
            const inputNorm = _adlgNormRoot(inputPath);
            // Try input-path match first, then any result key normalized.
            let info = res.results[inputPath];
            if (info === undefined) {
              for (const [resKey, val] of Object.entries(res.results)) {
                if (_adlgNormRoot(resKey) === inputNorm) { info = val; break; }
              }
            }
            if (info !== undefined) analyzeDlgInspectionCache.set(inputNorm, info);
          }
        }
        if (progFill) progFill.style.width = '100%';
        renderAnalyzeDlgTree();
        // After inspection, auto-check roots that DIRECTLY have work to do
        // (per the user's "auto-check root with direct work" decision).
        _adlgAutoCheckRootsWithDirectWork();
        renderAnalyzeDlgTree();
        refreshAnalyzeDlgSummary();
      } catch (e) {
        console.warn('[analyzeDlg] inspect_folders failed', e);
      } finally {
        setTimeout(() => {
          if (progWrap) progWrap.classList.add('hidden');
          if (progFill) progFill.style.width = '0%';
        }, 350);
      }
    }

    // Auto-check any root that directly contains photos with work to do.
    // Subdirectories are NEVER auto-checked (per user: avoid auto-queueing
    // darktable_exports etc.). Only the root itself gets the convenience.
    function _adlgAutoCheckRootsWithDirectWork() {
      for (const root of _adlgGetAllRoots()) {
        const norm = _adlgNormRoot(root.path);
        const info = analyzeDlgInspectionCache.get(norm);
        if (!info) continue;
        const total = info.total || 0;
        const processed = info.processed || 0;
        const errored = info.errored || 0;
        // "Has direct work" = photos exist AND (not yet fully done OR has errors).
        const hasDirectWork = total > 0 && (processed < total || errored > 0);
        if (hasDirectWork) analyzeDlgCheckedPaths.add(norm);
      }
    }

    // ── Recents chips (Phase 3G — independent from main tree's folder_recents) ──
    async function renderAnalyzeDlgRecentsChips() {
      const row = document.getElementById('analyzeDlgRecentsRow');
      if (!row) return;
      const s = (typeof loadSettings === 'function') ? loadSettings() : {};
      const recents = Array.isArray(s.analyze_recents) ? s.analyze_recents.slice(0, 16) : [];
      if (recents.length === 0) {
        row.classList.add('hidden');
        row.innerHTML = '';
        return;
      }
      // Probe existence + still-has-work via inspect_folders.
      const paths = recents.map(r => r.path).filter(Boolean);
      let available = paths;
      let workMap = new Map(); // path-norm -> info
      try {
        if (hasPywebviewApi && window.pywebview?.api?.inspect_folders && paths.length > 0) {
          const res = await window.pywebview.api.inspect_folders(paths);
          if (res && res.success) {
            // Keep paths that have work remaining OR errored images.
            available = paths.filter(p => {
              const norm = _adlgNormRoot(p);
              let info = res.results && res.results[p];
              if (info === undefined && res.results) {
                for (const [k, v] of Object.entries(res.results)) {
                  if (_adlgNormRoot(k) === norm) { info = v; break; }
                }
              }
              if (!info) return false;
              workMap.set(norm, info);
              const total = info.total || 0;
              const processed = info.processed || 0;
              const errored = info.errored || 0;
              return total === 0 || processed < total || errored > 0;
            });
          } else if (res && Array.isArray(res.invalid_paths)) {
            const invalid = new Set(res.invalid_paths.map(_adlgNormRoot));
            available = paths.filter(p => !invalid.has(_adlgNormRoot(p)));
          }
        }
      } catch (e) { /* best-effort */ }
      // Hide chips for paths already loaded in the dialog tree.
      const loaded = new Set(analyzeDlgRootOrder);
      available = available.filter(p => !loaded.has(_adlgNormRoot(p)));
      if (available.length === 0) {
        row.classList.add('hidden');
        row.innerHTML = '';
        return;
      }
      function ellipsize(p, maxLen = 28) {
        if (!p) return '';
        const s = p.replace(/\\/g, '/');
        if (s.length <= maxLen) return s;
        const parts = s.split('/').filter(Boolean);
        if (parts.length <= 2) return s.slice(0, maxLen - 1) + '…';
        const t = `${parts[0]}/…/${parts[parts.length - 1]}`;
        return t.length <= maxLen ? t : (t.slice(0, maxLen - 1) + '…');
      }
      row.innerHTML = '';
      for (const path of available) {
        const chip = document.createElement('button');
        chip.type = 'button';
        chip.className = 'folder-recents-chip';
        chip.title = path;
        const plus = document.createElement('span');
        plus.className = 'folder-recents-chip-plus';
        plus.textContent = '+ 📂';
        const label = document.createElement('span');
        label.textContent = ' ' + ellipsize(path);
        chip.appendChild(plus); chip.appendChild(label);
        chip.addEventListener('click', async () => {
          chip.disabled = true;
          try {
            const r = await addAnalyzeDlgRoot(path);
            if (r && r.added) {
              // Bump-to-top in analyze_recents + persist.
              await _persistAnalyzeRecentsBump(path);
              await renderAnalyzeDlgRecentsChips();
              await _inspectAndRenderAnalyzeDlg();
            }
          } finally { chip.disabled = false; }
        });
        row.appendChild(chip);
      }
      row.classList.remove('hidden');
    }

    async function _persistAnalyzeRecentsBump(path) {
      try {
        const s = loadSettings();
        const existing = Array.isArray(s.analyze_recents) ? s.analyze_recents : [];
        const np = _adlgNormRoot(path);
        const filtered = existing.filter(e => e && _adlgNormRoot(e.path) !== np);
        const ts = new Date().toISOString();
        s.analyze_recents = [{ path: np, timestamp: ts }, ...filtered].slice(0, 16);
        saveSettings(s);
        if (hasPywebviewApi && window.pywebview?.api?.save_settings_data) {
          try { await window.pywebview.api.save_settings_data(s); } catch (_) { }
        }
      } catch (e) { /* best-effort */ }
    }

    // ── Queue summary computation (Phase 3I) ────────────────────────────────────
    //
    // Reads analyzeDlgCheckedPaths + analyzeDlgInspectionCache. Computes:
    //   - total images to process (skips already-fully-analyzed unless unlock is on)
    //   - total folders
    //   - per-folder list (name + image count + pill)
    //   - estimated time (hardcoded baseline; "—" if too few images)
    //   - warning summary if any folders will lose user data
    //   - shows/hides the "Re-analyze fully-analyzed" unlock row
    function refreshAnalyzeDlgSummary() {
      const headline = document.getElementById('analyzeDlgSummaryHeadline');
      const warningsEl = document.getElementById('analyzeDlgWarnings');
      const listEl = document.getElementById('analyzeDlgQueuedList');
      const unlockRow = document.getElementById('analyzeDlgReanalyzeUnlockRow');
      const startBtn = document.getElementById('analyzeDlgAdd');
      const countEl = document.getElementById('analyzeDlgCount');
      if (!headline || !listEl || !startBtn) return;

      const useGpu = !!document.getElementById('analyzeUseGpu')?.checked;
      const retryErrored = !!document.getElementById('adlgRetryErrored')?.checked;
      const checkedNorms = Array.from(analyzeDlgCheckedPaths);
      const checkedCount = checkedNorms.length;

      // Classify each checked folder.
      let totalImagesToProcess = 0;
      let fullyAnalyzedSkipCount = 0;       // would skip-silently
      let fullyAnalyzedWillReanalyzeCount = 0; // will lose data (unlock ON)
      let outdatedReanalyzeCount = 0;       // will lose data (outdated → always re-analyze)
      const perFolder = []; // {name, path, info, pillSpec, willSkip, willReanalyze}
      for (const norm of checkedNorms) {
        const node = _adlgFindNode(norm);
        const info = analyzeDlgInspectionCache.get(norm);
        const name = node ? node.name : (norm.split('/').filter(Boolean).pop() || norm);
        const total = (info && info.total) || 0;
        const processed = (info && info.processed) || 0;
        const errored = (info && info.errored) || 0;
        const outdated = node ? _isOutdatedNode(node) : false;
        const isFullyAnalyzed = total > 0 && processed >= total;
        let willSkip = false;
        let willReanalyze = false;
        let imagesThisFolder = 0;
        if (outdated) {
          // Outdated always wipes + re-analyzes.
          willReanalyze = true;
          outdatedReanalyzeCount++;
          imagesThisFolder = total;
        } else if (isFullyAnalyzed && errored === 0) {
          if (analyzeDlgReanalyzeUnlocked) {
            willReanalyze = true;
            fullyAnalyzedWillReanalyzeCount++;
            imagesThisFolder = total;
          } else {
            // Skip silently — no work counted.
            willSkip = true;
            fullyAnalyzedSkipCount++;
          }
        } else if (errored > 0 && retryErrored) {
          imagesThisFolder = errored;
        } else if (errored > 0 && !retryErrored) {
          // Errored images but retry not ticked → only NEW images counted.
          imagesThisFolder = Math.max(0, total - processed - errored);
        } else {
          imagesThisFolder = Math.max(0, total - processed);
        }
        if (!willSkip) totalImagesToProcess += imagesThisFolder;
        const pillSpec = _computeAnalyzeDlgPill(node || { path: norm }, info, true);
        perFolder.push({ name, path: norm, info, pillSpec, willSkip, willReanalyze, imagesThisFolder });
      }

      // Headline
      if (checkedCount === 0) {
        headline.innerHTML = '<span class="analyze-dlg-summary-empty">Check folders in the queue builder to add them.</span>';
      } else {
        const folderWord = checkedCount === 1 ? 'folder' : 'folders';
        const imgCount = totalImagesToProcess.toLocaleString();
        const skipNote = fullyAnalyzedSkipCount > 0
          ? ` <span class="analyze-dlg-summary-folder-count">(${fullyAnalyzedSkipCount} will be skipped — already analyzed)</span>`
          : '';
        // Time estimate
        let timeFrag = '';
        if (totalImagesToProcess >= _EST_MIN_IMAGES_TO_ESTIMATE) {
          const rate = useGpu ? _EST_SECS_PER_IMG_GPU : _EST_SECS_PER_IMG_CPU;
          const seconds = totalImagesToProcess * rate;
          timeFrag = `<span class="analyze-dlg-summary-time">~${_formatDuration(seconds)} estimated (${useGpu ? 'GPU' : 'CPU'})</span>`;
        } else if (totalImagesToProcess > 0) {
          timeFrag = `<span class="analyze-dlg-summary-time">— too few images to estimate time</span>`;
        }
        headline.innerHTML =
          `<div class="analyze-dlg-summary-image-count">${imgCount} image${totalImagesToProcess === 1 ? '' : 's'}</div>` +
          `<div class="analyze-dlg-summary-folder-count">across ${checkedCount} ${folderWord}${skipNote}</div>` +
          timeFrag;
      }

      // Warnings
      const reanalyzeWithUnlockCount = fullyAnalyzedWillReanalyzeCount + outdatedReanalyzeCount;
      if (reanalyzeWithUnlockCount > 0) {
        const word = reanalyzeWithUnlockCount === 1 ? 'folder' : 'folders';
        warningsEl.innerHTML = `⚠ ${reanalyzeWithUnlockCount} ${word} will lose user data on re-analysis (ratings, decisions, scene names).`;
        warningsEl.classList.remove('hidden');
      } else {
        warningsEl.classList.add('hidden');
        warningsEl.innerHTML = '';
      }

      // Per-folder list
      listEl.innerHTML = '';
      if (perFolder.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'analyze-dlg-queued-list-empty';
        empty.textContent = 'No folders queued yet.';
        listEl.appendChild(empty);
      } else {
        for (const f of perFolder) {
          const item = document.createElement('div');
          item.className = 'analyze-dlg-queued-item';
          item.title = f.path;
          const pill = document.createElement('span');
          pill.className = `state-pill state-pill--${f.pillSpec.color} state-pill--${f.pillSpec.shape}`;
          const name = document.createElement('span');
          name.className = 'analyze-dlg-queued-item-name';
          name.textContent = f.name;
          const count = document.createElement('span');
          count.className = 'analyze-dlg-queued-item-count';
          if (f.willSkip) count.textContent = 'will skip';
          else if (f.willReanalyze) count.textContent = `${f.imagesThisFolder} (re-analyze)`;
          else count.textContent = `${f.imagesThisFolder} img`;
          item.appendChild(pill);
          item.appendChild(name);
          item.appendChild(count);
          listEl.appendChild(item);
        }
      }

      // Unlock row: visible only when at least one fully-analyzed-current-version
      // folder is checked (i.e., would skip silently OR is being re-analyzed via unlock).
      const showUnlockRow = fullyAnalyzedSkipCount > 0 || fullyAnalyzedWillReanalyzeCount > 0;
      if (unlockRow) {
        unlockRow.classList.toggle('hidden', !showUnlockRow);
      }

      // Selected count + Start button enable state
      if (countEl) {
        countEl.textContent = `${checkedCount} folder${checkedCount === 1 ? '' : 's'} selected`;
      }
      // Start button enabled if there's any work to do OR any skip-silently
      // pending (the worker handles the no-op gracefully).
      startBtn.disabled = checkedCount === 0;
    }

    function _formatDuration(seconds) {
      if (!isFinite(seconds) || seconds <= 0) return '—';
      const h = Math.floor(seconds / 3600);
      const m = Math.floor((seconds % 3600) / 60);
      const s = Math.round(seconds % 60);
      if (h > 0) return `${h}h ${m}m`;
      if (m > 0) return `${m}m ${s}s`;
      return `${s}s`;
    }

    // ── Dialog open / settings hydration ────────────────────────────────────────
    async function openAnalyzeDialog() {
      if (!hasPywebviewApi) {
        alert('Analysis queue is only available in the desktop (pywebview) mode.\n\nRun kestrel_visualizer as a desktop app to use this feature.');
        return;
      }
      // Hydrate critical settings from persisted values. The radios mirror to
      // the hidden #adlgWildlifeModelMode <select> so existing read/write paths
      // (event-wiring.js Start handler) keep working unchanged.
      const _hiddenModelSelect = document.getElementById('adlgWildlifeModelMode');
      const savedMode = String(getSetting('wildlife_model_mode', 'accurate') || 'accurate').toLowerCase();
      const modeFinal = (savedMode === 'fast') ? 'fast' : 'accurate';
      if (_hiddenModelSelect) _hiddenModelSelect.value = modeFinal;
      const _modeAccurate = document.getElementById('adlgWildlifeModelModeAccurate');
      const _modeFast = document.getElementById('adlgWildlifeModelModeFast');
      if (_modeAccurate && _modeFast) {
        _modeAccurate.checked = (modeFinal === 'accurate');
        _modeFast.checked = (modeFinal === 'fast');
        // Mirror radio change → hidden select (so the existing read path works).
        const _syncModeRadios = () => {
          const v = _modeAccurate.checked ? 'accurate' : 'fast';
          if (_hiddenModelSelect) _hiddenModelSelect.value = v;
        };
        _modeAccurate.addEventListener('change', _syncModeRadios);
        _modeFast.addEventListener('change', _syncModeRadios);
      }

      // Hydrate "More options" settings from persisted values (same keys as before).
      const _adlgDt = document.getElementById('adlgDetectionThreshold');
      if (_adlgDt) _adlgDt.value = getSetting('detection_threshold', 0.25);
      const _adlgMbc = document.getElementById('adlgMaxBirdCrops');
      if (_adlgMbc) _adlgMbc.value = getSetting('max_bird_crops', 10);
      const _adlgEq = document.getElementById('adlgExposureQuality');
      if (_adlgEq) {
        const savedEq = String(getSetting('exposure_quality', 'balanced') || 'balanced').toLowerCase();
        _adlgEq.value = ['lenient', 'balanced', 'aggressive'].includes(savedEq) ? savedEq : 'balanced';
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

      // Unlock checkbox state — reset every open. User must explicitly re-tick
      // it to unlock destructive re-analysis on each queue.
      const unlockBox = document.getElementById('analyzeDlgReanalyzeUnlock');
      if (unlockBox) {
        unlockBox.checked = false;
        analyzeDlgReanalyzeUnlocked = false;
        // Wire the change handler (idempotent — multiple openAnalyzeDialog calls
        // would add multiple listeners; gate with a sentinel).
        if (!unlockBox.dataset.wired) {
          unlockBox.dataset.wired = '1';
          unlockBox.addEventListener('change', () => {
            analyzeDlgReanalyzeUnlocked = !!unlockBox.checked;
            refreshAnalyzeDlgSummary();
          });
        }
      }
      // Wire GPU + retry-errored toggles to refresh summary live (they affect
      // time estimate + image counts).
      const _gpuBox = document.getElementById('analyzeUseGpu');
      if (_gpuBox && !_gpuBox.dataset.wiredSummary) {
        _gpuBox.dataset.wiredSummary = '1';
        _gpuBox.addEventListener('change', refreshAnalyzeDlgSummary);
      }
      const _retryBox = document.getElementById('adlgRetryErrored');
      if (_retryBox && !_retryBox.dataset.wiredSummary) {
        _retryBox.dataset.wiredSummary = '1';
        _retryBox.addEventListener('change', refreshAnalyzeDlgSummary);
      }

      // Render recents first (no roots yet → just shows chips). Then if the
      // dialog already has roots from a prior open (we keep state across
      // opens until Clear), inspect + render the tree.
      await renderAnalyzeDlgRecentsChips();
      if (_adlgHasAnyRoots()) {
        renderAnalyzeDlgTree();
        await _inspectAndRenderAnalyzeDlg();
      } else {
        renderAnalyzeDlgTree();
      }
      refreshAnalyzeDlgSummary();
      document.getElementById('analyzeQueueDlg').showModal();
    }

