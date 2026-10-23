    // ---------------------------------------------------------------------
    // Regroup Scenes — re-scene an already-analyzed folder without re-running
    // analysis.
    //
    // The pipeline writes every value its scene-boundary decisions were based
    // on (capture_time, orientation, the raw similarity scores) into
    // kestrel_database.csv, so the boundaries can be recomputed from the
    // database alone. The rules themselves live in Python
    // (kestrel_analyzer/regroup.py) so they can be unit-tested against the
    // pipeline's own ladder; this file is the dialog and the apply step.
    //
    // Because it works off stored data, it does not care where the analysis
    // ran: a cloud-analyzed folder regroups exactly like a local one.
    // ---------------------------------------------------------------------

    let _regroupFolder = null;
    let _regroupPreview = null;      // last preview payload from the bridge
    let _regroupPrepared = null;     // prepare() response for the open folder
    let _regroupPreviewSeq = 0;      // drops out-of-order preview responses
    let _regroupTimer = null;
    let _regroupBusy = false;

    // The columns the rules read. Kept in one place so the payload shipped to
    // the bridge stays minimal — this is sent once per dialog open, and a
    // 20k-image folder is already ~1MB at this width.
    const REGROUP_COLUMNS = [
      'filename', 'capture_time', 'orientation',
      'feature_similarity', 'feature_confidence',
      'color_similarity', 'color_confidence', 'scene_count',
    ];

    function _regroupEl(id) { return document.getElementById(id); }

    function _regroupControls() {
      const groupWithin = parseFloat(_regroupEl('regroupGroupWithin')?.value);
      const splitAfter = parseFloat(_regroupEl('regroupSplitAfter')?.value);
      const similarity = parseFloat(_regroupEl('regroupSimilarity')?.value);
      return {
        group_within_seconds: Number.isFinite(groupWithin) ? Math.max(0, groupWithin) : 1.0,
        split_after_seconds: Number.isFinite(splitAfter) ? Math.max(0, splitAfter) : 0,
        similarity_threshold: Number.isFinite(similarity) ? Math.max(0, Math.min(1, similarity)) : 0.5,
      };
    }

    function _regroupFormatDuration(seconds) {
      if (!Number.isFinite(seconds) || seconds <= 0) return 'off';
      if (seconds < 60) return `${Math.round(seconds)}s`;
      if (seconds < 3600) {
        const m = seconds / 60;
        return `${Number.isInteger(m) ? m : m.toFixed(1)} min`;
      }
      const h = seconds / 3600;
      return `${Number.isInteger(h) ? h : h.toFixed(1)} h`;
    }

    const _REGROUP_REASON_LABEL = {
      'first': 'first image',
      'orientation': 'camera rotated',
      'time-gap': 'long pause',
      'appearance': 'looks different',
    };

    function _regroupSimilarityNote(control) {
      if (control <= 0.001) return 'Never split on appearance — only time and rotation start a new scene.';
      if (Math.abs(control - 0.5) < 0.001) return 'As analyzed — reproduces the grouping this folder already has.';
      if (control < 0.5) return 'More forgiving than the analysis — scenes will tend to join up.';
      return 'Stricter than the analysis — images must look more alike to stay in one scene.';
    }

    /** Rows for one folder, in the order the pipeline walked them. */
    function _regroupRowsForFolder(folderPath) {
      return rows.filter(r => r.__rootPath === folderPath);
    }

    async function openRegroupDialog(folderPath) {
      if (!hasPywebviewApi) {
        showToast('Regrouping scenes requires desktop mode', 4000);
        return;
      }
      const dlg = _regroupEl('regroupDlg');
      if (!dlg) return;

      const folderRows = _regroupRowsForFolder(folderPath);
      if (folderRows.length === 0) {
        showToast('No analyzed images in this folder to regroup', 4000);
        return;
      }

      _regroupFolder = folderPath;
      _regroupPreview = null;
      _regroupPrepared = null;
      _regroupBusy = false;

      const nameEl = _regroupEl('regroupFolderName');
      if (nameEl) nameEl.textContent = folderBaseName(folderPath);
      const applyBtn = _regroupEl('regroupApplyBtn');
      if (applyBtn) applyBtn.disabled = true;
      _regroupEl('regroupSummary').textContent = 'Reading analysis data…';
      _regroupEl('regroupCurrentList').innerHTML = '';
      _regroupEl('regroupProposedList').innerHTML = '';
      _regroupEl('regroupScoredNote').textContent = '';
      dlg.showModal();

      // Ship only the columns the rules read, as plain values — the in-memory
      // rows carry blobs (crops_json, detection_scores) that would bloat the
      // bridge payload by an order of magnitude for no benefit.
      const payload = folderRows.map(r => {
        const out = {};
        for (const col of REGROUP_COLUMNS) out[col] = r[col] ?? '';
        return out;
      });

      try {
        const res = await window.pywebview.api.regroup_scenes_prepare(folderPath, payload);
        if (!res || !res.success) {
          showToast('Could not read this folder for regrouping: ' + ((res && res.error) || 'unknown error'), 5000);
          closeRegroupDialog();
          return;
        }
        _regroupPrepared = res;

        // Open on the settings this folder was analyzed with, so the first
        // preview reproduces its current grouping and any difference the user
        // then sees is a difference they asked for.
        const gw = _regroupEl('regroupGroupWithin');
        if (gw) gw.value = String(res.analyzed_group_within ?? 1.0);
        const sa = _regroupEl('regroupSplitAfter');
        if (sa) sa.value = String(res.analyzed_split_after ?? 0);
        const sim = _regroupEl('regroupSimilarity');
        if (sim) sim.value = '0.5';

        const scoreNote = _regroupEl('regroupScoredNote');
        if (scoreNote) {
          const scored = res.scored_pairs || 0;
          const total = scored + (res.unscored_pairs || 0);
          scoreNote.textContent = total === 0
            ? ''
            : `Appearance was measured for ${scored} of ${total} image pairs. ` +
              `The rest were grouped by the burst rule during analysis and carry no score, ` +
              `so only the time settings can separate them.`;
        }
        _regroupRefreshPreview(true);
      } catch (e) {
        console.error('[regroup] prepare failed', e);
        showToast('Could not start regrouping', 4000);
        closeRegroupDialog();
      }
    }

    function closeRegroupDialog() {
      const dlg = _regroupEl('regroupDlg');
      if (dlg && dlg.open) dlg.close();
      _regroupFolder = null;
      _regroupPreview = null;
      _regroupPrepared = null;
      clearTimeout(_regroupTimer);
      _regroupTimer = null;
      try { window.pywebview?.api?.regroup_scenes_release?.(); } catch (_) {}
    }

    function _regroupSchedulePreview() {
      clearTimeout(_regroupTimer);
      _regroupTimer = setTimeout(() => _regroupRefreshPreview(false), 160);
    }

    async function _regroupRefreshPreview(immediate) {
      if (!_regroupFolder) return;
      const opts = _regroupControls();

      // Live labels update straight away even though the preview is debounced,
      // so dragging a slider never feels laggy.
      const splitLabel = _regroupEl('regroupSplitAfterVal');
      if (splitLabel) splitLabel.textContent = _regroupFormatDuration(opts.split_after_seconds);
      const simLabel = _regroupEl('regroupSimilarityVal');
      if (simLabel) simLabel.textContent = opts.similarity_threshold.toFixed(2);
      const simNote = _regroupEl('regroupSimilarityNote');
      if (simNote) simNote.textContent = _regroupSimilarityNote(opts.similarity_threshold);

      if (!immediate && _regroupBusy) { _regroupSchedulePreview(); return; }

      const seq = ++_regroupPreviewSeq;
      _regroupBusy = true;
      try {
        const res = await window.pywebview.api.regroup_scenes_preview(_regroupFolder, opts, false);
        // A slower earlier request must never overwrite a newer result.
        if (seq !== _regroupPreviewSeq) return;
        if (!res || !res.success) {
          _regroupEl('regroupSummary').textContent =
            'Preview failed: ' + ((res && res.error) || 'unknown error');
          return;
        }
        _regroupPreview = res;
        _regroupRenderPreview(res);
        const applyBtn = _regroupEl('regroupApplyBtn');
        if (applyBtn) applyBtn.disabled = !_regroupHasChange(res);
      } catch (e) {
        console.error('[regroup] preview failed', e);
      } finally {
        if (seq === _regroupPreviewSeq) _regroupBusy = false;
      }
    }

    /** True when the proposal differs from what the folder already has. */
    function _regroupHasChange(preview) {
      const cur = preview.current || [];
      const next = preview.proposed || [];
      if (cur.length !== next.length) return true;
      for (let i = 0; i < cur.length; i++) {
        if (cur[i].size !== next[i].size) return true;
      }
      return false;
    }

    function _regroupRenderSceneList(host, entries, opts) {
      host.innerHTML = '';
      if (!entries || entries.length === 0) {
        host.innerHTML = '<div class="regroup-empty muted">No scenes</div>';
        return;
      }
      // A folder can regroup into thousands of scenes; render a bounded window
      // and say how many were elided rather than building 5,000 DOM rows on
      // every slider tick.
      const LIMIT = 200;
      const shown = entries.slice(0, LIMIT);
      const frag = document.createDocumentFragment();
      for (const s of shown) {
        const row = document.createElement('div');
        row.className = 'regroup-scene-row';
        const reason = opts?.showReason ? (_REGROUP_REASON_LABEL[s.reason] || '') : '';
        row.innerHTML =
          `<span class="regroup-scene-idx">#${s.scene}</span>` +
          `<span class="regroup-scene-size">${s.size} photo${s.size === 1 ? '' : 's'}</span>` +
          `<span class="regroup-scene-range muted">${escapeHtml(s.first_filename || '')}` +
          `${s.size > 1 ? ' – ' + escapeHtml(s.last_filename || '') : ''}</span>` +
          (reason ? `<span class="regroup-scene-reason muted">${escapeHtml(reason)}</span>` : '');
        frag.appendChild(row);
      }
      host.appendChild(frag);
      if (entries.length > LIMIT) {
        const more = document.createElement('div');
        more.className = 'regroup-empty muted';
        more.textContent = `… and ${entries.length - LIMIT} more scenes`;
        host.appendChild(more);
      }
    }

    function _regroupRenderPreview(preview) {
      const stats = preview.stats || {};
      _regroupRenderSceneList(_regroupEl('regroupCurrentList'), preview.current, { showReason: false });
      _regroupRenderSceneList(_regroupEl('regroupProposedList'), preview.proposed, { showReason: true });

      _regroupEl('regroupCurrentHead').textContent =
        `Current — ${stats.current_scene_count || 0} scene${stats.current_scene_count === 1 ? '' : 's'}` +
        (stats.largest_current_scene ? ` (largest ${stats.largest_current_scene})` : '');
      _regroupEl('regroupProposedHead').textContent =
        `After regrouping — ${stats.proposed_scene_count || 0} scene${stats.proposed_scene_count === 1 ? '' : 's'}` +
        (stats.largest_proposed_scene ? ` (largest ${stats.largest_proposed_scene})` : '');

      const reasons = stats.breaks_by_reason || {};
      const bits = [];
      if (reasons['time-gap']) bits.push(`${reasons['time-gap']} from a long pause`);
      if (reasons['appearance']) bits.push(`${reasons['appearance']} from a change in appearance`);
      if (reasons['orientation']) bits.push(`${reasons['orientation']} from the camera rotating`);
      const delta = (stats.proposed_scene_count || 0) - (stats.current_scene_count || 0);
      const deltaText = delta === 0 ? 'Same number of scenes'
        : delta > 0 ? `${delta} more scene${delta === 1 ? '' : 's'}`
        : `${-delta} fewer scene${delta === -1 ? '' : 's'}`;
      _regroupEl('regroupSummary').textContent =
        `${preview.image_count} photos · ${deltaText}` +
        (bits.length ? ` · new scenes start: ${bits.join(', ')}` : '');
    }

    /**
     * Carry a folder's scenedata across a regroup.
     *
     * Scene names, verified tags and review status hang off the scene number,
     * and regrouping renumbers everything — so without an explicit migration
     * the save path would hand scene 3's verified species list to whatever
     * ends up numbered 3. The policy:
     *
     *   - A new scene holding exactly the same images as an old one inherits
     *     that scene's entry untouched.
     *   - A new scene that is a strict subset of ONE old scene (a pure split)
     *     inherits its name and tags, with tags pruned to the ones its own
     *     images still support — the same treatment the manual Split Scene
     *     action applies.
     *   - A new scene drawing from two or more old scenes starts fresh: the
     *     old labels contradict each other and guessing between them would
     *     silently mislabel photos.
     *
     * Per-image data (star ratings, accept/reject decisions) is keyed by
     * filename, not by scene, so it is untouched by any of this.
     */
    function _regroupMigrateScenedata(rootPath, folderRows, oldSceneByFilename, newSceneByFilename) {
      const sd = _initScenedata(rootPath);
      const oldScenes = sd.scenes || {};

      const rowsByFilename = new Map();
      for (const r of folderRows) rowsByFilename.set(r.filename || '', r);

      // Old scene -> how many of its images survive anywhere, so a subset can
      // be told apart from an exact match.
      const oldSizes = new Map();
      for (const [, oldScene] of oldSceneByFilename) {
        oldSizes.set(oldScene, (oldSizes.get(oldScene) || 0) + 1);
      }

      const newGroups = new Map();
      for (const [filename, newScene] of newSceneByFilename) {
        if (!newGroups.has(newScene)) newGroups.set(newScene, []);
        newGroups.get(newScene).push(filename);
      }

      let inherited = 0;
      let reset = 0;
      const migrated = {};
      for (const [newScene, filenames] of newGroups) {
        const key = String(newScene);
        const contributors = new Map();
        for (const fn of filenames) {
          const src = oldSceneByFilename.get(fn);
          if (src === undefined) continue;
          contributors.set(src, (contributors.get(src) || 0) + 1);
        }

        let entry = null;
        if (contributors.size === 1) {
          const [srcScene, count] = contributors.entries().next().value;
          const source = oldScenes[String(srcScene)];
          if (source) {
            entry = JSON.parse(JSON.stringify(source));
            entry.scene_id = key;
            entry.image_filenames = filenames.slice();
            if (count < (oldSizes.get(srcScene) || 0)) {
              // Strict subset: some of the images that justified the stored
              // tags went elsewhere, so drop the ones this half no longer
              // supports before carrying them over.
              const keptRows = filenames.map(fn => rowsByFilename.get(fn)).filter(Boolean);
              const goneRows = folderRows.filter(
                r => oldSceneByFilename.get(r.filename || '') === srcScene
                  && !filenames.includes(r.filename || '')
              );
              try { _pruneFinalizedSceneTagsAfterRemoval(entry, keptRows, goneRows); } catch (_) {}
            }
            inherited++;
          }
        }

        if (!entry) {
          entry = {
            scene_id: key,
            image_filenames: filenames.slice(),
            name: '',
            status: 'pending',
            user_tags: { species: [], families: [], finalized: false },
          };
          if (contributors.size > 1) reset++;
        }
        migrated[key] = entry;
      }

      sd.scenes = migrated;
      return { inherited, reset };
    }

    async function applyRegroup() {
      if (!_regroupFolder || !_regroupPreview) return;
      const folderPath = _regroupFolder;
      const applyBtn = _regroupEl('regroupApplyBtn');
      if (applyBtn) applyBtn.disabled = true;

      try {
        const opts = _regroupControls();
        const res = await window.pywebview.api.regroup_scenes_preview(folderPath, opts, true);
        if (!res || !res.success || !Array.isArray(res.assignment)) {
          showToast('Regroup failed: ' + ((res && res.error) || 'unknown error'), 5000);
          if (applyBtn) applyBtn.disabled = false;
          return;
        }

        const folderRows = _regroupRowsForFolder(folderPath);
        const rowsByFilename = new Map();
        for (const r of folderRows) rowsByFilename.set(r.filename || '', r);

        const oldSceneByFilename = new Map();
        for (const r of folderRows) {
          oldSceneByFilename.set(r.filename || '', String(r.scene_count ?? ''));
        }
        const newSceneByFilename = new Map();
        for (const a of res.assignment) {
          if (rowsByFilename.has(a.filename)) {
            newSceneByFilename.set(a.filename, String(a.scene));
          }
        }
        if (newSceneByFilename.size === 0) {
          showToast('Regroup produced no changes', 3000);
          if (applyBtn) applyBtn.disabled = false;
          return;
        }

        // Scenedata migration runs BEFORE the rows move, because it needs both
        // the old and the new membership to decide what each new scene may
        // inherit.
        const migration = _regroupMigrateScenedata(
          folderPath, folderRows, oldSceneByFilename, newSceneByFilename
        );

        let changed = 0;
        for (const [filename, newScene] of newSceneByFilename) {
          const r = rowsByFilename.get(filename);
          if (!r) continue;
          if (String(r.scene_count ?? '') !== newScene) changed++;
          r.scene_count = newScene;
          // scene_name is the legacy per-row mirror of the scene's name; the
          // authoritative copy now lives in the migrated scenedata entry, so
          // clearing it here stops a stale name reappearing on a scene that
          // did not inherit one.
          r.scene_name = '';
        }

        markDirty(folderPath);
        closeRegroupDialog();
        renderScenes();

        const bits = [`Regrouped into ${res.stats.proposed_scene_count} scene(s)`];
        if (changed) bits.push(`${changed} photo(s) moved`);
        if (migration.reset) bits.push(`${migration.reset} scene(s) need re-reviewing`);
        showToast(bits.join(' · '), 5000);
      } catch (e) {
        console.error('[regroup] apply failed', e);
        showToast('Regroup failed', 4000);
        if (applyBtn) applyBtn.disabled = false;
      }
    }

    function _regroupWireControls() {
      for (const id of ['regroupGroupWithin', 'regroupSplitAfter', 'regroupSimilarity']) {
        const elm = _regroupEl(id);
        if (elm) elm.addEventListener('input', _regroupSchedulePreview);
      }
      const applyBtn = _regroupEl('regroupApplyBtn');
      if (applyBtn) applyBtn.addEventListener('click', applyRegroup);
      const cancelBtn = _regroupEl('regroupCancelBtn');
      if (cancelBtn) cancelBtn.addEventListener('click', closeRegroupDialog);
      const dlg = _regroupEl('regroupDlg');
      // Esc closes a <dialog> natively; release the cached working set too.
      if (dlg) dlg.addEventListener('close', () => {
        try { window.pywebview?.api?.regroup_scenes_release?.(); } catch (_) {}
      });
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', _regroupWireControls);
    } else {
      _regroupWireControls();
    }
