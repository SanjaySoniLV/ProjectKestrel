    // ── Analysis Queue event wiring ───────────────────────────────────────────────

    // "Analyze Folders…" button opens the dialog
    const analyzeQueueBtn = document.getElementById('analyzeQueueBtn');
    if (analyzeQueueBtn) {
      analyzeQueueBtn.addEventListener('click', openAnalyzeDialog);
    }

    // ── Phase 3 Analyze Folders dialog event wiring ─────────────────────────────
    // The dialog has fully independent state (analyzeDlgRootNodes /
    // analyzeDlgCheckedPaths / analyzeDlgInspectionCache). All persistence and
    // .kestrel deletion happens in the queue worker (per-item flags).

    // Cancel: just close. State persists across opens until Clear is clicked.
    document.getElementById('analyzeDlgCancel')?.addEventListener('click', () => {
      document.getElementById('analyzeQueueDlg').close();
    });

    // + Add Folders: multi-select picker → addAnalyzeDlgRoot per pick →
    // persist each to analyze_recents → re-inspect + render.
    document.getElementById('analyzeDlgLoadFolders')?.addEventListener('click', async () => {
      if (!hasPywebviewApi) { alert('Folder picker is only available in the desktop app.'); return; }
      let pickedPaths = [];
      try {
        if (window.pywebview?.api?.choose_directories) {
          const res = await window.pywebview.api.choose_directories();
          if (Array.isArray(res)) pickedPaths = res.filter(Boolean);
          else if (typeof res === 'string' && res) pickedPaths = [res];
        } else {
          const single = await window.pywebview.api.choose_directory();
          if (single) pickedPaths = [single];
        }
      } catch (e) { console.error('Analyze dlg picker failed', e); return; }
      if (pickedPaths.length === 0) return;
      for (const p of pickedPaths) {
        const r = await addAnalyzeDlgRoot(p);
        if (r && r.added) await _persistAnalyzeRecentsBump(p);
      }
      await renderAnalyzeDlgRecentsChips();
      await _inspectAndRenderAnalyzeDlg();
    });

    // Clear: drop ALL dialog state (roots, checks, expansions). analyze_recents
    // is preserved so the user can re-click chips to rebuild.
    document.getElementById('analyzeDlgClear')?.addEventListener('click', () => {
      clearAnalyzeDlgRoots();
      renderAnalyzeDlgTree();
      refreshAnalyzeDlgSummary();
      renderAnalyzeDlgRecentsChips();
    });

    // ── Start Analysis (Phase 3H) ────────────────────────────────────────────
    // Compute per-item flags from the dialog state. If any folder will lose
    // user data, open the confirmation modal. Otherwise start immediately.
    const analyzeDlgAdd = document.getElementById('analyzeDlgAdd');
    if (analyzeDlgAdd) {
      analyzeDlgAdd.addEventListener('click', async () => {
        const checkedNorms = Array.from(analyzeDlgCheckedPaths);
        if (checkedNorms.length === 0) return;
        const retryErrored = document.getElementById('adlgRetryErrored')?.checked ?? true;

        // Compute per-item options and classify each folder.
        const perItemOptions = {};
        const destructive = []; // [{path, name, reason}]
        for (const norm of checkedNorms) {
          const node = _adlgFindNode(norm);
          const info = analyzeDlgInspectionCache.get(norm);
          const name = node ? node.name : (norm.split('/').filter(Boolean).pop() || norm);
          const total = (info && info.total) || 0;
          const processed = (info && info.processed) || 0;
          const errored = (info && info.errored) || 0;
          const outdated = node ? (typeof isVersionOutdated === 'function' && isVersionOutdated(node)) : false;
          const isFullyAnalyzed = total > 0 && processed >= total && errored === 0;

          const opts = {};
          if (outdated) {
            // Outdated always wipes + re-analyzes (the user picked a folder
            // analyzed on an older Kestrel; the new pipeline can't safely
            // resume from old artifacts).
            opts.delete_kestrel_on_start = true;
            destructive.push({ path: norm, name, reason: `outdated (v${node.kestrel_version} → v${_appVersion})` });
          } else if (isFullyAnalyzed) {
            if (analyzeDlgReanalyzeUnlocked) {
              opts.delete_kestrel_on_start = true;
              destructive.push({ path: norm, name, reason: 'fully analyzed — will erase data' });
            } else {
              // Worker re-inspects + silently marks done with no .kestrel touch.
              opts.skip_if_already_done = true;
            }
          }
          // Errored-only folders (partial + errored > 0) don't need wipe;
          // the pipeline's retry_errored logic handles them in-place.
          perItemOptions[norm] = opts;
        }

        // If any destructive flags are set, open the confirmation modal.
        if (destructive.length > 0) {
          _openAnalyzeConfirmModal(destructive, async () => {
            await _persistAnalyzeSettingsAndStart(checkedNorms, perItemOptions, retryErrored);
          });
          return;
        }
        await _persistAnalyzeSettingsAndStart(checkedNorms, perItemOptions, retryErrored);
      });
    }

    // Persist advanced settings, then call start_analysis_queue. Used by
    // both the direct-start path (no destructive flags) and the post-
    // confirmation path.
    async function _persistAnalyzeSettingsAndStart(paths, perItemOptions, retryErrored) {
      // Read + persist advanced settings.
      const dtVal = Math.max(0.1, Math.min(0.99, parseFloat(document.getElementById('adlgDetectionThreshold')?.value) || 0.15));
      const mbcRaw = parseInt(document.getElementById('adlgMaxBirdCrops')?.value, 10);
      const mbcVal = Math.max(1, Math.min(20, Number.isFinite(mbcRaw) ? mbcRaw : 10));
      const eqRaw = String(document.getElementById('adlgExposureQuality')?.value || 'balanced').toLowerCase();
      const eqVal = ['lenient', 'balanced', 'aggressive'].includes(eqRaw) ? eqRaw : 'balanced';
      const modelRaw = String(document.getElementById('adlgWildlifeModelMode')?.value || 'accurate').toLowerCase();
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

      const useGpu = document.getElementById('analyzeUseGpu')?.checked ?? true;
      const wildlifeEnabled = document.getElementById('analyzeWildlife')?.checked ?? false;
      const speciesDetectionEnabled = document.getElementById('analyzeSpeciesDetection')?.checked ?? true;

      const startBtn = document.getElementById('analyzeDlgAdd');
      if (startBtn) startBtn.disabled = true;
      document.getElementById('analyzeQueueDlg').close();
      try {
        // Cloud destination: clear .kestrel synchronously for any destructive
        // items (the cloud worker doesn't see local .kestrel state), then
        // submit each folder via the cloud-compute bridge. Local destination
        // falls through to start_analysis_queue with per-item options so the
        // queue worker handles .kestrel deletion just-in-time.
        // TODO(est-time-cloud): replace the dialog's local-rate estimate with
        // an upload-speed-test-aware estimate when destination=cloud. Local
        // perf_samples_gpu/cpu reflect compute time, not transfer time, and
        // are misleading here. Defer until upload-speed test lands.
        if (typeof _analyzeDestination !== 'undefined' && _analyzeDestination === 'cloud') {
          for (const p of paths) {
            const opts = (perItemOptions || {})[p] || {};
            if (opts.delete_kestrel_on_start) {
              try { await window.pywebview.api.clear_kestrel_data(p); } catch (e) {
                console.warn('[cloud] clear_kestrel_data failed', p, e);
              }
            }
          }
          await _ccSubmitSelectedFolders(paths);
          if (typeof _ccUpdateAddButtonLabel === 'function') _ccUpdateAddButtonLabel();
          return;
        }

        showLoadingAnalyzer();
        const result = await window.pywebview.api.start_analysis_queue(
          paths,
          useGpu,
          wildlifeEnabled,
          retryErrored,
          speciesDetectionEnabled,
          perItemOptions || {},
        );
        if (result && result.success) {
          _isFirstQueueStart = true;
          _queueSessionStartState.clear();
          _queueFolderInspections.clear();
          startPollingQueue();
          const status = await apiGetQueueStatus();
          renderQueuePanel(status);
          setStatus(`Analysis queue started — ${result.added || paths.length} folder(s) queued`);
          setTimeout(() => { try { hideLoadingAnalyzer(); } catch (e) { } }, 30000);
        } else {
          hideLoadingAnalyzer();
          alert('Failed to start analysis queue:\n\n' + (result?.error || 'Unknown error'));
        }
      } catch (e) {
        hideLoadingAnalyzer();
        alert('Failed to start analysis queue:\n\n' + (e.message || e));
      } finally {
        if (startBtn) startBtn.disabled = false;
      }
    }

    // Pre-queue confirmation modal (Phase 3H). Lists destructive folders,
    // forces an explicit click before .kestrel deletion happens.
    function _openAnalyzeConfirmModal(destructive, onProceed) {
      const dlg = document.getElementById('analyzeConfirmDlg');
      const list = document.getElementById('analyzeConfirmFolderList');
      const plural = document.getElementById('analyzeConfirmFolderPlural');
      const cancelBtn = document.getElementById('analyzeConfirmCancel');
      const proceedBtn = document.getElementById('analyzeConfirmProceed');
      if (!dlg || !list || !proceedBtn || !cancelBtn) {
        // Modal missing — fall back to a native confirm so the user isn't
        // silently bypassed past the destructive gate.
        const names = destructive.map(d => `  • ${d.name} (${d.reason})`).join('\n');
        if (!confirm(`The following folder(s) will lose all user data:\n\n${names}\n\nContinue?`)) return;
        onProceed();
        return;
      }
      if (plural) plural.textContent = destructive.length === 1 ? '' : 's';
      list.innerHTML = '';
      for (const d of destructive) {
        const li = document.createElement('li');
        const nameSpan = document.createElement('strong');
        nameSpan.textContent = d.name;
        const reasonSpan = document.createElement('span');
        reasonSpan.style.color = 'var(--muted)';
        reasonSpan.textContent = ` — ${d.reason}`;
        li.appendChild(nameSpan);
        li.appendChild(reasonSpan);
        list.appendChild(li);
      }
      // Make the parent dialog inert while the confirm is open so focus is
      // trapped in the modal.
      const parentDlg = document.getElementById('analyzeQueueDlg');
      if (parentDlg && 'inert' in parentDlg) parentDlg.inert = true;
      const handleCancel = () => {
        cleanup();
        dlg.close();
      };
      const handleProceed = async () => {
        cleanup();
        dlg.close();
        try { await onProceed(); } catch (e) { console.error('analyze confirm proceed error', e); }
      };
      function cleanup() {
        cancelBtn.removeEventListener('click', handleCancel);
        proceedBtn.removeEventListener('click', handleProceed);
        if (parentDlg && 'inert' in parentDlg) parentDlg.inert = false;
      }
      cancelBtn.addEventListener('click', handleCancel);
      proceedBtn.addEventListener('click', handleProceed);
      dlg.showModal();
    }

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

    // ── Timeline filter bar: "More" popover ───────────────────────────────────────
    // The set-and-forget grouping/display checkboxes live behind a popover so the
    // top filter bar stays compact. Click the button to toggle; click outside or
    // press Escape to close.
    (function wireTfbMorePopover() {
      const btn = document.getElementById('tfbMoreBtn');
      const popover = document.getElementById('tfbMorePopover');
      if (!btn || !popover) return;

      function closePopover() {
        popover.classList.add('hidden');
        btn.setAttribute('aria-expanded', 'false');
      }
      function openPopover() {
        popover.classList.remove('hidden');
        btn.setAttribute('aria-expanded', 'true');
      }

      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (popover.classList.contains('hidden')) openPopover();
        else closePopover();
      });

      document.addEventListener('click', (e) => {
        if (popover.classList.contains('hidden')) return;
        if (popover.contains(e.target) || btn.contains(e.target)) return;
        closePopover();
      });

      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !popover.classList.contains('hidden')) {
          closePopover();
        }
      });
    })();

