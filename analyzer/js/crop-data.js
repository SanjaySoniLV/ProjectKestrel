    const _sceneActiveCropIndexByImage = new Map();

    function _cropStateKey(row) {
      return `${row?.__rootPath || rootPath || ''}|${row?.filename || ''}`;
    }

    function _numberOr(value, fallback = 0) {
      const n = parseFloat(value);
      return Number.isFinite(n) ? n : fallback;
    }

    function _intOr(value, fallback = 0) {
      const n = parseInt(value, 10);
      return Number.isFinite(n) ? n : fallback;
    }

    function _clamp01(value, fallback) {
      const n = parseFloat(value);
      if (!Number.isFinite(n)) return fallback;
      return Math.max(0, Math.min(1, n));
    }

    function _fullFrameBbox() {
      return {
        x_min_norm: 0,
        x_max_norm: 1,
        y_min_norm: 0,
        y_max_norm: 1,
        x_center_norm: 0.5,
        y_center_norm: 0.5,
      };
    }

    function _normalizeCropBbox(rawBbox) {
      const b = rawBbox && typeof rawBbox === 'object' ? rawBbox : {};
      const norm = {
        x_min_norm: _clamp01(b.x_min_norm, 0),
        x_max_norm: _clamp01(b.x_max_norm, 1),
        y_min_norm: _clamp01(b.y_min_norm, 0),
        y_max_norm: _clamp01(b.y_max_norm, 1),
        x_center_norm: _clamp01(b.x_center_norm, 0.5),
        y_center_norm: _clamp01(b.y_center_norm, 0.5),
      };
      if (norm.x_max_norm <= norm.x_min_norm) norm.x_max_norm = Math.min(1, norm.x_min_norm + 0.01);
      if (norm.y_max_norm <= norm.y_min_norm) norm.y_max_norm = Math.min(1, norm.y_min_norm + 0.01);
      norm.x_center_norm = Math.max(norm.x_min_norm, Math.min(norm.x_max_norm, norm.x_center_norm));
      norm.y_center_norm = Math.max(norm.y_min_norm, Math.min(norm.y_max_norm, norm.y_center_norm));
      return norm;
    }

    function _invalidateRowCropCache(row) {
      if (!row) return;
      delete row.__cropRecordsCache;
    }

    function _serializeCropRecords(crops) {
      return (Array.isArray(crops) ? crops : []).map((crop, idx) => ({
        crop_index: _intOr(crop?.crop_index, idx),
        crop_path: String(crop?.crop_path || ''),
        detection_index: _intOr(crop?.detection_index, -1),
        detection_confidence: _numberOr(crop?.detection_confidence, 0),
        species: String(crop?.species || 'Unknown'),
        species_confidence: _numberOr(crop?.species_confidence, 0),
        family: String(crop?.family || 'Unknown'),
        family_confidence: _numberOr(crop?.family_confidence, 0),
        quality: _numberOr(crop?.quality, -1),
        rating: Math.max(0, Math.min(5, _intOr(crop?.rating, 0))),
        exposure_correction: _numberOr(crop?.exposure_correction, 0),
        exposure_pipeline: getRowExposurePipelineMode(crop),
        exposure_subject_stops: _numberOr(crop?.exposure_subject_stops, 0),
        exposure_meter_scale: _numberOr(crop?.exposure_meter_scale, 1),
        bbox: _normalizeCropBbox(crop?.bbox),
      }));
    }

    function getRowCropRecords(row) {
      if (!row) return [];
      if (row.__cropRecordsCache) return row.__cropRecordsCache;

      let parsed = [];
      const raw = String(row.crops_json || '').trim();
      if (raw) {
        try {
          const maybeArray = JSON.parse(raw);
          if (Array.isArray(maybeArray)) parsed = maybeArray;
        } catch (_) {
          parsed = [];
        }
      }

      const normalized = [];
      for (let i = 0; i < parsed.length; i++) {
        const src = parsed[i];
        if (!src || typeof src !== 'object') continue;
        const fallbackPath = i === 0 ? String(row.crop_path || '') : '';
        const cropPath = String(src.crop_path || fallbackPath || '').trim();
        if (!cropPath && i > 0) continue;
        normalized.push({
          crop_index: _intOr(src.crop_index, i),
          crop_path: cropPath,
          detection_index: _intOr(src.detection_index, -1),
          detection_confidence: _numberOr(src.detection_confidence, 0),
          species: String(src.species || row.species || 'Unknown'),
          species_confidence: _numberOr(src.species_confidence, _numberOr(row.species_confidence, 0)),
          family: String(src.family || row.family || 'Unknown'),
          family_confidence: _numberOr(src.family_confidence, _numberOr(row.family_confidence, 0)),
          quality: _numberOr(src.quality, _numberOr(row.quality, -1)),
          rating: Math.max(0, Math.min(5, _intOr(src.rating, 0))),
          exposure_correction: _numberOr(src.exposure_correction, _numberOr(row.exposure_correction, 0)),
          exposure_pipeline: String(src.exposure_pipeline || row.exposure_pipeline || 'legacy_auto_bright_v1').trim().toLowerCase() === 'no_auto_bright_metered_v1'
            ? 'no_auto_bright_metered_v1'
            : 'legacy_auto_bright_v1',
          exposure_subject_stops: _numberOr(src.exposure_subject_stops, _numberOr(row.exposure_subject_stops, 0)),
          exposure_meter_scale: _numberOr(src.exposure_meter_scale, _numberOr(row.exposure_meter_scale, 1)),
          bbox: _normalizeCropBbox(src.bbox),
        });
      }

      if (!normalized.length && row.crop_path) {
        normalized.push({
          crop_index: 0,
          crop_path: String(row.crop_path || ''),
          detection_index: -1,
          detection_confidence: 0,
          species: String(row.species || 'Unknown'),
          species_confidence: _numberOr(row.species_confidence, 0),
          family: String(row.family || 'Unknown'),
          family_confidence: _numberOr(row.family_confidence, 0),
          quality: _numberOr(row.quality, -1),
          rating: Math.max(0, Math.min(5, _intOr(row.normalized_rating, 0))),
          exposure_correction: _numberOr(row.exposure_correction, 0),
          exposure_pipeline: getRowExposurePipelineMode(row),
          exposure_subject_stops: _numberOr(row.exposure_subject_stops, 0),
          exposure_meter_scale: _numberOr(row.exposure_meter_scale, 1),
          bbox: _fullFrameBbox(),
        });
      }

      row.__cropRecordsCache = normalized;
      return normalized;
    }

    function getRowPrimaryCropIndex(row, crops = null) {
      const list = Array.isArray(crops) ? crops : getRowCropRecords(row);
      if (!list.length) return 0;
      const explicit = _intOr(row?.primary_crop_index, -1);
      if (explicit >= 0 && explicit < list.length) return explicit;
      const path = String(row?.crop_path || '').trim();
      if (path) {
        const idx = list.findIndex(c => String(c.crop_path || '') === path);
        if (idx >= 0) return idx;
      }
      return 0;
    }

    function getRowActiveCropIndex(row, crops = null) {
      const list = Array.isArray(crops) ? crops : getRowCropRecords(row);
      if (!list.length) return 0;
      const key = _cropStateKey(row);
      const stored = _sceneActiveCropIndexByImage.get(key);
      if (Number.isFinite(stored) && stored >= 0 && stored < list.length) return stored;
      const primary = getRowPrimaryCropIndex(row, list);
      _sceneActiveCropIndexByImage.set(key, primary);
      return primary;
    }

    function setRowActiveCropIndex(row, nextIndex, crops = null) {
      const list = Array.isArray(crops) ? crops : getRowCropRecords(row);
      if (!list.length) return 0;
      const clamped = Math.max(0, Math.min(list.length - 1, _intOr(nextIndex, 0)));
      _sceneActiveCropIndexByImage.set(_cropStateKey(row), clamped);
      return clamped;
    }

    function getRowActiveCropState(row) {
      const crops = getRowCropRecords(row);
      const activeIndex = getRowActiveCropIndex(row, crops);
      return {
        crops,
        activeIndex,
        activeCrop: crops[activeIndex] || null,
        primaryIndex: getRowPrimaryCropIndex(row, crops),
      };
    }

    function promoteActiveCropToPrimary(row) {
      const crops = getRowCropRecords(row);
      if (!crops.length) return false;
      const nextPrimary = getRowActiveCropIndex(row, crops);
      const prevPrimary = getRowPrimaryCropIndex(row, crops);
      const crop = crops[nextPrimary];
      if (!crop) return false;

      const changed = prevPrimary !== nextPrimary || String(row.crop_path || '') !== String(crop.crop_path || '');
      row.primary_crop_index = String(nextPrimary);
      row.crop_path = crop.crop_path || row.crop_path || '';
      row.species = crop.species || row.species || 'Unknown';
      row.species_confidence = _numberOr(crop.species_confidence, _numberOr(row.species_confidence, 0));
      row.family = crop.family || row.family || 'Unknown';
      row.family_confidence = _numberOr(crop.family_confidence, _numberOr(row.family_confidence, 0));
      row.quality = _numberOr(crop.quality, _numberOr(row.quality, -1));
      row.exposure_correction = _numberOr(crop.exposure_correction, _numberOr(row.exposure_correction, 0));
      row.exposure_pipeline = getRowExposurePipelineMode(crop);
      row.exposure_subject_stops = _numberOr(crop.exposure_subject_stops, _numberOr(row.exposure_subject_stops, 0));
      row.exposure_meter_scale = _numberOr(crop.exposure_meter_scale, _numberOr(row.exposure_meter_scale, 1));

      const origin = String(getOrigin(row) || '').toLowerCase();
      if (origin !== 'manual') {
        const autoRating = Math.max(0, Math.min(5, _intOr(crop.rating, 0)));
        row.normalized_rating = String(autoRating);
        row.__normalized_rating = autoRating;
        row.rating_origin = 'auto';
      }

      row.crops_json = JSON.stringify(_serializeCropRecords(crops));
      _invalidateRowCropCache(row);
      setRowActiveCropIndex(row, nextPrimary, crops);

      if (changed) markDirty(row);
      return changed;
    }

    function ensureSceneNameColumn() {
      if (!header.includes('scene_name')) { header.push('scene_name'); }
      for (const r of rows) if (!('scene_name' in r)) r.scene_name = '';
    }

    function ensureCropColumns() {
      if (!header.includes('crops_json')) header.push('crops_json');
      if (!header.includes('primary_crop_index')) header.push('primary_crop_index');
      for (const r of rows) {
        if (!('crops_json' in r)) r.crops_json = '';
        if (!('primary_crop_index' in r) || r.primary_crop_index === '') r.primary_crop_index = '0';
      }
    }

    // Ensure rating columns exist and default values are set
    function ensureRatingColumns() {
      ensureCropColumns();
      if (!header.includes('rating')) header.push('rating');
      if (!header.includes('rating_origin')) header.push('rating_origin');
      if (!header.includes('normalized_rating')) header.push('normalized_rating');
      if (!header.includes('exposure_correction')) header.push('exposure_correction');
      if (!header.includes('exposure_pipeline')) header.push('exposure_pipeline');
      if (!header.includes('exposure_subject_stops')) header.push('exposure_subject_stops');
      if (!header.includes('exposure_meter_scale')) header.push('exposure_meter_scale');
      if (!header.includes('detection_scores')) header.push('detection_scores');
      if (!header.includes('culled')) header.push('culled');
      if (!header.includes('culled_origin')) header.push('culled_origin');
      for (const r of rows) {
        if (!('rating' in r)) r.rating = '';
        if (!('rating_origin' in r)) r.rating_origin = '';
        if (!('normalized_rating' in r)) r.normalized_rating = '';
        if (!('exposure_correction' in r)) r.exposure_correction = '0';
        if (!('exposure_pipeline' in r)) r.exposure_pipeline = 'legacy_auto_bright_v1';
        if (!('exposure_subject_stops' in r)) r.exposure_subject_stops = '0';
        if (!('exposure_meter_scale' in r)) r.exposure_meter_scale = '1';
        if (!('detection_scores' in r)) r.detection_scores = '';
        if (!('culled' in r)) r.culled = '';
        if (!('culled_origin' in r)) r.culled_origin = '';
        r.exposure_pipeline = getRowExposurePipelineMode(r);
        r.culled_origin = normalizeCullOrigin(r);
      }
    }

    function normalizeCullOrigin(row) {
      const status = row?.culled === 'accept' || row?.culled === 'reject' ? row.culled : '';
      const raw = String(row?.culled_origin || '').toLowerCase();
      if (raw === 'manual' || raw === 'auto' || raw === 'verified') return raw;
      if (status) return 'manual';
      return '';
    }

    /** Get (or lazily initialise) the scenedata object for a rootPath. */
    function _initScenedata(rp) {
      if (!_scenedata[rp]) _scenedata[rp] = { version: '2.0', image_ratings: {}, scenes: {} };
      return _scenedata[rp];
    }

    function _getSceneIdParts(sceneId) {
      const parts = String(sceneId).split(':');
      const sceneCount = parts.pop();
      const slot = parts.length ? parseInt(parts[0], 10) : null;
      return { slot, sceneCount };
    }

    function _getSceneScenedataEntry(sceneOrId, create = false, sceneRows = null) {
      const sceneId = typeof sceneOrId === 'string' ? sceneOrId : sceneOrId?.id;
      if (!sceneId) return null;
      const { sceneCount } = _getSceneIdParts(sceneId);
      const rowsForScene = sceneRows || getSceneRows(sceneId);
      const rp = rowsForScene[0]?.__rootPath || rootPath || '';
      if (!rp) return null;
      const sd = _initScenedata(rp);
      if (!create) return sd.scenes?.[sceneCount] || null;
      if (!sd.scenes[sceneCount]) {
        sd.scenes[sceneCount] = {
          scene_id: sceneCount,
          image_filenames: rowsForScene.map(r => r.filename || '').filter(Boolean),
          name: '',
          status: 'pending',
          user_tags: { species: [], families: [], finalized: false }
        };
      }
      return sd.scenes[sceneCount];
    }

    // Labels that the pipeline emits as placeholders for "no usable
    // classification" — these should never become user-visible scene tags.
    // 'Unknown' is what SpeciesNet returns for "no cv result" routed wildlife
    // detections; 'N/A' is used for non-bird placeholder rows; 'No Bird'
    // is the explicit absence label.
    const _PLACEHOLDER_SPECIES_LABELS = new Set(['No Bird', 'Unknown', 'N/A']);
    const _PLACEHOLDER_FAMILY_LABELS = new Set(['Unknown', 'N/A']);

    function _isMeaningfulSpeciesLabel(name) {
      return !!name && !_PLACEHOLDER_SPECIES_LABELS.has(name);
    }
    function _isMeaningfulFamilyLabel(name) {
      return !!name && !_PLACEHOLDER_FAMILY_LABELS.has(name);
    }

    function _computeSceneTagsFromRows(sceneRows, confThreshold, includeSecondary, includeFamilies = true) {
      const speciesSet = new Set();
      const familySet = new Set();
      for (const r of sceneRows) {
        const conf = parseNumber(r.species_confidence);
        if (conf >= confThreshold && _isMeaningfulSpeciesLabel(r.species)) speciesSet.add(r.species);
        if (includeFamilies) {
          const fconf = parseNumber(r.family_confidence);
          if (fconf >= confThreshold && _isMeaningfulFamilyLabel(r.family)) familySet.add(r.family);
        }
        if (includeSecondary) {
          const secondary = parseSecondarySpecies(r);
          for (const { name, score } of secondary) {
            if (score >= confThreshold && _isMeaningfulSpeciesLabel(name)) speciesSet.add(name);
          }
          if (includeFamilies) {
            const secFams = parseSecondaryFamilies(r);
            for (const { name, score } of secFams) {
              if (score >= confThreshold && _isMeaningfulFamilyLabel(name)) familySet.add(name);
            }
          }
        }
      }
      return {
        species: Array.from(speciesSet).sort(),
        families: Array.from(familySet).sort(),
      };
    }

    // The species-confidence threshold and secondary-species toggle currently
    // in effect. Scene tags are derived with these so anything we compute
    // matches what the grid and the scene dialog are showing right now.
    function _currentSceneTagSettings() {
      const thresholdEl = el('#speciesConf');
      const confThreshold = thresholdEl ? (parseFloat(thresholdEl.value) || 0) : 0;
      const includeSecondaryCheckbox = document.getElementById('includeSecondarySpecies');
      const includeSecondary = includeSecondaryCheckbox ? includeSecondaryCheckbox.checked : !!getSetting('includeSecondarySpecies', false);
      return { confThreshold, includeSecondary };
    }

    function _collectCurrentlyVisibleSceneTags(sceneId) {
      const { confThreshold, includeSecondary } = _currentSceneTagSettings();
      return _computeSceneTagsFromRows(getSceneRows(sceneId), confThreshold, includeSecondary, true);
    }

    // Reconcile a finalized scene's stored tags after some of its images have
    // been moved out (scene split).
    //
    // A finalized scene renders `user_tags` verbatim rather than the tags
    // computed from its rows — see `aggregateScenes` in scenes.js and
    // `collectSceneSpecies` in scene-dialog.js — so shrinking the scene does
    // not on its own drop labels that only the departing images justified.
    //
    // Only labels we can positively attribute to the departing images are
    // removed: a label survives if any remaining image still supports it, and
    // a label no image ever supported (one the user typed in by hand) is left
    // alone. Returns true when something changed.
    function _pruneFinalizedSceneTagsAfterRemoval(sceneEntry, remainingRows, removedRows) {
      const tags = sceneEntry && sceneEntry.user_tags;
      if (!tags || tags.finalized !== true) return false;
      const { confThreshold, includeSecondary } = _currentSceneTagSettings();
      const kept = _computeSceneTagsFromRows(remainingRows || [], confThreshold, includeSecondary, true);
      const gone = _computeSceneTagsFromRows(removedRows || [], confThreshold, includeSecondary, true);
      let changed = false;
      for (const key of ['species', 'families']) {
        const stillSupported = new Set(kept[key] || []);
        const departed = new Set(gone[key] || []);
        const before = Array.isArray(tags[key]) ? tags[key] : [];
        const after = before.filter(name => stillSupported.has(name) || !departed.has(name));
        if (after.length !== before.length) {
          tags[key] = after;
          changed = true;
        }
      }
      return changed;
    }

    function _normalizeScenedataForSave(rp, groupRows) {
      const sd = _initScenedata(rp);
      const existingScenes = sd.scenes || {};
      const grouped = new Map();
      for (const r of groupRows) {
        const sceneCount = String(r.scene_count);
        if (!grouped.has(sceneCount)) grouped.set(sceneCount, []);
        grouped.get(sceneCount).push(r);
      }

      const normalizedScenes = {};
      for (const [sceneCount, sceneRows] of grouped) {
        const existing = existingScenes[sceneCount] || {};
        const existingTags = existing.user_tags || {};
        const finalized = existingTags.finalized === true;
        normalizedScenes[sceneCount] = {
          scene_id: sceneCount,
          image_filenames: sceneRows.map(r => r.filename || '').filter(Boolean),
          name: String(existing.name || sceneRows.find(r => String(r.scene_name || '').trim().length)?.scene_name || '').trim(),
          status: finalized ? 'accepted' : (existing.status === 'rejected' ? 'rejected' : 'pending'),
          user_tags: {
            species: finalized ? Array.from(new Set((existingTags.species || []).map(String).filter(Boolean))).sort() : [],
            families: finalized ? Array.from(new Set((existingTags.families || []).map(String).filter(Boolean))).sort() : [],
            finalized,
          },
        };
      }

      sd.scenes = normalizedScenes;
      return sd;
    }

    // Helper: is this image manually rated (>0 stars)?
