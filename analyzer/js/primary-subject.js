    // ── Primary subject (prototype) ──
    // In a multi-bird scene, number every bird consistently across the frames and
    // let the user follow one of them: each frame's active crop becomes that bird,
    // the filmstrip sorts by that bird's quality, and "Use as primary crop" writes it
    // through the normal promote/save path. Runs on the embeddings pipeline 3.0
    // already stored (bridge: primary_subject_scene), so nothing is re-analysed.
    // Hooks live in scene-dialog.js (open, filmstrip cards, preview, keys, overlay).

    const _ps = {
      sceneId: null,
      token: 0,
      loading: false,
      error: '',
      birds: new Map(),      // row key -> [bird number per crop]
      margins: new Map(),    // row key -> [margin per crop]
      nBirds: 0,
      embedded: 0,
      crops: 0,
      follow: null,          // { seedKey, bird, byKey: Map(key -> {crop, margin, confident}), found, unsure, missing }
      showIds: false,
    };

    function _psRowKey(row) { return _cropStateKey(row); }

    function _psCaptureOrder(rows) {
      return rows.slice().sort((a, b) => {
        const ta = parseCaptureTimeMs(a.capture_time);
        const tb = parseCaptureTimeMs(b.capture_time);
        const fa = Number.isFinite(ta), fb = Number.isFinite(tb);
        if (fa && fb && ta !== tb) return ta - tb;
        if (fa !== fb) return fa ? -1 : 1;
        return String(a.filename || '').localeCompare(String(b.filename || ''));
      });
    }

    function psSceneEligible(scene) {
      if (!scene || !Array.isArray(scene.images) || scene.images.length < 2) return false;
      return scene.images.some(r => getRowCropRecords(r).length > 1);
    }

    function _psPayload(scene) {
      const ordered = _psCaptureOrder(scene.images);
      const frames = ordered.map(r => ({
        filename: String(r.filename || ''),
        crops: getRowCropRecords(r).map(c => ({ crop_index: c.crop_index, bbox: c.bbox, quality: c.quality })),
      }));
      const root = scene.images[0]?.__rootPath || rootPath || '';
      return { root, ordered, frames };
    }

    function _psReset(sceneId) {
      _ps.sceneId = sceneId;
      _ps.token++;
      _ps.loading = false;
      _ps.error = '';
      _ps.birds.clear();
      _ps.margins.clear();
      _ps.nBirds = 0;
      _ps.embedded = 0;
      _ps.crops = 0;
      _ps.follow = null;
    }

    function _psStoreLabels(ordered, res) {
      _ps.birds.clear();
      _ps.margins.clear();
      let n = 0;
      ordered.forEach((r, f) => {
        const ids = res.birds?.[f] || [];
        _ps.birds.set(_psRowKey(r), ids);
        _ps.margins.set(_psRowKey(r), res.margins?.[f] || []);
        for (const id of ids) n = Math.max(n, id);
      });
      _ps.nBirds = n;
      _ps.embedded = res.embedded_crops || 0;
      _ps.crops = res.crops || 0;
    }

    async function psOnSceneOpen(scene) {
      const sid = scene ? String(scene.id) : null;
      if (sid !== _ps.sceneId) _psReset(sid);
      if (!psSceneEligible(scene)) { _psRenderBar(); return; }
      const api = window.pywebview?.api;
      if (!api?.primary_subject_scene) { _ps.error = 'unavailable'; _psRenderBar(); return; }
      if (_ps.birds.size) { _psRenderBar(); return; }
      const my = ++_ps.token;
      _ps.loading = true;
      _psRenderBar();
      const { root, ordered, frames } = _psPayload(scene);
      let res = null;
      try { res = await api.primary_subject_scene(root, frames, null); } catch (e) { res = { success: false, error: String(e?.message || e) }; }
      if (my !== _ps.token) return;
      _ps.loading = false;
      if (!res?.success) { _ps.error = res?.error || 'failed'; _psRenderBar(); return; }
      _psStoreLabels(ordered, res);
      _psRenderBar();
      if (_currentScene && String(_currentScene.id) === sid) psAfterSelect(_currentScene.images[currentImageIndex]);
    }

    function _psSubjectQuality(row) {
      const m = _ps.follow?.byKey.get(_psRowKey(row));
      if (!m || m.crop == null) return null;
      const crop = getRowCropRecords(row).find(c => c.crop_index === m.crop);
      return crop ? _numberOr(crop.quality, -1) : null;
    }

    function _psRank(row) {
      const m = _ps.follow?.byKey.get(_psRowKey(row));
      if (!m || m.crop == null) return 2;
      return m.confident ? 0 : 1;
    }

    // Filmstrip order while following: sure matches by the subject's quality,
    // then unsure matches, then frames where the subject wasn't found.
    function psApplyOrder(scene) {
      if (!_ps.follow || !scene || String(scene.id) !== _ps.sceneId) return scene;
      scene.images.sort((a, b) => {
        const ra = _psRank(a), rb = _psRank(b);
        if (ra !== rb) return ra - rb;
        return (_psSubjectQuality(b) ?? -1) - (_psSubjectQuality(a) ?? -1);
      });
      return scene;
    }

    function psKeepsActiveCrop(row) {
      return !!(_ps.follow && row && _ps.follow.byKey.has(_psRowKey(row)));
    }

    async function psFollow() {
      const scene = _currentScene;
      if (!scene || !psSceneEligible(scene) || _ps.loading) return;
      const api = window.pywebview?.api;
      if (!api?.primary_subject_scene) return;
      const seedRow = scene.images[currentImageIndex];
      if (!seedRow) return;
      const st = getRowActiveCropState(seedRow);
      if (!st.crops.length) { showToast('This frame has no bird crop to follow', 2200); return; }
      const { root, ordered, frames } = _psPayload(scene);
      const seedFrame = ordered.findIndex(r => _psRowKey(r) === _psRowKey(seedRow));
      const my = ++_ps.token;
      _ps.loading = true;
      _psRenderBar();
      let res = null;
      try {
        res = await api.primary_subject_scene(root, frames, { frame: seedFrame, crop: st.activeIndex });
      } catch (e) { res = { success: false, error: String(e?.message || e) }; }
      if (my !== _ps.token) return;
      _ps.loading = false;
      if (!res?.success || !Array.isArray(res.follow)) {
        _ps.error = res?.error || 'failed';
        _psRenderBar();
        showToast('Could not follow this bird: ' + _ps.error, 3000);
        return;
      }
      _psStoreLabels(ordered, res);
      const byKey = new Map();
      let found = 0, unsure = 0, missing = 0;
      ordered.forEach((r, f) => {
        const m = res.follow[f] || {};
        const list = getRowCropRecords(r);
        const crop = Number.isInteger(m.crop) && list[m.crop] ? list[m.crop].crop_index : null;
        byKey.set(_psRowKey(r), { crop, margin: m.margin || 0, confident: !!m.confident && crop != null });
        if (crop == null) missing++;
        else if (m.confident) found++;
        else unsure++;
        if (crop != null) setRowActiveCropIndex(r, list.findIndex(c => c.crop_index === crop), list);
      });
      const seedIds = _ps.birds.get(_psRowKey(seedRow)) || [];
      _ps.follow = { seedKey: _psRowKey(seedRow), bird: seedIds[st.activeIndex] || 0, byKey, found, unsure, missing };
      psApplyOrder(scene);
      currentImageIndex = Math.max(0, scene.images.findIndex(r => _psRowKey(r) === _ps.follow.seedKey));
      renderFilmstrip(scene);
      await selectFilmstripImage(currentImageIndex, scene, false, !_ps.showIds);
    }

    async function psStop() {
      if (!_ps.follow || !_currentScene) return;
      const row = _currentScene.images[currentImageIndex];
      for (const r of _currentScene.images) _sceneActiveCropIndexByImage.delete(_psRowKey(r));
      _ps.follow = null;
      await _resortAndFocusSceneImage(row, false);
    }

    // Write the followed bird as each frame's primary crop (sure matches only).
    async function psApply() {
      const f = _ps.follow;
      if (!f || !_currentScene) return;
      const seedRow = _currentScene.images.find(r => _psRowKey(r) === f.seedKey) || _currentScene.images[currentImageIndex];
      let changed = 0, skipped = 0;
      for (const r of _currentScene.images) {
        const m = f.byKey.get(_psRowKey(r));
        if (!m || m.crop == null || !m.confident) { skipped++; continue; }
        const list = getRowCropRecords(r);
        const pos = list.findIndex(c => c.crop_index === m.crop);
        if (pos < 0) { skipped++; continue; }
        setRowActiveCropIndex(r, pos, list);
        if (promoteActiveCropToPrimary(r)) changed++;
      }
      for (const r of _currentScene.images) _sceneActiveCropIndexByImage.delete(_psRowKey(r));
      _ps.follow = null;
      await _resortAndFocusSceneImage(seedRow, false);
      await renderScenes();
      showToast(`Primary crop set to Bird ${f.bird} on ${changed} frame${changed === 1 ? '' : 's'}`
        + (skipped ? ` · ${skipped} unsure/not-found frame${skipped === 1 ? '' : 's'} left as they were` : ''), 3500);
    }

    function psToggleIds() {
      _ps.showIds = !_ps.showIds;
      _psRenderBar();
      const r = _currentScene?.images?.[currentImageIndex];
      if (!r) return;
      if (_ps.showIds) psAfterSelect(r);
      else el('#previewBox')?.querySelectorAll('.scene-crop-overlay-layer').forEach(n => n.remove());
    }

    // Overlay label for crop position ``idx`` of ``row`` (null = default "#n").
    function psCropLabel(row, idx) {
      const ids = _ps.birds.get(_psRowKey(row));
      if (!ids || !ids[idx]) return null;
      const m = _ps.follow?.byKey.get(_psRowKey(row));
      const crop = getRowCropRecords(row)[idx];
      const isSubject = m && m.crop != null && crop && crop.crop_index === m.crop;
      return (isSubject ? '🎯 ' : '') + `Bird ${ids[idx]}`;
    }

    function psDecorateCard(card, row) {
      const f = _ps.follow;
      if (!f || String(_currentScene?.id) !== _ps.sceneId) return;
      const m = f.byKey.get(_psRowKey(row));
      if (!m) return;
      const badge = document.createElement('div');
      const q = _psSubjectQuality(row);
      if (m.crop == null) {
        badge.className = 'ps-badge missing';
        badge.textContent = 'not found';
        badge.title = `Bird ${f.bird} was not found in this frame`;
        card.classList.add('ps-missing');
      } else {
        badge.className = `ps-badge ${m.confident ? 'found' : 'unsure'}`;
        badge.textContent = `${m.confident ? '🎯' : '?'} ${q != null && q >= 0 ? q.toFixed(3) : '—'}`;
        badge.title = m.confident
          ? `Bird ${f.bird}: quality ${fmt3(q)}`
          : `Bird ${f.bird} (unsure match, margin ${m.margin.toFixed(2)}): quality ${fmt3(q)}`;
      }
      card.querySelector('.filmstrip-thumb')?.appendChild(badge);
    }

    function psAfterSelect(row) {
      _psRenderBar(row);
      // In a frame where the subject wasn't found the crop pane still shows the
      // frame's own primary crop -- say so, so it isn't read as the subject.
      const cropBox = el('#previewCropBox');
      cropBox?.querySelectorAll('.ps-crop-note').forEach(n => n.remove());
      const m = row && _ps.follow?.byKey.get(_psRowKey(row));
      if (cropBox && m && m.crop == null) {
        const note = document.createElement('div');
        note.className = 'ps-crop-note';
        note.textContent = `Bird ${_ps.follow.bird} not found in this frame — showing its usual crop`;
        cropBox.appendChild(note);
      }
      if (!_ps.showIds || !row || !_ps.birds.size) return;
      const img = el('#previewBox')?.querySelector('img');
      if (!img) return;
      const show = () => {
        if (!_ps.showIds || _currentScene?.images?.[currentImageIndex] !== row) return;
        _showSceneCropOverlay(row, getRowActiveCropState(row).activeIndex, 0);
      };
      if (img.complete && img.naturalWidth) show();
      else img.addEventListener('load', show, { once: true });
    }

    function _psRenderBar(row = null) {
      const bar = el('#scenePsBar');
      if (!bar) return;
      const scene = _currentScene;
      if (!scene || !psSceneEligible(scene) || _ps.error === 'unavailable') {
        bar.classList.add('hidden');
        bar.innerHTML = '';
        return;
      }
      bar.classList.remove('hidden');
      row = row || scene.images[currentImageIndex];
      const key = row ? _psRowKey(row) : '';
      const parts = [];
      const btn = (id, label, title, cls = '') => `<button type="button" class="ps-btn ${cls}" id="${id}" title="${escapeHtml(title)}">${label}</button>`;

      if (_ps.loading) {
        parts.push('<span class="ps-text muted">Linking birds across frames…</span>');
      } else if (_ps.error) {
        parts.push(`<span class="ps-text warn">Bird linking failed: ${escapeHtml(_ps.error)}</span>`);
      } else if (_ps.follow) {
        const f = _ps.follow;
        const total = f.found + f.unsure + f.missing;
        const m = f.byKey.get(key);
        const here = !m ? '' : m.crop == null ? 'not in this frame'
          : m.confident ? 'this frame: found' : `this frame: unsure (margin ${m.margin.toFixed(2)})`;
        parts.push(`<span class="ps-text"><b>🎯 Following Bird ${f.bird}</b> · found in ${f.found} of ${total} frames`
          + (f.unsure ? `, ${f.unsure} unsure` : '') + (f.missing ? `, ${f.missing} not found` : '')
          + ` · sorted by this bird's quality${here ? ` · <span class="ps-here">${here}</span>` : ''}</span>`);
        parts.push(btn('psApplyBtn', `Use Bird ${f.bird} as primary crop`,
          `Set Bird ${f.bird} as the primary crop in the ${f.found} frames where it was found with confidence`, 'primary'));
        parts.push(btn('psStopBtn', 'Stop following', 'Back to the normal order and crops'));
      } else {
        const ids = _ps.birds.get(key) || [];
        const st = row ? getRowActiveCropState(row) : null;
        const cur = st && ids[st.activeIndex] ? `Bird ${ids[st.activeIndex]}` : 'this bird';
        parts.push(btn('psFollowBtn', `🎯 Follow ${cur} (F)`,
          'Find this bird in every frame of the scene and rank the frames by its quality. ↑/↓ picks the bird.', 'primary'));
        parts.push(`<span class="ps-text muted">${_ps.nBirds ? `${_ps.nBirds} birds linked across ${scene.images.length} frames` : ''}</span>`);
      }
      if (!_ps.loading && _ps.birds.size) {
        parts.push(btn('psIdsBtn', _ps.showIds ? 'Hide bird numbers (B)' : 'Show bird numbers (B)',
          'Number each bird; the same number is the same bird in every frame', _ps.showIds ? 'on' : ''));
      }
      if (!_ps.loading && _ps.crops && _ps.embedded < _ps.crops) {
        parts.push(`<span class="ps-text warn" title="Upgrade the folder to pipeline 3.0 to store bird embeddings">`
          + `${_ps.embedded ? 'some birds' : 'no birds'} have embeddings, linking by position only</span>`);
      }
      parts.push('<span class="ps-tag">prototype</span>');
      bar.innerHTML = parts.join('');
      el('#psFollowBtn')?.addEventListener('click', (e) => { e.stopPropagation(); psFollow(); });
      el('#psStopBtn')?.addEventListener('click', (e) => { e.stopPropagation(); psStop(); });
      el('#psApplyBtn')?.addEventListener('click', (e) => { e.stopPropagation(); psApply(); });
      el('#psIdsBtn')?.addEventListener('click', (e) => { e.stopPropagation(); psToggleIds(); });
    }
