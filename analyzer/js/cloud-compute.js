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
    //   - Startup: list pending jobs (read-only); surface unmerged packs via
    //     account-button affordance + toast — user downloads from #cloudAccountDlg.
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
    let _ccLastDoneFolderPaths = new Set();
    // JS-side terminal statuses (set by api_bridge._worker terminal handler).
    // The Worker's 'complete'|'cancelled'|'failed' normalise to 'done'|'failed'|
    // 'cancelled'; the Worker's 'incomplete' (client disconnected >10min, uploads
    // unfinished) is surfaced AS 'incomplete' — terminal locally (restart-resume
    // is deferred) but distinct from 'failed' for the badge.
    const _CC_TERMINAL_STATUSES = new Set(['done', 'failed', 'cancelled', 'incomplete']);
    const _CC_FOLDER_UNAVAILABLE_MSG =
      'Cloud Compute results are available for download, but Kestrel cannot locate the folder that you analyzed.';
    // Cosmetic-only: hides finished rows from the cloud pill; never touches the ledger.
    const _ccPanelHiddenJobIds = new Set();
    // Preserve <details> open state across panel/history re-renders.
    const _ccOpenDetailsJobIds = new Set();
    const _ccHistoryDownloads = new Map();
    let _ccHistoryDownloadPollTimer = null;
    const _CC_HISTORY_DOWNLOAD_POLL_MS = 1500;
    window._ccInProgressFolderPaths = new Set();

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
    // **Upload-sequential by design**: only one folder uploads at a time,
    // even on paid tiers with maxConcurrentJobs>=2. The bottleneck is the
    // user's outbound bandwidth, so running two uploads in parallel just
    // halves each one's throughput. Once a job's uploadComplete flips true
    // (Modal can start processing it server-side), the next folder's
    // upload kicks off and the previous folder transitions to its
    // server-side processing phase silently.
    //
    // We do NOT gate on the Worker's concurrency limit — local "is anyone
    // uploading?" is the only check. If the Worker rejects with 403
    // job_in_progress (e.g. an orphan from another device), the folder is
    // pushed back to the head of the queue and we wait for the next
    // trigger. Entitlements is consulted ONLY for the one-time orphan
    // warning at the first iteration of the session.
    let _ccMaybeStartInFlight = false;
    async function maybeStartNextCloudJob() {
      if (_ccMaybeStartInFlight) return; // serialise overlapping triggers
      if (_ccPendingSubmits.length === 0) return;
      if (!window.pywebview?.api?.cloud_compute_submit_job) return;
      _ccMaybeStartInFlight = true;
      try {
        // One-time-per-session orphan warning. Skipped after we've shown
        // it once (or if there's no entitlements endpoint). The actual
        // gating logic below is independent of this check — orphan
        // jobs from another device will manifest as 403 job_in_progress
        // on the first submit, and we handle that path too.
        if (!_ccOrphanWarningShown && window.pywebview?.api?.cloud_compute_get_entitlements) {
          try {
            const ent = await window.pywebview.api.cloud_compute_get_entitlements();
            if (ent && ent.ok) {
              const active = Array.isArray(ent.activeJobs) ? ent.activeJobs : [];
              if (active.length > 0) {
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
            }
          } catch (e) { /* failsafe */ }
        }

        // Drain loop. On every iteration we check whether any local job is
        // currently uploading; if so, bail and wait for the uploadComplete
        // trigger. If not, try to submit the next folder. On nothingToDo
        // and hard errors we continue draining (no upload was started, so
        // bandwidth isn't tied up). On success we break — the just-started
        // upload now holds the pipe.
        while (_ccPendingSubmits.length > 0) {
          if (await _ccHasActiveLocalUpload()) break;
          const next = _ccPendingSubmits.shift();
          if (!next || !next.path) continue;
          let r;
          try {
            r = await window.pywebview.api.cloud_compute_submit_job(next.path);
          } catch (e) {
            _ccPendingSubmits.unshift(next);
            showToast(`Cloud Compute: submit error, will retry. (${e?.message || e})`, 5000);
            break;
          }
          if (r && r.ok) {
            if (typeof autoLoadFolderWhenTreeEmpty === 'function') {
              autoLoadFolderWhenTreeEmpty(next.path);
            }
            // Upload started. Wait for uploadComplete (or terminal) before
            // submitting the next folder — bandwidth is the constraint.
            break;
          }
          if (r && r.ok === false && r.error === 'job_in_progress') {
            // Worker says a slot is held (orphan from another device, or
            // a race with another session). Push back and wait for the
            // next trigger.
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

    async function _ccHasActiveLocalUpload() {
      // True if any session job is currently uploading from this machine.
      // "Uploading" means non-terminal status AND uploadComplete not yet
      // observed on the cached remote snapshot. Once the desktop has
      // called /api/jobs/:id/complete and the next poll picks up
      // uploadComplete=true, the slot frees for the next upload to start —
      // the previous job continues its server-side processing without
      // holding our bandwidth.
      if (!window.pywebview?.api?.cloud_compute_list_jobs) return false;
      try {
        const r = await window.pywebview.api.cloud_compute_list_jobs();
        const jobs = (r && r.jobs) || [];
        return jobs.some(j =>
          !_CC_TERMINAL_STATUSES.has(j.status) && !j.uploadComplete
        );
      } catch { return false; }
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
      if (s === 'cancelled' || s === 'failed' || s === 'incomplete') return s;
      // Worker flipped the job to 'incomplete' (client disconnected >10min with
      // uploads unfinished): uploads are halted server-side even while the local
      // job is still draining downloads. Grey the upload half immediately.
      if (state && state.remoteStatus === 'incomplete') return 'incomplete';
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

    // An 'incomplete' job is terminal on the Worker (client vanished with
    // uploads unfinished). While the desktop drains the last result packs the
    // LOCAL job status can still read 'uploading'/'running' for a poll cycle or
    // two — but once every uploaded image has been analyzed there is no more
    // server-side work and nothing left to produce or download. Treat that as
    // terminal in the UI so we stop showing "Analyzing" and hide the
    // cancel / retrieve affordances (the job is done — just partial).
    // True when `path` is already open as a loaded folder root in the browser.
    // Shares scope with folder-tree.js (folderTreeRootOrder); guarded so a load-
    // order race or missing global degrades to "not loaded" rather than throwing.
    // Paths are normalized case-insensitively (Windows filesystem).
    function _ccIsFolderLoaded(path) {
      if (!path) return false;
      try {
        if (typeof folderTreeRootOrder === 'undefined' || !Array.isArray(folderTreeRootOrder)) return false;
        const norm = (p) => String(p || '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
        const target = norm(path);
        return folderTreeRootOrder.some((r) => norm(r) === target);
      } catch { return false; }
    }

    function _ccRemoteIncompleteDone(state) {
      if (!state || state.remoteStatus !== 'incomplete') return false;
      const uploaded = Number(state.uploadedCount || 0);
      const analyzed = Number(state.analyzedCount || 0);
      return uploaded > 0 && analyzed >= uploaded;
    }

    function _ccAnalysisPhase(state) {
      // Phase of the analysis half of the round-trip. NOTE: 'incomplete' analysis
      // continues server-side during the in-session drain, so we only show the
      // terminal 'incomplete' label once the LOCAL job is terminal OR every
      // uploaded image has been analyzed (_ccRemoteIncompleteDone) — while
      // analysis is genuinely still draining the counts below keep the bar
      // advancing.
      const s = (state && state.status) || 'running';
      if (s === 'cancelled' || s === 'failed' || s === 'done' || s === 'incomplete') return s;
      if (_ccRemoteIncompleteDone(state)) return 'incomplete';
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
        incomplete:    'Incomplete',
      })[phase] || phase;
    }

    function _ccAnalysisPhaseLabel(phase) {
      return ({
        waiting_uploads: 'Waiting for uploads',
        analyzing:       'Analyzing',
        done:            'Done',
        cancelled:       'Cancelled',
        failed:          'Failed',
        incomplete:      'Incomplete',
      })[phase] || phase;
    }

    // ── §3: terminal-reason → friendly explanation ───────────────────────
    //
    // Single source of truth mapping a job's terminal reason (worker
    // `terminal_reason`, or the local `failureReason` set by the bootstrap
    // orphan reaper) to a human-readable {title, body, severity}. Keyed so
    // it's trivial to extend when the worker grows a new terminal_reason.
    //
    //   severity 'error'  → red banner (something went wrong server-side)
    //   severity 'warn'   → neutral/amber banner (incomplete, not a failure)
    //   severity 'info'   → calm banner (expected outcome, e.g. user cancel)
    //
    // `complete` deliberately has NO entry → no banner on a clean finish.
    const _CC_REASON_MESSAGES = {
      // Local failureReason (pre-dates the worker terminal_reason arriving).
      upload_interrupted: {
        severity: 'error',
        title: 'Upload interrupted',
        body: 'The desktop app closed mid-upload, so this job could not finish. '
            + 'Server-side files have been cleaned up — re-submit the folder to start a new analysis.',
      },
      // Worker terminal_reason values.
      user_cancel: {
        severity: 'info',
        title: 'Cancelled',
        body: 'You cancelled this job. Uploaded images were removed from the server; '
            + 'any results already downloaded stay on disk.',
      },
      orphan_reaped: {
        severity: 'info',
        title: 'Cancelled — app closed mid-upload',
        body: 'This job was cancelled because the app closed while it was still uploading. '
            + 'Re-submit the folder to analyze it.',
      },
      modal_retries_exhausted: {
        severity: 'error',
        title: 'Analysis failed on the server',
        body: 'Analysis kept failing on the server (the compute containers exhausted their retries). '
            + 'Any results that completed before the failure are still available to download.',
      },
      runaway_dispatch: {
        severity: 'error',
        title: 'Analysis stopped',
        body: 'The server stopped this job after detecting a dispatch problem. '
            + 'Any results that completed are still available to download — please re-submit, '
            + 'and contact support if it happens again.',
      },
      stalled_no_container: {
        severity: 'error',
        title: 'Analysis stalled',
        body: 'The server could not keep a compute container running for this job, so analysis stalled. '
            + 'Any completed results are available to download — please re-submit.',
      },
      client_disconnected: {
        severity: 'warn',
        title: 'Job incomplete',
        body: 'The app was closed mid-upload, so not all photos finished uploading and the job '
            + 'couldn\u2019t complete. Results for the photos that did upload may still be available '
            + 'to download; re-submit the folder to analyze the rest.',
      },
      account_deletion: {
        severity: 'info',
        title: 'Cancelled',
        body: 'This job was stopped due to an account change.',
      },
    };

    // Resolve a job's terminal explanation, or null when none should show
    // (non-terminal, or a clean completion). Centralizes the precedence:
    // a known local failureReason wins (it's set before the worker reason
    // propagates), then the worker terminal_reason, then a generic per-status
    // fallback so a raw enum never leaks to the user.
    function _ccTerminalExplanation(state) {
      const status = (state && state.status) || '';
      const localReason = state && state.failureReason;
      if (localReason && _CC_REASON_MESSAGES[localReason]) {
        return _CC_REASON_MESSAGES[localReason];
      }
      const isTerminal = ['done', 'failed', 'cancelled', 'incomplete'].includes(status);
      if (!isTerminal || status === 'done') return null;
      const tr = state && state.terminalReason;
      if (tr === 'complete') return null;
      if (tr && _CC_REASON_MESSAGES[tr]) return _CC_REASON_MESSAGES[tr];
      // Terminal but no mapped reason yet (worker reason not surfaced, or an
      // unrecognised enum). Generic, friendly per-status text — no enum leaks.
      if (status === 'incomplete') return _CC_REASON_MESSAGES.client_disconnected;
      if (status === 'cancelled') {
        return {
          severity: 'info',
          title: 'Cancelled',
          body: 'This job was cancelled. Any results already downloaded stay on disk.',
        };
      }
      if (status === 'failed') {
        return {
          severity: 'error',
          title: 'Analysis failed',
          body: (state && state.error)
            ? String(state.error)
            : 'This job failed on the server. Any results that completed are available to download — '
              + 'please try re-submitting.',
        };
      }
      return null;
    }

    // ── §2: per-job "Additional information" disclosure ──────────────────
    //
    // Collapsible <details> with the support-relevant facts: Job ID (with a
    // copy button), live running-container count, folder path, and created-at
    // when known. Reusable across the live queue panel and the §4 account
    // panel's history rows. `opts`: { jobId, folderPath, activeContainerCount,
    // createdAtUtc, isTerminal }.
    function _ccRenderAdditionalInfo(opts) {
      const jobId = String(opts.jobId || '');
      if (!jobId) return '';
      const folderPath = opts.folderPath || '';
      // Containers only count while running; a terminal job is always 0.
      const containers = opts.isTerminal ? 0 : Number(opts.activeContainerCount || 0);
      const created = opts.createdAtUtc ? _ccFormatCreatedAt(opts.createdAtUtc) : '';
      const rows = [];
      rows.push(
        `<div class="cc-info-row">`
        + `<span class="cc-info-key">Job ID</span>`
        + `<span class="cc-info-val cc-info-jobid">`
        + `<code>${escapeHtml(jobId)}</code>`
        + `<button type="button" class="cc-copy-btn" data-cc-action="copy-jobid" `
        + `data-job-id="${escapeHtml(jobId)}" title="Copy Job ID">⧉ Copy</button>`
        + `</span></div>`,
      );
      rows.push(
        `<div class="cc-info-row">`
        + `<span class="cc-info-key">Running containers</span>`
        + `<span class="cc-info-val">${containers}</span></div>`,
      );
      if (folderPath) {
        rows.push(
          `<div class="cc-info-row">`
          + `<span class="cc-info-key">Folder</span>`
          + `<span class="cc-info-val cc-info-path" title="${escapeHtml(folderPath)}">${escapeHtml(folderPath)}</span></div>`,
        );
      }
      if (created) {
        rows.push(
          `<div class="cc-info-row">`
          + `<span class="cc-info-key">Created</span>`
          + `<span class="cc-info-val">${escapeHtml(created)}</span></div>`,
        );
      }
      const openAttr = _ccOpenDetailsJobIds.has(jobId) ? ' open' : '';
      return `
        <details class="cc-additional-info"${openAttr}>
          <summary>Additional information</summary>
          <div class="cc-info-body">${rows.join('')}</div>
        </details>`;
    }

    function _ccWireDetailsPersistence() {
      if (document._ccDetailsPersistenceWired) return;
      document._ccDetailsPersistenceWired = true;
      document.addEventListener('toggle', (e) => {
        const det = e.target;
        if (!(det instanceof HTMLDetailsElement) || !det.classList.contains('cc-additional-info')) return;
        const host = det.closest('[data-job-id]');
        const jobId = host && host.getAttribute('data-job-id');
        if (!jobId) return;
        if (det.open) _ccOpenDetailsJobIds.add(jobId);
        else _ccOpenDetailsJobIds.delete(jobId);
      }, true);
    }

    function _ccFormatCreatedAt(iso) {
      try {
        const d = new Date(iso);
        if (isNaN(d.getTime())) return String(iso);
        return d.toLocaleString();
      } catch { return String(iso || ''); }
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
      // §1: retrieved = images whose result pack the client has pulled back
      // from the worker (results_status='results_retrieved'). retrieved ≤
      // analyzed ≤ uploaded ≤ total by construction on the worker side.
      const rawRetrieved = Number(state.retrievedCount || 0);
      // Numerators are also clamped to the displayed total so the text never
      // shows e.g. "182/181 uploaded".
      const analyzed = total > 0 ? Math.min(rawAnalyzed, total) : rawAnalyzed;
      const uploaded = total > 0 ? Math.min(rawUploaded, total) : rawUploaded;
      const retrieved = total > 0 ? Math.min(rawRetrieved, total) : rawRetrieved;
      const uploadPhase = _ccUploadPhase(state);
      const analysisPhase = _ccAnalysisPhase(state);
      const uploadPct = total > 0 ? Math.min(100, Math.round((uploaded / total) * 100)) : 0;
      const analysisPct = total > 0 ? Math.min(100, Math.round((analyzed / total) * 100)) : 0;
      const retrievedPct = total > 0 ? Math.min(100, Math.round((retrieved / total) * 100)) : 0;
      const anchor = state.anchorFilename
        ? ` <span class="muted">(+1 anchor for scene continuity)</span>`
        : '';
      // §3: friendly per-terminal-state explanation, sourced from the
      // centralized reason map (worker terminal_reason + local failureReason).
      // Covers user-cancel, server failures, incomplete, etc. — no raw enum
      // leaks. A clean completion returns null → no banner.
      const explanation = _ccTerminalExplanation(state);
      const reasonBanner = explanation
        ? `<div class="cloud-queue-item-notice cc-sev-${explanation.severity}">`
          + `<strong>${escapeHtml(explanation.title)}</strong> ${escapeHtml(explanation.body)}`
          + `</div>`
        : '';
      // Only surface a raw error string when no mapped explanation already
      // covers it (avoids double-banners and prevents enum/stacktrace leakage
      // once a friendly reason is known).
      const err = (!explanation && state.error)
        ? `<div class="cloud-queue-item-notice cc-sev-error">${escapeHtml(String(state.error))}</div>`
        : '';

      // Staleness signal: if the backend hasn't received a successful Worker
      // response recently, show a small "syncing…" badge but DO NOT zero the
      // counters. Last-known values persist across transient failures.
      // Terminal jobs intentionally have stale timestamps (the per-job poller
      // stops once a job is done), so suppress the badge for them.
      const updatedAt = Number(state.remoteUpdatedAtMs || 0);
      const ageMs = updatedAt > 0 ? (Date.now() - updatedAt) : Infinity;
      // A drained 'incomplete' job (remoteStatus terminal + all uploaded images
      // analyzed) counts as terminal even before the local lifecycle finalizes,
      // so we hide the Retrieve-Results affordance and suppress the syncing badge.
      const isTerminal = ['done', 'failed', 'cancelled', 'incomplete'].includes(state.status)
        || _ccRemoteIncompleteDone(state);
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
      const cancelDisabled = ['done', 'failed', 'cancelled', 'incomplete'].includes(state.status)
        || _ccRemoteIncompleteDone(state);
      // Load: only offer when the folder isn't already open in the browser.
      const showLoad = !!state.rootPath && !_ccIsFolderLoaded(state.rootPath);

      return `
        <div class="queue-item cloud-queue-item" data-job-id="${escapeHtml(state.jobId)}">
          <div class="cloud-queue-item-header-row">
            <div class="queue-item-header">
              <span class="queue-item-name" title="${escapeHtml(state.rootPath || '')}">${escapeHtml(folder)}</span>
              ${staleHint}
            </div>
            <div class="cloud-queue-item-controls">
              <button data-cc-action="pause" data-job-id="${escapeHtml(state.jobId)}" ${pauseDisabled ? 'disabled' : ''}>
                ⏸ Pause
              </button>
              <button data-cc-action="resume" data-job-id="${escapeHtml(state.jobId)}" ${resumeDisabled ? 'disabled' : ''}>
                ▶ Resume
              </button>
              ${showLoad ? `<button class="cc-load-btn" data-cc-action="load" data-job-id="${escapeHtml(state.jobId)}" data-folder-path="${escapeHtml(state.rootPath)}" title="Open this folder to browse results">
                + 📂 Load
              </button>` : ''}
              <button data-cc-action="cancel" data-job-id="${escapeHtml(state.jobId)}" ${cancelDisabled ? 'disabled' : ''}>
                ⏹ Cancel
              </button>
            </div>
          </div>
          <div class="cloud-phase-row">
            <span class="cloud-phase-pill upload ${uploadPhase}">↑ ${_ccUploadPhaseLabel(uploadPhase)}</span>
            <span class="cloud-phase-pill analysis ${analysisPhase}">⚙ ${_ccAnalysisPhaseLabel(analysisPhase)}</span>
          </div>
          <div class="cloud-bar-block">
            <div class="cloud-bar-label">${uploaded} / ${total} uploaded${anchor}</div>
            <div class="queue-item-progress">
              <div class="queue-item-progress-fill upload${['incomplete','cancelled','failed'].includes(uploadPhase) ? ' halted' : ''}" style="width:${uploadPct}%"></div>
            </div>
          </div>
          <div class="cloud-bar-block">
            <div class="cloud-bar-label">${analyzed} / ${total} analyzed</div>
            <div class="queue-item-progress">
              <div class="queue-item-progress-fill analysis${['cancelled','failed'].includes(analysisPhase) ? ' halted' : ''}" style="width:${analysisPct}%"></div>
            </div>
          </div>
          <div class="cloud-bar-block">
            <div class="cloud-bar-label">${retrieved} / ${total} results retrieved</div>
            <div class="queue-item-progress">
              <div class="queue-item-progress-fill retrieved${['cancelled','failed'].includes(analysisPhase) ? ' halted' : ''}" style="width:${retrievedPct}%"></div>
            </div>
          </div>
          ${reasonBanner}
          ${err}
          ${_ccRenderAdditionalInfo({
            jobId: state.jobId,
            folderPath: state.rootPath,
            activeContainerCount: state.activeContainerCount,
            createdAtUtc: state.createdAtUtc,
            isTerminal,
          })}
        </div>
      `;
    }

    function _ccPanelBadge(jobs, pendingCount) {
      const active = jobs.filter(j => !['done', 'failed', 'cancelled', 'incomplete'].includes(j.status)).length;
      // 'incomplete' folds into the done-ish bucket for the summary count — it
      // finished its run (partially), it's not a failure.
      const done = jobs.filter(j => j.status === 'done' || j.status === 'incomplete').length;
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
        if (window._ccInProgressFolderPaths && window._ccInProgressFolderPaths.size > 0) {
          window._ccInProgressFolderPaths = new Set();
          try {
            if (typeof updateInProgressFoldersInTree === 'function') updateInProgressFoldersInTree();
          } catch {}
        }
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
      const visibleJobs = jobs.filter(j => !_ccPanelHiddenJobIds.has(j.jobId));
      body.innerHTML =
        visibleJobs.map(_ccRenderItem).join('') +
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
        if (becameTerminal && (j.status === 'done' || j.status === 'incomplete')) {
          const fp = (j.rootPath || '').replace(/\\/g, '/');
          if (fp && !_ccLastDoneFolderPaths.has(fp) && typeof persistFolderRecentsBump === 'function') {
            persistFolderRecentsBump(j.rootPath);
          }
        }
      }
      _ccLastDoneFolderPaths = new Set(
        jobs.filter(j => j.status === 'done' || j.status === 'incomplete')
          .map(j => (j.rootPath || '').replace(/\\/g, '/')),
      );
      _ccPrevJobSnapshot = new Map(jobs.map(j => [j.jobId, {
        uploadComplete: !!j.uploadComplete,
        status: j.status,
      }]));
      if (shouldRetryQueue && _ccPendingSubmits.length > 0) {
        // Fire-and-forget; maybeStartNextCloudJob serialises itself.
        maybeStartNextCloudJob();
      }

      // ── Update cloud in-progress folder set for tree hourglass indicators ─
      const norm = p => (p || '').replace(/\\/g, '/');
      const ccInProg = new Set();
      for (const j of jobs) {
        if (!_CC_TERMINAL_STATUSES.has(j.status) && j.folderPath) ccInProg.add(norm(j.folderPath));
      }
      for (const p of pending) {
        if (p.path) ccInProg.add(norm(p.path));
      }
      window._ccInProgressFolderPaths = ccInProg;
      try {
        if (typeof updateInProgressFoldersInTree === 'function') {
          updateInProgressFoldersInTree();
          if (typeof _updateAutoRefreshTimers === 'function') _updateAutoRefreshTimers();
        }
      } catch {}
      // NOTE: pack-event draining used to live here, coupled to this render
      // (so it only fired when the panel had jobs and was repainting). It now
      // runs on its own steady timer — see _ccDrainPackEvents / _ccStartPackEventDrain
      // (§5) — so a merged pack refreshes the gallery even when the user is
      // looking at the photos rather than the cloud panel.
    }

    // ── §5: pack-merged → gallery refresh (decoupled from panel render) ──
    //
    // api_bridge enqueues a {jobId, folderPath, packName} event each time a
    // result pack is merged into a folder's local kestrel DB. We drain that
    // queue on a steady timer (independent of _ccRenderPanel) and:
    //   1) rescan the containing root so the folder tree picks up newly
    //      kestrel-ized subfolders + the in-progress indicators, and
    //   2) queue the folder for a merge-preserving silent reload AND kick
    //      silentRefreshPending() immediately, so the currently-open folder's
    //      gallery shows the new scenes within one drain interval — instead
    //      of waiting on the 10s in-progress auto-refresh timer (which only
    //      exists while the folder is still flagged in-progress + checked).
    //
    // silentRefreshPending() itself only touches *checked* folders and merges
    // additively (preserving ratings / scene names / culling), so triggering
    // it here can never clobber a user edit or reload a folder the user isn't
    // viewing.
    let _ccPackEventDrainInFlight = false;
    async function _ccDrainPackEvents() {
      if (_ccPackEventDrainInFlight) return;
      if (!window.pywebview?.api?.cloud_compute_get_pack_events) return;
      _ccPackEventDrainInFlight = true;
      try {
        const evRes = await window.pywebview.api.cloud_compute_get_pack_events();
        const events = (evRes && evRes.events) || [];
        if (events.length === 0) return;
        const folders = new Set();
        for (const ev of events) if (ev && ev.folderPath) folders.add(ev.folderPath);
        let queuedAny = false;
        for (const fp of folders) {
          try {
            if (typeof _findRootContaining === 'function'
                && typeof rescanFolderRoot === 'function') {
              const root = _findRootContaining(fp);
              if (root) rescanFolderRoot(root.path);
            }
          } catch {}
          try {
            if (typeof scheduleAutoRefresh === 'function') {
              scheduleAutoRefresh(fp);
              queuedAny = true;
            }
          } catch {}
        }
        // Immediately reload the open folder rather than waiting for the next
        // in-progress timer tick. silentRefreshPending self-guards against
        // concurrent runs and filters to checked paths.
        if (queuedAny && typeof silentRefreshPending === 'function') {
          try { await silentRefreshPending(); } catch {}
        }
      } catch {
        /* transient bridge failure — next tick retries (events aren't lost
           until successfully drained on the Python side). */
      } finally {
        _ccPackEventDrainInFlight = false;
      }
    }

    // Steady drain timer. Same cadence as the panel poller; lifecycle is tied
    // to it (started/stopped alongside polling) but it runs independently of
    // whether the panel is visible or has rows.
    let _ccPackEventTimer = null;
    function _ccStartPackEventDrain() {
      if (_ccPackEventTimer) return;
      _ccDrainPackEvents(); // immediate drain
      _ccPackEventTimer = setInterval(_ccDrainPackEvents, 4000);
    }
    function _ccStopPackEventDrain() {
      if (_ccPackEventTimer) { clearInterval(_ccPackEventTimer); _ccPackEventTimer = null; }
    }

    function _ccStartPolling() {
      // The pack-event drain runs independently of the panel poller (§5) so
      // gallery refresh keeps working even if the panel render path bails.
      _ccStartPackEventDrain();
      if (_ccPollingTimer) return;
      _ccRenderPanel(); // immediate paint
      // 4s cadence: backend cache is refreshed every 5s, so a slightly
      // tighter UI tick keeps the displayed numbers within one render of
      // the latest snapshot. No Worker I/O happens on this tick.
      _ccPollingTimer = setInterval(_ccRenderPanel, 4000);
    }

    function _ccStopPolling() {
      if (_ccPollingTimer) { clearInterval(_ccPollingTimer); _ccPollingTimer = null; }
      _ccStopPackEventDrain();
    }

    // Clipboard helper with a legacy fallback — some embedded webviews don't
    // grant async-clipboard access, so fall back to a hidden textarea +
    // execCommand('copy') before giving up.
    function _ccCopyText(text) {
      const str = String(text || '');
      if (navigator.clipboard && navigator.clipboard.writeText) {
        return navigator.clipboard.writeText(str).catch(() => _ccLegacyCopy(str));
      }
      return _ccLegacyCopy(str);
    }
    function _ccLegacyCopy(str) {
      return new Promise((resolve, reject) => {
        try {
          const ta = document.createElement('textarea');
          ta.value = str;
          ta.style.position = 'fixed';
          ta.style.opacity = '0';
          document.body.appendChild(ta);
          ta.focus();
          ta.select();
          const ok = document.execCommand('copy');
          document.body.removeChild(ta);
          ok ? resolve() : reject(new Error('copy failed'));
        } catch (e) { reject(e); }
      });
    }

    // Click delegation for per-item pause/resume/cancel buttons.
    document.addEventListener('click', async (ev) => {
      const t = ev.target;
      if (!t || !(t instanceof HTMLElement)) return;
      const action = t.getAttribute('data-cc-action');
      const jobId = t.getAttribute('data-job-id');
      if (!action || !jobId) return;
      // §2: copy Job ID to the clipboard — no bridge call, handy for support.
      if (action === 'copy-jobid') {
        _ccCopyText(jobId)
          .then(() => showToast('Job ID copied', 1800))
          .catch(() => showToast('Could not copy Job ID', 2500));
        return;
      }
      // Load: open the job's folder for browsing. No bridge round-trip beyond
      // the shared loader (which reads the folder's kestrel DB).
      if (action === 'load') {
        const folderPath = t.getAttribute('data-folder-path');
        if (folderPath && typeof loadFolderIntoBrowser === 'function') {
          loadFolderIntoBrowser(folderPath);
        }
        return;
      }
      if (!window.pywebview?.api) return;
      // NB: the live pill streams results automatically while a job is present,
      // so there's no per-pill "Retrieve Results" control. Re-registering an
      // older/in-progress job into the pill is done from the account panel's
      // history rows (history-retrieve), which call cloud_compute_retrieve_results.
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

    // ── Startup surface (read-only enumerate) ───────────────────────────

    let _ccResultsReadySurfaced = false;

    function _ccJobHasUnmergedPacks(j) {
      const downloaded = new Set(j.downloadedPacks || []);
      const available = j.availablePacks || [];
      return available.some(p => p && !downloaded.has(p));
    }

    function _ccSurfaceResultsReady() {
      const accountBtn = document.getElementById('accountBtn');
      if (accountBtn) accountBtn.classList.add('has-results');
      if (!_ccResultsReadySurfaced) {
        _ccResultsReadySurfaced = true;
        showToast('Cloud results ready — open your account to download.', 6000);
      }
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
          clearBtn.disabled = true;
          try {
            let jobs = [];
            if (window.pywebview?.api?.cloud_compute_list_jobs) {
              const r = await window.pywebview.api.cloud_compute_list_jobs();
              jobs = (r && r.jobs) || [];
            }
            let n = 0;
            for (const j of jobs) {
              if (_CC_TERMINAL_STATUSES.has(j.status)) {
                _ccPanelHiddenJobIds.add(j.jobId);
                n++;
              }
            }
            showToast(
              n > 0
                ? `Hidden ${n} finished job(s) from the panel`
                : 'No finished jobs to hide',
              2500,
            );
            _ccRenderPanel();
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
            .filter(j => !['done', 'failed', 'cancelled', 'incomplete'].includes(j.status))
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
    }

    // ═══ §4: Account & Cloud Compute panel ═══════════════════════════════
    //
    // A signed-in-only modal (#cloudAccountDlg) the user opens from the
    // account button. Shows account + usage + a myaccount link, and a job
    // history list driven by cloud_compute_list_pending_jobs. Per-job actions:
    //   - "Download results" → cloud_compute_resume_download (merge pending packs)
    //   - "Locate folder…"   → folder picker → cloud_compute_relocate_job
    // Actionable rows (pending packs and/or a missing folder) sort to the top
    // so the fastest path is: open panel → see what to download → click.

    const _CC_MYACCOUNT_URL = 'https://myaccount.projectkestrel.org';
    let _ccAccountRefreshTimer = null;

    function _ccAccountDlg() { return document.getElementById('cloudAccountDlg'); }

    // Is there pending work the user can act on for this job?
    function _ccHistoryUnmerged(j) {
      const downloaded = new Set(j.downloadedPacks || []);
      const available = j.availablePacks || [];
      return available.filter(p => !downloaded.has(p));
    }
    function _ccHistoryActionable(j) {
      if (_ccHistoryUnmerged(j).length > 0) return true;
      // Missing folder + plausibly-recoverable results → actionable (relocate
      // to find out). upload_interrupted / cancelled have nothing server-side.
      if (j.folderAvailable === false) {
        if (j.failureReason === 'upload_interrupted') return false;
        if (j.status === 'cancelled') return false;
        return true;
      }
      return false;
    }

    function _ccFmtBytes(n) {
      n = Number(n) || 0;
      if (n < 1024) return n + ' B';
      const units = ['KB', 'MB', 'GB', 'TB'];
      let v = n / 1024;
      let i = 0;
      while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
      return v.toFixed(v >= 100 ? 0 : 1) + ' ' + units[i];
    }

    function _ccTrackHistoryDownload(jobId, totalPacks) {
      const n = Number(totalPacks) || 0;
      if (n <= 0) return;
      _ccHistoryDownloads.set(jobId, { total: n });
      _ccStartHistoryDownloadPoll();
    }

    function _ccStartHistoryDownloadPoll() {
      if (_ccHistoryDownloadPollTimer) return;
      const tick = async () => {
        if (_ccHistoryDownloads.size === 0) {
          clearInterval(_ccHistoryDownloadPollTimer);
          _ccHistoryDownloadPollTimer = null;
          return;
        }
        await _ccRefreshAccountHistory();
      };
      _ccHistoryDownloadPollTimer = setInterval(tick, _CC_HISTORY_DOWNLOAD_POLL_MS);
      tick();
    }

    function _ccRenderHistoryRow(j) {
      const folder = (j.folderPath || '').split(/[\\/]/).pop() || j.jobId;
      const total = Number(j.imageCount || 0);
      const retrieved = Number(j.retrievedCount || 0);
      const isTerminal = ['done', 'failed', 'cancelled', 'incomplete'].includes(j.status);
      // Nothing left to pull back — suppress the Retrieve-Results affordance on
      // a non-terminal row once every result pack has already been retrieved.
      const allRetrieved = total > 0 && retrieved >= total;
      // Reuse §3's mapping. Build a state-shaped object from the history entry.
      const explanation = _ccTerminalExplanation({
        status: j.status,
        terminalReason: j.terminalReason,
        failureReason: j.failureReason,
      });
      const reasonBanner = explanation
        ? `<div class="cloud-queue-item-notice cc-sev-${explanation.severity}">`
          + `<strong>${escapeHtml(explanation.title)}</strong> ${escapeHtml(explanation.body)}`
          + `</div>`
        : '';

      const unmerged = _ccHistoryUnmerged(j);
      const folderMissing = j.folderAvailable === false;
      const hasPending = unmerged.length > 0;
      const packsOnServer = (j.availablePacks || []).length > 0;
      const downloadedCount = (j.downloadedPacks || []).length;

      const dlTrack = _ccHistoryDownloads.get(j.jobId);
      if (dlTrack && dlTrack.total > 0 && unmerged.length === 0) {
        _ccHistoryDownloads.delete(j.jobId);
      }

      // Status caption.
      let caption;
      if (j.status === 'done') {
        caption = `Complete · ${downloadedCount} pack(s) downloaded`;
      } else if (hasPending) {
        caption = `${unmerged.length} pack(s) ready to download`;
      } else if (folderMissing && (hasPending || packsOnServer)) {
        caption = 'Results available — folder not found';
      } else if (folderMissing) {
        caption = 'Folder not found';
      } else if (isTerminal) {
        const st = String(j.status || '');
        caption = escapeHtml(`${st.charAt(0).toUpperCase()}${st.slice(1)}`);
      } else {
        caption = `In progress${j.remoteStatus ? ' · ' + escapeHtml(j.remoteStatus) : ''}`;
      }

      const progressLine = total > 0
        ? `<div class="cloud-account-row-progress">${retrieved} / ${total} results retrieved</div>`
        : '';

      let downloadProgressHtml = '';
      const activeDl = _ccHistoryDownloads.get(j.jobId);
      if (activeDl && activeDl.total > 0) {
        const done = Math.max(0, activeDl.total - unmerged.length);
        const pct = Math.min(100, Math.round((done / activeDl.total) * 100));
        downloadProgressHtml = `
          <div class="cloud-account-dl-progress">
            <div class="cloud-account-dl-progress-label">Downloading &amp; installing packs… ${done} / ${activeDl.total}</div>
            <div class="queue-item-progress">
              <div class="queue-item-progress-fill retrieved" style="width:${pct}%"></div>
            </div>
          </div>`;
      }

      const folderNotice = (folderMissing && (hasPending || packsOnServer))
        ? `<div class="cloud-queue-item-notice cc-sev-warn">${escapeHtml(_CC_FOLDER_UNAVAILABLE_MSG)}</div>`
        : '';

      const downloadDisabled = !(j.folderAvailable === true && hasPending);
      // Only surface Download when there is actually something to pull: pending
      // packs to merge, or a missing folder hiding salvageable packs (paired
      // with Locate). A fully-retrieved job has nothing to download, so the
      // button vanishes rather than greying out.
      const showDownload = hasPending || (folderMissing && packsOnServer);
      const downloadBtn = `<button type="button" class="cloud-account-dl-btn" `
        + `data-cc-action="history-download" data-job-id="${escapeHtml(j.jobId)}" `
        + `data-pending-packs="${unmerged.length}" `
        + `${downloadDisabled ? 'disabled' : ''} `
        + `title="${folderMissing ? 'Locate the folder first' : 'Download &amp; merge the pending result pack(s)'}">`
        + `⬇ Download</button>`;
      const locateBtn = folderMissing
        ? `<button type="button" class="cloud-account-locate-btn" `
          + `data-cc-action="history-locate" data-job-id="${escapeHtml(j.jobId)}" `
          + `title="Re-point this job to the folder's new location">📁 Locate folder…</button>`
        : '';
      // For a still-running job, the continuous "Retrieve Results" loop is the
      // right affordance (downloads packs as they finish); the one-shot
      // Download stays for terminal jobs with salvageable packs.
      const retrieveBtn = `<button type="button" class="cloud-account-dl-btn" `
        + `data-cc-action="history-retrieve" data-job-id="${escapeHtml(j.jobId)}" `
        + `${folderMissing ? 'disabled' : ''} `
        + `title="${folderMissing ? 'Locate the folder first' : 'Auto-download result packs as they finish'}">`
        + `⤓ Retrieve Results</button>`;
      // Load: browse this folder's results in the gallery (works for completed
      // jobs too, which only ever appear here — never as a live pill). Hidden
      // once the folder is already open in the browser.
      const loadBtn = (j.folderAvailable === true && j.folderPath && !_ccIsFolderLoaded(j.folderPath))
        ? `<button type="button" class="cloud-account-load-btn" `
          + `data-cc-action="history-load" data-job-id="${escapeHtml(j.jobId)}" `
          + `data-folder-path="${escapeHtml(j.folderPath)}" `
          + `title="Open this folder to browse results">+ 📂 Load</button>`
        : '';

      const additionalInfo = _ccRenderAdditionalInfo({
        jobId: j.jobId,
        folderPath: j.folderPath,
        activeContainerCount: j.activeContainerCount,
        createdAtUtc: j.createdAtUtc,
        isTerminal,
      });

      return `
        <div class="cloud-account-row${_ccHistoryActionable(j) ? ' cloud-account-row--actionable' : ''}${folderMissing ? ' cloud-account-row--missing' : ''}" data-job-id="${escapeHtml(j.jobId)}">
          <div class="cloud-account-row-head">
            <div class="cloud-account-row-title-block">
              <span class="cloud-account-row-folder" title="${escapeHtml(j.folderPath || '')}">${escapeHtml(folder)}</span>
              <span class="cloud-account-row-caption">${caption}</span>
            </div>
            <div class="cloud-account-row-actions">
              ${isTerminal ? (showDownload ? downloadBtn : '') : (allRetrieved ? '' : retrieveBtn)}
              ${loadBtn}
              ${locateBtn}
            </div>
          </div>
          ${progressLine}
          ${folderNotice}
          ${downloadProgressHtml}
          ${reasonBanner}
          ${additionalInfo}
        </div>`;
    }

    async function _ccLoadAccountIdentity() {
      const nameEl = document.getElementById('cloudAccountName');
      const metaEl = document.getElementById('cloudAccountMeta');
      const avatarEl = document.getElementById('cloudAccountAvatar');
      const tierEl = document.getElementById('cloudAccountTier');
      try {
        if (window.pywebview?.api?.get_perch_account) {
          const res = await window.pywebview.api.get_perch_account();
          if (res && res.success && res.account) {
            const acc = res.account;
            const display = acc.displayName || acc.display_name || acc.firstName
              || acc.first_name || acc.username || 'Signed in';
            const handle = acc.username ? '@' + acc.username : (acc.email || acc.userId || acc.user_id || '');
            if (nameEl) nameEl.textContent = display;
            if (metaEl) metaEl.textContent = handle;
            if (avatarEl) avatarEl.textContent = (String(display).trim()[0] || '?').toUpperCase();
          } else if (nameEl) {
            nameEl.textContent = 'Signed in';
          }
        }
      } catch { if (nameEl) nameEl.textContent = 'Signed in'; }

      // Cloud Compute plan + usage. Driven entirely by /v1/me/entitlements
      // (cloud_compute_get_entitlements) — the same payload MyAccount's Cloud
      // Compute card renders. This call is NOT cached on the backend, so it's
      // always fresh per identity (the old path read a 5-min-cached /api/usage
      // that lingered across account switches and showed the prior user's
      // numbers). currentUsage carries periodStart/imageCount/imagesReserved;
      // creditBalance + effectiveRemaining ride at the top level.
      await _ccRenderEntitlementsCard();
    }

    function _ccFmtN(x) { return (Number(x) || 0).toLocaleString(); }

    async function _ccRenderEntitlementsCard() {
      const tierEl   = document.getElementById('cloudAccountTier');
      const periodEl = document.getElementById('cloudAccountPeriod');
      const holdEl   = document.getElementById('cloudAccountHold');
      const concEl   = document.getElementById('cloudAccountUsageConcurrent');
      const analyzedEl = document.getElementById('ccStatAnalyzed');
      const remainingEl = document.getElementById('ccStatRemaining');
      const creditsEl = document.getElementById('ccStatCredits');
      const quotaBar = document.getElementById('ccQuotaBar');

      let ent = null;
      try {
        if (window.pywebview?.api?.cloud_compute_get_entitlements) {
          const r = await window.pywebview.api.cloud_compute_get_entitlements();
          if (r && r.ok) ent = r;
        }
      } catch {}

      const limits = (ent && ent.limits) || null;
      const usage = (ent && ent.currentUsage) || null;

      // Plan tier.
      const tier = ent && ent.tier ? String(ent.tier) : null;
      if (tierEl) tierEl.textContent = tier
        ? `Plan: ${tier.charAt(0).toUpperCase()}${tier.slice(1)}`
        : '';

      // Billing period — "start – renewal" from currentUsage.periodStart.
      if (periodEl) periodEl.textContent = _ccBillingPeriodLabel(usage && usage.periodStart);

      // Account-hold banner.
      if (holdEl) holdEl.hidden = !(ent && ent.suspended);

      // Concurrent-job limit.
      const maxConcurrent = limits
        ? (limits.maxConcurrentJobs ?? limits.max_concurrent_jobs ?? null)
        : null;
      if (concEl) concEl.textContent = (maxConcurrent != null)
        ? `Concurrent job limit: ${maxConcurrent}` : '';

      const cap = limits ? (Number(limits.maxImagesAnalyzedMonthly) || 0) : 0;
      const used = usage ? (Number(usage.imageCount) || 0) : 0;
      const reserved = usage ? (Number(usage.imagesReserved) || 0) : 0;

      // Headline stats.
      if (analyzedEl) analyzedEl.textContent = usage ? _ccFmtN(used) : '—';
      if (remainingEl) {
        // Prefer effectiveRemaining (subscription headroom + credits). Unbounded
        // cap with no credit concept shows "Unlimited".
        if (ent && typeof ent.effectiveRemaining === 'number') {
          remainingEl.textContent = _ccFmtN(ent.effectiveRemaining);
        } else if (cap > 0 && usage) {
          remainingEl.textContent = _ccFmtN(Math.max(0, cap - used - reserved));
        } else if (usage && cap === 0) {
          remainingEl.textContent = 'Unlimited';
        } else {
          remainingEl.textContent = '—';
        }
      }
      if (creditsEl) {
        creditsEl.textContent = (ent && typeof ent.creditBalance === 'number')
          ? _ccFmtN(ent.creditBalance) : '—';
      }

      // Quota bar — two segments: used (green) + in-flight reservations (blue).
      if (quotaBar) {
        if (cap > 0 && usage) {
          const usedPct = Math.min(100, (used / cap) * 100);
          const reservedPct = Math.min(100 - usedPct, (reserved / cap) * 100);
          // Floor any non-zero segment to a visible sliver; legend carries the
          // precise counts.
          const vis = (p, v) => (v > 0 && p < 1.2) ? 1.2 : p;
          const fill = document.getElementById('ccQuotaFill');
          const fillRes = document.getElementById('ccQuotaFillReserved');
          if (fill) fill.style.width = vis(usedPct, used).toFixed(2) + '%';
          if (fillRes) fillRes.style.width = vis(reservedPct, reserved).toFixed(2) + '%';
          const usedLabel = reserved > 0
            ? `${_ccFmtN(used)} used · ${_ccFmtN(reserved)} in-progress`
            : `${_ccFmtN(used)} used`;
          const usedEl = document.getElementById('ccQuotaUsed');
          const capEl = document.getElementById('ccQuotaCap');
          if (usedEl) usedEl.textContent = usedLabel;
          if (capEl) capEl.textContent = `${_ccFmtN(cap)} quota`;
          quotaBar.hidden = false;
        } else {
          quotaBar.hidden = true;
        }
      }
    }

    // Render a billing period as "start – renewal" (start + 1 month). Mirrors
    // MyAccount's billingPeriodLabel. Falls back to '' when start is unknown.
    function _ccBillingPeriodLabel(periodStart) {
      if (!periodStart) return '';
      try {
        const start = new Date(periodStart);
        if (isNaN(start.getTime())) return '';
        const end = new Date(start);
        end.setMonth(end.getMonth() + 1);
        const fmt = (d) => d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
        return `Billing period: ${fmt(start)} – ${fmt(end)}`;
      } catch { return ''; }
    }

    async function _ccLoadPerchAccountSection() {
      const perchesEl = document.getElementById('perchAccountUsagePerches');
      const imagesEl = document.getElementById('perchAccountUsageImages');
      const assetsEl = document.getElementById('perchAccountUsageAssets');
      const bytesEl = document.getElementById('perchAccountUsageBytes');
      const listEl = document.getElementById('perchAccountList');
      if (!listEl) return;

      if (window.pywebview?.api?.get_perch_usage) {
        try {
          const u = await window.pywebview.api.get_perch_usage();
          if (u && u.success && u.usage) {
            const usage = u.usage;
            const fmtN = (x) => (Number(x) || 0).toLocaleString();
            if (perchesEl) {
              perchesEl.textContent = `${fmtN(usage.perchCount)} perch(es)`;
            }
            if (imagesEl) imagesEl.textContent = `${fmtN(usage.totalImages)} photo(s) uploaded`;
            if (assetsEl) assetsEl.textContent = `${fmtN(usage.totalAssets)} total asset(s)`;
            if (bytesEl) bytesEl.textContent = `${_ccFmtBytes(usage.totalBytes)} storage used`;
          } else {
            if (perchesEl) perchesEl.textContent = 'Usage unavailable';
          }
        } catch {
          if (perchesEl) perchesEl.textContent = 'Usage unavailable';
        }
      }

      if (!window.pywebview?.api?.get_perch_list) {
        listEl.innerHTML = '<div class="cloud-account-history-empty">Perch list unavailable in this build.</div>';
        return;
      }
      try {
        const r = await window.pywebview.api.get_perch_list(200);
        const perches = (r && r.success && Array.isArray(r.perches)) ? r.perches : [];
        if (perches.length === 0) {
          listEl.innerHTML = '<div class="cloud-account-history-empty">No perches yet.</div>';
          return;
        }
        listEl.innerHTML = perches.map((p) => {
          const title = escapeHtml(p.title || '(untitled)');
          const state = String(p.uploadState || p.status || '—');
          const stateCls = state === 'complete' ? ' complete' : '';
          const bytes = _ccFmtBytes(p.actualBytes);
          const imgs = (Number(p.imageCount) || 0).toLocaleString();
          return `
            <div class="perch-account-row">
              <span class="perch-account-row-name" title="${title}">${title}</span>
              <span class="perch-account-row-meta">${imgs} photos · ${bytes}</span>
              <span class="perch-account-row-state${stateCls}">${escapeHtml(state)}</span>
            </div>`;
        }).join('');
      } catch {
        listEl.innerHTML = '<div class="cloud-account-history-empty">Could not load perches.</div>';
      }
    }

    async function _ccRefreshAccountHistory() {
      const listEl = document.getElementById('cloudAccountHistory');
      const hintEl = document.getElementById('cloudAccountHistoryHint');
      if (!listEl) return;
      if (!window.pywebview?.api?.cloud_compute_list_pending_jobs) {
        listEl.innerHTML = '<div class="cloud-account-history-empty">Job history unavailable in this build.</div>';
        return;
      }
      let r;
      try {
        // include_terminal=true: also surface salvageable packs on terminal
        // failed/cancelled/incomplete jobs (bounded server-side). This is the
        // account panel only — the startup resume dialog still calls with the
        // default (terminal-skipping) behavior.
        r = await window.pywebview.api.cloud_compute_list_pending_jobs(true);
      } catch {
        listEl.innerHTML = '<div class="cloud-account-history-empty">Could not load job history.</div>';
        return;
      }
      const jobs = (r && r.ok && Array.isArray(r.jobs)) ? r.jobs.slice() : [];
      if (jobs.length === 0) {
        listEl.innerHTML = '<div class="cloud-account-history-empty">No cloud analysis jobs yet.</div>';
        if (hintEl) hintEl.textContent = '';
        return;
      }
      // Sort: actionable first, then newest createdAtUtc first.
      jobs.sort((a, b) => {
        const aAct = _ccHistoryActionable(a) ? 1 : 0;
        const bAct = _ccHistoryActionable(b) ? 1 : 0;
        if (aAct !== bAct) return bAct - aAct;
        const aT = a.createdAtUtc || '';
        const bT = b.createdAtUtc || '';
        return bT < aT ? -1 : (bT > aT ? 1 : 0);
      });
      const actionableCount = jobs.filter(_ccHistoryActionable).length;
      if (hintEl) {
        hintEl.textContent = actionableCount > 0
          ? `${actionableCount} need${actionableCount === 1 ? 's' : ''} your attention`
          : '';
      }
      listEl.innerHTML = jobs.map(_ccRenderHistoryRow).join('');
    }

    async function _ccLocateFolderForJob(jobId, btn) {
      if (!window.pywebview?.api?.choose_directory || !window.pywebview?.api?.cloud_compute_relocate_job) {
        showToast('Folder relocation unavailable in this build.', 4000);
        return;
      }
      let chosen;
      try {
        chosen = await window.pywebview.api.choose_directory();
      } catch { chosen = null; }
      if (!chosen) return; // user cancelled
      if (btn) btn.disabled = true;
      try {
        const r = await window.pywebview.api.cloud_compute_relocate_job(jobId, chosen);
        if (r && r.ok) {
          showToast('Folder re-pointed. You can download results now.', 3500);
          await _ccRefreshAccountHistory();
        } else {
          showToast(`Could not use that folder: ${r?.error || 'unknown error'}`, 5000);
          if (btn) btn.disabled = false;
        }
      } catch (e) {
        showToast(`Locate folder failed: ${e?.message || e}`, 5000);
        if (btn) btn.disabled = false;
      }
    }

    function _ccWireAccountPanel() {
      const dlg = _ccAccountDlg();
      if (!dlg || dlg._ccWired) return;
      dlg._ccWired = true;
      document.getElementById('cloudAccountClose')?.addEventListener('click', closeCloudAccountPanel);
      // Backdrop click / Esc close.
      dlg.addEventListener('cancel', () => closeCloudAccountPanel());
      dlg.addEventListener('click', (e) => {
        if (e.target === dlg) closeCloudAccountPanel(); // click on backdrop
      });
      document.getElementById('cloudAccountManageBtn')?.addEventListener('click', () => {
        if (window.pywebview?.api?.open_url) {
          try { window.pywebview.api.open_url(_CC_MYACCOUNT_URL); } catch {}
        }
      });
      // Sign out of the Perch/Cloud session from within the app. Python clears
      // the keychain + revokes the refresh token, then fires window.onAuthSignOut
      // (auth.js) which resets the account button and closes this panel.
      document.getElementById('cloudAccountSignOutBtn')?.addEventListener('click', async (e) => {
        const btn = e.currentTarget;
        if (!window.pywebview?.api?.sign_out) {
          showToast('Sign out unavailable in this build.', 4000);
          return;
        }
        btn.disabled = true;
        try {
          const r = await window.pywebview.api.sign_out();
          if (r && r.success) {
            showToast('Signed out.', 3000);
            closeCloudAccountPanel();
          } else {
            showToast(`Sign out failed: ${r?.error || 'unknown error'}`, 5000);
            btn.disabled = false;
          }
        } catch (err) {
          showToast(`Sign out failed: ${err?.message || err}`, 5000);
          btn.disabled = false;
        }
      });
      // Delegated per-row actions (download / locate).
      dlg.addEventListener('click', async (e) => {
        const t = e.target;
        if (!t || !(t instanceof HTMLElement)) return;
        const action = t.getAttribute('data-cc-action');
        const jobId = t.getAttribute('data-job-id');
        if (!action || !jobId) return;
        if (action === 'copy-jobid') return; // handled by the global delegate
        if (action === 'history-download') {
          const pendingPacks = Number(t.getAttribute('data-pending-packs') || 0);
          t.disabled = true;
          try {
            const r = await window.pywebview.api.cloud_compute_resume_download(jobId);
            if (r && r.ok) {
              if (pendingPacks > 0) _ccTrackHistoryDownload(jobId, pendingPacks);
              showToast('Downloading results…', 3000);
              _ccStartPolling();
              await _ccRefreshAccountHistory();
            } else if (r && r.reason === 'folder_unavailable') {
              showToast(_CC_FOLDER_UNAVAILABLE_MSG, 6000);
              await _ccRefreshAccountHistory();
              t.disabled = false;
            } else {
              showToast(`Download failed: ${r?.error || 'unknown error'}`, 5000);
              t.disabled = false;
            }
          } catch (err) {
            showToast(`Download failed: ${err?.message || err}`, 5000);
            t.disabled = false;
          }
          return;
        }
        if (action === 'history-retrieve') {
          // Continuous retrieval: re-registers the job into the live cloud
          // pill and keeps downloading packs until the job completes.
          t.disabled = true;
          try {
            const r = await window.pywebview.api.cloud_compute_retrieve_results(jobId);
            if (r && r.ok) {
              showToast(r.alreadyRunning ? 'Already retrieving results…' : 'Retrieving results…', 3000);
              // Surface the job in the live cloud pill — that's where it now
              // streams results until done. Expand the pill and close the
              // account panel so the user sees it picking up.
              _cloudQueuePanelExpanded = true;
              _ccStartPolling();
              _ccRenderPanel();
              closeCloudAccountPanel();
            } else if (r && r.reason === 'folder_unavailable') {
              showToast(_CC_FOLDER_UNAVAILABLE_MSG, 6000);
              await _ccRefreshAccountHistory();
              t.disabled = false;
            } else {
              showToast(`Retrieve failed: ${r?.error || 'unknown error'}`, 5000);
              t.disabled = false;
            }
          } catch (err) {
            showToast(`Retrieve failed: ${err?.message || err}`, 5000);
            t.disabled = false;
          }
          return;
        }
        if (action === 'history-load') {
          const folderPath = t.getAttribute('data-folder-path');
          if (folderPath && typeof loadFolderIntoBrowser === 'function') {
            loadFolderIntoBrowser(folderPath);
            showToast('Loading folder…', 2500);
            closeCloudAccountPanel();
          }
          return;
        }
        if (action === 'history-locate') {
          await _ccLocateFolderForJob(jobId, t);
          return;
        }
      });
    }

    async function openCloudAccountPanel() {
      _ccWireAccountPanel();
      const dlg = _ccAccountDlg();
      if (!dlg) return;
      try { dlg.showModal(); } catch { try { dlg.show(); } catch {} }
      _ccRefreshAccountHistory();
      _ccLoadAccountIdentity();
      _ccLoadPerchAccountSection();
      // Light refresh while open so async download progress + newly-merged
      // packs reflect without a manual reopen. Cleared on close.
      if (_ccAccountRefreshTimer) clearInterval(_ccAccountRefreshTimer);
      _ccAccountRefreshTimer = setInterval(() => {
        const d = _ccAccountDlg();
        if (!d || !d.open) { clearInterval(_ccAccountRefreshTimer); _ccAccountRefreshTimer = null; return; }
        _ccRefreshAccountHistory();
        _ccLoadPerchAccountSection();
      }, 5000);
    }

    function closeCloudAccountPanel() {
      const dlg = _ccAccountDlg();
      if (dlg && dlg.open) { try { dlg.close(); } catch {} }
      if (_ccAccountRefreshTimer) { clearInterval(_ccAccountRefreshTimer); _ccAccountRefreshTimer = null; }
      if (_ccHistoryDownloadPollTimer) {
        clearInterval(_ccHistoryDownloadPollTimer);
        _ccHistoryDownloadPollTimer = null;
      }
    }

    // Exposed for auth.js (account button) + sign-out cleanup.
    window.openCloudAccountPanel = openCloudAccountPanel;
    window.closeCloudAccountPanel = closeCloudAccountPanel;

    // Wire startup hooks. The pywebview ready event is the canonical signal
    // that the bridge is alive; we also tolerate a fallback timer in case
    // the event fired before our listener attached.
    async function _ccBootstrap() {
      _ccWireDetailsPersistence();
      _ccWirePanelControls();
      _ccWireAccountPanel();
      let hasPending = false;
      let hasAvailablePacks = false;
      try {
        if (window.pywebview?.api?.cloud_compute_list_pending_jobs) {
          const r = await window.pywebview.api.cloud_compute_list_pending_jobs();
          const jobs = (r && r.jobs) || [];
          hasPending = jobs.some(j => !['done', 'failed', 'cancelled', 'incomplete'].includes(j.status));
          hasAvailablePacks = jobs.some(_ccJobHasUnmergedPacks);
        }
      } catch {}
      if (hasAvailablePacks) _ccSurfaceResultsReady();
      if (hasPending) _ccStartPolling();
    }
    if (window.pywebview && window.pywebview.api) {
      _ccBootstrap();
    } else {
      window.addEventListener('pywebviewready', _ccBootstrap, { once: true });
      setTimeout(() => { if (window.pywebview?.api && !_ccPollingTimer) _ccBootstrap(); }, 3000);
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

