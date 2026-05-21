    // ─── Cloud Compute — minimal v1 dialog ───────────────────────────────
    // Confirms file count + opens the job, then shows a polling toast. No
    // queue-manager integration in v1 (per the unified-auth refactor plan,
    // C3 of the commercialization roadmap will flesh out the UX).
    // ───────────────────────────────────────────────────────────────────
    // Cloud Compute UX (Stage 1)
    //
    // Design:
    //   - Entry point is the unified #analyzeQueueDlg destination toggle.
    //     "Local" (default) routes to start_analysis_queue; "Cloud" routes
    //     to cloud_compute_submit_job for each selected folder.
    //   - Live state lives in #cloudQueuePanel, a sibling of #queuePanel.
    //     We mirror the local queue's polling shape but at 10s cadence
    //     (Worker rate limits + packs arrive in batches of 10).
    //   - Pause = upload-side only (Modal keeps analyzing already-uploaded
    //     backlog). Cancel = terminal (stops Modal, deletes staging).
    //   - Pack-merged events drain into rescanFolderTree + scheduleAutoRefresh
    //     so folder gallery updates as packs arrive — mirrors local live update.
    //   - Startup resume: on pywebviewready, list pending jobs; if any have
    //     unmerged packs, show #cloudResumeDlg.
    // ───────────────────────────────────────────────────────────────────

    // -- destination state in analyze dialog --
    let _analyzeDestination = 'local'; // 'local' | 'cloud'
    let _cloudSpeedTestResult = null;  // { mbps, samples_uploaded, total_bytes } | null

    // -- cloud-queue auto-drain state --
    //
    // FIFO of folders waiting for a Cloud Compute concurrency slot. Each
    // entry: { path: string, perItemOptions: object | null }. Populated at
    // Start Analysis time; drained event-driven from _ccRenderPanel's diff
    // pass and re-tried each time a job's uploadComplete flips true OR its
    // remoteStatus becomes terminal.
    let _ccPendingSubmits = [];
    // Set once we've surfaced the "orphan job detected" toast this session
    // so triggers that fire repeatedly (every 4s poll tick) don't re-toast.
    let _ccOrphanWarningShown = false;
    // Map<jobId, {uploadComplete: bool, status: string}> snapshot of the
    // previous poll tick, used to detect the trigger transitions.
    let _ccPrevJobSnapshot = new Map();
    // JS-side terminal statuses (set by api_bridge._worker terminal handler).
    // NOT to be confused with the Worker's 'complete'|'cancelled'|'failed'|
    // 'incomplete' set — the desktop normalises those to 'done'|'failed'|
    // 'cancelled' before we ever see them.
    const _CC_TERMINAL_STATUSES = new Set(['done', 'failed', 'cancelled']);

    function _ccPickFirstSelectedFolder() {
      // Pull the first checked folder from the Phase 3 dialog state (the
      // legacy _dlgSelected was renamed to analyzeDlgCheckedPaths in the
      // dialog redesign). Returns the first selected path (alphabetical) or
      // null when nothing is checked.
      try {
        if (typeof analyzeDlgCheckedPaths === 'object' && analyzeDlgCheckedPaths && analyzeDlgCheckedPaths.size > 0) {
          const arr = Array.from(analyzeDlgCheckedPaths).sort();
          return arr[0] || null;
        }
      } catch (_) { /* ignore */ }
      return null;
    }

    function _ccUpdateAddButtonLabel() {
      const btn = document.getElementById('analyzeDlgAdd');
      if (!btn) return;
      const label = _analyzeDestination === 'cloud'
        ? '☁ Add to Cloud Analysis Queue'
        : '➕ Add to Local Analysis Queue';
      // Preserve disabled state; just rename.
      btn.textContent = label;
    }

    function _ccSetDestination(dest) {
      _analyzeDestination = (dest === 'cloud') ? 'cloud' : 'local';
      const local = document.getElementById('analyzeDestLocal');
      const cloud = document.getElementById('analyzeDestCloud');
      if (local && cloud) {
        local.classList.toggle('selected', _analyzeDestination === 'local');
        local.setAttribute('aria-checked', _analyzeDestination === 'local' ? 'true' : 'false');
        cloud.classList.toggle('selected', _analyzeDestination === 'cloud');
        cloud.setAttribute('aria-checked', _analyzeDestination === 'cloud' ? 'true' : 'false');
      }
      _ccUpdateAddButtonLabel();
      if (_analyzeDestination === 'cloud') {
        _ccRefreshUsage();
      }
      // Re-render the summary so the est. time stat + cloud panel match the
      // new destination immediately (button label, beta notice, "?" badge).
      if (typeof refreshAnalyzeDlgSummary === 'function') {
        try { refreshAnalyzeDlgSummary(); } catch (e) { /* failsafe */ }
      }
    }

    async function _ccCheckSignedIn() {
      // Returns true when a usable Clerk JWT exists; false otherwise. All
      // services in the umbrella (Perch, Cloud Compute, Auth Worker) share
      // the same Clerk identity, so the token from the keyring is enough.
      if (!window.pywebview?.api?.get_auth_token) return false;
      try {
        const r = await window.pywebview.api.get_auth_token();
        return !!(r && r.success && r.token);
      } catch { return false; }
    }

    async function _ccApplySignInGate() {
      // Show/hide the sign-in overlay based on auth state, but DO NOT mark the
      // card as .disabled — selection should always be possible so the user
      // sees the "?" est. time and beta affordances even before signing in.
      // The actual sign-in requirement is enforced at speed-test + submit time.
      const overlay = document.getElementById('cloudDestSignInOverlay');
      if (!overlay) return;
      try {
        const signedIn = await _ccCheckSignedIn();
        overlay.classList.toggle('hidden', signedIn);
      } catch {
        // If the bridge call hangs/fails, default to showing the overlay
        // (safer: prompts sign-in rather than letting submit fail later).
        overlay.classList.remove('hidden');
      }
    }

    async function _ccRefreshUsage() {
      // Race the bridge call against a 3s timeout so the "Loading usage…"
      // line never gets stuck when the API hangs (e.g. user not signed in,
      // network down, or worker slow to respond).
      const el = document.getElementById('cloudDestUsage');
      if (!el) return;
      if (!window.pywebview?.api?.cloud_compute_get_usage) {
        el.textContent = 'Usage unavailable.';
        return;
      }
      const timeoutPromise = new Promise((resolve) => setTimeout(() => resolve({ __timedOut: true }), 3000));
      let r;
      try {
        r = await Promise.race([window.pywebview.api.cloud_compute_get_usage(), timeoutPromise]);
      } catch {
        r = null;
      }
      if (r && r.__timedOut) {
        el.textContent = 'Usage unavailable (timed out).';
        return;
      }
      if (r && r.ok && r.usage) {
        const u = r.usage;
        if (u.remainingImages == null) {
          el.textContent = 'Usage metering not configured yet.';
        } else {
          el.textContent = `Remaining cloud images: ${u.remainingImages}`;
        }
      } else {
        el.textContent = 'Usage unavailable.';
      }
    }

    async function _ccRunSpeedTest() {
      // Speed-test button now lives in the est. time panel (analyze-dialog.js
      // markup #analyzeDlgRunSpeedTest), not on the destination card. The
      // status line under the button shows progress + final mbps.
      const btn = document.getElementById('analyzeDlgRunSpeedTest');
      const status = document.getElementById('analyzeDlgCloudSpeedStatus');
      const folder = _ccPickFirstSelectedFolder();
      if (!btn || !status) return;
      if (!folder) {
        status.classList.remove('hidden', 'success');
        status.classList.add('error');
        status.textContent = 'Pick at least one folder before running the speed test.';
        return;
      }
      if (!window.pywebview?.api?.cloud_compute_upload_test) {
        status.classList.remove('hidden', 'success');
        status.classList.add('error');
        status.textContent = 'Speed test unavailable in this build.';
        return;
      }
      // Sign-in gate happens here, not at card-click, so the user can browse
      // the cloud destination's est. time affordances before authenticating.
      const signedIn = await _ccCheckSignedIn();
      if (!signedIn) {
        status.classList.remove('hidden', 'success');
        status.classList.add('error');
        status.textContent = 'Sign in to Perch to run the upload speed test.';
        if (typeof openPerchSignInWindow === 'function') {
          try { openPerchSignInWindow(); } catch {}
        }
        return;
      }
      btn.disabled = true;
      status.classList.remove('hidden', 'success', 'error');
      status.textContent = 'Running speed test…';
      let dots = 0;
      const tick = setInterval(() => {
        dots = (dots + 1) % 4;
        status.textContent = `Running speed test${'.'.repeat(dots).padEnd(3, ' ')}`;
      }, 500);
      let r;
      try {
        r = await window.pywebview.api.cloud_compute_upload_test(folder, 10);
      } catch (e) {
        clearInterval(tick);
        status.classList.add('error');
        status.textContent = 'Speed test failed: ' + (e?.message || e);
        btn.disabled = false;
        return;
      }
      clearInterval(tick);
      btn.disabled = false;
      if (!r || !r.ok) {
        status.classList.add('error');
        if (r && r.error && String(r.error).indexOf('file_too_large') !== -1) {
          status.textContent = 'Some files exceed the 200 MB cap. Try a folder with smaller files.';
        } else {
          status.textContent = 'Speed test failed: ' + (r?.error || 'unknown error');
        }
        return;
      }
      _cloudSpeedTestResult = r;
      const mbps = Number(r.mbps || 0);
      status.classList.add('success');
      status.textContent = `Measured upload: ${mbps.toFixed(1)} MB/s`;
      // Recompute est. time + per-folder estimates against the new rate.
      if (typeof refreshAnalyzeDlgSummary === 'function') {
        try { refreshAnalyzeDlgSummary(); } catch (e) { /* failsafe */ }
      }
    }

    function _ccWireDestinationDialog() {
      const local = document.getElementById('analyzeDestLocal');
      const cloud = document.getElementById('analyzeDestCloud');
      const speedBtn = document.getElementById('analyzeDlgRunSpeedTest');
      if (!local || !cloud || local._ccWired) return;
      local._ccWired = true;
      const pickLocal = () => _ccSetDestination('local');
      const pickCloud = () => _ccSetDestination('cloud');
      local.addEventListener('click', pickLocal);
      cloud.addEventListener('click', pickCloud);
      local.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pickLocal(); } });
      cloud.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pickCloud(); } });
      if (speedBtn && !speedBtn._ccWired) {
        speedBtn._ccWired = true;
        speedBtn.addEventListener('click', _ccRunSpeedTest);
      }
    }

    async function _ccResetDestinationOnDialogOpen() {
      _ccWireDestinationDialog();
      _ccSetDestination('local');
      _cloudSpeedTestResult = null;
      const speedStatus = document.getElementById('analyzeDlgCloudSpeedStatus');
      if (speedStatus) {
        speedStatus.classList.add('hidden');
        speedStatus.classList.remove('success', 'error');
        speedStatus.textContent = '';
      }
      await _ccApplySignInGate();
      _ccRefreshUsage();
      // Repaint the summary so the cloud panel reflects the freshly-reset
      // state (e.g. button label flips back to "Run Upload Speed Test").
      if (typeof refreshAnalyzeDlgSummary === 'function') {
        try { refreshAnalyzeDlgSummary(); } catch (e) { /* failsafe */ }
      }
    }

    async function _ccSubmitSelectedFolders(folderPaths) {
      // Dispatched by the analyze dialog's Add button when destination=cloud.
      // Pushes every selected folder into _ccPendingSubmits and kicks off
      // maybeStartNextCloudJob(), which drains as many as the user's tier
      // allows. As each in-flight job hits uploadComplete or terminal status,
      // the _ccRenderPanel diff pass calls maybeStartNextCloudJob() again to
      // submit the next one. Sequential on the free tier (limit=1), parallel
      // up to limit on paid tiers — no branches needed; the same loop does
      // both.
      if (!Array.isArray(folderPaths) || folderPaths.length === 0) return;
      if (!window.pywebview?.api?.cloud_compute_submit_job) {
        showToast('Cloud Compute requires desktop mode', 4000);
        return;
      }
      // Reset orphan-warning flag per Start-Analysis click so a session that
      // submits → finishes → submits again can re-warn if a new orphan
      // appears later.
      _ccOrphanWarningShown = false;
      for (const fp of folderPaths) {
        if (!fp) continue;
        // Per-folder kestrel-clear has already happened synchronously in
        // event-wiring.js before this function runs (cloud worker can't
        // clear local .kestrel itself). Queue entries only need the path.
        _ccPendingSubmits.push({ path: fp });
      }
      showToast(`Cloud Compute: queued ${folderPaths.length} folder(s).`, 4000);
      _ccStartPolling();
      maybeStartNextCloudJob();
    }

    // ── Cloud auto-drain queue ────────────────────────────────────────────
    //
    // Single entry point for "is there work to do and can we submit it?"
    // Called from three trigger points:
    //   1) _ccSubmitSelectedFolders (Start Analysis click — initial drain)
    //   2) _ccRenderPanel diff pass (job's uploadComplete flipped false→true)
    //   3) _ccRenderPanel diff pass (job's status entered terminal)
    //
    // Loops until either the queue is empty OR entitlements say there's no
    // free slot (or a 403 job_in_progress comes back from a submit). On a
    // hard error other than 403, the offending folder is dropped from the
    // queue and a toast is shown so the user knows.
    let _ccMaybeStartInFlight = false;
    async function maybeStartNextCloudJob() {
      if (_ccMaybeStartInFlight) return; // serialise overlapping triggers
      if (_ccPendingSubmits.length === 0) return;
      if (!window.pywebview?.api?.cloud_compute_submit_job) return;
      _ccMaybeStartInFlight = true;
      try {
        // Loop: drain the queue while entitlements (or the Worker's 403)
        // say we can fit another submit.
        while (_ccPendingSubmits.length > 0) {
          // Best-effort entitlement check. On failure we still try the
          // submit and let the Worker's 403 (with rich body) tell us the
          // truth. The entitlements path is the fast path that avoids one
          // round-trip when we already know we're at capacity.
          let ent = null;
          if (window.pywebview?.api?.cloud_compute_get_entitlements) {
            try {
              ent = await window.pywebview.api.cloud_compute_get_entitlements();
            } catch (e) { /* network blip — fall through */ }
          }
          if (ent && ent.ok) {
            const active = Array.isArray(ent.activeJobs) ? ent.activeJobs : [];
            const limit = Number(ent.limits?.maxConcurrentJobs);
            // Orphan detection: if this is the first iteration of a fresh
            // session AND entitlements show jobs in flight that aren't in
            // our session's _cc_jobs map, surface a one-time toast.
            if (!_ccOrphanWarningShown && active.length > 0) {
              const knownJobIds = await _ccGetKnownJobIds();
              const orphan = active.find(j => !knownJobIds.has(String(j.jobId)));
              if (orphan) {
                _ccOrphanWarningShown = true;
                showToast(
                  '⚠ You have an active Cloud Compute job running ' +
                  '(maybe from another device or a previous session). ' +
                  'Manage it at myaccount.projectkestrel.org/cloud-compute — ' +
                  'your queue will start once that job completes.',
                  9000,
                );
              }
            }
            if (Number.isFinite(limit) && limit > 0 && active.length >= limit) {
              // No slot. Stop draining; the next poll-tick trigger will
              // re-enter this function when a slot frees.
              break;
            }
          }
          const next = _ccPendingSubmits.shift();
          if (!next || !next.path) continue;
          let r;
          try {
            r = await window.pywebview.api.cloud_compute_submit_job(next.path);
          } catch (e) {
            // Transport-level failure. Treat as transient: push back to the
            // front of the queue and break — next trigger will retry.
            _ccPendingSubmits.unshift(next);
            showToast(`Cloud Compute: submit error, will retry. (${e?.message || e})`, 5000);
            break;
          }
          if (r && r.ok) {
            // Accepted; continue draining. The new job appears in the panel
            // on the next _ccRenderPanel tick.
            continue;
          }
          if (r && r.ok === false && r.error === 'job_in_progress') {
            // Race: entitlements said we were free, but a slot was claimed
            // between then and our submit (or entitlements was unavailable
            // and we made a best-effort try). Push folder back and wait
            // for the next trigger. Don't re-toast — the orphan flow above
            // already surfaced a warning if one was warranted.
            _ccPendingSubmits.unshift(next);
            break;
          }
          if (r && r.nothingToDo) {
            showToast(
              `"${next.path.split(/[\\/]/).pop()}" already fully analyzed — skipped.`,
              4000,
            );
            continue;
          }
          // Hard error (quota, legal, auth, network, etc.) — drop this
          // folder from the queue but keep draining the rest. Surface the
          // reason so the user can fix it without losing other queued work.
          const name = next.path.split(/[\\/]/).pop();
          showToast(`Cloud Compute: skipped ${name} — ${r?.error || 'unknown error'}`, 6000);
          if (r?.needSignIn && typeof openPerchSignInWindow === 'function') {
            try { openPerchSignInWindow(); } catch {}
            // If sign-in is required, no further submits will succeed —
            // bail and let the user re-trigger once signed in.
            break;
          }
        }
      } finally {
        _ccMaybeStartInFlight = false;
        // Repaint the panel so pending-row count + active-row count match.
        try { _ccRenderPanel(); } catch (e) { /* failsafe */ }
      }
    }

    async function _ccGetKnownJobIds() {
      // Pull the jobIds the desktop has in this session. Use the same bridge
      // call _ccRenderPanel uses so we share its cache; on failure return an
      // empty set (orphan detection will trip spuriously, but that's strictly
      // better than missing a real orphan).
      try {
        if (!window.pywebview?.api?.cloud_compute_list_jobs) return new Set();
        const r = await window.pywebview.api.cloud_compute_list_jobs();
        const jobs = (r && r.jobs) || [];
        return new Set(jobs.map(j => String(j.jobId)).filter(Boolean));
      } catch { return new Set(); }
    }

    // ── Cloud queue panel (rendering + polling) ──────────────────────────
    //
    // Architecture (post Stage-1 refactor):
    //   - Backend (api_bridge.py) runs ONE remote-status poller per active
    //     job that refreshes a cached snapshot every 5s. JS only ever calls
    //     `cloud_compute_list_jobs`, which reads from cache — no Worker I/O
    //     on the JS render path. Hence: no N+1, no per-render JWT calls.
    //   - The cached snapshot includes `remoteUpdatedAtMs` (wall clock of
    //     last successful Worker fetch). UI flags a job as "syncing" when
    //     that timestamp is older than _CC_STALE_THRESHOLD_MS, but never
    //     zeroes out the displayed counters — the previous good values
    //     persist, so a transient network blip doesn't reset to "0/132".
    //   - Each row shows two progress bars (uploaded + analyzed) and two
    //     status pills (upload phase + analysis phase) so the user can see
    //     both halves of the cloud round-trip at a glance.
    // ─────────────────────────────────────────────────────────────────────

    const _ccPanelEl = () => document.getElementById('cloudQueuePanel');
    const _ccBodyEl  = () => document.getElementById('cloudQueuePanelBody');
    const _ccBadgeEl = () => document.getElementById('cloudQueuePanelBadge');

    let _cloudQueuePanelExpanded = true;

    function _ccRepositionPanel() {
      const panel = _ccPanelEl();
      if (!panel || panel.classList.contains('hidden')) return;
      const queue = document.getElementById('queuePanel');
      if (queue && !queue.classList.contains('hidden')) {
        const h = queue.getBoundingClientRect().height;
        if (h > 0) { panel.style.bottom = (20 + h + 12) + 'px'; return; }
      }
      panel.style.bottom = '20px';
    }

    function _ccInstallPanelObservers() {
      if (_ccInstallPanelObservers._done) return;
      _ccInstallPanelObservers._done = true;
      const queue = document.getElementById('queuePanel');
      if (!queue) return;
      try { new ResizeObserver(() => _ccRepositionPanel()).observe(queue); } catch {}
      try {
        new MutationObserver(() => _ccRepositionPanel())
          .observe(queue, { attributes: true, attributeFilter: ['class'] });
      } catch {}
      window.addEventListener('resize', _ccRepositionPanel);
    }

    let _ccPollingTimer = null;
    // After this many ms with no successful poll, the row shows a "syncing…"
    // badge. Counters keep their last-known values regardless.
    const _CC_STALE_THRESHOLD_MS = 30_000;

    function _ccUploadPhase(state) {
      // Phase of the upload half of the round-trip. Independent of analysis.
      const s = (state && state.status) || 'running';
      if (s === 'cancelled' || s === 'failed') return s;
      if (s === 'upload_paused') return 'upload_paused';
      // Use total-with-anchor here so the "Uploaded" pill flips only when ALL
      // wire-level uploads are complete (including the anchor), even though
      // the visible bar uses newImageCount as denominator.
      const total = Number(state.imageCount || 0);
      const uploaded = Number(state.uploadedCount || 0);
      // "uploaded" on the Worker side is the union of uploaded/dispatched/
      // downloaded/analyzed — any image past the wire boundary counts.
      if (total > 0 && uploaded >= total) return 'uploaded';
      return 'uploading';
    }

    function _ccAnalysisPhase(state) {
      // Phase of the analysis half of the round-trip.
      const s = (state && state.status) || 'running';
      if (s === 'cancelled' || s === 'failed' || s === 'done') return s;
      const r = state && state.remoteStatus;
      if (r === 'complete') return 'done';
      const analyzed = Number(state.analyzedCount || 0);
      const dispatched = Number(state.dispatchedCount || 0);
      if (r === 'processing' || analyzed > 0 || dispatched > 0) return 'analyzing';
      return 'waiting_uploads';
    }

    function _ccUploadPhaseLabel(phase) {
      return ({
        uploading:     'Uploading',
        uploaded:      'Uploaded',
        upload_paused: 'Paused',
        cancelled:     'Cancelled',
        failed:        'Failed',
      })[phase] || phase;
    }

    function _ccAnalysisPhaseLabel(phase) {
      return ({
        waiting_uploads: 'Waiting for uploads',
        analyzing:       'Analyzing',
        done:            'Done',
        cancelled:       'Cancelled',
        failed:          'Failed',
      })[phase] || phase;
    }

    function _ccRenderItem(state) {
      const folder = (state.rootPath || '').split(/[\\/]/).pop() || state.jobId;
      // Display denominator: use newImageCount when available (excludes the
      // scene-continuity anchor), so the user sees "146/181" not "146/182" on
      // re-analysis of a folder with prior results. The percentage bars clamp
      // at 100%, so when uploaded/analyzed transiently exceeds the displayed
      // total (the anchor is in the numerator), the bar still looks right.
      const total = Number(state.newImageCount ?? state.imageCount ?? 0);
      const rawAnalyzed = Number(state.analyzedCount || 0);
      const rawUploaded = Number(state.uploadedCount || 0);
      // Numerators are also clamped to the displayed total so the text never
      // shows e.g. "182/181 uploaded".
      const analyzed = total > 0 ? Math.min(rawAnalyzed, total) : rawAnalyzed;
      const uploaded = total > 0 ? Math.min(rawUploaded, total) : rawUploaded;
      const uploadPhase = _ccUploadPhase(state);
      const analysisPhase = _ccAnalysisPhase(state);
      const uploadPct = total > 0 ? Math.min(100, Math.round((uploaded / total) * 100)) : 0;
      const analysisPct = total > 0 ? Math.min(100, Math.round((analyzed / total) * 100)) : 0;
      const anchor = state.anchorFilename
        ? ` <span class="muted">(+1 anchor for scene continuity)</span>`
        : '';
      // Surface a known failureReason (set by the bootstrap orphan reaper) as
      // an explanatory banner. Without this the user just sees "failed" with
      // no clue why their upload disappeared after a crash.
      const reasonMsg = (() => {
        if (state.failureReason !== 'upload_interrupted') return '';
        return 'Upload was interrupted (the desktop closed mid-upload). '
             + 'The job cannot be resumed — server-side files have been cleaned up. '
             + 'Re-submit the folder to start a new analysis.';
      })();
      const reasonBanner = reasonMsg
        ? `<div class="cloud-queue-item-error">${escapeHtml(reasonMsg)}</div>`
        : '';
      const err = state.error
        ? `<div class="cloud-queue-item-error">${escapeHtml(String(state.error))}</div>`
        : '';

      // Staleness signal: if the backend hasn't received a successful Worker
      // response recently, show a small "syncing…" badge but DO NOT zero the
      // counters. Last-known values persist across transient failures.
      // Terminal jobs intentionally have stale timestamps (the per-job poller
      // stops once a job is done), so suppress the badge for them.
      const updatedAt = Number(state.remoteUpdatedAtMs || 0);
      const ageMs = updatedAt > 0 ? (Date.now() - updatedAt) : Infinity;
      const isTerminal = ['done', 'failed', 'cancelled'].includes(state.status);
      const isStale = !isTerminal && ageMs > _CC_STALE_THRESHOLD_MS;
      const failureCount = Number(state.remoteFailureCount || 0);
      const staleHint = isStale
        ? `<span class="cloud-sync-badge" title="Last successful sync ${updatedAt > 0 ? Math.round(ageMs/1000)+'s ago' : 'never'}${failureCount > 0 ? ' — ' + failureCount + ' failed attempt(s)' : ''}">syncing…</span>`
        : '';

      // Per-row controls:
      //  - Pause/Resume only meaningful while uploads are active. Once the
      //    last image is on the wire there's nothing to pause; the analysis
      //    half is server-side and can't be paused from the desktop.
      //  - Cancel disabled in terminal states.
      const pauseDisabled = uploadPhase !== 'uploading';
      const resumeDisabled = uploadPhase !== 'upload_paused';
      const cancelDisabled = ['done', 'failed', 'cancelled'].includes(state.status);

      return `
        <div class="queue-item cloud-queue-item" data-job-id="${escapeHtml(state.jobId)}">
          <div class="queue-item-header">
            <span class="queue-item-name" title="${escapeHtml(state.rootPath || '')}">${escapeHtml(folder)}</span>
            ${staleHint}
          </div>
          <div class="cloud-phase-row">
            <span class="cloud-phase-pill upload ${uploadPhase}">↑ ${_ccUploadPhaseLabel(uploadPhase)}</span>
            <span class="cloud-phase-pill analysis ${analysisPhase}">⚙ ${_ccAnalysisPhaseLabel(analysisPhase)}</span>
          </div>
          <div class="cloud-bar-block">
            <div class="cloud-bar-label">${uploaded} / ${total} uploaded${anchor}</div>
            <div class="queue-item-progress">
              <div class="queue-item-progress-fill upload" style="width:${uploadPct}%"></div>
            </div>
          </div>
          <div class="cloud-bar-block">
            <div class="cloud-bar-label">${analyzed} / ${total} analyzed</div>
            <div class="queue-item-progress">
              <div class="queue-item-progress-fill analysis" style="width:${analysisPct}%"></div>
            </div>
          </div>
          ${reasonBanner}
          ${err}
          <div class="cloud-queue-item-controls">
            <button data-cc-action="pause" data-job-id="${escapeHtml(state.jobId)}" ${pauseDisabled ? 'disabled' : ''}>
              ⏸ Pause
            </button>
            <button data-cc-action="resume" data-job-id="${escapeHtml(state.jobId)}" ${resumeDisabled ? 'disabled' : ''}>
              ▶ Resume
            </button>
            <button data-cc-action="cancel" data-job-id="${escapeHtml(state.jobId)}" ${cancelDisabled ? 'disabled' : ''}>
              ⏹ Cancel
            </button>
          </div>
        </div>
      `;
    }

    function _ccPanelBadge(jobs, pendingCount) {
      const active = jobs.filter(j => !['done', 'failed', 'cancelled'].includes(j.status)).length;
      const done = jobs.filter(j => j.status === 'done').length;
      const failed = jobs.filter(j => j.status === 'failed' || j.status === 'cancelled').length;
      const parts = [];
      if (active > 0) parts.push(`${active} active`);
      if (pendingCount > 0) parts.push(`${pendingCount} queued`);
      if (parts.length === 0 && (done > 0 || failed > 0)) parts.push(`${done + failed} done`);
      return parts.length === 0 ? 'Idle' : parts.join(' · ');
    }

    function _ccRenderPendingItem(entry) {
      // Ghost row for a queue entry that hasn't been accepted by the Worker
      // yet. Same outer container as a real job row (so panel layout stays
      // consistent) but no progress bars, no per-item controls, and a
      // distinct status badge so the user understands this folder is on
      // deck but not yet using a concurrency slot.
      const name = (entry.path || '').split(/[\\/]/).pop() || entry.path || '(unknown)';
      return `
        <div class="queue-item cloud-queue-item cc-queue-item--pending"
             title="${escapeHtml(entry.path || '')}">
          <div class="queue-item-header">
            <span class="queue-item-name">${escapeHtml(name)}</span>
            <span class="cc-pending-badge">⏳ Queued — waiting for slot</span>
          </div>
        </div>
      `;
    }

    async function _ccRenderPanel() {
      const panel = _ccPanelEl(); const body = _ccBodyEl(); const badge = _ccBadgeEl();
      if (!panel || !body || !badge) return;
      if (!window.pywebview?.api?.cloud_compute_list_jobs) return;
      let listRes;
      try {
        listRes = await window.pywebview.api.cloud_compute_list_jobs();
      } catch { return; }
      const jobs = (listRes && listRes.jobs) || [];
      const pending = _ccPendingSubmits.slice(); // snapshot for this render
      // Panel is visible when there's anything to show — accepted jobs OR
      // pending queue entries. Hiding when only pending exists would lose
      // the "your 5 folders are queued" affordance.
      if (jobs.length === 0 && pending.length === 0) {
        panel.classList.add('hidden');
        body.innerHTML = '';
        _ccPrevJobSnapshot = new Map();
        return;
      }
      panel.classList.remove('hidden');
      const controls = document.getElementById('cloudQueuePanelControls');
      const toggle = document.getElementById('cloudQueuePanelToggle');
      if (toggle) toggle.classList.toggle('open', _cloudQueuePanelExpanded);
      body.classList.toggle('hidden', !_cloudQueuePanelExpanded);
      if (controls) controls.classList.toggle('hidden', !_cloudQueuePanelExpanded);
      // Active jobs first, pending ghost rows below. Pending shows the user
      // exactly what's still ahead in their queue.
      body.innerHTML =
        jobs.map(_ccRenderItem).join('') +
        pending.map(_ccRenderPendingItem).join('');
      badge.textContent = _ccPanelBadge(jobs, pending.length);
      _ccRepositionPanel();
      _ccInstallPanelObservers();

      // ── Diff pass for auto-drain triggers ──────────────────────────────
      // Compare against the previous tick's snapshot of (uploadComplete,
      // status) per jobId. If any job's uploadComplete flipped false→true
      // (paid-tier slot-freeing path) OR its status entered terminal
      // (free-tier slot-freeing path), call maybeStartNextCloudJob() to
      // drain another folder from the queue.
      let shouldRetryQueue = false;
      for (const j of jobs) {
        const prev = _ccPrevJobSnapshot.get(j.jobId);
        if (!prev) continue;
        const becameUploadComplete = !prev.uploadComplete && !!j.uploadComplete;
        const becameTerminal = !_CC_TERMINAL_STATUSES.has(prev.status)
          && _CC_TERMINAL_STATUSES.has(j.status);
        if (becameUploadComplete || becameTerminal) { shouldRetryQueue = true; break; }
      }
      _ccPrevJobSnapshot = new Map(jobs.map(j => [j.jobId, {
        uploadComplete: !!j.uploadComplete,
        status: j.status,
      }]));
      if (shouldRetryQueue && _ccPendingSubmits.length > 0) {
        // Fire-and-forget; maybeStartNextCloudJob serialises itself.
        maybeStartNextCloudJob();
      }

      // Drain pack-merged events and trigger folder rescan so new photos show
      // in the gallery as packs arrive — same UX as local live update. Uses
      // the multi-root _findRootContaining lookup (from folder-tree.js) so a
      // pack landing in folder X rescans the root that contains X, not the
      // legacy singleton folderTreeRootNode.
      try {
        if (window.pywebview?.api?.cloud_compute_get_pack_events) {
          const evRes = await window.pywebview.api.cloud_compute_get_pack_events();
          const events = (evRes && evRes.events) || [];
          const folders = new Set();
          for (const ev of events) if (ev && ev.folderPath) folders.add(ev.folderPath);
          for (const fp of folders) {
            try {
              if (typeof _findRootContaining === 'function'
                  && typeof rescanFolderRoot === 'function') {
                const root = _findRootContaining(fp);
                if (root) rescanFolderRoot(root.path);
              }
            } catch {}
            try { if (typeof scheduleAutoRefresh === 'function') scheduleAutoRefresh(fp); } catch {}
          }
        }
      } catch {}
    }

    function _ccStartPolling() {
      if (_ccPollingTimer) return;
      _ccRenderPanel(); // immediate paint
      // 4s cadence: backend cache is refreshed every 5s, so a slightly
      // tighter UI tick keeps the displayed numbers within one render of
      // the latest snapshot. No Worker I/O happens on this tick.
      _ccPollingTimer = setInterval(_ccRenderPanel, 4000);
    }

    function _ccStopPolling() {
      if (_ccPollingTimer) { clearInterval(_ccPollingTimer); _ccPollingTimer = null; }
    }

    // Click delegation for per-item pause/resume/cancel buttons.
    document.addEventListener('click', async (ev) => {
      const t = ev.target;
      if (!t || !(t instanceof HTMLElement)) return;
      const action = t.getAttribute('data-cc-action');
      const jobId = t.getAttribute('data-job-id');
      if (!action || !jobId || !window.pywebview?.api) return;
      const fnName = ({
        pause: 'cloud_compute_pause_job',
        resume: 'cloud_compute_resume_job',
        cancel: 'cloud_compute_cancel_job',
      })[action];
      if (!fnName || !window.pywebview.api[fnName]) return;
      t.disabled = true;
      if (action === 'cancel') {
        if (!window.confirm('Cancel this cloud analysis job?\n\nUploaded images will be deleted from the server. Any results already downloaded stay on disk.')) {
          t.disabled = false;
          return;
        }
      }
      try {
        const r = await window.pywebview.api[fnName](jobId);
        if (r && r.ok) {
          showToast(`Cloud Compute: ${action} OK`, 2500);
          _ccRenderPanel();
        } else {
          showToast(`Cloud Compute ${action} failed: ${r?.error || 'unknown'}`, 5000);
        }
      } catch (e) {
        showToast(`Cloud Compute ${action} failed: ${e?.message || e}`, 5000);
      } finally {
        t.disabled = false;
      }
    });

    // ── Startup resume ───────────────────────────────────────────────────

    // Helper: filter list_pending_jobs result down to "needs download" candidates.
    // Used by both startup resume and the periodic folder-recheck timer.
    function _ccPickResumeCandidates(jobs) {
      return (jobs || []).filter(j => {
        // Defence in depth: backend already skips Worker I/O for terminal jobs,
        // but make sure cancelled / failed never appear as resumable here even
        // if the local store somehow shows pending packs (cancelled mid-flight
        // could leave stale availablePacks data).
        if (j.status === 'cancelled' || j.status === 'failed' || j.status === 'done') return false;
        const downloaded = new Set(j.downloadedPacks || []);
        const available = j.availablePacks || [];
        const unmerged = available.filter(p => !downloaded.has(p));
        return unmerged.length > 0 || (j.remoteStatus === 'complete' && downloaded.size === 0);
      });
    }

    // Fire-and-forget downloads. Centralised so startup, dialog, recheck, and
    // the manual "Retry downloads" link all use the same path.
    function _ccTriggerResume(jobIds, toastVerb = 'Resuming') {
      if (!jobIds || jobIds.length === 0) return;
      (async () => {
        for (const jid of jobIds) {
          try { await window.pywebview.api.cloud_compute_resume_download(jid); } catch {}
        }
        showToast(`${toastVerb} ${jobIds.length} cloud download(s).`, 3500);
        _ccStartPolling();
        _ccRenderPanel();
      })();
    }

    async function _ccStartupResume() {
      if (!window.pywebview?.api?.cloud_compute_list_pending_jobs) return;
      let r;
      try {
        r = await window.pywebview.api.cloud_compute_list_pending_jobs();
      } catch { return; }
      if (!r || !r.ok) return;
      const candidates = _ccPickResumeCandidates(r.jobs);
      if (candidates.length === 0) return;
      const accessible = candidates.filter(j => j.folderAvailable !== false);
      const inaccessible = candidates.filter(j => j.folderAvailable === false);
      // All folders accessible → just auto-resume; no need to show a dialog.
      if (inaccessible.length === 0) {
        _ccTriggerResume(accessible.map(j => j.jobId), 'Auto-resuming');
        return;
      }
      // At least one inaccessible folder. Show the grouped dialog AND start
      // the recheck timer so inaccessible folders auto-resume when mounted,
      // regardless of how the user dismisses the dialog.
      _ccShowResumeDialog(accessible, inaccessible);
      _ccStartFolderRecheckTimer();
    }

    function _ccShowResumeDialog(accessibleJobs, inaccessibleJobs) {
      const dlg = document.getElementById('cloudResumeDlg');
      const list = document.getElementById('cloudResumeList');
      const intro = document.getElementById('cloudResumeIntro');
      if (!dlg || !list) return;
      const accCount = accessibleJobs.length;
      const inaccCount = inaccessibleJobs.length;
      const totalCount = accCount + inaccCount;
      let introText;
      if (inaccCount === 0) {
        introText = `You have ${totalCount} cloud analysis job(s) with result packs ready to download.`;
      } else if (accCount === 0) {
        introText = `${inaccCount} cloud analysis job(s) have packs ready, but their folders aren't currently mounted. They will auto-resume when the folders come back online.`;
      } else {
        introText = `${totalCount} cloud analysis job(s) have packs ready. ${accCount} can resume now; ${inaccCount} are waiting for a folder to be mounted.`;
      }
      intro.textContent = introText;
      const renderItem = (j) => {
        const folder = (j.folderPath || '').split(/[\\/]/).pop() || j.jobId;
        const downloaded = (j.downloadedPacks || []).length;
        const available = (j.availablePacks || []).length;
        const status = j.remoteStatus || j.status || '?';
        const isUnavail = j.folderAvailable === false;
        // Disabled checkboxes are skipped by the "Resume All" handler so a
        // user can't accidentally fire a download for a missing folder. The
        // recheck timer auto-resumes them when the folder reappears.
        const attrs = isUnavail ? 'disabled' : 'checked';
        const cls = isUnavail ? ' cloud-resume-item-unavail' : '';
        const meta = isUnavail
          ? `${escapeHtml(j.folderPath || '')}<br><em>Folder not currently mounted — will auto-resume when available.</em>`
          : `${escapeHtml(j.folderPath || '')}<br>Status: ${escapeHtml(status)} · ${available - downloaded} pack(s) waiting`;
        return `
          <label class="cloud-resume-item${cls}">
            <input type="checkbox" data-job-id="${escapeHtml(j.jobId)}" ${attrs} />
            <div class="cloud-resume-item-body">
              <div class="cloud-resume-item-folder">${escapeHtml(folder)}</div>
              <div class="cloud-resume-item-meta">${meta}</div>
            </div>
          </label>
        `;
      };
      let html = accessibleJobs.map(renderItem).join('');
      if (inaccessibleJobs.length > 0) {
        html += `
          <details class="cloud-resume-section-deferred"${accCount === 0 ? ' open' : ''}>
            <summary>Folders not currently accessible (${inaccCount})</summary>
            ${inaccessibleJobs.map(renderItem).join('')}
          </details>
        `;
      }
      list.innerHTML = html;
      try { dlg.showModal(); } catch { dlg.show(); }
    }

    // ── Periodic folder-availability recheck ────────────────────────────
    // Runs only while at least one job has packs ready but its folder is
    // unmounted. Self-terminates as soon as the deferred set is empty so
    // there's zero idle traffic when nothing's waiting.
    let _ccFolderRecheckTimer = null;
    const _CC_FOLDER_RECHECK_MS = 30000;

    function _ccStartFolderRecheckTimer() {
      if (_ccFolderRecheckTimer != null) return;
      _ccFolderRecheckTimer = setInterval(_ccFolderRecheck, _CC_FOLDER_RECHECK_MS);
      // Surface the manual "Retry downloads" entry while the timer is active
      // so the user can collapse the wait if they just plugged a drive in.
      const btn = document.getElementById('cloudQueueRetryDownloadsBtn');
      if (btn) btn.classList.remove('hidden');
    }
    function _ccStopFolderRecheckTimer() {
      if (_ccFolderRecheckTimer != null) {
        clearInterval(_ccFolderRecheckTimer);
        _ccFolderRecheckTimer = null;
      }
      const btn = document.getElementById('cloudQueueRetryDownloadsBtn');
      if (btn) btn.classList.add('hidden');
    }

    async function _ccFolderRecheck() {
      if (!window.pywebview?.api?.cloud_compute_list_pending_jobs) return;
      let r;
      try {
        r = await window.pywebview.api.cloud_compute_list_pending_jobs();
      } catch { return; }
      if (!r || !r.ok) return;
      const candidates = _ccPickResumeCandidates(r.jobs);
      const stillDeferred = candidates.filter(j => j.folderAvailable === false);
      const nowAvailable = candidates.filter(j => j.folderAvailable !== false);
      if (nowAvailable.length > 0) {
        // Folders that flipped from unavailable to available since last check
        // (any candidate without an in-memory _cc_jobs entry counts; the
        // backend's _cc_jobs map will already include resume jobs from this
        // session, so calling resume_download is idempotent).
        _ccTriggerResume(nowAvailable.map(j => j.jobId), 'Folder back online — resuming');
      }
      if (stillDeferred.length === 0) _ccStopFolderRecheckTimer();
    }

    function _ccWireResumeDialog() {
      const dlg = document.getElementById('cloudResumeDlg');
      if (!dlg || dlg._ccWired) return;
      dlg._ccWired = true;
      const close = () => { try { dlg.close(); } catch {} };
      document.getElementById('cloudResumeLater')?.addEventListener('click', close);
      const fireResume = async (jobIds) => {
        for (const jid of jobIds) {
          try {
            await window.pywebview.api.cloud_compute_resume_download(jid);
          } catch {}
        }
        showToast(`Resuming ${jobIds.length} cloud download(s).`, 3500);
        _ccStartPolling();
        close();
      };
      document.getElementById('cloudResumeAll')?.addEventListener('click', () => {
        // Skip :disabled — those are inaccessible-folder rows that auto-resume
        // via the recheck timer. User clicking "Resume All" should not trigger
        // a noisy folder_unavailable round-trip for them.
        const all = [...dlg.querySelectorAll('input[data-job-id]:not(:disabled)')]
          .map(i => i.getAttribute('data-job-id'));
        if (all.length === 0) { showToast('No accessible folders to resume right now.', 3500); close(); return; }
        fireResume(all);
      });
      document.getElementById('cloudResumeSelected')?.addEventListener('click', () => {
        const picked = [...dlg.querySelectorAll('input[data-job-id]:checked')].map(i => i.getAttribute('data-job-id'));
        if (picked.length === 0) { showToast('Pick at least one job, or click Later.', 3000); return; }
        fireResume(picked);
      });
    }

    // Panel-level controls (one-time wiring): Clear done removes terminal jobs
    // from the persistent ledger; Pause All / Cancel All apply to every active
    // job in the panel. The buttons themselves live in visualizer.html.
    let _ccPanelControlsWired = false;
    function _ccWirePanelControls() {
      if (_ccPanelControlsWired) return;
      _ccPanelControlsWired = true;
      const clearBtn = document.getElementById('cloudQueueClearBtn');
      const pauseBtn = document.getElementById('cloudQueuePauseBtn');
      const cancelBtn = document.getElementById('cloudQueueCancelBtn');
      if (clearBtn) {
        clearBtn.addEventListener('click', async () => {
          if (!window.pywebview?.api?.cloud_compute_clear_done) return;
          clearBtn.disabled = true;
          try {
            const r = await window.pywebview.api.cloud_compute_clear_done();
            if (r && r.ok) {
              showToast(`Cleared ${(r.removed || []).length} finished job(s)`, 2500);
              _ccRenderPanel();
            } else {
              showToast(`Clear done failed: ${r?.error || 'unknown'}`, 5000);
            }
          } catch (e) {
            showToast(`Clear done failed: ${e?.message || e}`, 5000);
          } finally {
            clearBtn.disabled = false;
          }
        });
      }
      const _activeJobIds = async () => {
        if (!window.pywebview?.api?.cloud_compute_list_jobs) return [];
        try {
          const r = await window.pywebview.api.cloud_compute_list_jobs();
          return ((r && r.jobs) || [])
            .filter(j => !['done', 'failed', 'cancelled'].includes(j.status))
            .map(j => j.jobId);
        } catch { return []; }
      };
      if (pauseBtn) {
        pauseBtn.addEventListener('click', async () => {
          if (!window.pywebview?.api?.cloud_compute_pause_job) return;
          pauseBtn.disabled = true;
          try {
            const ids = await _activeJobIds();
            if (ids.length === 0) { showToast('No active jobs to pause', 2500); return; }
            await Promise.all(ids.map(id => window.pywebview.api.cloud_compute_pause_job(id).catch(() => null)));
            showToast(`Paused ${ids.length} job(s)`, 2500);
            _ccRenderPanel();
          } finally {
            pauseBtn.disabled = false;
          }
        });
      }
      if (cancelBtn) {
        cancelBtn.addEventListener('click', async () => {
          if (!window.pywebview?.api?.cloud_compute_cancel_job) return;
          const ids = await _activeJobIds();
          if (ids.length === 0) { showToast('No active jobs to cancel', 2500); return; }
          if (!window.confirm(`Cancel ${ids.length} active cloud job(s)?\n\nUploaded images will be deleted from the server. Any results already downloaded stay on disk.`)) return;
          cancelBtn.disabled = true;
          try {
            await Promise.all(ids.map(id => window.pywebview.api.cloud_compute_cancel_job(id).catch(() => null)));
            showToast(`Cancelled ${ids.length} job(s)`, 2500);
            _ccRenderPanel();
          } finally {
            cancelBtn.disabled = false;
          }
        });
      }
      // "Retry downloads" — manual short-circuit for the 30s recheck timer.
      // Visible only while the timer is running (i.e. at least one job is
      // waiting for a folder to come back online).
      const retryBtn = document.getElementById('cloudQueueRetryDownloadsBtn');
      if (retryBtn) {
        retryBtn.addEventListener('click', async () => {
          retryBtn.disabled = true;
          try {
            await _ccFolderRecheck();
          } finally {
            retryBtn.disabled = false;
          }
        });
      }
    }

    // Wire startup hooks. The pywebview ready event is the canonical signal
    // that the bridge is alive; we also tolerate a fallback timer in case
    // the event fired before our listener attached.
    async function _ccBootstrap() {
      _ccWireResumeDialog();
      _ccWirePanelControls();
      // Only spin up the panel poller if there's actually a non-terminal
      // job to watch. Avoids burning a 4s tick forever after app launch
      // when the user has never used cloud compute.
      let hasPending = false;
      try {
        if (window.pywebview?.api?.cloud_compute_list_pending_jobs) {
          const r = await window.pywebview.api.cloud_compute_list_pending_jobs();
          const jobs = (r && r.jobs) || [];
          hasPending = jobs.some(j => !['done', 'failed', 'cancelled'].includes(j.status));
        }
      } catch {}
      _ccStartupResume();   // resume dialog gates itself on candidate count
      if (hasPending) {
        _ccStartPolling();
      }
    }
    if (window.pywebview && window.pywebview.api) {
      _ccBootstrap();
    } else {
      window.addEventListener('pywebviewready', _ccBootstrap, { once: true });
      setTimeout(() => { if (window.pywebview?.api && !_ccPollingTimer) _ccBootstrap(); }, 3000);
    }

    async function shareWithPerchFolder(rootPath) {
      if (!window.pywebview?.api) {
        showToast('Share with Perch requires desktop mode', 4000);
        return;
      }
      if (_perchActiveJobId) {
        showToast('A Perch upload is already running.', 4000);
        return;
      }
      if (dirty) {
        const userChoice = await showCullingAssistantPrompt();
        if (userChoice === 'cancel') return;
        if (userChoice === 'save') await saveCsv();
      }
      _perchWirePerchDialogOnce();

      // Stale-link gate: if perch_link.json exists, verify the perch is still
      // alive on the server BEFORE opening the dialog. The dialog gets a banner
      // that lets the user open the existing perch or start a fresh upload.
      let verify = null;
      try { verify = await window.pywebview.api.verify_perch_link(rootPath); } catch {}
      const status = verify?.status;
      if (status === 'unauthorized') {
        showToast('Sign in to Perch first (use the account button at top-right).', 5000);
        return;
      }
      if (status === 'forbidden') {
        showToast('This folder was published from a different Perch account. Right-click the Published pill to unlink locally.', 7000);
        return;
      }
      if (status === 'deleted') {
        showToast('The previously linked Perch was deleted — proceeding with a fresh upload.', 5500);
        // Hide the pill on the matching folder card if rendered.
        document.querySelectorAll(`.folder-perch-pill[data-folder-path="${cssEscape(rootPath)}"]`)
          .forEach(el => el.classList.add('hidden'));
      }
      // For 'alive' — open the dialog, then show the banner via _perchOpenDialog.
      // For 'missing'/'unreachable'/null — open dialog as normal.
      _perchOpenDialog(rootPath, verify);
    }

    /** Minimal CSS-attribute-value escape — avoids needing a polyfill for
     *  CSS.escape on older webviews. We only use it for paths in selectors. */
    function cssEscape(s) {
      return String(s || '').replace(/(["\\\]\[])/g, '\\$1');
    }
    
    // Custom dialog prompt for Culling Assistant save decision
    function showCullingAssistantPrompt() {
      return new Promise((resolve) => {
        const dlg = document.createElement('dialog');
        dlg.style.cssText = 'border:none;border-radius:8px;background:#1a1d28;color:#e8f0f8;font-family:inherit;padding:0;max-width:450px;box-shadow:0 8px 32px rgba(0,0,0,0.3)';
        
        const content = document.createElement('div');
        content.style.cssText = 'padding:24px;display:flex;flex-direction:column;gap:16px';
        
        const msg = document.createElement('div');
        msg.style.cssText = 'font-size:16px;font-weight:500;line-height:1.4';
        msg.textContent = 'You have unsaved changes. What would you like to do?';
        content.appendChild(msg);
        
        const btnContainer = document.createElement('div');
        btnContainer.style.cssText = 'display:flex;gap:8px;justify-content:flex-end';
        
        const btnCancel = document.createElement('button');
        btnCancel.style.cssText = 'padding:8px 16px;border:1px solid #444;border-radius:4px;background:#2d3142;color:#e8f0f8;cursor:pointer;font-size:14px;transition:background 0.2s';
        btnCancel.textContent = 'Cancel';
        btnCancel.addEventListener('click', () => { dlg.close(); document.body.removeChild(dlg); resolve('cancel'); });
        btnContainer.appendChild(btnCancel);
        
        const btnDontSave = document.createElement('button');
        btnDontSave.style.cssText = 'padding:8px 16px;border:1px solid #444;border-radius:4px;background:#2d3142;color:#e8f0f8;cursor:pointer;font-size:14px;transition:background 0.2s';
        btnDontSave.textContent = 'Don\'t Save';
        btnDontSave.addEventListener('click', () => { dlg.close(); document.body.removeChild(dlg); resolve('dontsave'); });
        btnContainer.appendChild(btnDontSave);
        
        const btnSave = document.createElement('button');
        btnSave.style.cssText = 'padding:8px 16px;border:1px solid #5a9fd4;border-radius:4px;background:#3d5a7e;color:#e8f0f8;cursor:pointer;font-size:14px;font-weight:500;transition:background 0.2s';
        btnSave.textContent = 'Save Changes';
        btnSave.addEventListener('click', () => { dlg.close(); document.body.removeChild(dlg); resolve('save'); });
        btnContainer.appendChild(btnSave);
        
        content.appendChild(btnContainer);
        dlg.appendChild(content);
        
        dlg.addEventListener('keydown', (e) => {
          if (e.key === 'Escape') { 
            e.preventDefault(); 
            dlg.close(); 
            document.body.removeChild(dlg); 
            resolve('cancel');
          }
        });
        
        document.body.appendChild(dlg);
        dlg.showModal();
      });
    }

    function resetFolderCullState(rootPath, mode) {
      let changed = 0;
      for (const r of rows) {
        if (r.__rootPath !== rootPath) continue;
        const origin = normalizeCullOrigin(r);
        const isResetAll = mode === 'all' && (origin === 'manual' || origin === 'verified');
        const isResetVerified = mode === 'verified' && origin === 'verified';
        if (!isResetAll && !isResetVerified) continue;
        if (r.culled || r.culled_origin) {
          r.culled = '';
          r.culled_origin = '';
          changed++;
        }
      }
      if (changed > 0) {
        markDirty(rootPath);
        renderScenes();
        if (currentSceneId != null && _currentScene) {
          const refreshed = reloadScene(currentSceneId);
          if (refreshed) {
            _currentScene = refreshed;
            renderFilmstrip(refreshed);
            selectFilmstripImage(Math.min(currentImageIndex, Math.max(0, refreshed.images.length - 1)), refreshed);
          }
        }
      }
      return changed;
    }

    function showFolderOptionsDialog(folderPath) {
      const folderName = folderBaseName(folderPath) || folderPath || 'folder';
      const dlg = document.createElement('dialog');
      dlg.style.cssText = [
        'border:1px solid #303a52',
        'border-radius:12px',
        'background:#141a24',
        'color:#e8f0f8',
        'padding:0',
        'min-width:440px',
        'max-width:540px',
        'width:90vw',
        // `fit-content` hugs the actual content; `auto` can render at the
        // max-height on Chromium when combined with overflow-y:auto.
        'height:fit-content',
        'max-height:92vh',
        'overflow-x:hidden',
        'overflow-y:auto',
        'box-shadow:0 8px 40px rgba(0,0,0,0.6)',
      ].join(';');

      dlg.innerHTML = `
        <div style="padding:20px 22px 14px;border-bottom:1px solid #222e45;">
          <div style="font-size:17px;font-weight:700;margin-bottom:4px;">Folder Options</div>
          <div style="color:#7a90b8;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${escapeHtml(folderPath)}">${escapeHtml(folderName)}</div>
        </div>

        <div style="padding:14px 22px;">
          <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.06em;color:#5a7099;margin-bottom:10px;">Reset Culling Decisions</div>

          <div class="folder-opt-card" id="folderOptCardVerified" style="
            display:flex;align-items:flex-start;gap:12px;padding:12px 14px;
            border:1px solid #263045;border-radius:8px;background:#1a2235;
            cursor:pointer;margin-bottom:8px;transition:border-color 0.15s,background 0.15s;">
            <div style="margin-top:2px;font-size:16px;line-height:1;">↺</div>
            <div style="flex:1;min-width:0;">
              <div style="font-size:13px;font-weight:600;margin-bottom:3px;">Reset Confirmed Decisions</div>
              <div style="font-size:12px;color:#7a90b8;line-height:1.45;">Clears only Accept/Reject decisions that were <em>Confirmed</em> via the Culling Assistant's finalize step. Manual (user-assigned) decisions are kept.</div>
            </div>
          </div>

          <div class="folder-opt-card" id="folderOptCardAll" style="
            display:flex;align-items:flex-start;gap:12px;padding:12px 14px;
            border:1px solid #3f2020;border-radius:8px;background:#2a1a1a;
            cursor:pointer;margin-bottom:0;transition:border-color 0.15s,background 0.15s;">
            <div style="margin-top:2px;font-size:16px;line-height:1;color:#ff8888;">⊘</div>
            <div style="flex:1;min-width:0;">
              <div style="font-size:13px;font-weight:600;margin-bottom:3px;color:#ffc8c8;">Reset All Decisions</div>
              <div style="font-size:12px;color:#b07878;line-height:1.45;">Clears <strong style="color:#ffaaaa">all</strong> manual and confirmed Accept/Reject decisions for this folder, returning every image to Undecided. Auto-categorized decisions are unaffected.</div>
            </div>
          </div>
        </div>

        <div style="padding:10px 22px 18px;display:flex;justify-content:flex-end;border-top:1px solid #1a2235;margin-top:4px;">
          <button id="folderOptCancel" style="padding:8px 16px;border:1px solid #3a465f;background:#1c2433;color:#e8f0f8;border-radius:6px;cursor:pointer;font-size:13px;">Close</button>
        </div>
      `;
      document.body.appendChild(dlg);

      const closeAndRemove = () => {
        dlg.close();
        if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
      };

      dlg.querySelector('#folderOptCancel').addEventListener('click', closeAndRemove);

      const cardVerified = dlg.querySelector('#folderOptCardVerified');
      cardVerified.addEventListener('mouseenter', () => { cardVerified.style.borderColor = '#4d6a9a'; cardVerified.style.background = '#1e2a40'; });
      cardVerified.addEventListener('mouseleave', () => { cardVerified.style.borderColor = '#263045'; cardVerified.style.background = '#1a2235'; });
      cardVerified.addEventListener('click', () => {
        const changed = resetFolderCullState(folderPath, 'verified');
        showToast(changed > 0 ? `Reset ${changed} confirmed decision${changed === 1 ? '' : 's'}` : 'No confirmed decisions to reset', 3000);
        closeAndRemove();
      });

      const cardAll = dlg.querySelector('#folderOptCardAll');
      cardAll.addEventListener('mouseenter', () => { cardAll.style.borderColor = '#7f3f3f'; cardAll.style.background = '#361818'; });
      cardAll.addEventListener('mouseleave', () => { cardAll.style.borderColor = '#3f2020'; cardAll.style.background = '#2a1a1a'; });
      cardAll.addEventListener('click', () => {
        const ok = confirm(`Reset ALL manual and confirmed culling decisions for "${folderName}"?\n\nThis cannot be undone.`);
        if (!ok) return;
        const changed = resetFolderCullState(folderPath, 'all');
        showToast(changed > 0 ? `Reset ${changed} manual/confirmed decision${changed === 1 ? '' : 's'}` : 'No manual or confirmed decisions to reset', 3000);
        closeAndRemove();
      });

      dlg.addEventListener('close', () => { if (dlg.parentNode) dlg.parentNode.removeChild(dlg); });
      dlg.showModal();
    }

