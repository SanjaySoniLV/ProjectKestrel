    // ── Primary subject (prototype) ──
    // Every bird in a scene carries a number that is the same bird in every frame
    // (stored in .kestrel/bird_links.json by analysis / the 3.0 upgrade / folder load;
    // bridge: primary_subject_links). Focusing a bird (F, or Shift+Click its box on the
    // full image) makes it the primary crop in every frame where it was found; F on the
    // same bird again undoes that. Unsure links show as "Bird 3?". Hooks live in
    // scene-dialog.js (open, overlay labels, filmstrip cards, preview, keys, Shift+Click).

    const PS_LINKS_TTL_MS = 30000;
    const _ps = {
      links: new Map(),      // root -> { at, scenes, minMargin }
      loading: new Map(),    // root -> Promise
      focus: null,           // { sceneId, key, bird, prev: Map(rowKey -> primary index), unsure, missing }
      showIds: false,
    };

    function _psRowKey(row) { return _cropStateKey(row); }
    function _psRoot(row) { return row?.__rootPath || rootPath || ''; }

    async function _psLoadLinks(root, force = false) {
      const api = window.pywebview?.api;
      if (!root || !api?.primary_subject_links) return null;
      const have = _ps.links.get(root);
      if (have && !force && Date.now() - have.at < PS_LINKS_TTL_MS) return have;
      if (_ps.loading.has(root)) return _ps.loading.get(root);
      const p = (async () => {
        let res = null;
        try { res = await api.primary_subject_links(root); } catch (_) { res = null; }
        _ps.loading.delete(root);
        if (!res?.success) return have || null;
        const entry = { at: Date.now(), scenes: res.scenes || {}, minMargin: Number(res.min_margin) || 1 };
        _ps.links.set(root, entry);
        return entry;
      })();
      _ps.loading.set(root, p);
      return p;
    }

    // {key, bird, margin, minMargin} for crop position ``idx`` of ``row``, or null.
    function _psBirdOf(row, idx) {
      const L = _ps.links.get(_psRoot(row));
      const sc = String(row?.scene_count ?? '').trim();
      const ent = L && sc ? L.scenes[sc] : null;
      const crop = getRowCropRecords(row)[idx];
      if (!ent || !crop) return null;
      const ci = String(crop.crop_index);
      const bird = ent.ids?.[row.filename]?.[ci];
      if (!bird) return null;
      const margin = Number(ent.margins?.[row.filename]?.[ci] ?? 0);
      return { key: `${sc}:${bird}`, bird, margin, minMargin: L.minMargin };
    }

    function _psFindBird(row, key) {
      const n = getRowCropRecords(row).length;
      for (let i = 0; i < n; i++) {
        const b = _psBirdOf(row, i);
        if (b && b.key === key) return { pos: i, ...b };
      }
      return null;
    }

    function _psFocusHere(row) {
      return !!(_ps.focus && _currentScene && String(_currentScene.id) === _ps.focus.sceneId && row);
    }

    async function psOnSceneOpen(scene) {
      if (_ps.focus && String(scene?.id) !== _ps.focus.sceneId) _ps.focus = null;
      const roots = new Set((scene?.images || []).map(_psRoot));
      await Promise.all([...roots].map(r => _psLoadLinks(r)));
      if (_ps.showIds && _currentScene && String(_currentScene.id) === String(scene?.id)) {
        psAfterSelect(_currentScene.images[currentImageIndex]);
      }
    }

    // Overlay label for crop position ``idx`` of ``row`` (null = default "#n").
    function psCropLabel(row, idx) {
      const b = _psBirdOf(row, idx);
      if (!b) return null;
      const unsure = b.margin < b.minMargin ? '?' : '';
      const focused = _psFocusHere(row) && _ps.focus.key === b.key;
      return `${focused ? '🎯 ' : ''}Bird ${b.bird}${unsure}`;
    }

    // Hint under the active box: how to focus the scene on this bird.
    function psCropHint(row, idx) {
      const b = _psBirdOf(row, idx);
      if (!b || (_currentScene?.images?.length || 0) < 2) return null;
      if (_psFocusHere(row) && _ps.focus.key === b.key) return 'F again to undo focus';
      return 'Shift+Click or press F to focus this scene on this bird';
    }

    async function _psRestore(f) {
      for (const r of (_currentScene?.images || [])) {
        const k = _psRowKey(r);
        if (!f.prev.has(k)) continue;
        setRowActiveCropIndex(r, parseInt(f.prev.get(k), 10) || 0);
        promoteActiveCropToPrimary(r);
      }
    }

    // Focus the scene on the bird in crop position ``pos`` of ``row``.
    async function psFocus(row, pos) {
      const scene = _currentScene;
      if (!scene || !row) return;
      await _psLoadLinks(_psRoot(row));
      const b = _psBirdOf(row, pos);
      if (!b) { showToast('No bird links for this photo yet (needs a pipeline 3.0 folder)', 2600); return; }
      const sameAgain = _psFocusHere(row) && _ps.focus.key === b.key;
      if (_psFocusHere(row)) await _psRestore(_ps.focus);
      if (sameAgain) {
        _ps.focus = null;
        await _resortAndFocusSceneImage(row, true);
        await renderScenes();
        showToast(`Focus on Bird ${b.bird} undone`, 1800);
        return;
      }
      const f = { sceneId: String(scene.id), key: b.key, bird: b.bird, prev: new Map(), unsure: 0, missing: 0, found: 0 };
      for (const r of scene.images) {
        const hit = _psFindBird(r, b.key);
        if (!hit) { f.missing++; continue; }
        f.found++;
        if (hit.margin < hit.minMargin) f.unsure++;
        f.prev.set(_psRowKey(r), String(getRowPrimaryCropIndex(r)));
        setRowActiveCropIndex(r, hit.pos);
        promoteActiveCropToPrimary(r);
      }
      _ps.focus = f;
      await _resortAndFocusSceneImage(row, true);
      await renderScenes();
      showToast(`Focused on Bird ${b.bird}: primary crop in ${f.found} of ${scene.images.length} photos`
        + (f.unsure ? ` (${f.unsure} unsure)` : '') + ' · F again to undo', 3500);
    }

    function psFocusActive() {
      const r = _currentScene?.images?.[currentImageIndex];
      if (r) psFocus(r, getRowActiveCropState(r).activeIndex);
    }

    // Shift+Click on the full image: focus the bird whose box is under the cursor.
    function psHandleShiftClick(row, ev, box) {
      const img = box?.querySelector('img');
      if (!img || !row) return false;
      const rect = img.getBoundingClientRect();
      const x = (ev.clientX - rect.left) / rect.width, y = (ev.clientY - rect.top) / rect.height;
      let best = -1, bestArea = Infinity;
      getRowCropRecords(row).forEach((c, i) => {
        const bb = c.bbox || {};
        if (x >= bb.x_min_norm && x <= bb.x_max_norm && y >= bb.y_min_norm && y <= bb.y_max_norm) {
          const a = (bb.x_max_norm - bb.x_min_norm) * (bb.y_max_norm - bb.y_min_norm);
          if (a < bestArea) { bestArea = a; best = i; }
        }
      });
      if (best < 0) return false;
      psFocus(row, best);
      return true;
    }

    function psToggleIds() {
      _ps.showIds = !_ps.showIds;
      const r = _currentScene?.images?.[currentImageIndex];
      if (!r) return;
      if (_ps.showIds) psAfterSelect(r);
      else el('#previewBox')?.querySelectorAll('.scene-crop-overlay-layer').forEach(n => n.remove());
    }

    function psDecorateCard(card, row) {
      if (!_psFocusHere(row)) return;
      const hit = _psFindBird(row, _ps.focus.key);
      if (hit && hit.margin >= hit.minMargin) return;
      const badge = document.createElement('div');
      badge.className = `ps-badge ${hit ? 'unsure' : 'missing'}`;
      badge.textContent = hit ? `Bird ${hit.bird}?` : `no Bird ${_ps.focus.bird}`;
      badge.title = hit ? 'Unsure match for the focused bird' : 'The focused bird was not found in this photo';
      if (!hit) card.classList.add('ps-missing');
      card.querySelector('.filmstrip-thumb')?.appendChild(badge);
    }

    function psAfterSelect(row) {
      // Crop pane: say so when the focused bird is not in this photo.
      const cropBox = el('#previewCropBox');
      cropBox?.querySelectorAll('.ps-crop-note').forEach(n => n.remove());
      if (cropBox && _psFocusHere(row) && !_psFindBird(row, _ps.focus.key)) {
        const note = document.createElement('div');
        note.className = 'ps-crop-note';
        note.textContent = `Bird ${_ps.focus.bird} not found in this photo — showing its usual crop`;
        cropBox.appendChild(note);
      }
      if (!_ps.showIds || !row) return;
      const img = el('#previewBox')?.querySelector('img');
      if (!img) return;
      const show = () => {
        if (!_ps.showIds || _currentScene?.images?.[currentImageIndex] !== row) return;
        _showSceneCropOverlay(row, getRowActiveCropState(row).activeIndex, 0);
      };
      if (img.complete && img.naturalWidth) show();
      else img.addEventListener('load', show, { once: true });
    }
