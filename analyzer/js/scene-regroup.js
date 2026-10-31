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
    let _regroupOrder = [];          // filenames in the order scene 'ranges' index into
    let _regroupRowByName = new Map();
    let _regroupSceneLimit = 0;      // scenes rendered so far in the current view

    // Rendering budget. A 1000+ photo folder can regroup into hundreds of
    // scenes and this redraws on every (debounced) slider tick, so the list is
    // built in chunks of scenes and each scene shows a bounded number of
    // thumbnails. The thumbnails themselves load lazily as they scroll into
    // view (lazyLoadImg), so only what is on screen is ever read from disk.
    const REGROUP_SCENE_CHUNK = 40;
    const REGROUP_THUMBS_PER_SCENE = 24;

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

    /** "4m 12s", "38s", "1h 5m" — exact enough to compare against a threshold. */
    function _regroupFormatGap(seconds) {
      if (!Number.isFinite(seconds)) return '?';
      const s = Math.round(seconds);
      if (s < 60) return seconds < 10 ? `${seconds.toFixed(1)}s` : `${s}s`;
      if (s < 3600) {
        const m = Math.floor(s / 60);
        const rem = s % 60;
        return rem ? `${m}m ${rem}s` : `${m}m`;
      }
      const h = Math.floor(s / 3600);
      const m = Math.floor((s % 3600) / 60);
      return m ? `${h}h ${m}m` : `${h}h`;
    }

    /**
     * Why a scene starts where it does, as one line:
     *   "time gap 4m 12s > 2m threshold", "similarity 0.03 < 0.05 threshold",
     *   "camera rotated (landscape → portrait)".
     * Returns '' for the first scene and for entries without a recorded reason.
     */
    function _regroupBoundaryLabel(entry) {
      const d = entry && entry.detail;
      if (!d) return '';
      if (d.kind === 'time') {
        return `time gap ${_regroupFormatGap(d.value)} > ${_regroupFormatGap(d.threshold)} threshold`;
      }
      if (d.kind === 'feature' || d.kind === 'color') {
        return `similarity ${Number(d.value).toFixed(2)} < ${Number(d.threshold).toFixed(2)} threshold`;
      }
      if (d.kind === 'orientation') {
        return `camera rotated (${d.from} → ${d.to})`;
      }
      return '';
    }

    function _regroupChangeBadge(entry, view) {
      const n = entry.change_count || 0;
      if (entry.change === 'same') return { cls: 'same', text: 'Unchanged' };
      if (view === 'proposed') {
        if (entry.change === 'merged') return { cls: 'merged', text: `Merged from ${n} scenes` };
        return { cls: 'split', text: 'Split from a larger scene' };
      }
      if (entry.change === 'split') return { cls: 'split', text: `Will split into ${n} scenes` };
      return { cls: 'merged', text: 'Will be joined with other photos' };
    }

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
      _regroupOrder = [];
      _regroupRowByName = new Map();
      for (const r of folderRows) _regroupRowByName.set(r.filename || '', r);
      _regroupSceneLimit = REGROUP_SCENE_CHUNK;
      const changedOnly = _regroupEl('regroupChangedOnly');
      if (changedOnly) changedOnly.checked = false;

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
        _regroupOrder = Array.isArray(res.order) ? res.order : [];

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
          const full = total === 0
            ? ''
            : `Appearance was measured for ${scored} of ${total} image pairs. ` +
              `The rest were grouped by the burst rule during analysis and carry no score, ` +
              `so only the time settings can separate them.`;
          scoreNote.textContent = total === 0
            ? ''
            : `Appearance measured for ${scored} of ${total} pairs; the similarity control only acts on those.`;
          scoreNote.title = full;
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
      return (preview.proposed || []).some(s => s.change !== 'same');
    }

    /** Filenames of a scene's photos (up to `cap`), resolved from its index runs. */
    function _regroupSceneFilenames(scene, cap) {
      const out = [];
      for (const [first, last] of scene.ranges || []) {
        for (let i = first; i <= last; i++) {
          if (out.length >= cap) return out;
          const name = _regroupOrder[i];
          if (name !== undefined) out.push(name);
        }
      }
      return out;
    }

    function _regroupTimeText(raw) {
      if (!raw) return '';
      const t = new Date(String(raw).replace(' ', 'T'));
      if (Number.isNaN(t.getTime())) return '';
      return t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    }

    function _regroupMakeThumb(filename) {
      const cell = document.createElement('div');
      cell.className = 'regroup-thumb';
      const r = _regroupRowByName.get(filename);
      cell.title = filename;
      const img = document.createElement('img');
      img.alt = '';
      img.decoding = 'async';
      const path = r && (r.export_path || r.crop_path);
      if (path) {
        lazyLoadImg(img, () => getBlobUrlForPath(path, r.__rootPath));
      }
      cell.appendChild(img);
      return cell;
    }

    function _regroupMakeScene(s, view) {
      const sec = document.createElement('section');
      sec.className = `regroup-scene regroup-scene--${s.change || 'same'}`;

      const head = document.createElement('div');
      head.className = 'regroup-scene-head';
      const idx = document.createElement('span');
      idx.className = 'regroup-scene-idx';
      idx.textContent = `Scene ${s.scene}`;
      const size = document.createElement('span');
      size.className = 'regroup-scene-size';
      size.textContent = `${s.size} photo${s.size === 1 ? '' : 's'}`;
      head.append(idx, size);
      const when = _regroupTimeText(s.start_time);
      if (when) {
        const t = document.createElement('span');
        t.className = 'regroup-scene-time muted';
        t.textContent = `from ${when}`;
        head.appendChild(t);
      }
      const badge = _regroupChangeBadge(s, view);
      const b = document.createElement('span');
      b.className = `regroup-badge regroup-badge--${badge.cls}`;
      b.textContent = badge.text;
      head.appendChild(b);
      sec.appendChild(head);

      const strip = document.createElement('div');
      strip.className = 'regroup-thumbs';
      const names = _regroupSceneFilenames(s, REGROUP_THUMBS_PER_SCENE);
      for (const name of names) strip.appendChild(_regroupMakeThumb(name));
      if (s.size > names.length) {
        const more = document.createElement('div');
        more.className = 'regroup-thumb-more';
        more.textContent = `+${s.size - names.length}`;
        more.title = `${s.size - names.length} more photos in this scene`;
        strip.appendChild(more);
      }
      sec.appendChild(strip);
      return sec;
    }

    /** Draw one grouping ('current' or 'proposed') as one block per scene. */
    function _regroupRenderList(view) {
      const host = _regroupEl(view === 'current' ? 'regroupCurrentList' : 'regroupProposedList');
      if (!host || !_regroupPreview) return;
      const entries = (view === 'current' ? _regroupPreview.current : _regroupPreview.proposed) || [];
      const changedOnly = !!_regroupEl('regroupChangedOnly')?.checked;
      const visible = changedOnly ? entries.filter(s => s.change !== 'same') : entries;

      const keepScroll = host.scrollTop;
      host.innerHTML = '';
      if (visible.length === 0) {
        host.innerHTML = '<div class="regroup-empty muted">' +
          (changedOnly ? 'No scenes change with these settings.' : 'No scenes') + '</div>';
        return;
      }

      const frag = document.createDocumentFragment();
      const shown = visible.slice(0, _regroupSceneLimit);
      for (const s of shown) {
        // In the proposed view the boundary reason sits on the dividing line
        // above the scene it created.
        const label = view === 'proposed' ? _regroupBoundaryLabel(s) : '';
        if (label) {
          const div = document.createElement('div');
          div.className = 'regroup-boundary';
          const text = document.createElement('span');
          text.textContent = label;
          div.appendChild(text);
          frag.appendChild(div);
        }
        frag.appendChild(_regroupMakeScene(s, view));
      }
      host.appendChild(frag);

      if (visible.length > shown.length) {
        const rest = visible.length - shown.length;
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn regroup-more';
        btn.textContent = `Show ${Math.min(rest, REGROUP_SCENE_CHUNK)} more of ${rest} remaining scenes`;
        btn.addEventListener('click', () => {
          _regroupSceneLimit += REGROUP_SCENE_CHUNK;
          _regroupRenderLists();
        });
        host.appendChild(btn);
      }
      host.scrollTop = keepScroll;
    }

    function _regroupRenderLists() {
      _regroupRenderList('current');
      _regroupRenderList('proposed');
    }

    function _regroupRenderPreview(preview) {
      const stats = preview.stats || {};
      const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

      _regroupEl('regroupCurrentHead').textContent =
        `Current grouping · ${plural(stats.current_scene_count || 0, 'scene')}`;
      _regroupEl('regroupProposedHead').textContent =
        `Proposed grouping · ${plural(stats.proposed_scene_count || 0, 'scene')}`;

      const legend = _regroupEl('regroupLegend');
      if (legend) {
        legend.innerHTML =
          '<span class="regroup-badge regroup-badge--same">Unchanged</span>' +
          '<span class="regroup-badge regroup-badge--merged">Merged</span>' +
          '<span class="regroup-badge regroup-badge--split">Split</span>';
      }

      const delta = (stats.proposed_scene_count || 0) - (stats.current_scene_count || 0);
      const deltaText = delta === 0 ? 'Same number of scenes'
        : delta > 0 ? `${plural(delta, 'more scene')}`
        : `${-delta} fewer scene${delta === -1 ? '' : 's'}`;
      const bits = [];
      if (stats.unchanged_scenes) bits.push(`${stats.unchanged_scenes} unchanged`);
      if (stats.merged_scenes) bits.push(`${stats.merged_scenes} merged`);
      if (stats.split_scenes) bits.push(`${stats.split_scenes} split`);
      _regroupEl('regroupSummary').textContent =
        `${plural(preview.image_count, 'photo')} · ${deltaText}` +
        (bits.length ? ` · proposed scenes: ${bits.join(', ')}` : '');

      _regroupRenderLists();
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
      const changedOnly = _regroupEl('regroupChangedOnly');
      if (changedOnly) changedOnly.addEventListener('change', () => {
        _regroupSceneLimit = REGROUP_SCENE_CHUNK;
        _regroupRenderLists();
      });
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
