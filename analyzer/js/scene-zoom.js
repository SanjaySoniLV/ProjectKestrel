    // ---- Scene dialog RAW zoom (click-drag on thumbnail → zoom in previewBox) ----
    let sceneZoomActive = false;
    let sceneZoomRow = null;
    let sceneZoomThumbEl = null;
    let sceneZoomScale = 5;   // adjustable via scroll or slider
    let zoomLastX = 0, zoomLastY = 0; // last mouse pos for slider re-apply
    const sceneRawCache = new Map();   // unique row key -> blob URL
    const sceneRawLoading = new Set(); // (rootPath|filename) currently being fetched

    function getRowExposurePipelineMode(row) {
      const mode = String(row?.exposure_pipeline || '').trim().toLowerCase();
      if (mode === 'no_auto_bright_metered_v1') return mode;
      return 'legacy_auto_bright_v1';
    }

    /**
     * User-tunable strength (0..1) for how much of the solver's auto exposure
     * compensation is applied to *preview* imagery. Drives both the export
     * thumbnail's CSS brightness and the RAW zoom preview's EV. 1.0 = full auto
     * (default / legacy look), 0.0 = compensation off. Persisted as
     * `exposure_preview_strength`; the pipeline-baked crop thumbnails ignore it.
     */
    function getExposurePreviewStrength() {
      // `exposure_preview_strength` (0..1) is the SINGLE authority for how much
      // exposure correction appears in previews (export thumbnails + RAW zoom),
      // exposed by one slider that lives in both the scene viewer and Settings.
      // The two retired checkboxes fold in as strength 0 (both meant "no preview
      // correction"); the one-time init migration normalizes them away.
      if (getSetting('raw_exposure_correction_disabled', false)) return 0;
      if (getSetting('exposure_corrected_thumbs', true) === false) return 0;
      const v = _numberOr(getSetting('exposure_preview_strength', 0.7), 0.7);
      if (!Number.isFinite(v)) return 0.7;
      return Math.max(0, Math.min(1, v));
    }

    function getRowRawPreviewRequestStops(row, disabled = false) {
      if (disabled) return 0.0;
      // Strength scales the stored total correction; 0 → no EV shift requested.
      const requested = (parseFloat(row?.exposure_correction) || 0) * getExposurePreviewStrength();
      return Math.max(-2.0, Math.min(3.0, requested));
    }

    function getRowRawPreviewMeterScale(row, disabled = false) {
      if (disabled) return 1.0;
      if (getRowExposurePipelineMode(row) !== 'no_auto_bright_metered_v1') return 1.0;
      const meter = _numberOr(row?.exposure_meter_scale, 1);
      if (!Number.isFinite(meter) || meter <= 0) return 1.0;
      // Apply strength in stops-space: meter^strength == strength * log2(meter).
      // Keeps the backend's no-detection meter fallback consistent with the knob;
      // strength 0 → 1.0 (no baseline metering applied to the RAW preview).
      const scaled = Math.pow(meter, getExposurePreviewStrength());
      return Math.max(0.25, Math.min(8.0, scaled));
    }

    function getRowRawPreviewEffectiveStops(row, disabled = false) {
      const requested = getRowRawPreviewRequestStops(row, disabled);
      if (disabled) return requested;
      if (getRowExposurePipelineMode(row) !== 'no_auto_bright_metered_v1') return requested;
      if (Math.abs(requested) > 0.0001) return requested;

      const meterScale = getRowRawPreviewMeterScale(row, false);
      const meterStops = Math.log2(meterScale);
      if (Math.abs(meterStops) <= 0.001) return requested;
      return Math.max(-2.0, Math.min(3.0, meterStops));
    }

    /**
     * Stops to apply to the export thumbnail via CSS brightness() — an
     * approximation of the bird crop's exposure on the full-frame preview.
     *
     * The export JPEG is rendered from the *raw linear* decode with NO
     * correction baked in (pipeline.py export_image: noauto_linear → sRGB, no
     * meter scale, no subject stops). The bird crop, by contrast, bakes the full
     * total_scale = 2^exposure_correction (= log2(meter_scale) + subject_stops).
     * So to make the subject roughly match the crop, the browser applies the
     * full strength-scaled exposure_correction here — NOT subject stops only.
     * (An earlier version applied subject stops only on the false premise that
     * the export was meter-balanced; that under-corrected by the meter term and
     * made the strength knob nearly invisible.)
     *
     * The strength scaling, meter fallback, and clamp all live in
     * getRowRawPreviewEffectiveStops, shared with the RAW zoom preview so the
     * flat preview and the RAW zoom agree.
     */
    function getThumbnailExposureStopsForCss(row) {
      // getExposurePreviewStrength() already folds in the retired legacy toggles.
      if (getExposurePreviewStrength() <= 0.0005) return 0;

      // The export thumbnail is rendered from the raw linear decode with NO
      // exposure correction baked in (see pipeline.py export_image stage:
      // noauto_linear → sRGB, no meter scale, no subject stops). So the browser
      // applies the FULL strength-scaled *total* correction
      // (log2(meter_scale) + subject_stops == exposure_correction) — the same
      // value the bird crop is baked with — so the subject roughly matches the
      // crop. Strength scaling + clamp live in getRowRawPreviewEffectiveStops,
      // shared with the RAW zoom preview so the flat preview and the RAW zoom
      // agree.
      const eff = getRowRawPreviewEffectiveStops(row, false);
      return Number.isFinite(eff) ? eff : 0;
    }

    /**
     * Map stops → CSS brightness() multiplier. Browser brightness scales sRGB channels roughly
     * linearly in display space; it is **not** linear-light exposure + highlight roll-off like
     * crop processing. Use gentler gain on brightening and a modest ceiling to limit clipped highlights.
     */
    function stopsToThumbnailBrightnessMultiplier(stops) {
      if (!Number.isFinite(stops) || Math.abs(stops) < 0.0005) return 1;
      // The bird crop scales pixels by 2^stops in LINEAR light, then sRGB-encodes
      // (apply_exposure_crop_numpy). CSS brightness() instead multiplies the
      // gamma-encoded sRGB values, so a linear factor k shows on screen as
      // ~k^(1/2.2). Apply that exponent so the full-image preview tracks the
      // crop's brightness rather than over-shooting it.
      const mult = Math.pow(2, stops / 2.2);
      // Keep within a sane display range (~±2.4 perceived stops).
      return Math.max(0.32, Math.min(2.6, mult));
    }

    function getThumbnailExposureFilterStyle(row) {
      const stops = getThumbnailExposureStopsForCss(row);
      if (!Number.isFinite(stops) || Math.abs(stops) < 0.0005) return '';
      const mult = stopsToThumbnailBrightnessMultiplier(stops);
      if (Math.abs(mult - 1) < 0.002) return '';
      return `brightness(${mult})`;
    }

    // Registry of live export-thumbnail <img>s so the strength slider can
    // re-apply CSS brightness across the grid + viewer without a full re-render.
    const _expRowByImg = new WeakMap();

    function applyThumbnailExposureToImg(imgEl, row) {
      if (!imgEl || !row) return;
      _expRowByImg.set(imgEl, row);
      imgEl.dataset.expManaged = '1';
      const f = getThumbnailExposureFilterStyle(row);
      imgEl.style.filter = f || '';
    }

    // ── Highlight clip / blow-out mask + "% clipped" readout ──
    let clipMaskEnabled = false;      // session view-aid toggle (default off)
    const CLIP_MASK_U8 = 250;         // channel value counted as "blown"
    const CLIP_MASK_RGB = [255, 96, 0]; // orange highlight-clipping overlay

    /**
     * Sample a drawable (img or canvas) at downscaled resolution, optionally
     * with a brightness() pre-filter, and report the fraction of pixels whose
     * brightest channel is clipped. When wantMask, also returns a magenta mask
     * canvas (clipped pixels opaque, the rest transparent) sized to the sample.
     * Returns {pct, canvas} — {0,null} on any failure (e.g. tainted canvas).
     */
    function _computeClipStats(source, srcW, srcH, brightnessMult, wantMask) {
      if (!source || !srcW || !srcH) return { pct: 0, canvas: null };
      const maxEdge = 900;
      const scale = Math.min(1, maxEdge / Math.max(srcW, srcH));
      const w = Math.max(1, Math.round(srcW * scale));
      const h = Math.max(1, Math.round(srcH * scale));
      let data;
      const work = document.createElement('canvas');
      work.width = w; work.height = h;
      const wctx = work.getContext('2d', { willReadFrequently: true });
      if (!wctx) return { pct: 0, canvas: null };
      try {
        if (brightnessMult && Math.abs(brightnessMult - 1) > 0.002) {
          wctx.filter = `brightness(${brightnessMult})`;
        }
        wctx.drawImage(source, 0, 0, w, h);
        wctx.filter = 'none';
        data = wctx.getImageData(0, 0, w, h).data;
      } catch (e) {
        return { pct: 0, canvas: null };
      }
      const total = w * h;
      let clipped = 0;
      const maskImg = wantMask ? wctx.createImageData(w, h) : null;
      for (let i = 0; i < total; i++) {
        const o = i * 4;
        const r = data[o], g = data[o + 1], b = data[o + 2];
        const mx = r > g ? (r > b ? r : b) : (g > b ? g : b);
        if (mx >= CLIP_MASK_U8) {
          clipped++;
          if (maskImg) {
            maskImg.data[o] = CLIP_MASK_RGB[0];
            maskImg.data[o + 1] = CLIP_MASK_RGB[1];
            maskImg.data[o + 2] = CLIP_MASK_RGB[2];
            maskImg.data[o + 3] = 255;
          }
        }
      }
      let canvas = null;
      if (wantMask) {
        canvas = document.createElement('canvas');
        canvas.width = w; canvas.height = h;
        canvas.getContext('2d').putImageData(maskImg, 0, 0);
      }
      return { pct: total ? (clipped / total) * 100 : 0, canvas };
    }

    // Letterbox rect of a contain-fitted image inside its host box.
    function _containRect(box, natW, natH) {
      const bw = box.clientWidth, bh = box.clientHeight;
      if (!natW || !natH || !bw || !bh) return null;
      const s = Math.min(bw / natW, bh / natH);
      const w = natW * s, h = natH * s;
      return { left: (bw - w) / 2, top: (bh - h) / 2, width: w, height: h };
    }

    function _setOverexposedReadout(pct) {
      const stat = el('#sceneOverexposedToggle');
      if (!stat) return;
      if (!Number.isFinite(pct)) { stat.textContent = '— clipped'; stat.classList.remove('hot'); return; }
      const txt = pct >= 0.1 ? pct.toFixed(1) : (pct > 0 ? '<0.1' : '0.0');
      stat.textContent = `${txt}% clipped`;
      // Turns orange once ≥1% of the frame is clipping.
      stat.classList.toggle('hot', pct >= 1.0);
    }

    // Update the export panel's clip overlay (if enabled) and the "% clipped"
    // readout. Clipping is measured on the UNCORRECTED export image (brightness
    // multiplier = 1): blown highlights are a property of the captured source,
    // not of the preview's exposure-comp boost. Stable regardless of zoom.
    function updateExportClipPreview(row) {
      const box = el('#previewBox');
      if (!box) return;
      const old = box.querySelector('canvas.scene-clip-overlay');
      if (old) old.remove();
      // Skip while RAW zoom owns the box; the zoom path paints its own overlay.
      if (box.classList.contains('zoom-active')) return;
      const img = box.querySelector('img');
      if (!img || !row) { _setOverexposedReadout(NaN); return; }
      const draw = () => {
        // Bail if a newer selection replaced this image while we awaited load.
        if (img.parentNode !== box) return;
        const natW = img.naturalWidth, natH = img.naturalHeight;
        if (!natW || !natH) return;
        const { pct, canvas } = _computeClipStats(img, natW, natH, 1, clipMaskEnabled);
        _setOverexposedReadout(pct);
        const prev = box.querySelector('canvas.scene-clip-overlay');
        if (prev) prev.remove();
        if (clipMaskEnabled && canvas) {
          const rect = _containRect(box, natW, natH);
          if (rect) {
            canvas.className = 'scene-clip-overlay';
            canvas.style.left = `${rect.left}px`;
            canvas.style.top = `${rect.top}px`;
            canvas.style.width = `${rect.width}px`;
            canvas.style.height = `${rect.height}px`;
            box.appendChild(canvas);
          }
        }
      };
      if (img.complete && img.naturalWidth) draw();
      else img.addEventListener('load', draw, { once: true });
    }

    // Paint / clear the clip overlay on the live RAW zoom canvas.
    function updateZoomClipOverlay() {
      const box = el('#previewBox');
      if (!box) return;
      const old = box.querySelector('canvas.scene-zoom-clip-overlay');
      const zoomCanvas = box.querySelector('canvas.scene-zoom-canvas');
      if (!clipMaskEnabled || !zoomCanvas) { if (old) old.remove(); return; }
      const w = zoomCanvas.width, h = zoomCanvas.height;
      // Readout stays the stable full-frame value (set before zoom started);
      // the zoom overlay is a visual aid only.
      const { canvas } = _computeClipStats(zoomCanvas, w, h, 1, true);
      if (old) old.remove();
      if (canvas) {
        canvas.className = 'scene-zoom-clip-overlay';
        box.appendChild(canvas);
      }
    }

    // Re-apply strength-scaled CSS brightness to every managed thumbnail
    // (grid + viewer) and refresh the export clip preview. RAW zoom previews
    // re-fetch with the new EV on the next click-hold (cache-keyed on strength).
    function refreshManagedExposurePreviews() {
      document.querySelectorAll('img[data-exp-managed="1"]').forEach((img) => {
        const row = _expRowByImg.get(img);
        if (!row) return;
        const f = getThumbnailExposureFilterStyle(row);
        img.style.filter = f || '';
      });
      const row = (_currentScene && Array.isArray(_currentScene.images))
        ? _currentScene.images[currentImageIndex]
        : null;
      if (row) updateExportClipPreview(row);
    }

    function getSceneRawCacheKey(row) {
      const disabled = getSetting('raw_exposure_correction_disabled', false);
      const expCorr = getRowRawPreviewEffectiveStops(row, disabled);
      const expKey = Number.isFinite(expCorr) ? expCorr.toFixed(4) : '0.0000';
      const mode = getRowExposurePipelineMode(row);
      return [
        row.__rootPath || '',
        row.filename || '',
        row.export_path || '',
        row.crop_path || '',
        `mode=${mode}`,
        `exp=${expKey}`
      ].join('|');
    }

    function applySceneZoomTransform(imgEl, thumbEl, clientX, clientY, scale) {
      if (!imgEl || !thumbEl) return;
      const box = imgEl.closest('#previewBox');
      if (!box) return;
      const iw = imgEl.naturalWidth || imgEl.width;
      const ih = imgEl.naturalHeight || imgEl.height;
      if (!iw || !ih) return;

      const rect = thumbEl.getBoundingClientRect();
      const xNorm = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      const yNorm = Math.max(0, Math.min(1, (clientY - rect.top) / rect.height));

      const z = Math.max(1, Number(scale) || 1);
      let cropW = Math.max(1, iw / z);
      let cropH = Math.max(1, ih / z);

      const dpr = window.devicePixelRatio || 1;
      const targetW = Math.max(1, Math.round(box.clientWidth * dpr));
      const targetH = Math.max(1, Math.round(box.clientHeight * dpr));
      const boxAspect = targetW / targetH;
      if (cropW / cropH > boxAspect) cropW = cropH * boxAspect;
      else cropH = cropW / boxAspect;

      let sx = xNorm * iw - cropW * 0.5;
      let sy = yNorm * ih - cropH * 0.5;
      sx = Math.max(0, Math.min(iw - cropW, sx));
      sy = Math.max(0, Math.min(ih - cropH, sy));

      let canvas = box.querySelector('canvas.scene-zoom-canvas');
      if (!canvas) {
        canvas = document.createElement('canvas');
        canvas.className = 'scene-zoom-canvas';
        box.appendChild(canvas);
      }

      if (canvas.width !== targetW || canvas.height !== targetH) {
        canvas.width = targetW;
        canvas.height = targetH;
      }

      const ctx = canvas.getContext('2d', { alpha: false });
      if (!ctx) return;
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = 'high';
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(imgEl, sx, sy, cropW, cropH, 0, 0, canvas.width, canvas.height);
      imgEl.style.visibility = 'hidden';
      updateZoomClipOverlay();
    }

    function formatExposureEv(v) {
      const n = parseFloat(v) || 0;
      const abs = Math.abs(n);
      if (abs < 0.005) return '+0.00';
      const sign = n >= 0 ? '+' : '-';
      return sign + abs.toFixed(2);
    }

    async function loadSceneRawAsync(row) {
      const disabled = getSetting('raw_exposure_correction_disabled', false);
      const expCorrRequested = getRowRawPreviewRequestStops(row, disabled);
      const expCorrEffective = getRowRawPreviewEffectiveStops(row, disabled);
      const expMode = getRowExposurePipelineMode(row);
      const meterScale = getRowRawPreviewMeterScale(row, disabled);
      const key = getSceneRawCacheKey(row);
      sceneRawLoading.add(key);
      try {
        const res = await window.pywebview.api.read_raw_full(
          row.filename, row.__rootPath || '', expCorrRequested, expMode, meterScale
        );
        if (res && res.debug) {
          console.info('[raw-debug][scene]', row.filename, res.debug);
        }
        if (res && res.success && res.data) {
          const url = _base64ToBlobUrl(res.data, res.mime || 'image/jpeg');
          sceneRawCache.set(key, url);
          // Upgrade preview if this row is still the active zoom row
          if (sceneZoomActive && sceneZoomRow === row) {
            const box = el('#previewBox');
            const curImg = box?.querySelector('img');
            if (curImg) {
              curImg.src = url;
              curImg.dataset.isRaw = '1';
              curImg.onload = () => {
                if (sceneZoomActive && sceneZoomRow === row && sceneZoomThumbEl) {
                  applySceneZoomTransform(curImg, sceneZoomThumbEl, zoomLastX, zoomLastY, sceneZoomScale);
                }
              };
              if (box) box.dataset.rawLabel = `RAW (${formatExposureEv(expCorrEffective)} EV)`;
              box.classList.add('raw-loaded');
              if (sceneZoomThumbEl) {
                applySceneZoomTransform(curImg, sceneZoomThumbEl, zoomLastX, zoomLastY, sceneZoomScale);
              }
            }
          }
        }
      } catch (e) {
        console.warn('loadSceneRawAsync error:', e);
      } finally {
        sceneRawLoading.delete(key);
      }
    }

    function startSceneZoomPreview(row, thumbEl, mouseEv) {
      sceneZoomActive = true;
      sceneZoomRow = row;
      sceneZoomThumbEl = thumbEl;
      const key = getSceneRawCacheKey(row);
      const previewBox = el('#previewBox');
      const disabled = getSetting('raw_exposure_correction_disabled', false);
      const expCorr = getRowRawPreviewEffectiveStops(row, disabled);
      previewBox.classList.add('zoom-active');
      previewBox.dataset.rawLabel = `RAW Zoom (${formatExposureEv(expCorr)} EV) (Scroll to zoom in/out)`;
      zoomLastX = mouseEv.clientX;
      zoomLastY = mouseEv.clientY;

      // Step 1: Immediately show the already-loaded thumbnail as a placeholder
      const thumbImgEl = thumbEl.querySelector('img');
      const thumbImgSrc = thumbImgEl?.src;
      if (thumbImgSrc) {
        _clearScenePreviewBox(previewBox);
        const stub = document.createElement('img');
        stub.src = thumbImgSrc;
        stub.style.filter = thumbImgEl?.style?.filter || '';
        stub.style.imageRendering = 'crisp-edges';
        stub.onload = () => {
          if (sceneZoomActive && sceneZoomRow === row && sceneZoomThumbEl === thumbEl) {
            applySceneZoomTransform(stub, thumbEl, zoomLastX, zoomLastY, sceneZoomScale);
          }
        };
        previewBox.appendChild(stub);
        applySceneZoomTransform(stub, thumbEl, mouseEv.clientX, mouseEv.clientY, sceneZoomScale);
      }

      // Step 2: Async — upgrade to full export or cached RAW
      (async () => {
        if (!sceneZoomActive || sceneZoomRow !== row) return;
        const cachedRaw = sceneRawCache.get(key);
        if (cachedRaw) {
          _clearScenePreviewBox(previewBox);
          const imgEl = document.createElement('img');
          imgEl.src = cachedRaw;
          imgEl.dataset.isRaw = '1';
          imgEl.style.imageRendering = 'crisp-edges';
          imgEl.onload = () => {
            if (sceneZoomActive && sceneZoomRow === row && sceneZoomThumbEl === thumbEl) {
              applySceneZoomTransform(imgEl, thumbEl, zoomLastX, zoomLastY, sceneZoomScale);
            }
          };
          previewBox.appendChild(imgEl);
          previewBox.classList.add('raw-loaded');
          applySceneZoomTransform(imgEl, thumbEl, zoomLastX, zoomLastY, sceneZoomScale);
        } else {
          const url = await getBlobUrlForPath(row.export_path || row.crop_path, row.__rootPath);
          if (!sceneZoomActive || sceneZoomRow !== row) return;
          if (url && url !== thumbImgSrc) {
            _clearScenePreviewBox(previewBox);
            const imgEl = document.createElement('img');
            imgEl.src = url;
            imgEl.style.imageRendering = 'crisp-edges';
            imgEl.onload = () => {
              if (sceneZoomActive && sceneZoomRow === row && sceneZoomThumbEl === thumbEl) {
                applySceneZoomTransform(imgEl, thumbEl, zoomLastX, zoomLastY, sceneZoomScale);
              }
            };
            previewBox.appendChild(imgEl);
            applySceneZoomTransform(imgEl, thumbEl, zoomLastX, zoomLastY, sceneZoomScale);
          }
        }
      })();

      // Step 3: Kick off RAW load in background
      if (!sceneRawCache.has(key) && !sceneRawLoading.has(key) && hasPywebviewApi) {
        loadSceneRawAsync(row);
      }

      // Show zoom slider
      const zoomWrap = el('#sceneZoomWrap');
      const slider = el('#sceneZoomSlider');
      if (slider) {
        slider.value = sceneZoomScale;
        slider.oninput = () => {
          sceneZoomScale = parseFloat(slider.value);
          const curImg = el('#previewBox')?.querySelector('img');
          if (curImg) applySceneZoomTransform(curImg, thumbEl, zoomLastX, zoomLastY, sceneZoomScale);
        };
      }

      const onMove = (ev) => {
        if (!sceneZoomActive) return;
        zoomLastX = ev.clientX; zoomLastY = ev.clientY;
        const curImg = el('#previewBox')?.querySelector('img');
        if (curImg) applySceneZoomTransform(curImg, thumbEl, ev.clientX, ev.clientY, sceneZoomScale);
      };

      const onWheel = (ev) => {
        if (!sceneZoomActive) return;
        ev.preventDefault();
        const delta = ev.deltaY < 0 ? 0.5 : -0.5;
        sceneZoomScale = Math.max(2, Math.min(12, sceneZoomScale + delta));
        if (slider) slider.value = sceneZoomScale;
        const curImg = el('#previewBox')?.querySelector('img');
        if (curImg) applySceneZoomTransform(curImg, thumbEl, ev.clientX, ev.clientY, sceneZoomScale);
        zoomLastX = ev.clientX; zoomLastY = ev.clientY;
      };

      const onUp = () => {
        sceneZoomActive = false;
        sceneZoomRow = null;
        sceneZoomThumbEl = null;
        window.removeEventListener('mousemove', onMove);
        window.removeEventListener('mouseup', onUp);
        window.removeEventListener('wheel', onWheel);
        const box = el('#previewBox');
        box.classList.remove('zoom-active', 'raw-loaded');
        const canvas = box?.querySelector('canvas.scene-zoom-canvas');
        if (canvas) canvas.remove();
        const zoomClip = box?.querySelector('canvas.scene-zoom-clip-overlay');
        if (zoomClip) zoomClip.remove();
        const curImg = box?.querySelector('img');
        if (curImg) {
          curImg.style.visibility = '';
          curImg.style.transform = '';
          curImg.style.transformOrigin = '';
          delete curImg.dataset.isRaw;
        }
        box.dataset.rawLabel = 'RAW';
        // Static full-image is back: recompute its clip overlay + readout.
        updateExportClipPreview(row);
      };

      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
      window.addEventListener('wheel', onWheel, { passive: false });
    }
    // ---- End scene dialog RAW zoom ----

    // ── Exposure-preview controls (one strength slider, mirrored in the scene
    //    viewer and the Settings panel; plus the highlight clip-mask toggle) ──
    let _expStrengthSaveTimer = null;
    function _persistExposureStrength(v) {
      // The slider is the single authority: writing it also normalizes the two
      // retired checkboxes (raw_exposure_correction_disabled, exposure_corrected_thumbs)
      // to their non-gating state so they can never override the slider again.
      // localStorage write is immediate so getSetting() reflects it live; backend
      // persist is debounced to avoid spamming during a slider drag.
      try {
        const s = loadSettings();
        s.exposure_preview_strength = v;
        s.raw_exposure_correction_disabled = false;
        s.exposure_corrected_thumbs = true;
        s.exposure_preview_strength_migrated = true;
        saveSettings(s);
      } catch (_) {}
      if (hasPywebviewApi && window.pywebview?.api?.save_settings_data) {
        clearTimeout(_expStrengthSaveTimer);
        _expStrengthSaveTimer = setTimeout(() => {
          try {
            window.pywebview.api.save_settings_data({
              exposure_preview_strength: v,
              raw_exposure_correction_disabled: false,
              exposure_corrected_thumbs: true,
              exposure_preview_strength_migrated: true,
            });
          } catch (_) {}
        }, 250);
      }
    }

    function _currentSceneRowForExposure() {
      return (_currentScene && Array.isArray(_currentScene.images))
        ? _currentScene.images[currentImageIndex]
        : null;
    }

    // Update the scene-viewer "Exp Comp (+#.##EV)" label with the exposure
    // compensation actually applied to the current image's preview = the
    // strength-scaled exposure_correction for the selected row.
    function updateExpCompEvLabel() {
      const lbl = el('#sceneExpStrengthEv');
      if (!lbl) return;
      const row = _currentSceneRowForExposure();
      let stops = 0;
      if (row) {
        const eff = getRowRawPreviewEffectiveStops(row, false);
        if (Number.isFinite(eff)) stops = eff;
      }
      lbl.textContent = formatExposureEv(stops);
    }

    // Keep both strength sliders (scene viewer + Settings) and their value labels
    // in sync without firing each other's input handlers.
    function _syncExposureStrengthSliders(pct) {
      [['#sceneExpStrengthSlider', '#sceneExpStrengthVal'],
       ['#settingsExpStrengthSlider', '#settingsExpStrengthVal']].forEach(([sliderSel, valSel]) => {
        const slider = el(sliderSel);
        if (slider && Math.round(parseFloat(slider.value)) !== pct) slider.value = String(pct);
        const valEl = el(valSel);
        if (valEl) valEl.textContent = `${pct}%`;
      });
      updateExpCompEvLabel();
    }

    // Single entry point for committing a new strength from EITHER slider.
    // Exposed (same script scope) so settings.js can drive it too.
    function applyExposurePreviewStrengthPct(pct) {
      pct = Math.max(0, Math.min(100, Math.round(Number(pct) || 0)));
      _persistExposureStrength(Number((pct / 100).toFixed(3)));
      _syncExposureStrengthSliders(pct);
      refreshManagedExposurePreviews();
    }

    // One-time migration: everyone lands on the 70% default the first time they
    // run the unified slider — intentionally NOT derived from the retired
    // disable/corrected-thumbs flags or any value an earlier dev build wrote.
    // The marker is set on every slider write, so a real user choice afterward
    // is never clobbered by this migration.
    const EXPOSURE_PREVIEW_DEFAULT = 0.7;
    function _migrateLegacyExposureToggles() {
      if (!getSetting('exposure_preview_strength_migrated', false)) {
        _persistExposureStrength(EXPOSURE_PREVIEW_DEFAULT);
      }
    }

    function initExposurePreviewControls() {
      _migrateLegacyExposureToggles();

      const strengthSlider = el('#sceneExpStrengthSlider');
      const overToggle = el('#sceneOverexposedToggle');

      const cur = Math.round(getExposurePreviewStrength() * 100);
      _syncExposureStrengthSliders(cur);

      if (strengthSlider) {
        strengthSlider.addEventListener('input', () => {
          applyExposurePreviewStrengthPct(parseFloat(strengthSlider.value));
        });
      }

      // The "% clipped" readout doubles as the highlight-clip-mask toggle.
      if (overToggle) {
        overToggle.classList.toggle('mask-on', clipMaskEnabled);
        overToggle.setAttribute('aria-pressed', clipMaskEnabled ? 'true' : 'false');
        overToggle.addEventListener('click', () => {
          clipMaskEnabled = !clipMaskEnabled;
          overToggle.classList.toggle('mask-on', clipMaskEnabled);
          overToggle.setAttribute('aria-pressed', clipMaskEnabled ? 'true' : 'false');
          const box = el('#previewBox');
          if (box && box.classList.contains('zoom-active')) updateZoomClipOverlay();
          else updateExportClipPreview(_currentSceneRowForExposure());
        });
      }
    }

    // Called by settings.js when the Settings panel opens, so its slider mirrors
    // the current strength (e.g. after the scene-viewer slider changed it).
    function syncSettingsExposureStrengthSlider() {
      _syncExposureStrengthSliders(Math.round(getExposurePreviewStrength() * 100));
    }

    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initExposurePreviewControls, { once: true });
    } else {
      initExposurePreviewControls();
    }

    // ── Filmstrip scene view state ──
