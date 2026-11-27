// Cull mode's two big previews: the full image (click and hold to zoom into
// the RAW) and the bird crop, with the RAW Zoom and Exposure Adjustment
// sliders and the "% clipped" toggle. These reuse the scene viewer's exposure
// and clipping helpers (js/scene-zoom.js) and its RAW cache, so a RAW decoded
// in one place is not decoded again in the other. The zoom drawing is the
// scene viewer's, pointed at Cull mode's own preview box.
(function () {
  const TEXT = {
    noBird: 'No bird found',
    noBirdBody: "Kestrel didn't detect a bird here, so it has no Quality or stars. Your starting picks {rule} these; check it and decide.",
    noPreview: 'No preview',
    birdOf: 'Bird crop · bird {i} of {n}',
    crop: 'Bird crop',
    exposure: 'Exposure Adjustment ({ev} EV)',
    clipped: '{pct}% clipped',
    rawLabel: 'RAW ({ev} EV)',
    rawEmbedded: 'RAW (embedded preview)',
  };
  const fill = (s, p) => String(s).replace(/\{(\w+)\}/g, (_, k) => (p && k in p ? String(p[k]) : ''));
  let token = 0;
  let zoom = null; // { row, img, x, y, isRaw }
  let windowWired = false;

  const box = (S, which) => S.el?.querySelector(`.cm-pv--${which}`);

  function clearBox(b) {
    b.querySelectorAll('img, canvas, .cm-nobird').forEach(n => n.remove());
  }

  function setClipReadout(S, pct) {
    const btn = S.el?.querySelector('.cm-clip');
    if (!btn) return;
    btn.classList.toggle('is-on', clipMaskEnabled);
    btn.setAttribute('aria-pressed', clipMaskEnabled ? 'true' : 'false');
    if (!Number.isFinite(pct)) { btn.textContent = fill(TEXT.clipped, { pct: '—' }); return; }
    btn.textContent = fill(TEXT.clipped, { pct: pct >= 0.1 ? pct.toFixed(1) : (pct > 0 ? '<0.1' : '0.0') });
    btn.classList.toggle('is-hot', pct >= 1.0);
  }

  /** Measure clipping on the uncorrected image and paint the mask when it's on. */
  function drawClip(S, b, img) {
    b.querySelector('canvas.cm-clipmask')?.remove();
    const natW = img.naturalWidth, natH = img.naturalHeight;
    if (!natW || !natH) return;
    const { pct, canvas } = _computeClipStats(img, natW, natH, 1, clipMaskEnabled);
    setClipReadout(S, pct);
    if (!clipMaskEnabled || !canvas) return;
    const rect = _containRect(b, natW, natH);
    if (!rect) return;
    canvas.className = 'cm-clipmask';
    Object.assign(canvas.style, { left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px` });
    b.appendChild(canvas);
  }

  function syncSliders(S, r) {
    const pct = Math.round(getExposurePreviewStrength() * 100);
    const exp = S.el.querySelector('.cm-exp');
    if (exp && Math.round(Number(exp.value)) !== pct) exp.value = String(pct);
    S.el.querySelector('.cm-expval').textContent = `${pct}%`;
    const eff = r && !r.__moved ? getRowRawPreviewEffectiveStops(r, false) : 0;
    S.el.querySelector('.cm-explbl').textContent = fill(TEXT.exposure, { ev: formatExposureEv(Number.isFinite(eff) ? eff : 0) });
    const rz = S.el.querySelector('.cm-rawzoom');
    if (rz) rz.value = String(S.rawZoom);
  }

  async function show(S, r) {
    if (!S.el) return;
    const my = ++token;
    const full = box(S, 'full');
    const crop = box(S, 'crop');
    syncSliders(S, r);
    if (!r) { clearBox(full); clearBox(crop); setClipReadout(S, NaN); return; }
    if (zoom && zoom.row !== r) endZoom(S);
    if (zoom) return;

    const [eurl, curl] = await Promise.all([
      getBlobUrlForPath(r.export_path || r.crop_path, r.__rootPath),
      KestrelCullMode.isNoBird(r) ? null : getBlobUrlForPath(activeCropPath(r), r.__rootPath),
    ]);
    if (my !== token || !S.el) return;

    clearBox(full);
    if (eurl) {
      const img = document.createElement('img');
      img.src = eurl;
      img.draggable = false;
      applyThumbnailExposureToImg(img, r);
      full.prepend(img);
      if (img.complete && img.naturalWidth) drawClip(S, full, img);
      else img.addEventListener('load', () => { if (my === token) drawClip(S, full, img); }, { once: true });
    } else {
      setClipReadout(S, NaN);
    }

    clearBox(crop);
    const lbl = crop.querySelector('.cm-lbl');
    const crops = r.__moved ? [] : getRowCropRecords(r);
    const active = r.__moved ? 0 : getRowActiveCropState(r).activeIndex;
    lbl.textContent = crops.length > 1 ? fill(TEXT.birdOf, { i: active + 1, n: crops.length }) : TEXT.crop;
    if (KestrelCullMode.isNoBird(r)) {
      const nb = document.createElement('div');
      nb.className = 'cm-nobird';
      const b = document.createElement('b');
      b.innerHTML = `${KestrelIcon('warning')} `;
      b.appendChild(document.createTextNode(TEXT.noBird));
      const sp = document.createElement('span');
      sp.textContent = fill(TEXT.noBirdBody, { rule: S.session?.rules?.no_bird === 'accept' ? 'accept' : 'reject' });
      nb.append(b, sp);
      crop.appendChild(nb);
    } else if (curl) {
      const cimg = document.createElement('img');
      cimg.src = curl;
      cimg.draggable = false;
      crop.prepend(cimg);
    }
  }

  function activeCropPath(r) {
    if (r.__moved) return r.crop_path;
    const st = getRowActiveCropState(r);
    return st.crops?.[st.activeIndex]?.crop_path || getRowDisplayLabels(r).cropPath || r.crop_path;
  }

  // ── Click-and-hold RAW zoom ────────────────────────────────────────────────

  function drawZoom(S) {
    const b = box(S, 'full');
    if (!zoom || !b) return;
    const img = zoom.img;
    const iw = img.naturalWidth, ih = img.naturalHeight;
    if (!iw || !ih) return;
    const rect = b.getBoundingClientRect();
    const xn = Math.max(0, Math.min(1, (zoom.x - rect.left) / rect.width));
    const yn = Math.max(0, Math.min(1, (zoom.y - rect.top) / rect.height));
    const dpr = window.devicePixelRatio || 1;
    const tw = Math.max(1, Math.round(b.clientWidth * dpr));
    const th = Math.max(1, Math.round(b.clientHeight * dpr));
    let cw = iw / Math.max(1, S.rawZoom), ch = ih / Math.max(1, S.rawZoom);
    if (cw / ch > tw / th) cw = ch * (tw / th); else ch = cw / (tw / th);
    const sx = Math.max(0, Math.min(iw - cw, xn * iw - cw / 2));
    const sy = Math.max(0, Math.min(ih - ch, yn * ih - ch / 2));
    let canvas = b.querySelector('canvas.cm-zoomcanvas');
    if (!canvas) {
      canvas = document.createElement('canvas');
      canvas.className = 'cm-zoomcanvas';
      b.appendChild(canvas);
    }
    if (canvas.width !== tw || canvas.height !== th) { canvas.width = tw; canvas.height = th; }
    const ctx = canvas.getContext('2d', { alpha: false });
    if (!ctx) return;
    ctx.imageSmoothingQuality = 'high';
    ctx.drawImage(img, sx, sy, cw, ch, 0, 0, tw, th);
    canvas.style.filter = zoom.isRaw ? '' : (img.style.filter || '');
  }

  async function loadRaw(S, r) {
    const key = getSceneRawCacheKey(r);
    if (sceneRawCache.has(key)) return { url: sceneRawCache.get(key) };
    if (sceneRawLoading.has(key) || !hasPywebviewApi) return null;
    sceneRawLoading.add(key);
    try {
      const disabled = getSetting('raw_exposure_correction_disabled', false);
      const res = await window.pywebview.api.read_raw_full(
        r.filename, r.__rootPath || '', getRowRawPreviewRequestStops(r, disabled),
        getRowExposurePipelineMode(r), getRowRawPreviewMeterScale(r, disabled));
      if (!res?.success || !res.data) return null;
      let url = sceneRawCache.has(key) ? sceneRawCache.get(key) : null;
      if (!url) { url = _base64ToBlobUrl(res.data, res.mime || 'image/jpeg'); sceneRawCache.set(key, url); }
      return { url, embedded: res.fallback === 'embedded_jpeg_preview' };
    } catch (e) {
      console.warn('[cull] RAW load failed', e);
      return null;
    } finally {
      sceneRawLoading.delete(key);
    }
  }

  function startZoom(S, r, ev) {
    const b = box(S, 'full');
    const shown = b?.querySelector('img');
    if (!shown || r.__moved) return;
    zoom = { row: r, img: shown, x: ev.clientX, y: ev.clientY, isRaw: false };
    b.classList.add('is-zoom');
    const disabled = getSetting('raw_exposure_correction_disabled', false);
    b.dataset.rawLabel = fill(TEXT.rawLabel, { ev: formatExposureEv(getRowRawPreviewEffectiveStops(r, disabled)) });
    drawZoom(S);
    loadRaw(S, r).then((res) => {
      if (!res || !zoom || zoom.row !== r) return;
      const raw = new Image();
      raw.onload = () => {
        if (!zoom || zoom.row !== r) return;
        zoom.img = raw;
        zoom.isRaw = true;
        if (res.embedded) b.dataset.rawLabel = TEXT.rawEmbedded;
        drawZoom(S);
      };
      raw.src = res.url;
    });
  }

  function endZoom(S) {
    if (!zoom) return;
    const r = zoom.row;
    zoom = null;
    const b = box(S, 'full');
    if (!b) return;
    b.classList.remove('is-zoom');
    b.querySelector('canvas.cm-zoomcanvas')?.remove();
    show(S, r);
  }

  function toggleClip(S) {
    clipMaskEnabled = !clipMaskEnabled;
    const b = box(S, 'full');
    const img = b?.querySelector('img');
    if (img) drawClip(S, b, img); else setClipReadout(S, NaN);
  }

  function wire(S) {
    if (!windowWired) {
      windowWired = true;
      window.addEventListener('mousemove', (e) => {
        if (!zoom || !S.el) return;
        zoom.x = e.clientX; zoom.y = e.clientY;
        drawZoom(S);
      });
      window.addEventListener('mouseup', () => { if (zoom && S.el) endZoom(S); });
    }
    box(S, 'full').addEventListener('wheel', (e) => {
      if (!zoom) return;
      e.preventDefault();
      S.rawZoom = Math.max(2, Math.min(12, S.rawZoom + (e.deltaY < 0 ? 0.5 : -0.5)));
      S.el.querySelector('.cm-rawzoom').value = String(S.rawZoom);
      drawZoom(S);
    }, { passive: false });
    S.el.querySelector('.cm-rawzoom').addEventListener('input', (e) => { S.rawZoom = Number(e.target.value) || 5; });
    S.el.querySelector('.cm-exp').addEventListener('input', (e) => {
      applyExposurePreviewStrengthPct(Number(e.target.value));
      syncSliders(S, S.sel);
    });
  }

  window.KestrelCullStage = { show, startZoom, endZoom, toggleClip, wire };
})();
