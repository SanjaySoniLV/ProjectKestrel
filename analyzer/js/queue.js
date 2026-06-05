    // ── Analysis Queue Panel / Polling ───────────────────────────────────────────

    let _queuePollingTimer = null;
    let _queuePanelExpanded = true;
    let _queueLastDoneSet = new Set(); // track newly-done folders to auto-refresh tree
    let _queueLastRunningSet = new Set(); // track newly-running folders to update tree
    let _queueLastInProgressSet = new Set(); // pending + running (auto-load when tree empty)
    let _tempKestrelPaths = new Set(); // transiently-marked paths to prevent flicker
    let _analyticsConsentPending = false; // guard against showing consent dialog multiple times
    let _queueCountsTimer = null; // interval for updating folder counts from queue
    
    // In-progress folder tracking and auto-refresh for live updates
    let _inProgressFolderPaths = new Set(); // folders with pending/running status
    let _autoRefreshTimers = new Map(); // path -> intervalId for auto-refresh listeners
    let _inProgressFoldersCheckedCount = 0; // count of in-progress folders that are checked
    
    // Session state for ETA calculations: track baseline state from folder inspection
    let _queueSessionStartState = new Map(); // path -> { initialProcessed: int, totalImages: int, toAnalyze: int }
    let _queueFolderInspections = new Map(); // path -> full inspection data from inspect_folder/inspect_folders
    // ETA smoothing: exponential moving average to prevent wild per-image swings
    let _etaSmoothed = null;   // smoothed secs/image
    let _etaLastPath = null;   // reset EMA when folder changes
    const _thumbCache = new Map();    // relPath+'|'+rootPath → dataUrl (avoids reload flash)
    let _liveAnalysisDlgOpen = false;
    let _liveLastThumbKey = '';
    let _liveLastOverlayKey = '';
    let _liveLastCropKeys = [];
    const CONF_HIGH = 0.75;
    const CONF_LOW = 0.30;

    /** Call the backend queue API (desktop pywebview mode only). */
    async function apiStartQueue(paths, useGpu = true, wildlifeEnabled = true, retryErrored = false, speciesDetectionEnabled = true) {
      if (!window.pywebview?.api?.start_analysis_queue) {
        throw new Error('Desktop API unavailable: start_analysis_queue');
      }
      return window.pywebview.api.start_analysis_queue(JSON.stringify(paths), useGpu, wildlifeEnabled, retryErrored, speciesDetectionEnabled);
    }

    async function apiQueueControl(action) {
      if (!window.pywebview?.api) {
        throw new Error('Desktop API unavailable: queue control');
      }
      const fn = { pause: 'pause_analysis_queue', resume: 'resume_analysis_queue', cancel: 'cancel_analysis_queue', clear: 'clear_queue_done' }[action];
      if (!fn || typeof window.pywebview.api[fn] !== 'function') {
        throw new Error(`Desktop API missing queue action: ${action}`);
      }
      return window.pywebview.api[fn]();
    }

    async function apiGetQueueStatus() {
      if (!window.pywebview?.api?.get_queue_status) {
        throw new Error('Desktop API unavailable: get_queue_status');
      }
      return window.pywebview.api.get_queue_status();
    }

    async function apiGetRecoveryStatus() {
      if (!window.pywebview?.api?.get_recovery_status) {
        throw new Error('Desktop API unavailable: get_recovery_status');
      }
      return window.pywebview.api.get_recovery_status();
    }

    // Phase 3: apiRestoreRecoveryQueue removed — queue-restore feature
    // replaced by the analyze dialog's analyze_recents chip row.

    async function apiClearRecoveryState(clearQueueState = true) {
      if (!window.pywebview?.api?.clear_recovery_state) {
        throw new Error('Desktop API unavailable: clear_recovery_state');
      }
      return window.pywebview.api.clear_recovery_state(!!clearQueueState);
    }

    async function apiSendRecoveryCrashReport() {
      if (!window.pywebview?.api?.send_recovery_crash_report) {
        throw new Error('Desktop API unavailable: send_recovery_crash_report');
      }
      return window.pywebview.api.send_recovery_crash_report();
    }

    let _startupRecoveryHandled = false;

    // Phase 3: queue-restore branch removed. This function now only handles
    // the unclean-shutdown crash-report prompt. Interrupted queues now
    // surface as recents in the Analyze Folders dialog (analyze_recents
    // settings key) instead of a startup modal.
    async function maybeHandleStartupRecovery() {
      if (_startupRecoveryHandled) return;
      _startupRecoveryHandled = true;

      if (!hasPywebviewApi) { const ready = await waitForPywebview(); if (!ready) return; }

      let recovery = null;
      try {
        recovery = await apiGetRecoveryStatus();
      } catch (_) {
        return;
      }
      if (!recovery || recovery.success === false) return;

      const exitReason = String(recovery.exit_reason || '').toLowerCase();
      // 'os_shutdown' (PC reboot/logoff) and 'clean' never warrant a dialog.
      // 'crash' is a real unhandled exception. 'unknown' is ambiguous
      // (SIGKILL, power loss, or pre-upgrade install) and gets a soft prompt.
      const hadUncleanShutdown = !!recovery.unclean_shutdown
        && (exitReason === 'crash' || exitReason === 'unknown' || exitReason === '');
      if (!hadUncleanShutdown) return;

      const promptText = exitReason === 'crash'
        ? 'Kestrel detected that the previous session crashed.\n\nSend a crash report now?'
        : 'Kestrel did not exit cleanly. This is sometimes caused by a system shutdown or power loss — would you still like to send a report?';
      const sendReport = confirm(promptText);
      if (sendReport) {
        try {
          const reportResult = await apiSendRecoveryCrashReport();
          if (reportResult && reportResult.success === false) {
            alert('Could not send crash report:\n\n' + (reportResult.error || 'Unknown error'));
          } else {
            showToast('Crash report sent. Thank you.', 3500);
          }
        } catch (e) {
          alert('Could not send crash report:\n\n' + (e.message || e));
        }
      }

      try {
        // Clear only the unclean-shutdown timestamp; we no longer persist
        // queue recovery state so the `false` arg is a no-op there.
        await apiClearRecoveryState(false);
      } catch (_) { }
    }

    /** Format a duration in seconds to a readable string like "2m 30s". */
    function formatDuration(secs) {
      if (!isFinite(secs) || secs < 0) return '–';
      secs = Math.round(secs);
      if (secs < 60) return secs + 's';
      const m = Math.floor(secs / 60), s = secs % 60;
      if (m < 60) return m + 'm ' + (s > 0 ? s + 's' : '');
      const h = Math.floor(m / 60), rm = m % 60;
      return h + 'h ' + (rm > 0 ? rm + 'm' : '');
    }

    /** Render the queue panel from a status object. */
    function renderQueuePanel(status) {
      window._lastQueueStatus = status; // store for analyze dialog queue preview
      const panel = document.getElementById('queuePanel');
      const badge = document.getElementById('queuePanelBadge');
      const body = document.getElementById('queuePanelBody');
      const controls = document.getElementById('queuePanelControls');
      const pauseBtn = document.getElementById('queuePauseBtn');
      const overallEtaEl = document.getElementById('queueOverallEta');
      if (!panel || !badge || !body) return;

      const items = status.items || [];
      const running = !!status.running;
      const paused = !!status.paused;
      const hasItems = items.length > 0;

      // While the queue is running, keep a short-lived poll to update folder rows
      if (running) startQueueCountsPoll(); else stopQueueCountsPoll();

      if (!hasItems && !running) {
        panel.classList.add('hidden');
        if (overallEtaEl) overallEtaEl.classList.add('hidden');
        stopPollingQueue();
        return;
      }

      panel.classList.remove('hidden');

      // Badge
      const runningItems = items.filter(i => i.status === 'running');
      const pendingItems = items.filter(i => i.status === 'pending');
      const doneItems = items.filter(i => i.status === 'done');
      if (paused) {
        badge.textContent = 'Paused'; badge.className = 'queue-panel-badge paused';
      } else if (running) {
        const cur = runningItems[0];
        if (cur && cur.total > 0) {
          badge.textContent = `${cur.processed} / ${cur.total}`; badge.className = 'queue-panel-badge';
        } else {
          badge.textContent = `${pendingItems.length + runningItems.length} pending`; badge.className = 'queue-panel-badge';
        }
      } else if (doneItems.length === items.length && items.length > 0) {
        badge.textContent = 'Done'; badge.className = 'queue-panel-badge done';
      } else {
        badge.textContent = `${pendingItems.length} pending`; badge.className = 'queue-panel-badge';
      }

      // Pause/resume button label
      if (pauseBtn) pauseBtn.textContent = paused ? '▶ Resume' : '⏸ Pause';

      if (!_queuePanelExpanded) { body.classList.add('hidden'); if (controls) controls.classList.add('hidden'); return; }
      body.classList.remove('hidden'); if (controls) controls.classList.remove('hidden');

      // ETA computation: secs/image from running item using TRUE baseline from folder inspection
      const cur = runningItems[0];
      let secsPerImage = null;
      // inspectionReady: inspection data exists for the running folder.
      // Until it arrives, suppress ETA entirely (show "Calculating ETA…") so the early
      // incorrect progress_cb(alreadyDone, total) call cannot produce a near-zero ETA.
      const normCurPath = normalizePath(cur?.path);
      const inspectionReady = cur && _queueSessionStartState.has(normCurPath);
      if (cur && inspectionReady && cur.elapsed_seconds > 0) {
        const sess = _queueSessionStartState.get(normCurPath);
        const initialProcessed = sess.initialProcessed || 0;
        const processedThisSession = Math.max(0, (cur.processed || 0) - initialProcessed);
        if (processedThisSession > 0) {
          const rawSecsPerImage = cur.elapsed_seconds / processedThisSession;
          // Reset EMA if we moved to a different folder (compare normalized paths)
          if (_etaLastPath !== normCurPath) { _etaSmoothed = null; _etaLastPath = normCurPath; }
          // Exponential moving average (α=0.15) — smooths per-image jitter without
          // lagging too far behind the true rate
          const alpha = 0.15;
          _etaSmoothed = _etaSmoothed === null ? rawSecsPerImage : alpha * rawSecsPerImage + (1 - alpha) * _etaSmoothed;
          secsPerImage = _etaSmoothed;
        }
      }

      // Show loading message if models are being loaded (early in run)
      if (running && cur) {
        const overallEl = overallEtaEl;
        const loadingMsg = (cur.current_status_msg || '').toLowerCase().includes('load');
        if (loadingMsg || (cur.processed === 0 && cur.current_status_msg)) {
          if (overallEl) { overallEl.textContent = `⏳ ${cur.current_status_msg || 'Loading analyzer... please wait'}`; overallEl.classList.remove('hidden'); }
          try { showLoadingAnalyzer(); } catch (e) { }
        } else {
          try { hideLoadingAnalyzer(); } catch (e) { }
        }
      }

      // Overall ETA: aggregate remaining images across queue using inspection data for accuracy
      if (overallEtaEl && running && cur) {
        if (!inspectionReady) {
          // Inspection data still in flight — show placeholder so user isn't misled
          overallEtaEl.textContent = '⏳ Calculating ETA…';
          overallEtaEl.classList.remove('hidden');
        } else if (secsPerImage !== null) {
          let totalRemaining = 0;
          for (const item of items) {
            const sess = _queueSessionStartState.get(item.path);
            if (item.status === 'running' && item.total > 0) {
              const remaining = Math.max(0, (item.total || 0) - (item.processed || 0));
              totalRemaining += secsPerImage * remaining;
            } else if (item.status === 'pending') {
              const toAnalyze = sess && typeof sess.toAnalyze === 'number' ? sess.toAnalyze : 200;
              totalRemaining += secsPerImage * toAnalyze;
            }
          }
          if (totalRemaining > 5) {
            overallEtaEl.textContent = `⏱ Overall est. remaining: ${formatDuration(totalRemaining)}`;
            overallEtaEl.classList.remove('hidden');
          } else {
            overallEtaEl.classList.add('hidden');
          }
        } else {
          overallEtaEl.classList.add('hidden');
        }
      } else if (overallEtaEl) {
        overallEtaEl.classList.add('hidden');
      }

      // Queue items
      const frag = document.createDocumentFragment();
      for (const item of items) {
        const div = document.createElement('div');
        const isDone = item.status === 'done';
        const isAlreadyAnalyzed = isDone &&
          (item.current_status_msg || '').toLowerCase().includes('no new files');
        div.className = 'queue-item' + (isDone || item.status === 'cancelled' ? ' done-item' : '');

        // Header row: name + status badge
        const hdr = document.createElement('div');
        hdr.className = 'queue-item-header';
        const nameEl = document.createElement('span');
        nameEl.className = 'queue-item-name';
        nameEl.textContent = item.name;
        nameEl.title = item.path;
        const statusEl = document.createElement('span');
        statusEl.className = `queue-item-status ${item.status}`;
        const labels = {
          pending: '⏳ In Queue',
          running: '⚙ Analyzing',
          done: isAlreadyAnalyzed ? '✓ Already analyzed' : '✓ Done',
          error: '✗ Error',
          cancelled: '— Cancelled',
        };
        statusEl.textContent = labels[item.status] || item.status;
        if (item.status === 'error' && item.error) statusEl.title = item.error;
        hdr.appendChild(nameEl); hdr.appendChild(statusEl);
        // "Load" affordance: jump to this folder's results in the gallery.
        // Shown once a folder has analyzable output (done) or is producing it
        // (running) — loading mid-run is safe; new scenes stream in additively.
        if (item.path && (item.status === 'done' || item.status === 'running')) {
          const loadBtn = document.createElement('button');
          loadBtn.className = 'queue-item-load-btn';
          loadBtn.type = 'button';
          loadBtn.textContent = '📂 Load';
          loadBtn.title = 'Open this folder to browse results';
          loadBtn.addEventListener('click', (ev) => {
            ev.stopPropagation();
            if (typeof loadFolderIntoBrowser === 'function') loadFolderIntoBrowser(item.path);
          });
          hdr.appendChild(loadBtn);
        }
        div.appendChild(hdr);

        // Progress bar
        if (item.status === 'running' && item.total > 0) {
          const prog = document.createElement('div'); prog.className = 'queue-item-progress';
          const fill = document.createElement('div'); fill.className = 'queue-item-progress-fill';
          fill.style.width = Math.round((item.processed / item.total) * 100) + '%';
          prog.appendChild(fill); div.appendChild(prog);

          // ETA / paused row
          {
            const etaEl = document.createElement('div');
            etaEl.className = 'queue-item-eta';
            if (item.is_paused) {
              etaEl.textContent = `${item.processed} / ${item.total} — ⏸ Paused`;
            } else if (!inspectionReady) {
              etaEl.textContent = `${item.processed} / ${item.total} — ⏳ Calculating ETA…`;
            } else if (secsPerImage !== null && item.total > item.processed) {
              const remaining = secsPerImage * (item.total - item.processed);
              etaEl.textContent = `${item.processed} / ${item.total} — est. ${formatDuration(remaining)} left`;
            } else {
              etaEl.textContent = `${item.processed} / ${item.total}`;
            }
            div.appendChild(etaEl);
          }

          // Current filename
          if (item.current_filename) {
            const fileEl = document.createElement('div');
            fileEl.className = 'queue-item-file';
            fileEl.textContent = item.current_filename;
            div.appendChild(fileEl);
          }

          // Live preview thumbnail (async load deferred after DOM insert)
          if (item.current_export_path && hasPywebviewApi) {
            const preview = document.createElement('div');
            preview.className = 'queue-live-preview';
            const thumb = document.createElement('img');
            thumb.className = 'queue-live-thumb';
            thumb.alt = '';
            // Store paths as data attributes; loaded after DOM insert
            thumb.dataset.thumbRel = item.current_export_path;
            thumb.dataset.thumbRoot = item.path;
            preview.appendChild(thumb);
            div.appendChild(preview);
          }
        } else if (item.status === 'done' && item.total > 0) {
          const prog = document.createElement('div'); prog.className = 'queue-item-progress';
          const fill = document.createElement('div'); fill.className = 'queue-item-progress-fill';
          fill.style.width = '100%'; fill.style.background = '#2ecc71';
          prog.appendChild(fill); div.appendChild(prog);
        }

        frag.appendChild(div);
      }
      body.innerHTML = '';
      body.appendChild(frag);

      // Async: load thumbnails for any img[data-thumb-rel] elements (cache avoids reload flash)
      body.querySelectorAll('img[data-thumb-rel]').forEach(async img => {
        try {
          const rel = img.dataset.thumbRel || '';
          const root = img.dataset.thumbRoot || '';
          const key = rel + '|' + root;
          const isLive = rel.indexOf('__live_') >= 0;
          if (!isLive && _thumbCache.has(key)) { img.src = _thumbCache.get(key); return; }
          const result = await window.pywebview.api.read_image_file(rel, root);
          if (result && result.success && result.data) {
            const url = _base64ToBlobUrl(result.data, result.mime);
            if (!isLive) _thumbCache.set(key, url);
            img.src = url;
          }
        } catch (_) { }
      });

      // Check if any folder newly finished — refresh tree + auto-reload CSV data
      const nowDone = new Set(items.filter(i => i.status === 'done').map(i => i.path));
      let treeRescanNeeded = false;
      for (const p of nowDone) {
        if (!_queueLastDoneSet.has(p)) {
          treeRescanNeeded = true;
          scheduleAutoRefresh(p);
          // First-time folder completion → offer analytics consent if not yet asked
          if (!getSetting('analytics_consent_shown', false)) showAnalyticsConsentDialog();
        }
      }
      if (treeRescanNeeded) {
        // Rescan only the specific root(s) whose folders just finished, not
        // every loaded root. nowDone is the set of just-completed folder
        // paths — find which root contains each, then rescan that root.
        setTimeout(() => {
          const rootsToRescan = new Set();
          for (const p of nowDone) {
            const root = _findRootContaining(p);
            if (root) rootsToRescan.add(root.path);
          }
          for (const rp of rootsToRescan) rescanFolderRoot(rp);
        }, 1200);
        // Newly-finished folders become home-page recents (fully analyzed).
        for (const p of nowDone) {
          if (!_queueLastDoneSet.has(p) && typeof persistFolderRecentsBump === 'function') {
            persistFolderRecentsBump(p);
          }
        }
      }
      _queueLastDoneSet = nowDone;

      // Update in-progress folder tracking and UI (pending + running folders)
      try {
        const norm = p => (p || '').replace(/\\/g, '/');
        const inProgressNow = new Set();
        for (const item of items) {
          if (item.status === 'pending' || item.status === 'running') {
            inProgressNow.add(norm(item.path));
          }
        }
        
        const runningNow = new Set(items.filter(i => i.status === 'running').map(i => norm(i.path)));
        const inProgressRawPaths = {};
        items.filter(i => i.status === 'pending' || i.status === 'running').forEach(i => {
          inProgressRawPaths[norm(i.path)] = i.path;
        });
        for (const p of inProgressNow) {
          if (!_queueLastInProgressSet.has(p) && typeof autoLoadFolderWhenTreeEmpty === 'function') {
            autoLoadFolderWhenTreeEmpty(inProgressRawPaths[p] || p);
          }
        }
        _queueLastInProgressSet = new Set(inProgressNow);
        const prevRunningSet = _queueLastRunningSet;
        _queueLastRunningSet = runningNow;
        
        // Update in-progress set and refresh tree styling
        _inProgressFolderPaths = inProgressNow;
        updateInProgressFoldersInTree();
        _updateAutoRefreshTimers();
        
        // Newly-starting items: update the main folder tree after 500ms delay
        for (const p of inProgressNow) {
          if (!prevRunningSet.has(p)) {
            setTimeout(() => {
              try {
                if (!_hasAnyRoots()) return;
                updateFolderTreeNode(p);
              } catch (e) { /* ignore */ }
            }, 500);
          }
        }
      } catch (e) { console.warn('[queue] in-progress tracking error:', e); }

      // Remove auto-refresh timers for finished folders
      try {
        const norm = p => (p || '').replace(/\\/g, '/');
        const nowDone = new Set(items.filter(i => i.status === 'done').map(i => norm(i.path)));
        for (const p of nowDone) {
          if (_autoRefreshTimers.has(p)) {
            clearInterval(_autoRefreshTimers.get(p));
            _autoRefreshTimers.delete(p);
          }
        }
      } catch (e) { console.warn('[timer] cleanup error:', e); }

      // Update live dialog if open
      if (_liveAnalysisDlgOpen) {
        const runningItem = items.find(i => i.status === 'running') || null;
        updateLiveAnalysisDlg(runningItem || items[items.length - 1] || null);
      }
    }

    // Normalize paths consistently: strip trailing slashes
    function normalizePath(p) {
      if (!p) return '';
      let pp = String(p).trim();
      while (pp && pp[pp.length - 1] in {'\\': 1, '/': 1}) pp = pp.slice(0, -1);
      return pp;
    }

    function startPollingQueue() {
      if (_queuePollingTimer) return;
      startAutoRefresh();

      // Initialize session state by inspecting all folders in the queue
      // This gives us TRUE baselines for accurate ETA calculations
      (async () => {
        try {
          const status = await apiGetQueueStatus();
          if (status && status.items && status.items.length > 0) {
            // Batch-inspect all folders to get true processed/total counts
            const paths = status.items.map(item => item.path);
            if (hasPywebviewApi && window.pywebview?.api?.inspect_folders) {
              try {
                const inspectRes = await window.pywebview.api.inspect_folders(paths);
                if (inspectRes && inspectRes.success && inspectRes.results) {
                  for (const [path, info] of Object.entries(inspectRes.results)) {
                    if (info) {
                      const normPath = normalizePath(path);
                      _queueFolderInspections.set(normPath, info);
                      const initialProcessed = info.processed || 0;
                      const totalImages = info.total || 0;
                      const toAnalyze = Math.max(0, totalImages - initialProcessed);
                      _queueSessionStartState.set(normPath, {
                        initialProcessed,
                        totalImages,
                        toAnalyze
                      });
                    }
                  }
                }
              } catch (e) { /* ignore */ }
            }
          }
        } catch (e) { /* ignore */ }
      })();

      // Poll more frequently to reflect per-image progress (500ms)
      _queuePollingTimer = setInterval(async () => {
        try {
          const status = await apiGetQueueStatus();
          renderQueuePanel(status);
          // Update auto-refresh timers based on pause state
          _updateAutoRefreshTimers();

          // When new items appear, inspect and capture their baseline state
          if (status && status.items) {
            const newPaths = [];
            for (const item of status.items) {
              const normPath = normalizePath(item.path);
              if (!_queueSessionStartState.has(normPath)) {
                newPaths.push(item.path);
              }
            }
            if (newPaths.length > 0 && hasPywebviewApi && window.pywebview?.api?.inspect_folders) {
              try {
                const inspectRes = await window.pywebview.api.inspect_folders(newPaths);
                if (inspectRes && inspectRes.success && inspectRes.results) {
                  for (const [path, info] of Object.entries(inspectRes.results)) {
                    if (info) {
                      const normPath = normalizePath(path);
                      _queueFolderInspections.set(normPath, info);
                      const initialProcessed = info.processed || 0;
                      const totalImages = info.total || 0;
                      const toAnalyze = Math.max(0, totalImages - initialProcessed);
                      _queueSessionStartState.set(normPath, {
                        initialProcessed,
                        totalImages,
                        toAnalyze
                      });
                    }
                  }
                }
              } catch (e) { /* ignore */ }
            }
          }

          if (!status.running && (status.items || []).every(i => i.status !== 'pending' && i.status !== 'running')) {
            stopPollingQueue();
          }
        } catch (_) { }
      }, 500);
    }

    function stopPollingQueue() {
      if (_queuePollingTimer) { clearInterval(_queuePollingTimer); _queuePollingTimer = null; }
      stopAutoRefresh();
      // Cleanup auto-refresh timers for in-progress folders
      for (const timerId of _autoRefreshTimers.values()) {
        clearInterval(timerId);
      }
      _autoRefreshTimers.clear();
      _inProgressFolderPaths.clear();
      // Cleanup session state
      _queueSessionStartState.clear();
      _queueFolderInspections.clear();
      _etaSmoothed = null;
      _etaLastPath = null;
    }

    // Poll the queue status frequently and update folder rows in the ANALYZE DIALOG ONLY
    // with the running item's processed/total. This keeps per-folder counts live
    // while analysis is in progress.
    function startQueueCountsPoll() {
      if (_queueCountsTimer) return;
      _queueCountsTimer = setInterval(async () => {
        try {
          const status = await apiGetQueueStatus();
          if (!status || !status.items) return;
          const items = status.items;
          // Normalize helper
          const norm = p => (p || '').replace(/\\/g, '/');
          // Update ONLY the Analyze dialog tree rows (not the main folder tree)
          const rows = Array.from(document.querySelectorAll('#analyzeDlgTree .adlg-node-row'));
          for (const it of items) {
            const ip = norm(it.path);
            const related = rows.filter(r => norm(r.dataset.path) === ip);
            for (const row of related) {
              const span = row.querySelector('.tree-count');
              if (span) {
                if (it.total && it.total > 0) span.textContent = ` ${it.processed}/${it.total}`;
                else span.textContent = '';
              }
              // Update analysis classes (partial/full/none) - only for analyze dialog
              row.classList.remove('analyzed-full', 'analyzed-partial', 'analyzed-none');
              if (it.total && it.total > 0) {
                if ((it.processed || 0) === 0) row.classList.add('analyzed-none');
                else if ((it.processed || 0) >= it.total) row.classList.add('analyzed-full');
                else row.classList.add('analyzed-partial');
              }
            }
          }
          // Stop polling if queue no longer running
          if (!status.running) stopQueueCountsPoll();
        } catch (_) { }
      }, 500);
    }

    function stopQueueCountsPoll() {
      if (_queueCountsTimer) { clearInterval(_queueCountsTimer); _queueCountsTimer = null; }
    }

    // ── Live Analysis Details dialog ──────────────────────────────────────────

    function openLiveAnalysisDlg() {
      _liveAnalysisDlgOpen = true;
      document.getElementById('liveAnalysisDlg').showModal();
    }

    /**
     * Load an image by relative path + root into an <img> element, using _thumbCache.
     * Only issues a network/IPC call on cache miss.
     */
    async function _loadImg(imgEl, relPath, rootPath) {
      if (!relPath || !rootPath || !hasPywebviewApi) return;
      const key = relPath + '|' + rootPath;
      const isLive = String(relPath).indexOf('__live_') >= 0;
      if (!isLive && _thumbCache.has(key)) { imgEl.src = _thumbCache.get(key); return; }
      try {
        const r = await window.pywebview.api.read_image_file(relPath, rootPath);
        if (r && r.success && r.data) {
          const url = _base64ToBlobUrl(r.data, r.mime);
          if (!isLive) _thumbCache.set(key, url);
          imgEl.src = url;
        }
      } catch (_) { }
    }

    /** Update the live dialog with data from a running (or recently-finished) queue item. */
    function updateLiveAnalysisDlg(item) {
      const dlg = document.getElementById('liveAnalysisDlg');
      if (!dlg || !dlg.open) { _liveAnalysisDlgOpen = false; return; }

      // Header
      const folderEl = document.getElementById('liveDlgFolderName');
      const fnameEl = document.getElementById('liveDlgFilename');
      const statusEl = document.getElementById('liveDlgStatus');
      if (folderEl) folderEl.textContent = item ? item.name : '–';
      if (fnameEl) fnameEl.textContent = item ? (item.current_filename || '') : '';
      if (statusEl) {
        const msg = item ? (item.current_status_msg || '') : '';
        const paused = item && item.is_paused;
        statusEl.textContent = paused ? '⏸ Paused — ' + msg : msg;
      }

      if (!item) return;

      // Thumbnail
      const thumbEl = document.getElementById('liveDlgThumb');
      if (thumbEl && item.current_export_path) {
        const k = item.current_export_path + '|' + item.path;
        if (_liveLastThumbKey !== k) { _liveLastThumbKey = k; _loadImg(thumbEl, item.current_export_path, item.path); }
      }

      // Detection overlay
      const overlayEl = document.getElementById('liveDlgOverlay');
      if (overlayEl) {
        if (item.current_overlay_rel) {
          const k = item.current_overlay_rel + '|' + item.path;
          // Always reload live overlay images (they are overwritten in place).
          const isLiveOverlay = String(item.current_overlay_rel).indexOf('__live_') >= 0;
          if (isLiveOverlay) {
            _liveLastOverlayKey = k + '|' + Date.now();
            _loadImg(overlayEl, item.current_overlay_rel, item.path);
          } else if (_liveLastOverlayKey !== k) {
            _liveLastOverlayKey = k; _loadImg(overlayEl, item.current_overlay_rel, item.path);
          }
          overlayEl.style.visibility = '';
        } else {
          overlayEl.style.visibility = 'hidden';
        }
      }

      // Crop cards
      _updateLiveCropCards(item);
    }

    function _formatStars(rating) {
      const r = Math.max(0, Math.min(5, Math.round(rating || 0)));
      return '★'.repeat(r) + '☆'.repeat(5 - r);
    }

    function _rawQualityToRating(quality) {
      const q = Number(quality);
      if (!Number.isFinite(q) || q < 0) return 0;
      if (q < 0.15) return 1;
      if (q < 0.3) return 2;
      if (q < 0.6) return 3;
      if (q < 0.9) return 4;
      return 5;
    }

    function _updateLiveCropCards(item) {
      const row = document.getElementById('liveDlgCrops');
      if (!row) return;
      const crops = item.current_crops_rel || [];
      const dets = item.current_detections || [];
      const quality = item.current_quality_results || [];
      const species = item.current_species_results || [];
      const cardCount = Math.max(crops.length, dets.length, quality.length, species.length);

      // Keep card count in sync with current live detections.
      while (row.children.length < cardCount) {
        const card = document.createElement('div');
        card.className = 'live-dlg-crop-card';
        card.innerHTML = `
        <img class="live-dlg-crop-img" alt="" />
        <div class="ldc-conf">–</div>
        <div class="ldc-quality">Quality: —</div>
        <div class="ldc-stars">☆☆☆☆☆</div>
        <div class="ldc-species">–</div>
        <div class="ldc-family">–</div>`;
        row.appendChild(card);
      }
      while (row.children.length > cardCount) {
        row.removeChild(row.lastChild);
      }

      for (let i = 0; i < cardCount; i++) {
        const card = row.children[i];
        const imgEl = card.querySelector('.live-dlg-crop-img');
        const confEl = card.querySelector('.ldc-conf');
        const qualityEl = card.querySelector('.ldc-quality');
        const starsEl = card.querySelector('.ldc-stars');
        const spEl = card.querySelector('.ldc-species');
        const fmEl = card.querySelector('.ldc-family');
        const hasCrop = i < crops.length && crops[i];

        card.style.opacity = hasCrop ? '1' : '0.3';

        if (hasCrop) {
          const k = crops[i] + '|' + item.path;
          const prev = _liveLastCropKeys[i] || '';
          const isLiveCrop = String(crops[i]).indexOf('__live_') >= 0;
          if (isLiveCrop) {
            _liveLastCropKeys[i] = k + '|' + Date.now();
            _loadImg(imgEl, crops[i], item.path);
          } else if (prev !== k) { _liveLastCropKeys[i] = k; _loadImg(imgEl, crops[i], item.path); }
        } else {
          if (imgEl.src) imgEl.removeAttribute('src');
          _liveLastCropKeys[i] = '';
          confEl.textContent = '–';
          qualityEl.textContent = 'Quality: —';
          starsEl.textContent = '☆☆☆☆☆';
          spEl.textContent = '–'; spEl.className = 'ldc-species';
          fmEl.textContent = '–'; fmEl.className = 'ldc-family';
          continue;
        }

        // Detection confidence
        confEl.textContent = i < dets.length
          ? `Conf: ${dets[i].confidence.toFixed(2)}`
          : '–';

        const qVal = i < quality.length ? Number(quality[i].quality) : NaN;
        if (Number.isFinite(qVal) && qVal >= 0) {
          qualityEl.textContent = `Quality: ${qVal.toFixed(3)}`;
        } else {
          qualityEl.textContent = i < crops.length ? 'Quality: …' : 'Quality: —';
        }

        // Live dialog intentionally uses raw quality thresholds (not normalized ratings).
        const rawRating = Number.isFinite(qVal) ? _rawQualityToRating(qVal) : 0;
        starsEl.textContent = i < quality.length
          ? _formatStars(rawRating)
          : (i < crops.length ? '…' : '☆☆☆☆☆');

        // Species
        if (i < species.length) {
          const sp = species[i];
          const spConf = sp.species_confidence ?? 0;
          const fmConf = sp.family_confidence ?? 0;
          spEl.textContent = `${sp.species || '–'} (${spConf.toFixed(2)})`;
          spEl.className = 'ldc-species ' + (spConf >= CONF_HIGH ? 'high-conf' : spConf < CONF_LOW ? 'low-conf' : '');
          fmEl.textContent = sp.family ? `${sp.family} (${fmConf.toFixed(2)})` : '–';
          fmEl.className = 'ldc-family ' + (fmConf >= CONF_HIGH ? 'high-conf' : fmConf < CONF_LOW ? 'low-conf' : '');
        } else {
          spEl.textContent = i < crops.length ? 'Classifying…' : '–';
          spEl.className = i < crops.length ? 'ldc-species low-conf' : 'ldc-species';
          fmEl.textContent = '–'; fmEl.className = 'ldc-family';
        }
      }

      _liveLastCropKeys.length = cardCount;
    }

    // ── End Live Analysis Dialog ─────────────────────────────────────────────────

    // ── Auto-refresh: silently reload CSV data for newly-analyzed folders ─────────

    let _autoRefreshTimer = null;
    let _autoRefreshPendingPaths = new Set(); // paths that need a quiet reload
    let _silentRefreshRunning = false;        // guard against concurrent silentRefreshPending

    /** Queue a silent reload for `path` (called when a queue item becomes done). */
    function scheduleAutoRefresh(path) {
      _autoRefreshPendingPaths.add(path);
    }

    function startAutoRefresh() {
      if (_autoRefreshTimer) return;
      _autoRefreshTimer = setInterval(silentRefreshPending, 7000);
    }

    function stopAutoRefresh() {
      if (_autoRefreshTimer) { clearInterval(_autoRefreshTimer); _autoRefreshTimer = null; }
    }

    // User-editable row fields that must survive auto-refresh merges.
    // Pipeline-written columns are updated from disk; these are kept from memory.
    const _USER_EDITABLE_FIELDS = [
      'rating', 'rating_origin', 'normalized_rating',
      'culled', 'culled_origin',
      'scene_name',
    ];

    /** Silently reload CSV data for any paths in _autoRefreshPendingPaths that are checked.
     *
     *  Merges new pipeline data into existing in-memory rows instead of replacing
     *  them wholesale, so unsaved user edits (ratings, culling, scene names) are
     *  preserved.  Only genuinely new images (not yet in memory) are appended.
     *  Scenedata is merged additively: new scenes are added but user-edited
     *  scenes are never overwritten.
     */
    async function silentRefreshPending() {
      if (_autoRefreshPendingPaths.size === 0) return;
      if (_silentRefreshRunning) return;
      _silentRefreshRunning = true;
      try {
        const toRefresh = Array.from(_autoRefreshPendingPaths).filter(p => _isPathChecked(p));
        _autoRefreshPendingPaths.clear();
        if (toRefresh.length === 0) return;

        const normPath = p => (p || '').replace(/\\/g, '/');
        let hasNewRows = false;
        let hasUpdates = false;
        for (const p of toRefresh) {
          try {
            if (!hasPywebviewApi || !window.pywebview?.api?.read_kestrel_csv) continue;
            const result = await window.pywebview.api.read_kestrel_csv(p);
            if (!result.success) continue;
            const parsed = parseCsvText(result.data);
            const diskRows = parsed.data || [];
            const newFields = parsed.meta.fields || [];
            const root = result.root || p;
            const rootN = normPath(root);
            for (const f of newFields) if (!header.includes(f)) header.push(f);

            // Build a lookup of existing in-memory rows for this folder by filename
            const existingByName = new Map();
            for (const r of rows) {
              if (normPath(r.__rootPath) === rootN && r.filename) {
                existingByName.set(r.filename, r);
              }
            }

            const sample = rows.find(r => normPath(r.__rootPath) === rootN);
            const slot = sample ? sample.__folderSlot : rows.length;
            const addedRows = [];

            for (const diskRow of diskRows) {
              const fname = diskRow.filename;
              if (!fname) continue;
              const existing = existingByName.get(fname);
              if (existing) {
                // Merge: update pipeline-written fields from disk, keep user edits
                const savedEdits = {};
                for (const field of _USER_EDITABLE_FIELDS) {
                  if (field in existing) savedEdits[field] = existing[field];
                }
                // Also preserve internal UI fields
                const savedInternal = {
                  __rootPath: existing.__rootPath,
                  __folderSlot: existing.__folderSlot,
                  __normalized_rating: existing.__normalized_rating,
                };

                // Update pipeline fields from disk
                for (const key of Object.keys(diskRow)) {
                  if (key.startsWith('__')) continue;
                  if (!_USER_EDITABLE_FIELDS.includes(key)) {
                    existing[key] = diskRow[key];
                  }
                }

                // Restore user edits
                for (const [field, val] of Object.entries(savedEdits)) {
                  // Only restore if the user actually set something
                  if (val !== undefined && val !== '') existing[field] = val;
                }
                // Restore internal fields
                Object.assign(existing, savedInternal);
                hasUpdates = true;
              } else {
                // Genuinely new row from pipeline — append it
                diskRow.__rootPath = root;
                diskRow.__folderSlot = slot;
                addedRows.push(diskRow);
              }
            }

            if (addedRows.length > 0) {
              rows = rows.concat(addedRows);
              hasNewRows = true;
            }

            // Merge scenedata: add new scenes from disk without overwriting user-edited ones
            if (hasPywebviewApi && window.pywebview?.api?.read_kestrel_scenedata) {
              try {
                const sdRes = await window.pywebview.api.read_kestrel_scenedata(root);
                if (sdRes?.success && sdRes.data) {
                  const diskSd = sdRes.data;
                  const memSd = _scenedata[root] || { version: '2.0', image_ratings: {}, scenes: {} };

                  // Merge image_ratings: disk values only for images without user edits
                  if (diskSd.image_ratings) {
                    if (!memSd.image_ratings) memSd.image_ratings = {};
                    for (const [fname, rating] of Object.entries(diskSd.image_ratings)) {
                      if (!(fname in memSd.image_ratings)) {
                        memSd.image_ratings[fname] = rating;
                      }
                    }
                  }

                  // Merge scenes: add new scenes, update non-user-edited scenes
                  if (diskSd.scenes) {
                    if (!memSd.scenes) memSd.scenes = {};
                    for (const [sceneId, diskScene] of Object.entries(diskSd.scenes)) {
                      const memScene = memSd.scenes[sceneId];
                      if (!memScene) {
                        // New scene from pipeline — add it
                        memSd.scenes[sceneId] = diskScene;
                      } else {
                        // Existing scene — only update image list, keep user edits
                        const userFinalized = memScene.user_tags && memScene.user_tags.finalized;
                        const userRenamed = memScene.name && memScene.name.trim() !== '';
                        const userAccepted = memScene.status === 'accepted';
                        if (!userFinalized && !userRenamed && !userAccepted) {
                          // No user edits — safe to update from disk
                          // But still merge image_filenames additively
                          const existingFiles = new Set(memScene.image_filenames || []);
                          for (const f of (diskScene.image_filenames || [])) {
                            if (!existingFiles.has(f)) {
                              memScene.image_filenames.push(f);
                            }
                          }
                        } else {
                          // User has edited this scene — only add new filenames
                          const existingFiles = new Set(memScene.image_filenames || []);
                          for (const f of (diskScene.image_filenames || [])) {
                            if (!existingFiles.has(f)) {
                              memScene.image_filenames.push(f);
                            }
                          }
                        }
                      }
                    }
                  }

                  _scenedata[root] = memSd;
                }
              } catch (_) {}
            }

            // Apply normalization to new rows only
            if (addedRows.length > 0 && hasPywebviewApi && window.pywebview?.api?.apply_normalization) {
              try {
                const normRes = await window.pywebview.api.apply_normalization(root);
                if (normRes?.success && normRes?.normalized_ratings) {
                  const mapping = normRes.normalized_ratings;
                  for (const r of addedRows) {
                    if (r.filename in mapping) r.__normalized_rating = mapping[r.filename];
                  }
                }
              } catch (_) {}
            }
          } catch (e) {
            console.warn('[autorefresh]', p, e);
          }
        }

        if (hasNewRows || hasUpdates) {
          ensureSceneNameColumn();
          ensureRatingColumns();
          await renderScenes();
          // If a scene dialog is open, its filmstrip is stale after renderScenes
          // rebuilt the scenes array with fresh row objects — re-render it now.
          if (_currentScene) {
            renderFilmstrip(_currentScene);
            const safeIdx = Math.max(0, Math.min(currentImageIndex, _currentScene.images.length - 1));
            await selectFilmstripImage(safeIdx, _currentScene);
          }
          if (hasNewRows) {
            setStatus(`Auto-refreshed: new images added from analysis`);
          }
        }
      } finally {
        _silentRefreshRunning = false;
      }
    }

    /** Re-scan a single root in place — updates only that root's node in the
     *  Map, preserves expanded/checked state, then re-renders. Used after a
     *  queued folder under that root finishes analyzing. */
    async function rescanFolderRoot(rootPath) {
      if (!hasPywebviewApi || !window.pywebview?.api?.list_subfolders) return;
      const normRoot = (rootPath || '').replace(/\\/g, '/').replace(/\/+$/, '');
      if (!folderTreeRootNodes.has(normRoot)) return;
      try {
        const depth = getSetting('treeScanDepth', 3);
        const result = await window.pywebview.api.list_subfolders(normRoot, depth);
        if (!result.success) return;
        const rootName = normRoot.split('/').filter(Boolean).pop() || normRoot;
        const updated = {
          name: rootName,
          path: normRoot,
          has_kestrel: !!result.root_has_kestrel,
          kestrel_version: result.root_kestrel_version || '',
          children: result.tree || [],
        };
        // Apply transient kestrel markings (folders recently queued/started
        // should appear analyzed until the real scan state differs).
        try {
          const norm = p => (p || '').replace(/\\/g, '/');
          function applyTemp(n) {
            if (!n) return;
            const p = norm(n.path || '');
            if (_tempKestrelPaths.has(p)) n.has_kestrel = true;
            (n.children || []).forEach(c => applyTemp(c));
          }
          applyTemp(updated);
        } catch (e) { /* ignore */ }
        folderTreeRootNodes.set(normRoot, updated);
        renderFolderTree();
      } catch (_) { }
    }
    // Legacy alias for any straggling callers.
    const rescanFolderTree = rescanFolderRoot;

    /** Update UI to reflect in-progress folders with hourglass indicator and checkboxes. */
    function updateInProgressFoldersInTree() {
      try {
        const norm = p => (p || '').replace(/\\/g, '/');
        const rows = Array.from(document.querySelectorAll('#folderTree .tree-node-row'));
        for (const row of rows) {
          const rp = norm(row.dataset.path || '');
          const isNowInProgress = _inProgressFolderPaths.has(rp)
            || (window._ccInProgressFolderPaths && window._ccInProgressFolderPaths.has(rp));

          if (isNowInProgress) {
            row.classList.add('in-progress');
            row.classList.remove('no-kestrel');
            row.classList.add('has-kestrel');
            _tempKestrelPaths.add(rp);

            if (!row.querySelector('.tree-in-progress-hourglass')) {
              const hg = document.createElement('span');
              hg.className = 'tree-in-progress-hourglass';
              hg.textContent = '⏳';
              hg.title = 'Analysis in progress';
              const featherEl = row.querySelector('.tree-perch-feather');
              const anchorEl = featherEl || row.querySelector('.tree-icon');
              if (anchorEl && anchorEl.nextSibling) anchorEl.parentNode.insertBefore(hg, anchorEl.nextSibling);
              else if (anchorEl) anchorEl.parentNode.appendChild(hg);
            }

            if (!row.querySelector('.tree-cb')) {
              const cb = document.createElement('input');
              cb.type = 'checkbox';
              cb.className = 'tree-cb';
              cb.title = 'Include in multi-folder view (analyzing now)';
              cb.checked = _isPathChecked(row.dataset.path);
              cb.addEventListener('change', (e) => {
                e.stopPropagation();
                if (cb.checked) checkedFolderPaths.add(row.dataset.path);
                else checkedFolderPaths.delete(row.dataset.path);
                _updateAutoRefreshTimers();
                debouncedAutoLoad();
              });
              const icon = row.querySelector('.tree-icon');
              if (icon && icon.parentNode) icon.parentNode.insertBefore(cb, icon);
              else row.insertBefore(cb, row.firstChild);
            }
          } else if (row.classList.contains('in-progress')) {
            row.classList.remove('in-progress');
            const hg = row.querySelector('.tree-in-progress-hourglass');
            if (hg) hg.remove();
          }
        }
      } catch (e) { console.warn('[tree] updateInProgressFoldersInTree error:', e); }
    }

    // Path-insensitive check: does checkedFolderPaths contain a path matching p?
    function _isPathChecked(p) {
      const n = (p || '').replace(/\\/g, '/');
      for (const cp of checkedFolderPaths) {
        if (cp.replace(/\\/g, '/') === n) return true;
      }
      return false;
    }

    function _normalizeFolderPathForCache(path) {
      return String(path || '').trim().replace(/\\/g, '/').replace(/\/+$/, '');
    }

    function _snapshotCheckedFolderPathMap() {
      const snapshot = new Map();
      for (const rawPath of checkedFolderPaths) {
        const normalized = _normalizeFolderPathForCache(rawPath);
        if (!normalized || snapshot.has(normalized)) continue;
        snapshot.set(normalized, rawPath);
      }
      return snapshot;
    }

    async function _cleanupCullingCachesForPaths(paths, reason = '') {
      if (!paths || paths.length === 0) return;

      // Also clear in-memory caches so the UI can't show stale previews.
      try { blobUrlCache.clear(); } catch (_) {}
      try { sceneRawCache.clear(); } catch (_) {}
      try { sceneRawLoading.clear(); } catch (_) {}

      if (!hasPywebviewApi || !window.pywebview?.api?.cleanup_culling_cache) return;
      await Promise.all(paths.map(async (rootPath) => {
        try {
          const res = await window.pywebview.api.cleanup_culling_cache(rootPath);
          if (!res?.success) {
            console.warn('[cache] cleanup_culling_cache failed:', rootPath, res?.error || 'Unknown error', reason);
          }
        } catch (e) {
          console.warn('[cache] cleanup_culling_cache error:', rootPath, reason, e);
        }
      }));
    }

    async function _cleanupUncheckedFolderCaches() {
      const current = _snapshotCheckedFolderPathMap();
      const removed = [];
      for (const [normalized, rawPath] of _checkedFolderPathSnapshot.entries()) {
        if (!current.has(normalized)) removed.push(rawPath);
      }
      _checkedFolderPathSnapshot = current;
      if (removed.length > 0) {
        await _cleanupCullingCachesForPaths(removed, 'folder_unchecked');
      }
    }

    function _collectLoadedRootsForCleanup() {
      const roots = new Set();
      try {
        for (const p of checkedFolderPaths) if (p) roots.add(p);
      } catch (_) {}
      try {
        for (const r of rows) {
          if (r && r.__rootPath) roots.add(r.__rootPath);
        }
      } catch (_) {}
      if (rootPath) roots.add(rootPath);
      return Array.from(roots);
    }

    function _cleanupCachesOnAppClose() {
      const roots = _collectLoadedRootsForCleanup();
      if (!roots.length) return;
      _cleanupCullingCachesForPaths(roots, 'app_close').catch(() => {});
    }

    /** Start or stop auto-refresh timers for checked in-progress folders. */
    function _updateAutoRefreshTimers() {
      try {
        const queueStatus = window._lastQueueStatus;
        const isPaused = queueStatus && queueStatus.paused;
        
        if (isPaused) {
          for (const timerId of _autoRefreshTimers.values()) {
            clearInterval(timerId);
          }
          _autoRefreshTimers.clear();
          return;
        }
        
        const ccPaths = window._ccInProgressFolderPaths || new Set();
        for (const [path, timerId] of _autoRefreshTimers.entries()) {
          const isStillInProgress = _inProgressFolderPaths.has(path) || ccPaths.has(path);
          const isStillChecked = _isPathChecked(path);
          if (!isStillInProgress || !isStillChecked) {
            clearInterval(timerId);
            _autoRefreshTimers.delete(path);
          }
        }

        const allInProgress = new Set([..._inProgressFolderPaths, ...ccPaths]);
        for (const inProgPath of allInProgress) {
          if (_isPathChecked(inProgPath) && !_autoRefreshTimers.has(inProgPath)) {
            const capturedPath = inProgPath;
            const timerId = setInterval(async () => {
              try {
                _autoRefreshPendingPaths.add(capturedPath);
                silentRefreshPending();
              } catch (e) { console.warn('[refresh] auto-refresh error:', e); }
            }, 10000);
            _autoRefreshTimers.set(inProgPath, timerId);
          }
        }
      } catch (e) { console.warn('[timer] _updateAutoRefreshTimers error:', e); }
    }

    /** Count how many analyzed (non-in-progress) folders exist across ALL
     *  loaded roots. */
    function countAnalyzedFolders() {
      try {
        let count = 0;
        function traverse(n) {
          if (!n) return;
          const np = (n.path || '').replace(/\\/g, '/');
          const ccPaths = window._ccInProgressFolderPaths || new Set();
          if (n.has_kestrel && !_inProgressFolderPaths.has(np) && !ccPaths.has(np)) count++;
          (n.children || []).forEach(c => traverse(c));
        }
        for (const root of _getAllRoots()) traverse(root);
        return count;
      } catch (e) { return 0; }
    }

    // ── End Analysis Queue ────────────────────────────────────────────────────────

