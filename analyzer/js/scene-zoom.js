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

    function getRowRawPreviewRequestStops(row, disabled = false) {
      const requested = disabled ? 0.0 : (parseFloat(row?.exposure_correction) || 0);
      return Math.max(-2.0, Math.min(3.0, requested));
    }

    function getRowRawPreviewMeterScale(row, disabled = false) {
      if (disabled) return 1.0;
      if (getRowExposurePipelineMode(row) !== 'no_auto_bright_metered_v1') return 1.0;
      const meter = _numberOr(row?.exposure_meter_scale, 1);
      if (!Number.isFinite(meter) || meter <= 0) return 1.0;
      return Math.max(0.25, Math.min(8.0, meter));
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
     * Stops to use for thumbnail CSS (approximate full-frame preview vs bird crop).
     *
     * Pipeline stores exposure_correction = log2(meter_scale) + subject_stops (see compose_total_stops).
     * Export JPEGs are built from the meter-balanced full frame *before* subject-only linear
     * correction is applied to crops — so applying 2^total EV via brightness() re-applies meter
     * on already-metered sRGB and blows highlights.
     *
     * For metered paths we therefore use **subject stops only** (optionally derived from total − meter).
     * CSS brightness() still operates on gamma-encoded values; we dampen and cap in stopsToThumbnailBrightnessMultiplier.
     *
     * Future: analysis will standardize on the numpy linear exposure path only (`numpy_linear_v2`);
     * other `exposure_pipeline` values may remain only in older `.kestrel` data until a migration/cleanup pass.
     */
    function getThumbnailExposureStopsForCss(row) {
      if (!getSetting('exposure_corrected_thumbs', true)) return 0;
      if (getSetting('raw_exposure_correction_disabled', false)) return 0;

      const mode = String(row?.exposure_pipeline || '').trim().toLowerCase();
      // Canonical metered path is numpy_linear_v2; keep legacy strings for old databases.
      const meteredModes = new Set(['numpy_linear_v2', 'no_auto_bright_metered_v1']);

      if (meteredModes.has(mode)) {
        let subj = _numberOr(row?.exposure_subject_stops, NaN);
        if (!Number.isFinite(subj)) {
          const tot = parseFloat(row?.exposure_correction) || 0;
          const m = _numberOr(row?.exposure_meter_scale, 1);
          const meterSt = Math.log2(Math.max(1e-6, m));
          subj = tot - meterSt;
        }
        return Math.max(-4.0, Math.min(4.0, subj));
      }

      // Legacy / non-metered: single stored EV is the best available hint (no separate meter term).
      const eff = getRowRawPreviewEffectiveStops(row, false);
      return Math.max(-4.0, Math.min(4.0, eff));
    }

    /**
     * Map stops → CSS brightness() multiplier. Browser brightness scales sRGB channels roughly
     * linearly in display space; it is **not** linear-light exposure + highlight roll-off like
     * crop processing. Use gentler gain on brightening and a modest ceiling to limit clipped highlights.
     */
    function stopsToThumbnailBrightnessMultiplier(stops) {
      if (!Number.isFinite(stops) || Math.abs(stops) < 0.0005) return 1;
      if (stops > 0) {
        const dampened = stops * 0.62;
        const mult = Math.pow(2, dampened);
        return Math.max(0.35, Math.min(2.05, mult));
      }
      const mult = Math.pow(2, stops);
      return Math.max(0.35, Math.min(2.85, mult));
    }

    function getThumbnailExposureFilterStyle(row) {
      const stops = getThumbnailExposureStopsForCss(row);
      if (!Number.isFinite(stops) || Math.abs(stops) < 0.0005) return '';
      const mult = stopsToThumbnailBrightnessMultiplier(stops);
      if (Math.abs(mult - 1) < 0.002) return '';
      return `brightness(${mult})`;
    }

    function applyThumbnailExposureToImg(imgEl, row) {
      if (!imgEl || !row) return;
      const f = getThumbnailExposureFilterStyle(row);
      imgEl.style.filter = f || '';
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
        const curImg = box?.querySelector('img');
        if (curImg) {
          curImg.style.visibility = '';
          curImg.style.transform = '';
          curImg.style.transformOrigin = '';
          delete curImg.dataset.isRaw;
        }
        box.dataset.rawLabel = 'RAW';
      };

      window.addEventListener('mousemove', onMove);
      window.addEventListener('mouseup', onUp);
      window.addEventListener('wheel', onWheel, { passive: false });
    }
    // ---- End scene dialog RAW zoom ----

    // ── Filmstrip scene view state ──
