    const SETTINGS_KEY = 'kestrel-webviz-settings-v1';
    let _autoSaveEnabled = true;  // cached value to avoid repeated lookups
    let _autoSaveTimer = null;     // debounce timer for auto-saves
    
    function loadSettings() {
      try { return JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {}; } catch { return {}; }
    }
    function saveSettings(obj) { localStorage.setItem(SETTINGS_KEY, JSON.stringify(obj || {})); }
    function getSetting(k, def) { const s = loadSettings(); return (k in s) ? s[k] : def; }
    
    // Auto-save logic: debounced save when auto-save is enabled
    async function attemptAutoSave() {
      _setDirtyUi(true);
      
      if (!_autoSaveEnabled) {
        clearTimeout(_autoSaveTimer);
        _autoSaveTimer = null;
        return;  // Save/Revert workflow; user will click Save button
      }
      
      // Debounce to avoid saving on every keystroke/change (save after 2 seconds of inactivity)
      clearTimeout(_autoSaveTimer);
      _autoSaveTimer = setTimeout(async () => {
        _autoSaveTimer = null;
        if (!_autoSaveEnabled || !dirty) return;
        try {
          await saveCsv();
        } catch (e) {
          console.warn('Auto-save failed:', e);
        }
      }, 2000);
    }

    async function hydrateSettingsFromServer() {
      try {
        if (!window.pywebview?.api?.get_settings) return;
        const data = await window.pywebview.api.get_settings();
        const incoming = (data && data.settings && typeof data.settings === 'object') ? data.settings : null;
        if (!incoming) return;
        saveSettings(incoming);
        _applyScenePreviewSplit(_clampScenePreviewSplitRatio(incoming[SCENE_PREVIEW_SPLIT_KEY]));
        _autoSaveEnabled = incoming.auto_save_enabled !== false;
        if (!_autoSaveEnabled) {
          clearTimeout(_autoSaveTimer);
          _autoSaveTimer = null;
        }
        _updateSaveRevertVisibility();
      } catch (_) { }
    }

    async function showSettings() {
      // Refresh local cache from persisted settings before rendering values.
      try { await hydrateSettingsFromServer(); } catch (_) { }

      const dlg = document.getElementById('settingsDlg');
      const editor = getSetting('editor', 'darktable');
      const editorSelect = document.getElementById('editorChoice');
      const customRow = document.getElementById('customEditorRow');
      const customHint = document.getElementById('customEditorHint');
      const customPath = document.getElementById('customEditorPath');
      editorSelect.value = editor;
      // If saved editor isn't in the dropdown options, treat as custom
      if (editorSelect.value !== editor) {
        editorSelect.value = 'custom';
      }
      customPath.value = getSetting('customEditorPath', '');
      const isCustom = editorSelect.value === 'custom';
      customRow.classList.toggle('hidden', !isCustom);
      customHint.classList.toggle('hidden', !isCustom);
      // Show/hide custom row when selection changes
      editorSelect.onchange = () => {
        const c = editorSelect.value === 'custom';
        customRow.classList.toggle('hidden', !c);
        customHint.classList.toggle('hidden', !c);
      };
      // Browse button
      document.getElementById('customEditorBrowse').onclick = async () => {
        if (window.pywebview?.api?.choose_application) {
          const path = await window.pywebview.api.choose_application();
          if (path) customPath.value = path;
        } else {
          showToast('Browse is only available in the desktop app', 3000);
        }
      };
      document.getElementById('treeScanDepth').value = getSetting('treeScanDepth', 3);
      // Rating profile
      const profileSelect = document.getElementById('ratingProfile');
      if (profileSelect) profileSelect.value = getSetting('rating_profile', 'balanced');
      // RAW preview cache
      const rawCacheCb = document.getElementById('rawPreviewCacheEnabled');
      if (rawCacheCb) rawCacheCb.checked = getSetting('raw_preview_cache_enabled', true);
      const optedIn = getSetting('analytics_opted_in', null);
      const consentShown = getSetting('analytics_consent_shown', false);
      const cb = document.getElementById('settingsAnalyticsOptIn');
      const lbl = document.getElementById('settingsAnalyticsLabel');
      cb.checked = optedIn === true;
      lbl.textContent = consentShown
        ? (optedIn === true ? 'Opted in' : 'Not sharing')
        : 'Not yet decided';

      // Crash reports default ON (opt-out).
      const crashCb = document.getElementById('settingsCrashReports');
      if (crashCb) crashCb.checked = getSetting('crash_reports_enabled', true);
      
      // Display total impact (photos analyzed) from hydrated local settings.
      const totalPhotos = getSetting('kestrel_impact_total_files', 0);
      const impactEl = document.getElementById('settingsTotalImpact');
      if (impactEl) {
        impactEl.textContent = totalPhotos > 0 ? totalPhotos.toLocaleString() + ' photos' : '0 photos';
      }
      
      // Auto-Save setting
      const autoSaveCb = document.getElementById('settingsAutoSave');
      if (autoSaveCb) autoSaveCb.checked = getSetting('auto_save_enabled', true);

      const rawExpDisableCb = document.getElementById('rawExposureCorrectionDisabled');
      if (rawExpDisableCb) rawExpDisableCb.checked = getSetting('raw_exposure_correction_disabled', false);
      const ectSettings = document.getElementById('settingsExposureCorrectedThumbs');
      if (ectSettings) ectSettings.checked = !!getSetting('exposure_corrected_thumbs', true);

      // ── Species & Region section ─────────────────────────────────────────
      const showSciCb = document.getElementById('showScientificNames');
      if (showSciCb) showSciCb.checked = !!getSetting('show_scientific_names', false);

      const regionsPicker = document.getElementById('birdRegionsPicker');
      if (regionsPicker) {
        // Make sure catalog meta is loaded before we paint checkboxes -- the
        // load is cached so re-entering Settings is cheap.
        try { await loadSpeciesFamilyMap(); } catch (_) { /* non-fatal */ }
        const meta = _birdCatalogMeta || { regions: [], default_regions: ['NA'] };
        const selected = new Set(_getCurrentBirdRegions());
        if (!Array.isArray(meta.regions) || meta.regions.length === 0) {
          regionsPicker.innerHTML = '<span class="muted" style="font-size:11px">Region list unavailable — defaulting to North America.</span>';
        } else {
          regionsPicker.innerHTML = meta.regions.map(r => {
            const checked = selected.has(r.code) ? 'checked' : '';
            return (
              `<label class="inline" style="gap:6px;cursor:pointer;font-size:13px">` +
                `<input type="checkbox" class="bird-region-cb" data-region-code="${escapeHtml(r.code)}" ${checked} />` +
                `<span>${escapeHtml(r.label)}</span>` +
                `<span class="muted" style="font-size:11px;margin-left:auto">${escapeHtml(r.code)}</span>` +
              `</label>`
            );
          }).join('');
        }
      }

      dlg.showModal();
    }
    async function applySettings() {
      const editorSelect = document.getElementById('editorChoice');
      const editor = editorSelect.value || 'darktable';
      const customEditorPath = document.getElementById('customEditorPath').value.trim();
      const treeScanDepth = Math.max(1, Math.min(6, parseInt(document.getElementById('treeScanDepth').value, 10) || 3));
      const analyticsOptIn = document.getElementById('settingsAnalyticsOptIn').checked;
      const crashReportsCb = document.getElementById('settingsCrashReports');
      const crashReportsEnabled = crashReportsCb ? crashReportsCb.checked : true;
      const profileEl = document.getElementById('ratingProfile');
      const ratingProfile = profileEl ? profileEl.value : 'balanced';
      const rawCacheCb2 = document.getElementById('rawPreviewCacheEnabled');
      const rawPreviewCacheEnabled = rawCacheCb2 ? rawCacheCb2.checked : true;
      const autoSaveCb = document.getElementById('settingsAutoSave');
      const autoSaveEnabled = autoSaveCb ? autoSaveCb.checked : true;
      // Merge into existing settings so keys like machine_id / analytics_consent_shown are preserved
      const existing = loadSettings();
      const prevProfile = existing.rating_profile || 'balanced';
      const ectCb = document.getElementById('settingsExposureCorrectedThumbs');
      const exposureCorrectedThumbs = ectCb ? !!ectCb.checked : getSetting('exposure_corrected_thumbs', true);
      const prevExposureThumbs = getSetting('exposure_corrected_thumbs', true);
      const rawExpEl = document.getElementById('rawExposureCorrectionDisabled');
      const rawExposureCorrectionDisabled = rawExpEl ? !!rawExpEl.checked : !!existing.raw_exposure_correction_disabled;
      const prevRawExposureDisabled = !!existing.raw_exposure_correction_disabled;

      // ── Species & Region: collect region picker state + show-sci toggle ──
      // An empty selection falls back to ``['NA']`` rather than producing an
      // unusable combobox where no species ever surface.
      const regionsPicker = document.getElementById('birdRegionsPicker');
      const regionInputs = regionsPicker
        ? regionsPicker.querySelectorAll('input.bird-region-cb')
        : [];
      const birdRegions = Array.from(regionInputs)
        .filter(cb => cb.checked)
        .map(cb => cb.dataset.regionCode)
        .filter(c => typeof c === 'string' && c.length > 0);
      const finalRegions = birdRegions.length > 0 ? birdRegions : ['NA'];
      const showSciCbEl = document.getElementById('showScientificNames');
      const showScientificNames = showSciCbEl ? !!showSciCbEl.checked : !!existing.show_scientific_names;
      const prevRegions = Array.isArray(existing.bird_regions) ? existing.bird_regions.slice().sort().join(',') : '';
      const nextRegions = finalRegions.slice().sort().join(',');
      const regionsChanged = prevRegions !== nextRegions;
      const showSciChanged = !!existing.show_scientific_names !== showScientificNames;

      const settings = {
        ...existing, editor, customEditorPath, treeScanDepth,
        analytics_opted_in: analyticsOptIn, analytics_consent_shown: true,
        crash_reports_enabled: crashReportsEnabled,
        rating_profile: ratingProfile,
        raw_preview_cache_enabled: rawPreviewCacheEnabled,
        auto_save_enabled: autoSaveEnabled,
        raw_exposure_correction_disabled: rawExposureCorrectionDisabled,
        exposure_corrected_thumbs: exposureCorrectedThumbs,
        bird_regions: finalRegions,
        show_scientific_names: showScientificNames,
      };
      _autoSaveEnabled = autoSaveEnabled;
      if (!_autoSaveEnabled) {
        clearTimeout(_autoSaveTimer);
        _autoSaveTimer = null;
      }
      _updateSaveRevertVisibility();
      // Persist settings to localStorage immediately
      saveSettings(settings);
      if (hasPywebviewApi && window.pywebview?.api?.save_settings_data) {
        try { await window.pywebview.api.save_settings_data(settings); } catch (_) { }
      }
      document.getElementById('settingsDlg').close();
      // If rating profile changed and folders are loaded, reapply immediately
      if (ratingProfile !== prevProfile && rows.length > 0) {
        await reapplyNormalizationForLoadedFolders();
      }
      const thumbPreviewChanged =
        exposureCorrectedThumbs !== prevExposureThumbs || rawExposureCorrectionDisabled !== prevRawExposureDisabled;
      if (thumbPreviewChanged && rows.length > 0) {
        await renderScenes();
        if (sceneDlg?.open && _currentScene) {
          renderFilmstrip(_currentScene);
          await selectFilmstripImage(currentImageIndex, _currentScene, false, false);
        }
      }
      // Show-scientific-names + region changes don't affect the model output,
      // just the way pills render and which species the combobox surfaces.
      // Repaint anything that's currently on screen so the change is visible
      // immediately rather than waiting for the next interaction.
      if ((showSciChanged || regionsChanged) && rows.length > 0) {
        await renderScenes();
        if (sceneDlg?.open && _currentScene) {
          renderTopbarTags(_currentScene);
        }
      }
    }

    /** Recompute normalized_rating for every currently-loaded folder and refresh the view. */
    async function reapplyNormalizationForLoadedFolders() {
      if (!hasPywebviewApi || !window.pywebview?.api?.apply_normalization) return;
      // Collect the unique root paths of all loaded rows
      const folderPaths = [...new Set(rows.map(r => r.__rootPath).filter(Boolean))];
      if (folderPaths.length === 0) return;
      for (const p of folderPaths) {
        try {
          const res = await window.pywebview.api.apply_normalization(p);
          if (res?.success && res?.normalized_ratings) {
            const mapping = res.normalized_ratings;
            for (const r of rows) {
              if (r.__rootPath === p && r.filename in mapping) {
                r.__normalized_rating = mapping[r.filename];
              }
            }
          }
        } catch (e) {
          console.warn('[normalization] Failed for', p, e);
        }
      }
      await renderScenes();
    }

    /** Show or hide the Save/Revert wrap based on whether auto-save is active. */
    function _updateSaveRevertVisibility() {
      const wrap = document.getElementById('saveRevertWrap');
      if (!wrap) return;
      if (_autoSaveEnabled) {
        wrap.classList.add('hidden');
      } else {
        wrap.classList.remove('hidden');
      }
    }

    /** Mark settings Save button dirty (yellow) or clean. */
    function _setSettingsDirty(dirty) {
      const btn = document.getElementById('settingsSave');
      if (!btn) return;
      if (dirty) btn.classList.add('dirty'); else btn.classList.remove('dirty');
    }

    // Track changes inside the settings dialog to highlight the Save button
    document.getElementById('settingsDlg').addEventListener('change', () => _setSettingsDirty(true));
    document.getElementById('settingsDlg').addEventListener('input', () => _setSettingsDirty(true));

    document.getElementById('openSettings').addEventListener('click', () => { void showSettings(); });
    document.getElementById('settingsSave').addEventListener('click', async () => {
      await applySettings();
      _setSettingsDirty(false);
    });
    document.getElementById('settingsCancel').addEventListener('click', () => {
      document.getElementById('settingsDlg').close();
      _setSettingsDirty(false);
    });

    // ── Sidebar resize ────────────────────────────────────────────────────────
    (function initSidebarResize() {
      const resizer = document.getElementById('sidebarResizer');
      const sidebar = document.querySelector('header');
      if (!resizer || !sidebar) return;

      let dragging = false;
      let startX = 0;
      let startW = 0;

      resizer.addEventListener('mousedown', (e) => {
        dragging = true;
        startX = e.clientX;
        startW = sidebar.getBoundingClientRect().width;
        resizer.classList.add('dragging');
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
        e.preventDefault();
      });

      document.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        const delta = e.clientX - startX;
        const newW = Math.max(260, Math.min(600, startW + delta));
        sidebar.style.width = newW + 'px';
        sidebar.style.flex = '0 0 ' + newW + 'px';
      });

      document.addEventListener('mouseup', () => {
        if (!dragging) return;
        dragging = false;
        resizer.classList.remove('dragging');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
      });
    })();

    // ─── Telemetry helpers ────────────────────────────────────────────────────
    /** Merge a single key into persisted settings (localStorage + pywebview). */
    function mergeSetting(k, v) {
      const s = loadSettings();
      s[k] = v;
      saveSettings(s);
      if (hasPywebviewApi && window.pywebview?.api?.save_settings_data) {
        try { window.pywebview.api.save_settings_data(s); } catch (_) { }
      }
    }

