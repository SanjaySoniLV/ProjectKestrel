    // ── Analyze Folders Dialog (completely independent state) ──────────────────
    //
    // The dialog tree never reads or writes the main sidebar tree's state
    // (folderTreeRootNodes / folderTreeRootOrder / checkedFolderPaths). It has
    // its own Map, its own checked set, its own expanded set, its own recents
    // settings key (analyze_recents). The two trees never share state. This
    // makes "what am I viewing" (main) and "what am I queueing for analysis"
    // (dialog) two cleanly-separable mental models.

    // ── Time estimation ────────────────────────────────────────────────────────
    // The maths lives in analyze-estimate.js; these wrappers feed it the
    // settings and the last speed test result.
    const _EST = window.KestrelAnalyzeEstimate;
    const _EST_MIN_IMAGES_TO_ESTIMATE = _EST.MIN_IMAGES_TO_ESTIMATE;
    const _EST_MIN_SAMPLED_IMAGES_FOR_LOCAL = _EST.MIN_SAMPLED_FOR_LOCAL;

    // Returns { rate, source, sampledImgs } where source is 'local' or 'baseline'.
    function _getEstRate(useGpu) {
      const key = useGpu ? 'perf_samples_gpu' : 'perf_samples_cpu';
      const samples = typeof getSetting === 'function' ? getSetting(key, []) : [];
      return _EST.localRate(samples, useGpu);
    }

    function _buildUncertainBadge(sampledImgs) {
      const tip =
        'Estimate is based on average hardware. Kestrel will refine this once ' +
        `you've analyzed ~${_EST_MIN_SAMPLED_IMAGES_FOR_LOCAL} images on this ` +
        `machine (currently: ${sampledImgs}).`;
      const el = document.createElement('span');
      el.className = 'est-time-uncertain';
      el.setAttribute('title', tip);
      el.setAttribute('aria-label', tip);
      el.textContent = '?';
      return el;
    }

    function _cloudSpeedTest() {
      return (typeof _cloudSpeedTestResult !== 'undefined') ? _cloudSpeedTestResult : null;
    }
    function _cloudPipelineParams() {
      return _EST.cloudParams(_cloudSpeedTest()?.pipeline);
    }
    // { secs, analysisSecs, uploadSecs, regime, hasTest }; secs is null before a speed test.
    function _getCloudEstSeconds(totalImages) {
      return _EST.cloudEstimate(totalImages, _cloudSpeedTest());
    }

    const _CLOUD_REGIME_TEXT = {
      upload:   { label: 'Limited by your upload speed',  cls: 'upload' },
      balanced: { label: 'Balanced',                      cls: 'balanced' },
      compute:  { label: 'Limited by Cloud Compute speed', cls: 'compute' },
    };

    function _buildCloudRegimeChip(regime, uploadSecs, computeSecs) {
      const spec = _CLOUD_REGIME_TEXT[regime];
      if (!spec) return null;
      const el = document.createElement('span');
      el.className = `adlg-cloud-regime adlg-cloud-regime--${spec.cls}`;
      el.textContent = spec.label;
      const tip = regime === 'upload'
        ? `Your connection needs ~${KestrelUtil.formatDuration(uploadSecs)} to send these images; `
          + `Cloud Compute could analyze them in ~${KestrelUtil.formatDuration(computeSecs)}. `
          + 'A faster upload would finish sooner.'
        : regime === 'compute'
          ? `These images upload in ~${KestrelUtil.formatDuration(uploadSecs)} but take `
            + `~${KestrelUtil.formatDuration(computeSecs)} to analyze — Cloud Compute is the bottleneck.`
          : `Upload (~${KestrelUtil.formatDuration(uploadSecs)}) and analysis `
            + `(~${KestrelUtil.formatDuration(computeSecs)}) take about the same time.`;
      el.setAttribute('title', tip);
      el.setAttribute('aria-label', tip);
      return el;
    }

    // Always-on "?" badge for cloud est. time. Phrasing differs from the local
    // baseline badge — the cloud "?" stays permanent because the estimate
    // depends on transfer speed + worker availability.
    function _buildCloudUncertainBadge() {
      const p = _cloudPipelineParams();
      const tip =
        'The estimate models your upload speed (once measured) against Cloud ' +
        'Compute analyzing roughly ' +
        `${p.containerImagesPerSec} images per second per cloud machine, using up to ` +
        `${p.maxContainers} machines as the queue grows. Actual time ` +
        'depends on upload speed and how many machines are free.';
      const el = document.createElement('span');
      el.className = 'est-time-uncertain est-time-uncertain--cloud';
      el.setAttribute('title', tip);
      el.setAttribute('aria-label', tip);
      el.textContent = '?';
      return el;
    }

    function _isCloudDestination() {
      try { return typeof _analyzeDestination !== 'undefined' && _analyzeDestination === 'cloud'; }
      catch { return false; }
    }

    // ── Dialog-local state (NEVER shared with the main tree) ────────────────────
    let analyzeDlgRootNodes = new Map();      // Map<rootPath, syntheticRootNode>
    let analyzeDlgRootOrder = [];             // insertion order (render order)
    let analyzeDlgExpandedPaths = new Set();
    let analyzeDlgCheckedPaths = new Set();   // queue-builder selections
    let analyzeDlgInspectionCache = new Map();// Map<path, {total, processed, errored, has_kestrel, pipeline_major}>
    let analyzeDlgReanalyzeUnlocked = false;  // gates the "re-analyze fully-analyzed" path
    let _analyzeDlgInspectionVersion = 0;     // bumped on each scan to cancel stale callbacks
    // Pipeline-3 upgrade eligibility for checked, fully analysed folders
    // (upgrade_v3.eligibility via smart_search_status). A folder whose status
    // is 'upgradable' gets a per-folder "Upgrade to 3.0 / Re-analyze" choice in
    // the queued list, defaulting to the upgrade. Unknown or failed lookups keep
    // the dialog's pre-3.0 behaviour for that folder.
    let analyzeDlgUpgradeStatus = new Map();  // Map<norm, eligibility|null>
    let analyzeDlgUpgradeChoice = new Map();  // Map<norm, 'upgrade'|'reanalyze'>
    const _adlgUpgradePending = new Map();    // Map<norm, Promise>

    /** Fully analysed folders are the only ones offered the upgrade: the
     *  upgrade re-reads stored crops and cannot analyse images missing from
     *  the database, so a partial folder keeps the normal analysis path. */
    function _adlgIsUpgradeCandidate(norm) {
      const info = analyzeDlgInspectionCache.get(norm);
      const total = (info && info.total) || 0;
      return total > 0 && ((info && info.processed) || 0) >= total;
    }

    /** Fetch upgrade eligibility for the candidates among `norms` that have
     *  not been looked up yet. Resolves once every lookup has settled; the
     *  summary re-renders as results arrive. */
    function _adlgEnsureUpgradeStatus(norms) {
      const api = window.pywebview?.api;
      if (!api?.smart_search_status) return Promise.resolve();
      const waits = [];
      for (const norm of norms || []) {
        if (!_adlgIsUpgradeCandidate(norm)) continue;
        if (analyzeDlgUpgradeStatus.has(norm)) continue;
        let p = _adlgUpgradePending.get(norm);
        if (!p) {
          const node = _adlgFindNode(norm);
          const path = (node && node.path) || norm;
          const lookup = (typeof folderPipelineStatus === 'function')
            ? folderPipelineStatus(path)
            : Promise.resolve(api.smart_search_status(path)).then(st => (st?.success ? st.upgrade || null : null));
          p = Promise.resolve(lookup).catch(() => null).then((up) => {
            analyzeDlgUpgradeStatus.set(norm, up || null);
            _adlgUpgradePending.delete(norm);
            const dlg = document.getElementById('analyzeQueueDlg');
            if (dlg && dlg.open) {
              try { renderAnalyzeDlgTree(); refreshAnalyzeDlgSummary(); } catch (_) { }
            }
          });
          _adlgUpgradePending.set(norm, p);
        }
        waits.push(p);
      }
      return Promise.all(waits).then(() => undefined);
    }

    /** 'upgrade' | 'reanalyze' for a folder that can be upgraded to pipeline
     *  3.0, or null when the choice does not apply (not upgradable, not fully
     *  analysed, or eligibility unknown). */
    function analyzeDlgUpgradeModeFor(norm) {
      if (!_adlgIsUpgradeCandidate(norm)) return null;
      const up = analyzeDlgUpgradeStatus.get(norm);
      if (!up || up.status !== 'upgradable') return null;
      return analyzeDlgUpgradeChoice.get(norm) === 'reanalyze' ? 'reanalyze' : 'upgrade';
    }

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

    // Helpers for the load-progress bar — shared between addAnalyzeDlgRoot
    // (the slow list_subfolders call) and _inspectAndRenderAnalyzeDlg (the
    // inspect_folders pass). Surfaces a single progress bar that walks from
    // "Scanning folder structure…" through "Inspecting N folders…" so the
    // user never sees a dead UI gap after clicking Add Folders.
    function _adlgShowProgress(label, pct) {
      const wrap = document.getElementById('analyzeScanProgress');
      const lbl = document.getElementById('analyzeScanLabel');
      const fill = document.getElementById('analyzeScanFill');
      if (wrap) wrap.classList.remove('hidden');
      if (lbl) lbl.textContent = label;
      if (fill) fill.style.width = Math.max(0, Math.min(100, pct)) + '%';
    }
    function _adlgHideProgress(delayMs = 0) {
      const apply = () => {
        const wrap = document.getElementById('analyzeScanProgress');
        const fill = document.getElementById('analyzeScanFill');
        if (wrap) wrap.classList.add('hidden');
        if (fill) fill.style.width = '0%';
      };
      if (delayMs > 0) setTimeout(apply, delayMs);
      else apply();
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
      // Show the scanning progress IMMEDIATELY (before the slow bridge call)
      // so the user never sees a dead-UI gap after clicking Add Folders.
      const folderName = norm.split('/').filter(Boolean).pop() || norm;
      _adlgShowProgress(`Scanning ${folderName}…`, 15);
      try {
        const result = await window.pywebview.api.list_subfolders(norm, depth);
        if (!result || !result.success) {
          _adlgHideProgress();
          return { added: false, error: (result && result.error) || 'scan-failed' };
        }
        _adlgShowProgress(`Scanning ${folderName}…`, 45);
        const rootHasKestrel = !!result.root_has_kestrel;
        const rootName = norm.split('/').filter(Boolean).pop() || norm;
        const node = {
          name: rootName,
          path: norm,
          has_kestrel: rootHasKestrel,
          pipeline_major: result.root_pipeline_major ?? null,
          pipeline_version: result.root_pipeline_version || '',
          upgraded: !!result.root_upgraded,
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
      analyzeDlgUpgradeStatus.clear();
      analyzeDlgUpgradeChoice.clear();
      analyzeDlgReanalyzeUnlocked = false;
      const unlockBox = document.getElementById('analyzeDlgReanalyzeUnlock');
      if (unlockBox) unlockBox.checked = false;
    }

    // ── State pill computation ──────────────────────────────────────────────────
    //
    // Two orthogonal axes — homogenized color scheme so colors only signal
    // queue inclusion, never confuse the user with a "done = blue" that
    // looks selected:
    //   color = queue state (orange = checked-for-queue, gray = not)
    //   shape = work progress (solid = done, half = partial, outline = not started)
    // Red = errored (independent override).
    // Dim dot = no photos / nothing to do.
    function _computeAnalyzeDlgPill(node, info, isChecked) {
      // Inspection not yet done → neutral outline.
      if (!info) {
        return { color: 'gray', shape: 'outline' };
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
      const queueColor = isChecked ? 'orange' : 'gray';
      const isFullyAnalyzed = (processed >= total);
      const isPartial = (processed > 0 && processed < total);
      if (isFullyAnalyzed) return { color: queueColor, shape: 'solid' };
      if (isPartial)       return { color: queueColor, shape: 'half' };
      return { color: queueColor, shape: 'outline' };
    }

    function _isOutdatedNode(node) {
      // Reuse the main tree's version-compare helper if available.
      if (typeof isVersionOutdated === 'function') {
        try { return isVersionOutdated(node); } catch (e) { /* fall through */ }
      }
      return false;
    }

    // ── Tree rendering — state pills, rails and a right-side checkbox ──
    //
    // Row layout (mirrors the main tree):
    //   [rails…] [arrow] [pill] [icon] [label] [count] [outdated-badge] [cb-col]
    //
    // ancestorContinueFlags: array of booleans (one per ancestor depth ≥ 1).
    //   Element i = true if the ancestor at depth i+1 has more siblings after
    //   the current branch — the vertical line in its rail column continues.
    //   null = root (no rail drawn).
    // isLastSibling: whether this node is the last of its own siblings.
    //   Determines elbow shape (└─ vs ├─).
    function buildAnalyzeDlgNode(node, ancestorContinueFlags = null, isLastSibling = true) {
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
      if (outdated) cls += ' version-outdated';
      if (noPhotosDeep) cls += ' no-photos-deep';
      row.className = cls;
      row.dataset.path = node.path;
      row.title = outdated
        ? `Analyzed with analysis version ${folderPipelineLabel(node)} (current: ${CURRENT_PIPELINE_MAJOR}.0) — re-analyze to update.\n${node.path}`
        : (folderPipelineNote(node) ? `${folderPipelineNote(node)}
${node.path}` : node.path);

      // ── Rails (mirror the main tree) ─────────────────────────────────────────
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

      // Arrow (expand toggle). Leaf rows get a hidden arrow for alignment.
      const arrow = document.createElement('span');
      arrow.className = 'tree-arrow' + (hasChildren ? (isExpanded ? ' open' : '') : ' leaf');
      arrow.textContent = '▶';

      // State pill (orthogonal color × shape)
      const pillSpec = _computeAnalyzeDlgPill(node, info, isChecked);
      const pill = document.createElement('span');
      pill.className = `state-pill state-pill--${pillSpec.color} state-pill--${pillSpec.shape}`;
      pill.setAttribute('aria-hidden', 'true');

      // Folder icon
      const icon = KestrelIcon.el(node.has_kestrel ? 'folder-open' : 'folder');
      icon.classList.add('tree-icon');

      // Label
      const label = document.createElement('span');
      label.className = 'tree-label';
      label.textContent = node.name;

      // Count text
      const countSpan = document.createElement('span');
      countSpan.className = 'tree-count';
      if (info && info.total > 0) {
        const erroredFrag = info.errored > 0 ? ` · ${info.errored} failed` : '';
        countSpan.textContent = ` ${info.processed}/${info.total}${erroredFrag}`;
      } else {
        countSpan.textContent = '';
      }

      // Outdated badge (orthogonal to the pill).
      let outdatedBadge = null;
      if (outdated) {
        outdatedBadge = document.createElement('span');
        const upgradeMode = analyzeDlgUpgradeModeFor(_adlgNormRoot(node.path));
        outdatedBadge.className = 'adlg-outdated-badge'
          + (isChecked ? (upgradeMode === 'upgrade' ? ' upgrade' : ' warning') : '');
        outdatedBadge.textContent = `v↑ ${folderPipelineLabel(node)}`.trim();
        outdatedBadge.title = upgradeMode
          ? `Analyzed with analysis version ${folderPipelineLabel(node)}; current is ${CURRENT_PIPELINE_MAJOR}.0. Can be upgraded to analysis version 3.0 (keeps your culls) — choose in the queued folders list.`
          : `Analyzed with analysis version ${folderPipelineLabel(node)}; current is ${CURRENT_PIPELINE_MAJOR}.0. Will re-analyze (destructive) if checked.`;
      }

      // ── Right-side checkbox column (mirrors the main tree) ──────────────────
      // Always present (placeholder if no checkbox available) so the right
      // edge stays aligned across all rows.
      const cbCol = document.createElement('span');
      cbCol.className = 'tree-cb-col';
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
        renderAnalyzeDlgTree();
        refreshAnalyzeDlgSummary();
      });
      cbCol.appendChild(cb);

      row.appendChild(arrow);
      row.appendChild(pill);
      row.appendChild(icon);
      row.appendChild(label);
      row.appendChild(countSpan);
      if (outdatedBadge) row.appendChild(outdatedBadge);
      row.appendChild(cbCol);

      // Right-click: clear .kestrel data (preserved from legacy dialog)
      if (node.has_kestrel) {
        row.addEventListener('contextmenu', (e) => {
          e.preventDefault();
          e.stopPropagation();
          showContextMenu(e.clientX, e.clientY, [{
            label: 'Clear Kestrel Analysis Data',
            danger: true,
            action: () => {
              clearKestrelDataForFolder(node.path, node.name, () => {
                node.has_kestrel = false;
                node.pipeline_major = null;
                node.pipeline_version = '';
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
        // Children's ancestor flags = our flags + a new entry at our depth
        // saying "this row continues if I have more siblings after me". Root
        // passes [] (children start fresh).
        const childAncestors = isRoot ? [] : [...ancestorContinueFlags, !isLastSibling];
        const childCount = node.children.length;
        node.children.forEach((child, idx) => {
          const childIsLast = idx === childCount - 1;
          childWrap.appendChild(buildAnalyzeDlgNode(child, childAncestors, childIsLast));
        });
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
        // Click on label/icon also toggles expand (non-destructive).
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
      const emptyHint = document.getElementById('analyzeDlgTreeEmptyHint');
      if (!treeEl) return;
      treeEl.innerHTML = '';
      if (!_adlgHasAnyRoots()) {
        if (wrap) wrap.classList.remove('has-roots');
        if (footer) footer.classList.add('hidden');
        if (emptyHint) emptyHint.style.display = '';
        return;
      }
      if (wrap) wrap.classList.add('has-roots');
      if (footer) footer.classList.remove('hidden');
      if (emptyHint) emptyHint.style.display = 'none';
      for (const root of _adlgGetAllRoots()) {
        treeEl.appendChild(buildAnalyzeDlgNode(root));
      }
    }

    // ── Folder inspection (populates the pill cache) ────────────────────────────
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
      if (paths.length === 0) { _adlgHideProgress(); return; }
      _adlgShowProgress(`Inspecting ${paths.length} folder${paths.length === 1 ? '' : 's'}…`, 60);
      try {
        const res = await window.pywebview.api.inspect_folders(paths);
        if (myVer !== _analyzeDlgInspectionVersion) return; // cancelled
        if (res && res.success && res.results) {
          // inspect_folders keys each result by the exact path we sent, so the
          // direct lookup normally hits. The normalized scan below is a safety
          // net for a backend that keys by another spelling of the same path.
          for (const inputPath of paths) {
            const inputNorm = _adlgNormRoot(inputPath);
            let info = res.results[inputPath];
            if (info === undefined) {
              for (const [resKey, val] of Object.entries(res.results)) {
                if (_adlgNormRoot(resKey) === inputNorm) { info = val; break; }
              }
            }
            if (info !== undefined) analyzeDlgInspectionCache.set(inputNorm, info);
          }
        }
        _adlgShowProgress('Done.', 100);
        renderAnalyzeDlgTree();
        _adlgAutoCheckRootsWithDirectWork();
        renderAnalyzeDlgTree();
        refreshAnalyzeDlgSummary();
      } catch (e) {
        console.warn('[analyzeDlg] inspect_folders failed', e);
      } finally {
        _adlgHideProgress(400);
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

    // ── Recents chips (independent from main tree's folder_recents) ──

    function _adlgBuildRecentsChip(path) {
      const wrap = buildFolderRecentsChip(
        path,
        async () => {
          const chipBtn = wrap.querySelector('.folder-recents-chip');
          if (chipBtn) chipBtn.disabled = true;
          try {
            const r = await addAnalyzeDlgRoot(path);
            if (r && r.added) {
              await _persistAnalyzeRecentsBump(path);
              _renderAnalyzeDlgRecentsChipsSync();
              renderAnalyzeDlgRecentsChips().catch(() => {});
              _inspectAndRenderAnalyzeDlg().catch(() => {});
            }
          } finally {
            const chipBtn = wrap.querySelector('.folder-recents-chip');
            if (chipBtn) chipBtn.disabled = false;
          }
        },
        (p) => {
          if (typeof persistAnalyzeRecentsRemove === 'function') persistAnalyzeRecentsRemove(p);
        },
      );
      return wrap;
    }

    /** Sync pass: hide until async inspect confirms disk + remaining work. */
    function _renderAnalyzeDlgRecentsChipsSync() {
      const row = document.getElementById('analyzeDlgRecentsRow');
      if (!row) return;
      row.classList.add('hidden');
      row.innerHTML = '';
    }

    async function renderAnalyzeDlgRecentsChips() {
      const row = document.getElementById('analyzeDlgRecentsRow');
      if (!row) return;
      const s = (typeof loadSettings === 'function') ? loadSettings() : {};
      const recents = Array.isArray(s.analyze_recents) ? s.analyze_recents.slice(0, 8) : [];
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
              return total > 0 && (processed < total || errored > 0);
            });
          } else if (res && Array.isArray(res.invalid_paths)) {
            const invalid = new Set(res.invalid_paths.map(_adlgNormRoot));
            available = paths.filter(p => !invalid.has(_adlgNormRoot(p)));
          } else {
            available = [];
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
      row.innerHTML = '';
      for (const path of available) row.appendChild(_adlgBuildRecentsChip(path));
      row.classList.remove('hidden');
    }

    async function _persistAnalyzeRecentsBump(path) {
      try {
        const s = loadSettings();
        const existing = Array.isArray(s.analyze_recents) ? s.analyze_recents : [];
        const np = _adlgNormRoot(path);
        const filtered = existing.filter(e => e && _adlgNormRoot(e.path) !== np);
        const ts = new Date().toISOString();
        s.analyze_recents = [{ path: np, timestamp: ts }, ...filtered].slice(0, 8);
        saveSettings(s);
        if (hasPywebviewApi && window.pywebview?.api?.save_settings_data) {
          try { await window.pywebview.api.save_settings_data(s); } catch (_) { }
        }
      } catch (e) { /* best-effort */ }
    }

    // ── Queue summary computation ───────────────────────────────────────────────
    //
    // Reads analyzeDlgCheckedPaths + analyzeDlgInspectionCache. Computes:
    //   - total images to process (skips already-fully-analyzed unless unlock is on)
    //   - total folders
    //   - per-folder list (name + image count + pill)
    //   - estimated time (hardcoded baseline; "—" if too few images)
    //   - warning summary if any folders will lose user data
    //   - shows/hides the "Re-analyze fully-analyzed" unlock row
    // ── Step 3: job list, destination times, footer totals ─────────────────────
    // Classify each checked folder with the shared plan (the Start handler in
    // event-wiring.js uses the same one, so the preview can't disagree).
    function _adlgClassifyChecked(retryErrored) {
      const checkedNorms = Array.from(analyzeDlgCheckedPaths);
      const t = { folders: checkedNorms.length, images: 0, skip: 0, upgrade: 0, loss: 0, unlockable: 0 };
      const perFolder = [];
      // Upgrade eligibility for newly checked folders arrives async and
      // re-renders the summary.
      _adlgEnsureUpgradeStatus(checkedNorms).catch(e => console.warn('[adlg] upgrade status failed', e));
      for (const norm of checkedNorms) {
        const node = _adlgFindNode(norm);
        const info = analyzeDlgInspectionCache.get(norm);
        const total = (info && info.total) || 0;
        const errored = (info && info.errored) || 0;
        const upgradeMode = analyzeDlgUpgradeModeFor(norm);
        const plan = _EST.planFolder({
          total, processed: (info && info.processed) || 0, errored,
          outdated: node ? _isOutdatedNode(node) : false, upgradeMode,
        }, { reanalyzeUnlocked: analyzeDlgReanalyzeUnlocked, retryErrored });
        const all = total.toLocaleString();
        let warningMsg = '';
        let warningKind = '';
        if (plan.kind === 'upgrade') t.upgrade++;
        else if (plan.kind === 'skip') { t.skip++; t.unlockable++; }
        else t.images += plan.images;
        if (plan.kind === 'reanalyze') {
          t.loss++;
          warningKind = 'warn';
          if (plan.reason === 'unlocked') t.unlockable++;
          warningMsg = plan.reason === 'outdated'
            ? `Analyzed with analysis version ${folderPipelineLabel(node)} (current: ${CURRENT_PIPELINE_MAJOR}.0). Kestrel data will be erased and all ${all} photos analyzed again.`
            : `Kestrel data (ratings, decisions, scene names) will be erased and all ${all} photos analyzed again.`;
        } else if (plan.retriedErrored) {
          warningMsg = `Retrying ${errored} failed ${errored === 1 ? 'photo' : 'photos'}.`;
          warningKind = 'error';
        } else if (plan.skippedErrored) {
          warningMsg = `${errored} ${errored === 1 ? 'photo' : 'photos'} failed last time. Turn on "Retry failed photos" in All settings to try again.`;
          warningKind = 'error';
        }
        perFolder.push({
          name: node ? node.name : (norm.split('/').filter(Boolean).pop() || norm),
          path: norm, plan, total, upgradeMode, warningMsg, warningKind,
          pillSpec: _computeAnalyzeDlgPill(node || { path: norm }, info, true),
        });
      }
      return { perFolder, t };
    }

    function _adlgJobRow(f, useGpu) {
      const row = document.createElement('div');
      row.className = 'adlg-job-row' + (f.plan.kind === 'skip' ? ' is-skip' : '');
      row.title = f.path;
      const pill = document.createElement('span');
      pill.className = `state-pill state-pill--${f.pillSpec.color} state-pill--${f.pillSpec.shape}`;
      const name = document.createElement('span');
      name.className = 'adlg-job-name';
      name.textContent = f.name;
      const count = document.createElement('span');
      count.className = 'adlg-job-count';
      const time = document.createElement('span');
      time.className = 'adlg-job-time';
      const n = f.plan.kind === 'upgrade' ? f.total : f.plan.images;
      count.textContent = f.plan.kind === 'skip' ? 'already analyzed'
        : `${n.toLocaleString()} ${n === 1 ? 'photo' : 'photos'}`;
      if (f.plan.kind !== 'skip' && f.plan.kind !== 'upgrade' && !_isCloudDestination()
          && f.plan.images >= _EST_MIN_IMAGES_TO_ESTIMATE) {
        time.textContent = `~${KestrelUtil.formatDuration(f.plan.images * _getEstRate(useGpu).rate)}`;
      }
      row.append(pill, name, count, time);
      const sub = document.createElement('div');
      sub.className = 'adlg-job-sub';
      if (f.plan.kind === 'skip') {
        const skip = document.createElement('span');
        skip.className = 'adlg-job-skip';
        skip.append(KestrelIcon.el('ban'), 'Will be skipped');
        sub.appendChild(skip);
      }
      if (f.upgradeMode) sub.appendChild(_adlgBuildUpgradeChoice(f));
      if (f.warningMsg) {
        const warn = document.createElement('div');
        warn.className = 'adlg-job-warn' + (f.warningKind === 'error' ? ' is-error' : '');
        warn.append(KestrelIcon.el('warning'), ' ' + f.warningMsg);
        sub.appendChild(warn);
      }
      if (sub.childNodes.length) row.appendChild(sub);
      return row;
    }

    // "about 1 h 10 min on your GPU" in the This computer card.
    function _adlgRenderLocalTime(images, useGpu) {
      const el = document.getElementById('adlgLocalTime');
      if (!el) return null;
      el.textContent = '';
      if (images === 0) { el.textContent = 'Nothing to analyze'; return null; }
      if (images < _EST_MIN_IMAGES_TO_ESTIMATE) return null;
      const { rate, source, sampledImgs } = _getEstRate(useGpu);
      const secs = images * rate;
      const big = document.createElement('b');
      big.textContent = `about ${KestrelUtil.formatDuration(secs)}`;
      el.append(big, ` ${useGpu ? 'on your GPU' : 'on the CPU'}`);
      if (source === 'baseline') el.appendChild(_buildUncertainBadge(sampledImgs));
      return secs;
    }

    // Bottleneck chip in the Cloud Compute card; returns the footer estimate.
    function _adlgRenderCloudTime(images) {
      const regimeEl = document.getElementById('adlgCloudRegime');
      if (regimeEl) regimeEl.textContent = '';
      if (images < _EST_MIN_IMAGES_TO_ESTIMATE) return null;
      const { secs, regime, uploadSecs, analysisSecs } = _getCloudEstSeconds(images);
      if (regimeEl) {
        regimeEl.appendChild(_buildCloudUncertainBadge());
        const chip = secs != null ? _buildCloudRegimeChip(regime, uploadSecs, analysisSecs) : null;
        if (chip) regimeEl.appendChild(chip);
      }
      return secs != null ? { secs, bestCase: false } : { secs: analysisSecs, bestCase: true };
    }

    function refreshAnalyzeDlgSummary() {
      const listEl = document.getElementById('analyzeDlgQueuedList');
      if (!listEl) return;
      const useGpu = !!document.getElementById('analyzeUseGpu')?.checked;
      const retryErrored = !!document.getElementById('adlgRetryErrored')?.checked;
      const { perFolder, t } = _adlgClassifyChecked(retryErrored);

      listEl.textContent = '';
      for (const f of perFolder) listEl.appendChild(_adlgJobRow(f, useGpu));

      const localSecs = _adlgRenderLocalTime(t.images, useGpu);
      const cloudEta = _adlgRenderCloudTime(t.images);
      // The cloud card compares the queue with the images left on the plan.
      if (typeof _ccUpdateCloudPricing === 'function') _ccUpdateCloudPricing(t.images);

      const runBtn = document.getElementById('analyzeDlgRunSpeedTest');
      const statusEl = document.getElementById('analyzeDlgCloudSpeedStatus');
      const test = _cloudSpeedTest();
      if (runBtn) {
        runBtn.disabled = t.folders === 0;
        runBtn.replaceChildren(KestrelIcon.el('bolt'), test ? ' Test again' : ' Test my upload speed');
      }
      if (statusEl && test) {
        statusEl.textContent = `Measured upload: ${Number(test.mbps || 0).toFixed(1)} MB/s`;
        statusEl.classList.remove('hidden', 'error');
        statusEl.classList.add('success');
      }

      const warningsEl = document.getElementById('analyzeDlgWarnings');
      if (warningsEl) {
        warningsEl.textContent = '';
        if (t.loss > 0) {
          warningsEl.append(KestrelIcon.el('warning'), ` ${t.loss} ${t.loss === 1 ? 'folder' : 'folders'} will lose ratings, decisions and scene names. You'll be asked to confirm.`);
        }
        warningsEl.classList.toggle('hidden', t.loss === 0);
      }
      document.getElementById('analyzeDlgReanalyzeUnlockRow')?.classList.toggle('hidden', t.unlockable === 0);

      const cloud = _isCloudDestination();
      const eta = cloud
        ? (cloudEta && { secs: cloudEta.secs, cloud: true, bestCase: cloudEta.bestCase })
        : (localSecs != null ? { secs: localSecs, cloud: false, bestCase: false } : null);
      window.KestrelAnalyzeSteps.setSummary({ folders: t.folders, images: t.images, upgrade: t.upgrade, skip: t.skip, eta });
    }

    /** Per-folder "Upgrade to 3.0 / Re-analyze" dropdown for a folder that
     *  was analysed on pipeline 2.x. The upgrade always runs on this
     *  computer (it re-reads the stored crops), even with Cloud Compute as
     *  the destination; only "Re-analyze" goes to the chosen destination. */
    function _adlgBuildUpgradeChoice(f) {
      const wrap = document.createElement('div');
      wrap.className = 'adlg-upgrade-choice';
      const sel = document.createElement('select');
      sel.className = 'adlg-upgrade-mode';
      sel.setAttribute('aria-label', `What to do with ${f.name}`);
      const cloud = _isCloudDestination();
      const opts = [
        ['upgrade', 'Upgrade to analysis version 3.0 (fast, keeps your culls)'],
        ['reanalyze', cloud
          ? 'Analyze again from scratch on Cloud Compute (erases Kestrel data)'
          : 'Analyze again from scratch (erases Kestrel data)'],
      ];
      for (const [value, text] of opts) {
        const o = document.createElement('option');
        o.value = value;
        o.textContent = text;
        sel.appendChild(o);
      }
      sel.value = f.upgradeMode;
      sel.addEventListener('change', () => {
        analyzeDlgUpgradeChoice.set(f.path, sel.value === 'reanalyze' ? 'reanalyze' : 'upgrade');
        renderAnalyzeDlgTree();
        refreshAnalyzeDlgSummary();
      });
      wrap.appendChild(sel);
      if (f.upgradeMode === 'upgrade') {
        const hint = document.createElement('div');
        hint.className = 'adlg-upgrade-hint';
        hint.textContent = 'Re-reads the saved crops to update species IDs, add the sharpness score '
          + 'and smart search. Ratings, decisions and edited tags are kept.'
          + (cloud ? ' Runs on this computer; upgrades don\u2019t use Cloud Compute.' : '');
        wrap.appendChild(hint);
      }
      return wrap;
    }

    // ── Dialog open / settings hydration ────────────────────────────────────────
    // ── Shoot questions ↔ hidden checkboxes ───────────────────────────────────
    // The two "what did you shoot" questions are the visible controls for
    // #analyzeWildlife and #analyzeSpeciesDetection. Those checkboxes stay in
    // the DOM (hidden) because event-wiring.js reads them when Start is
    // clicked, and the cloud-compute snapshot reads them too — mirroring keeps
    // that single read path intact while the user only ever sees the questions.
    //
    // Listeners are attached at load rather than on dialog open: a throw
    // anywhere earlier in the open path would otherwise leave the answers
    // visually live but disconnected, silently analyzing with the wrong scope.
    const _ANSWER_PAIRS = [
      { on: 'adlgSubjectScopeWildlife', off: 'adlgSubjectScopeBirds', box: 'analyzeWildlife' },
      { on: 'adlgSpeciesScopeYes', off: 'adlgSpeciesScopeNo', box: 'analyzeSpeciesDetection' },
    ];

    // Push the checkbox state out to the radios (settings → UI).
    function syncAnalyzeAnswersFromSettings() {
      for (const { on, off, box } of _ANSWER_PAIRS) {
        const onEl = document.getElementById(on);
        const offEl = document.getElementById(off);
        const boxEl = document.getElementById(box);
        if (!onEl || !offEl || !boxEl) continue;
        onEl.checked = !!boxEl.checked;
        offEl.checked = !boxEl.checked;
      }
    }

    // Attach the radios → checkbox mirror once (UI → settings).
    function wireAnalyzeAnswerMirrors() {
      for (const { on, off, box } of _ANSWER_PAIRS) {
        const onEl = document.getElementById(on);
        const offEl = document.getElementById(off);
        const boxEl = document.getElementById(box);
        if (!onEl || !offEl || !boxEl || onEl.dataset.wiredMirror) continue;
        onEl.dataset.wiredMirror = '1';
        const sync = () => { boxEl.checked = !!onEl.checked; };
        onEl.addEventListener('change', sync);
        offEl.addEventListener('change', sync);
      }
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', wireAnalyzeAnswerMirrors, { once: true });
    } else {
      wireAnalyzeAnswerMirrors();
    }

    // ── Species geofence (per queued folder) ─────────────────────────────────
    // "Where were these photos taken?" picks the species list BioCLIP chooses
    // from for the folders queued by this Start. Options: the user's default
    // regions (Settings → Species & Region, ``bird_regions``), one region,
    // several regions (checkbox grid), or Global. Reset to the default on every
    // open so a one-off trip abroad doesn't silently stick.
    function _adlgRegionLabel(code) {
      const meta = (typeof _birdCatalogMeta !== 'undefined' && _birdCatalogMeta) || null;
      const hit = meta && Array.isArray(meta.regions) ? meta.regions.find(r => r.code === code) : null;
      return hit ? hit.label : code;
    }

    function _adlgDefaultRegions() {
      try {
        if (typeof _getCurrentBirdRegions === 'function') return _getCurrentBirdRegions();
      } catch (_) { /* fall through */ }
      const v = getSetting('bird_regions', null);
      return (Array.isArray(v) && v.length) ? v : ['NA'];
    }

    function _adlgFmtCount(n) {
      return (typeof n === 'number' && n > 0) ? n.toLocaleString() : '';
    }

    function _adlgRenderSpeciesRegionPicker() {
      const sel = document.getElementById('adlgSpeciesRegionMode');
      const custom = document.getElementById('adlgSpeciesRegionCustom');
      if (!sel || !custom) return;
      const meta = (typeof _birdCatalogMeta !== 'undefined' && _birdCatalogMeta) || { regions: [], region_counts: {}, total_species: 0 };
      const counts = meta.region_counts || {};
      // "Worldwide" on its own only means cosmopolitan species; it is always
      // included in any region's list, so it is not offered as a choice.
      const regions = (Array.isArray(meta.regions) ? meta.regions : []).filter(r => r && r.code && r.code !== 'Worldwide');
      const defaults = _adlgDefaultRegions();
      const defaultLabel = defaults.map(_adlgRegionLabel).join(', ') || 'North America';
      let html = `<option value="default">My default regions (${escapeHtml(defaultLabel)})</option>`;
      if (regions.length) {
        html += '<optgroup label="One region">';
        for (const r of regions) {
          const c = _adlgFmtCount(counts[r.code]);
          html += `<option value="region:${escapeHtml(r.code)}">${escapeHtml(r.label)}${c ? ` — ${c} species` : ''}</option>`;
        }
        html += '</optgroup>';
        html += '<option value="custom">Several regions…</option>';
      }
      const total = _adlgFmtCount(meta.total_species);
      html += `<option value="global">Global — all world birds${total ? ` (${total})` : ''}</option>`;
      sel.innerHTML = html;
      sel.value = 'default';
      const defaultSet = new Set(defaults);
      custom.innerHTML = regions.map(r =>
        `<label title="${escapeHtml(r.label)}"><input type="checkbox" class="adlg-species-region-cb" data-region-code="${escapeHtml(r.code)}" ${defaultSet.has(r.code) ? 'checked' : ''} />` +
        `<span>${escapeHtml(r.label)}</span></label>`
      ).join('');
      _adlgSyncSpeciesRegionUi();
    }

    function _adlgSyncSpeciesRegionUi() {
      const sel = document.getElementById('adlgSpeciesRegionMode');
      const custom = document.getElementById('adlgSpeciesRegionCustom');
      const block = document.getElementById('adlgSpeciesRegionBlock');
      const speciesOn = document.getElementById('analyzeSpeciesDetection')?.checked ?? true;
      if (block) block.classList.toggle('hidden', !speciesOn);
      if (custom && sel) custom.classList.toggle('hidden', sel.value !== 'custom');
    }

    /** Species regions for the folders being queued: list of region codes,
     *  or ['global']. Never empty (falls back to the default regions). */
    function getAnalyzeDlgSpeciesRegions() {
      const sel = document.getElementById('adlgSpeciesRegionMode');
      const mode = sel ? String(sel.value || 'default') : 'default';
      if (mode === 'global') return ['global'];
      if (mode.startsWith('region:')) return [mode.slice('region:'.length)];
      if (mode === 'custom') {
        const picked = Array.from(document.querySelectorAll('#adlgSpeciesRegionCustom input.adlg-species-region-cb'))
          .filter(cb => cb.checked).map(cb => cb.dataset.regionCode).filter(Boolean);
        if (picked.length) return picked;
      }
      return _adlgDefaultRegions().slice();
    }

    function _adlgWireSpeciesRegionPicker() {
      const sel = document.getElementById('adlgSpeciesRegionMode');
      if (sel && !sel.dataset.wired) {
        sel.dataset.wired = '1';
        sel.addEventListener('change', _adlgSyncSpeciesRegionUi);
      }
      for (const id of ['adlgSpeciesScopeYes', 'adlgSpeciesScopeNo']) {
        const el = document.getElementById(id);
        if (el && !el.dataset.wiredRegion) {
          el.dataset.wiredRegion = '1';
          // Runs after the mirror listener (attached at load), so the hidden
          // checkbox already reflects the new answer.
          el.addEventListener('change', _adlgSyncSpeciesRegionUi);
        }
      }
    }

    async function openAnalyzeDialog() {
      if (!hasPywebviewApi) {
        await KestrelDialog.notice('desktopOnlyQueue');
        return;
      }
      // Detection model now lives as a plain <select> in More options →
      // Finding subjects, defaulting to Accurate. Anything that isn't an
      // explicit 'fast' resolves to 'accurate'.
      const _modelSelect = document.getElementById('adlgWildlifeModelMode');
      const savedMode = String(getSetting('wildlife_model_mode', 'accurate') || 'accurate').toLowerCase();
      const modeFinal = (savedMode === 'fast') ? 'fast' : 'accurate';
      if (_modelSelect) _modelSelect.value = modeFinal;

      // Hydrate "More options" settings from persisted values (same keys as before).
      // Default 0.15 now matches the suggested-baseline message shown by the
      // inline warning below — modern wildlife detectors are well-calibrated
      // for low thresholds.
      const _adlgDt = document.getElementById('adlgDetectionThreshold');
      if (_adlgDt) _adlgDt.value = getSetting('detection_threshold', 0.15);
      const _adlgMbc = document.getElementById('adlgMaxBirdCrops');
      if (_adlgMbc) _adlgMbc.value = getSetting('max_bird_crops', 10);
      const _adlgEq = document.getElementById('adlgExposureQuality');
      if (_adlgEq) {
        const savedEq = String(getSetting('exposure_quality', 'balanced') || 'balanced').toLowerCase();
        _adlgEq.value = ['lenient', 'balanced', 'aggressive'].includes(savedEq) ? savedEq : 'balanced';
      }
      const _adlgSt = document.getElementById('adlgSceneTime');
      if (_adlgSt) _adlgSt.value = getSetting('scene_time_threshold', 1.0);
      const _adlgSbg = document.getElementById('adlgSceneBreakGap');
      if (_adlgSbg) _adlgSbg.value = getSetting('scene_break_gap_seconds', 0);
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

      // Hydrate wildlife/species-detection toggles from persisted settings so
      // the user's choice survives dialog re-opens and is visible to the cloud
      // compute snapshot path (settings.js getSetting reads localStorage).
      const _adlgWildlife = document.getElementById('analyzeWildlife');
      if (_adlgWildlife) _adlgWildlife.checked = !!getSetting('wildlife_enabled', true);
      const _adlgSpecies = document.getElementById('analyzeSpeciesDetection');
      if (_adlgSpecies) _adlgSpecies.checked = getSetting('species_detection_enabled') !== false;

      // Point the visible answers at the freshly-hydrated checkbox state.
      syncAnalyzeAnswersFromSettings();

      // Species geofence picker: paint synchronously from whatever catalog
      // meta is cached, then repaint once it has loaded (cached after the
      // first call, so this is normally instant).
      _adlgWireSpeciesRegionPicker();
      _adlgRenderSpeciesRegionPicker();
      if (typeof loadSpeciesFamilyMap === 'function') {
        loadSpeciesFamilyMap().then(() => {
          const sel = document.getElementById('adlgSpeciesRegionMode');
          // Don't clobber a choice the user already made.
          if (!sel || sel.value === 'default') _adlgRenderSpeciesRegionPicker();
        }).catch(() => {});
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
      // Wire GPU toggle (live time-estimate refresh).
      const _gpuBox = document.getElementById('analyzeUseGpu');
      if (_gpuBox && !_gpuBox.dataset.wiredSummary) {
        _gpuBox.dataset.wiredSummary = '1';
        _gpuBox.addEventListener('change', refreshAnalyzeDlgSummary);
      }
      // Retry changes which photos each folder will analyze.
      const _retryBox = document.getElementById('adlgRetryErrored');
      if (_retryBox && !_retryBox.dataset.wiredSummary) {
        _retryBox.dataset.wiredSummary = '1';
        _retryBox.addEventListener('change', refreshAnalyzeDlgSummary);
      }

      // Detection confidence warning — wildlife detection models are quite
      // accurate, so high thresholds tend to miss real subjects. Show a
      // suggestion banner when the user sets a value above 0.25.
      const _dtBox = document.getElementById('adlgDetectionThreshold');
      const _dtWarn = document.getElementById('adlgDetectionConfidenceWarning');
      function _syncDetectionConfidenceWarning() {
        if (!_dtBox || !_dtWarn) return;
        const v = parseFloat(_dtBox.value);
        const show = Number.isFinite(v) && v > 0.25;
        _dtWarn.classList.toggle('hidden', !show);
      }
      if (_dtBox && !_dtBox.dataset.wiredWarn) {
        _dtBox.dataset.wiredWarn = '1';
        _dtBox.addEventListener('input', _syncDetectionConfidenceWarning);
        _dtBox.addEventListener('change', _syncDetectionConfidenceWarning);
      }
      _syncDetectionConfidenceWarning();

      // Upgrade eligibility can change between opens (an upgrade finished,
      // a folder was re-analysed), and the upgrade-vs-re-analyze choice
      // resets to the upgrade like the unlock box above.
      analyzeDlgUpgradeStatus.clear();
      analyzeDlgUpgradeChoice.clear();

      // Sync render passes — fast, no IPC. These paint a usable dialog
      // immediately. Background tasks below refine the data when ready.
      _renderAnalyzeDlgRecentsChipsSync();
      renderAnalyzeDlgTree();
      window.KestrelAnalyzeSteps.reset();
      refreshAnalyzeDlgSummary();

      // Open the dialog NOW. Everything below is background work that
      // updates the UI in-place when results arrive.
      document.getElementById('analyzeQueueDlg').showModal();

      // Async refinement (fire-and-forget): inspect_folders on recents to
      // filter out invalid/already-done paths; on roots to populate per-folder
      // counts. _ccResetDestinationOnDialogOpen wires the cloud card + auth gate.
      renderAnalyzeDlgRecentsChips().catch(e => console.warn('[adlg] recents refresh failed', e));
      if (_adlgHasAnyRoots()) {
        _inspectAndRenderAnalyzeDlg().catch(e => console.warn('[adlg] inspect failed', e));
      }
      if (typeof _ccResetDestinationOnDialogOpen === 'function') {
        _ccResetDestinationOnDialogOpen().catch(e => console.warn('[cc] reset failed', e));
      }
    }

