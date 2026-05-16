    // ── Analysis Queue event wiring ───────────────────────────────────────────────

    // "Analyze Folders…" button opens the dialog
    const analyzeQueueBtn = document.getElementById('analyzeQueueBtn');
    if (analyzeQueueBtn) {
      analyzeQueueBtn.addEventListener('click', openAnalyzeDialog);
    }

    // Analyze dialog Cancel
    const analyzeDlgCancel = document.getElementById('analyzeDlgCancel');
    if (analyzeDlgCancel) {
      analyzeDlgCancel.addEventListener('click', () => {
        // Save the current selection so user can restore it on next dialog open
        if (_dlgSelected && _dlgSelected.size > 0) {
          const s = loadSettings();
          s.lastQueueState = Array.from(_dlgSelected);
          saveSettings(s);
        }
        document.getElementById('analyzeQueueDlg').close();
      });
    }

    // Analyze dialog Add to Queue
    const analyzeDlgAdd = document.getElementById('analyzeDlgAdd');
    if (analyzeDlgAdd) {
      analyzeDlgAdd.addEventListener('click', async () => {
        const paths = Array.from(_dlgSelected);
        if (paths.length === 0) return;
        const useGpu = document.getElementById('analyzeUseGpu')?.checked ?? true;
        const wildlifeEnabled = document.getElementById('analyzeWildlife')?.checked ?? false;
        const speciesDetectionEnabled = document.getElementById('analyzeSpeciesDetection')?.checked ?? true;
        const retryErrored = document.getElementById('adlgRetryErrored')?.checked ?? true;

        // Outdated-version + re-analyze gates run for BOTH destinations. Cloud
        // jobs need the same .kestrel/ clearing as local before submit — without
        // it, an out-of-date local CSV either feeds a stale resume-anchor to
        // the cloud pipeline or silently merges incompatible rows back on
        // pack-merge. See plan: cloud previously short-circuited these gates.
        const outdatedPaths = [];
        function findNode(node, targetPath) {
          if (node.path === targetPath) return node;
          if (node.children) {
            for (const c of node.children) {
              const found = findNode(c, targetPath);
              if (found) return found;
            }
          }
          return null;
        }
        for (const p of paths) {
          if (_dlgReanalyze.has(p)) continue; // already confirmed at selection time
          const node = folderTreeRootNode ? findNode(folderTreeRootNode, p) : null;
          if (node && isVersionOutdated(node)) {
            outdatedPaths.push({ path: p, name: node.name, version: node.kestrel_version });
          }
        }

        if (outdatedPaths.length > 0) {
          const names = outdatedPaths.map(o => `  • ${o.name} (v${o.version})`).join('\n');
          const confirmed = confirm(
            `The following folder(s) were analyzed on an older version of Kestrel:\n\n${names}\n\n` +
            `Current version: v${_appVersion}\n\n` +
            `Re-analyzing will DELETE existing analysis data (.kestrel folder) before proceeding.\n\n` +
            `Continue?`
          );
          if (!confirmed) return;
          // Clear .kestrel for outdated folders before re-analysis
          for (const o of outdatedPaths) {
            try {
              await window.pywebview.api.clear_kestrel_data(o.path);
              // Update in-memory node
              const node = findNode(folderTreeRootNode, o.path);
              if (node) { node.has_kestrel = false; node.kestrel_version = ''; }
            } catch (e) {
              console.warn('Failed to clear kestrel data for', o.path, e);
            }
          }
        }

        // Clear .kestrel for fully-analyzed re-queue folders (confirmed at selection time)
        for (const p of _dlgReanalyze) {
          if (!paths.includes(p)) continue;
          try {
            await window.pywebview.api.clear_kestrel_data(p);
            const node = folderTreeRootNode ? findNode(folderTreeRootNode, p) : null;
            if (node) { node.has_kestrel = false; node.kestrel_version = ''; }
          } catch (e) {
            console.warn('Failed to clear kestrel data for re-analyze', p, e);
          }
        }

        // Cloud destination: branch here so the outdated/re-analyze gates above
        // ran (clearing .kestrel/ on user confirm) before submission. Local
        // settings persistence + start_analysis_queue do not apply to cloud.
        if (_analyzeDestination === 'cloud') {
          document.getElementById('analyzeQueueDlg').close();
          analyzeDlgAdd.disabled = true;
          try {
            await _ccSubmitSelectedFolders(paths);
            _dlgSelected.clear();
            _dlgReanalyze.clear();
          } finally {
            analyzeDlgAdd.disabled = false;
            _ccUpdateAddButtonLabel();
          }
          return;
        }

        // Persist advanced analysis settings before starting the queue
        {
          const dtVal = Math.max(0.1, Math.min(0.99, parseFloat(document.getElementById('adlgDetectionThreshold')?.value) || 0.25));
          const mbcRaw = parseInt(document.getElementById('adlgMaxBirdCrops')?.value, 10);
          const mbcVal = Math.max(1, Math.min(20, Number.isFinite(mbcRaw) ? mbcRaw : 10));
          const eqRaw = String(document.getElementById('adlgExposureQuality')?.value || 'balanced').toLowerCase();
          const eqVal = ['lenient', 'balanced', 'aggressive'].includes(eqRaw) ? eqRaw : 'balanced';
          const modelRaw = String(document.getElementById('adlgWildlifeModelMode')?.value || 'fast').toLowerCase();
          const modelVal = modelRaw === 'accurate' ? 'accurate' : 'fast';
          const detectorName = modelVal === 'accurate' ? 'mdv5a' : 'mdv1000-cedar';
          const stVal = Math.max(0, parseFloat(document.getElementById('adlgSceneTime')?.value) || 1.0);
          const ppRaw = parseInt(document.getElementById('adlgParallelPrefetch')?.value, 10);
          const ppVal = Math.max(1, Math.min(5, Number.isFinite(ppRaw) ? ppRaw : 3));
          const twRaw = parseInt(document.getElementById('adlgThumbnailMaxWidth')?.value, 10);
          const twVal = Math.max(400, Math.min(2400, Number.isFinite(twRaw) ? twRaw : 1200));
          const tcRaw = parseFloat(document.getElementById('adlgThumbnailJpegCompression')?.value);
          const tcVal = Math.max(0.5, Math.min(1.0, Number.isFinite(tcRaw) ? tcRaw : 0.75));
          const tqVal = Math.max(50, Math.min(100, Math.round(tcVal * 100)));
          const adlgSettings = loadSettings();
          adlgSettings.detection_threshold = dtVal;
          adlgSettings.max_bird_crops = mbcVal;
          adlgSettings.exposure_quality = eqVal;
          adlgSettings.wildlife_model_mode = modelVal;
          adlgSettings.detector_name = detectorName;
          adlgSettings.scene_time_threshold = stVal;
          adlgSettings.parallel_prefetch = ppVal;
          adlgSettings.thumbnail_max_width = twVal;
          adlgSettings.thumbnail_jpeg_compression = tcVal;
          adlgSettings.thumbnail_jpeg_quality = tqVal;
          saveSettings(adlgSettings);
          if (hasPywebviewApi && window.pywebview?.api?.save_settings_data) {
            try { await window.pywebview.api.save_settings_data(adlgSettings); } catch (_) { }
          }
        }

        document.getElementById('analyzeQueueDlg').close();
        analyzeDlgAdd.disabled = true;
        try {
          // Show loading overlay while analyzer imports models (lazy-load)
          showLoadingAnalyzer();
          const result = await apiStartQueue(paths, useGpu, wildlifeEnabled, retryErrored, speciesDetectionEnabled);
          if (result && result.success) {
            queuedFolderPaths.clear();
            _dlgSelected.clear();
            _isFirstQueueStart = true; // reset for Case 1 logic on next queue start
            // Clear saved queue state since we're starting a new queue
            const s = loadSettings();
            delete s.lastQueueState;
            saveSettings(s);
            // Clear session state for new queue start, so ETA calculations use fresh folder inspections
            _queueSessionStartState.clear();
            _queueFolderInspections.clear();
            startPollingQueue();
            const status = await apiGetQueueStatus();
            renderQueuePanel(status);
            setStatus(`Analysis queue started — ${result.added || paths.length} folder(s) queued`);
            // Start polling; renderQueuePanel will hide the loader when processing begins.
            // As a safety, hide the loader after 30s if nothing starts.
            setTimeout(() => { try { hideLoadingAnalyzer(); } catch (e) { } }, 30000);
          } else {
            hideLoadingAnalyzer();
            alert('Failed to start analysis queue:\n\n' + (result?.error || 'Unknown error'));
          }
        } catch (e) {
          hideLoadingAnalyzer();
          alert('Failed to start analysis queue:\n\n' + (e.message || e));
        } finally {
          analyzeDlgAdd.disabled = false;
        }
      });
    }

    // Analyze dialog: Change Folder button
    document.getElementById('analyzeDlgChangeRoot')?.addEventListener('click', async () => {
      if (!hasPywebviewApi) { alert('Directory browsing is only available in the desktop app.'); return; }
      const fp = await window.pywebview.api.choose_directory();
      if (!fp) return;
      await scanFolderTree(fp);
      if (!folderTreeRootNode) return;
      _dlgExpandedPaths = new Set([folderTreeRootNode.path]);
      _dlgSelected.clear();
      function refreshDlg2() {
        const countEl = document.getElementById('analyzeDlgCount');
        const addBtn = document.getElementById('analyzeDlgAdd');
        if (countEl) countEl.textContent = _dlgSelected.size + ' folder' + (_dlgSelected.size === 1 ? '' : 's') + ' selected';
        if (addBtn) addBtn.disabled = _dlgSelected.size === 0;
        _refreshAnalyzeDlgQueuePreview();
      }
      const treeEl = document.getElementById('analyzeDlgTree');
      treeEl.innerHTML = '';
      treeEl.appendChild(buildAnalyzeDlgNode(folderTreeRootNode, _dlgSelected, refreshDlg2));
      populateAnalyzeFolderCounts();
      refreshDlg2();
    });

    // ── Welcome Panel action wiring ──────────────────────────────────────────────

    // ── Legal Agreement Logic ──────────────────────────────────────────────
    let _pendingLegalEffectiveDate = '';
    const DEFAULT_TERMS_URL = 'https://projectkestrel.org/terms-of-use';
    const DEFAULT_PRIVACY_URL = 'https://projectkestrel.org/privacy-policy';

    function renderLegalBanner(status) {
      const banner = document.getElementById('legalNotice');
      const msgEl = document.getElementById('legalNoticeMsg');
      const termsLink = document.getElementById('legalViewTerms');
      const privacyLink = document.getElementById('legalViewPrivacy');
      if (!banner || !msgEl) return;

      const termsUrl = status?.terms_url || DEFAULT_TERMS_URL;
      const privacyUrl = status?.privacy_url || DEFAULT_PRIVACY_URL;
      if (termsLink) termsLink.href = termsUrl;
      if (privacyLink) privacyLink.href = privacyUrl;

      if (status?.reason === 'terms_updated') {
        msgEl.textContent = 'The Privacy Policy and/or Terms of Use have been updated. Please review and accept to continue using Project Kestrel.';
      } else {
        msgEl.textContent = 'By using Project Kestrel you agree to our Terms of Use and Privacy Policy.';
      }
      banner.classList.remove('hidden');
    }

    async function checkLegalAgreement() {
      if (!hasPywebviewApi || !window.pywebview?.api?.get_legal_status) return;
      try {
        const status = await window.pywebview.api.get_legal_status();
        _pendingLegalEffectiveDate = status?.effective_date || '';
        if (!status?.agreed) {
          renderLegalBanner(status);
        } else {
          document.getElementById('legalNotice')?.classList.add('hidden');
        }
      } catch (e) {
        console.error('Failed to check legal status', e);
      }
    }

    const legalAgreeBtn = document.getElementById('legalAgreeBtn');
    if (legalAgreeBtn) {
      legalAgreeBtn.addEventListener('click', async () => {
        try {
          if (hasPywebviewApi && window.pywebview?.api?.agree_to_legal) {
            await window.pywebview.api.agree_to_legal(_pendingLegalEffectiveDate || '');
            // Keep local settings in sync immediately after consent so later
            // UI-driven settings writes include the persisted legal flags.
            await hydrateSettingsFromServer();
            document.getElementById('legalNotice').classList.add('hidden');
            showToast('Terms accepted. Welcome to Project Kestrel!', 4000);
          }
        } catch (e) {
          console.error('Failed to agree to legal terms', e);
        }
      });
    }

    // Initial checks
    if (hasPywebviewApi) {
      checkLegalAgreement();
    }

    // Queue panel header: toggle expand / collapse
    const queuePanelHeader = document.getElementById('queuePanelHeader');
    if (queuePanelHeader) {
      queuePanelHeader.addEventListener('click', () => {
        _queuePanelExpanded = !_queuePanelExpanded;
        const toggle = document.getElementById('queuePanelToggle');
        const body = document.getElementById('queuePanelBody');
        const controls = document.getElementById('queuePanelControls');
        if (toggle) toggle.classList.toggle('open', _queuePanelExpanded);
        if (body) body.classList.toggle('hidden', !_queuePanelExpanded);
        if (controls) controls.classList.toggle('hidden', !_queuePanelExpanded);
      });
    }

    // Cloud queue panel header: toggle expand / collapse
    const cloudQueuePanelHeader = document.getElementById('cloudQueuePanelHeader');
    if (cloudQueuePanelHeader) {
      cloudQueuePanelHeader.addEventListener('click', () => {
        _cloudQueuePanelExpanded = !_cloudQueuePanelExpanded;
        const toggle = document.getElementById('cloudQueuePanelToggle');
        const body = document.getElementById('cloudQueuePanelBody');
        const controls = document.getElementById('cloudQueuePanelControls');
        if (toggle) toggle.classList.toggle('open', _cloudQueuePanelExpanded);
        if (body) body.classList.toggle('hidden', !_cloudQueuePanelExpanded);
        if (controls) controls.classList.toggle('hidden', !_cloudQueuePanelExpanded);
        if (typeof _ccRepositionPanel === 'function') _ccRepositionPanel();
        if (typeof _perchRepositionUploadsPanel === 'function') _perchRepositionUploadsPanel();
      });
    }

    // Pause / Resume button
    const queuePauseBtn = document.getElementById('queuePauseBtn');
    if (queuePauseBtn) {
      queuePauseBtn.addEventListener('click', async () => {
        try {
          const status = await apiGetQueueStatus();
          if (status.paused) {
            // When resuming, re-inspect folders to get accurate baselines
            if (status.items && status.items.length > 0 && hasPywebviewApi && window.pywebview?.api?.inspect_folders) {
              try {
                const paths = status.items.map(item => item.path);
                const inspectRes = await window.pywebview.api.inspect_folders(paths);
                if (inspectRes && inspectRes.success && inspectRes.results) {
                  for (const [path, info] of Object.entries(inspectRes.results)) {
                    if (info) {
                      const initialProcessed = info.processed || 0;
                      const totalImages = info.total || 0;
                      const toAnalyze = Math.max(0, totalImages - initialProcessed);
                      _queueSessionStartState.set(path, {
                        initialProcessed,
                        totalImages,
                        toAnalyze
                      });
                    }
                  }
                }
              } catch (e) { /* ignore */ }
            }
            await apiQueueControl('resume');
          } else {
            await apiQueueControl('pause');
          }
        } catch (_) { }
      });
    }

    // Cancel button
    const queueCancelBtn = document.getElementById('queueCancelBtn');
    if (queueCancelBtn) {
      queueCancelBtn.addEventListener('click', async () => {
        if (!confirm('Cancel the analysis queue? Pending folders will not be analyzed.')) return;
        try { await apiQueueControl('cancel'); } catch (_) { }
      });
    }

    // Clear done button
    const queueClearBtn = document.getElementById('queueClearBtn');
    if (queueClearBtn) {
      queueClearBtn.addEventListener('click', async () => {
        try {
          await apiQueueControl('clear');
          const status = await apiGetQueueStatus();
          if (!(status.items || []).some(i => i.status === 'pending' || i.status === 'running')) {
            document.getElementById('queuePanel')?.classList.add('hidden');
            stopPollingQueue();
          } else {
            renderQueuePanel(status);
          }
        } catch (_) { }
      });
    }

    // ---- Culling Assistant launcher ----
    async function openCullingAssistant(rootPath) {
      if (!window.pywebview?.api) {
        showToast('Culling Assistant requires desktop mode', 4000);
        return;
      }
      // Prompt to save unsaved changes before opening (using custom dialog)
      if (dirty) {
        const userChoice = await showCullingAssistantPrompt();
        if (userChoice === 'cancel') {
          return;
        }
        if (userChoice === 'save') {
          await saveCsv();
        }
      }
      try {
        showToast('Opening Culling Assistant...', 2000);
        const res = await window.pywebview.api.open_culling_window(rootPath);
        if (res && !res.success) {
          showToast('Failed to open Culling Assistant: ' + (res.error || 'Unknown error'), 5000);
        }
      } catch (e) {
        console.error('openCullingAssistant error', e);
        showToast('Error opening Culling Assistant', 4000);
      }
    }
