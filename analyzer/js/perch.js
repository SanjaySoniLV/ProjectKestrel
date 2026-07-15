
    // ── Share-with-Perch dialog + progress card ────────────────────────
    // State scoped to the currently-open dialog. Refreshed on each open.
    const _perchDlgState = {
      rootPath: null,
      preflight: null,        // full response from Api.preflight_perch_upload
      deselected: new Set(),  // sceneIds the user has unchecked
      skipRejected: true,     // tracks the "Skip rejected photos" checkbox
      resumable: null,        // {perchId, idempotencyKey, total, committed, pending} when partial upload found
      tagDiff: null,          // {perchId, changes:[...]} when linked-folder tags differ from the perch (H7)
    };
    // Job state for the in-flight upload (only one allowed at a time).
    let _perchActiveJobId = null;
    let _perchActivePollTimer = null;

    function formatPerchBytes(n) {
      const x = Number(n) || 0;
      if (x <= 0) return '0 KB';
      if (x < 1024) return x + ' B';
      if (x < 1024 * 1024) return (x / 1024).toFixed(1) + ' KB';
      if (x < 1024 * 1024 * 1024) return (x / (1024 * 1024)).toFixed(1) + ' MB';
      return (x / (1024 * 1024 * 1024)).toFixed(2) + ' GB';
    }

    function _perchFormatTimestamp(ms) {
      if (!Number.isFinite(ms)) return '';
      try {
        return new Date(ms).toLocaleString(undefined, {
          month: 'short', day: 'numeric',
          hour: 'numeric', minute: '2-digit',
        });
      } catch { return ''; }
    }

    function _perchSelectedTotals() {
      const pre = _perchDlgState.preflight;
      if (!pre) return { scenes: 0, photos: 0, exports: 0, crops: 0, files: 0, bytes: 0 };
      let scenes = 0, photos = 0, exp = 0, crops = 0, bytes = 0;
      for (const s of pre.scenes || []) {
        if (_perchDlgState.deselected.has(String(s.sceneId))) continue;
        scenes++;
        photos += Number(s.imageCount || 0);
        exp += Number(s.exportCount || 0);
        crops += Number(s.cropCount || 0);
        bytes += Number(s.totalBytes || 0);
      }
      return { scenes, photos, exports: exp, crops, files: exp + crops, bytes };
    }

    function _perchUpdateDialogTotals() {
      const t = _perchSelectedTotals();
      const elSubmit = document.getElementById('perchUploadSubmitBtn');
      if (elSubmit) {
        const label = t.photos > 0 ? `📤 Upload ${t.photos.toLocaleString()} photo${t.photos === 1 ? '' : 's'}` : '📤 Upload';
        elSubmit.textContent = label;
        elSubmit.disabled = (t.photos === 0);
      }
      // Sidebar panel updates (totals card replaces the old timeline-summary).
      if (typeof _perchUpdateTotalsCard === 'function') _perchUpdateTotalsCard();
      if (typeof _perchUpdateFirstPhotoPanel === 'function') _perchUpdateFirstPhotoPanel();
      if (typeof _perchUpdateReviewPanel === 'function') _perchUpdateReviewPanel();
    }

    // Mirror of scene-grid.js:buildTimeClusters/computeDynamicClusterGapMs —
    // groups scenes into time-adjacent clusters so the Perch timeline reads
    // the same as the main folder timeline (bursts cluster together,
    // long gaps create new nodes).
    const _PERCH_CLUSTER_GAP_FALLBACK_MS = 15 * 60 * 1000;
    const _PERCH_CLUSTER_GAP_MULTIPLIER = 8;
    const _PERCH_CLUSTER_GAP_MIN_MS = 90 * 1000;
    const _PERCH_CLUSTER_GAP_MAX_MS = 4 * 60 * 60 * 1000;

    function _perchComputeClusterGapMs(scenes) {
      const times = [];
      for (const s of scenes) {
        if (Number.isFinite(s.captureTimeMs)) times.push(s.captureTimeMs);
      }
      if (times.length < 3) return _PERCH_CLUSTER_GAP_FALLBACK_MS;
      times.sort((a, b) => a - b);
      const gaps = [];
      for (let i = 1; i < times.length; i++) {
        const g = times[i] - times[i - 1];
        if (g > 0) gaps.push(g);
      }
      if (!gaps.length) return _PERCH_CLUSTER_GAP_FALLBACK_MS;
      gaps.sort((a, b) => a - b);
      const median = gaps[Math.floor(gaps.length / 2)];
      const threshold = median * _PERCH_CLUSTER_GAP_MULTIPLIER;
      return Math.max(_PERCH_CLUSTER_GAP_MIN_MS, Math.min(_PERCH_CLUSTER_GAP_MAX_MS, threshold));
    }

    function _perchBuildTimeClusters(scenes, gapMs) {
      const timed = [];
      const untimed = [];
      for (const s of scenes || []) {
        if (Number.isFinite(s.captureTimeMs)) timed.push(s);
        else untimed.push(s);
      }
      timed.sort((a, b) => a.captureTimeMs - b.captureTimeMs);
      const clusters = [];
      for (const s of timed) {
        const last = clusters[clusters.length - 1];
        if (!last || s.captureTimeMs - last.endMs > gapMs) {
          clusters.push({ scenes: [s], startMs: s.captureTimeMs, endMs: s.captureTimeMs, untimed: false });
        } else {
          last.scenes.push(s);
          if (s.captureTimeMs > last.endMs) last.endMs = s.captureTimeMs;
        }
      }
      if (untimed.length) {
        clusters.push({ scenes: untimed, startMs: null, endMs: null, untimed: true });
      }
      return clusters;
    }

    function _perchDayKey(ms) {
      if (!Number.isFinite(ms)) return '';
      const d = new Date(ms);
      if (isNaN(d)) return '';
      const p2 = (n) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())}`;
    }
    function _perchFormatClusterTime(ms) {
      if (!Number.isFinite(ms)) return '';
      try {
        return new Date(ms).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
      } catch { return ''; }
    }
    function _perchFormatClusterDay(ms) {
      if (!Number.isFinite(ms)) return '';
      try {
        return new Date(ms).toLocaleDateString(undefined, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
      } catch { return ''; }
    }
    function _perchClusterImageCount(cluster) {
      let n = 0;
      for (const s of cluster.scenes) n += Number(s.imageCount || 0);
      return n;
    }
    function _perchDotSizePx(imgCount, maxInFolder) {
      // Mirrors scene-grid.js dot-sizing intent: log-ish scale, 8-22px.
      if (!Number.isFinite(imgCount) || imgCount <= 0) return 8;
      const minPx = 8, maxPx = 22;
      if (maxInFolder <= 1) return minPx;
      const t = Math.log(1 + imgCount) / Math.log(1 + maxInFolder);
      return Math.round(minPx + t * (maxPx - minPx));
    }

    /** Build one scene card for the Perch timeline. Mirrors the visual treatment
     *  of the folder-grid scene cards (same .card and .scene-approved classes)
     *  but click-to-toggle exclude instead of opening the scene dialog. */
    function _perchBuildSceneCard(scene, rootPath) {
      const sid = String(scene.sceneId);
      const card = document.createElement('article');
      card.className = 'card perch-card';
      if (scene.reviewed) card.classList.add('scene-approved');
      if (_perchDlgState.deselected.has(sid)) card.classList.add('is-perch-excluded');
      card.dataset.sceneId = sid;
      card.title = 'Click to ' + (_perchDlgState.deselected.has(sid) ? 'include' : 'exclude') + ' this scene';

      // Thumbnail
      const thumbWrap = document.createElement('div');
      thumbWrap.className = 'card-thumb';
      const img = document.createElement('img');
      img.alt = '';
      img.loading = 'lazy';
      thumbWrap.appendChild(img);
      const overlay = document.createElement('div');
      overlay.className = 'card-perch-overlay';
      overlay.textContent = '\u{1F6AB} Excluded';
      thumbWrap.appendChild(overlay);
      card.appendChild(thumbWrap);
      if (scene.thumbnailPath && rootPath && typeof getBlobUrlForPath === 'function') {
        getBlobUrlForPath(scene.thumbnailPath, rootPath).then(url => {
          if (url) img.src = url;
          else thumbWrap.classList.add('no-image');
        }).catch(() => thumbWrap.classList.add('no-image'));
      } else {
        thumbWrap.classList.add('no-image');
      }

      // Species/family chips (limit to first 3 so cards stay compact)
      const tagBag = [...(scene.species || []), ...(scene.families || [])];
      if (tagBag.length) {
        const chipRow = document.createElement('div');
        chipRow.className = 'perch-card-chips';
        const shown = tagBag.slice(0, 3);
        for (const t of shown) {
          const chip = document.createElement('span');
          chip.className = 'chip' + (scene.reviewed ? ' manual-approved' : '');
          chip.textContent = t;
          chipRow.appendChild(chip);
        }
        if (tagBag.length > shown.length) {
          const more = document.createElement('span');
          more.className = 'chip more';
          more.textContent = `+${tagBag.length - shown.length}`;
          chipRow.appendChild(more);
        }
        card.appendChild(chipRow);
      }

      // Metrics row: "12 photos · 24 MB" (+ "· N hidden" when skip-rejected drops some)
      const metrics = document.createElement('div');
      metrics.className = 'card-perch-metrics';
      _perchUpdateCardMetrics(metrics, scene);
      card.appendChild(metrics);

      // Click toggles include/exclude. Only re-render this card + totals; do NOT
      // re-render the timeline (would lose scroll position).
      card.addEventListener('click', () => {
        if (_perchDlgState.deselected.has(sid)) _perchDlgState.deselected.delete(sid);
        else _perchDlgState.deselected.add(sid);
        const excluded = _perchDlgState.deselected.has(sid);
        card.classList.toggle('is-perch-excluded', excluded);
        card.title = 'Click to ' + (excluded ? 'include' : 'exclude') + ' this scene';
        _perchUpdateDialogTotals();
      });

      return card;
    }

    function _perchUpdateCardMetrics(metricsEl, scene) {
      const photos = Number(scene.imageCount || 0);
      const skipped = Number(scene.rejectedSkipped || 0);
      const parts = [
        `${photos.toLocaleString()} photo${photos === 1 ? '' : 's'}`,
        formatPerchBytes(scene.totalBytes),
      ];
      if (skipped > 0 && _perchDlgState.skipRejected) {
        parts.push(`${skipped.toLocaleString()} hidden`);
      }
      metricsEl.textContent = parts.join(' · ');
    }

    /** Render the timeline of scene cards. Mirrors the main folder timeline:
     *  gap-based clusters, rail dot per cluster (sized by image count), time
     *  header per cluster, day banners between calendar-date changes. */
    function _perchRenderTimeline() {
      const container = document.getElementById('perchDlgTimeline');
      if (!container) return;
      container.innerHTML = '';
      const pre = _perchDlgState.preflight;
      const rootPath = _perchDlgState.rootPath;
      if (!pre || !pre.scenes || pre.scenes.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'perch-scene-empty';
        empty.textContent = 'No scenes found in this folder.';
        container.appendChild(empty);
        return;
      }
      const body = document.createElement('div');
      body.className = 'timeline-body perch-timeline';
      const gapMs = _perchComputeClusterGapMs(pre.scenes);
      const clusters = _perchBuildTimeClusters(pre.scenes, gapMs);
      let maxImg = 1;
      for (const c of clusters) {
        const n = _perchClusterImageCount(c);
        if (n > maxImg) maxImg = n;
      }
      let prevDay = null;
      for (let i = 0; i < clusters.length; i++) {
        const cluster = clusters[i];
        const isLast = i === clusters.length - 1;
        const thisDay = cluster.untimed ? '' : _perchDayKey(cluster.startMs);

        if (thisDay && thisDay !== prevDay) {
          const banner = document.createElement('div');
          banner.className = 'timeline-day-banner';
          banner.textContent = _perchFormatClusterDay(cluster.startMs);
          body.appendChild(banner);
          prevDay = thisDay;
        }

        const node = document.createElement('div');
        node.className = 'timeline-node' + (cluster.untimed ? ' timeline-node-untimed' : '');

        // Rail column: dot (sized by image count) + connecting line
        const rail = document.createElement('div');
        rail.className = 'timeline-rail-col';
        const dot = document.createElement('div');
        dot.className = 'timeline-dot';
        const imgCount = _perchClusterImageCount(cluster);
        const dotSize = _perchDotSizePx(imgCount, maxImg);
        dot.style.width = dotSize + 'px';
        dot.style.height = dotSize + 'px';
        dot.title = `${cluster.scenes.length} scene${cluster.scenes.length === 1 ? '' : 's'} · ${imgCount} image${imgCount === 1 ? '' : 's'}`;
        const line = document.createElement('div');
        line.className = 'timeline-line' + (isLast ? ' last' : '');
        rail.appendChild(dot);
        rail.appendChild(line);

        // Content column: header (time + count) + grid of scene cards
        const content = document.createElement('div');
        content.className = 'timeline-content-col';

        const hdr = document.createElement('div');
        hdr.className = 'timeline-node-header';
        const timeSpan = document.createElement('span');
        timeSpan.className = 'timeline-node-time';
        if (cluster.untimed) {
          timeSpan.textContent = 'Unknown time';
        } else {
          const spanMs = cluster.endMs - cluster.startMs;
          timeSpan.textContent = spanMs < 2 * 60 * 1000
            ? _perchFormatClusterTime(cluster.startMs)
            : `${_perchFormatClusterTime(cluster.startMs)} – ${_perchFormatClusterTime(cluster.endMs)}`;
        }
        hdr.appendChild(timeSpan);
        const countSpan = document.createElement('span');
        countSpan.className = 'timeline-node-count muted';
        countSpan.textContent =
          `${cluster.scenes.length} scene${cluster.scenes.length === 1 ? '' : 's'} · ${imgCount} image${imgCount === 1 ? '' : 's'}`;
        hdr.appendChild(countSpan);
        content.appendChild(hdr);

        const grid = document.createElement('div');
        grid.className = 'grid timeline-grid perch-cluster-grid';
        for (const s of cluster.scenes) {
          grid.appendChild(_perchBuildSceneCard(s, rootPath));
        }
        content.appendChild(grid);

        node.appendChild(rail);
        node.appendChild(content);
        body.appendChild(node);
      }
      container.appendChild(body);
    }

    // Latest entitlements snapshot (limits + currentUsage). Lazy-loaded by
    // _perchLoadAccountAndUsage so the usage bar can render absolute %.
    let _perchEntitlements = null;

    /** Update the Review Total Usage card: scenes/images/bytes plus the % of
     *  this perch's bytes against the user's maxTotalStorageMB. */
    function _perchUpdateTotalsCard() {
      const t = _perchSelectedTotals();
      const totalScenes = (_perchDlgState.preflight?.scenes || []).length;
      const totalPhotos = (_perchDlgState.preflight?.scenes || [])
        .reduce((acc, s) => acc + Number(s.imageCount || 0), 0);
      const elScenes = document.getElementById('perchTotalsScenes');
      const elPhotos = document.getElementById('perchTotalsPhotos');
      const elBytes = document.getElementById('perchTotalsBytes');
      const elPct = document.getElementById('perchTotalsBytesPct');
      if (elScenes) {
        elScenes.textContent = t.scenes === totalScenes
          ? t.scenes.toLocaleString()
          : `${t.scenes.toLocaleString()} / ${totalScenes.toLocaleString()}`;
      }
      if (elPhotos) elPhotos.textContent = `${t.photos.toLocaleString()} / ${totalPhotos.toLocaleString()}`;
      if (elBytes) elBytes.textContent = formatPerchBytes(t.bytes);
      // Compute % of plan storage if we have entitlements.
      const limitMB = Number(_perchEntitlements?.limits?.maxTotalStorageMB || 0);
      if (elPct) {
        if (limitMB > 0 && t.bytes > 0) {
          const pct = (t.bytes / (limitMB * 1024 * 1024)) * 100;
          const formatted = pct < 0.1 ? '<0.1' : pct < 10 ? pct.toFixed(1) : Math.round(pct);
          elPct.textContent = `${formatted}% of plan`;
        } else {
          elPct.textContent = 'Size';
        }
      }
      // Also push to the usage bar's pending segment so it reflects current selection.
      _perchUpdateUsageBar(t.bytes);
    }

    /** Update the usage bar's pending segment (this perch's contribution).
     *  Used: total currently consumed on the server. Pending: t.bytes from
     *  the current dialog selection. */
    function _perchUpdateUsageBar(pendingBytes) {
      const barUsed = document.getElementById('perchUsageBarUsed');
      const barPending = document.getElementById('perchUsageBarPending');
      const txtEl = document.getElementById('perchUsageText');
      const barEl = document.getElementById('perchUsageBar');
      const limitMB = Number(_perchEntitlements?.limits?.maxTotalStorageMB || 0);
      const usedBytes = Number(_perchEntitlements?.currentUsage?.totalStorageBytes
        ?? _perchDlgState._usageTotalBytes
        ?? 0);
      if (limitMB <= 0) {
        // No plan info — hide bar fill, just show absolute numbers.
        if (barUsed) barUsed.style.width = '0%';
        if (barPending) barPending.style.width = '0%';
        if (txtEl) {
          txtEl.textContent = pendingBytes > 0
            ? `${formatPerchBytes(usedBytes)} used · +${formatPerchBytes(pendingBytes)} this perch`
            : `${formatPerchBytes(usedBytes)} used`;
        }
        return;
      }
      const limitBytes = limitMB * 1024 * 1024;
      const usedPct = Math.max(0, Math.min(100, (usedBytes / limitBytes) * 100));
      const pendingPct = Math.max(0, Math.min(100 - usedPct, (pendingBytes / limitBytes) * 100));
      if (barUsed) barUsed.style.width = usedPct + '%';
      if (barPending) {
        barPending.style.width = pendingPct + '%';
        barPending.style.left = usedPct + '%';
      }
      if (txtEl) {
        const usedTxt = formatPerchBytes(usedBytes);
        const limitTxt = formatPerchBytes(limitBytes);
        txtEl.textContent = pendingBytes > 0
          ? `${usedTxt} used · +${formatPerchBytes(pendingBytes)} this perch · ${limitTxt} total`
          : `${usedTxt} of ${limitTxt} used`;
      }
      if (barEl) {
        barEl.title = `${formatPerchBytes(usedBytes)} used of ${formatPerchBytes(limitBytes)} plan limit`
          + (pendingBytes > 0 ? ` · +${formatPerchBytes(pendingBytes)} pending this upload` : '');
      }
    }

    /** Refresh the "Check photo capture time" panel — first non-excluded
     *  scene with a capture time + its thumbnail. The time is written into
     *  the inline <strong id="perchFirstPhotoTime"> inside the description. */
    function _perchUpdateFirstPhotoPanel() {
      const thumbEl = document.getElementById('perchFirstPhotoThumb');
      const timeEl = document.getElementById('perchFirstPhotoTime');
      const pre = _perchDlgState.preflight;
      const rootPath = _perchDlgState.rootPath;
      if (!pre || !pre.scenes) {
        if (timeEl) timeEl.textContent = '—';
        return;
      }
      const firstIncluded = pre.scenes.find(s =>
        !_perchDlgState.deselected.has(String(s.sceneId))
        && Number.isFinite(Number(s.captureTimeMs))
        && Number(s.captureTimeMs) > 0
      );
      if (!firstIncluded) {
        if (timeEl) timeEl.textContent = 'an unknown time';
        if (thumbEl) { thumbEl.removeAttribute('src'); thumbEl.classList.add('no-image'); }
        return;
      }
      if (timeEl) timeEl.textContent = _perchFormatTimestamp(firstIncluded.captureTimeMs) || '—';
      if (thumbEl && firstIncluded.thumbnailPath && rootPath && typeof getBlobUrlForPath === 'function') {
        getBlobUrlForPath(firstIncluded.thumbnailPath, rootPath).then(url => {
          if (url) { thumbEl.src = url; thumbEl.classList.remove('no-image'); }
          else thumbEl.classList.add('no-image');
        }).catch(() => thumbEl.classList.add('no-image'));
      }
    }

    /** Refresh the Review Species Tags panel's inline count. */
    function _perchUpdateReviewPanel() {
      const countEl = document.getElementById('perchReviewCount');
      if (!countEl) return;
      const pre = _perchDlgState.preflight;
      if (!pre || !pre.scenes) { countEl.textContent = '— of — scenes reviewed.'; return; }
      const included = pre.scenes.filter(s => !_perchDlgState.deselected.has(String(s.sceneId)));
      const reviewed = included.filter(s => !!s.reviewed).length;
      countEl.textContent = `${reviewed.toLocaleString()} of ${included.length.toLocaleString()} scene${included.length === 1 ? '' : 's'} reviewed.`;
    }

    async function _perchLoadAccountAndUsage() {
      const elName = document.getElementById('perchAccountName');
      const elMeta = document.getElementById('perchAccountMeta');
      const elAvatar = document.getElementById('perchAccountAvatar');
      // Account info (avatar + name + handle)
      try {
        const res = await window.pywebview.api.get_perch_account();
        if (res && res.success && res.account) {
          const acc = res.account;
          const display = acc.displayName || acc.display_name || acc.first_name || acc.username || 'Signed in';
          const handle = acc.username ? '@' + acc.username : (acc.email || acc.user_id || '');
          if (elName) elName.textContent = display;
          if (elMeta) elMeta.textContent = handle;
          if (elAvatar) elAvatar.textContent = (display.trim()[0] || '?').toUpperCase();
        } else if (elName) {
          elName.textContent = 'Signed in';
        }
      } catch { if (elName) elName.textContent = 'Signed in'; }
      // Current Perch usage (used for the bar's "used" segment fallback when
      // entitlements don't include currentUsage).
      try {
        const res = await window.pywebview.api.get_perch_usage();
        if (res && res.success && res.usage) {
          _perchDlgState._usageTotalBytes = Number(res.usage.totalBytes || 0);
        }
      } catch {}
      // Entitlements — gives us maxTotalStorageMB for the usage-bar limit
      // and totals card percentage. Reuses the same /v1/me/entitlements
      // endpoint that MyAccount + Cloud Compute already call.
      try {
        if (window.pywebview?.api?.cloud_compute_get_entitlements) {
          const r = await window.pywebview.api.cloud_compute_get_entitlements();
          if (r && r.ok) {
            // Bridge returns {ok, tier, limits, currentUsage, activeJobs}
            _perchEntitlements = {
              tier: r.tier,
              limits: r.limits || {},
              currentUsage: r.currentUsage || {},
            };
          }
        }
      } catch {}
      // Initial paint of the usage bar — pending=0 until totals card update.
      _perchUpdateUsageBar(0);
    }

    function _perchClosePerchDialog() {
      const dlg = document.getElementById('perchUploadDlg');
      if (dlg && dlg.open) {
        try { dlg.close(); } catch {}
      }
    }

    function _perchSetButtonsDisabled(disabled) {
      const buttons = document.querySelectorAll('.share-perch-btn');
      buttons.forEach(b => {
        b.disabled = !!disabled;
        b.title = disabled
          ? 'A Perch upload is already running'
          : 'Create an Unfinished Perch on the web with this folder’s Kestrel analysis (export and crop images)';
      });
    }

    function _perchEnsureUploadsPanel() {
      const panel = document.getElementById('perchUploadsPanel');
      if (panel) panel.classList.remove('hidden');
      _perchRepositionUploadsPanel();
      return panel;
    }

    /**
     * Position the Perch uploads panel above whichever queue panels are
     * visible (local analysis + cloud analysis). Right-aligned with them;
     * bottom = 20 + sum of visible queue heights + 12px gap per visible queue.
     */
    function _perchRepositionUploadsPanel() {
      const panel = document.getElementById('perchUploadsPanel');
      if (!panel || panel.classList.contains('hidden')) return;
      const gap = 12;
      let bottom = 20;
      const queue = document.getElementById('queuePanel');
      const cloud = document.getElementById('cloudQueuePanel');
      if (queue && !queue.classList.contains('hidden')) {
        const h = queue.getBoundingClientRect().height;
        if (h > 0) bottom += h + gap;
      }
      if (cloud && !cloud.classList.contains('hidden')) {
        const h = cloud.getBoundingClientRect().height;
        if (h > 0) bottom += h + gap;
      }
      panel.style.bottom = bottom + 'px';
    }

    function _perchInstallPanelObservers() {
      if (_perchInstallPanelObservers._done) return;
      _perchInstallPanelObservers._done = true;
      const targets = [
        document.getElementById('queuePanel'),
        document.getElementById('cloudQueuePanel'),
      ].filter(Boolean);
      for (const el of targets) {
        // Watch for size changes (queue items added/removed, body collapsed).
        try {
          const ro = new ResizeObserver(() => _perchRepositionUploadsPanel());
          ro.observe(el);
        } catch {}
        // Watch for the .hidden class flipping.
        try {
          const mo = new MutationObserver(() => _perchRepositionUploadsPanel());
          mo.observe(el, { attributes: true, attributeFilter: ['class'] });
        } catch {}
      }
      window.addEventListener('resize', _perchRepositionUploadsPanel);
    }

    function _perchUploadCardHtml() {
      return `
        <div class="perch-upload-card-header">
          <span class="perch-upload-card-title">Uploading to Perch</span>
          <span class="perch-upload-card-status" data-role="status">Starting…</span>
          <button type="button" class="perch-upload-card-cancel" data-role="cancel" title="Cancel">✕</button>
        </div>
        <div class="perch-upload-card-body" data-role="body">
          <div class="perch-upload-card-progress"><div class="perch-upload-card-progress-fill" data-role="fill" style="width:0%"></div></div>
          <div class="perch-upload-card-current" data-role="current">Preparing files…</div>
        </div>
      `;
    }

    function _perchRenderUploadCard(jobId) {
      const panel = _perchEnsureUploadsPanel();
      if (!panel) return null;
      const body = document.getElementById('perchUploadsBody');
      if (!body) return null;
      // Replace the body — only one upload card at a time (concurrency-gated).
      body.innerHTML = '';
      const card = document.createElement('div');
      card.className = 'perch-upload-card running';
      card.dataset.jobId = jobId;
      card.innerHTML = _perchUploadCardHtml();
      body.appendChild(card);
      const cancelBtn = card.querySelector('[data-role="cancel"]');
      if (cancelBtn) {
        cancelBtn.addEventListener('click', async () => {
          cancelBtn.disabled = true;
          try { await window.pywebview.api.cancel_share(jobId); } catch {}
        });
      }
      const badge = document.getElementById('perchUploadsBadge');
      if (badge) { badge.textContent = 'Uploading'; badge.className = 'perch-uploads-badge'; }
      return card;
    }

    function _perchUpdateUploadCard(card, prog) {
      if (!card || !prog) return;
      const status = card.querySelector('[data-role="status"]');
      const fill = card.querySelector('[data-role="fill"]');
      const current = card.querySelector('[data-role="current"]');
      const phase = prog.phase;
      if (phase === 'creating_perch') {
        if (status) status.textContent = 'Creating perch…';
        if (current) current.textContent = 'Reserving a draft perch on the server.';
      } else if (phase === 'presigning') {
        if (status) status.textContent = `Presigning ${prog.current || 0}/${prog.total || 0}`;
        if (current) current.textContent = 'Allocating upload slots…';
      } else if (phase === 'uploading') {
        const uploaded = Number(prog.uploaded || 0);
        const total = Number(prog.total || 0);
        const pct = total > 0 ? Math.round((uploaded / total) * 100) : 0;
        if (status) status.textContent = `${pct}% · ${uploaded}/${total}`;
        if (fill) fill.style.width = pct + '%';
        if (current) current.textContent = prog.filename ? `Uploading ${prog.filename}` : 'Uploading…';
      }
    }

    function _perchSwapToDoneState(card, prog) {
      if (!card) return;
      const url = prog && prog.perch_url;
      card.className = 'perch-upload-card is-done';
      card.innerHTML = `
        <div class="perch-upload-card-header">
          <span class="perch-upload-card-title">✓ Uploaded to Perch</span>
          <button type="button" class="perch-upload-card-dismiss" data-role="dismiss" title="Dismiss">✕</button>
        </div>
        <div class="perch-upload-card-body">
          <div class="perch-upload-card-success">Your perch is ready.</div>
          ${url ? `<button type="button" class="perch-upload-card-cta" data-role="open">Open in browser →</button>` : ''}
        </div>
      `;
      const open = card.querySelector('[data-role="open"]');
      if (open && url) {
        open.addEventListener('click', () => {
          try { window.pywebview.api.open_perch_url(url); } catch {}
        });
      }
      const dismiss = card.querySelector('[data-role="dismiss"]');
      if (dismiss) dismiss.addEventListener('click', () => _perchDismissCard(card));
      const badge = document.getElementById('perchUploadsBadge');
      if (badge) { badge.textContent = 'Done'; badge.className = 'perch-uploads-badge done'; }
    }

    function _perchSwapToCanceledState(card, prog) {
      if (!card) return;
      const url = prog && prog.perch_url;
      card.className = 'perch-upload-card is-canceled';
      const uploaded = Number(prog && prog.uploaded || 0);
      const total = Number(prog && prog.total || 0);
      card.innerHTML = `
        <div class="perch-upload-card-header">
          <span class="perch-upload-card-title">Upload canceled</span>
          <button type="button" class="perch-upload-card-dismiss" data-role="dismiss" title="Dismiss">✕</button>
        </div>
        <div class="perch-upload-card-body">
          <div class="perch-upload-card-warn">Stopped after uploading ${uploaded.toLocaleString()} of ${total.toLocaleString()} files. The partial perch is still using your storage.</div>
          ${url ? `<button type="button" class="perch-upload-card-cta warn" data-role="open">Delete this Perch to clear your usage →</button>` : ''}
        </div>
      `;
      const open = card.querySelector('[data-role="open"]');
      if (open && url) {
        open.addEventListener('click', () => {
          try { window.pywebview.api.open_perch_url(url); } catch {}
        });
      }
      const dismiss = card.querySelector('[data-role="dismiss"]');
      if (dismiss) dismiss.addEventListener('click', () => _perchDismissCard(card));
      const badge = document.getElementById('perchUploadsBadge');
      if (badge) { badge.textContent = 'Canceled'; badge.className = 'perch-uploads-badge canceled'; }
    }

    // Plain-language copy for each plan-limit error code from the Perch
    // Worker (see Perch Worker/src/lib/caps.ts). Falls back to a generic
    // line when the code is unknown.
    function _perchPlanLimitCopy(prog) {
      const code = prog && prog.errorCode;
      const limit = Number(prog && prog.limit);
      const current = Number(prog && prog.current);
      const limitMB = Number.isFinite(limit) ? Math.round(limit / (1024 * 1024)) : null;
      const filename = (prog && prog.filename) ? String(prog.filename) : null;
      const tier = (prog && prog.tier) ? String(prog.tier) : null;
      const tierLine = tier ? ` Your current plan is ${tier}.` : '';
      switch (code) {
        case 'perch_limit_reached':
          return {
            title: 'Perch limit reached',
            body: `You've used ${Number.isFinite(current) ? current : '?'} of ${Number.isFinite(limit) ? limit : '?'} perches available on your plan.${tierLine}`,
          };
        case 'perch_storage_limit_reached':
          return {
            title: 'Perch is full',
            body: `This perch has hit the storage limit (${limitMB != null ? limitMB + ' MB' : 'plan cap'}). Split the upload across multiple perches, or get in touch from your account portal — extra storage can often be arranged.${tierLine}`,
          };
        case 'perch_image_limit_reached':
          return {
            title: 'Perch image limit reached',
            body: `This perch is at the maximum number of images allowed on your plan (${Number.isFinite(limit) ? limit : '?'}).${tierLine}`,
          };
        case 'perch_asset_limit_reached':
          return {
            title: 'Perch asset limit reached',
            body: `This perch is at the maximum number of assets (images + crops) allowed on your plan (${Number.isFinite(limit) ? limit : '?'}).${tierLine}`,
          };
        case 'asset_too_large':
          return {
            title: 'File too large for your plan',
            body: `${filename ? `"${filename}" is` : 'A file is'} larger than your plan's per-file limit (${limitMB != null ? limitMB + ' MB' : 'plan cap'}). Get in touch from your account portal if you need a higher limit.${tierLine}`,
          };
        default:
          return {
            title: 'Plan limit reached',
            body: (prog && prog.friendlyMessage) ? String(prog.friendlyMessage) : 'Your upload exceeds your current plan limits.',
          };
      }
    }

    function _perchSwapToPlanLimitState(card, prog) {
      if (!card) return;
      const copy = _perchPlanLimitCopy(prog);
      // Account management, not a purchase CTA. Perch tiers grant no extra
      // storage today — caps are lifted per-account on request — so "Upgrade"
      // would sell a fix that doesn't exist. It would also void the App Store's
      // 3.1.3(f) IAP exemption, which requires no purchase calls-to-action.
      const manageUrl = (prog && (prog.manageUrl || prog.upgradeUrl))
        || 'https://myaccount.projectkestrel.org/perch';
      const esc = (s) => String(s).replace(/[<>&"']/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;'}[c]));
      card.className = 'perch-upload-card is-error';
      card.innerHTML = `
        <div class="perch-upload-card-header">
          <span class="perch-upload-card-title">${esc(copy.title)}</span>
          <button type="button" class="perch-upload-card-dismiss" data-role="dismiss" title="Dismiss">✕</button>
        </div>
        <div class="perch-upload-card-body">
          <div class="perch-upload-card-err">${esc(copy.body)}</div>
          <button type="button" class="perch-upload-card-cta" data-role="manage">Manage my account →</button>
        </div>
      `;
      const manage = card.querySelector('[data-role="manage"]');
      if (manage) {
        manage.addEventListener('click', () => {
          try { window.pywebview.api.open_perch_url(manageUrl); } catch {}
        });
      }
      const dismiss = card.querySelector('[data-role="dismiss"]');
      if (dismiss) dismiss.addEventListener('click', () => _perchDismissCard(card));
      const badge = document.getElementById('perchUploadsBadge');
      if (badge) { badge.textContent = 'Plan limit'; badge.className = 'perch-uploads-badge error'; }
    }

    function _perchSwapToErrorState(card, prog, rootPath) {
      if (!card) return;
      // Stage 7: tier-cap denials get their own card with an account CTA
      // (no Retry — retrying against the same cap would just 403 again).
      if (prog && prog.message === 'plan_limit_exceeded') {
        _perchSwapToPlanLimitState(card, prog);
        return;
      }
      const msg = (prog && prog.message) || 'Unknown error';
      card.className = 'perch-upload-card is-error';
      card.innerHTML = `
        <div class="perch-upload-card-header">
          <span class="perch-upload-card-title">Upload failed</span>
          <button type="button" class="perch-upload-card-dismiss" data-role="dismiss" title="Dismiss">✕</button>
        </div>
        <div class="perch-upload-card-body">
          <div class="perch-upload-card-err">${String(msg).replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}</div>
          <button type="button" class="perch-upload-card-cta" data-role="retry">Retry</button>
        </div>
      `;
      const retry = card.querySelector('[data-role="retry"]');
      if (retry && rootPath) {
        retry.addEventListener('click', () => {
          _perchDismissCard(card);
          openPerchDialog(rootPath);
        });
      }
      const dismiss = card.querySelector('[data-role="dismiss"]');
      if (dismiss) dismiss.addEventListener('click', () => _perchDismissCard(card));
      const badge = document.getElementById('perchUploadsBadge');
      if (badge) { badge.textContent = 'Error'; badge.className = 'perch-uploads-badge error'; }
    }

    function _perchDismissCard(card) {
      if (card && card.parentNode) card.parentNode.removeChild(card);
      const body = document.getElementById('perchUploadsBody');
      const panel = document.getElementById('perchUploadsPanel');
      if (body && panel && body.children.length === 0) panel.classList.add('hidden');
    }

    async function _perchPollProgress(jobId, card, rootPath) {
      let res;
      try {
        res = await window.pywebview.api.get_share_progress(jobId);
      } catch (e) {
        return;
      }
      if (!res || !res.success) return;
      const prog = res.progress || {};
      const phase = prog.phase;
      if (phase === 'done') {
        _perchSwapToDoneState(card, prog);
        _perchActiveJobId = null;
        if (_perchActivePollTimer) { clearInterval(_perchActivePollTimer); _perchActivePollTimer = null; }
        _perchSetButtonsDisabled(false);
        // Server confirmed the perch is live → transition the folder's
        // "Share with Perch" button straight to "On Perch".
        if (rootPath) _perchMarkFolderOnPerch(rootPath, prog.perch_url || prog.perchUrl || '');
      } else if (phase === 'canceled') {
        _perchSwapToCanceledState(card, prog);
        _perchActiveJobId = null;
        if (_perchActivePollTimer) { clearInterval(_perchActivePollTimer); _perchActivePollTimer = null; }
        _perchSetButtonsDisabled(false);
      } else if (phase === 'error') {
        _perchSwapToErrorState(card, prog, rootPath);
        _perchActiveJobId = null;
        if (_perchActivePollTimer) { clearInterval(_perchActivePollTimer); _perchActivePollTimer = null; }
        _perchSetButtonsDisabled(false);
      } else {
        _perchUpdateUploadCard(card, prog);
      }
    }

    async function kickShareJob(rootPath, excludedSceneIds, skipRejected, resumeOpts) {
      let res;
      const idempotencyKey = (resumeOpts && resumeOpts.idempotencyKey) || null;
      try {
        res = await window.pywebview.api.share_with_perch(
          rootPath,
          excludedSceneIds || [],
          skipRejected !== false,
          idempotencyKey,
        );
      } catch (e) {
        showToast('Failed to start upload: ' + e.message, 6000);
        return;
      }
      if (!res || !res.success) {
        if (res && res.error === 'already_running') {
          showToast('A Perch upload is already running.', 5000);
        } else if (res && (res.error === 'not_signed_in' || res.needSignIn)) {
          showToast('Sign in to Perch first (use the account button).', 6000);
        } else {
          showToast('Perch: ' + ((res && res.error) || 'Unknown error'), 6000);
        }
        return;
      }
      const jobId = String(res.job_id);
      _perchActiveJobId = jobId;
      _perchSetButtonsDisabled(true);
      const card = _perchRenderUploadCard(jobId);
      // Initial poll fast for snappy first-frame, then slow down.
      let interval = 500;
      _perchActivePollTimer = setInterval(() => {
        _perchPollProgress(jobId, card, rootPath);
      }, interval);
      // Bump the interval after 5 seconds — big uploads don't need 2Hz forever.
      setTimeout(() => {
        if (_perchActivePollTimer) {
          clearInterval(_perchActivePollTimer);
          _perchActivePollTimer = setInterval(() => {
            _perchPollProgress(jobId, card, rootPath);
          }, 1000);
        }
      }, 5000);
    }

    function _perchUpdateRejectToggleHint() {
      const hint = document.getElementById('perchRejectToggleHint');
      const label = document.getElementById('perchRejectToggleLabel');
      if (!hint || !label) return;
      const pre = _perchDlgState.preflight;
      if (!pre) { hint.textContent = ''; return; }
      // The preflight payload reports how many rows the *current* skip_rejected
      // setting omitted. When the toggle is ON we got the skipped count back.
      // When OFF, rejectedSkipped is 0 — so we'd need to know the actual count
      // some other way. Cheap solution: stash the last "ON" count and reuse.
      if (_perchDlgState.skipRejected) {
        const n = Number(pre.rejectedSkipped || 0);
        _perchDlgState._lastSkipCount = n;
        hint.textContent = n > 0
          ? `(${n.toLocaleString()} rejected photo${n === 1 ? '' : 's'} will be skipped)`
          : '(none rejected)';
      } else {
        const n = Number(_perchDlgState._lastSkipCount || pre.rejectedSkipped || 0);
        hint.textContent = n > 0
          ? `(${n.toLocaleString()} rejected photo${n === 1 ? '' : 's'} will be uploaded)`
          : '';
      }
    }

    async function _perchOnRejectToggleChange() {
      const cb = document.getElementById('perchRejectToggleCb');
      if (!cb) return;
      _perchDlgState.skipRejected = !!cb.checked;
      const rootPath = _perchDlgState.rootPath;
      if (!rootPath) return;
      // Re-run preflight with the new flag — totals + scene list update.
      let pre;
      try {
        pre = await window.pywebview.api.preflight_perch_upload(rootPath, _perchDlgState.skipRejected);
      } catch {
        return;
      }
      if (!pre || !pre.ok) return;
      _perchDlgState.preflight = pre;
      // Drop deselected entries that no longer exist in the new scene list.
      const liveIds = new Set((pre.scenes || []).map(s => String(s.sceneId)));
      for (const sid of [..._perchDlgState.deselected]) {
        if (!liveIds.has(sid)) _perchDlgState.deselected.delete(sid);
      }
      _perchRenderTimeline();
      _perchUpdateRejectToggleHint();
      _perchUpdateDialogTotals();
    }

    /** Phase 2: ask the bridge whether a partial upload exists for this folder.
     *  On "resumable" → populate the resume banner and show it. On "complete"
     *  → quietly clean up the manifest by treating it as a normal fresh open
     *  (the bridge has already converted to no-op state on the server). On any
     *  error → leave the dialog as a normal fresh upload. */
    async function _perchCheckResumable(rootPath) {
      let res;
      try { res = await window.pywebview.api.detect_resumable_upload(rootPath); }
      catch { return; }
      if (!res || !res.present) return;
      const status = res.status;
      const banner = document.getElementById('perchResumableBanner');
      const sub = document.getElementById('perchResumableSubtitle');
      if (status === 'resumable') {
        _perchDlgState.resumable = {
          perchId: String(res.perch_id || ''),
          idempotencyKey: String(res.idempotency_key || ''),
          total: Number(res.total || 0),
          committed: Number(res.committed || 0),
          pending: Number(res.pending || 0),
          title: String(res.title || ''),
        };
        if (banner) {
          if (sub) {
            const r = _perchDlgState.resumable;
            sub.textContent = `${r.committed.toLocaleString()} of ${r.total.toLocaleString()} files already uploaded — ${r.pending.toLocaleString()} remaining.`;
          }
          banner.classList.remove('hidden');
        }
      } else if (status === 'complete') {
        // Server says everything's already there — let the user know but
        // don't block the dialog. (The link.json will be missing because the
        // commit/finalize step didn't write it; user can finish via Resume,
        // which is a no-op upload that just writes the link.)
        _perchDlgState.resumable = {
          perchId: String(res.perch_id || ''),
          idempotencyKey: '',
          total: Number(res.total || 0),
          committed: Number(res.committed || 0),
          pending: 0,
          title: '',
        };
        if (banner) {
          if (sub) sub.textContent = `Previous upload appears complete (${res.committed} of ${res.total} files). Click Resume to finalize.`;
          banner.classList.remove('hidden');
        }
      }
      // For 'unauthorized' / 'forbidden' / 'unreachable' / 'deleted' — silent.
    }

    async function _perchOpenDialog(rootPath, verifyResult) {
      const dlg = document.getElementById('perchUploadDlg');
      if (!dlg) {
        showToast('Perch dialog not available', 4000);
        return;
      }
      _perchDlgState.rootPath = rootPath;
      _perchDlgState.preflight = null;
      _perchDlgState.deselected = new Set();
      _perchDlgState.skipRejected = true;  // dialog opens with skip-rejected ON
      _perchDlgState.resumable = null;
      // Hide the linked-state takeover and resumable banner; linked-state is
      // entered via openPerchDialog() (not this path).
      const linkedView = document.getElementById('perchDlgLinkedView');
      if (linkedView) linkedView.classList.add('hidden');
      const resumableBanner = document.getElementById('perchResumableBanner');
      if (resumableBanner) resumableBanner.classList.add('hidden');
      // Show a starting state; preflight populates real numbers.
      const loading = document.getElementById('perchDlgLoading');
      const signedOut = document.getElementById('perchDlgSignedOut');
      const signedIn = document.getElementById('perchDlgSignedIn');
      const submit = document.getElementById('perchUploadSubmitBtn');
      const sigIn = document.getElementById('perchUploadSignInBtn');
      const ready = document.getElementById('perchDlgReadyLine');
      const rejectCb = document.getElementById('perchRejectToggleCb');
      if (rejectCb) rejectCb.checked = true;
      if (loading) loading.classList.remove('hidden');
      if (signedOut) signedOut.classList.add('hidden');
      if (signedIn) signedIn.classList.add('hidden');
      if (submit) { submit.classList.add('hidden'); submit.disabled = true; }
      if (sigIn) sigIn.classList.add('hidden');
      if (ready) ready.textContent = '';
      try { dlg.showModal(); } catch { return; }

      let preflight;
      try {
        preflight = await window.pywebview.api.preflight_perch_upload(rootPath, true);
      } catch (e) {
        if (loading) loading.classList.add('hidden');
        if (signedOut) signedOut.classList.remove('hidden');
        if (ready) ready.textContent = 'Could not inspect folder: ' + (e && e.message ? e.message : String(e));
        return;
      }
      if (loading) loading.classList.add('hidden');
      if (!preflight || !preflight.ok) {
        showToast('Perch: ' + ((preflight && preflight.error) || 'Could not inspect folder'), 6000);
        try { dlg.close(); } catch {}
        return;
      }
      _perchDlgState.preflight = preflight;
      if (preflight.signedIn) {
        if (signedIn) signedIn.classList.remove('hidden');
        if (submit) submit.classList.remove('hidden');
        _perchRenderTimeline();
        _perchUpdateRejectToggleHint();
        _perchUpdateDialogTotals();
        _perchLoadAccountAndUsage();
        // Phase 2: detect a resumable upload and surface a banner if found.
        // Fire-and-forget; user can interact with the dialog meanwhile.
        _perchCheckResumable(rootPath);
      } else {
        if (signedOut) signedOut.classList.remove('hidden');
        if (sigIn) sigIn.classList.remove('hidden');
        if (ready) {
          const photos = Number(preflight.imageCount || 0);
          const scenes = Number(preflight.sceneCount || 0);
          if (photos > 0) {
            ready.textContent = `Ready to share: ${scenes.toLocaleString()} scene${scenes === 1 ? '' : 's'}, ${photos.toLocaleString()} photo${photos === 1 ? '' : 's'}, ${formatPerchBytes(preflight.totalBytes)}.`;
          }
        }
      }
    }

    function _perchWirePerchDialogOnce() {
      if (_perchWirePerchDialogOnce._done) return;
      _perchWirePerchDialogOnce._done = true;
      const dlg = document.getElementById('perchUploadDlg');
      if (!dlg) return;
      const cancelBtn = document.getElementById('perchUploadCancelBtn');
      const closeBtn = document.getElementById('perchUploadDlgClose');
      const submit = document.getElementById('perchUploadSubmitBtn');
      const sigIn = document.getElementById('perchUploadSignInBtn');
      const cullBtn = document.getElementById('perchOpenCullingBtn');
      const selAll = document.getElementById('perchSelectAllBtn');
      const deselAll = document.getElementById('perchDeselectAllBtn');
      if (cancelBtn) cancelBtn.addEventListener('click', () => _perchClosePerchDialog());
      if (closeBtn) closeBtn.addEventListener('click', () => _perchClosePerchDialog());
      if (submit) submit.addEventListener('click', () => {
        const root = _perchDlgState.rootPath;
        const excluded = [..._perchDlgState.deselected];
        const skipRejected = !!_perchDlgState.skipRejected;
        _perchClosePerchDialog();
        kickShareJob(root, excluded, skipRejected);
      });
      const rejectCb = document.getElementById('perchRejectToggleCb');
      if (rejectCb) rejectCb.addEventListener('change', _perchOnRejectToggleChange);
      // Re-link an already-shared folder to an existing perch (no re-upload).
      const relinkOpen = document.getElementById('perchRelinkOpenBtn');
      if (relinkOpen) relinkOpen.addEventListener('click', () => _perchOpenRelinkPicker());
      const relinkClose = document.getElementById('perchRelinkClose');
      if (relinkClose) relinkClose.addEventListener('click', () => {
        const d = document.getElementById('perchRelinkDlg');
        if (d && d.open) { try { d.close(); } catch {} }
      });
      const relinkList = document.getElementById('perchRelinkList');
      if (relinkList) relinkList.addEventListener('click', (e) => {
        const row = e.target.closest('.perch-relink-row');
        if (row) _perchDoRelink(row.getAttribute('data-perch-id'));
      });
      // Linked-state takeover buttons.
      const linkedOpen = document.getElementById('perchLinkedOpenBtn');
      const linkedUnlink = document.getElementById('perchLinkedUnlinkBtn');
      const linkedShareCopy = document.getElementById('perchLinkedShareCopy');
      if (linkedOpen) linkedOpen.addEventListener('click', async () => {
        const url = linkedOpen.dataset.perchUrl;
        if (url) { try { await window.pywebview.api.open_perch_url(url); } catch {} }
      });
      if (linkedUnlink) linkedUnlink.addEventListener('click', async () => {
        const root = linkedUnlink.dataset.folderPath || _perchDlgState.rootPath;
        if (!root) return;
        const ok = window.confirm(
          'Unlink this folder from Perch?\n\n' +
          'The perch on projectkestrel.org will NOT be deleted. To remove it from the web, delete it there. ' +
          'This only removes the local connection so you can upload a fresh perch from this folder.'
        );
        if (!ok) return;
        let res = null;
        try { res = await window.pywebview.api.delete_perch_link(root); } catch (e) {
          showToast('Could not unlink: ' + (e?.message || String(e)), 5000);
          return;
        }
        if (!res || !res.success) {
          showToast('Could not unlink: ' + (res?.error || 'unknown error'), 5000);
          return;
        }
        showToast('Perch link removed for this folder.', 3000);
        _perchRefreshHeaderButton(root);
        await _perchSwapLinkedToUploadForm(root);
      });
      if (linkedShareCopy) linkedShareCopy.addEventListener('click', async () => {
        const input = document.getElementById('perchLinkedShareUrl');
        const url = input?.value;
        if (!url) return;
        try {
          await navigator.clipboard.writeText(url);
          showToast('Share URL copied to clipboard.', 2500);
        } catch {
          // Fallback: select the input so user can Ctrl+C
          if (input) { input.focus(); input.select(); }
        }
      });
      // H7 tag-sync: banner opens the preview; dialog buttons confirm/cancel.
      const tagSyncBtn = document.getElementById('perchTagSyncBtn');
      if (tagSyncBtn) tagSyncBtn.addEventListener('click', _perchOpenTagSyncPreview);
      const tagSyncClose = document.getElementById('perchTagSyncDlgClose');
      const tagSyncCancel = document.getElementById('perchTagSyncCancelBtn');
      const tagSyncConfirm = document.getElementById('perchTagSyncConfirmBtn');
      if (tagSyncClose) tagSyncClose.addEventListener('click', _perchCloseTagSyncDlg);
      if (tagSyncCancel) tagSyncCancel.addEventListener('click', _perchCloseTagSyncDlg);
      if (tagSyncConfirm) tagSyncConfirm.addEventListener('click', _perchDoTagSync);
      // Phase 2 resumable-upload banner buttons.
      const resumeBtn = document.getElementById('perchResumableResumeBtn');
      const startOverBtn = document.getElementById('perchResumableStartOverBtn');
      if (resumeBtn) resumeBtn.addEventListener('click', () => {
        const root = _perchDlgState.rootPath;
        const r = _perchDlgState.resumable;
        if (!root || !r) return;
        // Resume kicks off a new share job using the existing perch + key.
        // Skip-rejected / scene-deselect controls don't apply mid-resume —
        // we re-send the same payload the original session computed.
        _perchClosePerchDialog();
        kickShareJob(root, [], _perchDlgState.skipRejected, {
          existingPerchId: r.perchId,
          idempotencyKey: r.idempotencyKey,
        });
      });
      if (startOverBtn) startOverBtn.addEventListener('click', async () => {
        const root = _perchDlgState.rootPath;
        if (!root) return;
        const ok = window.confirm(
          'Discard the partial upload?\n\nThis deletes the unfinished perch on Perch and removes the local progress file. The folder stays unchanged.'
        );
        if (!ok) return;
        try { await window.pywebview.api.discard_resumable_upload(root); } catch {}
        _perchDlgState.resumable = null;
        const banner = document.getElementById('perchResumableBanner');
        if (banner) banner.classList.add('hidden');
      });
      if (sigIn) sigIn.addEventListener('click', async () => {
        try {
          if (window.pywebview?.api?.start_oauth_sign_in) {
            await window.pywebview.api.start_oauth_sign_in();
            if (typeof showToast === 'function') {
              showToast('Sign-in in progress — complete sign-in in your browser, then return here.', 8000);
            }
          }
        } catch {}
        _perchClosePerchDialog();
      });
      if (cullBtn) cullBtn.addEventListener('click', () => {
        const root = _perchDlgState.rootPath;
        _perchClosePerchDialog();
        if (root) openCullingAssistant(root);
      });
      const adjBtn = document.getElementById('perchAdjustTimeBtn');
      if (adjBtn) adjBtn.addEventListener('click', () => {
        const root = _perchDlgState.rootPath;
        _perchClosePerchDialog();
        if (root) showAdjustCaptureTimeDialog(root);
      });
      if (selAll) selAll.addEventListener('click', (e) => {
        e.preventDefault();
        _perchDlgState.deselected.clear();
        _perchRenderTimeline();
        _perchUpdateDialogTotals();
      });
      if (deselAll) deselAll.addEventListener('click', (e) => {
        e.preventDefault();
        const pre = _perchDlgState.preflight;
        if (pre) {
          for (const s of pre.scenes || []) _perchDlgState.deselected.add(String(s.sceneId));
        }
        _perchRenderTimeline();
        _perchUpdateDialogTotals();
      });
      // Uploads-panel collapse toggle
      const panelHeader = document.getElementById('perchUploadsHeader');
      const panel = document.getElementById('perchUploadsPanel');
      if (panelHeader && panel) {
        panelHeader.addEventListener('click', () => {
          panel.classList.toggle('collapsed');
          const t = document.getElementById('perchUploadsToggle');
          if (t) t.classList.toggle('open');
          _perchRepositionUploadsPanel();
        });
      }
      _perchInstallPanelObservers();
    }

    function _perchRelativeDate(ms) {
      const now = Date.now();
      const diff = Math.max(0, now - Number(ms));
      const day = 24 * 60 * 60 * 1000;
      if (diff < 60 * 1000) return 'just now';
      if (diff < 60 * 60 * 1000) return Math.floor(diff / 60000) + ' min ago';
      if (diff < day) return Math.floor(diff / 3600000) + 'h ago';
      if (diff < 30 * day) return Math.floor(diff / day) + 'd ago';
      try { return new Date(Number(ms)).toLocaleDateString(); } catch { return ''; }
    }

    // Friendly labels for the Perch Worker's visibility enum.
    const _PERCH_VISIBILITY_LABELS = {
      private: 'Private (only you)',
      unlisted: 'Unlisted (anyone with link)',
      restricted: 'Restricted (named viewers)',
      public: 'Public',
      draft: 'Draft (not published)',
    };
    const _PERCH_VISIBILITY_ICONS = {
      private: '\u{1F512}', unlisted: '\u{1F517}', restricted: '\u{1F465}',
      public: '\u{1F310}', draft: '\u{1F4DD}',
    };
    const _PERCH_COMMENTS_LABELS = {
      everyone: 'Everyone',
      invited: 'Invited only',
      off: 'Off',
    };

    function _perchClearLinkedView() {
      const ids = [
        'perchLinkedTitle', 'perchLinkedSub',
        'perchStatusVisibility', 'perchStatusBytes',
        'perchStatusPhotos', 'perchStatusComments',
      ];
      for (const id of ids) {
        const el = document.getElementById(id);
        if (el) el.textContent = '—';
      }
      const shareRow = document.getElementById('perchLinkedShareRow');
      if (shareRow) shareRow.classList.add('hidden');
      const warn = document.getElementById('perchLinkedWarn');
      if (warn) { warn.classList.add('hidden'); warn.textContent = ''; }
      const url = document.getElementById('perchLinkedShareUrl');
      if (url) url.value = '';
      // H7 tag-sync banner starts hidden; the diff probe reveals it if needed.
      const tagBanner = document.getElementById('perchTagSyncBanner');
      if (tagBanner) tagBanner.classList.add('hidden');
      _perchDlgState.tagDiff = null;
    }

    /** Flip every .folder-perch-btn for this path straight to the linked
     *  ("On Perch") state from a known perch URL — no disk read. Used the moment
     *  a share job reports phase 'done' (server-confirmed), since the bridge
     *  writes perch_link.json *after* emitting 'done', so a read_perch_link here
     *  could race the file write. The disk becomes the source of truth again on
     *  the next natural refresh (e.g. reopening the Perch dialog). */
    function _perchMarkFolderOnPerch(rootPath, perchUrl) {
      const sel = `.folder-perch-btn[data-folder-path="${cssEscape(rootPath)}"]`;
      document.querySelectorAll(sel).forEach(btn => {
        const lbl = btn.querySelector('.folder-perch-btn-label');
        btn.classList.add('is-linked');
        if (lbl) lbl.textContent = 'On Perch';
        if (perchUrl) btn.dataset.perchUrl = String(perchUrl);
        btn.title = 'This folder is published to Perch (click to manage)';
      });
      // Reflect the new Perch link in the sidebar folder tree immediately. The
      // 🪶 feather marker is data-driven off node.has_perch_link, which is only
      // re-scanned from disk on folder load / app restart — so without this the
      // feather wouldn't appear until a restart. Set the flag in-memory and
      // repaint the tree now.
      try {
        if (typeof findNodeInAnyRoot === 'function') {
          const node = findNodeInAnyRoot(rootPath);
          if (node && !node.has_perch_link) {
            node.has_perch_link = true;
            if (typeof renderFolderTree === 'function') renderFolderTree();
          }
        }
      } catch (e) { /* tree refresh is best-effort */ }
    }

    // ── Re-link: associate this folder with an EXISTING perch (no re-upload) ──
    function _perchEsc(s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }
    function _perchFmtBytes(n) {
      n = Number(n) || 0;
      if (n < 1024) return n + ' B';
      const u = ['KB', 'MB', 'GB', 'TB'];
      let v = n / 1024, i = 0;
      while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
      return v.toFixed(v >= 100 ? 0 : 1) + ' ' + u[i];
    }

    /** Open the picker listing the user's existing perches to re-link to. */
    async function _perchOpenRelinkPicker() {
      const dlg = document.getElementById('perchRelinkDlg');
      const list = document.getElementById('perchRelinkList');
      if (!dlg || !list) return;
      list.innerHTML = '<div class="perch-relink-empty">Loading your perches…</div>';
      try { dlg.showModal(); } catch { try { dlg.show(); } catch {} }
      let perches = [];
      try {
        const r = await window.pywebview?.api?.get_perch_list?.(200);
        if (r && r.success && Array.isArray(r.perches)) {
          perches = r.perches;
        } else {
          list.innerHTML = '<div class="perch-relink-empty">Could not load your perches'
            + (r && r.error ? ' (' + _perchEsc(r.error) + ')' : '') + '.</div>';
          return;
        }
      } catch {
        list.innerHTML = '<div class="perch-relink-empty">Could not load your perches.</div>';
        return;
      }
      if (!perches.length) {
        list.innerHTML = '<div class="perch-relink-empty">You don’t have any perches yet — upload one instead.</div>';
        return;
      }
      list.innerHTML = perches.map((p) => {
        const id = _perchEsc(p.id || '');
        const title = _perchEsc(p.title || '(untitled)');
        const imgs = (Number(p.imageCount) || 0).toLocaleString();
        const bytes = _perchFmtBytes(p.actualBytes);
        const state = _perchEsc(String(p.uploadState || p.status || ''));
        return '<button type="button" class="perch-relink-row" data-perch-id="' + id + '">'
          + '<span class="perch-relink-row-title">' + title + '</span>'
          + '<span class="perch-relink-row-meta">' + imgs + ' photos · ' + bytes
          + (state ? ' · ' + state : '') + '</span>'
          + '</button>';
      }).join('');
    }

    /** Write the local link for the chosen perch, then show the linked view. */
    async function _perchDoRelink(perchId) {
      const root = _perchDlgState.rootPath;
      if (!root || !perchId) return;
      let res = null;
      try {
        res = await window.pywebview?.api?.relink_perch?.(root, perchId);
      } catch (e) {
        showToast('Re-link failed: ' + (e?.message || String(e)), 5000);
        return;
      }
      if (!res || !res.success) {
        const map = {
          not_found: 'That perch no longer exists on Perch.',
          forbidden: 'That perch belongs to a different account.',
          unauthorized: 'Please sign in to Perch again.',
          no_auth: 'Please sign in to Perch again.',
        };
        showToast('Re-link failed: ' + (map[res?.error] || res?.error || 'unknown error'), 5500);
        return;
      }
      const dlg = document.getElementById('perchRelinkDlg');
      if (dlg && dlg.open) { try { dlg.close(); } catch {} }
      showToast('Folder re-linked to Perch.', 3000);
      _perchMarkFolderOnPerch(root, (res.link && res.link.perch_url) || '');
      // Swap the open share dialog to the linked takeover; probe live status.
      if (res.link) {
        _perchOpenLinkedView(root, res.link);
        const pid = String(res.link.perch_id || '').trim();
        if (pid) {
          (async () => {
            try {
              const s = await window.pywebview.api.get_perch_status(pid);
              if (s && s.ok) _perchPopulateLinkedView(s.status);
            } catch { /* advisory */ }
          })();
        }
      }
    }

    /** Refresh every .folder-perch-btn for this path to its current linked state.
     *  Re-runs read_perch_link so the UI mirrors whatever the bridge says. */
    async function _perchRefreshHeaderButton(rootPath) {
      const sel = `.folder-perch-btn[data-folder-path="${cssEscape(rootPath)}"]`;
      const btns = document.querySelectorAll(sel);
      if (!btns.length) return;
      let linked = false;
      let perchUrl = '';
      try {
        const res = await window.pywebview?.api?.read_perch_link?.(rootPath);
        if (res && res.present && res.link) {
          linked = true;
          perchUrl = String(res.link.perch_url || '');
        }
      } catch {}
      btns.forEach(btn => {
        const lbl = btn.querySelector('.folder-perch-btn-label');
        if (linked) {
          btn.classList.add('is-linked');
          if (lbl) lbl.textContent = 'On Perch';
          btn.dataset.perchUrl = perchUrl;
          btn.title = 'This folder is published to Perch (click to manage)';
        } else {
          btn.classList.remove('is-linked');
          if (lbl) lbl.textContent = 'Share with Perch';
          delete btn.dataset.perchUrl;
          btn.title = 'Share this folder to Perch (or manage existing perch)';
        }
      });
    }

    /** Show the linked-state takeover. Renders local data immediately;
     *  caller fires get_perch_status separately to fill in live fields. */
    function _perchOpenLinkedView(rootPath, link) {
      _perchDlgState.rootPath = rootPath;
      _perchDlgState.preflight = null;
      _perchClearLinkedView();
      // Hide every other dialog section.
      const ids = ['perchDlgLoading', 'perchDlgSignedOut', 'perchDlgSignedIn', 'perchResumableBanner'];
      for (const id of ids) {
        const el = document.getElementById(id);
        if (el) el.classList.add('hidden');
      }
      // Hide the footer's submit/sign-in buttons; linked-view has its own actions.
      const submit = document.getElementById('perchUploadSubmitBtn');
      const sigIn = document.getElementById('perchUploadSignInBtn');
      if (submit) { submit.classList.add('hidden'); submit.disabled = true; }
      if (sigIn) sigIn.classList.add('hidden');
      // Hero: title + relative-date sub-line from local perch_link.json.
      const title = String(link.title || 'Untitled perch');
      const ms = Number(link.uploaded_at_ms || 0);
      const rel = ms > 0 ? _perchRelativeDate(ms) : '';
      const titleEl = document.getElementById('perchLinkedTitle');
      const subEl = document.getElementById('perchLinkedSub');
      if (titleEl) titleEl.textContent = title;
      if (subEl) subEl.textContent = rel ? `Published ${rel}` : 'Published to Perch';
      // Photos cell: fallback to local cached count until live status arrives.
      const cachedPhotos = Number(link.image_count || link.asset_count || 0);
      if (cachedPhotos > 0) {
        const p = document.getElementById('perchStatusPhotos');
        if (p) p.textContent = cachedPhotos.toLocaleString() + ' photos';
      }
      // Stash url + id on buttons so click handlers can use them.
      const openBtn = document.getElementById('perchLinkedOpenBtn');
      const unlinkBtn = document.getElementById('perchLinkedUnlinkBtn');
      if (openBtn) openBtn.dataset.perchUrl = String(link.perch_url || '');
      if (unlinkBtn) unlinkBtn.dataset.folderPath = rootPath;
      // Reveal the linked view.
      const linkedView = document.getElementById('perchDlgLinkedView');
      if (linkedView) linkedView.classList.remove('hidden');
      const dlg = document.getElementById('perchUploadDlg');
      if (dlg && !dlg.open) { try { dlg.showModal(); } catch {} }
    }

    /** Fill in live status fields fetched from Perch Worker. */
    function _perchPopulateLinkedView(status) {
      if (!status || typeof status !== 'object') return;
      const visEl = document.getElementById('perchStatusVisibility');
      if (visEl) {
        const v = String(status.visibility || status.status || '');
        const label = _PERCH_VISIBILITY_LABELS[v] || (v ? v[0].toUpperCase() + v.slice(1) : '—');
        const icon = _PERCH_VISIBILITY_ICONS[v] || '';
        visEl.textContent = icon ? `${icon} ${label}` : label;
      }
      const bytesEl = document.getElementById('perchStatusBytes');
      if (bytesEl) {
        bytesEl.textContent = (status.actualBytes != null)
          ? formatPerchBytes(Number(status.actualBytes))
          : '—';
      }
      const photosEl = document.getElementById('perchStatusPhotos');
      if (photosEl) {
        const imgCount = Number(status.imageCount || 0);
        const assetCount = Number(status.assetCount || 0);
        if (imgCount > 0) {
          let txt = `${imgCount.toLocaleString()} photo${imgCount === 1 ? '' : 's'}`;
          if (assetCount > imgCount) {
            txt += ` (${assetCount.toLocaleString()} assets)`;
          }
          photosEl.textContent = txt;
        }
      }
      const commentsEl = document.getElementById('perchStatusComments');
      if (commentsEl) {
        const c = String(status.commentsPermission || '');
        commentsEl.textContent = _PERCH_COMMENTS_LABELS[c] || (c || '—');
      }
      // Share URL — only visible for shareable visibilities with a public URL.
      const shareRow = document.getElementById('perchLinkedShareRow');
      const shareInput = document.getElementById('perchLinkedShareUrl');
      const isShareable = status.visibility === 'public' || status.visibility === 'unlisted';
      const publicUrl = String(status.publicUrl || '');
      if (shareRow && shareInput && isShareable && publicUrl) {
        shareInput.value = publicUrl;
        shareRow.classList.remove('hidden');
      } else if (shareRow) {
        shareRow.classList.add('hidden');
      }
    }

    function _perchShowLinkedWarning(errorCode) {
      const warn = document.getElementById('perchLinkedWarn');
      if (!warn) return;
      const msgs = {
        unauthorized: "Couldn't verify with Perch — sign in to refresh live status.",
        forbidden: "This perch belongs to a different Perch account.",
        unreachable: "Couldn't reach Perch — showing cached info.",
        no_auth: "Sign in to Perch to see live status.",
      };
      warn.textContent = msgs[errorCode] || `Couldn't refresh status (${errorCode}).`;
      warn.classList.remove('hidden');
    }

    // ── H7 tag-sync: push local species/family corrections to a linked perch ──

    /** Order-sensitive equality for two tag lists (used to highlight which of
     *  species / family actually changed in the preview). */
    function _perchArrEq(a, b) {
      const aa = Array.isArray(a) ? a : [];
      const bb = Array.isArray(b) ? b : [];
      if (aa.length !== bb.length) return false;
      for (let i = 0; i < aa.length; i++) if (String(aa[i]) !== String(bb[i])) return false;
      return true;
    }

    /** Render a tag list as text ("(none)" when empty). */
    function _perchFmtTags(arr) {
      const a = Array.isArray(arr) ? arr.filter((x) => String(x).trim()) : [];
      return a.length ? a.join(', ') : '(none)';
    }

    /** Build one "Label: old → new" diff line (textContent only — tags are
     *  user-controlled strings, never interpolate into innerHTML). */
    function _perchTagDiffLine(label, oldArr, newArr) {
      const line = document.createElement('div');
      line.className = 'perch-tag-sync-diff';
      const lab = document.createElement('span');
      lab.className = 'perch-tag-sync-diff-label';
      lab.textContent = label + ': ';
      const oldEl = document.createElement('span');
      oldEl.className = 'perch-tag-sync-old';
      oldEl.textContent = _perchFmtTags(oldArr);
      const arrow = document.createElement('span');
      arrow.className = 'perch-tag-sync-arrow';
      arrow.textContent = ' → ';
      const newEl = document.createElement('span');
      newEl.className = 'perch-tag-sync-new';
      newEl.textContent = _perchFmtTags(newArr);
      line.append(lab, oldEl, arrow, newEl);
      return line;
    }

    /** Render the per-scene preview list inside the confirm dialog. */
    function _perchRenderTagSyncList(changes) {
      const list = document.getElementById('perchTagSyncList');
      if (!list) return;
      list.innerHTML = '';
      for (const c of changes) {
        const row = document.createElement('div');
        row.className = 'perch-tag-sync-row';
        const title = document.createElement('div');
        title.className = 'perch-tag-sync-row-title';
        title.textContent = c.title || ('Scene ' + c.kestrelSceneId);
        row.appendChild(title);
        if (!_perchArrEq(c.species, c.remoteSpecies)) {
          row.appendChild(_perchTagDiffLine('Species', c.remoteSpecies, c.species));
        }
        if (!_perchArrEq(c.family, c.remoteFamily)) {
          row.appendChild(_perchTagDiffLine('Family', c.remoteFamily, c.family));
        }
        list.appendChild(row);
      }
    }

    /** Probe the linked folder for tag changes vs the live perch; reveal the
     *  accent banner when the changeset is non-empty. Fire-and-forget; guards
     *  against a stale result after the dialog moved to another folder. */
    async function _perchProbeTagDiff(rootPath) {
      const banner = document.getElementById('perchTagSyncBanner');
      if (banner) banner.classList.add('hidden');
      _perchDlgState.tagDiff = null;
      if (!window.pywebview?.api?.compute_perch_tag_diff) return;
      let res = null;
      try { res = await window.pywebview.api.compute_perch_tag_diff(rootPath); } catch { return; }
      if (_perchDlgState.rootPath !== rootPath) return;  // dialog moved on
      if (!res || !res.ok) return;
      const changes = Array.isArray(res.changes) ? res.changes : [];
      if (changes.length === 0) return;
      _perchDlgState.tagDiff = { perchId: res.perch_id, changes };
      const n = changes.length;
      const titleEl = document.getElementById('perchTagSyncTitle');
      if (titleEl) {
        titleEl.textContent = `${n} scene${n === 1 ? '' : 's'} ${n === 1 ? 'has' : 'have'} tag changes since you published`;
      }
      if (banner) banner.classList.remove('hidden');
    }

    /** Open the per-scene preview confirm dialog for the current changeset. */
    function _perchOpenTagSyncPreview() {
      const diff = _perchDlgState.tagDiff;
      if (!diff || !Array.isArray(diff.changes) || diff.changes.length === 0) return;
      _perchRenderTagSyncList(diff.changes);
      const n = diff.changes.length;
      const confirmBtn = document.getElementById('perchTagSyncConfirmBtn');
      if (confirmBtn) {
        confirmBtn.disabled = false;
        confirmBtn.textContent = `Sync ${n} scene${n === 1 ? '' : 's'}`;
      }
      const dlg = document.getElementById('perchTagSyncDlg');
      if (dlg && !dlg.open) { try { dlg.showModal(); } catch {} }
    }

    function _perchCloseTagSyncDlg() {
      const dlg = document.getElementById('perchTagSyncDlg');
      if (dlg && dlg.open) { try { dlg.close(); } catch {} }
    }

    /** Confirm handler: push the changeset, then refresh the banner. */
    async function _perchDoTagSync() {
      const root = _perchDlgState.rootPath;
      if (!root) return;
      const confirmBtn = document.getElementById('perchTagSyncConfirmBtn');
      if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = 'Syncing…'; }
      let res = null;
      try { res = await window.pywebview.api.sync_perch_tags(root); }
      catch (e) { res = { ok: false, error: (e && e.message) || String(e) }; }
      if (res && res.ok) {
        _perchCloseTagSyncDlg();
        const n = Number(res.updated || 0);
        let msg = n > 0
          ? `Synced ${n} scene${n === 1 ? '' : 's'} to Perch.`
          : 'Tags are already up to date on Perch.';
        if (Array.isArray(res.skipped) && res.skipped.length) {
          const s = res.skipped.length;
          msg += ` ${s} scene${s === 1 ? '' : 's'} not on the perch ${s === 1 ? 'was' : 'were'} skipped.`;
        }
        showToast(msg, 4500);
        // Re-probe so the banner reflects the new (empty) diff.
        await _perchProbeTagDiff(root);
      } else {
        const err = (res && res.error) || 'unknown';
        const friendly = {
          no_auth: 'Sign in to Perch to sync tag changes.',
          unauthorized: 'Sign in to Perch to sync tag changes.',
          forbidden: 'This perch belongs to a different Perch account.',
          not_found: 'This perch no longer exists on Perch.',
          unreachable: "Couldn't reach Perch — check your connection and try again.",
          not_linked: 'This folder is no longer linked to a perch.',
        }[err] || ('Sync failed: ' + err);
        showToast(friendly, 5500);
        if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = 'Retry sync'; }
      }
    }

    /** After Unlink (or on detected server-side delete): hide the linked view,
     *  fall through to the upload form by running _perchOpenDialog. */
    async function _perchSwapLinkedToUploadForm(rootPath) {
      const linkedView = document.getElementById('perchDlgLinkedView');
      if (linkedView) linkedView.classList.add('hidden');
      // _perchOpenDialog will reset state, run preflight, and reveal the
      // signed-in (or signed-out) body. We pass null since there's no
      // verifyResult to react to.
      await _perchOpenDialog(rootPath, null);
    }

    /** Unified entry point for the Perch button. Branches on link state:
     *  linked    -> linked-state takeover view (live status probed async)
     *  unlinked  -> upload form (existing _perchOpenDialog flow)
     *
     *  Also handles the upload-already-running gate + dirty-CSV save prompt
     *  that the previous shareWithPerchFolder used to handle. */
    async function openPerchDialog(rootPath) {
      if (!window.pywebview?.api) {
        showToast('Share with Perch requires desktop mode', 4000);
        return;
      }
      if (_perchActiveJobId) {
        showToast('A Perch upload is already running.', 4000);
        return;
      }
      if (typeof dirty !== 'undefined' && dirty) {
        const userChoice = await showCullingAssistantPrompt();
        if (userChoice === 'cancel') return;
        if (userChoice === 'save') await saveCsv();
      }
      _perchWirePerchDialogOnce();

      // Local link probe first (cheap, just reads JSON from disk).
      let linkRes = null;
      try { linkRes = await window.pywebview.api.read_perch_link(rootPath); } catch {}
      if (linkRes && linkRes.present && linkRes.link) {
        // Show the takeover immediately with cached data; probe live status async.
        _perchOpenLinkedView(rootPath, linkRes.link);
        const perchId = String(linkRes.link.perch_id || '').trim();
        if (!perchId) return;
        // H7: probe for local tag changes vs the perch (independent of the
        // status probe below); reveals the "Sync" banner when non-empty.
        _perchProbeTagDiff(rootPath);
        (async () => {
          let res = null;
          try { res = await window.pywebview.api.get_perch_status(perchId); } catch (e) {
            _perchShowLinkedWarning('unreachable');
            return;
          }
          if (res && res.ok) {
            _perchPopulateLinkedView(res.status);
          } else if (res && res.error === 'not_found') {
            // Server says the perch is gone — clear local link + fall through.
            try { await window.pywebview.api.delete_perch_link(rootPath); } catch {}
            _perchRefreshHeaderButton(rootPath);
            showToast('This perch was deleted on Perch — the folder has been unlinked locally.', 5500);
            await _perchSwapLinkedToUploadForm(rootPath);
          } else {
            _perchShowLinkedWarning((res && res.error) || 'unreachable');
          }
        })();
        return;
      }
      // No link locally: standard upload-form flow.
      await _perchOpenDialog(rootPath, null);
    }
