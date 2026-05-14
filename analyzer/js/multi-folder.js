
    // Helper function to load folder using native path (for pywebview API)
    // Loads a single folder. For multi-folder loading, see loadMultipleFolders().

    // Auto-load: fires after a short debounce whenever checkboxes change.
    // If nothing is checked, clears the view gracefully.
    const debouncedAutoLoad = debounce(async () => {
      await _cleanupUncheckedFolderCaches();
      if (checkedFolderPaths.size > 0) {
        await loadMultipleFolders(Array.from(checkedFolderPaths));
      } else {
        ++_loadFoldersVersion; // cancel any in-progress load
        rows = []; header = []; scenes = [];
        sceneGrid.innerHTML = '';
        setStatus('No folders selected — check folders in the tree to load scenes');
      }
    }, 400);

    // Collect all kestrel paths from the tree (recursively) for check-all
    function collectKestrelPaths(node, out = []) {
      if (!node) return out;
      if (node.has_kestrel) out.push(node.path);
      (node.children || []).forEach(c => collectKestrelPaths(c, out));
      return out;
    }

    function checkAllTreeFolders() {
      const all = collectKestrelPaths(folderTreeRootNode);
      all.forEach(p => checkedFolderPaths.add(p));
      renderFolderTree();
      debouncedAutoLoad();
    }

    function checkNoneTreeFolders() {
      checkedFolderPaths.clear();
      renderFolderTree();
      debouncedAutoLoad();
    }

    // Progress bar helpers
    function showProgress(label, pct) {
      const row = document.getElementById('loadProgressRow');
      const lbl = document.getElementById('loadProgressLabel');
      const fill = document.getElementById('loadProgressFill');
      if (row) row.classList.remove('hidden');
      if (lbl) lbl.textContent = label;
      if (fill) fill.style.width = Math.round(Math.max(0, Math.min(100, pct))) + '%';
    }
    function hideProgress() {
      const row = document.getElementById('loadProgressRow');
      if (row) row.classList.add('hidden');
    }

    async function loadMultipleFolders(paths) {
      if (!paths || paths.length === 0) return;
      const myVer = ++_loadFoldersVersion;
      blobUrlCache.clear();
      _sceneActiveCropIndexByImage.clear();
      rows = [];
      header = [];
      let loadedCount = 0;
      let slot = 0;
      const total = paths.length;
      showProgress(`Loading 0 / ${total} folders…`, 0);
      for (let i = 0; i < paths.length; i++) {
        if (myVer !== _loadFoldersVersion) { hideProgress(); return; }
        const p = paths[i];
        const folderName = p.replace(/.*[/\\]/, '');
        showProgress(`Loading ${i + 1} / ${total}: ${folderName}`, (i / total) * 90);
        try {
          const result = await window.pywebview.api.read_kestrel_csv(p);
          if (myVer !== _loadFoldersVersion) { hideProgress(); return; }
          if (!result.success) continue;
          const parsed = parseCsvText(result.data);
          const newRows = parsed.data || [];
          const newFields = parsed.meta.fields || [];
          for (const f of newFields) if (!header.includes(f)) header.push(f);
          const root = result.root || p;
          const currentSlot = slot++;
          for (const r of newRows) { r.__rootPath = root; r.__folderSlot = currentSlot; }
          rows = rows.concat(newRows);
          // Load scenedata for this folder
          if (hasPywebviewApi && window.pywebview?.api?.read_kestrel_scenedata) {
            try {
              const sdRes = await window.pywebview.api.read_kestrel_scenedata(root);
              if (sdRes?.success) _scenedata[root] = sdRes.data;
            } catch (_) {}
          }
          // Apply normalization (in-memory: sets r.__normalized_rating)
          if (hasPywebviewApi && window.pywebview?.api?.apply_normalization) {
            try {
              const normRes = await window.pywebview.api.apply_normalization(root);
              if (normRes?.success && normRes?.normalized_ratings) {
                const mapping = normRes.normalized_ratings;
                for (const r of newRows) {
                  if (r.filename in mapping) r.__normalized_rating = mapping[r.filename];
                }
              }
            } catch (_) {}
          }
          loadedCount++;
        } catch (e) {
          console.warn('[multi] Failed to load', p, e);
        }
      }
      if (myVer !== _loadFoldersVersion) { hideProgress(); return; }
      if (loadedCount === 0) { hideProgress(); setStatus('No folders could be loaded'); return; }
      showProgress(`Building scenes from ${loadedCount} folder${loadedCount === 1 ? '' : 's'}…`, 95);
      // For single-folder image-loading compat: set rootPath to first loaded root.
      // Per-row __rootPath handles multi-folder image loading in getBlobUrlForPath.
      const firstRow = rows.find(r => r.__rootPath);
      if (firstRow) rootPath = firstRow.__rootPath;
      ensureSceneNameColumn();
      ensureRatingColumns();
      _clearDirtyRoots();
      _setDirtyUi(false);
      takeSnapshot();
      const mergeBtn = document.getElementById('openMerge');
      if (mergeBtn) mergeBtn.disabled = true;
      treeActivePath = paths.length === 1 ? paths[0] : null;
      renderFolderTree();
      await renderScenes();
      showProgress('Done', 100);
      await sleep(400);
      hideProgress();
      const label = loadedCount === 1 ? paths[0].replace(/.*[/\\]/, '') : `${loadedCount} folders`;
      setStatus(`Loaded ${label} — ${rows.length} images`);
    }

    async function loadFolderFromPath(folderPath) {
      if (!folderPath) return;

      try {
        // Use pywebview API to read the CSV file
        const result = await window.pywebview.api.read_kestrel_csv(folderPath);

        if (!result.success) {
          throw new Error(result.error || 'Failed to read CSV');
        }

        // Parse the CSV data
        const parsed = parseCsvText(result.data);
        header = parsed.meta.fields || [];
        const loadedRoot = result.root || folderPath;
        rows = (parsed.data || []).map(r => ({ ...r, __rootPath: loadedRoot, __folderSlot: 0 }));
        _sceneActiveCropIndexByImage.clear();
        
        // Load scenedata for this folder
        if (hasPywebviewApi && window.pywebview?.api?.read_kestrel_scenedata) {
          try {
            const sdRes = await window.pywebview.api.read_kestrel_scenedata(loadedRoot);
            if (sdRes?.success) _scenedata[loadedRoot] = sdRes.data;
          } catch (_) {}
        }
        
        // Apply normalization (in-memory: sets r.__normalized_rating)
        if (hasPywebviewApi && window.pywebview?.api?.apply_normalization) {
          try {
            const normRes = await window.pywebview.api.apply_normalization(loadedRoot);
            if (normRes?.success && normRes?.normalized_ratings) {
              const mapping = normRes.normalized_ratings;
              for (const r of rows) {
                if (r.filename in mapping) r.__normalized_rating = mapping[r.filename];
              }
            }
          } catch (_) {}
        }
        
        ensureSceneNameColumn();
        ensureRatingColumns();
        blobUrlCache.clear(); // new folder — clear stale cache entries

        // IMPORTANT: Set rootPath BEFORE renderScenes so image loading works
        rootPath = loadedRoot;
        _clearDirtyRoots();
        _setDirtyUi(false);
        takeSnapshot();

        // Now render with rootPath set
        await renderScenes();

        // Also save in settings for file opening (use rootHint for consistency)
        const settings = loadSettings();
        settings.rootHint = rootPath;
        saveSettings(settings);

        setStatus(`Loaded from: ${result.path}`);
        const mergeBtn = document.getElementById('openMerge');
        if (mergeBtn) mergeBtn.disabled = true; // Can't save in pywebview mode

        // Update active selection in tree if tree is open
        if (folderTreeData) {
          const loadedPath = result.root || folderPath;
          treeActivePath = loadedPath;
          checkedFolderPaths.clear();
          checkedFolderPaths.add(loadedPath);
          _checkedFolderPathSnapshot = _snapshotCheckedFolderPathMap();
          renderFolderTree();
        }
      } catch (e) {
        const errorMsg = (e.message || String(e)).replace(/^Error: /, '');
        // If the folder tree is already visible the user may have clicked a parent folder
        // intentionally (no .kestrel there). Show a soft status message instead of an alert.
        if (folderTreeData) {
          setStatus(`No Kestrel database in this folder — select one that shows 📂 in the tree`);
        } else {
          alert(`Could not load Kestrel database from this folder.\n\nMake sure:\n1. The folder has been analyzed with Kestrel Analyzer\n2. The .kestrel folder exists (it may be hidden on macOS)\n3. You selected the correct folder\n\nTip: On macOS, .kestrel folders are hidden by default. You can:\n• Press Cmd+Shift+. (period) to show hidden files in Finder\n• Or select the parent folder that contains the .kestrel folder\n\nError: ${errorMsg}`);
          setStatus('Failed to load database');
        }
      }
    }

    // Event wiring
    el('#pickFolder').addEventListener('click', async () => {
      kdebug('[pickFolder] clicked');
      kdebug('[pickFolder] hasPywebviewApi:', hasPywebviewApi);
      kdebug('[pickFolder] window.pywebview:', window.pywebview);
      kdebug('[pickFolder] window.pywebview?.api:', window.pywebview?.api);

      // Wait for pywebview API if it's not ready yet
      if (!hasPywebviewApi) {
        kdebug('[pickFolder] Waiting for pywebview API...');
        const ready = await waitForPywebview();
        kdebug('[pickFolder] Pywebview API ready:', ready);
      }
      // When user opens a folder, reset any checked folders in the main tree
      // (acts like pressing "Check none") so we don't accidentally load
      // folders from a previous root selection.
      try {
        checkedFolderPaths.clear();
        renderFolderTree();
        debouncedAutoLoad(); // Unload current folder (same as Check None)
      } catch (e) { /* ignore */ }

      try {
        // PRIORITY 1: Python API (desktop app - all platforms)
        // When available, ALWAYS use this for consistency
        if (hasPywebviewApi && window.pywebview?.api?.choose_directory) {
          kdebug('[pickFolder] Using Python API for folder picker');
          try {
            setStatus('Opening folder picker...');
            const folderPath = await window.pywebview.api.choose_directory();
            if (folderPath) {
              // Scan the selected folder as the tree root (user may have picked a parent
              // folder with multiple analyzed sub-folders, or a leaf folder directly).
              treeExpandedPaths.clear();
              const treeScanned = await scanFolderTree(folderPath);
              // Use the root_has_kestrel flag returned directly by scanFolderTree
              // (folderTreeRootHasKestrel is set inside scanFolderTree).
              // Only attempt CSV load if the root itself is an analyzed folder.
              if (treeScanned && !folderTreeRootHasKestrel) {
                // Tree scan succeeded but root has no .kestrel — it's a parent folder.
                setStatus('Select a folder from the tree below to load its scenes');
              } else {
                // Either scan wasn't available, or the root itself has .kestrel — load it.
                await loadFolderFromPath(folderPath);
              }
              return; // Success - Python API handled everything
            } else {
              setStatus('Folder selection cancelled');
              return; // User cancelled
            }
          } catch (e) {
            console.error('Python API folder picker failed:', e);
            alert(`Desktop folder picker failed: ${e.message || e}\n\nPlease restart the application and try again.`);
            setStatus('Folder picker failed');
            return; // Don't fall through - Python API should always work in desktop app
          }
        }

        setStatus('Folder picker unavailable: Desktop API missing.');
      } catch (e) {
        console.error('Unexpected error in pickFolder:', e);
        setStatus('An unexpected error occurred');
      }
    });

    el('#saveCsv').addEventListener('click', saveCsv);
    el('#search').addEventListener('input', debounce(() => renderScenes(), 250));
    el('#speciesConf').addEventListener('change', () => renderScenes());
    el('#sortBy').addEventListener('change', () => {
      const s = loadSettings();
      s.sortBy = el('#sortBy').value;
      saveSettings(s);
      renderScenes();
    });

