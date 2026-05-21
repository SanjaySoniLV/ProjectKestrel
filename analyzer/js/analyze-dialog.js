    // ── Analyze Folders Dialog (Phase 3 — completely independent state) ──────────
    //
    // The dialog tree no longer reads or writes the main sidebar tree's state
    // (folderTreeRootNodes / folderTreeRootOrder / checkedFolderPaths). It has
    // its own Map, its own checked set, its own expanded set, its own recents
    // settings key (analyze_recents). The two trees never share state. This
    // makes "what am I viewing" (main) and "what am I queueing for analysis"
    // (dialog) two cleanly-separable mental models.

    // ── Time estimation ────────────────────────────────────────────────────────
    // Baseline rates for fresh installs (release-tunable). Local samples from
    // the queue worker (perf_samples_gpu / perf_samples_cpu in settings.json)
    // override these once the per-mode total crosses _EST_MIN_SAMPLED_IMAGES_FOR_LOCAL.
    const _EST_BASELINE_SECS_PER_IMG_GPU = 4.0;
    const _EST_BASELINE_SECS_PER_IMG_CPU = 10.0;
    const _EST_MIN_IMAGES_TO_ESTIMATE = 50;       // min queued imgs before any estimate is shown
    const _EST_MIN_SAMPLED_IMAGES_FOR_LOCAL = 100; // switchover threshold for local rate

    // Returns { rate, source, sampledImgs } where source is 'local' or 'baseline'.
    function _getEstRate(useGpu) {
      try {
        const key = useGpu ? 'perf_samples_gpu' : 'perf_samples_cpu';
        const samples = (typeof getSetting === 'function' ? getSetting(key, []) : []) || [];
        let totalImgs = 0, totalSecs = 0;
        for (const s of samples) {
          const i = Number(s && s.imgs);
          const t = Number(s && s.secs);
          if (i > 0 && t > 0) { totalImgs += i; totalSecs += t; }
        }
        if (totalImgs >= _EST_MIN_SAMPLED_IMAGES_FOR_LOCAL) {
          return { rate: totalSecs / totalImgs, source: 'local', sampledImgs: totalImgs };
        }
        return {
          rate: useGpu ? _EST_BASELINE_SECS_PER_IMG_GPU : _EST_BASELINE_SECS_PER_IMG_CPU,
          source: 'baseline',
          sampledImgs: totalImgs,
        };
      } catch (_e) {
        return {
          rate: useGpu ? _EST_BASELINE_SECS_PER_IMG_GPU : _EST_BASELINE_SECS_PER_IMG_CPU,
          source: 'baseline',
          sampledImgs: 0,
        };
      }
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

    // Cloud destination est. time:
    //   max(upload_seconds, analysis_floor_seconds)
    // where analysis_floor = images × 0.5s (sustained 2 imgs/sec on Modal),
    // and upload_seconds is computed from the speed-test result if present.
    // Returns null when destination=cloud and no speed test has run yet.
    const _CLOUD_ANALYSIS_FLOOR_SECS_PER_IMG = 0.5; // 2 imgs/sec sustained

    function _getCloudEstSeconds(totalImages) {
      const result = (typeof _cloudSpeedTestResult !== 'undefined') ? _cloudSpeedTestResult : null;
      const analysisSecs = totalImages * _CLOUD_ANALYSIS_FLOOR_SECS_PER_IMG;
      if (!result || !(result.mbps > 0) || !(result.samples_uploaded > 0)) {
        return { secs: null, analysisSecs, uploadSecs: null, hasTest: false };
      }
      const avgBytes = Number(result.total_bytes || 0) / Number(result.samples_uploaded || 1);
      if (!(avgBytes > 0)) return { secs: null, analysisSecs, uploadSecs: null, hasTest: true };
      const totalMB = (totalImages * avgBytes) / 1_048_576;
      const uploadSecs = totalMB / Number(result.mbps);
      return {
        secs: Math.max(analysisSecs, uploadSecs),
        analysisSecs,
        uploadSecs,
        hasTest: true,
      };
    }

    // Always-on "?" badge for cloud est. time. Phrasing differs from the local
    // baseline badge — the cloud "?" stays permanent because the estimate is
    // beta and depends on transfer speed + Modal worker availability.
    function _buildCloudUncertainBadge() {
      const tip =
        'Cloud Compute is in beta. Estimates use the higher of (a) projected ' +
        'upload time and (b) a 2-images-per-second analysis floor. Actual time ' +
        'depends on transfer speed and Modal worker availability.';
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

    // Helpers for the load-progress bar — shared between addAnalyzeDlgRoot
    // (the slow list_subfolders call) and _inspectAndRenderAnalyzeDlg (the
    // inspect_folders pass). Surfaces a single progress bar that walks from
    // "Scanning folder structure…" through "Inspecting N folders…" so the
    // user never sees a dead UI gap after clicking + Add Folders.
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
      // so the user never sees a dead-UI gap after clicking + Add Folders.
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

    // ── State pill computation (Phase 3F polish v2) ─────────────────────────────
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

    // ── Tree rendering — uses state pills + Phase 1.5 rails + right-side cb ──
    //
    // Row layout (Phase 3 polish to mirror the main tree):
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
        ? `Analyzed on Kestrel v${node.kestrel_version} (current: v${_appVersion}) — re-analyze to update.\n${node.path}`
        : node.path;

      // ── Rails (Phase 1.5B mirror) ────────────────────────────────────────────
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

      // State pill (orthogonal color × shape — Phase 3F)
      const pillSpec = _computeAnalyzeDlgPill(node, info, isChecked);
      const pill = document.createElement('span');
      pill.className = `state-pill state-pill--${pillSpec.color} state-pill--${pillSpec.shape}`;
      pill.setAttribute('aria-hidden', 'true');

      // Folder icon
      const icon = document.createElement('span');
      icon.className = 'tree-icon';
      icon.textContent = node.has_kestrel ? '📂' : '📁';

      // Label
      const label = document.createElement('span');
      label.className = 'tree-label';
      label.textContent = node.name;

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

      // ── Right-side checkbox column (Phase 1.5A mirror) ──────────────────────
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
            label: '🗑 Clear Kestrel Analysis Data',
            danger: true,
            action: () => {
              clearKestrelDataForFolder(node.path, node.name, () => {
                node.has_kestrel = false;
                node.kestrel_version = '';
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
      if (paths.length === 0) { _adlgHideProgress(); return; }
      _adlgShowProgress(`Inspecting ${paths.length} folder${paths.length === 1 ? '' : 's'}…`, 60);
      try {
        const res = await window.pywebview.api.inspect_folders(paths);
        if (myVer !== _analyzeDlgInspectionVersion) return; // cancelled
        if (res && res.success && res.results) {
          // inspect_folders keys results by realpath; we key the cache by
          // normalized-input path. Build a lookup that matches both.
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
      const perFolder = [];
      for (const norm of checkedNorms) {
        const node = _adlgFindNode(norm);
        const info = analyzeDlgInspectionCache.get(norm);
        const name = node ? node.name : (norm.split('/').filter(Boolean).pop() || norm);
        const total = (info && info.total) || 0;
        const processed = (info && info.processed) || 0;
        const errored = (info && info.errored) || 0;
        const outdated = node ? _isOutdatedNode(node) : false;
        const isFullyAnalyzed = total > 0 && processed >= total && errored === 0;
        let willSkip = false;
        let willReanalyze = false;
        let warningMsg = '';
        let warningKind = ''; // '' | 'warn' | 'error'
        let imagesThisFolder = 0;
        if (outdated) {
          willReanalyze = true;
          outdatedReanalyzeCount++;
          imagesThisFolder = total;
          warningMsg = `Analyzed on Kestrel v${node.kestrel_version} (current: v${_appVersion}). Will erase .kestrel data and re-analyze all ${total.toLocaleString()} images.`;
          warningKind = 'warn';
        } else if (isFullyAnalyzed) {
          if (analyzeDlgReanalyzeUnlocked) {
            willReanalyze = true;
            fullyAnalyzedWillReanalyzeCount++;
            imagesThisFolder = total;
            warningMsg = `Already fully analyzed. Will erase user data (ratings, decisions, scene names) and re-analyze all ${total.toLocaleString()} images.`;
            warningKind = 'warn';
          } else {
            willSkip = true;
            fullyAnalyzedSkipCount++;
          }
        } else if (errored > 0 && retryErrored) {
          imagesThisFolder = errored;
          warningMsg = `Re-attempting ${errored} previously-errored image${errored === 1 ? '' : 's'}.`;
          warningKind = 'error';
        } else if (errored > 0 && !retryErrored) {
          imagesThisFolder = Math.max(0, total - processed - errored);
          warningMsg = `${errored} image${errored === 1 ? '' : 's'} previously errored — enable "Re-attempt errored images" to retry them.`;
          warningKind = 'error';
        } else {
          imagesThisFolder = Math.max(0, total - processed);
        }
        if (!willSkip) totalImagesToProcess += imagesThisFolder;
        const pillSpec = _computeAnalyzeDlgPill(node || { path: norm }, info, true);
        perFolder.push({ name, path: norm, info, pillSpec, willSkip, willReanalyze, imagesThisFolder, warningMsg, warningKind });
      }

      // Per-folder list
      listEl.innerHTML = '';
      if (perFolder.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'analyze-dlg-queued-list-empty';
        empty.textContent = 'No folders queued yet. Check folders in the queue builder to add them here.';
        listEl.appendChild(empty);
      } else {
        for (const f of perFolder) {
          const item = document.createElement('div');
          let itemCls = 'analyze-dlg-queued-item';
          if (f.willSkip) itemCls += ' will-skip';
          if (f.warningKind === 'warn') itemCls += ' has-warning';
          if (f.warningKind === 'error') itemCls += ' has-error';
          item.className = itemCls;
          item.title = f.path;

          const head = document.createElement('div');
          head.className = 'analyze-dlg-queued-item-head';
          const pill = document.createElement('span');
          pill.className = `state-pill state-pill--${f.pillSpec.color} state-pill--${f.pillSpec.shape}`;
          const name = document.createElement('span');
          name.className = 'analyze-dlg-queued-item-name';
          name.textContent = f.name;
          head.appendChild(pill);
          head.appendChild(name);
          item.appendChild(head);

          const meta = document.createElement('div');
          meta.className = 'analyze-dlg-queued-item-meta';
          if (f.willSkip) {
            const skip = document.createElement('span');
            skip.className = 'meta-skip';
            skip.textContent = 'Already analyzed — will be skipped';
            meta.appendChild(skip);
          } else {
            const imgSpan = document.createElement('span');
            imgSpan.className = 'meta-images';
            const imgs = f.imagesThisFolder.toLocaleString();
            imgSpan.textContent = `${imgs} image${f.imagesThisFolder === 1 ? '' : 's'}`;
            if (f.willReanalyze) imgSpan.textContent += ' (re-analyze)';
            meta.appendChild(imgSpan);
            // Per-folder time estimate
            if (f.imagesThisFolder >= _EST_MIN_IMAGES_TO_ESTIMATE) {
              if (_isCloudDestination()) {
                const { secs } = _getCloudEstSeconds(f.imagesThisFolder);
                if (secs != null) {
                  const timeSpan = document.createElement('span');
                  timeSpan.className = 'meta-time';
                  timeSpan.textContent = `· ~${_formatDuration(secs)}`;
                  meta.appendChild(timeSpan);
                  meta.appendChild(_buildCloudUncertainBadge());
                }
                // Pre-speed-test cloud: no per-folder estimate; the panel
                // below the headline carries the "?" + run-test affordance.
              } else {
                const { rate, source, sampledImgs } = _getEstRate(useGpu);
                const sec = f.imagesThisFolder * rate;
                const timeSpan = document.createElement('span');
                timeSpan.className = 'meta-time';
                timeSpan.textContent = `· ~${_formatDuration(sec)}`;
                meta.appendChild(timeSpan);
                if (source === 'baseline') meta.appendChild(_buildUncertainBadge(sampledImgs));
              }
            }
          }
          item.appendChild(meta);

          if (f.warningMsg) {
            const warn = document.createElement('div');
            warn.className = 'analyze-dlg-queued-item-warning' + (f.warningKind === 'error' ? ' error' : '');
            warn.textContent = (f.warningKind === 'error' ? '⚠ ' : '⚠ ') + f.warningMsg;
            item.appendChild(warn);
          }
          listEl.appendChild(item);
        }
      }

      // Headline — horizontal 3-stat layout (images | folders | est. time).
      // Each stat is a number-on-top, label-below block separated by thin
      // vertical dividers. Reads at a glance without parsing a sentence.
      if (checkedCount === 0) {
        headline.innerHTML = '<span class="analyze-dlg-summary-empty">Check folders in the queue builder to add them.</span>';
      } else {
        const imgCount = totalImagesToProcess.toLocaleString();
        const folderWord = checkedCount === 1 ? 'folder' : 'folders';
        const imgLabel = totalImagesToProcess === 1 ? 'image' : 'images';
        const skipFrag = fullyAnalyzedSkipCount > 0
          ? `<span class="analyze-dlg-stat-skip">${fullyAnalyzedSkipCount} skipped</span>`
          : '';
        // Time stat — destination-dependent.
        //   Local: existing baseline-or-local logic with optional "?" badge.
        //   Cloud: "?" until a speed test has run, then max(upload, 2-img/sec
        //          floor) with a permanent "?" + beta affordance underneath.
        let timeValue = '—';
        let timeLabel = 'estimated time';
        let timeDim = true;
        let timeBadgeKind = null; // null | 'local-baseline' | 'cloud'
        let timeBadgeSampledImgs = 0;
        const isCloud = _isCloudDestination();
        if (isCloud) {
          if (totalImagesToProcess >= _EST_MIN_IMAGES_TO_ESTIMATE) {
            const { secs, hasTest } = _getCloudEstSeconds(totalImagesToProcess);
            if (secs != null) {
              timeValue = `~${_formatDuration(secs)}`;
              timeLabel = 'est. time (cloud · beta)';
              timeDim = false;
              timeBadgeKind = 'cloud';
            } else {
              // Cloud destination, no speed test yet — show "?" prominently
              // and let the panel below host the Run Speed Test button.
              timeValue = '?';
              timeLabel = 'est. time (cloud · beta)';
              timeDim = false; // we want the "?" to read clearly, not muted
              timeBadgeKind = null; // value IS the "?"; no separate badge
            }
          }
        } else if (totalImagesToProcess >= _EST_MIN_IMAGES_TO_ESTIMATE) {
          const { rate, source, sampledImgs } = _getEstRate(useGpu);
          const seconds = totalImagesToProcess * rate;
          timeValue = `~${_formatDuration(seconds)}`;
          timeLabel = `est. time (${useGpu ? 'GPU' : 'CPU'}${source === 'local' ? ' · local' : ''})`;
          timeDim = false;
          if (source === 'baseline') {
            timeBadgeKind = 'local-baseline';
            timeBadgeSampledImgs = sampledImgs;
          }
        }
        headline.innerHTML =
          `<div class="analyze-dlg-stat">` +
            `<div class="analyze-dlg-stat-value">${imgCount}</div>` +
            `<div class="analyze-dlg-stat-label">${imgLabel}</div>` +
          `</div>` +
          `<div class="analyze-dlg-stat">` +
            `<div class="analyze-dlg-stat-value">${checkedCount}</div>` +
            `<div class="analyze-dlg-stat-label">${folderWord}</div>` +
            skipFrag +
          `</div>` +
          `<div class="analyze-dlg-stat">` +
            `<div class="analyze-dlg-stat-value${timeDim ? ' dim' : ''}" id="_analyzeDlgTimeStatValue">${timeValue}</div>` +
            `<div class="analyze-dlg-stat-label">${timeLabel}</div>` +
          `</div>`;
        if (timeBadgeKind === 'local-baseline') {
          const valueEl = headline.querySelector('#_analyzeDlgTimeStatValue');
          if (valueEl) valueEl.appendChild(_buildUncertainBadge(timeBadgeSampledImgs));
        } else if (timeBadgeKind === 'cloud') {
          const valueEl = headline.querySelector('#_analyzeDlgTimeStatValue');
          if (valueEl) valueEl.appendChild(_buildCloudUncertainBadge());
        }
      }

      // Toggle the cloud-only affordances panel under the headline. Speed-test
      // button is enabled iff at least one folder is checked. When a speed test
      // has already run, the button reads "Re-run upload speed test" and the
      // status line surfaces the measured mbps for transparency.
      const cloudPanel = document.getElementById('analyzeDlgCloudEstPanel');
      if (cloudPanel) {
        const showCloudPanel = _isCloudDestination();
        cloudPanel.classList.toggle('hidden', !showCloudPanel);
        if (showCloudPanel) {
          const runBtn = document.getElementById('analyzeDlgRunSpeedTest');
          const statusEl = document.getElementById('analyzeDlgCloudSpeedStatus');
          const hasResult = !!(typeof _cloudSpeedTestResult !== 'undefined' && _cloudSpeedTestResult);
          if (runBtn) {
            runBtn.disabled = checkedCount === 0;
            const labelText = hasResult ? ' Re-run upload speed test' : ' Run Upload Speed Test';
            // Keep the leading lightning icon span intact.
            const iconHtml = '<span class="adlg-cloud-speed-btn-icon">⚡</span>';
            runBtn.innerHTML = iconHtml + labelText;
          }
          if (statusEl) {
            if (hasResult) {
              const mbps = Number(_cloudSpeedTestResult.mbps || 0).toFixed(1);
              statusEl.textContent = `Measured upload: ${mbps} MB/s`;
              statusEl.classList.remove('hidden', 'error');
              statusEl.classList.add('success');
            } else {
              statusEl.classList.add('hidden');
            }
          }
        }
      }

      // Aggregate warning row (above the unlock checkbox)
      const reanalyzeWithUnlockCount = fullyAnalyzedWillReanalyzeCount + outdatedReanalyzeCount;
      if (reanalyzeWithUnlockCount > 0) {
        const word = reanalyzeWithUnlockCount === 1 ? 'folder' : 'folders';
        warningsEl.innerHTML = `⚠ ${reanalyzeWithUnlockCount} ${word} will lose user data on re-analysis (ratings, decisions, scene names).`;
        warningsEl.classList.remove('hidden');
      } else {
        warningsEl.classList.add('hidden');
        warningsEl.innerHTML = '';
      }

      // Unlock row visible only when fully-analyzed-current-version folder(s) are checked.
      const showUnlockRow = fullyAnalyzedSkipCount > 0 || fullyAnalyzedWillReanalyzeCount > 0;
      if (unlockRow) unlockRow.classList.toggle('hidden', !showUnlockRow);

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
      // Fetch the app version once if not already cached — needed for the
      // outdated-version pill badges (isVersionOutdated returns false when
      // _appVersion is empty, so badges silently never render). The main-tree
      // addFolderRoot also fetches this; we duplicate the guard here because
      // the user can open the analyze dialog before touching the main tree.
      if (!_appVersion && window.pywebview?.api?.get_app_version) {
        try {
          const vr = await window.pywebview.api.get_app_version();
          if (vr && vr.success) _appVersion = vr.version || '';
        } catch (e) { /* ignore — badges just won't show */ }
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
      // Wire GPU toggle (live time-estimate refresh).
      const _gpuBox = document.getElementById('analyzeUseGpu');
      if (_gpuBox && !_gpuBox.dataset.wiredSummary) {
        _gpuBox.dataset.wiredSummary = '1';
        _gpuBox.addEventListener('change', refreshAnalyzeDlgSummary);
      }
      // Retry-errored has TWO instances (one in critical settings, one in
      // More options). Keep them in sync so users see consistent state.
      // Both refresh the queue summary (errored counts change per-folder).
      const _retryBoxCritical = document.getElementById('adlgRetryErrored');
      const _retryBoxMore = document.getElementById('adlgRetryErroredMore');
      function _syncRetryBoxes(src) {
        const checked = !!src.checked;
        if (_retryBoxCritical && _retryBoxCritical !== src) _retryBoxCritical.checked = checked;
        if (_retryBoxMore && _retryBoxMore !== src) _retryBoxMore.checked = checked;
        refreshAnalyzeDlgSummary();
      }
      if (_retryBoxCritical && !_retryBoxCritical.dataset.wiredSummary) {
        _retryBoxCritical.dataset.wiredSummary = '1';
        _retryBoxCritical.addEventListener('change', () => _syncRetryBoxes(_retryBoxCritical));
      }
      if (_retryBoxMore && !_retryBoxMore.dataset.wiredSummary) {
        _retryBoxMore.dataset.wiredSummary = '1';
        _retryBoxMore.addEventListener('change', () => _syncRetryBoxes(_retryBoxMore));
      }
      // Mirror initial state in case settings hydration only touched one.
      if (_retryBoxCritical && _retryBoxMore) {
        _retryBoxMore.checked = _retryBoxCritical.checked;
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
      // Wire the Local/Cloud destination cards + reset destination state.
      // _ccWireDestinationDialog is idempotent (sentinel on the Local card),
      // so safe to call on every open. Without this the cards are inert.
      if (typeof _ccResetDestinationOnDialogOpen === 'function') {
        try { await _ccResetDestinationOnDialogOpen(); } catch (e) { console.warn('[cc] reset failed', e); }
      }
      document.getElementById('analyzeQueueDlg').showModal();
    }

