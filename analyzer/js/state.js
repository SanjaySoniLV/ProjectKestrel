    // ── Debug-gated console logging ─────────────────────────────────────────
    // Verbose diagnostic console.log lines are routed through kdebug() so
    // they're silent by default. To enable for a session, open DevTools and
    // run:  window.__KESTREL_DEBUG = true; location.reload();
    // (console.warn / console.error are NOT gated — they always fire.)
    const _KESTREL_DEBUG = !!window.__KESTREL_DEBUG;
    function kdebug(...args) { if (_KESTREL_DEBUG) console.log(...args); }

    // State (desktop mode only)
    let rootPath = '';             // Absolute path to root folder (desktop pywebview mode)
    let rows = [];                 // CSV rows (objects)
    let _scenedata = {};           // Map of rootPath → kestrel_scenedata.json contents
    let header = [];               // CSV header fields
    let scenes = [];               // Aggregated scene objects
    let dirty = false;             // Track unsaved edits
    let _dirtyRoots = new Set();   // In desktop mode, tracks which folder roots have unsaved edits
    let _dirtyRootsUnknown = false; // True when an edit can't be scoped to a specific root
    // Notify Python backend whenever dirty state changes (for close-prompt)
    function _notifyDirty(val) {
      try { if (window.pywebview?.api?.notify_dirty) window.pywebview.api.notify_dirty(!!val); } catch (_) {}
    }
    function _normalizeRootPath(v) {
      return String(v || '').trim();
    }
    function _addDirtyRoot(rootCandidate) {
      const rp = _normalizeRootPath(rootCandidate);
      if (!rp) return false;
      _dirtyRoots.add(rp);
      return true;
    }
    function _markDirtyRoots(rootHint) {
      let marked = false;
      const collect = (value) => {
        if (!value) return;
        if (typeof value === 'string') {
          marked = _addDirtyRoot(value) || marked;
          return;
        }
        if (Array.isArray(value)) {
          for (const entry of value) collect(entry);
          return;
        }
        if (value instanceof Set) {
          for (const entry of value) collect(entry);
          return;
        }
        if (typeof value !== 'object') return;
        if (Object.prototype.hasOwnProperty.call(value, '__rootPath')) {
          marked = _addDirtyRoot(value.__rootPath) || marked;
        }
        if (Object.prototype.hasOwnProperty.call(value, 'rootPath')) {
          marked = _addDirtyRoot(value.rootPath) || marked;
        }
        if (value.representative && typeof value.representative === 'object') {
          marked = _addDirtyRoot(value.representative.__rootPath) || marked;
        }
        if (Array.isArray(value.images)) {
          for (const img of value.images) collect(img);
        }
      };
      collect(rootHint);
      return marked;
    }
    function _clearDirtyRoots() {
      _dirtyRoots.clear();
      _dirtyRootsUnknown = false;
    }
    function _setDirtyUi(isDirty) {
      dirty = !!isDirty;
      _notifyDirty(dirty);
      const saveBtn = document.getElementById('saveCsv');
      if (saveBtn) saveBtn.disabled = !dirty;
      const revertBtn = document.getElementById('revertCsv');
      if (revertBtn) revertBtn.disabled = !dirty;
    }
    let _cleanSnapshot = null;      // Snapshot of rows+header at last clean state (load or save)
    let selectedSceneIds = new Set(); // Multi-select: selected scene IDs ("slot:count")
    let collapsedFolders = new Set(); // rootPaths of collapsed folder groups
    let _lastSelectedIdx = -1;        // Shift-click range: last clicked index in _visibleSceneOrder
    let _visibleSceneOrder = [];       // Flat ordered list of visible scene IDs after last render
    let _focusedCardId = null;         // Scene ID of the keyboard-focused card in the grid
    // Track which scene dialog is open for refreshing filters
    let currentSceneId = null;
    const SCENE_PREVIEW_SPLIT_KEY = 'scene_preview_split_ratio';
    const SCENE_PREVIEW_SPLIT_DEFAULT = 0.68;
    const SCENE_PREVIEW_SPLIT_MIN = 0.25;
    const SCENE_PREVIEW_SPLIT_MAX = 0.85;
    const SCENE_PREVIEW_MIN_EXPORT_PX = 180;
    const SCENE_PREVIEW_MIN_CROP_PX = 140;
    let _scenePreviewSplitRatio = SCENE_PREVIEW_SPLIT_DEFAULT;

    const el = (sel) => document.querySelector(sel);
    const sceneGrid = el('#sceneGrid');
    const imageGrid = el('#imageGrid');
    const statusEl = el('#status');
    const sceneDlg = el('#sceneDlg');
    const versionBadge = el('#versionBadge');

    let hasPywebviewApi = !!(window.pywebview && window.pywebview.api);

    // ── Global error handlers ─────────────────────────────────────────────────
    // Catch unhandled synchronous exceptions and unhandled promise rejections.
    // Forward them to the Python log via report_js_error so they appear in the
    // runtime log file even when DevTools isn't open.
    window.onerror = function (msg, source, line, col, err) {
      const payload = {
        type: 'uncaught_exception',
        msg: String(msg || '').slice(0, 500),
        source: String(source || '').slice(0, 200),
        line,
        col,
        stack: err?.stack ? String(err.stack).slice(0, 1500) : '',
      };
      console.error('[Kestrel] Uncaught JS exception:', payload);
      try {
        window.pywebview?.api?.report_js_error?.(payload)?.catch?.(() => {});
      } catch (_) {}
      return false; // don't suppress — let DevTools still see it
    };
    window.addEventListener('unhandledrejection', function (ev) {
      const reason = ev.reason;
      const payload = {
        type: 'unhandled_rejection',
        msg: String(reason?.message || reason || '').slice(0, 500),
        stack: reason?.stack ? String(reason.stack).slice(0, 1500) : '',
      };
      console.error('[Kestrel] Unhandled promise rejection:', payload);
      try {
        window.pywebview?.api?.report_js_error?.(payload)?.catch?.(() => {});
      } catch (_) {}
    });
    // ─────────────────────────────────────────────────────────────────────────

    // Debug: Log what APIs are available (initial check)
    kdebug('[init] API detection start');
    kdebug('  - Pywebview API (window.pywebview):', hasPywebviewApi);
    if (hasPywebviewApi) {
      kdebug('  - window.pywebview object:', window.pywebview);
      kdebug('  - window.pywebview.api:', window.pywebview.api);
      if (window.pywebview.api) {
        kdebug('  - Available API methods:', Object.keys(window.pywebview.api));
      }
    }

    // Pywebview API might load asynchronously, so wait for it.
    // On macOS (WKWebView) the bridge is injected later than on Windows and
    // pywebview fires a 'pywebviewready' event when it is truly available.
    // We listen for that event AND poll as a fallback, with a generous timeout.
    async function waitForPywebview() {
      if (typeof window.pywebview !== 'undefined' && window.pywebview.api) {
        return true;
      }
      return new Promise((resolve) => {
        // Hoist before settle()/onReady are defined — settle reads `elapsed`
        // in its kdebug calls, and if pywebviewready fires synchronously during
        // addEventListener registration the TDZ would throw and never resolve.
        let elapsed = 0;
        let pollTimer = null;
        let settled = false;
        function settle(found) {
          if (settled) return;
          settled = true;
          if (pollTimer !== null) clearInterval(pollTimer);
          window.removeEventListener('pywebviewready', onReady);
          if (found) {
            hasPywebviewApi = true;
            el('#compat')?.classList.add('hidden');
            kdebug('[init] Pywebview API ready (elapsed ~' + elapsed + 'ms)');
          } else {
            kdebug('[init] Pywebview API not available after ' + elapsed + 'ms');
          }
          resolve(found);
        }
        function onReady() {
          // pywebviewready fires on all platforms when the JS bridge is injected
          if (typeof window.pywebview !== 'undefined' && window.pywebview.api) {
            settle(true);
          }
        }
        window.addEventListener('pywebviewready', onReady);
        // Polling fallback — catches cases where the event already fired
        pollTimer = setInterval(() => {
          elapsed += 100;
          if (typeof window.pywebview !== 'undefined' && window.pywebview.api) {
            settle(true);
          } else if (elapsed >= 10000) {
            settle(false);
          }
        }, 100);
      });
    }

    // Desktop mode requires pywebview API at startup.
    (async function() {
      const apiReady = !hasPywebviewApi ? await waitForPywebview() : true;
      if (!apiReady) {
        setStatus('Error: Desktop API unavailable. Please relaunch Project Kestrel.');
        const compat = el('#compat');
        if (compat) compat.classList.remove('hidden');
        return;
      }
      hasPywebviewApi = true;
      // Signal the production-JS-saw-the-bridge proof to --api-probe
      // (no-op outside probe mode; report_bridge_ready is side-effect-free
      // unless Api._probe_ready_event is set on the Python side).
      try { window.pywebview?.api?.report_bridge_ready?.(); } catch (_) { }
      // After API is ready, check legal agreement
      checkLegalAgreement();
      el('#compat').classList.add('hidden');
      // After API is confirmed ready, wait for settings to be hydrated, then check donation threshold
      await new Promise(function(r) { setTimeout(r, 500); });
      // Hydrate settings from server to ensure localStorage has the latest data
      await hydrateSettingsFromServer();
      // Load species→family taxonomy map (used for auto-link, cascade, and autocomplete)
      loadSpeciesFamilyMap();
      // Then check donation threshold (after settings are loaded into localStorage)
      checkDonationThresholdOnStartup();
    })();

    // Utilities
    function setStatus(msg) { statusEl.textContent = msg; }

    // Temporary toast notification (clickable) — default 5s
    function showToast(msg, timeout = 5000, onclick) {
      try {
        // Determine where to attach the container: prefer the topmost open dialog
        const openDialogs = Array.from(document.querySelectorAll('dialog[open]'));
        let attachParent = document.body;
        if (openDialogs.length > 0) {
          // Use the last-opened dialog (assumed topmost) so toast is visible above it
          attachParent = openDialogs[openDialogs.length - 1];
        }

        let container = document.getElementById('toastContainer');
        if (!container) {
          container = document.createElement('div');
          container.id = 'toastContainer';
          // ensure basic layout
          container.style.position = 'fixed';
          container.style.right = '18px';
          container.style.bottom = '18px';
          container.style.display = 'flex';
          container.style.flexDirection = 'column';
          container.style.gap = '8px';
          container.style.zIndex = '2147483647';
          container.style.pointerEvents = 'none';
        }

        // If the container isn't in the preferred parent, move it there.
        if (container.parentNode !== attachParent) {
          attachParent.appendChild(container);
        }

        container.style.zIndex = '2147483647';

        const el = document.createElement('div');
        el.className = 'toast';
        el.textContent = msg;
        el.style.background = '#111318';
        el.style.border = '1px solid #2a3040';
        el.style.color = 'var(--text)';
        el.style.padding = '10px 14px';
        el.style.borderRadius = '8px';
        el.style.marginTop = '8px';
        el.style.pointerEvents = 'auto';
        el.style.cursor = onclick ? 'pointer' : 'default';
        el.style.minWidth = '160px';
        el.style.boxShadow = '0 6px 18px rgba(0,0,0,.6)';

        if (onclick) el.addEventListener('click', (e) => { try { onclick(e); } catch (_) { } el.remove(); });

        container.appendChild(el);
        if (timeout && timeout > 0) setTimeout(() => { try { el.remove(); } catch (_) { } }, timeout);
      } catch (e) { console.warn('showToast failed', e); }
    }

    function showLoadingAnalyzer() {
      const o = document.getElementById('loadingOverlay'); if (!o) return; o.classList.remove('hidden'); o.style.pointerEvents = 'auto';
    }
    function hideLoadingAnalyzer() {
      const o = document.getElementById('loadingOverlay'); if (!o) return; o.classList.add('hidden'); o.style.pointerEvents = 'none';
    }

    async function _waitForPipelineReady(timeoutMs = 30000) {
      const start = Date.now();
      while (Date.now() - start < timeoutMs) {
        try {
          const s = await apiGetQueueStatus();
          if (s && s.items && s.items.length > 0) {
            const cur = s.items.find(i => i.status === 'running');
            if (cur && (cur.processed > 0 || (cur.current_export_path && cur.current_export_path.length > 0))) return true;
          }
        } catch (e) { }
        await new Promise(r => setTimeout(r, 500));
      }
      return false;
    }

