    // ── Folder "Change Settings" dialog ─────────────────────────────────────────
    // Opened from Folder Actions → Change Settings…. Gathers the things a user
    // can change on an analysed folder without re-analysing it:
    //   • folder info (read-only)             • ignore non-bird wildlife (display filter)
    //   • species detection region (re-score) • capture time    • scene grouping
    //   • reset culling decisions (destructive, separated at the bottom)
    // Built on demand (no static markup) and removed again on close.

    /** Fetch one folder's display settings into _folderSettings (called when folders load). */
    async function loadFolderSettingsFor(root) {
      if (!root) return;
      const api = window.pywebview?.api;
      if (!api?.get_folder_settings) return;
      try {
        const res = await api.get_folder_settings(root);
        _folderSettings[root] = res?.success ? (res.settings || {}) : {};
      } catch (_) {
        _folderSettings[root] = {};
      }
    }

    /** Drop per-row caches derived from crops_json so a changed wildlife filter takes effect. */
    function _fsetInvalidateRows(folderPath) {
      for (const r of rows) {
        if (r.__rootPath === folderPath) _invalidateRowCropCache(r);
      }
    }

    /** Folder counts the dialog needs from the loaded rows (scenes, reviewed, cull decisions). */
    function _fsetLoadedCounts(folderPath) {
      const folderRows = rows.filter(r => r.__rootPath === folderPath);
      if (!folderRows.length) return null;
      const groups = new Map();
      for (const r of folderRows) {
        const sc = String(r.scene_count);
        if (!groups.has(sc)) groups.set(sc, []);
        groups.get(sc).push(r);
      }
      let reviewed = 0;
      for (const [sc, arr] of groups) {
        const sd = _scenedata[folderPath]?.scenes?.[sc];
        const scene = {
          images: arr,
          isApproved: !!sd?.user_tags?.finalized,
          sceneName: sd?.name || (arr.find(a => (a.scene_name || '').trim())?.scene_name || '').trim(),
        };
        if (isManuallyReviewedScene(scene)) reviewed++;
      }
      const cull = _cullCounts(folderRows);
      return { images: folderRows.length, scenes: groups.size, reviewed, ...cull };
    }

    function _fsetFmtDate(isoOrMs) {
      const d = new Date(isoOrMs);
      if (Number.isNaN(d.getTime())) return '';
      return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
    }

    function _fsetPipelineText(info) {
      if ((Number(info.pipeline_major) || 0) >= 3) return '3.0';
      return info.pipeline_version || 'older than 2.0';
    }

    function _fsetRegionText(codes) {
      if (!Array.isArray(codes) || !codes.length) return 'Not recorded';
      if (codes.some(c => String(c).toLowerCase() === 'global')) return 'Global (all world birds)';
      const meta = (typeof _birdCatalogMeta !== 'undefined' && _birdCatalogMeta) || null;
      return codes.map(c => (meta?.regions || []).find(r => r.code === c)?.label || c).join(', ');
    }

    function _fsetRow(label, valueHtml) {
      return `<div class="fset-k">${escapeHtml(label)}</div><div class="fset-v">${valueHtml}</div>`;
    }

    async function openFolderSettingsDialog(folderPath) {
      if (!folderPath) { showToast('No folder selected', 2500); return; }
      const api = window.pywebview?.api;
      const folderName = folderBaseName(folderPath) || folderPath;
      document.getElementById('folderSettingsDlg')?.remove();

      const dlg = document.createElement('dialog');
      dlg.id = 'folderSettingsDlg';
      dlg.className = 'kdlg kdlg--md kdlg--flex fset-dlg';
      dlg.setAttribute('aria-labelledby', 'fsetTitle');
      dlg.innerHTML = `
        <div class="fset-head">
          <h3 id="fsetTitle">Change Settings</h3>
          <div class="fset-sub muted" title="${escapeHtml(folderPath)}">${escapeHtml(folderName)}</div>
        </div>
        <div class="fset-body">
          <section class="fset-sec">
            <h4>Folder info</h4>
            <div class="fset-grid" id="fsetInfo"><div class="fset-k">Loading…</div><div class="fset-v"></div></div>
          </section>
          <section class="fset-sec">
            <h4>Ignore wildlife</h4>
            <label class="fset-toggle">
              <input type="checkbox" id="fsetIgnoreWildlife" disabled />
              <span>Hide non-bird wildlife in this folder</span>
            </label>
            <div class="fset-hint muted" id="fsetWildlifeHint">Only changes what Kestrel shows. The detections stay saved, so you can turn this off again.</div>
          </section>
          <section class="fset-sec">
            <h4>Species detection region</h4>
            <div class="fset-hint muted" id="fsetRegionNow"></div>
            <div class="fset-region" id="fsetRegionBlock">
              <select id="fsetRegionMode" aria-label="Species region"></select>
              <div class="fset-region-grid hidden" id="fsetRegionCustom" role="group" aria-label="Species regions"></div>
              <div class="fset-region-actions">
                <button type="button" id="fsetRegionApply">Re-score species</button>
              </div>
              <div class="fset-progress hidden" id="fsetRegionProgress"><div class="fset-progress-fill" id="fsetRegionFill"></div></div>
            </div>
            <div class="fset-hint muted" id="fsetRegionHint">Re-scores the species names from data saved during analysis. Photos are not re-analyzed, and your reviewed scene tags are kept.</div>
          </section>
          <section class="fset-sec fset-sec--row">
            <div>
              <h4>Capture time</h4>
              <div class="fset-hint muted">Shift every photo's time if the camera clock was wrong.</div>
            </div>
            <button type="button" id="fsetCaptureTime">Adjust Capture Time…</button>
          </section>
          <section class="fset-sec fset-sec--row">
            <div>
              <h4>Scene grouping</h4>
              <div class="fset-hint muted">Change how photos are split into scenes.</div>
            </div>
            <button type="button" id="fsetRegroup">Regroup Scenes…</button>
          </section>
          <section class="fset-sec fset-sec--danger">
            <h4>Reset culling decisions</h4>
            <div class="fset-hint muted">Returns photos to Undecided. Star ratings and tags are not changed.</div>
            <div class="fset-danger-actions">
              <button type="button" id="fsetResetVerified">Reset Confirmed Decisions</button>
              <button type="button" id="fsetResetAll" class="fset-danger-btn">Reset All Decisions…</button>
            </div>
          </section>
        </div>
        <div class="kdlg-footer"><button type="button" id="fsetClose">Close</button></div>
      `;
      document.body.appendChild(dlg);
      const $ = (sel) => dlg.querySelector(sel);
      const closeDlg = () => { try { dlg.close(); } catch (_) { } dlg.remove(); };
      dlg.addEventListener('close', () => dlg.remove());
      $('#fsetClose').addEventListener('click', closeDlg);
      dlg.showModal();

      // ── Capture time / regroup: hand over to their own dialogs ──────────────
      $('#fsetCaptureTime').addEventListener('click', () => { closeDlg(); showAdjustCaptureTimeDialog(folderPath); });
      $('#fsetRegroup').addEventListener('click', () => { closeDlg(); openRegroupDialog(folderPath); });

      // ── Reset culling decisions ─────────────────────────────────────────────
      const afterReset = (changed, what) => {
        showToast(changed > 0 ? `Reset ${changed} ${what} decision${changed === 1 ? '' : 's'}` : `No ${what} decisions to reset`, 3000);
        renderInfo();
      };
      $('#fsetResetVerified').addEventListener('click', () => {
        afterReset(resetFolderCullState(folderPath, 'verified'), 'confirmed');
      });
      $('#fsetResetAll').addEventListener('click', () => {
        if (!confirm(`Reset ALL manual and confirmed culling decisions for "${folderName}"?\n\nThis cannot be undone.`)) return;
        afterReset(resetFolderCullState(folderPath, 'all'), 'manual/confirmed');
      });

      // ── Folder info ─────────────────────────────────────────────────────────
      let info = null;
      let perchLink = null;
      const renderInfo = () => {
        const counts = _fsetLoadedCounts(folderPath);
        const parts = [_fsetRow('Folder', `<span class="fset-path">${escapeHtml(folderPath)}</span>`)];
        const imgs = counts ? counts.images : info?.image_count;
        const scs = counts ? counts.scenes : info?.scene_count;
        if (imgs != null) parts.push(_fsetRow('Images', Number(imgs).toLocaleString()));
        if (scs != null) parts.push(_fsetRow('Scenes', Number(scs).toLocaleString()));
        if (counts) {
          parts.push(_fsetRow('Scenes reviewed', `${counts.reviewed.toLocaleString()} of ${counts.scenes.toLocaleString()}`));
          parts.push(_fsetRow('Culling', `${counts.accepted.toLocaleString()} accepted · ${counts.rejected.toLocaleString()} rejected · ${counts.undecided.toLocaleString()} undecided`));
        }
        if (info) {
          parts.push(_fsetRow('Analysis pipeline', escapeHtml(_fsetPipelineText(info))));
          const when = info.analyzed_utc ? _fsetFmtDate(info.analyzed_utc) : '';
          if (when) parts.push(_fsetRow('Analyzed', escapeHtml(when)));
        }
        if (perchLink) {
          const url = String(perchLink.perch_url || '');
          const published = Number(perchLink.uploaded_at_ms) > 0 ? ` · published ${escapeHtml(_fsetFmtDate(Number(perchLink.uploaded_at_ms)))}` : '';
          parts.push(_fsetRow('Perch', `${escapeHtml(String(perchLink.title || 'Shared'))}${published}` +
            (url ? ` <button type="button" class="fset-link" id="fsetPerchOpen">Open on Perch</button>` : '')));
        }
        $('#fsetInfo').innerHTML = parts.join('');
        const openBtn = $('#fsetPerchOpen');
        if (openBtn) openBtn.addEventListener('click', () => { try { api.open_perch_url(String(perchLink.perch_url)); } catch (_) { } });
      };
      renderInfo();

      try { if (typeof loadSpeciesFamilyMap === 'function') await loadSpeciesFamilyMap(); } catch (_) { }
      try {
        const [infoRes, linkRes] = await Promise.all([
          api?.get_folder_info ? api.get_folder_info(folderPath) : null,
          api?.read_perch_link ? api.read_perch_link(folderPath) : null,
        ]);
        if (infoRes?.success) info = infoRes.info;
        if (linkRes?.present && linkRes.link) perchLink = linkRes.link;
      } catch (e) { console.warn('[folder-settings] info load failed', e); }
      if (!dlg.isConnected) return;
      renderInfo();
      if (!info) {
        $('#fsetWildlifeHint').textContent = 'Could not read this folder\'s data.';
        $('#fsetRegionApply').disabled = true;
        return;
      }

      // ── Ignore wildlife ─────────────────────────────────────────────────────
      const wl = $('#fsetIgnoreWildlife');
      wl.checked = !!_folderSettings[folderPath]?.ignore_wildlife;
      if (info.wildlife_crops > 0 || wl.checked) {
        wl.disabled = false;
        $('#fsetWildlifeHint').textContent = `${info.wildlife_crops.toLocaleString()} wildlife detection${info.wildlife_crops === 1 ? '' : 's'} in this folder. ` +
          'Turning this on only changes what Kestrel shows; the detections stay saved.';
      } else {
        $('#fsetWildlifeHint').textContent = info.wildlife_enabled
          ? 'No non-bird wildlife was detected in this folder.'
          : 'This folder was analyzed with wildlife detection off, so there is nothing to hide.';
      }
      wl.addEventListener('change', async () => {
        const want = wl.checked;
        wl.disabled = true;
        let res = null;
        try { res = await api.set_folder_setting(folderPath, 'ignore_wildlife', want); } catch (e) { res = { success: false, error: String(e) }; }
        wl.disabled = false;
        if (!res?.success) {
          wl.checked = !want;
          showToast(`Could not save: ${res?.error || 'unknown error'}`, 5000);
          return;
        }
        _folderSettings[folderPath] = res.settings || { ignore_wildlife: want };
        _fsetInvalidateRows(folderPath);
        try { await renderScenes(); } catch (_) { }
        showToast(want ? 'Hiding non-bird wildlife in this folder' : 'Showing non-bird wildlife in this folder', 3000);
      });

      // ── Species detection region ────────────────────────────────────────────
      $('#fsetRegionNow').textContent = `Current region: ${_fsetRegionText(info.species_regions)}`;
      _fsetBuildRegionPicker(dlg, info);
      const applyBtn = $('#fsetRegionApply');
      if (!info.rescore_supported) {
        $('#fsetRegionBlock').classList.add('is-disabled');
        for (const c of dlg.querySelectorAll('#fsetRegionBlock select, #fsetRegionBlock input, #fsetRegionBlock button')) c.disabled = true;
        $('#fsetRegionHint').textContent = info.rescore_reason || 'Not available for this folder.';
        $('#fsetRegionHint').classList.add('fset-hint--warn');
      }
      applyBtn.addEventListener('click', () => _fsetApplyRegions(dlg, folderPath, folderName, info));
    }

    function _fsetBuildRegionPicker(dlg, info) {
      const sel = dlg.querySelector('#fsetRegionMode');
      const custom = dlg.querySelector('#fsetRegionCustom');
      const meta = (typeof _birdCatalogMeta !== 'undefined' && _birdCatalogMeta) || { regions: [], region_counts: {}, total_species: 0 };
      const counts = meta.region_counts || {};
      const regions = (Array.isArray(meta.regions) ? meta.regions : []).filter(r => r && r.code && r.code !== 'Worldwide');
      const current = Array.isArray(info.species_regions) ? info.species_regions : [];
      const isGlobal = current.some(c => String(c).toLowerCase() === 'global');
      // No recorded region (e.g. a pre-3.0 folder): don't pre-select an arbitrary one.
      let html = current.length ? '' : '<option value="" disabled selected>Choose a region…</option>';
      if (regions.length) {
        html += '<optgroup label="One region">';
        for (const r of regions) {
          const c = counts[r.code] > 0 ? ` — ${Number(counts[r.code]).toLocaleString()} species` : '';
          html += `<option value="region:${escapeHtml(r.code)}">${escapeHtml(r.label)}${c}</option>`;
        }
        html += '</optgroup><option value="custom">Several regions…</option>';
      }
      const total = meta.total_species > 0 ? ` (${Number(meta.total_species).toLocaleString()})` : '';
      html += `<option value="global">Global — all world birds${total}</option>`;
      sel.innerHTML = html;
      custom.innerHTML = regions.map(r =>
        `<label title="${escapeHtml(r.label)}"><input type="checkbox" class="fset-region-cb" data-region-code="${escapeHtml(r.code)}"` +
        ` ${current.includes(r.code) ? 'checked' : ''} /><span>${escapeHtml(r.label)}</span></label>`).join('');
      if (isGlobal) sel.value = 'global';
      else if (current.length === 1 && regions.some(r => r.code === current[0])) sel.value = `region:${current[0]}`;
      else if (current.length > 1) sel.value = 'custom';
      const sync = () => custom.classList.toggle('hidden', sel.value !== 'custom');
      sel.addEventListener('change', sync);
      sync();
    }

    function _fsetSelectedRegions(dlg) {
      const mode = dlg.querySelector('#fsetRegionMode').value;
      if (mode === 'global') return ['global'];
      if (mode.startsWith('region:')) return [mode.slice('region:'.length)];
      return Array.from(dlg.querySelectorAll('#fsetRegionCustom .fset-region-cb'))
        .filter(cb => cb.checked).map(cb => cb.dataset.regionCode).filter(Boolean);
    }

    async function _fsetApplyRegions(dlg, folderPath, folderName, info) {
      const api = window.pywebview?.api;
      const regions = _fsetSelectedRegions(dlg);
      if (!regions.length) { showToast('Pick at least one region', 3000); return; }
      const label = _fsetRegionText(regions);
      if (!confirm(`Re-score species in "${folderName}" for: ${label}?\n\n` +
        'Kestrel reuses the data saved during analysis, so photos are not re-analyzed. ' +
        'Species you edited by hand and non-bird wildlife are not changed.')) return;

      const btn = dlg.querySelector('#fsetRegionApply');
      const bar = dlg.querySelector('#fsetRegionProgress');
      const fill = dlg.querySelector('#fsetRegionFill');
      const hint = dlg.querySelector('#fsetRegionHint');
      btn.disabled = true;
      // The backend rewrites the CSV; flush pending edits first so a later save
      // cannot write the old species back.
      try { if (dirty && typeof saveCsv === 'function') await saveCsv(); } catch (_) { }
      let res = null;
      try { res = await api.rescore_folder_species_regions(folderPath, regions); } catch (e) { res = { success: false, error: String(e) }; }
      if (!res?.success) {
        btn.disabled = false;
        showToast(res?.error || 'Could not start re-scoring', 6000);
        return;
      }
      bar.classList.remove('hidden');
      hint.textContent = 'Re-scoring species…';
      let st = null;
      for (;;) {
        await new Promise(r => setTimeout(r, 400));
        try { st = await api.get_species_rescore_status(folderPath); } catch (_) { st = null; }
        if (!st?.success) break;
        if (st.total > 0) fill.style.width = `${Math.round((st.done / st.total) * 100)}%`;
        if (st.state !== 'running') break;
      }
      bar.classList.add('hidden');
      fill.style.width = '0%';
      btn.disabled = false;
      if (st?.state === 'done') {
        const s = st.stats || {};
        const msg = `Species re-scored for ${label}: ${Number(s.changed || 0).toLocaleString()} name${s.changed === 1 ? '' : 's'} changed across ${Number(s.images || 0).toLocaleString()} image${s.images === 1 ? '' : 's'}` +
          (s.locked ? `; your tags on reviewed scenes were kept (${Number(s.locked).toLocaleString()} image${s.locked === 1 ? '' : 's'})` : '') + '.';
        showToast(msg, 7000);
        info.species_regions = s.regions || regions;
        if (dlg.isConnected) {
          dlg.querySelector('#fsetRegionNow').textContent = `Current region: ${_fsetRegionText(info.species_regions)}`;
          hint.textContent = msg;
        }
        try { if (typeof reloadCurrentFolders === 'function') await reloadCurrentFolders(); } catch (e) { console.warn('[folder-settings] reload failed', e); }
      } else {
        const err = st?.error || 'unknown error';
        hint.textContent = `Re-scoring failed: ${err}`;
        showToast(`Re-scoring failed: ${err}`, 7000);
      }
    }
