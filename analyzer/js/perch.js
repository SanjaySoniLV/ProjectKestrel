
    // ── Share-with-Perch dialog + progress card ────────────────────────
    // State scoped to the currently-open dialog. Refreshed on each open.
    const _perchDlgState = {
      rootPath: null,
      preflight: null,        // full response from Api.preflight_perch_upload
      deselected: new Set(),  // sceneIds the user has unchecked
      skipRejected: true,     // tracks the "Skip rejected photos" checkbox
      resumable: null,        // {perchId, idempotencyKey, total, committed, pending} when partial upload found
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
      const elScenes = document.getElementById('perchStatScenes');
      const elPhotos = document.getElementById('perchStatPhotos');
      const elBytes = document.getElementById('perchStatBytes');
      const elSub = document.getElementById('perchStatSub');
      const elSubmit = document.getElementById('perchUploadSubmitBtn');
      if (elScenes) elScenes.textContent = t.scenes.toLocaleString();
      if (elPhotos) elPhotos.textContent = t.photos.toLocaleString();
      if (elBytes) elBytes.textContent = formatPerchBytes(t.bytes);
      if (elSub) elSub.textContent =
        `${t.exports.toLocaleString()} exports + ${t.crops.toLocaleString()} crops · ${t.files.toLocaleString()} files`;
      if (elSubmit) {
        const label = t.photos > 0 ? `📤 Upload ${t.photos.toLocaleString()} photo${t.photos === 1 ? '' : 's'}` : '📤 Upload';
        elSubmit.textContent = label;
        elSubmit.disabled = (t.photos === 0);
      }
    }

    function _perchRenderSceneList() {
      const list = document.getElementById('perchSceneList');
      if (!list) return;
      list.innerHTML = '';
      const pre = _perchDlgState.preflight;
      if (!pre || !pre.scenes || pre.scenes.length === 0) {
        const empty = document.createElement('div');
        empty.className = 'perch-scene-empty';
        empty.textContent = 'No scenes found in this folder.';
        list.appendChild(empty);
        return;
      }
      const rootPath = _perchDlgState.rootPath;
      for (const s of pre.scenes) {
        const sid = String(s.sceneId);
        const cbId = `perch-scene-cb-${sid.replace(/[^a-z0-9_-]/gi, '_')}`;

        const row = document.createElement('div');
        row.className = 'perch-scene-row';
        row.dataset.sceneId = sid;

        const cb = document.createElement('input');
        cb.type = 'checkbox';
        cb.id = cbId;
        cb.className = 'perch-scene-cb';
        cb.checked = !_perchDlgState.deselected.has(sid);
        cb.addEventListener('change', () => {
          if (cb.checked) _perchDlgState.deselected.delete(sid);
          else _perchDlgState.deselected.add(sid);
          _perchUpdateDialogTotals();
        });

        // Thumbnail — click expands an inline preview below the row.
        const thumbBtn = document.createElement('button');
        thumbBtn.type = 'button';
        thumbBtn.className = 'perch-scene-thumb';
        thumbBtn.title = 'Click to preview this scene';
        const thumbImg = document.createElement('img');
        thumbImg.alt = '';
        thumbImg.loading = 'lazy';
        thumbBtn.appendChild(thumbImg);

        const main = document.createElement('label');
        main.htmlFor = cbId;
        main.className = 'perch-scene-main';
        const title = document.createElement('div');
        title.className = 'perch-scene-title';
        title.textContent = s.title || `Scene ${sid}`;
        main.appendChild(title);
        const stamp = _perchFormatTimestamp(s.captureTimeMs);
        if (stamp) {
          const meta = document.createElement('div');
          meta.className = 'perch-scene-meta';
          meta.textContent = stamp;
          main.appendChild(meta);
        }

        const right = document.createElement('label');
        right.htmlFor = cbId;
        right.className = 'perch-scene-right';
        const photos = document.createElement('div');
        photos.className = 'perch-scene-photos';
        photos.textContent = `${Number(s.imageCount || 0).toLocaleString()} photo${s.imageCount === 1 ? '' : 's'}`;
        const size = document.createElement('div');
        size.className = 'perch-scene-size';
        size.textContent = formatPerchBytes(s.totalBytes);
        right.appendChild(photos);
        right.appendChild(size);

        const expand = document.createElement('button');
        expand.type = 'button';
        expand.className = 'perch-scene-expand';
        expand.title = 'Preview scene';
        expand.textContent = '▾';

        row.appendChild(cb);
        row.appendChild(thumbBtn);
        row.appendChild(main);
        row.appendChild(right);
        row.appendChild(expand);

        // Inline preview pane (hidden until thumbnail/expand clicked).
        const preview = document.createElement('div');
        preview.className = 'perch-scene-preview hidden';
        preview.dataset.sceneId = sid;
        const previewImg = document.createElement('img');
        previewImg.className = 'perch-scene-preview-img';
        previewImg.alt = '';
        preview.appendChild(previewImg);

        const togglePreview = async () => {
          const wasHidden = preview.classList.contains('hidden');
          preview.classList.toggle('hidden');
          row.classList.toggle('is-expanded', wasHidden);
          expand.textContent = wasHidden ? '▴' : '▾';
          // Lazy-load the larger image on first expand.
          if (wasHidden && !previewImg.src && s.thumbnailPath && rootPath) {
            try {
              const url = await getBlobUrlForPath(s.thumbnailPath, rootPath);
              if (url) previewImg.src = url;
            } catch {}
          }
        };
        thumbBtn.addEventListener('click', (e) => { e.preventDefault(); togglePreview(); });
        expand.addEventListener('click', (e) => { e.preventDefault(); togglePreview(); });

        list.appendChild(row);
        list.appendChild(preview);

        // Lazy-load the small thumbnail image immediately (cheap, lazy-decoded).
        if (s.thumbnailPath && rootPath) {
          getBlobUrlForPath(s.thumbnailPath, rootPath).then(url => {
            if (url) thumbImg.src = url;
            else thumbBtn.classList.add('no-image');
          }).catch(() => thumbBtn.classList.add('no-image'));
        } else {
          thumbBtn.classList.add('no-image');
        }
      }
    }

    async function _perchLoadAccountAndUsage() {
      const elName = document.getElementById('perchAccountName');
      const elMeta = document.getElementById('perchAccountMeta');
      const elAvatar = document.getElementById('perchAccountAvatar');
      const elUsage = document.getElementById('perchUsageRow');
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
      try {
        const res = await window.pywebview.api.get_perch_usage();
        if (res && res.success && res.usage) {
          const u = res.usage;
          const photos = Number(u.totalImages || 0);
          const bytes = Number(u.totalBytes || 0);
          if (elUsage) elUsage.textContent = `Currently using ${formatPerchBytes(bytes)} across ${photos.toLocaleString()} photo${photos === 1 ? '' : 's'}.`;
        } else if (elUsage) {
          elUsage.textContent = '';
        }
      } catch { if (elUsage) elUsage.textContent = ''; }
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

    function _perchSwapToErrorState(card, prog, rootPath) {
      if (!card) return;
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
          shareWithPerchFolder(rootPath);
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
      const existingPerchId = (resumeOpts && resumeOpts.existingPerchId) || null;
      const idempotencyKey = (resumeOpts && resumeOpts.idempotencyKey) || null;
      try {
        res = await window.pywebview.api.share_with_perch(
          rootPath,
          excludedSceneIds || [],
          skipRejected !== false,
          existingPerchId,
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
      _perchRenderSceneList();
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
      // Already-published banner: shown only for "alive" — the user picks
      // between opening the existing perch and starting a fresh upload.
      const banner = document.getElementById('perchAlreadyPublishedBanner');
      const bannerSub = document.getElementById('perchAlreadyPublishedSubtitle');
      const resumableBanner = document.getElementById('perchResumableBanner');
      if (banner) banner.classList.add('hidden');
      if (resumableBanner) resumableBanner.classList.add('hidden');
      if (verifyResult && verifyResult.status === 'alive' && verifyResult.link) {
        const link = verifyResult.link;
        if (banner) {
          if (bannerSub) {
            const t = link.title ? `"${link.title}"` : 'this folder';
            const ms = Number(link.uploaded_at_ms || 0);
            const rel = ms > 0 ? _perchRelativeDate(ms) : '';
            bannerSub.textContent = rel
              ? `${t} was uploaded ${rel}.`
              : `${t} is already on Perch.`;
          }
          banner.dataset.perchUrl = String(link.perch_url || '');
          banner.dataset.folderPath = rootPath;
          banner.classList.remove('hidden');
        }
      }
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
        _perchRenderSceneList();
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
      // Already-published banner buttons.
      const alreadyOpen = document.getElementById('perchAlreadyPublishedOpenBtn');
      const alreadyNew = document.getElementById('perchAlreadyPublishedNewBtn');
      if (alreadyOpen) alreadyOpen.addEventListener('click', async () => {
        const banner = document.getElementById('perchAlreadyPublishedBanner');
        const url = banner?.dataset?.perchUrl;
        if (url) { try { await window.pywebview.api.open_perch_url(url); } catch {} }
        _perchClosePerchDialog();
      });
      if (alreadyNew) alreadyNew.addEventListener('click', async () => {
        const banner = document.getElementById('perchAlreadyPublishedBanner');
        const folderPath = banner?.dataset?.folderPath || _perchDlgState.rootPath;
        // Clear the local link so the new upload writes a fresh perch_link.json.
        try { await window.pywebview.api.delete_perch_link(folderPath); } catch {}
        // Hide the matching folder-card pill.
        document.querySelectorAll(`.folder-perch-pill[data-folder-path="${cssEscape(folderPath)}"]`)
          .forEach(el => el.classList.add('hidden'));
        // Hide the banner; let the rest of the dialog continue normally.
        if (banner) banner.classList.add('hidden');
      });
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
        _perchRenderSceneList();
        _perchUpdateDialogTotals();
      });
      if (deselAll) deselAll.addEventListener('click', (e) => {
        e.preventDefault();
        const pre = _perchDlgState.preflight;
        if (pre) {
          for (const s of pre.scenes || []) _perchDlgState.deselected.add(String(s.sceneId));
        }
        _perchRenderSceneList();
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

    /** Apply a parsed perch_link.json to a folder-card pill: show it,
     *  populate dataset, refresh hover title with relative date + asset count. */
    function applyPerchLinkToPill(pillEl, link) {
      if (!pillEl || !link) return;
      pillEl.dataset.perchUrl = String(link.perch_url || '');
      pillEl.dataset.perchId = String(link.perch_id || '');
      pillEl.dataset.title = String(link.title || '');
      pillEl.classList.remove('hidden');
      // Hover title: "Published 3 days ago as "Falconry trip" — 387 photos"
      const ms = Number(link.uploaded_at_ms || 0);
      const rel = ms > 0 ? _perchRelativeDate(ms) : '';
      const count = Number(link.image_count || link.asset_count || 0);
      const photoStr = count > 0 ? ` — ${count.toLocaleString()} photo${count === 1 ? '' : 's'}` : '';
      const titleStr = link.title ? ` as "${link.title}"` : '';
      pillEl.title = `Published${rel ? ' ' + rel : ''}${titleStr}${photoStr}\n(Click: open in browser · Right-click: unlink)`;
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

    /** Click the Published pill: verify the link is still valid, then open URL.
     *  On a definite 404 the helper has already cleared the local file — we
     *  hide the pill and toast. On 401/403/network we leave the link alone
     *  but warn the user. */
    async function handlePerchPillClick(folderPath, pillEl) {
      let res;
      try { res = await window.pywebview.api.verify_perch_link(folderPath); }
      catch (e) {
        showToast('Could not verify Perch link: ' + (e?.message || String(e)), 5000);
        return;
      }
      const status = res?.status;
      if (status === 'alive') {
        const url = pillEl?.dataset?.perchUrl || res?.link?.perch_url;
        if (url) { try { await window.pywebview.api.open_perch_url(url); } catch {} }
        return;
      }
      if (status === 'deleted') {
        pillEl?.classList?.add('hidden');
        showToast('This perch was deleted on Perch — the folder has been unlinked.', 5500);
        return;
      }
      if (status === 'unauthorized') {
        showToast('Sign in to Perch first (use the account button at top-right).', 5000);
        return;
      }
      if (status === 'forbidden') {
        showToast('This perch is owned by a different Perch account. Right-click the pill to unlink locally.', 6000);
        return;
      }
      if (status === 'unreachable') {
        showToast('Couldn’t reach Perch. Check your connection and try again.', 5000);
        return;
      }
      // status === 'missing' — the pill should not have been visible. Hide it.
      pillEl?.classList?.add('hidden');
    }

    /** Right-click the Published pill: confirm and remove the local link
     *  (does NOT touch the Worker — the perch on the server is untouched). */
    async function handlePerchPillUnlink(folderPath, pillEl) {
      const ok = window.confirm('Remove this folder’s Perch link locally?\n\nThe perch on perch.projectkestrel.org will NOT be deleted — to remove it from the web, delete it there.');
      if (!ok) return;
      try {
        const res = await window.pywebview.api.delete_perch_link(folderPath);
        if (res && res.success) {
          pillEl.classList.add('hidden');
          // Hide the sibling Sync button too — they share the same lifecycle.
          document.querySelectorAll(`.folder-perch-sync[data-folder-path="${cssEscape(folderPath)}"]`)
            .forEach(el => el.classList.add('hidden'));
          showToast('Perch link removed for this folder.', 3000);
        } else {
          showToast('Could not unlink: ' + (res?.error || 'unknown error'), 5000);
        }
      } catch (e) {
        showToast('Could not unlink: ' + (e?.message || String(e)), 5000);
      }
    }

    /** Show the Sync button (mirrors the pill's lifecycle) and grey it out
     *  when the saved state hash matches the current local state — i.e. the
     *  user hasn't edited anything since last upload/sync. */
    function applyPerchLinkToSyncBtn(btnEl, link, folderPath) {
      if (!btnEl || !link) return;
      btnEl.dataset.perchId = String(link.perch_id || '');
      btnEl.dataset.perchUrl = String(link.perch_url || '');
      btnEl.classList.remove('hidden');
      // Re-check current state hash. The saved hash is only updated on a
      // successful upload (Phase 1) or a successful sync (Phase 3 runner).
      (async () => {
        try {
          const cur = await window.pywebview?.api?.compute_state_hash?.(folderPath);
          const savedHash = link.state_hash_at_upload || null;
          if (cur && cur.success && savedHash && cur.hash === savedHash) {
            btnEl.classList.add('is-clean');
            btnEl.title = 'Up to date — no local edits to sync';
          } else {
            btnEl.classList.remove('is-clean');
            btnEl.title = 'Push local edits (species/family/scene names, rejections) to this perch';
          }
        } catch { /* leave button enabled if we can't compute the hash */ }
      })();
    }

    /** Click handler for the Sync button on a folder card. Verifies the
     *  perch is still alive, then opens the diff-preview modal. */
    async function handlePerchSyncClick(folderPath, btnEl) {
      if (!window.pywebview?.api) {
        showToast('Sync to Perch requires desktop mode', 4000);
        return;
      }
      if (_perchActiveJobId) {
        showToast('A Perch upload or sync is already running.', 4000);
        return;
      }
      // Save unsaved CSV edits first (otherwise we'd diff against in-memory
      // state the bridge can't see).
      if (dirty) {
        const userChoice = await showCullingAssistantPrompt();
        if (userChoice === 'cancel') return;
        if (userChoice === 'save') await saveCsv();
      }
      // Stale-link gate (Phase 3e). On 'deleted' the bridge already cleared
      // the link file; flip the UI back to its un-published state.
      let verify;
      try { verify = await window.pywebview.api.verify_perch_link(folderPath); }
      catch (e) { showToast('Could not verify Perch link: ' + (e?.message || String(e)), 5000); return; }
      const status = verify?.status;
      if (status === 'deleted') {
        document.querySelectorAll(`.folder-perch-pill[data-folder-path="${cssEscape(folderPath)}"]`)
          .forEach(el => el.classList.add('hidden'));
        document.querySelectorAll(`.folder-perch-sync[data-folder-path="${cssEscape(folderPath)}"]`)
          .forEach(el => el.classList.add('hidden'));
        showToast('This perch no longer exists on Perch — the folder has been unlinked.', 5500);
        return;
      }
      if (status === 'unauthorized') {
        showToast('Sign in to Perch first (account button at top-right).', 5000);
        return;
      }
      if (status === 'forbidden') {
        showToast('This perch is owned by a different Perch account.', 6000);
        return;
      }
      if (status === 'unreachable') {
        showToast('Couldn’t reach Perch. Try again later.', 5000);
        return;
      }
      if (status === 'missing') {
        // Race — pill clicked but link is gone. Hide the controls.
        btnEl?.classList?.add('hidden');
        return;
      }
      // 'alive' → fetch the diff and open the modal.
      _perchWireSyncDialogOnce();
      _perchOpenSyncDialog(folderPath);
    }

    // ── Sync diff dialog ────────────────────────────────────────────────
    const _perchSyncDlgState = {
      rootPath: null,
      diff: null,
    };

    function _perchWireSyncDialogOnce() {
      if (_perchWireSyncDialogOnce._done) return;
      _perchWireSyncDialogOnce._done = true;
      const closeBtn = document.getElementById('perchSyncDlgClose');
      const cancelBtn = document.getElementById('perchSyncDlgCancelBtn');
      const submitBtn = document.getElementById('perchSyncDlgSubmitBtn');
      if (closeBtn) closeBtn.addEventListener('click', _perchCloseSyncDialog);
      if (cancelBtn) cancelBtn.addEventListener('click', _perchCloseSyncDialog);
      if (submitBtn) submitBtn.addEventListener('click', () => {
        const root = _perchSyncDlgState.rootPath;
        _perchCloseSyncDialog();
        if (root) kickSyncJob(root);
      });
    }

    function _perchCloseSyncDialog() {
      const dlg = document.getElementById('perchSyncDlg');
      if (dlg && dlg.open) { try { dlg.close(); } catch {} }
    }

    async function _perchOpenSyncDialog(rootPath) {
      const dlg = document.getElementById('perchSyncDlg');
      if (!dlg) { showToast('Sync dialog not available', 4000); return; }
      _perchSyncDlgState.rootPath = rootPath;
      _perchSyncDlgState.diff = null;
      const loading = document.getElementById('perchSyncDlgLoading');
      const body = document.getElementById('perchSyncDlgBody');
      const submit = document.getElementById('perchSyncDlgSubmitBtn');
      if (loading) loading.classList.remove('hidden');
      if (body) body.classList.add('hidden');
      if (submit) { submit.disabled = true; submit.textContent = 'Syncing…'; }
      try { dlg.showModal(); } catch { return; }

      let res;
      try { res = await window.pywebview.api.compute_sync_diff(rootPath); }
      catch (e) {
        if (loading) loading.classList.add('hidden');
        showToast('Could not compute diff: ' + (e?.message || String(e)), 6000);
        try { dlg.close(); } catch {}
        return;
      }
      if (loading) loading.classList.add('hidden');
      if (!res || !res.success) {
        if (res?.error === 'perch_deleted') {
          // The bridge already cleared the local link.
          document.querySelectorAll(`.folder-perch-pill[data-folder-path="${cssEscape(rootPath)}"], .folder-perch-sync[data-folder-path="${cssEscape(rootPath)}"]`)
            .forEach(el => el.classList.add('hidden'));
          showToast('This perch no longer exists on Perch — the folder has been unlinked.', 5500);
        } else {
          showToast('Sync diff failed: ' + (res?.error || 'unknown'), 6000);
        }
        try { dlg.close(); } catch {}
        return;
      }
      _perchSyncDlgState.diff = res.diff;
      _perchRenderSyncDiff(res.diff);
      if (body) body.classList.remove('hidden');
      if (submit) {
        submit.disabled = false;
        submit.textContent = 'Sync now';
      }
    }

    function _perchRenderSyncDiff(diff) {
      const el = document.getElementById('perchSyncDlgBody');
      if (!el) return;
      el.innerHTML = '';
      const t = diff.totals || {};
      const total = Number(t.deletions || 0) + Number(t.field_updates || 0) + Number(t.scene_title_updates || 0);

      // Header: 3 big numbers (Updates / Deletions / Scene renames)
      const stats = document.createElement('div');
      stats.className = 'perch-sync-stats';
      stats.innerHTML = `
        <div class="perch-sync-stat"><div class="perch-sync-stat-num">${(t.field_updates || 0).toLocaleString()}</div><div class="perch-sync-stat-label">Field updates</div></div>
        <div class="perch-sync-stat"><div class="perch-sync-stat-num">${(t.deletions || 0).toLocaleString()}</div><div class="perch-sync-stat-label">Deletions</div></div>
        <div class="perch-sync-stat"><div class="perch-sync-stat-num">${(t.scene_title_updates || 0).toLocaleString()}</div><div class="perch-sync-stat-label">Scene renames</div></div>
      `;
      el.appendChild(stats);

      const sub = document.createElement('div');
      sub.className = 'perch-sync-sub';
      sub.textContent = total === 0
        ? 'Nothing to sync — local state matches Perch.'
        : `${total.toLocaleString()} change${total === 1 ? '' : 's'} ready to push.`;
      el.appendChild(sub);

      if (Number(t.additions || 0) > 0) {
        const addNote = document.createElement('div');
        addNote.className = 'perch-sync-note warn';
        addNote.textContent = `${(t.additions || 0).toLocaleString()} new photo${t.additions === 1 ? '' : 's'} found locally that aren’t on Perch yet. Sync v1 doesn’t upload new photos — re-publish the folder to add them.`;
        el.appendChild(addNote);
      }

      // Detail sections — collapsed by default, scrollable.
      const mkSection = (title, items, fmt) => {
        if (!items || items.length === 0) return;
        const det = document.createElement('details');
        det.className = 'perch-sync-section';
        const sum = document.createElement('summary');
        sum.textContent = `${title} (${items.length.toLocaleString()})`;
        det.appendChild(sum);
        const list = document.createElement('div');
        list.className = 'perch-sync-list';
        for (const it of items) {
          const row = document.createElement('div');
          row.className = 'perch-sync-list-row';
          row.textContent = fmt(it);
          list.appendChild(row);
        }
        det.appendChild(list);
        el.appendChild(det);
      };
      mkSection('Field updates', diff.field_updates, (u) => {
        const fields = Object.keys(u.changes || {}).join(', ');
        return `${u.filename} — ${fields}`;
      });
      mkSection('Deletions (rejected photos)', diff.deletions, (d) => `${d.filename} (${d.kind})`);
      mkSection('Scene renames', diff.scene_title_updates, (s) =>
        `Scene ${s.kestrel_scene_id}: "${s.old_title || '(unnamed)'}" → "${s.new_title}"`);
    }

    async function kickSyncJob(rootPath) {
      let res;
      try { res = await window.pywebview.api.sync_to_perch(rootPath); }
      catch (e) { showToast('Could not start sync: ' + (e?.message || String(e)), 6000); return; }
      if (!res || !res.success) {
        if (res?.error === 'already_running') {
          showToast('A Perch upload or sync is already running.', 5000);
        } else if (res?.error === 'perch_deleted') {
          document.querySelectorAll(`.folder-perch-pill[data-folder-path="${cssEscape(rootPath)}"], .folder-perch-sync[data-folder-path="${cssEscape(rootPath)}"]`)
            .forEach(el => el.classList.add('hidden'));
          showToast('This perch no longer exists on Perch — the folder has been unlinked.', 5500);
        } else {
          showToast('Sync failed: ' + (res?.error || 'unknown'), 6000);
        }
        return;
      }
      const jobId = String(res.job_id);
      _perchActiveJobId = jobId;
      _perchSetButtonsDisabled(true);
      const card = _perchRenderSyncCard(jobId);
      _perchActivePollTimer = setInterval(() => {
        _perchPollSyncProgress(jobId, card, rootPath);
      }, 500);
      setTimeout(() => {
        if (_perchActivePollTimer) {
          clearInterval(_perchActivePollTimer);
          _perchActivePollTimer = setInterval(() => {
            _perchPollSyncProgress(jobId, card, rootPath);
          }, 1000);
        }
      }, 5000);
    }

    function _perchRenderSyncCard(jobId) {
      const panel = _perchEnsureUploadsPanel();
      if (!panel) return null;
      const body = document.getElementById('perchUploadsBody');
      if (!body) return null;
      body.innerHTML = '';
      const card = document.createElement('div');
      card.className = 'perch-upload-card running is-sync';
      card.dataset.jobId = jobId;
      card.innerHTML = `
        <div class="perch-upload-card-header">
          <span class="perch-upload-card-title">Syncing to Perch</span>
          <span class="perch-upload-card-status" data-role="status">Starting…</span>
        </div>
        <div class="perch-upload-card-body" data-role="body">
          <div class="perch-upload-card-progress"><div class="perch-upload-card-progress-fill" data-role="fill" style="width:0%"></div></div>
          <div class="perch-upload-card-current" data-role="current">Computing diff…</div>
        </div>
      `;
      body.appendChild(card);
      const badge = document.getElementById('perchUploadsBadge');
      if (badge) { badge.textContent = 'Syncing'; badge.className = 'perch-uploads-badge'; }
      return card;
    }

    async function _perchPollSyncProgress(jobId, card, rootPath) {
      let res;
      try { res = await window.pywebview.api.get_share_progress(jobId); } catch { return; }
      if (!res || !res.success) return;
      const prog = res.progress || {};
      const phase = prog.phase;
      const status = card?.querySelector('[data-role="status"]');
      const fill = card?.querySelector('[data-role="fill"]');
      const current = card?.querySelector('[data-role="current"]');
      if (phase === 'fetching_state') {
        if (status) status.textContent = 'Fetching server state…';
        if (current) current.textContent = 'Asking Perch what it has.';
      } else if (phase === 'computing_diff') {
        if (status) status.textContent = `Computing diff (${prog.total || 0} changes)`;
      } else if (phase === 'applying') {
        const cur = Number(prog.current || 0);
        const tot = Number(prog.total || 0);
        const pct = tot > 0 ? Math.round((cur / tot) * 100) : 0;
        if (status) status.textContent = `${pct}% · ${cur}/${tot}`;
        if (fill) fill.style.width = pct + '%';
        if (current) current.textContent = prog.label
          ? `${prog.action || 'update'}: ${prog.label}`
          : 'Applying changes…';
      } else if (phase === 'done') {
        _perchSwapToSyncDoneState(card, prog);
        _perchActiveJobId = null;
        if (_perchActivePollTimer) { clearInterval(_perchActivePollTimer); _perchActivePollTimer = null; }
        _perchSetButtonsDisabled(false);
        // Refresh the Sync button state hash on the matching folder card.
        document.querySelectorAll(`.folder-perch-sync[data-folder-path="${cssEscape(rootPath)}"]`)
          .forEach(async (btn) => {
            try {
              const linkRes = await window.pywebview.api.read_perch_link(rootPath);
              if (linkRes && linkRes.present && linkRes.link) {
                applyPerchLinkToSyncBtn(btn, linkRes.link, rootPath);
              }
            } catch {}
          });
      } else if (phase === 'error') {
        _perchSwapToSyncErrorState(card, prog, rootPath);
        _perchActiveJobId = null;
        if (_perchActivePollTimer) { clearInterval(_perchActivePollTimer); _perchActivePollTimer = null; }
        _perchSetButtonsDisabled(false);
      }
    }

    function _perchSwapToSyncDoneState(card, prog) {
      if (!card) return;
      card.className = 'perch-upload-card is-done is-sync';
      const applied = Number(prog.applied || 0);
      const total = Number(prog.total || 0);
      const errs = Array.isArray(prog.errors) ? prog.errors.length : 0;
      const additions = Number(prog.additions_skipped || 0);
      let summary = `Applied ${applied.toLocaleString()} of ${total.toLocaleString()} change${total === 1 ? '' : 's'}.`;
      if (errs > 0) summary += ` ${errs} failed.`;
      if (additions > 0) summary += ` ${additions} addition${additions === 1 ? '' : 's'} skipped (re-publish to upload).`;
      card.innerHTML = `
        <div class="perch-upload-card-header">
          <span class="perch-upload-card-title">✓ Sync complete</span>
          <button type="button" class="perch-upload-card-dismiss" data-role="dismiss" title="Dismiss">✕</button>
        </div>
        <div class="perch-upload-card-body">
          <div class="perch-upload-card-success">${summary}</div>
        </div>
      `;
      const dismiss = card.querySelector('[data-role="dismiss"]');
      if (dismiss) dismiss.addEventListener('click', () => _perchDismissCard(card));
      const badge = document.getElementById('perchUploadsBadge');
      if (badge) { badge.textContent = 'Synced'; badge.className = 'perch-uploads-badge done'; }
    }

    function _perchSwapToSyncErrorState(card, prog, rootPath) {
      if (!card) return;
      const msg = (prog && prog.message) || 'Unknown error';
      card.className = 'perch-upload-card is-error is-sync';
      card.innerHTML = `
        <div class="perch-upload-card-header">
          <span class="perch-upload-card-title">Sync failed</span>
          <button type="button" class="perch-upload-card-dismiss" data-role="dismiss" title="Dismiss">✕</button>
        </div>
        <div class="perch-upload-card-body">
          <div class="perch-upload-card-err">${String(msg).replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]))}</div>
        </div>
      `;
      const dismiss = card.querySelector('[data-role="dismiss"]');
      if (dismiss) dismiss.addEventListener('click', () => _perchDismissCard(card));
      const badge = document.getElementById('perchUploadsBadge');
      if (badge) { badge.textContent = 'Error'; badge.className = 'perch-uploads-badge error'; }
    }

