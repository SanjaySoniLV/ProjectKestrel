    let currentImageIndex = 0;
    let _currentScene = null; // reference to the scene object currently shown

    function ensureCulledColumn() {
      if (!header.includes('culled')) header.push('culled');
      for (const r of rows) { if (r.culled === undefined) r.culled = ''; }
    }

    function getCullStatus(row) {
      const raw = (row.culled === 'accept' || row.culled === 'reject') ? row.culled : '';
      if (!raw) return '';
      const origin = normalizeCullOrigin(row);
      // Auto culls are non-authoritative in filmstrip/main scene view.
      if (origin === 'auto') return '';
      return raw;
    }

    function getRawCullStatus(row) {
      return (row.culled === 'accept' || row.culled === 'reject') ? row.culled : '';
    }

    function setCullStatus(row, status) {
      ensureRatingColumns();
      row.culled = status || ''; // 'accept', 'reject', or ''
      row.culled_origin = status ? 'manual' : '';
      markDirty(row);
    }

    async function _blobUrlToBlob(url) {
      const resp = await fetch(url);
      if (!resp.ok) throw new Error(`Failed to fetch image blob (${resp.status})`);
      return await resp.blob();
    }

    async function _convertImageBlobToPng(blob) {
      if (blob.type === 'image/png') return blob;
      return await new Promise((resolve, reject) => {
        const srcUrl = URL.createObjectURL(blob);
        const img = new Image();
        img.onload = () => {
          try {
            const w = img.naturalWidth || img.width;
            const h = img.naturalHeight || img.height;
            if (!w || !h) throw new Error('Invalid image dimensions');
            const canvas = document.createElement('canvas');
            canvas.width = w;
            canvas.height = h;
            const ctx = canvas.getContext('2d');
            if (!ctx) throw new Error('Canvas context unavailable');
            ctx.drawImage(img, 0, 0, w, h);
            canvas.toBlob((pngBlob) => {
              URL.revokeObjectURL(srcUrl);
              if (!pngBlob) {
                reject(new Error('PNG conversion failed'));
                return;
              }
              resolve(pngBlob);
            }, 'image/png');
          } catch (e) {
            URL.revokeObjectURL(srcUrl);
            reject(e);
          }
        };
        img.onerror = () => {
          URL.revokeObjectURL(srcUrl);
          reject(new Error('Image decode failed'));
        };
        img.src = srcUrl;
      });
    }

    function _clearScenePreviewBox(box) {
      if (!box) return;
      try {
        if (box.__sceneCropOverlayTimer) {
          clearTimeout(box.__sceneCropOverlayTimer);
          box.__sceneCropOverlayTimer = null;
        }
      } catch (_) {}
      for (const child of Array.from(box.children)) {
        if (child.classList && child.classList.contains('scene-preview-copy-btn')) continue;
        child.remove();
      }
    }

    function _showSceneCropOverlay(row, activeIndex = 0, durationMs = 1000) {
      const box = el('#previewBox');
      if (!box || !row) return;
      const img = box.querySelector('img');
      if (!img) return;

      const cropState = getRowActiveCropState(row);
      if (!cropState.crops.length) return;

      const boxRect = box.getBoundingClientRect();
      const imgRect = img.getBoundingClientRect();
      if (imgRect.width <= 0 || imgRect.height <= 0 || boxRect.width <= 0 || boxRect.height <= 0) return;

      try {
        if (box.__sceneCropOverlayTimer) {
          clearTimeout(box.__sceneCropOverlayTimer);
          box.__sceneCropOverlayTimer = null;
        }
      } catch (_) {}

      box.querySelectorAll('.scene-crop-overlay-layer').forEach(n => n.remove());

      const layer = document.createElement('div');
      layer.className = 'scene-crop-overlay-layer';

      const originX = imgRect.left - boxRect.left;
      const originY = imgRect.top - boxRect.top;
      const drawW = imgRect.width;
      const drawH = imgRect.height;

      cropState.crops.forEach((crop, idx) => {
        const bbox = crop?.bbox || _fullFrameBbox();
        const x0 = Math.max(0, Math.min(1, _numberOr(bbox.x_min_norm, 0)));
        const x1 = Math.max(0, Math.min(1, _numberOr(bbox.x_max_norm, 1)));
        const y0 = Math.max(0, Math.min(1, _numberOr(bbox.y_min_norm, 0)));
        const y1 = Math.max(0, Math.min(1, _numberOr(bbox.y_max_norm, 1)));
        const left = originX + drawW * Math.min(x0, x1);
        const top = originY + drawH * Math.min(y0, y1);
        const width = Math.max(2, drawW * Math.abs(x1 - x0));
        const height = Math.max(2, drawH * Math.abs(y1 - y0));

        const boxEl = document.createElement('div');
        boxEl.className = `scene-crop-overlay-box ${idx === activeIndex ? 'active' : 'inactive'}`;
        boxEl.style.left = `${left}px`;
        boxEl.style.top = `${top}px`;
        boxEl.style.width = `${width}px`;
        boxEl.style.height = `${height}px`;

        const label = document.createElement('div');
        label.className = 'scene-crop-overlay-label';
        label.textContent = `#${idx + 1}`;
        boxEl.appendChild(label);
        layer.appendChild(boxEl);
      });

      box.appendChild(layer);
      requestAnimationFrame(() => layer.classList.add('visible'));
      box.__sceneCropOverlayTimer = setTimeout(() => {
        layer.classList.remove('visible');
        setTimeout(() => {
          if (layer.parentNode) layer.parentNode.removeChild(layer);
        }, 140);
      }, Math.max(250, durationMs || 1000));
    }

    async function copyRowImageToClipboard(row, relPathOverride, copyLabel = 'image') {
      const relPath = relPathOverride || row?.export_path || row?.crop_path;
      if (!relPath) {
        showToast('No image available to copy', 2500);
        return;
      }
      if (!navigator.clipboard || typeof navigator.clipboard.write !== 'function' || typeof window.ClipboardItem === 'undefined') {
        setStatus('Clipboard image copy is not supported on this system');
        showToast('Clipboard image copy is not supported on this system', 3500);
        return;
      }
      try {
        // Keep clipboard.write close to the user gesture for better compatibility.
        const pngBlobPromise = (async () => {
          const blobUrl = await getBlobUrlForPath(relPath, row.__rootPath);
          if (!blobUrl) throw new Error('Image unavailable');
          const blob = await _blobUrlToBlob(blobUrl);
          if (!blob || !blob.size) throw new Error('Empty image payload');
          return await _convertImageBlobToPng(blob);
        })();
        await navigator.clipboard.write([
          new window.ClipboardItem({ 'image/png': pngBlobPromise })
        ]);

        const label = row.filename ? `Copied ${copyLabel} (${row.filename})` : `Copied ${copyLabel}`;
        setStatus('Image copied to clipboard');
        showToast(`${label} to clipboard`, 2200);
      } catch (e) {
        console.error('copyRowImageToClipboard failed:', e);
        setStatus('Failed to copy image to clipboard');
        showToast('Failed to copy image to clipboard', 3500);
      }
    }

    function renderFilmstrip(scene) {
      const grid = el('#imageGrid');
      grid.innerHTML = '';
      const images = scene.images;
      const frag = document.createDocumentFragment();

      for (let idx = 0; idx < images.length; idx++) {
        const r = images[idx];
        const card = document.createElement('div');
        card.className = 'filmstrip-card';
        card.dataset.idx = idx;
        const cull = getCullStatus(r);
        const cullOrigin = normalizeCullOrigin(r);
        if (cull === 'accept') card.classList.add('accepted');
        if (cull === 'reject') card.classList.add('rejected');
        if (cullOrigin === 'manual') card.classList.add('manual-cull');
        if (cullOrigin === 'verified') card.classList.add('verified-cull');
        if (cullOrigin === 'auto') card.classList.add('auto-cull');
        if (idx === currentImageIndex) card.classList.add('active');

        // Thumbnail
        const th = document.createElement('div');
        th.className = 'filmstrip-thumb';
        const img = document.createElement('img');
        img.alt = r.filename || '';
        img.loading = 'lazy';
        applyThumbnailExposureToImg(img, r);
        lazyLoadImg(img, () => getBlobUrlForPath(r.export_path || r.crop_path, r.__rootPath));
        th.appendChild(img);
        card.appendChild(th);

        // Info
        const info = document.createElement('div');
        info.className = 'filmstrip-info';
        const fn = document.createElement('div');
        fn.className = 'filmstrip-filename';
        fn.textContent = r.filename || '';
        info.appendChild(fn);
        const meta = document.createElement('div');
        meta.className = 'filmstrip-meta';
        const rating = getRating(r);
        const origin = getOrigin(r);
        let starHtml = '';
        for (let s = 1; s <= 5; s++) {
          const filled = s <= rating;
          const cls = filled ? (origin === 'manual' ? 'filled manual' : 'filled auto') : '';
          starHtml += `<span class="${cls}">${filled ? '★' : '☆'}</span>`;
        }
        meta.innerHTML = `<span class="filmstrip-stars">${starHtml}</span><span>Q ${fmt3(r.quality)}</span>`;
        info.appendChild(meta);
        card.appendChild(info);

        // Tooltip with detailed metadata
        const tip = document.createElement('div');
        tip.className = 'filmstrip-tooltip';
        tip.innerHTML = [
          `<b>${escapeHtml(r.filename || '')}</b>`,
          `Species: ${escapeHtml(r.species || 'Unknown')} (${fmt3(r.species_confidence)})`,
          `Quality: ${fmt3(r.quality)}`,
          `Rating: ${'★'.repeat(rating)}${'☆'.repeat(5 - rating)} ${origin ? `(${origin})` : ''}`,
          cull ? `Status: ${cull === 'accept' ? '✓ Accepted' : '✗ Rejected'}` : '',
        ].filter(Boolean).join('<br>');
        card.appendChild(tip);

        // Click to select
        card.addEventListener('click', () => {
          if (_splitMode) return; // handled by split mode
          selectFilmstripImage(idx, scene);
        });

        // Hover to temporarily preview
        card.addEventListener('mouseenter', () => {
          if (_splitMode) return;
          selectFilmstripImage(idx, scene, true);
        });
        card.addEventListener('mouseleave', () => {
          if (_splitMode) return;
          selectFilmstripImage(currentImageIndex, scene, true);
        });

        // Double-click to open in editor
        card.addEventListener('dblclick', (ev) => { ev.stopPropagation(); openInEditor(r); });

        frag.appendChild(card);
      }
      grid.appendChild(frag);

      // Update scene navigation hints
      updateFilmstripHints(scene);
    }

    function updateFilmstripHints(scene) {
      const sceneIdx = scenes.indexOf(scene);
      const hintL = el('#filmstripHintLeft');
      const hintR = el('#filmstripHintRight');
      if (hintL) {
        if (sceneIdx > 0) { hintL.classList.remove('hidden'); }
        else { hintL.classList.add('hidden'); }
      }
      if (hintR) {
        if (sceneIdx >= 0 && sceneIdx < scenes.length - 1) { hintR.classList.remove('hidden'); }
        else { hintR.classList.add('hidden'); }
      }
    }

    function scrollFilmstripToCenter(idx) {
      const grid = el('#imageGrid');
      const card = grid?.children[idx];
      if (!card || !grid) return;
      const gridRect = grid.getBoundingClientRect();
      const cardRect = card.getBoundingClientRect();
      const targetScrollLeft = card.offsetLeft - grid.offsetWidth / 2 + card.offsetWidth / 2;
      grid.scrollTo({ left: targetScrollLeft, behavior: 'smooth' });
    }

    // Monotonic token used to invalidate stale async preview loads triggered
    // by rapid filmstrip hovers. Without this, two `await`-ing invocations
    // could each append an <img> to the preview boxes, producing a
    // "split in half" rendering as flex layout shrinks both side-by-side.
    let _filmstripPreviewToken = 0;

    async function selectFilmstripImage(idx, scene, isHover = false, overlayActiveCrop = false) {
      if (!scene || !scene.images || idx < 0 || idx >= scene.images.length) return;
      if (!isHover) {
        // Clear temporary crop selection for the image we're leaving so that
        // returning to it later shows the primary crop, not a stale temp pick.
        if (idx !== currentImageIndex && scene.images[currentImageIndex]) {
          _sceneActiveCropIndexByImage.delete(_cropStateKey(scene.images[currentImageIndex]));
        }
        currentImageIndex = idx;
      }
      const myToken = ++_filmstripPreviewToken;
      const r = scene.images[idx];
      const cropState = getRowActiveCropState(r);
      const activeCrop = cropState.activeCrop;
      const activeCropPath = activeCrop?.crop_path || r.crop_path;
      const activeSpecies = activeCrop?.species || r.species || 'Unknown';
      const activeSpeciesConf = activeCrop?.species_confidence ?? r.species_confidence;
      const activeFamily = activeCrop?.family || r.family || 'Unknown';
      const activeFamilyConf = activeCrop?.family_confidence ?? r.family_confidence;
      const activeQuality = activeCrop?.quality ?? r.quality;

      // Update filmstrip card active state and center if not just hovering
      const grid = el('#imageGrid');
      if (grid && !isHover) {
        grid.querySelectorAll('.filmstrip-card').forEach((c, i) => {
          c.classList.toggle('active', i === idx);
        });
        scrollFilmstripToCenter(idx);
      }

      // Load export preview
      const exportBox = el('#previewBox');
      if (exportBox) {
        _clearScenePreviewBox(exportBox);
        const eurl = await getBlobUrlForPath(r.export_path, r.__rootPath);
        // Bail out if a newer hover/selection has superseded this load.
        if (myToken !== _filmstripPreviewToken) return;
        // Re-clear in case other code added children while we were awaiting.
        _clearScenePreviewBox(exportBox);
        if (eurl) {
          const eimg = document.createElement('img');
          eimg.src = eurl;
          applyThumbnailExposureToImg(eimg, r);
          exportBox.appendChild(eimg);
          // Refresh "% clipped" readout + highlight clip overlay for this row.
          updateExportClipPreview(r);
        } else {
          const muted = document.createElement('span');
          muted.className = 'muted';
          muted.textContent = 'No export preview';
          exportBox.appendChild(muted);
          _setOverexposedReadout(NaN);
        }
      }
      // Reflect this image's applied exposure compensation in the slider label.
      updateExpCompEvLabel();

      // Load crop preview
      const cropBox = el('#previewCropBox');
      if (cropBox) {
        _clearScenePreviewBox(cropBox);
        const curl = await getBlobUrlForPath(activeCropPath, r.__rootPath);
        if (myToken !== _filmstripPreviewToken) return;
        _clearScenePreviewBox(cropBox);
        if (curl) {
          const cimg = document.createElement('img');
          cimg.src = curl;
          cropBox.appendChild(cimg);
        } else {
          const muted = document.createElement('span');
          muted.className = 'muted';
          muted.textContent = 'No crop preview';
          cropBox.appendChild(muted);
        }
      }

      // Wire preview copy actions for the currently displayed row.
      const copyExportBtn = el('#sceneCopyExportBtn');
      if (copyExportBtn) {
        copyExportBtn.disabled = !r.export_path;
        copyExportBtn.onmousedown = (ev) => {
          ev.stopPropagation();
          ev.preventDefault();
        };
        copyExportBtn.ondblclick = (ev) => {
          ev.stopPropagation();
          ev.preventDefault();
        };
        copyExportBtn.onclick = async (ev) => {
          ev.stopPropagation();
          ev.preventDefault();
          await copyRowImageToClipboard(r, r.export_path, 'full image');
        };
      }

      const copyCropBtn = el('#sceneCopyCropBtn');
      if (copyCropBtn) {
        copyCropBtn.disabled = !activeCropPath;
        copyCropBtn.onmousedown = (ev) => {
          ev.stopPropagation();
          ev.preventDefault();
        };
        copyCropBtn.ondblclick = (ev) => {
          ev.stopPropagation();
          ev.preventDefault();
        };
        copyCropBtn.onclick = async (ev) => {
          ev.stopPropagation();
          ev.preventDefault();
          await copyRowImageToClipboard(r, activeCropPath, 'bird crop');
        };
      }

      // Update preview panel accept/reject glow
      const exportPanel = el('#scenePreviewExport');
      const cropPanel = el('#scenePreviewCrop');
      const cull = getCullStatus(r);
      [exportPanel, cropPanel].forEach(p => {
        if (!p) return;
        p.classList.remove('scene-accepted', 'scene-rejected');
        if (cull === 'accept') p.classList.add('scene-accepted');
        if (cull === 'reject') p.classList.add('scene-rejected');
      });

      // Update info bar
      const fnEl = el('#sceneInfoFilename');
      if (fnEl) { fnEl.textContent = r.filename || '—'; fnEl.title = r.filename || ''; }

      const qEl = el('#sceneInfoQuality');
      if (qEl) qEl.textContent = `Quality: ${fmt3(activeQuality)}`;

      const cullToggle = el('#sceneCullToggle');
      if (cullToggle) {
        cullToggle.querySelectorAll('.cull-btn').forEach(btn => {
          const btnCull = btn.dataset.cull;
          btn.classList.toggle('active', btnCull === cull || (btnCull === 'none' && !cull));
          btn.onclick = (ev) => {
            ev.stopPropagation();
            const newCull = btnCull === 'none' ? null : btnCull;
            const currentRaw = getRawCullStatus(r);
            const currentNormalized = currentRaw || null;
            const forceClearAuto = newCull === null && normalizeCullOrigin(r) === 'auto' && currentRaw;
            if (currentNormalized !== newCull || forceClearAuto) {
              setCullStatus(r, newCull);
              _refreshCurrentFilmstripCard(); // re-renders card classes (borders) + info bar
              renderScenes(); // refresh timeline
            }
          };
        });
      }

      const metaTextEl = el('#sceneInfoMetaText');
      if (metaTextEl) {
        const sp = decodeEntities(activeSpecies);
        const spConf = fmt3(activeSpeciesConf);
        const fam = decodeEntities(activeFamily);
        const famConf = fmt3(activeFamilyConf);
        metaTextEl.textContent = `${sp} (${spConf}) | ${fam} (${famConf}) · Image ${idx + 1} of ${scene.images.length}`;
      }

      // Crop nav (shown only when a row exposes multiple bird crops).
      const cropNavEl = el('#sceneInfoCropNav');
      const cropLabelEl = el('#sceneInfoCropLabel');
      const cropPrevBtn = el('#sceneInfoCropPrev');
      const cropNextBtn = el('#sceneInfoCropNext');
      if (cropNavEl && cropLabelEl && cropPrevBtn && cropNextBtn) {
        const total = cropState.crops.length;
        if (total > 1) {
          cropNavEl.classList.remove('hidden');
          cropLabelEl.textContent = `Crop ${cropState.activeIndex + 1}/${total}`;
          cropPrevBtn.disabled = false;
          cropNextBtn.disabled = false;
          const cycleCrop = (step) => {
            const stateNow = getRowActiveCropState(r);
            if (stateNow.crops.length <= 1) return;
            const next = (stateNow.activeIndex + step + stateNow.crops.length) % stateNow.crops.length;
            setRowActiveCropIndex(r, next, stateNow.crops);
            selectFilmstripImage(currentImageIndex, _currentScene, true, true);
          };
          cropPrevBtn.onclick = (ev) => { ev.stopPropagation(); cycleCrop(-1); };
          cropNextBtn.onclick = (ev) => { ev.stopPropagation(); cycleCrop(1); };
        } else {
          cropNavEl.classList.add('hidden');
          cropLabelEl.textContent = '';
          cropPrevBtn.onclick = null;
          cropNextBtn.onclick = null;
        }
      }

      // "Open in editor" button — label reflects the currently configured editor.
      const editorBtn = el('#sceneInfoEditorBtn');
      const editorLabelEl = el('#sceneInfoEditorLabel');
      if (editorBtn && editorLabelEl) {
        const editorKey = getSetting('editor', 'darktable');
        editorLabelEl.textContent = `Open in ${_editorDisplayName(editorKey)}`;
        editorBtn.title = `Open original in ${_editorDisplayName(editorKey)} (Space)`;
        editorBtn.onclick = (ev) => {
          ev.stopPropagation();
          openInEditor(r);
        };
      }

      // Render star bar in info bar
      const starsEl = el('#sceneInfoStars');
      if (starsEl) {
        starsEl.innerHTML = '';
        starsEl.appendChild(createStarBar(r));
      }

      if (overlayActiveCrop) {
        _showSceneCropOverlay(r, cropState.activeIndex, 1000);
      }
    }

    // Allow other code to refresh the scene images when filter or ratings change
    window.refreshSceneFilter = function () {
      if (currentSceneId != null && _currentScene) {
        renderFilmstrip(_currentScene);
        selectFilmstripImage(currentImageIndex, _currentScene);
      }
    };

    // Render: Scene dialog
    let _splitMode = false;
    let _sceneEditMode = false;
    let _sceneEditDraft = null;

    function _beginSceneEditDraft(sceneId) {
      const current = collectSceneSpecies(sceneId);
      _sceneEditDraft = {
        sceneId: String(sceneId),
        species: current.species.slice().sort(),
        families: current.families.slice().sort(),
      };
    }

    function _finalizeSceneReview(sceneId) {
      if (!hasPywebviewApi) return false;
      const sceneRows = getSceneRows(sceneId);
      if (!sceneRows.length) return false;
      const sceneEntry = _getSceneScenedataEntry(sceneId, true, sceneRows);
      if (!sceneEntry) return false;
      const draft = (_sceneEditDraft && _sceneEditDraft.sceneId === String(sceneId))
        ? _sceneEditDraft
        : _collectCurrentlyVisibleSceneTags(sceneId);
      sceneEntry.image_filenames = sceneRows.map(r => r.filename || '').filter(Boolean);
      sceneEntry.status = 'accepted';
      sceneEntry.user_tags.species = draft.species.slice().sort();
      sceneEntry.user_tags.families = draft.families.slice().sort();
      sceneEntry.user_tags.finalized = true;
      markDirty(sceneRows);
      return true;
    }

    function collectSceneSpecies(sceneId) {
      if (_sceneEditMode && _sceneEditDraft && _sceneEditDraft.sceneId === String(sceneId)) {
        return {
          species: _sceneEditDraft.species.slice().sort(),
          families: _sceneEditDraft.families.slice().sort(),
          approved: false,
        };
      }
      const sdScene = _getSceneScenedataEntry(sceneId, false);
      if (sdScene?.user_tags?.finalized) {
        return {
          species: (sdScene.user_tags.species || []).slice().sort(),
          families: (sdScene.user_tags.families || []).slice().sort(),
          approved: true,
        };
      }
      const computed = _collectCurrentlyVisibleSceneTags(sceneId);
      return { ...computed, approved: false };
    }

    function _normalizeTagKey(tagName) {
      return String(tagName || '').trim().toLowerCase();
    }

    function _computeConfidenceWeightedSuggestion(sceneRows, tagType) {
      if (!Array.isArray(sceneRows) || !sceneRows.length) return null;
      const tallies = new Map();
      let totalWeight = 0;
      const invalid = new Set(tagType === 'species'
        ? ['no bird', 'unknown', 'n/a']
        : ['unknown', 'n/a', 'no bird']);

      for (const row of sceneRows) {
        const rawName = tagType === 'species' ? row.species : row.family;
        const name = String(rawName || '').trim();
        if (!name) continue;
        const key = _normalizeTagKey(name);
        if (!key || invalid.has(key)) continue;

        const rawConfidence = tagType === 'species' ? row.species_confidence : row.family_confidence;
        const parsedConfidence = parseNumber(rawConfidence);
        const weight = parsedConfidence >= 0 ? parsedConfidence : 1;
        if (!(weight > 0)) continue;

        const existing = tallies.get(key);
        if (existing) {
          existing.weight += weight;
          existing.count += 1;
        } else {
          tallies.set(key, { name, weight, count: 1 });
        }
        totalWeight += weight;
      }

      if (!tallies.size || !(totalWeight > 0)) return null;

      const ranked = Array.from(tallies.values()).sort((a, b) => {
        if (b.weight !== a.weight) return b.weight - a.weight;
        if (b.count !== a.count) return b.count - a.count;
        return a.name.localeCompare(b.name);
      });

      const winner = ranked[0];
      const share = winner.weight / totalWeight;
      if (!(share > 0.5)) return null;

      return {
        name: winner.name,
        share,
      };
    }

    function _computeSceneTagSuggestions(sceneId, selectedSpecies = [], selectedFamilies = []) {
      const sceneRows = getSceneRows(sceneId);
      if (!sceneRows.length) return { species: null, family: null };

      const speciesSuggestion = _computeConfidenceWeightedSuggestion(sceneRows, 'species');
      const familySuggestion = _computeConfidenceWeightedSuggestion(sceneRows, 'family');

      const selectedSpeciesKeys = new Set((selectedSpecies || []).map(_normalizeTagKey));
      const selectedFamilyKeys = new Set((selectedFamilies || []).map(_normalizeTagKey));

      return {
        species: speciesSuggestion && !selectedSpeciesKeys.has(_normalizeTagKey(speciesSuggestion.name)) ? speciesSuggestion : null,
        family: familySuggestion && !selectedFamilyKeys.has(_normalizeTagKey(familySuggestion.name)) ? familySuggestion : null,
      };
    }

    function _buildSuggestedTagButton(tagType, suggestion) {
      if (!suggestion || !suggestion.name) return '';
      const escapedName = escapeHtml(suggestion.name);
      const pct = Math.round((suggestion.share || 0) * 100);
      const readableType = tagType === 'species' ? 'species' : 'family';
      return `<button class="scene-chip-suggested" data-suggest-type="${readableType}" data-suggest-value="${escapedName}" title="Suggested ${readableType} (${pct}% confidence-weighted vote)">+ <em>${escapedName}</em></button>`;
    }

    let _activeTagInputType = null; // 'species' or 'family'
    let _activeTagInputSceneId = null;

    // Species → family taxonomy map (loaded once from backend on startup).
    // Used to auto-add the family chip when a recognized species is added and
    // to cascade-remove species when a family chip is X'd. Populated from the
    // legacy ``get_species_family_map`` endpoint so the auto-link behaviour
    // stays scoped to the 500 ML-model species the existing flow was designed
    // for -- the combobox itself sources its candidates from the bigger
    // regional bird catalog via ``search_birds``.
    let _speciesFamilyMap = null;        // { "American Robin": "Thrush sp.", ... } (canonical case)
    let _speciesFamilyMapLower = null;   // lowercase keys → { canonicalName, family }

    // Bird-catalog record cache for the new regional combobox. Records arrive
    // from the backend with ``scientific_name``, ``family_common``, ``regions``,
    // etc. We cache them keyed by lowercase canonical name so already-applied
    // species pills can render their italicised scientific-name subtext
    // without an extra round-trip per pill render.
    const _birdRecordCache = new Map();
    // Names we've asked ``lookup_birds`` about and got no record back for
    // (e.g. genus/family-level "sp." placeholders that aren't in the IOC
    // catalog). Tracked separately from the record cache so the hydration
    // loop terminates without polluting ``_lookupFamilyForSpecies`` with
    // empty-family sentinel rows.
    const _birdRecordMisses = new Set();
    let _birdCatalogMeta = null;         // { regions: [...], default_regions: [...], total_species: N }
    // family_common -> family_sci (e.g. "Hawk/Eagle/Kite sp." -> "Accipitridae").
    // Hydrated once at startup. Mirrors the catalog's full mapping so a family
    // pill can render its scientific-family subtext without needing a sibling
    // species record to be cached in the same scene.
    let _familyCommonToSci = null;       // Map or null until loaded
    function _getFamilySci(name) {
      if (!name || !_familyCommonToSci) return '';
      return _familyCommonToSci.get(String(name).trim()) || '';
    }

    async function loadSpeciesFamilyMap() {
      if (_speciesFamilyMap && _birdCatalogMeta) return;
      if (!hasPywebviewApi || !window.pywebview?.api?.get_species_family_map) {
        _speciesFamilyMap = _speciesFamilyMap || {};
        _speciesFamilyMapLower = _speciesFamilyMapLower || {};
        _birdCatalogMeta = _birdCatalogMeta || { regions: [], default_regions: ['NA'], total_species: 0 };
        return;
      }
      try {
        const res = await window.pywebview.api.get_species_family_map();
        const map = (res && res.success && res.map) ? res.map : {};
        _speciesFamilyMap = map;
        _speciesFamilyMapLower = {};
        for (const [sp, fam] of Object.entries(map)) {
          _speciesFamilyMapLower[sp.toLowerCase()] = { canonical: sp, family: fam };
        }
        kdebug(`[loadSpeciesFamilyMap] auto-link map loaded (${Object.keys(map).length} entries)`);
      } catch (e) {
        console.warn('[loadSpeciesFamilyMap] failed:', e);
        _speciesFamilyMap = {};
        _speciesFamilyMapLower = {};
      }
      // Catalog meta (regions list for settings + total catalog size for logging).
      try {
        if (window.pywebview?.api?.get_bird_catalog_meta) {
          const m = await window.pywebview.api.get_bird_catalog_meta();
          if (m && m.success) {
            _birdCatalogMeta = {
              regions: Array.isArray(m.regions) ? m.regions : [],
              default_regions: Array.isArray(m.default_regions) ? m.default_regions : ['NA'],
              total_species: typeof m.total_species === 'number' ? m.total_species : 0,
            };
            kdebug(`[loadSpeciesFamilyMap] bird catalog: ${_birdCatalogMeta.total_species} species across ${_birdCatalogMeta.regions.length} regions`);
          }
        }
      } catch (e) {
        console.warn('[bird catalog meta] failed:', e);
      }
      if (!_birdCatalogMeta) {
        _birdCatalogMeta = { regions: [], default_regions: ['NA'], total_species: 0 };
      }
      // Family-common -> family-sci direct lookup. Replaces the old
      // sibling-species fallback: family pills can now resolve their
      // Latin family name without needing a same-scene species record.
      try {
        if (window.pywebview?.api?.get_family_sci_map) {
          const fm = await window.pywebview.api.get_family_sci_map();
          if (fm && fm.success && fm.map) {
            _familyCommonToSci = new Map(Object.entries(fm.map));
            kdebug(`[loadSpeciesFamilyMap] family-sci map: ${_familyCommonToSci.size} families`);
          }
        }
      } catch (e) {
        console.warn('[family-sci map] failed:', e);
      }
      if (!_familyCommonToSci) _familyCommonToSci = new Map();
    }

    /** Return the user's selected biogeographic regions for the combobox.
     *  Falls back to the catalog meta's default selection (typically ``['NA']``). */
    function _getCurrentBirdRegions() {
      const v = getSetting('bird_regions', null);
      if (Array.isArray(v) && v.length > 0) return v.filter(r => typeof r === 'string' && r);
      if (_birdCatalogMeta && Array.isArray(_birdCatalogMeta.default_regions) && _birdCatalogMeta.default_regions.length) {
        return _birdCatalogMeta.default_regions.slice();
      }
      return ['NA'];
    }

    /** Return whether scientific-name subtext should render under species/family pills. */
    function _getShowSciNames() {
      return !!getSetting('show_scientific_names', false);
    }

    /** Match a scene against a lowercased search query.
     *
     *  Checks species and family terms. When includeSci is true, also
     *  checks the Latin binomial / family_sci pulled from the cached
     *  bird-catalog record for each species. Cache-only -- we never
     *  trigger a hydration round-trip from inside the search predicate,
     *  so sci-name search only works for species that have already been
     *  seen by a render pass or the combobox.
     */
    function _sceneMatchesQuery(scene, lcQuery, includeSci) {
      if (!scene || !lcQuery) return true;
      const species = scene.species || [];
      for (const sp of species) {
        if (sp.toLowerCase().includes(lcQuery)) return true;
      }
      const families = scene.families || [];
      for (const fm of families) {
        if (fm.toLowerCase().includes(lcQuery)) return true;
      }
      if (!includeSci) return false;
      for (const sp of species) {
        const rec = _getCachedBirdRecord(sp);
        if (!rec) continue;
        if (rec.scientific_name && rec.scientific_name.toLowerCase().includes(lcQuery)) return true;
        if (rec.family_sci && rec.family_sci.toLowerCase().includes(lcQuery)) return true;
      }
      return false;
    }

    /** Resolve the italicised subtext for a pill on a scene card.
     *
     *  Pills can be species (returns the Latin binomial) or family-tier
     *  labels (returns the scientific family, e.g. ``Columbidae``). For
     *  family-tier pills we scan sibling pills on the same scene for a
     *  cached species record whose family_common matches, then borrow
     *  its family_sci -- the same trick renderTopbarTags uses.
     */
    function _resolvePillSci(name, sceneTerms) {
      if (!name) return '';
      const rec = _getCachedBirdRecord(name);
      if (rec && rec.scientific_name) return rec.scientific_name;
      // Direct family lookup -- works for any family display name that the
      // catalog knows about, regardless of what species happen to be in
      // the same scene. This is the path that lets "Hawk/Eagle/Kite sp."
      // surface "Accipitridae" even when no hawk species crosses the
      // confidence threshold for the scene.
      const famSci = _getFamilySci(name);
      if (famSci) return famSci;
      // Sibling-species fallback (kept for free-form user-entered family
      // labels that aren't in the catalog's family_common index).
      if (!Array.isArray(sceneTerms)) return '';
      for (const other of sceneTerms) {
        if (other === name) continue;
        const r2 = _getCachedBirdRecord(other);
        if (r2 && r2.family_common === name && r2.family_sci) return r2.family_sci;
      }
      return '';
    }

    // Currently-open + popover (one at a time). Tracking it as a module
    // local lets clicks outside the popover dismiss it.
    let _morePillsPopover = null;
    function _closeMorePillsPopover() {
      if (!_morePillsPopover) return;
      const { el, onDocClick, onScroll } = _morePillsPopover;
      _morePillsPopover = null;
      document.removeEventListener('mousedown', onDocClick, true);
      document.removeEventListener('scroll', onScroll, true);
      el.remove();
    }

    /** Open a small popover anchored to ``anchor`` listing additional
     *  pills the card couldn't fit. ``names`` is the list to render and
     *  ``allTerms`` is the scene's full term list (used to derive
     *  family_sci subtext via _resolvePillSci). Dismisses on click
     *  outside, on Escape, or on scroll.
     */
    function _openMorePillsPopover(anchor, names, showSci, allTerms) {
      _closeMorePillsPopover();
      if (!Array.isArray(names) || !names.length) return;
      const pop = document.createElement('div');
      pop.className = 'card-more-popover';
      pop.setAttribute('role', 'dialog');
      for (const name of names) {
        const c = document.createElement('span');
        c.className = 'chip';
        if (showSci) c.classList.add('chip--with-sci');
        const primary = document.createElement('span');
        primary.className = 'chip-primary';
        primary.textContent = name;
        c.appendChild(primary);
        let titleStr = name;
        if (showSci) {
          const sciText = _resolvePillSci(name, allTerms);
          if (sciText) {
            const sci = document.createElement('span');
            sci.className = 'chip-sci';
            const em = document.createElement('em');
            em.textContent = sciText;
            sci.appendChild(em);
            c.appendChild(sci);
            titleStr = `${name} — ${sciText}`;
          }
        }
        c.title = titleStr;
        pop.appendChild(c);
      }
      document.body.appendChild(pop);
      const rect = anchor.getBoundingClientRect();
      const popH = pop.offsetHeight;
      const popW = pop.offsetWidth;
      let top = rect.bottom + 4;
      if (top + popH > window.innerHeight - 8) top = Math.max(8, rect.top - popH - 4);
      let left = rect.left;
      if (left + popW > window.innerWidth - 8) left = Math.max(8, window.innerWidth - popW - 8);
      pop.style.top = `${top}px`;
      pop.style.left = `${left}px`;
      const onDocClick = (ev) => {
        if (pop.contains(ev.target) || anchor.contains(ev.target)) return;
        _closeMorePillsPopover();
      };
      const onScroll = () => _closeMorePillsPopover();
      const onKey = (ev) => { if (ev.key === 'Escape') _closeMorePillsPopover(); };
      document.addEventListener('mousedown', onDocClick, true);
      document.addEventListener('scroll', onScroll, true);
      document.addEventListener('keydown', onKey, { once: true });
      _morePillsPopover = { el: pop, onDocClick, onScroll };
    }

    /** Cache a record so subsequent pill renders can show its scientific name. */
    function _cacheBirdRecord(rec) {
      if (!rec || typeof rec.canonical_common_name !== 'string') return;
      _birdRecordCache.set(rec.canonical_common_name.toLowerCase(), rec);
    }

    function _getCachedBirdRecord(name) {
      if (!name) return null;
      return _birdRecordCache.get(String(name).toLowerCase()) || null;
    }

    /** True if we've already resolved this name -- either to a record or to a
     *  confirmed miss. Render-loop guards use this so that names absent from
     *  the catalog don't re-trigger ``_hydrateBirdRecords`` on every paint. */
    function _isBirdRecordKnown(name) {
      if (!name) return false;
      const key = String(name).toLowerCase();
      return _birdRecordCache.has(key) || _birdRecordMisses.has(key);
    }

    /** Resolve a list of canonical names to records by hitting ``lookup_birds``.
     *  Already-cached names are skipped. No-op when the backend is unavailable. */
    async function _hydrateBirdRecords(names) {
      if (!Array.isArray(names) || !names.length) return;
      if (!hasPywebviewApi || !window.pywebview?.api?.lookup_birds) return;
      const need = [];
      for (const n of names) {
        if (typeof n !== 'string' || !n) continue;
        if (!_isBirdRecordKnown(n)) need.push(n);
      }
      if (!need.length) return;
      try {
        const res = await window.pywebview.api.lookup_birds(need);
        const map = (res && res.success && res.map) ? res.map : {};
        const resolvedKeys = new Set();
        for (const v of Object.values(map)) {
          _cacheBirdRecord(v);
          if (v && typeof v.canonical_common_name === 'string') {
            resolvedKeys.add(v.canonical_common_name.toLowerCase());
          }
        }
        // Record a miss for any requested name the backend didn't return so
        // the next renderScenes/renderTopbarTags pass doesn't request it
        // again. Without this, genus-level "sp." labels pin the renderer in
        // a hydrate->repaint->hydrate loop.
        for (const n of need) {
          const key = n.toLowerCase();
          if (!resolvedKeys.has(key) && !_birdRecordCache.has(key)) {
            _birdRecordMisses.add(key);
          }
        }
      } catch (e) {
        kdebug('[lookup_birds] failed:', e);
      }
    }

    // ── Custom species combobox (replaces native <datalist>) ──
    // Up to SPECIES_COMBO_MAX visible matches at a time. Arrow keys navigate
    // the highlight; Enter commits the highlighted item (or the typed value
    // if no highlight); clicking an item commits that item.
    const SPECIES_COMBO_MAX = 12;
    let _comboRequestSeq = 0;  // monotonically increasing -- drop stale responses

    /** Region-filtered fuzzy search via the backend bird catalog.
     *
     *  Resolves to a list of record dicts (caller surfaces them in the dropdown).
     *  A monotonic request id is returned alongside the results so callers can
     *  drop responses that arrive out-of-order after a newer keystroke.
     */
    async function _searchSpeciesForCombo(query) {
      const id = ++_comboRequestSeq;
      if (!hasPywebviewApi || !window.pywebview?.api?.search_birds) {
        return { id, items: [] };
      }
      const regions = _getCurrentBirdRegions();
      try {
        const res = await window.pywebview.api.search_birds(String(query || ''), regions, SPECIES_COMBO_MAX);
        const records = (res && res.success && Array.isArray(res.results)) ? res.results : [];
        // Cache every result so the next pill render can find scientific names.
        for (const rec of records) _cacheBirdRecord(rec);
        return { id, items: records };
      } catch (e) {
        kdebug('[search_birds] failed:', e);
        return { id, items: [] };
      }
    }

    /** Render the dropdown list inside the given container element.
     *
     *  Each row shows the canonical common name on the left and the family
     *  display name on the right. When the show-scientific-names toggle is on
     *  we also render the species' Latin binomial in italics beneath the
     *  common name, and the alpha-4 code in a small badge if one is set --
     *  so a user typing "AMRO" sees confirmation that ``AMRO`` → American Robin.
     */
    function _renderSpeciesComboDropdown(dropdownEl, items, highlightIdx) {
      if (!dropdownEl) return;
      if (!items || items.length === 0) {
        dropdownEl.innerHTML = '';
        dropdownEl.style.display = 'none';
        return;
      }
      const showSci = _getShowSciNames();
      const html = items.map((rec, i) => {
        const name = rec.canonical_common_name || '';
        const sci  = rec.scientific_name || '';
        const fam  = rec.family_common || '';
        const code = rec.alpha_4 || '';
        const cls = i === highlightIdx ? 'chip-combo-item chip-combo-item--active' : 'chip-combo-item';
        const sciHtml = (showSci && sci)
          ? `<span class="chip-combo-sci"><em>${escapeHtml(sci)}</em></span>` : '';
        const codeHtml = code ? `<span class="chip-combo-alpha">${escapeHtml(code)}</span>` : '';
        const famHtml  = fam ? `<span class="chip-combo-family">${escapeHtml(fam)}</span>` : '';
        return (
          `<div class="${cls}" data-combo-index="${i}" data-combo-name="${escapeHtml(name)}">` +
            `<div class="chip-combo-left">` +
              `<span class="chip-combo-name">${escapeHtml(name)}</span>` +
              sciHtml +
            `</div>` +
            `<div class="chip-combo-right">${codeHtml}${famHtml}</div>` +
          `</div>`
        );
      }).join('');
      dropdownEl.innerHTML = html;
      dropdownEl.style.display = '';
    }

    const FAMILY_COMBO_MAX = 12;
    let _familyComboRequestSeq = 0;

    async function _searchFamiliesForCombo(query) {
      const id = ++_familyComboRequestSeq;
      if (!hasPywebviewApi || !window.pywebview?.api?.search_families) {
        return { id, items: [] };
      }
      const regions = _getCurrentBirdRegions();
      try {
        const res = await window.pywebview.api.search_families(String(query || ''), regions, FAMILY_COMBO_MAX);
        const items = (res && res.success && Array.isArray(res.results)) ? res.results : [];
        return { id, items };
      } catch (e) {
        kdebug('[search_families] failed:', e);
        return { id, items: [] };
      }
    }

    function _renderFamilyComboDropdown(dropdownEl, items, highlightIdx) {
      if (!dropdownEl) return;
      if (!items || items.length === 0) {
        dropdownEl.innerHTML = '';
        dropdownEl.style.display = 'none';
        return;
      }
      const html = items.map((entry, i) => {
        const name = entry.family_common || '';
        const sci  = entry.family_sci || '';
        const order = entry.order || '';
        const cls = i === highlightIdx ? 'chip-combo-item chip-combo-item--active' : 'chip-combo-item';
        const sciHtml = sci
          ? `<span class="chip-combo-sci"><em>${escapeHtml(sci)}</em></span>` : '';
        const orderHtml = order ? `<span class="chip-combo-family">${escapeHtml(order)}</span>` : '';
        return (
          `<div class="${cls}" data-combo-index="${i}" data-combo-name="${escapeHtml(name)}">` +
            `<div class="chip-combo-left">` +
              `<span class="chip-combo-name">${escapeHtml(name)}</span>` +
              sciHtml +
            `</div>` +
            `<div class="chip-combo-right">${orderHtml}</div>` +
          `</div>`
        );
      }).join('');
      dropdownEl.innerHTML = html;
      dropdownEl.style.display = '';
    }

    /** Look up the family display name for a species name (case-insensitive).
     *  Returns { canonical, family } if matched, or null for free-form input.
     *  Prefers the cached bird-catalog record (richer info) and falls back to
     *  the legacy auto-link map for species not yet seen by the combobox. */
    function _lookupFamilyForSpecies(name) {
      if (!name) return null;
      const cached = _getCachedBirdRecord(name);
      if (cached) {
        return { canonical: cached.canonical_common_name, family: cached.family_common };
      }
      if (!_speciesFamilyMapLower) return null;
      const hit = _speciesFamilyMapLower[String(name).trim().toLowerCase()];
      return hit || null;
    }

    /** HTML for a species or family pill with optional italicised
     *  scientific-name subtext. ``primary`` is the visible chip label;
     *  ``sci`` is the Latin string (binomial for species, scientific family for
     *  family); ``removeAttr`` is the data attribute name used to wire the X
     *  button. Pass ``inline=true`` for the scene-card variant which stacks
     *  the subtext on a second line within the same chip. */
    function _renderTagPillHtml(primary, sci, options) {
      const opts = options || {};
      const chipClass = opts.chipClass || 'chip';
      const removeAttr = opts.removeAttr || '';
      const showSci = !!opts.showSci && !!sci;
      const removeBtn = removeAttr
        ? `<span class="chip-x" data-${removeAttr}="${escapeHtml(primary)}" title="Remove '${escapeHtml(primary)}'">×</span>`
        : '';
      const sciHtml = showSci
        ? `<span class="chip-sci"><em>${escapeHtml(sci)}</em></span>` : '';
      const cls = showSci ? `${chipClass} chip--with-sci` : chipClass;
      return `<span class="${cls}"><span class="chip-primary">${escapeHtml(primary)}</span>${sciHtml}${removeBtn}</span>`;
    }

    /** Add a species to the draft and, if the species is in the taxonomy map,
     *  also add its family. Returns:
     *    { speciesAdded: bool, speciesValue: string, familyAdded: string|null, matched: bool }
     *  - speciesValue is the canonical-cased name on match, the input as-typed otherwise.
     *  - familyAdded is the family display name if it was newly added, else null.
     *  - matched indicates whether the species was found in the taxonomy map.
     */
    function _applySpeciesAutoLink(draft, rawName) {
      const name = String(rawName || '').trim();
      if (!name) return { speciesAdded: false, speciesValue: '', familyAdded: null, matched: false };
      const hit = _lookupFamilyForSpecies(name);
      const speciesValue = hit ? hit.canonical : name;
      const speciesBefore = draft.species.length;
      draft.species = Array.from(new Set([...draft.species, speciesValue])).sort();
      const speciesAdded = draft.species.length !== speciesBefore;
      let familyAdded = null;
      if (hit) {
        const famBefore = draft.families.length;
        draft.families = Array.from(new Set([...draft.families, hit.family])).sort();
        if (draft.families.length !== famBefore) familyAdded = hit.family;
      }
      return { speciesAdded, speciesValue, familyAdded, matched: !!hit };
    }

    function renderTopbarTags(scene) {
      const tagsEl = el('#sceneTopbarTags');
      if (!tagsEl) return;
      const { species, families, approved } = collectSceneSpecies(scene.id);
      const suggestions = _computeSceneTagSuggestions(scene.id, species, families);
      const chipClass = approved ? 'chip manual-approved' : 'chip';
      const showSci = _getShowSciNames();

      // Kick off async hydration for any pills we don't already have records
      // cached for. The first paint may show plain pills; once records arrive
      // we re-render so the italicised scientific names appear without
      // blocking the initial render.
      if (showSci) {
        const need = (species || []).filter(sp => !_isBirdRecordKnown(sp));
        if (need.length) {
          _hydrateBirdRecords(need).then(() => {
            if (_activeTagInputSceneId === String(scene.id) || sceneDlg?.open) {
              const fresh = reloadScene(scene.id) || scene;
              renderTopbarTags(fresh);
            }
          });
        }
      }

      let html = '';
      // Species
      html += '<span class="scene-tag-label">Species:</span> ';
      if (species.length) {
        for (const sp of species) {
          const rec = _getCachedBirdRecord(sp);
          const sci = rec ? rec.scientific_name : '';
          html += _renderTagPillHtml(sp, sci, {
            chipClass, removeAttr: 'remove-species', showSci,
          });
        }
      } else {
        html += '<span class="muted" style="font-size:11px">—</span>';
      }
      if (suggestions.species) {
        html += _buildSuggestedTagButton('species', suggestions.species);
      }
      if (_activeTagInputType === 'species' && _activeTagInputSceneId === String(scene.id)) {
        html += `<span class="chip-input-wrap chip-input-wrap--species"><input type="text" class="chip-input" id="inlineTagInput" placeholder="Species..." autocomplete="off" /><button class="chip-commit-btn" title="Save">✓</button><div class="chip-input-dropdown" id="inlineTagDropdown" style="display:none"></div></span>`;
      } else {
        html += `<button class="scene-chip-add" data-add-type="species" title="Add species tag">+</button>`;
      }

      html += '<span class="scene-tag-sep"></span>';

      // Families
      html += '<span class="scene-tag-label">Family:</span> ';
      if (families.length) {
        for (const fm of families) {
          // Prefer the direct family_common -> family_sci map; fall back
          // to scanning sibling species records (free-form user-entered
          // family labels that aren't in the catalog).
          let sci = _getFamilySci(fm);
          if (!sci) {
            for (const sp of species) {
              const rec = _getCachedBirdRecord(sp);
              if (rec && rec.family_common === fm && rec.family_sci) {
                sci = rec.family_sci; break;
              }
            }
          }
          html += _renderTagPillHtml(fm, sci, {
            chipClass, removeAttr: 'remove-family', showSci,
          });
        }
      } else {
        html += '<span class="muted" style="font-size:11px">—</span>';
      }
      if (suggestions.family) {
        html += _buildSuggestedTagButton('family', suggestions.family);
      }
      if (_activeTagInputType === 'family' && _activeTagInputSceneId === String(scene.id)) {
        html += `<span class="chip-input-wrap chip-input-wrap--family"><input type="text" class="chip-input" id="inlineTagInput" placeholder="Family..." autocomplete="off" /><button class="chip-commit-btn" title="Save">✓</button><div class="chip-input-dropdown" id="inlineTagDropdown" style="display:none"></div></span>`;
      } else {
        html += `<button class="scene-chip-add" data-add-type="family" title="Add family tag">+</button>`;
      }

      if (approved) {
        html += '<span class="scene-tag-sep"></span><span class="approval-note" style="font-size:11px">✓ Reviewed</span>';
      } else {
        html += '<span class="scene-tag-sep"></span><button class="mark-reviewed-btn" id="markReviewedBtn" title="Confirm that Kestrel\'s tags are correct and mark this scene as reviewed">✓ Mark as Reviewed</button>';
      }

      tagsEl.innerHTML = html;

      // Wire remove buttons
      tagsEl.querySelectorAll('[data-remove-species]').forEach(btn => {
        btn.style.cursor = 'pointer';
        btn.onclick = () => {
          if (!_sceneEditDraft) _beginSceneEditDraft(scene.id);
          _sceneEditMode = true;
          removeSpeciesFromScene(scene, btn.dataset.removeSpecies);
          _finalizeSceneReview(scene.id);
          _sceneEditMode = false;
          _sceneEditDraft = null;
          const updatedScene = reloadScene(scene.id) || scene;
          renderTopbarTags(updatedScene);
          renderScenes();
        };
      });
      tagsEl.querySelectorAll('[data-remove-family]').forEach(btn => {
        btn.style.cursor = 'pointer';
        btn.onclick = () => {
          if (!_sceneEditDraft) _beginSceneEditDraft(scene.id);
          _sceneEditMode = true;
          const removedFamily = btn.dataset.removeFamily;
          removeFamilyFromScene(scene, removedFamily);
          // Cascade: also drop any species in the draft whose looked-up family
          // matches the removed family. Free-form species (no map entry) are
          // left in place — we cannot prove they belong to this family.
          let cascaded = 0;
          if (_sceneEditDraft && Array.isArray(_sceneEditDraft.species) && removedFamily) {
            const before = _sceneEditDraft.species.length;
            _sceneEditDraft.species = _sceneEditDraft.species.filter(sp => {
              const hit = _lookupFamilyForSpecies(sp);
              return !(hit && hit.family === removedFamily);
            });
            cascaded = before - _sceneEditDraft.species.length;
          }
          _finalizeSceneReview(scene.id);
          _sceneEditMode = false;
          _sceneEditDraft = null;
          const updatedScene = reloadScene(scene.id) || scene;
          renderTopbarTags(updatedScene);
          renderScenes();
          if (cascaded > 0) {
            showToast(`Removed family "${removedFamily}" and ${cascaded} associated ${cascaded === 1 ? 'species' : 'species tags'}`, 2200);
          }
        };
      });

      // Wire (+) add buttons
      tagsEl.querySelectorAll('.scene-chip-add').forEach(btn => {
        btn.onclick = () => {
          _activeTagInputType = btn.dataset.addType;
          _activeTagInputSceneId = String(scene.id);
          renderTopbarTags(scene);
          const inp = el('#inlineTagInput');
          if (inp) inp.focus();
        };
      });

      // Wire suggested tag buttons
      tagsEl.querySelectorAll('.scene-chip-suggested').forEach(btn => {
        btn.onclick = () => {
          const suggestType = btn.dataset.suggestType;
          const suggestValue = String(btn.dataset.suggestValue || '').trim();
          if (!suggestValue || (suggestType !== 'species' && suggestType !== 'family')) return;

          if (!_sceneEditDraft) _beginSceneEditDraft(scene.id);
          _sceneEditMode = true;

          let toastMsg = '';
          if (suggestType === 'species') {
            const result = _applySpeciesAutoLink(_sceneEditDraft, suggestValue);
            if (result.speciesAdded || result.familyAdded) {
              toastMsg = result.familyAdded
                ? `Added suggested species "${result.speciesValue}" (family: ${result.familyAdded})`
                : `Added suggested species "${result.speciesValue}"`;
            }
          } else {
            const before = _sceneEditDraft.families.length;
            _sceneEditDraft.families = Array.from(new Set([..._sceneEditDraft.families, suggestValue])).sort();
            if (_sceneEditDraft.families.length !== before) {
              toastMsg = `Added suggested family "${suggestValue}"`;
            }
          }

          if (toastMsg) {
            _finalizeSceneReview(scene.id);
            showToast(toastMsg, 2000);
          }

          _sceneEditMode = false;
          _sceneEditDraft = null;
          _activeTagInputType = null;
          _activeTagInputSceneId = null;

          const updatedScene = reloadScene(scene.id) || scene;
          renderTopbarTags(updatedScene);
          renderScenes();
        };
      });

      // Wire "Mark as Reviewed" button
      const markReviewedBtn = tagsEl.querySelector('#markReviewedBtn');
      if (markReviewedBtn) {
        markReviewedBtn.onclick = () => {
          _beginSceneEditDraft(scene.id);
          _sceneEditMode = true;
          _finalizeSceneReview(scene.id);
          _sceneEditMode = false;
          _sceneEditDraft = null;
          const updatedScene = reloadScene(scene.id) || scene;
          renderTopbarTags(updatedScene);
          renderScenes();
          showToast('Scene tags marked as reviewed', 2000);
        };
      }

      // Wire inline input (with combobox behavior for species and family)
      const inp = el('#inlineTagInput');
      if (inp) {
        const dropdown = tagsEl.querySelector('#inlineTagDropdown');
        const isSpeciesInput = _activeTagInputType === 'species';
        const isFamilyInput = _activeTagInputType === 'family';
        const hasCombo = (isSpeciesInput || isFamilyInput) && !!dropdown;
        let comboItems = [];
        let comboIndex = -1;

        const positionDropdown = () => {
          if (!dropdown || !inp) return;
          const rect = inp.getBoundingClientRect();
          dropdown.style.left = Math.round(rect.left) + 'px';
          dropdown.style.top = Math.round(rect.bottom + 4) + 'px';
          dropdown.style.minWidth = Math.max(Math.round(rect.width), 240) + 'px';
        };

        const refreshDropdown = async () => {
          if (!hasCombo) return;
          const pending = isSpeciesInput
            ? await _searchSpeciesForCombo(inp.value)
            : await _searchFamiliesForCombo(inp.value);
          if (!dropdown.isConnected) return;
          const seqRef = isSpeciesInput ? _comboRequestSeq : _familyComboRequestSeq;
          if (pending.id !== seqRef) return;
          comboItems = pending.items;
          comboIndex = comboItems.length > 0 ? 0 : -1;
          const renderFn = isSpeciesInput ? _renderSpeciesComboDropdown : _renderFamilyComboDropdown;
          renderFn(dropdown, comboItems, comboIndex);
          if (comboItems.length > 0) positionDropdown();
        };

        const renderCombo = () => {
          const renderFn = isSpeciesInput ? _renderSpeciesComboDropdown : _renderFamilyComboDropdown;
          renderFn(dropdown, comboItems, comboIndex);
        };

        const updateHighlight = (newIdx) => {
          if (!comboItems.length) return;
          const n = comboItems.length;
          comboIndex = ((newIdx % n) + n) % n;
          renderCombo();
          const active = dropdown?.querySelector('.chip-combo-item--active');
          if (active && active.scrollIntoView) {
            active.scrollIntoView({ block: 'nearest' });
          }
        };

        // Guard against re-entry: when commit() runs from Enter and then we
        // re-render, the OLD input element is detached from the DOM and its
        // onblur fires asynchronously (~150ms) — without this flag, that stale
        // blur callback re-invokes this same closure, reading the old input's
        // value but using the NEW _activeTagInputType, which can post the
        // species value as a family tag.
        let committed = false;
        const commit = (chosenValue) => {
          if (committed) return;
          committed = true;
          const raw = (chosenValue !== undefined && chosenValue !== null)
            ? String(chosenValue)
            : inp.value;
          const val = raw.trim();
          // Track whether to reopen the family input after a free-form species commit.
          let reopenFamilyInput = false;
          if (val) {
            if (!_sceneEditDraft) _beginSceneEditDraft(scene.id);
            _sceneEditMode = true;
            if (_activeTagInputType === 'species') {
              const result = _applySpeciesAutoLink(_sceneEditDraft, val);
              _finalizeSceneReview(scene.id);
              _sceneEditMode = false;
              _sceneEditDraft = null;
              if (result.matched) {
                if (result.familyAdded) {
                  showToast(`Added species "${result.speciesValue}" (family: ${result.familyAdded})`, 2200);
                } else {
                  showToast(`Added species "${result.speciesValue}"`, 2000);
                }
              } else {
                // Free-form species (not in taxonomy map). Per design, advance
                // focus to the family input so the user can add it next.
                showToast(`Added "${result.speciesValue}" — type family next`, 2200);
                reopenFamilyInput = true;
              }
            } else {
              _sceneEditDraft.families = Array.from(new Set([..._sceneEditDraft.families, val])).sort();
              _finalizeSceneReview(scene.id);
              _sceneEditMode = false;
              _sceneEditDraft = null;
              showToast(`Added family "${val}"`, 2000);
            }
          }
          if (reopenFamilyInput) {
            _activeTagInputType = 'family';
            _activeTagInputSceneId = String(scene.id);
          } else {
            _activeTagInputType = null;
            _activeTagInputSceneId = null;
          }
          const updated = reloadScene(scene.id) || scene;
          renderTopbarTags(updated);
          renderScenes();
          if (reopenFamilyInput) {
            const next = el('#inlineTagInput');
            if (next) next.focus();
          }
        };

        const _comboValueOf = (item) => {
          if (item == null) return undefined;
          if (typeof item === 'string') return item;
          if (typeof item.canonical_common_name === 'string') return item.canonical_common_name;
          if (typeof item.family_common === 'string') return item.family_common;
          return undefined;
        };

        inp.onkeydown = (e) => {
          if (hasCombo && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
            if (!comboItems.length) return;
            e.preventDefault();
            updateHighlight(comboIndex + (e.key === 'ArrowDown' ? 1 : -1));
            return;
          }
          if (e.key === 'Enter') {
            e.preventDefault();
            const chosen = (hasCombo && comboIndex >= 0 && comboIndex < comboItems.length)
              ? _comboValueOf(comboItems[comboIndex])
              : undefined;
            commit(chosen);
            return;
          }
          if (e.key === 'Escape') {
            e.preventDefault();
            _activeTagInputType = null;
            _activeTagInputSceneId = null;
            renderTopbarTags(scene);
          }
        };
        if (hasCombo) {
          inp.oninput = () => { refreshDropdown(); };
          inp.onfocus = () => { refreshDropdown(); };
          if (dropdown) {
            dropdown.onmousedown = (e) => {
              const row = e.target.closest('.chip-combo-item');
              if (!row) return;
              e.preventDefault();
              const idx = parseInt(row.dataset.comboIndex || '-1', 10);
              if (idx >= 0 && idx < comboItems.length) {
                commit(_comboValueOf(comboItems[idx]));
              }
            };
          }
          refreshDropdown();
        }
        inp.onblur = (e) => {
          // Small delay to allow clicking the commit button or a dropdown row.
          setTimeout(() => {
            if (document.activeElement === tagsEl.querySelector('.chip-commit-btn')) return;
            if (_activeTagInputType) commit();
          }, 150);
        };
        const commitBtn = tagsEl.querySelector('.chip-commit-btn');
        if (commitBtn) commitBtn.onclick = () => commit();
      }
    }

    // Keep renderSceneMetaChips as an alias for compatibility
    function renderSceneMetaChips(scene, editable) {
      renderTopbarTags(scene);
    }

    function _clampScenePreviewSplitRatio(value) {
      const n = parseFloat(value);
      if (!Number.isFinite(n)) return SCENE_PREVIEW_SPLIT_DEFAULT;
      return Math.max(SCENE_PREVIEW_SPLIT_MIN, Math.min(SCENE_PREVIEW_SPLIT_MAX, n));
    }

    function _getScenePreviewSplitSetting() {
      return _clampScenePreviewSplitRatio(getSetting(SCENE_PREVIEW_SPLIT_KEY, SCENE_PREVIEW_SPLIT_DEFAULT));
    }

    function _updateScenePreviewDividerAria(ratio) {
      const divider = document.getElementById('scenePreviewDivider');
      if (!divider) return;
      const pct = Math.round(_clampScenePreviewSplitRatio(ratio) * 100);
      divider.setAttribute('aria-valuemin', String(Math.round(SCENE_PREVIEW_SPLIT_MIN * 100)));
      divider.setAttribute('aria-valuemax', String(Math.round(SCENE_PREVIEW_SPLIT_MAX * 100)));
      divider.setAttribute('aria-valuenow', String(pct));
      divider.setAttribute('aria-valuetext', `Full image width ${pct}%`);
    }

    function _applyScenePreviewSplit(ratio) {
      const exportPanel = el('#scenePreviewExport');
      const cropPanel = el('#scenePreviewCrop');
      if (!exportPanel || !cropPanel) return;
      const clamped = _clampScenePreviewSplitRatio(ratio);
      _scenePreviewSplitRatio = clamped;
      exportPanel.style.flex = `${clamped.toFixed(4)} 1 0px`;
      cropPanel.style.flex = `${(1 - clamped).toFixed(4)} 1 0px`;
      _updateScenePreviewDividerAria(clamped);
    }

    function _persistScenePreviewSplit(ratio) {
      const clamped = _clampScenePreviewSplitRatio(ratio);
      mergeSetting(SCENE_PREVIEW_SPLIT_KEY, Number(clamped.toFixed(4)));
    }

    function _ratioFromPreviewDividerX(previewsEl, dividerEl, clientX) {
      const rect = previewsEl.getBoundingClientRect();
      const dividerWidth = dividerEl.getBoundingClientRect().width || 10;
      const usableWidth = Math.max(1, rect.width - dividerWidth);
      const minLeft = Math.max(0, Math.min(SCENE_PREVIEW_MIN_EXPORT_PX, usableWidth - SCENE_PREVIEW_MIN_CROP_PX));
      const minRight = Math.max(0, Math.min(SCENE_PREVIEW_MIN_CROP_PX, usableWidth - minLeft));
      const maxLeft = Math.max(minLeft, usableWidth - minRight);
      const leftPxRaw = clientX - rect.left - (dividerWidth * 0.5);
      const leftPx = Math.max(minLeft, Math.min(maxLeft, leftPxRaw));
      return _clampScenePreviewSplitRatio(leftPx / usableWidth);
    }

    async function openSceneDialog(sceneId, startIndex = 0) {
      const scene = scenes.find(s => String(s.id) === String(sceneId));
      if (!scene) return;
      currentSceneId = scene.id;
      _currentScene = scene;
      _splitMode = false;
      _sceneEditMode = false;
      _sceneEditDraft = null;
      currentImageIndex = startIndex;
      _applyScenePreviewSplit(_getScenePreviewSplitSetting());

      // ── Top bar: title ──
      const localNum = String(scene.id).split(':').pop();
      const folderName = folderBaseName(scene.representative?.__rootPath || '');
      let titleText = folderName || ('Scene ' + scene.id);
      titleText += ' — #' + localNum;
      if (scene.sceneName) titleText += ' — ' + scene.sceneName;
      titleText += ` (${scene.images.length} images)`;
      const titleEl = el('#sceneTopbarTitle');
      if (titleEl) titleEl.textContent = titleText;

      // ── Rename setup ──
      el('#sceneName').value = scene.sceneName || '';
      el('#sceneRenameInline').classList.add('hidden');

      // ── Pencil rename button ──
      el('#scenePencilBtn').onclick = () => {
        const renameRow = el('#sceneRenameInline');
        const isShown = !renameRow.classList.contains('hidden');
        if (isShown) {
          // Apply rename
          applySceneName(scene.id, el('#sceneName').value);
          renameRow.classList.add('hidden');
          // Update title
          const updScene = reloadScene(scene.id) || scene;
          const nm = updScene.sceneName || '';
          let t = folderName || ('Scene ' + scene.id);
          t += ' — #' + localNum;
          if (nm) t += ' — ' + nm;
          t += ` (${scene.images.length} images)`;
          titleEl.textContent = t;
          renderScenes();
        } else {
          renameRow.classList.remove('hidden');
          el('#sceneName').focus();
        }
      };
      el('#sceneRenameOk').onclick = () => { el('#scenePencilBtn').click(); };
      el('#sceneRenameCancel').onclick = () => { el('#sceneRenameInline').classList.add('hidden'); };
      el('#sceneName').onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); el('#scenePencilBtn').click(); } };

      // ── Tags ──
      renderTopbarTags(scene);

      // ── Shortcut legend toggle ──
      el('#sceneShortcutBtn').onclick = () => {
        el('#sceneShortcutLegend').classList.toggle('hidden');
      };
      el('#sceneShortcutLegend').classList.add('hidden');

      // ── Filmstrip ──
      renderFilmstrip(scene);

      // Wire horizontal scrolling via mouse wheel for filmstrip
      const grid = el('#imageGrid');
      if (grid) {
        grid.onwheel = (ev) => {
          if (ev.deltaY !== 0) {
            grid.scrollLeft += ev.deltaY;
            ev.preventDefault();
          }
        };
      }

      // ── RAW zoom on export preview (mousedown on the export preview box) ──
      const exportImgBox = el('#previewBox');
      if (exportImgBox) {
        exportImgBox.onmousedown = (ev) => {
          if (ev.button !== 0) return;
          const r = scene.images[currentImageIndex];
          if (!r) return;
          ev.preventDefault();
          startSceneZoomPreview(r, exportImgBox, ev);
        };
      }

      const cropImgBox = el('#previewCropBox');
      if (cropImgBox) {
        cropImgBox.onmouseenter = () => {
          const r = scene.images[currentImageIndex];
          if (!r) return;
          const cropState = getRowActiveCropState(r);
          _showSceneCropOverlay(r, cropState.activeIndex, 1000);
        };
      }

      // ── Close ──
      el('#closeDlg').onclick = () => {
        if (_splitMode) { exitSplitMode(); }
        const closingId = _currentScene ? String(_currentScene.id) : null;
        _sceneEditDraft = null;
        _sceneEditMode = false;
        _currentScene = null;
        document.removeEventListener('keydown', _sceneKeyHandler);
        sceneDlg.close();
        if (closingId) _focusGridCard(closingId);
      };

      // ── Split Scene ──
      el('#splitSceneBtn').onclick = () => {
        if (_splitMode) {
          if (_splitSelected.size > 0) {
            applySplitScene(scene);
          } else {
            exitSplitMode();
          }
        } else {
          enterSplitMode(scene);
        }
      };
      _updateSplitSceneButtonLabel();

      // ── Scene navigation hints ──
      const hintL = el('#filmstripHintLeft');
      const hintR = el('#filmstripHintRight');
      if (hintL) hintL.onclick = () => navigateToScene(-1);
      if (hintR) hintR.onclick = () => navigateToScene(1);

      // ── Keyboard handler ──
      document.removeEventListener('keydown', _sceneKeyHandler);
      document.addEventListener('keydown', _sceneKeyHandler);

      // ── Show dialog and select start image ──
      sceneDlg.showModal();
      await selectFilmstripImage(startIndex, scene);
    }

    // Navigate to prev/next scene — uses ID-based lookup so it survives
    // the scenes array being regenerated by auto-refresh / renderScenes.
    function navigateToScene(direction, startIndex = 0) {
      if (!_currentScene) return;
      if (_splitMode) {
        _flashSplitSceneButton();
        return;
      }
      const curId = String(_currentScene.id);
      const idx = scenes.findIndex(s => String(s.id) === curId);
      if (idx < 0) return;
      const newIdx = idx + direction;
      if (newIdx < 0 || newIdx >= scenes.length) return;
      const nextScene = scenes[newIdx];
      _sceneEditDraft = null;
      _sceneEditMode = false;
      document.removeEventListener('keydown', _sceneKeyHandler);
      sceneDlg.close();
      openSceneDialog(nextScene.id, startIndex);
    }

    // ── Review-flow shortcut helpers (used by _sceneKeyHandler) ──

    /** Mark the current scene as reviewed (same effect as clicking the button). */
    function _markCurrentSceneReviewed() {
      if (!_currentScene) return;
      _beginSceneEditDraft(_currentScene.id);
      _sceneEditMode = true;
      _finalizeSceneReview(_currentScene.id);
      _sceneEditMode = false;
      _sceneEditDraft = null;
      const updated = reloadScene(_currentScene.id) || _currentScene;
      renderTopbarTags(updated);
      renderScenes();
      showToast('Scene tags marked as reviewed', 1800);
    }

    /** Open the inline species/family tag input on the current scene and focus it. */
    function _openInlineTagInputForCurrentScene(type) {
      if (!_currentScene) return;
      if (type !== 'species' && type !== 'family') return;
      _activeTagInputType = type;
      _activeTagInputSceneId = String(_currentScene.id);
      renderTopbarTags(_currentScene);
      const inp = el('#inlineTagInput');
      if (inp) inp.focus();
    }

    /** Clear all species and family tags on the current scene (keeps reviewed state). */
    function _clearAllTagsForCurrentScene() {
      if (!_currentScene) return;
      _beginSceneEditDraft(_currentScene.id);
      _sceneEditMode = true;
      if (_sceneEditDraft) {
        _sceneEditDraft.species = [];
        _sceneEditDraft.families = [];
      }
      _finalizeSceneReview(_currentScene.id);
      _sceneEditMode = false;
      _sceneEditDraft = null;
      const updated = reloadScene(_currentScene.id) || _currentScene;
      renderTopbarTags(updated);
      renderScenes();
      showToast('Cleared all tags', 1600);
    }

    // Keyboard handler for scene dialog
    function _sceneKeyHandler(e) {
      // Skip if focused in input/textarea (but allow our inline tag input to handle its own Esc/Enter)
      const tag = (e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
      if (!_currentScene) return;

      const images = _currentScene.images;
      const len = images.length;

      const hasSceneModifier = e.ctrlKey || e.metaKey;
      const onlyCtrl = hasSceneModifier && !e.shiftKey && !e.altKey;
      const ctrlShift = hasSceneModifier && e.shiftKey && !e.altKey;
      const lowerKey = (e.key || '').toLowerCase();

      // ── Review-flow shortcuts ──
      // Ctrl+R: mark reviewed. Must preventDefault to suppress browser reload.
      if (onlyCtrl && lowerKey === 'r') {
        e.preventDefault();
        e.stopPropagation();
        _markCurrentSceneReviewed();
        return;
      }
      // Ctrl+T: open species tag input.
      if (onlyCtrl && lowerKey === 't') {
        e.preventDefault();
        e.stopPropagation();
        _openInlineTagInputForCurrentScene('species');
        return;
      }
      // Ctrl+Shift+T: open family tag input.
      if (ctrlShift && lowerKey === 't') {
        e.preventDefault();
        e.stopPropagation();
        _openInlineTagInputForCurrentScene('family');
        return;
      }
      // Ctrl+Shift+C: clear all tags. Must run before the plain-'c' cull-reject branch below.
      if (ctrlShift && lowerKey === 'c') {
        e.preventDefault();
        e.stopPropagation();
        _clearAllTagsForCurrentScene();
        return;
      }
      // Ctrl+Shift+R: reset and reassign — clear all tags, then open the species input.
      if (ctrlShift && lowerKey === 'r') {
        e.preventDefault();
        e.stopPropagation();
        _clearAllTagsForCurrentScene();
        _openInlineTagInputForCurrentScene('species');
        return;
      }

      // Tab skips to next scene; Ctrl/Cmd+Tab (or Shift+Tab) skips to previous
      if (e.key === 'Tab') {
        e.preventDefault();
        navigateToScene((hasSceneModifier || e.shiftKey) ? -1 : 1, 0);
        return;
      }

      switch (e.key) {
        case 'ArrowRight':
          e.preventDefault();
          if (hasSceneModifier) {
            // Jump to end of scene, or next scene's start if already at end
            if (currentImageIndex < len - 1) {
              selectFilmstripImage(len - 1, _currentScene);
            } else {
              navigateToScene(1, 0);
            }
          } else {
            if (currentImageIndex < len - 1) {
              selectFilmstripImage(currentImageIndex + 1, _currentScene);
            } else {
              navigateToScene(1, 0);
            }
          }
          break;
        case 'End':
          e.preventDefault();
          // Same behavior as Ctrl/Cmd+ArrowRight.
          if (currentImageIndex < len - 1) {
            selectFilmstripImage(len - 1, _currentScene);
          } else {
            navigateToScene(1, 0);
          }
          break;
        case 'ArrowLeft':
          e.preventDefault();
          if (hasSceneModifier) {
            // Jump to start of scene, or prev scene's start if already at start
            if (currentImageIndex > 0) {
              selectFilmstripImage(0, _currentScene);
            } else {
              navigateToScene(-1, 0);
            }
          } else {
            if (currentImageIndex > 0) {
              selectFilmstripImage(currentImageIndex - 1, _currentScene);
            } else {
              // At first image — jump to previous scene's LAST image
              const prevIdx = scenes.indexOf(_currentScene) - 1;
              if (prevIdx >= 0) {
                const prevScene = scenes[prevIdx];
                navigateToScene(-1, prevScene.images.length - 1);
              }
            }
          }
          break;
        case 'Home':
          e.preventDefault();
          // Same behavior as Ctrl/Cmd+ArrowLeft.
          if (currentImageIndex > 0) {
            selectFilmstripImage(0, _currentScene);
          } else {
            navigateToScene(-1, 0);
          }
          break;
        case 'ArrowUp':
        case 'ArrowDown':
          e.preventDefault();
          if (images[currentImageIndex]) {
            const row = images[currentImageIndex];
            const cropState = getRowActiveCropState(row);
            if (cropState.crops.length > 1) {
              const step = e.key === 'ArrowDown' ? 1 : -1;
              const next = (cropState.activeIndex + step + cropState.crops.length) % cropState.crops.length;
              setRowActiveCropIndex(row, next, cropState.crops);
              selectFilmstripImage(currentImageIndex, _currentScene, true, true);
            }
          }
          break;
        case 'Enter':
          e.preventDefault();
          if (images[currentImageIndex]) {
            const row = images[currentImageIndex];
            const stateBefore = getRowActiveCropState(row);
            if (stateBefore.crops.length > 0) {
              const changed = promoteActiveCropToPrimary(row);
              if (changed) {
                void (async () => {
                  await _resortAndFocusSceneImage(row, true);
                  await renderScenes();
                })();
              } else {
                selectFilmstripImage(currentImageIndex, _currentScene, true, true);
              }
              showToast(`Primary crop set to ${stateBefore.activeIndex + 1}/${stateBefore.crops.length}`, 1800);
            }
          }
          break;
        case 'z':
        case 'Z':
          e.preventDefault();
          if (images[currentImageIndex]) {
            setCullStatus(images[currentImageIndex], 'accept');
            _refreshCurrentFilmstripCard();
          }
          break;
        case 'x':
        case 'X':
          e.preventDefault();
          if (images[currentImageIndex]) {
            setCullStatus(images[currentImageIndex], '');
            _refreshCurrentFilmstripCard();
          }
          break;
        case 'c':
        case 'C':
          e.preventDefault();
          if (images[currentImageIndex]) {
            setCullStatus(images[currentImageIndex], 'reject');
            _refreshCurrentFilmstripCard();
          }
          break;
        case '1': case '2': case '3': case '4': case '5':
          e.preventDefault();
          if (images[currentImageIndex]) {
            setRating(images[currentImageIndex], parseInt(e.key, 10), 'manual');
            _refreshCurrentFilmstripCard();
          }
          break;
        case ' ':
          e.preventDefault();
          if (images[currentImageIndex]) openInEditor(images[currentImageIndex]);
          break;
        case 'Escape':
          e.preventDefault();
          el('#closeDlg')?.click();
          break;
      }
    }

    // Refresh the current filmstrip card + info bar after a status/rating change
    function _refreshCurrentFilmstripCard() {
      if (!_currentScene) return;
      // Re-render the filmstrip to update card classes
      renderFilmstrip(_currentScene);
      // Re-select the current image to update previews and info bar
      selectFilmstripImage(currentImageIndex, _currentScene);
    }

    async function _resortAndFocusSceneImage(targetRow, overlayActiveCrop = true) {
      if (!_currentScene || !targetRow) return;

      // Rebuild the scene from authoritative rows so quality changes are
      // reflected immediately in quality-sorted filmstrip order.
      const refreshed = reloadScene(_currentScene.id) || _currentScene;
      _currentScene = refreshed;

      const targetKey = _cropStateKey(targetRow);
      let nextIndex = refreshed.images.findIndex(r => _cropStateKey(r) === targetKey);
      if (nextIndex < 0) {
        nextIndex = refreshed.images.findIndex(
          r => String(r.filename || '') === String(targetRow.filename || '')
            && String(r.__rootPath || '') === String(targetRow.__rootPath || '')
        );
      }
      if (nextIndex < 0) {
        nextIndex = Math.max(0, Math.min(currentImageIndex, Math.max(0, refreshed.images.length - 1)));
      }

      currentImageIndex = nextIndex;
      renderFilmstrip(refreshed);
      await selectFilmstripImage(nextIndex, refreshed, false, overlayActiveCrop);
    }

    function applySceneName(sceneId, name) {
      const newName = String(name || '').trim();
      const { slot, sceneCount } = _getSceneIdParts(sceneId);
      let rowChanged = 0;
      let rp = null;
      const sceneRows = [];
      for (const r of rows) {
        const slotMatch = slot === null || (r.__folderSlot ?? 0) === slot;
        if (slotMatch && String(r.scene_count) === sceneCount) {
          if (!rp && r.__rootPath) rp = r.__rootPath;
          sceneRows.push(r);
          if ((r.scene_name || '') !== newName) { r.scene_name = newName; rowChanged++; }
        }
      }
      // Persist scene name in scenedata (pywebview mode)
      let sdChanged = false;
      if (hasPywebviewApi && rp) {
        const sceneEntry = _getSceneScenedataEntry(sceneId, true, sceneRows);
        if (sceneEntry) {
          sceneEntry.image_filenames = sceneRows.map(r => r.filename || '').filter(Boolean);
          if (sceneEntry.name !== newName) { sceneEntry.name = newName; sdChanged = true; }
        }
      }
      if (rowChanged || sdChanged) {
        markDirty(rp || sceneRows);
        const updatedScene = reloadScene(sceneId);
        if (updatedScene) renderSceneMetaChips(updatedScene, _sceneEditMode);
        renderScenes();
      }
    }

    // --- Species & Family editing helpers ---
    function markDirty(rootHint = null) {
      if (hasPywebviewApi) {
        const scoped = _markDirtyRoots(rootHint);
        if (!scoped) _dirtyRootsUnknown = true;
      }
      attemptAutoSave();
    }

    function _syncSceneUserTags() {
      // Tag edits stay in the edit-session draft until the user clicks Done Editing.
    }

    function getSceneRows(sceneId) {
      const parts = String(sceneId).split(':');
      const sceneCount = parts.pop();
      const slot = parts.length ? parseInt(parts[0], 10) : null;
      return rows.filter(r => {
        const slotMatch = slot === null || (r.__folderSlot ?? 0) === slot;
        return slotMatch && String(r.scene_count) === sceneCount;
      });
    }

    function removeSpeciesFromScene(scene, speciesName) {
      if (!_sceneEditDraft || _sceneEditDraft.sceneId !== String(scene.id)) return;
      const before = _sceneEditDraft.species.length;
      _sceneEditDraft.species = _sceneEditDraft.species.filter(sp => sp !== speciesName).sort();
      const changed = before - _sceneEditDraft.species.length;
      if (changed) {
        const updatedScene = reloadScene(scene.id);
        if (updatedScene) {
          renderSceneMetaChips(updatedScene, _sceneEditMode);
        }
        showToast(`Removed "${speciesName}" from reviewed scene tags`, 2000);
      }
    }

    function removeFamilyFromScene(scene, familyName) {
      if (!_sceneEditDraft || _sceneEditDraft.sceneId !== String(scene.id)) return;
      const before = _sceneEditDraft.families.length;
      _sceneEditDraft.families = _sceneEditDraft.families.filter(fm => fm !== familyName).sort();
      const changed = before - _sceneEditDraft.families.length;
      if (changed) {
        const updatedScene = reloadScene(scene.id);
        if (updatedScene) {
          renderSceneMetaChips(updatedScene, _sceneEditMode);
        }
        showToast(`Removed family "${familyName}" from reviewed scene tags`, 2000);
      }
    }

    function addSpeciesToScene(scene) {
      const input = el('#editAddSpecies');
      const name = (input.value || '').trim();
      if (!name) return;
      
      const wasEdit = _sceneEditMode;
      if (!_sceneEditDraft) _beginSceneEditDraft(scene.id);
      _sceneEditMode = true;
      
      const before = _sceneEditDraft.species.length;
      _sceneEditDraft.species = Array.from(new Set([..._sceneEditDraft.species, name])).sort();
      const changed = _sceneEditDraft.species.length !== before;
      
      if (changed) {
        _finalizeSceneReview(scene.id);
        input.value = '';
        const updatedScene = reloadScene(scene.id) || scene;
        renderTopbarTags(updatedScene);
        renderScenes();
        showToast(`Added species "${name}" to reviewed scene tags`, 2000);
      }
      
      if (!wasEdit) {
        _sceneEditMode = false;
        _sceneEditDraft = null;
      }
      el('#editPanel')?.classList.add('hidden');
    }

    function addFamilyToScene(scene) {
      const input = el('#editAddFamily');
      const name = (input.value || '').trim();
      if (!name) return;
      
      const wasEdit = _sceneEditMode;
      if (!_sceneEditDraft) _beginSceneEditDraft(scene.id);
      _sceneEditMode = true;
      
      const before = _sceneEditDraft.families.length;
      _sceneEditDraft.families = Array.from(new Set([..._sceneEditDraft.families, name])).sort();
      const changed = _sceneEditDraft.families.length !== before;
      
      if (changed) {
        _finalizeSceneReview(scene.id);
        input.value = '';
        const updatedScene = reloadScene(scene.id) || scene;
        renderTopbarTags(updatedScene);
        renderScenes();
        showToast(`Added family "${name}" to reviewed scene tags`, 2000);
      }
      
      if (!wasEdit) {
        _sceneEditMode = false;
        _sceneEditDraft = null;
      }
      el('#editPanel')?.classList.add('hidden');
    }

    function reloadScene(sceneId) {
      const minC = parseFloat(el('#speciesConf').value) || 0;
      const search = el('#search').value;
      const sortBy = el('#sortBy').value;
      const includeSecondary = document.getElementById('includeSecondarySpecies')?.checked ?? false;
      const all = aggregateScenes(minC, search, sortBy, includeSecondary, true);
      return all.find(s => String(s.id) === String(sceneId));
    }

    function refreshSceneMeta(scene) {
      renderSceneMetaChips(scene, _sceneEditMode);
    }

    // --- Scene split helpers ---
    let _splitSelected = new Set();
    let _splitLastSelectedIndex = -1;

    function _updateSplitSceneButtonLabel() {
      const btn = el('#splitSceneBtn');
      if (!btn) return;
      if (!_splitMode) {
        btn.textContent = 'Split Scene…';
        return;
      }
      btn.textContent = _splitSelected.size > 0
        ? 'Create New Scene from Selected'
        : 'Cancel Scene Split';
    }

    function _flashSplitSceneButton() {
      const btn = el('#splitSceneBtn');
      if (!btn) return;
      btn.classList.remove('split-scene-btn-flash');
      void btn.offsetWidth;
      btn.classList.add('split-scene-btn-flash');
    }

    function enterSplitMode(scene) {
      _splitMode = true;
      _splitSelected.clear();
      _splitLastSelectedIndex = -1;
      _updateSplitSceneButtonLabel();
      showToast('Click images to select them for the new scene. Use Shift+Click for ranges, or click "Cancel Scene Split" to exit.', 4500);
      // Re-render images with checkboxes
      renderSceneImagesWithSplit(scene);
    }

    function exitSplitMode() {
      _splitMode = false;
      _splitSelected.clear();
      _splitLastSelectedIndex = -1;
      _updateSplitSceneButtonLabel();
      // Re-render images without checkboxes
      const scene = scenes.find(s => String(s.id) === String(currentSceneId));
      if (scene) {
        renderFilmstrip(scene);
        selectFilmstripImage(currentImageIndex, scene);
      }
    }

    function renderSceneImagesWithSplit(scene) {
      const infoBox = el('#previewInfo');
      if (infoBox) infoBox.textContent = '—';
      const grid = el('#imageGrid');
      grid.innerHTML = '';
      
      // Temporarily sort images by filename for splitting
      const images = scene.images.slice().sort((a, b) => {
        return (a.filename || '').localeCompare(b.filename || '');
      });
      const frag = document.createDocumentFragment();
      const splitCards = [];
      const splitChecks = [];

      const setSplitCardChecked = (idx, checked) => {
        const cb = splitChecks[idx];
        const card = splitCards[idx];
        if (!cb || !card) return;
        cb.checked = !!checked;
        const key = cb.dataset.splitKey || '';
        if (!key) return;
        if (checked) {
          _splitSelected.add(key);
          card.classList.add('split-selected');
        } else {
          _splitSelected.delete(key);
          card.classList.remove('split-selected');
        }
      };

      const toggleSplitAtIndex = (idx, useRange = false, desiredState = null) => {
        if (idx < 0 || idx >= splitChecks.length) return;
        const targetChecked = desiredState == null ? !splitChecks[idx].checked : !!desiredState;

        if (useRange && _splitLastSelectedIndex >= 0) {
          const lo = Math.min(_splitLastSelectedIndex, idx);
          const hi = Math.max(_splitLastSelectedIndex, idx);
          for (let j = lo; j <= hi; j++) setSplitCardChecked(j, targetChecked);
        } else {
          setSplitCardChecked(idx, targetChecked);
        }

        _splitLastSelectedIndex = idx;
        _updateSplitSceneButtonLabel();
      };

      for (let i = 0; i < images.length; i++) {
        const r = images[i];
        const origIdx = scene.images.indexOf(r);
        const card = document.createElement('div');
        card.className = 'filmstrip-card split-mode';
        card.dataset.idx = origIdx;
        const key = r.filename || r.export_path || '';

        // Checkbox for split selection
        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.className = 'split-check';
        cb.dataset.splitKey = key;
        cb.checked = _splitSelected.has(key);
        if (cb.checked) card.classList.add('split-selected');

        splitCards.push(card);
        splitChecks.push(cb);

        cb.addEventListener('click', (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          toggleSplitAtIndex(i, !!ev.shiftKey);
        });
        card.appendChild(cb);

        // Thumbnail
        const th = document.createElement('div');
        th.className = 'filmstrip-thumb';
        const img = document.createElement('img');
        img.alt = r.filename || '';
        img.loading = 'lazy';
        applyThumbnailExposureToImg(img, r);
        lazyLoadImg(img, () => getBlobUrlForPath(r.export_path || r.crop_path, r.__rootPath));
        th.appendChild(img);
        card.appendChild(th);

        // Info
        const info = document.createElement('div');
        info.className = 'filmstrip-info';
        const fn = document.createElement('div');
        fn.className = 'filmstrip-filename';
        fn.textContent = r.filename || '';
        info.appendChild(fn);
        const meta = document.createElement('div');
        meta.className = 'filmstrip-meta';
        const rating = getRating(r);
        meta.innerHTML = `<span class="filmstrip-stars">${'★'.repeat(rating)}${'☆'.repeat(5 - rating)}</span><span>Q ${fmt3(r.quality)}</span>`;
        info.appendChild(meta);
        card.appendChild(info);

        // Click card to toggle selection
        card.addEventListener('click', (ev) => {
          ev.preventDefault();
          toggleSplitAtIndex(i, !!ev.shiftKey);
        });

        // Hover preview only (split mode click should not load/activate)
        card.addEventListener('mouseenter', () => {
          selectFilmstripImage(origIdx, scene, true);
        });
        card.addEventListener('mouseleave', () => {
          selectFilmstripImage(currentImageIndex, scene, true);
        });

        // Tooltip with detailed metadata
        const tip = document.createElement('div');
        tip.className = 'filmstrip-tooltip';
        tip.innerHTML = [
          `<b>${escapeHtml(r.filename || '')}</b>`,
          `Species: ${escapeHtml(r.species || 'Unknown')} (${fmt3(r.species_confidence)})`,
          `Quality: ${fmt3(r.quality)}`,
          `Rating: ${'★'.repeat(rating)}${'☆'.repeat(5 - rating)}`,
        ].filter(Boolean).join('<br>');
        card.appendChild(tip);

        frag.appendChild(card);
      }
      grid.appendChild(frag);
      updateFilmstripHints(scene);
      
      // Select the first one or current one by default to show preview
      if (images.length > 0) {
        const previewIndex = Math.min(currentImageIndex, Math.max(0, scene.images.length - 1));
        selectFilmstripImage(previewIndex, scene, true);
      }
    }

    function applySplitScene(scene) {
      if (_splitSelected.size === 0) {
        showToast('Select at least one image to split into a new scene', 3000);
        return;
      }
      if (_splitSelected.size === scene.images.length) {
        showToast('Cannot move all images — at least one must remain in the original scene', 3000);
        return;
      }
      // Find next available scene_count across the same folder slot
      const parts = String(scene.id).split(':');
      const slot = parts.length > 1 ? parseInt(parts[0], 10) : null;
      let maxCount = 0;
      for (const r of rows) {
        const slotMatch = slot === null || (r.__folderSlot ?? 0) === slot;
        if (slotMatch) {
          const c = parseInt(r.scene_count, 10);
          if (Number.isFinite(c) && c > maxCount) maxCount = c;
        }
      }
      const newSceneCount = String(maxCount + 1);
      // Snapshot scene rows BEFORE mutation so we can build scenedata diff
      const sceneRowsBefore = getSceneRows(scene.id).slice();
      const rpForSplit = sceneRowsBefore[0]?.__rootPath || rootPath || '';
      let moved = 0;
      for (const r of sceneRowsBefore) {
        const key = r.filename || r.export_path || '';
        if (_splitSelected.has(key)) {
          r.scene_count = newSceneCount;
          r.scene_name = '';
          moved++;
        }
      }
      if (moved) {
        // Update scenedata scene membership
        if (hasPywebviewApi && rpForSplit) {
          const parts2 = String(scene.id).split(':');
          const oldSceneCount = parts2.pop();
          const sd = _initScenedata(rpForSplit);
          const movedRows = sceneRowsBefore.filter(r => _splitSelected.has(r.filename || r.export_path || ''));
          const remainRows = sceneRowsBefore.filter(r => !_splitSelected.has(r.filename || r.export_path || ''));
          const movedFilenames = movedRows.map(r => r.filename || '').filter(Boolean);
          const remainFilenames = remainRows.map(r => r.filename || '').filter(Boolean);
          if (sd.scenes[oldSceneCount]) {
            sd.scenes[oldSceneCount].image_filenames = remainFilenames;
            // An approved scene shows its stored tags instead of the ones its
            // rows imply, so without this the original scene keeps species
            // that only the images we just moved out accounted for.
            _pruneFinalizedSceneTagsAfterRemoval(sd.scenes[oldSceneCount], remainRows, movedRows);
          }
          sd.scenes[newSceneCount] = {
            scene_id: newSceneCount,
            image_filenames: movedFilenames,
            name: '',
            status: 'pending',
            user_tags: { species: [], families: [], finalized: false }
          };
        }
        markDirty(rpForSplit || sceneRowsBefore);
        _splitMode = false;
        _splitSelected.clear();
        _splitLastSelectedIndex = -1;
        _updateSplitSceneButtonLabel();
        renderScenes();
        // Refresh the scene dialog with the remaining images
        const updatedScene = reloadScene(scene.id);
        if (updatedScene) {
          refreshSceneMeta(updatedScene);
          renderFilmstrip(updatedScene);
          selectFilmstripImage(0, updatedScene);
          el('#sceneName').value = updatedScene.sceneName || '';
        }
        showToast(`Split ${moved} image(s) into new scene #${newSceneCount}`, 3000);
      }
    }

    function fmt3(v) { const n = parseNumber(v); return n < 0 ? '—' : n.toFixed(3); }

    function decodeEntities(s) {
      if (!s || typeof s !== 'string') return s;
      const txt = document.createElement('textarea');
      txt.innerHTML = s;
      return txt.value;
    }
    function escapeHtml(s) { return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', '\'': '&#39;' }[c])); }
    function folderBaseName(path) { if (!path) return ''; return path.replace(/\\/g, '/').split('/').filter(Boolean).pop() || path; }

    // Snapshot helpers for revert
    function takeSnapshot() {
      _cleanSnapshot = { rows: rows.map(r => ({ ...r })), header: header.slice(), scenedata: JSON.parse(JSON.stringify(_scenedata)) };
      _clearDirtyRoots();
      const btn = el('#revertCsv');
      if (btn) btn.disabled = true;
    }
    function applySnapshot() {
      if (!_cleanSnapshot) return;
      clearTimeout(_autoSaveTimer);
      _autoSaveTimer = null;
      rows = _cleanSnapshot.rows.map(r => ({ ...r }));
      header = _cleanSnapshot.header.slice();
      if (_cleanSnapshot.scenedata !== undefined) _scenedata = JSON.parse(JSON.stringify(_cleanSnapshot.scenedata));
      _sceneActiveCropIndexByImage.clear();
      _clearDirtyRoots();
      _setDirtyUi(false);
      blobUrlCache.clear();
      renderScenes();
      setStatus('Reverted to last saved state.');
    }

    function csvEscape(val) {
      if (val == null) return '';
      let s = String(val);
      if (/[",\n]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"';
      return s;
    }

    async function saveCsv() {
      clearTimeout(_autoSaveTimer);
      _autoSaveTimer = null;
      ensureSceneNameColumn();
      ensureRatingColumns();
      const allCols = header.slice();
      if (!allCols.includes('scene_name')) allCols.push('scene_name');
      if (!allCols.includes('rating')) allCols.push('rating');
      if (!allCols.includes('rating_origin')) allCols.push('rating_origin');

      // Serialize a list of rows (excluding internal __ keys) to CSV string
      function rowsToCsvString(colList, rowList) {
        const lines = [colList.join(',')];
        for (const r of rowList) lines.push(colList.map(k => csvEscape(k in r ? r[k] : '')).join(','));
        return lines.join('\r\n');
      }

      // Pywebview desktop mode: save both CSV row state and scenedata JSON.
      if (window.pywebview?.api) {
        const groups = new Map();
        for (const r of rows) {
          const rp = _normalizeRootPath(r.__rootPath || rootPath || '');
          if (!rp) continue;
          if (!groups.has(rp)) groups.set(rp, []);
          groups.get(rp).push(r);
        }
        const allRoots = Array.from(groups.keys());
        let rootsToSave = allRoots;
        if (_dirtyRootsUnknown) {
          rootsToSave = allRoots;
        } else if (_dirtyRoots.size > 0) {
          rootsToSave = allRoots.filter(rp => _dirtyRoots.has(rp));
          if (!rootsToSave.length && dirty) rootsToSave = allRoots;
        } else if (dirty) {
          rootsToSave = allRoots;
        } else {
          rootsToSave = [];
        }
        if (!rootsToSave.length) {
          setStatus(dirty ? 'No folder changes to save.' : 'No unsaved changes.');
          return;
        }

        let saved = 0, failed = 0;
        const failedRoots = new Set();
        const exportCols = allCols.filter(c => !String(c).startsWith('__'));
        for (const rp of rootsToSave) {
          const groupRows = groups.get(rp) || [];
          try {
            // Persist cull/rating columns to CSV so culling assistant and reloads see authoritative state.
            if (typeof window.pywebview.api.write_kestrel_csv === 'function') {
              const content = rowsToCsvString(exportCols, groupRows);
              const csvRes = await window.pywebview.api.write_kestrel_csv(rp, content);
              if (!csvRes?.success) throw new Error(csvRes?.error || 'Failed to write kestrel_database.csv');
            }

            const sd = _normalizeScenedataForSave(rp, groupRows);
            const res = await window.pywebview.api.write_kestrel_scenedata(rp, sd);
            if (res.success) saved++;
            else {
              failed++;
              failedRoots.add(rp);
              console.warn('[save scenedata] Failed for', rp, res.error);
            }
          } catch (e) {
            failed++;
            failedRoots.add(rp);
            console.warn('[save pywebview] Error for', rp, e);
          }
        }
        if (failed > 0) {
          _dirtyRoots = failedRoots;
          _dirtyRootsUnknown = false;
          _setDirtyUi(true);
          setStatus(`Saved ${saved} folder(s), ${failed} failed`);
        } else {
          _clearDirtyRoots();
          _setDirtyUi(false);
          takeSnapshot();
          setStatus(`Saved changes to ${saved} folder(s)`);
        }
        return;
      }

      setStatus('Save unavailable: Desktop API not detected.');
    }

    // Warn user on unsaved changes when attempting to close/refresh
    window.addEventListener('beforeunload', (e) => {
      const analysisRunning = window.__queueRunning;
      if (dirty || analysisRunning) {
        const msg = analysisRunning
          ? 'Analysis is still running. Closing the page will stop the analysis.'
          : 'You have unsaved changes. Are you sure you want to leave?';
        e.preventDefault();
        e.returnValue = msg;
        return msg;
      }
    });

    // Cleanup local caches when the page unloads.
    window.addEventListener('unload', () => {
      try {
        _cleanupCachesOnAppClose();
      } catch (_) { }
    });

    // Scene preview divider: drag to resize Full Image vs Bird Crop panes
    (function setupScenePreviewDivider() {
      const previews = document.getElementById('scenePreviews');
      const divider = document.getElementById('scenePreviewDivider');
      if (!previews || !divider) return;

      let dragging = false;
      let activePointerId = null;

      const applyFromClientX = (clientX) => {
        const ratio = _ratioFromPreviewDividerX(previews, divider, clientX);
        _applyScenePreviewSplit(ratio);
      };

      const finishDrag = (persist) => {
        if (!dragging) return;
        dragging = false;
        divider.classList.remove('dragging');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
        if (persist) {
          _persistScenePreviewSplit(_scenePreviewSplitRatio);
        }
      };

      divider.addEventListener('pointerdown', (e) => {
        if (e.button !== 0) return;
        dragging = true;
        activePointerId = e.pointerId;
        divider.classList.add('dragging');
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
        try { divider.setPointerCapture(activePointerId); } catch (_) { }
        applyFromClientX(e.clientX);
        e.preventDefault();
      });

      divider.addEventListener('pointermove', (e) => {
        if (!dragging) return;
        if (activePointerId !== null && e.pointerId !== activePointerId) return;
        applyFromClientX(e.clientX);
        e.preventDefault();
      });

      divider.addEventListener('pointerup', (e) => {
        if (!dragging) return;
        if (activePointerId !== null && e.pointerId !== activePointerId) return;
        try { divider.releasePointerCapture(e.pointerId); } catch (_) { }
        activePointerId = null;
        finishDrag(true);
      });

      divider.addEventListener('pointercancel', () => {
        activePointerId = null;
        finishDrag(true);
      });

      divider.addEventListener('dblclick', () => {
        _applyScenePreviewSplit(SCENE_PREVIEW_SPLIT_DEFAULT);
        _persistScenePreviewSplit(_scenePreviewSplitRatio);
      });

      divider.addEventListener('keydown', (e) => {
        let next = _scenePreviewSplitRatio;
        const step = e.shiftKey ? 0.05 : 0.02;
        if (e.key === 'ArrowLeft') next -= step;
        else if (e.key === 'ArrowRight') next += step;
        else if (e.key === 'Home') next = SCENE_PREVIEW_SPLIT_MIN;
        else if (e.key === 'End') next = SCENE_PREVIEW_SPLIT_MAX;
        else return;
        e.preventDefault();
        _applyScenePreviewSplit(next);
        _persistScenePreviewSplit(_scenePreviewSplitRatio);
      });

      _applyScenePreviewSplit(_getScenePreviewSplitSetting());
      window.addEventListener('resize', () => {
        if (!sceneDlg?.open) return;
        _applyScenePreviewSplit(_scenePreviewSplitRatio);
      });
    })();

    // Settings storage
