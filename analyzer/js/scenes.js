    function isManualRated(r) { return getRating(r) > 0 && getOrigin(r) === 'manual'; }

    function isManualCullDecision(r) {
      const status = getCullStatus(r);
      return status === 'accept' || status === 'reject';
    }

    function hasReviewedSceneMetadata(scene) {
      if (!scene) return false;
      if (scene.isApproved) return true;
      return String(scene.sceneName || '').trim().length > 0;
    }

    function isManuallyReviewedScene(scene) {
      if (!scene) return false;
      if (hasReviewedSceneMetadata(scene)) return true;
      return Array.isArray(scene.images)
        && scene.images.some(r => isManualRated(r) || isManualCullDecision(r));
    }

    /** Highest-quality row in ``pool``; ties keep the earliest, as before. */
    function _bestByQuality(pool) {
      let best = null;
      for (const r of pool) {
        if (best === null || parseNumber(r.quality) > parseNumber(best.quality)) best = r;
      }
      return best;
    }

    /** Count manual accept / reject / undecided decisions across a scene's rows. */
    function _cullCounts(arr) {
      let accepted = 0, rejected = 0;
      for (const r of arr) {
        const status = getCullStatus(r);
        if (status === 'accept') accepted++;
        else if (status === 'reject') rejected++;
      }
      return { accepted, rejected, undecided: arr.length - accepted - rejected };
    }

    /**
     * Pick the scene thumbnail so it reflects the user's decisions, not just
     * raw quality. In order: the best accepted photo; else the best photo the
     * user has not rejected; else -- every photo was rejected -- the best of
     * those.
     *
     * Only manual decisions count. getCullStatus() already returns '' for
     * auto-culled rows, so a scene the user has not touched picks exactly the
     * representative it always did.
     */
    function _pickSceneRepresentative(arr) {
      const accepted = arr.filter(r => getCullStatus(r) === 'accept');
      if (accepted.length) return _bestByQuality(accepted);
      const notRejected = arr.filter(r => getCullStatus(r) !== 'reject');
      if (notRejected.length) return _bestByQuality(notRejected);
      return _bestByQuality(arr) || arr[0];
    }

    function aggregateScenes(minSpeciesConf, searchTerm, sortBy, includeSecondary, includeFamilies) {
      const groups = new Map();
      for (const r of rows) {
        // Prefix with folderSlot so scenes from different folders never collide
        const id = (r.__folderSlot != null ? r.__folderSlot + ':' : '') + r.scene_count;
        if (!groups.has(id)) groups.set(id, []);
        groups.get(id).push(r);
      }

      const list = [];
      for (const [sceneId, arr] of groups) {
        // Representative: best accepted, else best non-rejected, else best
        // overall. See _pickSceneRepresentative.
        const rep = _pickSceneRepresentative(arr);
        const cullCounts = _cullCounts(arr);

        const computedTags = _computeSceneTagsFromRows(arr, minSpeciesConf, includeSecondary, includeFamilies);
        let species = computedTags.species.slice().sort();
        let families = includeFamilies ? computedTags.families.slice().sort() : [];

        const maxQ = Math.max(...arr.map(a => parseNumber(a.quality)));
        const captureMsList = arr.map(a => parseCaptureTimeMs(a.capture_time)).filter(Number.isFinite);
        const captureTimeMs = captureMsList.length ? Math.min(...captureMsList) : Number.POSITIVE_INFINITY;
        const rowRp = arr[0]?.__rootPath || rootPath || '';
        const rowSc = arr[0] ? String(arr[0].scene_count) : '';
        const sdScene = rowRp && rowSc ? _scenedata[rowRp]?.scenes?.[rowSc] : null;
        const sceneName = sdScene?.name || (arr.find(a => (a.scene_name || '').trim().length)?.scene_name || '').trim();
        const isApproved = !!sdScene?.user_tags?.finalized;
        // If this scene has finalized user_tags, use them for species/family display
        if (isApproved) {
          species = (sdScene.user_tags.species || []).slice().sort();
          families = includeFamilies ? (sdScene.user_tags.families || []).slice().sort() : [];
        }

        list.push({
          id: sceneId,
          images: arr.slice().sort((a, b) => parseNumber(b.quality) - parseNumber(a.quality)),
          representative: rep,
          imageCount: arr.length,
          cullCounts,
          species,
          families,
          maxQuality: maxQ,
          captureTimeMs,
          sceneName,
          isApproved
        });
      }

      // Search predicate: match the typed query against any species or
      // family term on the scene, and -- when show_scientific_names is on
      // -- also the Latin binomial / scientific family name pulled from
      // the bird-catalog cache. The sci-name check is gated on the toggle
      // so the search experience matches the visible UI: if a user can't
      // see Latin names, typing one shouldn't covertly affect results.
      const q = (searchTerm || '').trim().toLowerCase();
      const sciSearch = !!q && _getShowSciNames();
      const filtered = q ? list.filter(s => _sceneMatchesQuery(s, q, sciSearch)) : list;

      // sort
      const sorted = filtered.sort((a, b) => {
        if (sortBy === 'captureTime') {
          if (a.captureTimeMs !== b.captureTimeMs) return a.captureTimeMs - b.captureTimeMs;
          return parseNumber(String(a.id).split(':').pop()) - parseNumber(String(b.id).split(':').pop());
        }
        if (sortBy === 'imageCount') return b.imageCount - a.imageCount;
        if (sortBy === 'sceneId') return parseNumber(String(a.id).split(':').pop()) - parseNumber(String(b.id).split(':').pop());
        return b.maxQuality - a.maxQuality;
      });

      return sorted;
    }

    function getRating(row) {
      const rp = row?.__rootPath || rootPath || '';
      const fn = row?.filename || '';
      // 1. Manual rating stored in scenedata (pywebview desktop mode)
      const sd = _scenedata[rp];
      if (sd?.image_ratings && fn && fn in sd.image_ratings) {
        const n = parseInt(sd.image_ratings[fn], 10);
        return Number.isFinite(n) ? Math.max(0, Math.min(5, n)) : 0;
      }
      // 2. Legacy row-level manual rating (FSAPI browser mode or pre-migration data)
      if (String(row?.rating_origin).toLowerCase() === 'manual') {
        const n = parseInt(row?.rating, 10);
        return Number.isFinite(n) ? Math.max(0, Math.min(5, n)) : 0;
      }
      // 3. Auto: normalized rating computed by last apply_normalization call
      const norm = parseInt(row?.__normalized_rating ?? row?.normalized_rating, 10);
      if (Number.isFinite(norm)) return Math.max(0, Math.min(5, norm));
      return 0;
    }
    function getOrigin(row) {
      const rp = row?.__rootPath || rootPath || '';
      const fn = row?.filename || '';
      const sd = _scenedata[rp];
      if (sd?.image_ratings && fn && fn in sd.image_ratings) return 'manual';
      const s = String(row?.rating_origin || '').toLowerCase();
      if (s === 'manual') return 'manual';
      const hasNorm = row?.__normalized_rating != null || (row?.normalized_rating != null && row?.normalized_rating !== '');
      return hasNorm ? 'auto' : '';
    }
    function setRating(row, val, origin = 'manual') {
      const v = Math.max(0, Math.min(5, parseInt(val, 10) || 0));
      const rp = row?.__rootPath || rootPath || '';
      const fn = row?.filename || '';
      if (hasPywebviewApi && rp && fn) {
        // pywebview desktop mode: persist rating in scenedata only
        const sd = _initScenedata(rp);
        const current = sd.image_ratings[fn];
        if (current === v && v !== 0) return; // no change
        if (v === 0) delete sd.image_ratings[fn]; else sd.image_ratings[fn] = v;
      } else {
        // FSAPI browser mode: legacy row-level storage
        const vs = String(v);
        if ((row.rating || '') === vs && (row.rating_origin || '') === origin) return;
        row.rating = vs;
        row.rating_origin = origin;
      }
      markDirty(row);
      if (typeof window.refreshSceneFilter === 'function') window.refreshSceneFilter();
    }
    function createStarBar(row) {
      const wrap = document.createElement('div');
      wrap.className = 'stars';

      function render(tempVal = null) {
        const val = tempVal != null ? tempVal : getRating(row);
        const origin = tempVal != null ? 'manual' : getOrigin(row);
        Array.from(wrap.children).forEach((st, i) => {
          const filled = i < val;
          st.classList.toggle('filled', filled);
          st.classList.toggle('manual', filled && origin === 'manual');
          st.classList.toggle('auto', filled && origin !== 'manual');
          st.textContent = filled ? '★' : '☆';
        });
      }

      for (let i = 1; i <= 5; i++) {
        const s = document.createElement('span');
        s.className = 'star';
        s.textContent = '☆';
        s.title = 'Click to set rating';
        // Click only — hover preview is handled by delegated listeners below
        s.addEventListener('click', (ev) => { ev.stopPropagation(); setRating(row, i, 'manual'); render(); });
        wrap.appendChild(s);
      }

      // 2 delegated listeners per bar instead of 10 per-star mouseenter/mouseleave
      wrap.addEventListener('mousemove', (ev) => {
        const t = ev.target;
        if (t.classList.contains('star')) {
          const idx = Array.prototype.indexOf.call(wrap.children, t);
          if (idx >= 0) render(idx + 1);
        }
      });
      wrap.addEventListener('mouseleave', () => render());

      render();
      return wrap;
    }

    function updateStatusBar(sceneList) {
      const totalImages = sceneList.reduce((acc, s) => acc + s.imageCount, 0);
      const totalScenes = sceneList.length;
      const allScenes = new Set(rows.map(r => r.scene_count)).size;
      const dirtyMark = dirty ? ' • unsaved changes' : '';
      setStatus(`Showing ${totalScenes} scenes with ${totalImages} images${totalScenes < allScenes ? ` (filtered from ${allScenes})` : ''}${dirtyMark}`);
    }

    // Render: Scenes — grouped by folder when multiple folders are loaded
