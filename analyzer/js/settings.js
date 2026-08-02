    const SETTINGS_KEY = 'kestrel-webviz-settings-v1';
    let _autoSaveEnabled = true;  // cached value to avoid repeated lookups
    let _autoSaveTimer = null;     // debounce timer for auto-saves
    
    function loadSettings() {
      try { return JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {}; } catch { return {}; }
    }
    function saveSettings(obj) { localStorage.setItem(SETTINGS_KEY, JSON.stringify(obj || {})); }
    function getSetting(k, def) { const s = loadSettings(); return (k in s) ? s[k] : def; }

    // ── Custom rating profile ─────────────────────────────────────────────────
    // The 'custom' profile's cutoffs live in the rating_thresholds_custom
    // setting instead of RATING_PROFILES. This editor is the only place they
    // can be edited; kestrel_analyzer/ratings.py normalizes whatever it is
    // handed, so the UI is free to be the convenient layer rather than the
    // authoritative one.
    const RATING_THRESHOLD_KEYS = ['five', 'four', 'three', 'two'];  // high → low
    const RATING_MIN_GAP = 0.01;   // must match ratings.MIN_THRESHOLD_GAP
    const RATING_BALANCED = { five: 0.85, four: 0.60, three: 0.40, two: 0.15 };
    let _customThresholds = { ...RATING_BALANCED };

    function _round2(v) { return Math.round(v * 100) / 100; }

    // Mirror of ratings.normalize_custom_thresholds: clamp to [0,1], then force
    // strictly descending so no star band collapses to nothing.
    function normalizeCustomThresholds(raw) {
      const src = (raw && typeof raw === 'object') ? raw : {};
      const out = {};
      for (const k of RATING_THRESHOLD_KEYS) {
        // Only numbers and numeric strings count as supplied. Number(null) and
        // Number('') are 0 and Number(true) is 1, which would silently become
        // real cutoffs here while Python's float() rejects them — the two
        // normalizers have to agree or the UI shows bands the backend won't use.
        const rawV = src[k];
        const usable =
          typeof rawV === 'number' ||
          (typeof rawV === 'string' && rawV.trim() !== '');
        const v = usable ? Number(rawV) : NaN;
        out[k] = Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : RATING_BALANCED[k];
      }
      let ceiling = 1;
      for (const k of RATING_THRESHOLD_KEYS) {
        out[k] = _round2(Math.min(out[k], ceiling));
        ceiling = _round2(out[k] - RATING_MIN_GAP);
      }
      let floor = 0;
      for (const k of [...RATING_THRESHOLD_KEYS].reverse()) {
        if (out[k] < floor) out[k] = _round2(floor);
        floor = _round2(out[k] + RATING_MIN_GAP);
      }
      return out;
    }

    function _isCustomProfileSelected() {
      return document.getElementById('ratingProfile')?.value === 'custom';
    }

    // Show the editor only for the Custom profile. Selecting a named profile
    // seeds the custom values from it, so switching to Custom starts from
    // whatever the user was already using rather than from balanced.
    function refreshRatingProfileEditor(seedFromProfile = null) {
      const editor = document.getElementById('ratingCustomEditor');
      if (!editor) return;
      const custom = _isCustomProfileSelected();
      editor.classList.toggle('hidden', !custom);
      if (!custom && seedFromProfile) {
        const t = _ratingProfileTable[seedFromProfile];
        if (t) _customThresholds = normalizeCustomThresholds(t);
      }
      if (custom) renderCustomThresholds();
    }

    // Built-in profiles, filled in from the bridge so the numbers are not
    // duplicated here. Falls back to balanced-only if the call fails.
    let _ratingProfileTable = { balanced: { ...RATING_BALANCED } };

    async function loadRatingProfileTable() {
      try {
        const res = await window.pywebview?.api?.get_rating_thresholds?.();
        if (res?.success && res.profiles && Object.keys(res.profiles).length) {
          _ratingProfileTable = res.profiles;
        }
      } catch (e) { console.warn('loadRatingProfileTable:', e); }
    }

    function renderCustomThresholds() {
      const bands = document.getElementById('ratingCustomBands');
      const handles = document.getElementById('ratingCustomHandles');
      const readout = document.getElementById('ratingCustomReadout');
      if (!bands || !handles) return;
      const t = _customThresholds;

      // Bands run ★1 from 0 up to the two-star cutoff, then each cutoff to the
      // next, with ★5 running to 1.00.
      const edges = [0, t.two, t.three, t.four, t.five, 1];
      bands.innerHTML = '';
      for (let star = 1; star <= 5; star++) {
        const from = edges[star - 1];
        const to = edges[star];
        const width = Math.max(0, to - from);
        const band = document.createElement('div');
        band.className = 'rating-custom-band' + (width < 0.07 ? ' narrow' : '');
        band.dataset.star = String(star);
        band.style.left = (from * 100) + '%';
        band.style.width = (width * 100) + '%';
        band.textContent = '★' + star;
        bands.appendChild(band);
      }

      handles.innerHTML = '';
      for (const key of RATING_THRESHOLD_KEYS) {
        const star = { five: 5, four: 4, three: 3, two: 2 }[key];
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'rating-custom-handle';
        btn.dataset.thresholdKey = key;
        btn.style.left = (t[key] * 100) + '%';
        btn.setAttribute('role', 'slider');
        btn.setAttribute('aria-label', `Minimum quality score for ${star} stars`);
        btn.setAttribute('aria-valuemin', '0');
        btn.setAttribute('aria-valuemax', '1');
        btn.setAttribute('aria-valuenow', t[key].toFixed(2));
        btn.title = `★${star} starts at ${t[key].toFixed(2)}`;
        const label = document.createElement('span');
        label.textContent = '★' + star;
        btn.appendChild(label);
        handles.appendChild(btn);
      }

      if (readout) {
        readout.textContent = RATING_THRESHOLD_KEYS
          .map(k => `★${{ five: 5, four: 4, three: 3, two: 2 }[k]} ≥ ${t[k].toFixed(2)}`)
          .join('   ·   ');
      }
    }

    // Move one cutoff, pushing its neighbours out of the way rather than
    // letting bands collapse. Returns the normalized set.
    function setCustomThreshold(key, value) {
      const next = { ..._customThresholds, [key]: Math.max(0, Math.min(1, Number(value) || 0)) };
      const idx = RATING_THRESHOLD_KEYS.indexOf(key);
      // Push higher stars up if this one crossed them.
      for (let i = idx - 1; i >= 0; i--) {
        const above = RATING_THRESHOLD_KEYS[i];
        const minAbove = _round2(next[RATING_THRESHOLD_KEYS[i + 1]] + RATING_MIN_GAP);
        if (next[above] < minAbove) next[above] = minAbove;
      }
      // Push lower stars down likewise.
      for (let i = idx + 1; i < RATING_THRESHOLD_KEYS.length; i++) {
        const below = RATING_THRESHOLD_KEYS[i];
        const maxBelow = _round2(next[RATING_THRESHOLD_KEYS[i - 1]] - RATING_MIN_GAP);
        if (next[below] > maxBelow) next[below] = maxBelow;
      }
      _customThresholds = normalizeCustomThresholds(next);
      renderCustomThresholds();
      return _customThresholds;
    }

    function wireCustomThresholdEditor() {
      const track = document.getElementById('ratingCustomTrack');
      const handles = document.getElementById('ratingCustomHandles');
      if (!track || !handles || handles.dataset.wired) return;
      handles.dataset.wired = '1';

      let dragKey = null;

      const positionFromEvent = (ev) => {
        const rect = track.getBoundingClientRect();
        if (!rect.width) return 0;
        return Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width));
      };

      handles.addEventListener('pointerdown', (ev) => {
        const btn = ev.target.closest('.rating-custom-handle');
        if (!btn) return;
        dragKey = btn.dataset.thresholdKey;
        btn.classList.add('dragging');
        // Capture on the container: re-rendering replaces the button mid-drag,
        // so the original element would stop receiving moves.
        handles.setPointerCapture(ev.pointerId);
        ev.preventDefault();
      });

      handles.addEventListener('pointermove', (ev) => {
        if (!dragKey) return;
        setCustomThreshold(dragKey, _round2(positionFromEvent(ev)));
      });

      const endDrag = (ev) => {
        if (!dragKey) return;
        dragKey = null;
        handles.querySelectorAll('.dragging').forEach(e => e.classList.remove('dragging'));
        try { handles.releasePointerCapture(ev.pointerId); } catch (_) {}
        if (typeof attemptAutoSave === 'function') attemptAutoSave();
      };
      handles.addEventListener('pointerup', endDrag);
      handles.addEventListener('pointercancel', endDrag);

      handles.addEventListener('keydown', (ev) => {
        const btn = ev.target.closest('.rating-custom-handle');
        if (!btn) return;
        const step = ev.shiftKey ? 0.05 : 0.01;
        const key = btn.dataset.thresholdKey;
        let delta = 0;
        if (ev.key === 'ArrowLeft' || ev.key === 'ArrowDown') delta = -step;
        else if (ev.key === 'ArrowRight' || ev.key === 'ArrowUp') delta = step;
        else if (ev.key === 'Home') delta = -1;
        else if (ev.key === 'End') delta = 1;
        else return;
        ev.preventDefault();
        setCustomThreshold(key, _round2(_customThresholds[key] + delta));
        // Keep focus on the same star after the re-render.
        document.querySelector(`.rating-custom-handle[data-threshold-key="${key}"]`)?.focus();
      });

      document.getElementById('ratingCustomReset')?.addEventListener('click', () => {
        _customThresholds = normalizeCustomThresholds(
          _ratingProfileTable.balanced || RATING_BALANCED
        );
        renderCustomThresholds();
      });

      document.getElementById('ratingProfile')?.addEventListener('change', (ev) => {
        refreshRatingProfileEditor(ev.target.value);
      });
    }
    
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
      // Custom thresholds: seed from the saved set, falling back to the active
      // named profile so switching to Custom starts where the user already was.
      await loadRatingProfileTable();
      const savedCustom = getSetting('rating_thresholds_custom', null);
      _customThresholds = normalizeCustomThresholds(
        savedCustom || _ratingProfileTable[getSetting('rating_profile', 'balanced')] || RATING_BALANCED
      );
      wireCustomThresholdEditor();
      refreshRatingProfileEditor();
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

      // Preview exposure-compensation strength — one slider shared with the
      // scene viewer (see scene-zoom.js). Mirror the current value and drive the
      // same apply path live so both stay in sync.
      const expStrengthSlider = document.getElementById('settingsExpStrengthSlider');
      if (expStrengthSlider) {
        if (typeof syncSettingsExposureStrengthSlider === 'function') {
          syncSettingsExposureStrengthSlider();
        }
        if (!expStrengthSlider.dataset.wired) {
          expStrengthSlider.dataset.wired = '1';
          expStrengthSlider.addEventListener('input', () => {
            if (typeof applyExposurePreviewStrengthPct === 'function') {
              applyExposurePreviewStrengthPct(parseFloat(expStrengthSlider.value));
            }
          });
        }
      }

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
      // Editing custom thresholds changes every auto rating without changing
      // the profile name, so compare the cutoffs too.
      const prevCustom = JSON.stringify(normalizeCustomThresholds(existing.rating_thresholds_custom));
      const nextCustom = JSON.stringify(normalizeCustomThresholds(_customThresholds));

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
        rating_thresholds_custom: normalizeCustomThresholds(_customThresholds),
        raw_preview_cache_enabled: rawPreviewCacheEnabled,
        auto_save_enabled: autoSaveEnabled,
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
      const thresholdsChanged =
        ratingProfile !== prevProfile ||
        (ratingProfile === 'custom' && nextCustom !== prevCustom);
      if (thresholdsChanged && rows.length > 0) {
        await reapplyNormalizationForLoadedFolders();
      }
      // Preview exposure-compensation strength is applied live by the shared
      // slider (applyExposurePreviewStrengthPct → refreshManagedExposurePreviews),
      // so no thumbnail re-render is needed here on save.
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

