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

    const sleep = (ms) => new Promise(r => setTimeout(r, ms));

    // ── Lazy image loader (throttled) ────────────────────────────────────────────
    // Concurrency-limited to avoid flooding the Python IPC bridge with dozens of
    // simultaneous read_image_file calls when a large section of the grid scrolls
    // into view.  Excess loads are queued and drained as earlier ones finish.
    const _imgLoadThrottle = { active: 0, max: 100, queue: [] };
    function _scheduleLoad(fn) {
      if (_imgLoadThrottle.active < _imgLoadThrottle.max) {
        _imgLoadThrottle.active++;
        fn().finally(() => {
          _imgLoadThrottle.active--;
          if (_imgLoadThrottle.queue.length) _scheduleLoad(_imgLoadThrottle.queue.shift());
        });
      } else {
        _imgLoadThrottle.queue.push(fn);
      }
    }

    const _lazyObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const img = entry.target;
        const loader = img._lazyLoader;
        if (loader) { _scheduleLoad(loader); delete img._lazyLoader; }
        _lazyObserver.unobserve(img);
      }
    }, { rootMargin: '300px' });

    function lazyLoadImg(img, resolverFn) {
      img._lazyLoader = async () => {
        const url = await resolverFn();
        if (url) {
          img.src = url;
          // Let the browser decode the image off the main thread before the
          // next paint, preventing jank from synchronous decode.
          try { await img.decode(); } catch (_) { /* broken/aborted image */ }
        }
      };
      _lazyObserver.observe(img);
    }
    // ── End lazy image loader ────────────────────────────────────────────────────

    // Generic debounce helper
    function debounce(fn, ms) {
      let timer;
      return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
    }

    // Version counter so concurrent renderScenes calls can bail out early
    let _renderScenesVersion = 0;
    // Version counter so loadMultipleFolders can be cancelled mid-flight
    let _loadFoldersVersion = 0;

    function loadVersionBadge() {
      if (!versionBadge) return;
      
      async function updateVersionBadge() {
        try {
          // Fetch app version from VERSION.txt
          let displayVersion = 'Version: unknown';
          try {
            const resp = await fetch('VERSION.txt', { cache: 'no-store' });
            if (resp.ok) {
              const text = await resp.text();
              const lines = text.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
              if (lines.length > 0) {
                const firstLine = lines[0];
                if (firstLine.toLowerCase().startsWith('version')) {
                  displayVersion = firstLine;
                } else {
                  displayVersion = `Version: ${firstLine}`;
                }
              }
            }
          } catch (e) {
            console.error('[loadVersionBadge] Failed to fetch VERSION.txt:', e);
          }
          
          // Fetch pipeline version from config.py via API
          if (hasPywebviewApi && window.pywebview?.api?.get_app_version) {
            try {
              const result = await window.pywebview.api.get_app_version();
              const pipelineVersion = result?.version || result;
              if (pipelineVersion && pipelineVersion !== 'unknown') {
                displayVersion += ` | Pipeline Version: ${pipelineVersion}`;
              }
            } catch (e) {
              console.error('[loadVersionBadge] Failed to fetch pipeline version:', e);
            }
          }
          
          versionBadge.textContent = displayVersion;
        } catch (e) {
          console.error('[loadVersionBadge] Unexpected error:', e);
          versionBadge.textContent = 'Version: error';
        }
      }
      
      // If API is not ready yet, wait for it
      if (!hasPywebviewApi) {
        waitForPywebview().then(() => updateVersionBadge());
      } else {
        updateVersionBadge();
      }
      
      // Check for new versions from remote JSON endpoint but we need pywebview to be ready, 
      // so listen for the event or execute immediately if already mounted
      if (window.pywebview?.api) {
        checkRemoteVersion();
      } else {
        window.addEventListener('pywebviewready', checkRemoteVersion);
      }
    }

    // Check if running as Windows Store app
    async function isWindowsStoreApp() {
      try {
        if (!window.pywebview?.api?.is_windows_store_app) return false;
        const result = await window.pywebview.api.is_windows_store_app();
        return result?.is_store ?? false;
      } catch (e) {
        return false;
      }
    }

    // Get platform info
    async function getPlatformInfo() {
      try {
        if (!window.pywebview?.api?.get_platform_info) {
          // Fallback to client-side detection
          if (navigator.platform.includes('Mac')) return 'macos';
          if (navigator.platform.includes('Win')) return 'windows';
          return 'windows'; // default
        }
        const result = await window.pywebview.api.get_platform_info();
        return result?.platform ?? 'windows';
      } catch (e) {
        return 'windows';
      }
    }

    // Check remote version from JSON endpoint
    async function checkRemoteVersion() {
      try {
        // Read current app version from VERSION.txt
        let currentVer = _appVersion;
        if (!currentVer) {
          try {
            const versionResp = await fetch('VERSION.txt', { cache: 'no-store' });
            if (versionResp.ok) {
              const text = await versionResp.text();
              const lines = text.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
              if (lines.length > 0) {
                // Extract just the version part (e.g., "v(Swamp Sparrow)" from "Version: v(Swamp Sparrow)")
                const line = lines[0];
                currentVer = line.toLowerCase().startsWith('version') 
                  ? line.replace(/^version:\s*/i, '').trim()
                  : line;
              }
            }
          } catch (e) { /* ignore */ }
        }
        
        let versionList;
        if (window.pywebview?.api?.fetch_remote_version) {
          const res = await window.pywebview.api.fetch_remote_version();
          if (res && res.success && res.data) {
            versionList = res.data;
          }
        }
        
        if (!versionList) {
          const resp = await fetch('https://projectkestrel.org/version.json', { cache: 'no-store' });
          if (!resp.ok) return;
          versionList = await resp.json();
        }
        
        if (!Array.isArray(versionList) || versionList.length === 0) return;
        
        const latestVersion = versionList[0]; // first entry is latest
        
        // Compare versions: check if latest name differs from current
        if (!latestVersion.name) return;
        const normalizedLocal = (currentVer || '').replace(/^v\(/ig, '').replace(/\)$/g, '').trim();
        const normalizedRemote = latestVersion.name.replace(/^v\(/ig, '').replace(/\)$/g, '').trim();
        
        if (normalizedRemote === normalizedLocal) return;
        
        // Show update notification
        showVersionUpdateNotification(latestVersion);
      } catch (e) {
        // Network error, offline mode - ignore
      }
    }

    // Display the version update notification as a toast
    async function showVersionUpdateNotification(versionInfo) {
      kdebug('[init] Showing version update notification for version:', versionInfo);
      const toast = document.getElementById('versionUpdateToast');
      if (!toast) return;
      
      const platform = await getPlatformInfo();
      const isStore = platform === 'windows' ? await isWindowsStoreApp() : false;
      
      // Priority symbol
      const priorityEl = document.getElementById('versionUpdatePriority');
      if (priorityEl) priorityEl.textContent = versionInfo.highPriority ? '⭐' : '•';
      
      // Title
      const titleEl = document.getElementById('versionUpdateTitle');
      if (titleEl) titleEl.textContent = `Update Available: ${versionInfo.name}`;
      
      // Changelog notes (show first 3)
      const notesEl = document.getElementById('versionUpdateNotes');
      if (notesEl && versionInfo.notes && Array.isArray(versionInfo.notes)) {
        notesEl.innerHTML = '';
        versionInfo.notes.slice(0, 3).forEach(note => {
          const li = document.createElement('li');
          li.textContent = note;
          notesEl.appendChild(li);
        });
      }
      
      // Windows-specific note (only show for Windows users)
      const windowsNoteEl = document.getElementById('versionUpdateWindowsNote');
      if (windowsNoteEl) {
        if (platform === 'windows') {
          windowsNoteEl.innerHTML = 'Windows users: Check for updates in the Microsoft Store within 1-3 days. If you used the traditional installer to install Kestrel, visit <a href="https://projectkestrel.org/download" target="_blank" style="color:#7ca3d9;text-decoration:underline;">projectkestrel.org/download</a> to manually update.';
          windowsNoteEl.style.display = 'block';
        } else {
          windowsNoteEl.style.display = 'none';
        }
      }
      
      // Download button
      const downloadBtn = document.getElementById('versionUpdateDownloadBtn');
      if (downloadBtn) {
        downloadBtn.href = `https://projectkestrel.org/download?platform=${platform}`;
        downloadBtn.textContent = platform === 'macos' ? 'Go to MacOS Download' : 'Go to Windows Download';
        downloadBtn.onclick = (e) => {
          e.preventDefault();
          window.open(`https://projectkestrel.org/download?platform=${platform}`, '_blank');
        };
      }
      
      // Close button
      const closeBtn = document.getElementById('versionUpdateClose');
      if (closeBtn) {
        closeBtn.onclick = () => {
          toast.style.display = 'none';
        };
      }
      
      // Show the toast
      toast.style.display = 'block';
      
      // Auto-hide after 10 seconds
      setTimeout(() => {
        if (toast.style.display === 'block') {
          toast.style.display = 'none';
        }
      }, 60000);
    }

    // Tooltip layer so tips can render over the main image area
    (function initTooltips() {
      const tipEl = document.createElement('div');
      tipEl.className = 'tooltip-layer';
      document.body.appendChild(tipEl);

      function positionTip(anchor) {
        const pad = 10;
        const rect = anchor.getBoundingClientRect();
        const vw = window.innerWidth;
        const vh = window.innerHeight;
        const box = tipEl.getBoundingClientRect();

        let left = rect.left;
        let top = rect.top - box.height - 10;
        if (left + box.width + pad > vw) left = vw - box.width - pad;
        if (left < pad) left = pad;
        if (top < pad) top = rect.bottom + 10;
        if (top + box.height + pad > vh) top = vh - box.height - pad;

        tipEl.style.left = left + 'px';
        tipEl.style.top = top + 'px';
      }

      function showTip(e) {
        const tip = e.currentTarget.getAttribute('data-tip');
        if (!tip) return;
        tipEl.textContent = tip;
        tipEl.classList.add('visible');
        positionTip(e.currentTarget);
      }

      function hideTip() {
        tipEl.classList.remove('visible');
      }

      document.querySelectorAll('.help-tip').forEach((el) => {
        el.addEventListener('mouseenter', showTip);
        el.addEventListener('mousemove', (e) => positionTip(e.currentTarget));
        el.addEventListener('mouseleave', hideTip);
        el.addEventListener('blur', hideTip);
      });
    })();

    // Simple UI zoom controls (Chromium webviews support CSS zoom)
    let uiZoom = 1;
    function applyZoom() {
      const zoomEl = document.getElementById('mainZoom');
      if (!zoomEl) return;
      const z = uiZoom;
      // Use non-transform zoom so position:sticky on headers continues to work.
      // Prefer CSS `zoom` when available; fall back to transform-only if not supported.
      try {
        zoomEl.style.zoom = z.toFixed(2);
        // Ensure no transform is set (transform can break sticky behavior)
        zoomEl.style.transform = '';
        zoomEl.style.width = '';
        zoomEl.style.height = '';
      } catch (e) {
        // Fallback: use transform if browser doesn't support zoom (sticky may be affected)
        const s = uiZoom.toFixed(2);
        zoomEl.style.transform = `scale(${s})`;
        zoomEl.style.width = `calc(100% / ${s})`;
        zoomEl.style.height = `calc(100% / ${s})`;
      }
    }

    function sanitizePath(p) {
      if (!p) return '';
      // Normalize to forward slashes, trim quotes
      return String(p).replace(/^\"|\"$/g, '').replace(/\\/g, '/');
    }

    function joinPath(a, b) {
      a = sanitizePath(a); b = sanitizePath(b);
      if (!a) return b; if (!b) return a;
      return a.replace(/\/$/, '') + '/' + b.replace(/^\//, '');
    }

    function parseCsvText(text) {
      if (!window.KestrelCsv || typeof window.KestrelCsv.parse !== 'function') {
        throw new Error('CSV parser unavailable (csv_parser.js not loaded)');
      }
      return window.KestrelCsv.parse(text, { header: true, skipEmptyLines: true });
    }


    // Blob URL cache per path
    const blobUrlCache = new Map();

    /** Convert a base64 string to a Blob Object URL.  Unlike data: URIs, blob:
     *  URLs are decoded asynchronously by the browser's image-decode thread,
     *  keeping the main thread free during scroll. */
    function _base64ToBlobUrl(b64, mime) {
      const bin = atob(b64);
      const buf = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
      return URL.createObjectURL(new Blob([buf], { type: mime || 'image/jpeg' }));
    }

    async function getBlobUrlForPath(relOrAbsPath, rootOverride) {
      if (!relOrAbsPath) return null;
      const effectiveRoot = rootOverride || rootPath;

      // Normalize separators immediately (cheap, matches culling.html behaviour)
      const rel = String(relOrAbsPath).replace(/\\/g, '/');

      // Cache check first — avoids ALL further work on already-loaded images
      // (this is the hot path during scroll-back through cached thumbnails)
      const cacheKey = `${effectiveRoot}:${rel}`;
      if (blobUrlCache.has(cacheKey)) return blobUrlCache.get(cacheKey);

      // PRIORITY 1: Python API (desktop app - all platforms)
      if (hasPywebviewApi && window.pywebview?.api?.read_image_file && effectiveRoot) {
        try {
          const result = await window.pywebview.api.read_image_file(rel, effectiveRoot);
          if (result && result.success && result.data) {
            // Use a blob: URL instead of a data: URL so the browser can decode
            // the image asynchronously on its decode thread rather than blocking
            // the main thread with synchronous base64 + JPEG/PNG parsing.
            const blobUrl = _base64ToBlobUrl(result.data, result.mime);
            blobUrlCache.set(cacheKey, blobUrl);
            return blobUrl;
          }
        } catch (e) {
          console.error('Python API image read failed:', e);
          return null;
        }
      }

      return null;
    }

    function parseNumber(v) {
      const n = parseFloat(v);
      return Number.isFinite(n) ? n : -1;
    }

    function parseCaptureTimeMs(v) {
      if (v == null) return Number.NaN;
      const raw = String(v).trim();
      if (!raw) return Number.NaN;
      let d = new Date(raw);
      if (isNaN(d)) d = new Date(raw.replace(' ', 'T'));
      const ms = d.getTime();
      return Number.isFinite(ms) ? ms : Number.NaN;
    }

    // Parse secondary species columns.
    // New format: JSON array strings e.g. '["Greater Yellowlegs","Vaux\'s Swift"]'.
    // Legacy format: numpy str() repr e.g. "[\'Greater Yellowlegs\' \"Vaux\'s Swift\"]".
    function parseSecondarySpecies(row) {
      if (row.__secondaryCache) return row.__secondaryCache;
      const listRaw = row.secondary_species_list;
      const scoresRaw = row.secondary_species_scores;
      const result = [];
      if (!listRaw || !scoresRaw) { row.__secondaryCache = result; return result; }
      try {
        let species = null, nums = null;
        // Try JSON first (new format)
        try {
          const ls = String(listRaw).trim();
          const ss = String(scoresRaw).trim();
          if (ls.startsWith('[') && ss.startsWith('[')) {
            const parsed = JSON.parse(ls);
            const parsedScores = JSON.parse(ss);
            if (Array.isArray(parsed) && Array.isArray(parsedScores)) {
              species = parsed; nums = parsedScores;
            }
          }
        } catch (_) { species = null; nums = null; }

        if (species === null) {
          // Legacy numpy repr fallback: numpy uses "..." for names with apostrophes
          species = [];
          const listStr = String(listRaw).replace(/\n\s*/g, ' ');
          const dqRe = /"([^"]+)"/g;
          const sqRe = /'([^']+)'/g;
          let m;
          while ((m = dqRe.exec(listStr)) !== null) { const n = m[1].trim(); if (n) species.push(n); }
          if (!species.length) {
            while ((m = sqRe.exec(listStr)) !== null) { const n = m[1].trim(); if (n) species.push(n); }
          }
          if (!species.length) {
            const inner = listStr.replace(/^\s*\[/, '').replace(/\]\s*$/, '');
            inner.split(/\s{2,}|\s/).forEach(tok => { const t = tok.trim(); if (t) species.push(t); });
          }
          const scoreStr = String(scoresRaw).replace(/^[^\[]*\[/, '[').replace(/\].*$/, '').replace(/[\[\]]/g, '').trim();
          nums = scoreStr.split(/\s+/).map(parseNumber).filter(x => x >= 0);
        }
        for (let i = 0; i < species.length && i < nums.length; i++) {
          result.push({ name: String(species[i]), score: parseNumber(nums[i]) });
        }
      } catch (_) { }
      row.__secondaryCache = result;
      return result;
    }

    // Parse secondary family columns similar to secondary species.
    function parseSecondaryFamilies(row) {
      if (row.__secondaryFamilyCache) return row.__secondaryFamilyCache;
      const listRaw = row.secondary_family_list;
      const scoresRaw = row.secondary_family_scores;
      const result = [];
      if (!listRaw || !scoresRaw) { row.__secondaryFamilyCache = result; return result; }
      try {
        let fams = null, nums = null;
        // Try JSON first
        try {
          const ls = String(listRaw).trim();
          const ss = String(scoresRaw).trim();
          if (ls.startsWith('[') && ss.startsWith('[')) {
            const parsed = JSON.parse(ls);
            const parsedScores = JSON.parse(ss);
            if (Array.isArray(parsed) && Array.isArray(parsedScores)) {
              fams = parsed; nums = parsedScores;
            }
          }
        } catch (_) { fams = null; nums = null; }

        if (fams === null) {
          // Legacy numpy repr fallback
          fams = [];
          const listStr = String(listRaw).replace(/\n\s*/g, ' ');
          const dqRe = /"([^"]+)"/g;
          const sqRe = /'([^']+)'/g;
          let m;
          while ((m = dqRe.exec(listStr)) !== null) { const n = m[1].trim(); if (n) fams.push(n); }
          if (!fams.length) {
            while ((m = sqRe.exec(listStr)) !== null) { const n = m[1].trim(); if (n) fams.push(n); }
          }
          if (!fams.length) {
            const inner = listStr.replace(/^\s*\[/, '').replace(/\]\s*$/, '');
            inner.split(/\s{2,}|\s/).forEach(tok => { const t = tok.trim(); if (t) fams.push(t); });
          }
          const scoreStr = String(scoresRaw).replace(/^[^\[]*\[/, '[').replace(/\].*$/, '').replace(/[\[\]]/g, '').trim();
          nums = scoreStr.split(/\s+/).map(parseNumber).filter(x => x >= 0);
        }
        for (let i = 0; i < fams.length && i < nums.length; i++) {
          result.push({ name: String(fams[i]), score: parseNumber(nums[i]) });
        }
      } catch (_) { }
      row.__secondaryFamilyCache = result;
      return result;
    }

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

    function _collectCurrentlyVisibleSceneTags(sceneId) {
      const thresholdEl = el('#speciesConf');
      const confThreshold = thresholdEl ? (parseFloat(thresholdEl.value) || 0) : 0;
      const includeSecondaryCheckbox = document.getElementById('includeSecondarySpecies');
      const includeSecondary = includeSecondaryCheckbox ? includeSecondaryCheckbox.checked : !!getSetting('includeSecondarySpecies', false);
      return _computeSceneTagsFromRows(getSceneRows(sceneId), confThreshold, includeSecondary, true);
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
    function isManualRated(r) { return getRating(r) > 0 && getOrigin(r) === 'manual'; }

    function isManualCullDecision(r) {
      const status = getCullStatus(r);
      return status === 'accept' || status === 'reject';
    }

    function hasReviewedSceneMetadata(scene) {
      if (!scene) return false;
      if (scene.isApproved) return true;
      return String(scene.sceneName || '').trim().length > 0;
    }

    function isManuallyReviewedScene(scene) {
      if (!scene) return false;
      if (hasReviewedSceneMetadata(scene)) return true;
      return Array.isArray(scene.images)
        && scene.images.some(r => isManualRated(r) || isManualCullDecision(r));
    }

    function aggregateScenes(minSpeciesConf, searchTerm, sortBy, includeSecondary, includeFamilies) {
      const groups = new Map();
      for (const r of rows) {
        // Prefix with folderSlot so scenes from different folders never collide
        const id = (r.__folderSlot != null ? r.__folderSlot + ':' : '') + r.scene_count;
        if (!groups.has(id)) groups.set(id, []);
        groups.get(id).push(r);
      }

      const list = [];
      for (const [sceneId, arr] of groups) {
        // representative by max quality
        let rep = arr[0];
        for (const r of arr) if (parseNumber(r.quality) > parseNumber(rep.quality)) rep = r;

        const computedTags = _computeSceneTagsFromRows(arr, minSpeciesConf, includeSecondary, includeFamilies);
        let species = computedTags.species.slice();
        if (includeFamilies) {
          const merged = new Set([...species, ...computedTags.families]);
          species = Array.from(merged).sort();
        }

        const maxQ = Math.max(...arr.map(a => parseNumber(a.quality)));
        const captureMsList = arr.map(a => parseCaptureTimeMs(a.capture_time)).filter(Number.isFinite);
        const captureTimeMs = captureMsList.length ? Math.min(...captureMsList) : Number.POSITIVE_INFINITY;
        const rowRp = arr[0]?.__rootPath || rootPath || '';
        const rowSc = arr[0] ? String(arr[0].scene_count) : '';
        const sdScene = rowRp && rowSc ? _scenedata[rowRp]?.scenes?.[rowSc] : null;
        const sceneName = sdScene?.name || (arr.find(a => (a.scene_name || '').trim().length)?.scene_name || '').trim();
        const isApproved = !!sdScene?.user_tags?.finalized;
        // If this scene has finalized user_tags, use them for species/family display
        if (isApproved) {
          const utSpecies = (sdScene.user_tags.species || []).slice().sort();
          const utFams = includeFamilies ? (sdScene.user_tags.families || []).slice().sort() : [];
          species = utFams.length ? Array.from(new Set([...utSpecies, ...utFams])).sort() : utSpecies;
        }

        list.push({
          id: sceneId,
          images: arr.slice().sort((a, b) => parseNumber(b.quality) - parseNumber(a.quality)),
          representative: rep,
          imageCount: arr.length,
          species,
          maxQuality: maxQ,
          captureTimeMs,
          sceneName,
          isApproved
        });
      }

      // filter by search term
      const q = (searchTerm || '').trim().toLowerCase();
      const filtered = q ? list.filter(s => s.species.some(sp => sp.toLowerCase().includes(q))) : list;

      // sort
      const sorted = filtered.sort((a, b) => {
        if (sortBy === 'captureTime') {
          if (a.captureTimeMs !== b.captureTimeMs) return a.captureTimeMs - b.captureTimeMs;
          return parseNumber(String(a.id).split(':').pop()) - parseNumber(String(b.id).split(':').pop());
        }
        if (sortBy === 'imageCount') return b.imageCount - a.imageCount;
        if (sortBy === 'sceneId') return parseNumber(String(a.id).split(':').pop()) - parseNumber(String(b.id).split(':').pop());
        return b.maxQuality - a.maxQuality;
      });

      return sorted;
    }

    function getRating(row) {
      const rp = row?.__rootPath || rootPath || '';
      const fn = row?.filename || '';
      // 1. Manual rating stored in scenedata (pywebview desktop mode)
      const sd = _scenedata[rp];
      if (sd?.image_ratings && fn && fn in sd.image_ratings) {
        const n = parseInt(sd.image_ratings[fn], 10);
        return Number.isFinite(n) ? Math.max(0, Math.min(5, n)) : 0;
      }
      // 2. Legacy row-level manual rating (FSAPI browser mode or pre-migration data)
      if (String(row?.rating_origin).toLowerCase() === 'manual') {
        const n = parseInt(row?.rating, 10);
        return Number.isFinite(n) ? Math.max(0, Math.min(5, n)) : 0;
      }
      // 3. Auto: normalized rating computed by last apply_normalization call
      const norm = parseInt(row?.__normalized_rating ?? row?.normalized_rating, 10);
      if (Number.isFinite(norm)) return Math.max(0, Math.min(5, norm));
      return 0;
    }
    function getOrigin(row) {
      const rp = row?.__rootPath || rootPath || '';
      const fn = row?.filename || '';
      const sd = _scenedata[rp];
      if (sd?.image_ratings && fn && fn in sd.image_ratings) return 'manual';
      const s = String(row?.rating_origin || '').toLowerCase();
      if (s === 'manual') return 'manual';
      const hasNorm = row?.__normalized_rating != null || (row?.normalized_rating != null && row?.normalized_rating !== '');
      return hasNorm ? 'auto' : '';
    }
    function setRating(row, val, origin = 'manual') {
      const v = Math.max(0, Math.min(5, parseInt(val, 10) || 0));
      const rp = row?.__rootPath || rootPath || '';
      const fn = row?.filename || '';
      if (hasPywebviewApi && rp && fn) {
        // pywebview desktop mode: persist rating in scenedata only
        const sd = _initScenedata(rp);
        const current = sd.image_ratings[fn];
        if (current === v && v !== 0) return; // no change
        if (v === 0) delete sd.image_ratings[fn]; else sd.image_ratings[fn] = v;
      } else {
        // FSAPI browser mode: legacy row-level storage
        const vs = String(v);
        if ((row.rating || '') === vs && (row.rating_origin || '') === origin) return;
        row.rating = vs;
        row.rating_origin = origin;
      }
      markDirty(row);
      if (typeof window.refreshSceneFilter === 'function') window.refreshSceneFilter();
    }
    function createStarBar(row) {
      const wrap = document.createElement('div');
      wrap.className = 'stars';

      function render(tempVal = null) {
        const val = tempVal != null ? tempVal : getRating(row);
        const origin = tempVal != null ? 'manual' : getOrigin(row);
        Array.from(wrap.children).forEach((st, i) => {
          const filled = i < val;
          st.classList.toggle('filled', filled);
          st.classList.toggle('manual', filled && origin === 'manual');
          st.classList.toggle('auto', filled && origin !== 'manual');
          st.textContent = filled ? '★' : '☆';
        });
      }

      for (let i = 1; i <= 5; i++) {
        const s = document.createElement('span');
        s.className = 'star';
        s.textContent = '☆';
        s.title = 'Click to set rating';
        // Click only — hover preview is handled by delegated listeners below
        s.addEventListener('click', (ev) => { ev.stopPropagation(); setRating(row, i, 'manual'); render(); });
        wrap.appendChild(s);
      }

      // 2 delegated listeners per bar instead of 10 per-star mouseenter/mouseleave
      wrap.addEventListener('mousemove', (ev) => {
        const t = ev.target;
        if (t.classList.contains('star')) {
          const idx = Array.prototype.indexOf.call(wrap.children, t);
          if (idx >= 0) render(idx + 1);
        }
      });
      wrap.addEventListener('mouseleave', () => render());

      render();
      return wrap;
    }

    function updateStatusBar(sceneList) {
      const totalImages = sceneList.reduce((acc, s) => acc + s.imageCount, 0);
      const totalScenes = sceneList.length;
      const allScenes = new Set(rows.map(r => r.scene_count)).size;
      const dirtyMark = dirty ? ' • unsaved changes' : '';
      setStatus(`Showing ${totalScenes} scenes with ${totalImages} images${totalScenes < allScenes ? ` (filtered from ${allScenes})` : ''}${dirtyMark}`);
    }

    // Render: Scenes — grouped by folder when multiple folders are loaded
    async function renderScenes() {
      const myVer = ++_renderScenesVersion;
      const minC = parseFloat(el('#speciesConf').value) || 0;
      const search = el('#search').value;
      const sortBy = el('#sortBy').value;
      const onlyReviewedScenes = !!document.getElementById('filterScenesManualRated')?.checked;
      const groupByFolder = document.getElementById('groupByFolder')?.checked ?? getSetting('groupByFolder', true);
      const groupByTime = document.getElementById('groupByTime')?.checked ?? getSetting('groupByTime', true);
      const showBirdThumbs = document.getElementById('showBirdThumbs')?.checked ?? getSetting('showBirdThumbs', false);
      // Widen each card's grid track when bird-crop thumbs are shown, so the
      // main thumb keeps its "no-crop" height and the crop slots into the
      // extra width rather than stealing height from the main image.
      sceneGrid.classList.toggle('grid--bird-thumbs', !!showBirdThumbs);
      const includeSecondaryCheckbox = document.getElementById('includeSecondarySpecies');
      const includeSecondary = includeSecondaryCheckbox ? includeSecondaryCheckbox.checked : !!getSetting('includeSecondarySpecies', false);
      const includeFamilies = true;
      scenes = aggregateScenes(minC, search, sortBy, includeSecondary, includeFamilies);

      // Re-resolve _currentScene so the open scene dialog keeps working
      // after the scenes array is regenerated with new objects.
      if (_currentScene) {
        const openId = String(_currentScene.id);
        const refreshed = scenes.find(s => String(s.id) === openId);
        if (refreshed) _currentScene = refreshed;
      }

      // Apply scene-level manual-reviewed filter without mutating global scenes.
      // Reviewed means any of:
      //  - scene tags finalized by user, or scene renamed by user
      //  - manual/verified accept-reject culling decision on any image
      //  - manual star rating on any image
      const visibleScenes = onlyReviewedScenes ? scenes.filter(isManuallyReviewedScene) : scenes;

      updateStatusBar(visibleScenes);

      // Prevent flash-of-empty-content: lock the grid's current height as a
      // minimum so the page layout doesn't collapse while we rebuild the DOM,
      // and save the scroll position of the main container for restoration.
      const mainEl = document.querySelector('main');
      const savedScrollTop = mainEl ? mainEl.scrollTop : 0;
      const currentHeight = sceneGrid.offsetHeight;
      if (currentHeight > 0) {
        sceneGrid.style.minHeight = currentHeight + 'px';
      }
      sceneGrid.innerHTML = '';

      // Show welcome panel when no data is loaded; hide it once a folder is open
      const _welcomePanel = document.getElementById('welcomePanel');
      if (_welcomePanel) _welcomePanel.classList.toggle('hidden', rows.length > 0);

      // Flat index for shift-click range selection
      _visibleSceneOrder = visibleScenes.map(s => String(s.id));

      // ---- Two-level grouping: folder → adaptive time clusters ----
      //
      // The timeline previously used a fixed 1-hour grid (YYYY-MM-DDTHH) which
      // both over-segmented long sessions (e.g. 55 minutes → 2 nodes straddling
      // the hour boundary) and under-segmented bursty ones (50 shots in 3
      // minutes became a single node identical to 1 shot in 30 minutes).
      // We now cluster by actual gaps between successive scenes so a "session"
      // of continuous shooting is one node, and a quiet pause cuts a new node
      // regardless of clock alignment.
      //
      // The gap threshold is derived from the folder itself: we take the
      // median inter-scene gap and multiply by a constant so anything notably
      // quieter than the folder's typical rhythm starts a new cluster. This
      // keeps dense burst folders (gap of ~1s → ~10s threshold) and casual
      // days (gap of ~30s → ~5min threshold) from needing different settings.
      // Hard floor/ceiling keep pathological data (single-scene folders,
      // multi-week bundles) from producing absurd thresholds.
      const CLUSTER_GAP_MIN_MS = 45 * 1000;       // 45 s — never split bursts
      const CLUSTER_GAP_MAX_MS = 10 * 60 * 1000;  // 10 min — never merge sessions
      const CLUSTER_GAP_FALLBACK_MS = 3 * 60 * 1000; // fallback if we can't infer
      const CLUSTER_GAP_MULTIPLIER = 10;

      function computeDynamicClusterGapMs(scenes) {
        const times = [];
        for (const s of scenes) {
          if (Number.isFinite(s.captureTimeMs)) times.push(s.captureTimeMs);
        }
        if (times.length < 3) return CLUSTER_GAP_FALLBACK_MS;
        times.sort((a, b) => a - b);
        const gaps = [];
        for (let i = 1; i < times.length; i++) {
          const g = times[i] - times[i - 1];
          if (g > 0) gaps.push(g);
        }
        if (!gaps.length) return CLUSTER_GAP_FALLBACK_MS;
        gaps.sort((a, b) => a - b);
        const median = gaps[Math.floor(gaps.length / 2)];
        const threshold = median * CLUSTER_GAP_MULTIPLIER;
        return Math.max(CLUSTER_GAP_MIN_MS, Math.min(CLUSTER_GAP_MAX_MS, threshold));
      }

      function _pad2(n) { return String(n).padStart(2, '0'); }
      function _dayKeyFromMs(ms) {
        if (!Number.isFinite(ms)) return '';
        const d = new Date(ms);
        if (isNaN(d)) return '';
        return `${d.getFullYear()}-${_pad2(d.getMonth()+1)}-${_pad2(d.getDate())}`;
      }
      function formatClusterTime(ms) {
        if (!Number.isFinite(ms)) return '';
        try {
          return new Date(ms).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
        } catch (_) { return ''; }
      }
      function formatClusterDay(ms) {
        if (!Number.isFinite(ms)) return '';
        try {
          return new Date(ms).toLocaleDateString(undefined, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
        } catch (_) { return ''; }
      }

      // Walk scenes in ascending time order, cutting a new cluster each time
      // the gap between the last scene's time and the next exceeds the
      // threshold. Scenes lacking capture time are dropped into a final
      // "untimed" cluster so they remain visible but don't distort ranges.
      function buildTimeClusters(scenes, gapMs) {
        const timed = [];
        const untimed = [];
        for (const s of scenes) {
          if (Number.isFinite(s.captureTimeMs)) timed.push(s);
          else untimed.push(s);
        }
        timed.sort((a, b) => a.captureTimeMs - b.captureTimeMs);
        const clusters = [];
        for (const s of timed) {
          const last = clusters[clusters.length - 1];
          if (!last || s.captureTimeMs - last.endMs > gapMs) {
            clusters.push({
              scenes: [s],
              startMs: s.captureTimeMs,
              endMs: s.captureTimeMs,
              untimed: false,
            });
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

      // Build folderMap: folderKey → { folderPath, scenes: [ordered scene list] }
      const folderOrder = [];
      const folderMap = new Map();
      for (const s of visibleScenes) {
        const rp = groupByFolder ? (s.representative?.__rootPath || '') : '';
        const fk = rp || '__single__';
        if (!folderMap.has(fk)) { folderMap.set(fk, { folderPath: rp, scenes: [] }); folderOrder.push(fk); }
        folderMap.get(fk).scenes.push(s);
      }
      const showFolderHeaders = groupByFolder;

      function buildCard(s) {
        const card = document.createElement('article');
        card.className = 'card';
        card.dataset.sceneId = String(s.id);
        if (selectedSceneIds.has(String(s.id))) card.classList.add('selected');

        const th = document.createElement('div');
        th.className = 'thumb';
        const img = document.createElement('img');
        img.alt = s.representative?.filename || '';
        applyThumbnailExposureToImg(img, s.representative);
        lazyLoadImg(img, () => getBlobUrlForPath(
          s.representative?.export_path || s.representative?.crop_path,
          s.representative?.__rootPath
        ));
        th.appendChild(img);
        if (showBirdThumbs && s.representative?.crop_path && s.representative?.export_path) {
          const row = document.createElement('div');
          row.className = 'thumb-row';
          const cropWrap = document.createElement('div');
          cropWrap.className = 'thumb-bird-crop';
          const cropImg = document.createElement('img');
          cropImg.alt = 'Bird crop';
          lazyLoadImg(cropImg, () => getBlobUrlForPath(
            s.representative.crop_path,
            s.representative.__rootPath
          ));
          cropWrap.appendChild(cropImg);
          row.appendChild(th);
          row.appendChild(cropWrap);
          card.appendChild(row);
        } else {
          card.appendChild(th);
        }

        const body = document.createElement('div');
        body.className = 'body';
        const title = document.createElement('div');
        title.className = 'title';
        const _localNum = String(s.id).split(':').pop();
        const _folderName = folderBaseName(s.representative?.__rootPath || '');
        // Build the title entirely from text nodes / trusted elements — never
        // assign to innerHTML with any user-controlled substring. The previous
        // decodeEntities(escapeHtml(...)) pattern round-tripped the escaped
        // string back to its raw form, which allowed a crafted scene name
        // (e.g. from a poisoned kestrel_scenedata.json) to inject a DOM-XSS
        // and, via the pywebview bridge, escalate to RCE. See FINDING-01.
        if (_folderName && !showFolderHeaders) {
          const folderEl = document.createElement('i');
          folderEl.className = 'folder-name';
          folderEl.textContent = _folderName;
          title.appendChild(folderEl);
          const sep = document.createElement('span');
          sep.className = 'title-sep';
          sep.textContent = ' / ';
          title.appendChild(sep);
        }
        const idBold = document.createElement('b');
        idBold.textContent = `#${_localNum}`;
        title.appendChild(idBold);
        if (s.sceneName) {
          title.appendChild(document.createTextNode(' \u2014 '));
          const nameSpan = document.createElement('span');
          nameSpan.className = 'name';
          nameSpan.textContent = String(s.sceneName);
          title.appendChild(nameSpan);
        }
        title.title = (s.representative?.__rootPath || String(s.id)) + (s.sceneName ? ` \u2014 ${s.sceneName}` : '');
        const meta = document.createElement('div');
        // Use a dedicated class for title-level badges so other .meta uses are unaffected
        meta.className = 'meta title-badges';
        meta.innerHTML = `<span class="score">★ ${fmt3(s.maxQuality)}</span><span>\ud83d\udcf8 ${s.imageCount}</span>`;
        const chips = document.createElement('div');
        chips.className = 'chips';
        if (s.isApproved) {
          card.classList.add('scene-approved');
          chips.classList.add('reviewed-tags');
        }
        for (const sp of s.species.slice(0, 3)) {
          const c = document.createElement('span'); c.className = s.isApproved ? 'chip manual-approved' : 'chip'; c.textContent = sp; c.title = sp; chips.appendChild(c);
        }
        if (s.species.length > 3) { const more = document.createElement('span'); more.className = 'chip badge'; more.textContent = `+${s.species.length - 3} more`; more.title = s.species.slice(3).join(', '); chips.appendChild(more); }
        // Put title and badges on the same physical line: left = title, right = badges
        const titleRow = document.createElement('div');
        titleRow.className = 'title-row';
        titleRow.appendChild(title);
        titleRow.appendChild(meta);
        body.appendChild(titleRow);
        body.appendChild(chips);
        card.appendChild(body);

        card.addEventListener('click', (ev) => {
          const sid = String(s.id);
          _focusGridCard(sid);
          if (ev.shiftKey && _lastSelectedIdx >= 0) {
            const idx = _visibleSceneOrder.indexOf(sid);
            if (idx >= 0) {
              const lo = Math.min(_lastSelectedIdx, idx);
              const hi = Math.max(_lastSelectedIdx, idx);
              for (let i = lo; i <= hi; i++) selectedSceneIds.add(_visibleSceneOrder[i]);
            }
            updateSelectionUI();
            ev.preventDefault(); return;
          }
          if (ev.ctrlKey || ev.metaKey) {
            if (selectedSceneIds.has(sid)) selectedSceneIds.delete(sid); else selectedSceneIds.add(sid);
            _lastSelectedIdx = _visibleSceneOrder.indexOf(sid);
            updateSelectionUI();
            ev.preventDefault(); return;
          }
          if (selectedSceneIds.size > 0) {
            if (selectedSceneIds.has(sid)) selectedSceneIds.delete(sid); else selectedSceneIds.add(sid);
            _lastSelectedIdx = _visibleSceneOrder.indexOf(sid);
            updateSelectionUI();
            return;
          }
          // Normal: open scene dialog
          _lastSelectedIdx = _visibleSceneOrder.indexOf(sid);
          openSceneDialog(sid);
        });
        return card;
      }

      const batch = 24;

      // ---- Timeline builder (used when groupByTime is on) ----
      //
      // Each cluster renders as a timeline node with:
      //   • rail dot (sized by image count) + connecting line
      //   • a header showing the time range, scene count, and image count
      //   • a grid of scene cards
      //
      // The rail dot scales with the cluster's image count so a quick scroll
      // down the left edge makes shooting bursts immediately visible — bigger
      // dot = denser cluster. Sizing uses sqrt so a 200-shot burst isn't 10×
      // the size of a 20-shot one.
      const _DOT_MIN_PX = 8;
      const _DOT_MAX_PX = 26;

      function _clusterImageCount(cluster) {
        let n = 0;
        for (const s of cluster.scenes) n += s.imageCount || 0;
        return n;
      }

      function _dotSizePx(imageCount, maxInFolder) {
        if (!Number.isFinite(imageCount) || imageCount <= 0) return _DOT_MIN_PX;
        if (!Number.isFinite(maxInFolder) || maxInFolder <= 1) return _DOT_MIN_PX + 3;
        const frac = Math.sqrt(imageCount) / Math.sqrt(maxInFolder);
        return Math.round(_DOT_MIN_PX + frac * (_DOT_MAX_PX - _DOT_MIN_PX));
      }

      function buildTimeline(fd, containerEl) {
        const timelineEl = document.createElement('div');
        timelineEl.className = 'timeline-body';
        const gapMs = computeDynamicClusterGapMs(fd.scenes);
        const clusters = buildTimeClusters(fd.scenes, gapMs);
        let prevDay = null;

        // Pre-compute the max image count across all clusters so dot sizes
        // are proportional within this folder (different folders can have
        // wildly different scales and shouldn't compete on the same axis).
        let maxImgCountInFolder = 1;
        for (const c of clusters) {
          const n = _clusterImageCount(c);
          if (n > maxImgCountInFolder) maxImgCountInFolder = n;
        }

        for (let ni = 0; ni < clusters.length; ni++) {
          const cluster = clusters[ni];
          const isLast = ni === clusters.length - 1;
          const thisDay = cluster.untimed ? '' : _dayKeyFromMs(cluster.startMs);

          // Day banner when the calendar date changes between clusters
          if (thisDay && thisDay !== prevDay) {
            const banner = document.createElement('div');
            banner.className = 'timeline-day-banner';
            banner.textContent = formatClusterDay(cluster.startMs);
            timelineEl.appendChild(banner);
            prevDay = thisDay;
          }

          const nodeEl = document.createElement('div');
          nodeEl.className = 'timeline-node' + (cluster.untimed ? ' timeline-node-untimed' : '');

          // Rail column: dot (sized by image count) + connecting line
          const railCol = document.createElement('div');
          railCol.className = 'timeline-rail-col';
          const dot = document.createElement('div');
          dot.className = 'timeline-dot';
          const imgCount = _clusterImageCount(cluster);
          const dotSize = _dotSizePx(imgCount, maxImgCountInFolder);
          dot.style.width = dotSize + 'px';
          dot.style.height = dotSize + 'px';
          // Title gives a fallback tooltip for folks who want the raw number.
          dot.title = `${cluster.scenes.length} scene${cluster.scenes.length === 1 ? '' : 's'} · ${imgCount} image${imgCount === 1 ? '' : 's'}`;
          const line = document.createElement('div');
          line.className = 'timeline-line' + (isLast ? ' last' : '');
          railCol.appendChild(dot);
          railCol.appendChild(line);

          // Content column: header + grid
          const contentCol = document.createElement('div');
          contentCol.className = 'timeline-content-col';

          const hdr = document.createElement('div');
          hdr.className = 'timeline-node-header';

          const timeSpan = document.createElement('span');
          timeSpan.className = 'timeline-node-time';
          if (cluster.untimed) {
            timeSpan.textContent = 'Unknown time';
          } else {
            const spanMs = cluster.endMs - cluster.startMs;
            // Collapse clusters that span less than two minutes to a single
            // time (otherwise the header reads "10:42 AM – 10:42 AM").
            timeSpan.textContent = spanMs < 2 * 60 * 1000
              ? formatClusterTime(cluster.startMs)
              : `${formatClusterTime(cluster.startMs)} – ${formatClusterTime(cluster.endMs)}`;
          }
          hdr.appendChild(timeSpan);

          const countSpan = document.createElement('span');
          countSpan.className = 'timeline-node-count muted';
          countSpan.textContent =
            `${cluster.scenes.length} scene${cluster.scenes.length === 1 ? '' : 's'} · ${imgCount} image${imgCount === 1 ? '' : 's'}`;
          hdr.appendChild(countSpan);

          contentCol.appendChild(hdr);

          const gridEl = document.createElement('div');
          gridEl.className = 'grid timeline-grid';
          contentCol.appendChild(gridEl);

          nodeEl.appendChild(railCol);
          nodeEl.appendChild(contentCol);
          timelineEl.appendChild(nodeEl);

          for (let i = 0; i < cluster.scenes.length; i += batch) {
            if (myVer !== _renderScenesVersion) { sceneGrid.style.minHeight = ''; return; }
            const slice = cluster.scenes.slice(i, i + batch);
            const frag = document.createDocumentFragment();
            for (const s of slice) frag.appendChild(buildCard(s));
            gridEl.appendChild(frag);
          }
        }
        containerEl.appendChild(timelineEl);
      }

      // ---- Main folder rendering loop ----
      for (const fk of folderOrder) {
        const fd = folderMap.get(fk);
        const allScenesInFolder = fd.scenes;
        let bodyEl; // receives the timeline or flat grid

        if (showFolderHeaders && fd.folderPath) {
          const folderName = folderBaseName(fd.folderPath) || fd.folderPath || '(unknown folder)';
          const collapsed = collapsedFolders.has(fk);

          const groupEl = document.createElement('div');
          groupEl.className = 'folder-group';

          const hdr = document.createElement('div');
          hdr.className = 'folder-group-header' + (collapsed ? ' collapsed' : '');
          hdr.innerHTML = `<span class="folder-group-toggle">\u25bc</span><span class="folder-group-name">${escapeHtml(folderName)}</span><span class="folder-group-count muted">${allScenesInFolder.length} scene${allScenesInFolder.length === 1 ? '' : 's'}</span>`;

          // Left-aligned secondary actions
          const leftActions = document.createElement('div');
          leftActions.className = 'folder-group-left-actions';

          const explorerBtn = document.createElement('button');
          explorerBtn.className = 'action-btn';
          explorerBtn.innerHTML = '<i>📂</i> Open';
          explorerBtn.title = 'Open this folder in File Explorer';
          explorerBtn.addEventListener('click', (ev) => { ev.stopPropagation(); window.pywebview.api.open_file_explorer(fd.folderPath); });
          leftActions.appendChild(explorerBtn);

          const folderOptionsBtn = document.createElement('button');
          folderOptionsBtn.className = 'action-btn';
          folderOptionsBtn.innerHTML = '<i>↺</i> Reset Culling Decisions';
          folderOptionsBtn.title = 'Reset Accept/Reject culling decisions for this folder';
          folderOptionsBtn.addEventListener('click', (ev) => { ev.stopPropagation(); showFolderOptionsDialog(fd.folderPath); });
          leftActions.appendChild(folderOptionsBtn);

          // Adjust Capture Time — shifts every row's capture_time by a
          // user-supplied offset (hours). Useful when the camera clock was
          // set to the wrong time zone or drifted relative to another body.
          const adjustTimeBtn = document.createElement('button');
          adjustTimeBtn.className = 'action-btn';
          adjustTimeBtn.innerHTML = '<i>⏱</i> Adjust Capture Time';
          adjustTimeBtn.title = 'Shift capture timestamps for every image in this folder by a fixed offset (useful for syncing between camera bodies)';
          adjustTimeBtn.addEventListener('click', (ev) => {
            ev.stopPropagation();
            showAdjustCaptureTimeDialog(fd.folderPath);
          });
          leftActions.appendChild(adjustTimeBtn);

          hdr.appendChild(leftActions);

          // Spacer pushes right actions to the far right
          const spacer = document.createElement('div');
          spacer.style.flex = '1';
          hdr.appendChild(spacer);

          // Right-aligned primary actions
          const rightActions = document.createElement('div');
          rightActions.className = 'folder-group-right-actions';

          const writeMetaBtn = document.createElement('button');
          writeMetaBtn.className = 'action-btn write-metadata-btn';
          writeMetaBtn.innerHTML = '<i>📝</i> Write Photo Metadata';
          writeMetaBtn.title = 'Write XMP sidecar files alongside your photos — carries star ratings, Accept/Reject decisions, and species tags. Readable by Lightroom, Capture One, darktable, and other editors.';
          writeMetaBtn.addEventListener('click', (ev) => { ev.stopPropagation(); writeMetadataForFolder(fd.folderPath); });
          rightActions.appendChild(writeMetaBtn);

          const cullingBtn = document.createElement('button');
          cullingBtn.className = 'action-btn culling-assistant-btn';
          cullingBtn.innerHTML = '<i>✂</i> Open Culling Assistant';
          cullingBtn.title = 'Open the AI-assisted culling workflow for this folder';
          cullingBtn.addEventListener('click', (ev) => { ev.stopPropagation(); openCullingAssistant(fd.folderPath); });
          rightActions.appendChild(cullingBtn);

          hdr.appendChild(rightActions);

          bodyEl = document.createElement('div');
          bodyEl.className = 'folder-group-body' + (collapsed ? ' hidden' : '');

          const _fk = fk, _bodyEl = bodyEl, _hdr = hdr;
          hdr.addEventListener('click', () => {
            if (collapsedFolders.has(_fk)) collapsedFolders.delete(_fk); else collapsedFolders.add(_fk);
            _hdr.classList.toggle('collapsed');
            _bodyEl.classList.toggle('hidden');
          });
          groupEl.appendChild(hdr);
          groupEl.appendChild(bodyEl);
          sceneGrid.appendChild(groupEl);
        } else {
          bodyEl = document.createElement('div');
          sceneGrid.appendChild(bodyEl);
        }

        if (groupByTime) {
          buildTimeline(fd, bodyEl);
        } else {
          const gridEl = document.createElement('div');
          gridEl.className = 'folder-group-grid grid';
          bodyEl.appendChild(gridEl);
          for (let i = 0; i < allScenesInFolder.length; i += batch) {
            if (myVer !== _renderScenesVersion) { sceneGrid.style.minHeight = ''; return; }
            const slice = allScenesInFolder.slice(i, i + batch);
            const frag = document.createDocumentFragment();
            for (const s of slice) frag.appendChild(buildCard(s));
            gridEl.appendChild(frag);
          }
        }
      }

      // Restore scroll position and release the minimum-height lock now that
      // the grid is rebuilt, preventing flash-of-empty-content.
      sceneGrid.style.minHeight = '';
      if (mainEl && savedScrollTop > 0) {
        mainEl.scrollTop = savedScrollTop;
      }
    }

    // Update card highlights and show/hide floating action bar based on current selection
    function updateSelectionUI() {
      const n = selectedSceneIds.size;
      document.querySelectorAll('.card[data-scene-id]').forEach(c => {
        c.classList.toggle('selected', selectedSceneIds.has(c.dataset.sceneId));
      });
      const bar = document.getElementById('selectActionBar');
      if (!bar) return;
      if (n >= 2) {
        bar.classList.remove('hidden');
        const lbl = document.getElementById('selectActionLabel');
        if (lbl) lbl.textContent = `${n} scene${n === 1 ? '' : 's'} selected`;
      } else {
        bar.classList.add('hidden');
      }
    }

    // Scroll to a scene card in the grid and give it keyboard focus
    function _focusGridCard(sceneId) {
      _focusedCardId = String(sceneId);
      document.querySelectorAll('.card.focused').forEach(c => c.classList.remove('focused'));
      const card = sceneGrid.querySelector(`.card[data-scene-id="${CSS.escape(_focusedCardId)}"]`);
      if (card) {
        card.classList.add('focused');
        card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }

    // Clear the focused-card highlight
    function _clearGridFocus() {
      _focusedCardId = null;
      document.querySelectorAll('.card.focused').forEach(c => c.classList.remove('focused'));
    }

    // Get all visible card elements in DOM order
    function _getVisibleCards() {
      return Array.from(sceneGrid.querySelectorAll('.card[data-scene-id]'));
    }

    // Grid keyboard navigation: arrow keys move focus, Enter opens scene dialog
    function _gridKeyHandler(e) {
      if (document.querySelector('dialog[open]')) return;
      if (selectedSceneIds.size > 0) return;
      const tag = (e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;

      const isArrow = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key);
      const isEnter = e.key === 'Enter';
      if (!isArrow && !isEnter) return;
      if (!_focusedCardId) return;

      e.preventDefault();

      if (isEnter) {
        openSceneDialog(_focusedCardId);
        return;
      }

      const cards = _getVisibleCards();
      if (cards.length === 0) return;
      const curIdx = cards.findIndex(c => c.dataset.sceneId === _focusedCardId);
      if (curIdx < 0) return;
      const curCard = cards[curIdx];

      let nextIdx = -1;
      if (e.key === 'ArrowLeft') {
        nextIdx = curIdx - 1;
      } else if (e.key === 'ArrowRight') {
        nextIdx = curIdx + 1;
      } else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        const curRect = curCard.getBoundingClientRect();
        const curCenterX = curRect.left + curRect.width / 2;
        const dir = e.key === 'ArrowDown' ? 1 : -1;
        let bestIdx = -1, bestDist = Infinity;
        for (let i = 0; i < cards.length; i++) {
          if (i === curIdx) continue;
          const r = cards[i].getBoundingClientRect();
          const rowDiff = dir > 0 ? r.top - curRect.top : curRect.top - r.top;
          if (rowDiff < 10) continue;
          const dist = Math.abs(r.left + r.width / 2 - curCenterX) + rowDiff * 2;
          if (dist < bestDist) { bestDist = dist; bestIdx = i; }
        }
        nextIdx = bestIdx;
      }

      if (nextIdx >= 0 && nextIdx < cards.length) {
        _focusGridCard(cards[nextIdx].dataset.sceneId);
      }
    }
    document.addEventListener('keydown', _gridKeyHandler);

    // Merge all currently selected scenes (must all be in same folder)
    async function executeSelectionMerge() {
      const ids = Array.from(selectedSceneIds);
      if (ids.length < 2) return;
      const parsed = ids.map(id => {
        const parts = String(id).split(':');
        const count = parts.pop();
        const slot = parts.length ? parseInt(parts[0], 10) : 0;
        return { id, slot, count };
      });
      const slots = new Set(parsed.map(p => p.slot));
      if (slots.size > 1) {
        alert('Cannot merge scenes from different folders.\nSelect scenes from the same folder only.');
        return;
      }
      const target = parsed.slice().sort((a, b) => parseNumber(a.count) - parseNumber(b.count))[0];
      const slot = target.slot;
      const targetCount = target.count;
      const mergedSceneId = String(slot != null ? slot + ':' + targetCount : targetCount);
      let changed = 0;
      for (const r of rows) {
        if ((r.__folderSlot ?? 0) !== slot) continue;
        if (parsed.some(p => p.count === String(r.scene_count)) && String(r.scene_count) !== targetCount) {
          r.scene_count = targetCount; changed++;
        }
      }
      const rpForMerge = rows.find(r => (r.__folderSlot ?? 0) === slot)?.__rootPath || rootPath || '';
      // Update scenedata: move filenames from non-target scenes into target scene
      if (hasPywebviewApi) {
        if (rpForMerge) {
          const sd = _initScenedata(rpForMerge);
          const allMovedFiles = new Set();
          for (const p of parsed) {
            if (p.count !== targetCount && sd.scenes[p.count]) {
              for (const f of sd.scenes[p.count].image_filenames || []) allMovedFiles.add(f);
              delete sd.scenes[p.count];
            }
          }
          if (!sd.scenes[targetCount]) {
            sd.scenes[targetCount] = { scene_id: targetCount, image_filenames: [], name: '', status: 'pending', user_tags: { species: [], families: [], finalized: false } };
          }
          for (const f of allMovedFiles) {
            if (!sd.scenes[targetCount].image_filenames.includes(f)) sd.scenes[targetCount].image_filenames.push(f);
          }
        }
      }
      if (changed) {
        markDirty(rpForMerge);
        setStatus(`Merged ${ids.length} scenes into #${targetCount}. ${changed} rows updated.`);
      }
      selectedSceneIds.clear();
      _lastSelectedIdx = -1;
      updateSelectionUI();
      await renderScenes();
      // Scroll to the merged scene card; fall back to current scroll position
      const mergedCard = document.querySelector(`.card[data-scene-id="${CSS.escape(mergedSceneId)}"]`);
      if (mergedCard) mergedCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    // Render images inside the scene dialog, honoring the manual-rated filter and stable ordering
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
        } else {
          const muted = document.createElement('span');
          muted.className = 'muted';
          muted.textContent = 'No export preview';
          exportBox.appendChild(muted);
        }
      }

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
    // Used to auto-add the family chip when a recognized species is added,
    // cascade-remove species when a family chip is X'd, and populate the
    // species autocomplete datalist.
    let _speciesFamilyMap = null;        // { "American Robin": "Thrush sp.", ... } (canonical case)
    let _speciesFamilyMapLower = null;   // lowercase keys → { canonicalName, family }
    let _speciesNameList = [];           // sorted array of canonical species names

    async function loadSpeciesFamilyMap() {
      if (_speciesFamilyMap) return;
      if (!hasPywebviewApi || !window.pywebview?.api?.get_species_family_map) {
        _speciesFamilyMap = {};
        _speciesFamilyMapLower = {};
        _speciesNameList = [];
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
        _speciesNameList = Object.keys(map).sort((a, b) => a.localeCompare(b));
        kdebug(`[loadSpeciesFamilyMap] loaded ${_speciesNameList.length} species`);
      } catch (e) {
        console.warn('[loadSpeciesFamilyMap] failed:', e);
        _speciesFamilyMap = {};
        _speciesFamilyMapLower = {};
        _speciesNameList = [];
      }
    }

    // ── Custom species combobox (replaces native <datalist>) ──
    // Up to SPECIES_COMBO_MAX visible matches at a time. Arrow keys navigate
    // the highlight; Enter commits the highlighted item (or the typed value
    // if no highlight); clicking an item commits that item.
    const SPECIES_COMBO_MAX = 12;

    /** Filter the species list for a typed query.
     *  Returns up to SPECIES_COMBO_MAX matches: prefix matches first, then
     *  substring matches. Empty query returns the alphabetical head of the list. */
    function _filterSpeciesForCombo(query) {
      if (!_speciesNameList || _speciesNameList.length === 0) return [];
      const q = String(query || '').trim().toLowerCase();
      if (!q) return _speciesNameList.slice(0, SPECIES_COMBO_MAX);
      const prefix = [];
      const substring = [];
      for (const name of _speciesNameList) {
        const lower = name.toLowerCase();
        if (lower.startsWith(q)) {
          prefix.push(name);
        } else if (lower.includes(q)) {
          substring.push(name);
        }
        if (prefix.length >= SPECIES_COMBO_MAX) break;
      }
      return prefix.concat(substring).slice(0, SPECIES_COMBO_MAX);
    }

    /** Render the dropdown list inside the given container element.
     *  Each row shows the species name and (if known) its family in muted text,
     *  so the user can confirm the auto-link before committing. */
    function _renderSpeciesComboDropdown(dropdownEl, items, highlightIdx) {
      if (!dropdownEl) return;
      if (!items || items.length === 0) {
        dropdownEl.innerHTML = '';
        dropdownEl.style.display = 'none';
        return;
      }
      const html = items.map((name, i) => {
        const family = (_speciesFamilyMap && _speciesFamilyMap[name]) || '';
        const cls = i === highlightIdx ? 'chip-combo-item chip-combo-item--active' : 'chip-combo-item';
        const fam = family ? `<span class="chip-combo-family">${escapeHtml(family)}</span>` : '';
        return `<div class="${cls}" data-combo-index="${i}"><span class="chip-combo-name">${escapeHtml(name)}</span>${fam}</div>`;
      }).join('');
      dropdownEl.innerHTML = html;
      dropdownEl.style.display = '';
    }

    /** Look up the family display name for a species name (case-insensitive).
     *  Returns { canonical, family } if matched, or null for free-form input. */
    function _lookupFamilyForSpecies(name) {
      if (!name || !_speciesFamilyMapLower) return null;
      const hit = _speciesFamilyMapLower[String(name).trim().toLowerCase()];
      return hit || null;
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

      let html = '';
      // Species
      html += '<span class="scene-tag-label">Species:</span> ';
      if (species.length) {
        for (const sp of species) {
          html += `<span class="${chipClass}">${escapeHtml(sp)}<span class="chip-x" data-remove-species="${escapeHtml(sp)}" title="Remove '${escapeHtml(sp)}'">×</span></span>`;
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
          html += `<span class="${chipClass}">${escapeHtml(fm)}<span class="chip-x" data-remove-family="${escapeHtml(fm)}" title="Remove '${escapeHtml(fm)}'">×</span></span>`;
        }
      } else {
        html += '<span class="muted" style="font-size:11px">—</span>';
      }
      if (suggestions.family) {
        html += _buildSuggestedTagButton('family', suggestions.family);
      }
      if (_activeTagInputType === 'family' && _activeTagInputSceneId === String(scene.id)) {
        html += `<span class="chip-input-wrap"><input type="text" class="chip-input" id="inlineTagInput" placeholder="Family..." /><button class="chip-commit-btn" title="Save">✓</button></span>`;
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

      // Wire inline input (with combobox behavior for species)
      const inp = el('#inlineTagInput');
      if (inp) {
        const dropdown = tagsEl.querySelector('#inlineTagDropdown');
        const isSpeciesInput = _activeTagInputType === 'species';
        // Combobox state — only meaningful for species inputs.
        let comboItems = [];
        let comboIndex = -1;

        const positionDropdown = () => {
          if (!dropdown || !inp) return;
          const rect = inp.getBoundingClientRect();
          dropdown.style.left = Math.round(rect.left) + 'px';
          dropdown.style.top = Math.round(rect.bottom + 4) + 'px';
          // Width: at least the input's width, but allow it to grow up to the
          // CSS max-width for longer species names.
          dropdown.style.minWidth = Math.max(Math.round(rect.width), 240) + 'px';
        };

        const refreshDropdown = () => {
          if (!isSpeciesInput || !dropdown) return;
          comboItems = _filterSpeciesForCombo(inp.value);
          comboIndex = comboItems.length > 0 ? 0 : -1;
          _renderSpeciesComboDropdown(dropdown, comboItems, comboIndex);
          if (comboItems.length > 0) positionDropdown();
        };

        const updateHighlight = (newIdx) => {
          if (!comboItems.length) return;
          const n = comboItems.length;
          comboIndex = ((newIdx % n) + n) % n;
          _renderSpeciesComboDropdown(dropdown, comboItems, comboIndex);
          // Scroll the active row into view if the dropdown is scrollable.
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

        inp.onkeydown = (e) => {
          if (isSpeciesInput && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
            if (!comboItems.length) return;
            e.preventDefault();
            updateHighlight(comboIndex + (e.key === 'ArrowDown' ? 1 : -1));
            return;
          }
          if (e.key === 'Enter') {
            e.preventDefault();
            // If a dropdown row is highlighted, commit that. Otherwise commit
            // the typed value (allowing free-form species like "Eurasian Wren").
            const chosen = (isSpeciesInput && comboIndex >= 0 && comboIndex < comboItems.length)
              ? comboItems[comboIndex]
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
        if (isSpeciesInput) {
          inp.oninput = () => { refreshDropdown(); };
          inp.onfocus = () => { refreshDropdown(); };
          // Mousedown (not click) so the selection registers BEFORE the input
          // loses focus and the blur-commit timer has a chance to fire.
          if (dropdown) {
            dropdown.onmousedown = (e) => {
              const row = e.target.closest('.chip-combo-item');
              if (!row) return;
              e.preventDefault(); // keep input focused so blur-commit doesn't race
              const idx = parseInt(row.dataset.comboIndex || '-1', 10);
              if (idx >= 0 && idx < comboItems.length) {
                commit(comboItems[idx]);
              }
            };
          }
          // Initial population on render.
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
          const movedFilenames = sceneRowsBefore.filter(r => _splitSelected.has(r.filename || r.export_path || '')).map(r => r.filename || '').filter(Boolean);
          const remainFilenames = sceneRowsBefore.filter(r => !_splitSelected.has(r.filename || r.export_path || '')).map(r => r.filename || '').filter(Boolean);
          if (sd.scenes[oldSceneCount]) {
            sd.scenes[oldSceneCount].image_filenames = remainFilenames;
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
    const SETTINGS_KEY = 'kestrel-webviz-settings-v1';
    let _autoSaveEnabled = true;  // cached value to avoid repeated lookups
    let _autoSaveTimer = null;     // debounce timer for auto-saves
    
    function loadSettings() {
      try { return JSON.parse(localStorage.getItem(SETTINGS_KEY)) || {}; } catch { return {}; }
    }
    function saveSettings(obj) { localStorage.setItem(SETTINGS_KEY, JSON.stringify(obj || {})); }
    function getSetting(k, def) { const s = loadSettings(); return (k in s) ? s[k] : def; }
    
    // Auto-save logic: debounced save when auto-save is enabled
    async function attemptAutoSave() {
      _setDirtyUi(true);
      
      if (!_autoSaveEnabled) {
        clearTimeout(_autoSaveTimer);
        _autoSaveTimer = null;
        return;  // Save/Revert workflow; user will click Save button
      }
      
      // Debounce to avoid saving on every keystroke/change (save after 2 seconds of inactivity)
      clearTimeout(_autoSaveTimer);
      _autoSaveTimer = setTimeout(async () => {
        _autoSaveTimer = null;
        if (!_autoSaveEnabled || !dirty) return;
        try {
          await saveCsv();
        } catch (e) {
          console.warn('Auto-save failed:', e);
        }
      }, 2000);
    }

    async function hydrateSettingsFromServer() {
      try {
        if (!window.pywebview?.api?.get_settings) return;
        const data = await window.pywebview.api.get_settings();
        const incoming = (data && data.settings && typeof data.settings === 'object') ? data.settings : null;
        if (!incoming) return;
        saveSettings(incoming);
        _applyScenePreviewSplit(_clampScenePreviewSplitRatio(incoming[SCENE_PREVIEW_SPLIT_KEY]));
        _autoSaveEnabled = incoming.auto_save_enabled !== false;
        if (!_autoSaveEnabled) {
          clearTimeout(_autoSaveTimer);
          _autoSaveTimer = null;
        }
        _updateSaveRevertVisibility();
      } catch (_) { }
    }

    async function showSettings() {
      // Refresh local cache from persisted settings before rendering values.
      try { await hydrateSettingsFromServer(); } catch (_) { }

      const dlg = document.getElementById('settingsDlg');
      const editor = getSetting('editor', 'darktable');
      const editorSelect = document.getElementById('editorChoice');
      const customRow = document.getElementById('customEditorRow');
      const customHint = document.getElementById('customEditorHint');
      const customPath = document.getElementById('customEditorPath');
      editorSelect.value = editor;
      // If saved editor isn't in the dropdown options, treat as custom
      if (editorSelect.value !== editor) {
        editorSelect.value = 'custom';
      }
      customPath.value = getSetting('customEditorPath', '');
      const isCustom = editorSelect.value === 'custom';
      customRow.classList.toggle('hidden', !isCustom);
      customHint.classList.toggle('hidden', !isCustom);
      // Show/hide custom row when selection changes
      editorSelect.onchange = () => {
        const c = editorSelect.value === 'custom';
        customRow.classList.toggle('hidden', !c);
        customHint.classList.toggle('hidden', !c);
      };
      // Browse button
      document.getElementById('customEditorBrowse').onclick = async () => {
        if (window.pywebview?.api?.choose_application) {
          const path = await window.pywebview.api.choose_application();
          if (path) customPath.value = path;
        } else {
          showToast('Browse is only available in the desktop app', 3000);
        }
      };
      document.getElementById('treeScanDepth').value = getSetting('treeScanDepth', 3);
      // Rating profile
      const profileSelect = document.getElementById('ratingProfile');
      if (profileSelect) profileSelect.value = getSetting('rating_profile', 'balanced');
      // RAW preview cache
      const rawCacheCb = document.getElementById('rawPreviewCacheEnabled');
      if (rawCacheCb) rawCacheCb.checked = getSetting('raw_preview_cache_enabled', true);
      const optedIn = getSetting('analytics_opted_in', null);
      const consentShown = getSetting('analytics_consent_shown', false);
      const cb = document.getElementById('settingsAnalyticsOptIn');
      const lbl = document.getElementById('settingsAnalyticsLabel');
      cb.checked = optedIn === true;
      lbl.textContent = consentShown
        ? (optedIn === true ? 'Opted in' : 'Not sharing')
        : 'Not yet decided';
      
      // Display total impact (photos analyzed) from hydrated local settings.
      const totalPhotos = getSetting('kestrel_impact_total_files', 0);
      const impactEl = document.getElementById('settingsTotalImpact');
      if (impactEl) {
        impactEl.textContent = totalPhotos > 0 ? totalPhotos.toLocaleString() + ' photos' : '0 photos';
      }
      
      // Auto-Save setting
      const autoSaveCb = document.getElementById('settingsAutoSave');
      if (autoSaveCb) autoSaveCb.checked = getSetting('auto_save_enabled', true);

      const rawExpDisableCb = document.getElementById('rawExposureCorrectionDisabled');
      if (rawExpDisableCb) rawExpDisableCb.checked = getSetting('raw_exposure_correction_disabled', false);
      const ectSettings = document.getElementById('settingsExposureCorrectedThumbs');
      if (ectSettings) ectSettings.checked = !!getSetting('exposure_corrected_thumbs', true);

      dlg.showModal();
    }
    async function applySettings() {
      const editorSelect = document.getElementById('editorChoice');
      const editor = editorSelect.value || 'darktable';
      const customEditorPath = document.getElementById('customEditorPath').value.trim();
      const treeScanDepth = Math.max(1, Math.min(6, parseInt(document.getElementById('treeScanDepth').value, 10) || 3));
      const analyticsOptIn = document.getElementById('settingsAnalyticsOptIn').checked;
      const profileEl = document.getElementById('ratingProfile');
      const ratingProfile = profileEl ? profileEl.value : 'balanced';
      const rawCacheCb2 = document.getElementById('rawPreviewCacheEnabled');
      const rawPreviewCacheEnabled = rawCacheCb2 ? rawCacheCb2.checked : true;
      const autoSaveCb = document.getElementById('settingsAutoSave');
      const autoSaveEnabled = autoSaveCb ? autoSaveCb.checked : true;
      // Merge into existing settings so keys like machine_id / analytics_consent_shown are preserved
      const existing = loadSettings();
      const prevProfile = existing.rating_profile || 'balanced';
      const ectCb = document.getElementById('settingsExposureCorrectedThumbs');
      const exposureCorrectedThumbs = ectCb ? !!ectCb.checked : getSetting('exposure_corrected_thumbs', true);
      const prevExposureThumbs = getSetting('exposure_corrected_thumbs', true);
      const rawExpEl = document.getElementById('rawExposureCorrectionDisabled');
      const rawExposureCorrectionDisabled = rawExpEl ? !!rawExpEl.checked : !!existing.raw_exposure_correction_disabled;
      const prevRawExposureDisabled = !!existing.raw_exposure_correction_disabled;
      const settings = {
        ...existing, editor, customEditorPath, treeScanDepth,
        analytics_opted_in: analyticsOptIn, analytics_consent_shown: true,
        rating_profile: ratingProfile,
        raw_preview_cache_enabled: rawPreviewCacheEnabled,
        auto_save_enabled: autoSaveEnabled,
        raw_exposure_correction_disabled: rawExposureCorrectionDisabled,
        exposure_corrected_thumbs: exposureCorrectedThumbs,
      };
      _autoSaveEnabled = autoSaveEnabled;
      if (!_autoSaveEnabled) {
        clearTimeout(_autoSaveTimer);
        _autoSaveTimer = null;
      }
      _updateSaveRevertVisibility();
      // Persist settings to localStorage immediately
      saveSettings(settings);
      if (hasPywebviewApi && window.pywebview?.api?.save_settings_data) {
        try { await window.pywebview.api.save_settings_data(settings); } catch (_) { }
      }
      document.getElementById('settingsDlg').close();
      // If rating profile changed and folders are loaded, reapply immediately
      if (ratingProfile !== prevProfile && rows.length > 0) {
        await reapplyNormalizationForLoadedFolders();
      }
      const thumbPreviewChanged =
        exposureCorrectedThumbs !== prevExposureThumbs || rawExposureCorrectionDisabled !== prevRawExposureDisabled;
      if (thumbPreviewChanged && rows.length > 0) {
        await renderScenes();
        if (sceneDlg?.open && _currentScene) {
          renderFilmstrip(_currentScene);
          await selectFilmstripImage(currentImageIndex, _currentScene, false, false);
        }
      }
    }

    /** Recompute normalized_rating for every currently-loaded folder and refresh the view. */
    async function reapplyNormalizationForLoadedFolders() {
      if (!hasPywebviewApi || !window.pywebview?.api?.apply_normalization) return;
      // Collect the unique root paths of all loaded rows
      const folderPaths = [...new Set(rows.map(r => r.__rootPath).filter(Boolean))];
      if (folderPaths.length === 0) return;
      for (const p of folderPaths) {
        try {
          const res = await window.pywebview.api.apply_normalization(p);
          if (res?.success && res?.normalized_ratings) {
            const mapping = res.normalized_ratings;
            for (const r of rows) {
              if (r.__rootPath === p && r.filename in mapping) {
                r.__normalized_rating = mapping[r.filename];
              }
            }
          }
        } catch (e) {
          console.warn('[normalization] Failed for', p, e);
        }
      }
      await renderScenes();
    }

    /** Show or hide the Save/Revert wrap based on whether auto-save is active. */
    function _updateSaveRevertVisibility() {
      const wrap = document.getElementById('saveRevertWrap');
      if (!wrap) return;
      if (_autoSaveEnabled) {
        wrap.classList.add('hidden');
      } else {
        wrap.classList.remove('hidden');
      }
    }

    /** Mark settings Save button dirty (yellow) or clean. */
    function _setSettingsDirty(dirty) {
      const btn = document.getElementById('settingsSave');
      if (!btn) return;
      if (dirty) btn.classList.add('dirty'); else btn.classList.remove('dirty');
    }

    // Track changes inside the settings dialog to highlight the Save button
    document.getElementById('settingsDlg').addEventListener('change', () => _setSettingsDirty(true));
    document.getElementById('settingsDlg').addEventListener('input', () => _setSettingsDirty(true));

    document.getElementById('openSettings').addEventListener('click', () => { void showSettings(); });
    document.getElementById('settingsSave').addEventListener('click', async () => {
      await applySettings();
      _setSettingsDirty(false);
    });
    document.getElementById('settingsCancel').addEventListener('click', () => {
      document.getElementById('settingsDlg').close();
      _setSettingsDirty(false);
    });

    // ── Sidebar resize ────────────────────────────────────────────────────────
    (function initSidebarResize() {
      const resizer = document.getElementById('sidebarResizer');
      const sidebar = document.querySelector('header');
      if (!resizer || !sidebar) return;

      let dragging = false;
      let startX = 0;
      let startW = 0;

      resizer.addEventListener('mousedown', (e) => {
        dragging = true;
        startX = e.clientX;
        startW = sidebar.getBoundingClientRect().width;
        resizer.classList.add('dragging');
        document.body.style.cursor = 'col-resize';
        document.body.style.userSelect = 'none';
        e.preventDefault();
      });

      document.addEventListener('mousemove', (e) => {
        if (!dragging) return;
        const delta = e.clientX - startX;
        const newW = Math.max(260, Math.min(600, startW + delta));
        sidebar.style.width = newW + 'px';
        sidebar.style.flex = '0 0 ' + newW + 'px';
      });

      document.addEventListener('mouseup', () => {
        if (!dragging) return;
        dragging = false;
        resizer.classList.remove('dragging');
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
      });
    })();

    // ─── Telemetry helpers ────────────────────────────────────────────────────
    /** Merge a single key into persisted settings (localStorage + pywebview). */
    function mergeSetting(k, v) {
      const s = loadSettings();
      s[k] = v;
      saveSettings(s);
      if (hasPywebviewApi && window.pywebview?.api?.save_settings_data) {
        try { window.pywebview.api.save_settings_data(s); } catch (_) { }
      }
    }

    // ─── Feedback dialog ──────────────────────────────────────────────────────
    function openFeedbackDialog() {
      document.getElementById('feedbackDesc').value = '';
      document.getElementById('feedbackContact').value = '';
      document.getElementById('feedbackStatus').textContent = '';
      document.getElementById('feedbackIncludeLogs').checked = false;
      document.getElementById('feedbackIncludeScreenshot').checked = false;
      document.getElementById('feedbackScreenshotFile').value = '';
      const preview = document.getElementById('feedbackSsPreview');
      preview.src = ''; preview.style.display = 'none'; preview.dataset.b64 = '';
      document.getElementById('feedbackDlg').showModal();
    }

    // Auto-check logs when Bug Report type is selected
    document.getElementById('feedbackType').addEventListener('change', function () {
      document.getElementById('feedbackIncludeLogs').checked = (this.value === 'bug');
    });

    // Screenshot file-picker wiring
    document.getElementById('feedbackIncludeScreenshot').addEventListener('change', function () {
      if (this.checked) {
        document.getElementById('feedbackScreenshotFile').click();
      } else {
        const preview = document.getElementById('feedbackSsPreview');
        preview.src = ''; preview.style.display = 'none'; preview.dataset.b64 = '';
        document.getElementById('feedbackScreenshotFile').value = '';
      }
    });
    document.getElementById('feedbackScreenshotFile').addEventListener('change', function () {
      const file = this.files[0];
      if (!file) { document.getElementById('feedbackIncludeScreenshot').checked = false; return; }
      const reader = new FileReader();
      reader.onload = (e) => {
        const b64 = (e.target.result || '').split(',')[1] || '';
        const preview = document.getElementById('feedbackSsPreview');
        preview.src = e.target.result;
        preview.style.display = 'block';
        preview.dataset.b64 = b64;
        document.getElementById('feedbackIncludeScreenshot').checked = true;
      };
      reader.readAsDataURL(file);
    });

    async function submitFeedback() {
      const desc = document.getElementById('feedbackDesc').value.trim();
      if (!desc) {
        document.getElementById('feedbackStatus').textContent = '⚠ Please enter a description.';
        document.getElementById('feedbackDesc').focus();
        return;
      }
      const sendBtn = document.getElementById('feedbackSend');
      sendBtn.disabled = true;
      document.getElementById('feedbackStatus').textContent = 'Sending…';
      const data = {
        type: document.getElementById('feedbackType').value,
        description: desc,
        contact: document.getElementById('feedbackContact').value.trim(),
        include_logs: document.getElementById('feedbackIncludeLogs').checked,
        screenshot_b64: document.getElementById('feedbackSsPreview').dataset.b64 || '',
      };
      try {
        if (!window.pywebview?.api?.send_feedback) {
          throw new Error('Desktop API unavailable');
        }
        const result = await window.pywebview.api.send_feedback(data);
        if (result && result.success !== false) {
          document.getElementById('feedbackStatus').textContent = '✓ Feedback sent — thank you!';
          setTimeout(() => { try { document.getElementById('feedbackDlg').close(); } catch (_) { } }, 1200);
        } else {
          document.getElementById('feedbackStatus').textContent = '⚠ Could not send — please try again later.';
        }
      } catch (e) {
        document.getElementById('feedbackStatus').textContent = '⚠ Send failed: ' + (e.message || e);
      } finally {
        sendBtn.disabled = false;
      }
    }

    document.getElementById('openFeedback').addEventListener('click', openFeedbackDialog);
    document.getElementById('feedbackCancel').addEventListener('click', () => document.getElementById('feedbackDlg').close());
    document.getElementById('feedbackSend').addEventListener('click', submitFeedback);

    // ─── Analytics Consent dialog ─────────────────────────────────────────────
    function showAnalyticsConsentDialog() {
      if (_analyticsConsentPending) return;
      if (getSetting('analytics_consent_shown', false)) return;
      _analyticsConsentPending = true;
      document.getElementById('analyticsConsentDlg').showModal();
    }

    function handleAnalyticsConsent(optedIn) {
      mergeSetting('analytics_opted_in', optedIn);
      mergeSetting('analytics_consent_shown', true);
      try { document.getElementById('analyticsConsentDlg').close(); } catch (_) { }
      _analyticsConsentPending = false;
    }

    document.getElementById('analyticsAccept').addEventListener('click', () => handleAnalyticsConsent(true));
    document.getElementById('analyticsDecline').addEventListener('click', () => handleAnalyticsConsent(false));

    // ─── Donation / Support ──────────────────────────────────────────────────────
    const DONATE_URL = 'https://www.paypal.com/donate/?hosted_button_id=CXH4FE5AKZD3A';
    const DONATE_THRESHOLD_KEY = 'kestrel-donate-thresholds-shown-v1';

    function openDonateLink() {
      if (hasPywebviewApi && window.pywebview?.api?.open_url) {
        window.pywebview.api.open_url(DONATE_URL);
      } else {
        try { window.open(DONATE_URL, '_blank', 'noopener,noreferrer'); } catch (_) { }
      }
    }

    function _loadDonateThresholdsShown() {
      // Load from persistent settings (saved to settings.json)
      return getSetting('kestrel_donate_thresholds_shown', []);
    }
    function _saveDonateThresholdsShown(arr) {
      // Save to both localStorage and backend settings (persists to settings.json)
      const existing = loadSettings();
      const settings = { ...existing, kestrel_donate_thresholds_shown: arr };
      saveSettings(settings);
      // Persist to backend (settings.json)
      if (hasPywebviewApi && window.pywebview?.api?.save_settings_data) {
        try { window.pywebview.api.save_settings_data(settings); } catch (_) { }
      }
    }

    function showDonatePrompt(totalFiles) {
      const countEl = document.getElementById('donateCountDisplay');
      // Round down to nearest threshold for "over N photos" phrasing
      const thresholds = [1000, 5000, 10000, 25000, 50000, 100000, 200000];
      let milestone = totalFiles || 0;
      for (let i = thresholds.length - 1; i >= 0; i--) {
        if (milestone >= thresholds[i]) { milestone = thresholds[i]; break; }
      }
      if (countEl) countEl.textContent = milestone.toLocaleString();
      const dlg = document.getElementById('donateDlg');
      // Only show if no other dialog is already open
      if (dlg && !document.querySelector('dialog[open]')) dlg.showModal();
    }

    /** Check if the cumulative total crosses a donation milestone. Call after a folder finishes. */
    async function checkDonationThresholdAsync() {
      try {
        let total = 0;
        if (hasPywebviewApi && window.pywebview?.api?.get_settings) {
          const res = await window.pywebview.api.get_settings();
          const s = (res && res.success && res.settings && typeof res.settings === 'object') ? res.settings : null;
          if (s) {
            saveSettings({ ...loadSettings(), ...s });
            total = Number(s.kestrel_impact_total_files || 0);
          }
        }
        if (total <= 0) return;
        const thresholds = [1000, 5000, 10000, 25000, 50000, 100000, 200000];
        const shown = _loadDonateThresholdsShown();
        for (const t of thresholds) {
          if (total >= t && !shown.includes(t)) {
            shown.push(t);
            _saveDonateThresholdsShown(shown);
            // Small delay so queue panel settles first
            setTimeout(() => showDonatePrompt(total), 2000);
            break;
          }
        }
      } catch (_) { /* failsafe */ }
    }

    /** Check donation threshold on app startup (only once). */
    async function checkDonationThresholdOnStartup() {
      try {
        // Prefer persisted settings from backend, then local cache as fallback.
        let total = getSetting('kestrel_impact_total_files', 0);
        if (hasPywebviewApi && window.pywebview?.api?.get_settings) {
          try {
            const res = await window.pywebview.api.get_settings();
            const s = (res && res.success && res.settings && typeof res.settings === 'object') ? res.settings : null;
            if (s) {
              saveSettings({ ...loadSettings(), ...s });
              total = Number(s.kestrel_impact_total_files || total || 0);
            }
          } catch (_) { }
        }
        kdebug('[donation] checkDonationThresholdOnStartup: total =', total);
        if (total < 1000) {
          kdebug('[donation] Total < 1000, skipping');
          return;
        }
        const thresholds = [1000, 5000, 10000, 25000, 50000, 100000, 200000];
        const shown = _loadDonateThresholdsShown();
        kdebug('[donation] Thresholds already shown:', shown);
        for (const t of thresholds) {
          if (total >= t && !shown.includes(t)) {
            kdebug('[donation] Milestone crossed:', t, '- showing dialog');
            shown.push(t);
            _saveDonateThresholdsShown(shown);
            // Show dialog after a brief delay to let UI settle
            setTimeout(() => showDonatePrompt(total), 1000);
            break;
          }
        }
        if (shown.includes(1000)) {
          kdebug('[donation] 1000 threshold already shown, no dialog needed');
        }
      } catch (e) {
        console.error('[donation] checkDonationThresholdOnStartup error:', e);
      }
    }

    document.getElementById('donateBtnMain')?.addEventListener('click', openDonateLink);
    // Note: donateDlg button listeners are wired in the inline script after the dialog HTML,
    // because that dialog is defined after this script block and wouldn't be in the DOM yet.
    // ─── End Donation ─────────────────────────────────────────────────────

    async function readMetadata() {
      if (!rootPath || !window.pywebview?.api?.read_kestrel_metadata) {
        return { error: 'kestrel_metadata.json not found. Open a folder in desktop mode first.' };
      }
      try {
        const res = await window.pywebview.api.read_kestrel_metadata(rootPath);
        if (!res?.success || !res?.metadata || typeof res.metadata !== 'object') {
          return { error: res?.error || 'Unable to read kestrel_metadata.json' };
        }
        return res.metadata;
      } catch {
        return { error: 'Unable to read kestrel_metadata.json' };
      }
    }
    async function openInfo() {
      const dlg = document.getElementById('infoDlg');
      const contentEl = document.getElementById('infoContent');
      const noticeEl = document.getElementById('infoNotice');
      contentEl.textContent = 'Loading…';
      noticeEl.classList.add('hidden');
      dlg.showModal();
      const meta = await readMetadata();
      if (meta && !meta.error) {
        // Add derived helper fields (non-destructive)
        const enriched = { ...meta };
        if (rootPath) enriched.photo_root_name = rootPath.replace(/.*[/\\]/, '') || rootPath;
        contentEl.textContent = JSON.stringify(enriched, null, 2);
      } else {
        contentEl.textContent = '—';
        noticeEl.textContent = meta.error;
        noticeEl.classList.remove('hidden');
      }
    }
    const openInfoBtn = document.getElementById('openInfo');
    if (openInfoBtn) openInfoBtn.addEventListener('click', openInfo);
    const infoCloseBtn = document.getElementById('infoClose');
    if (infoCloseBtn) infoCloseBtn.addEventListener('click', () => document.getElementById('infoDlg').close());

    // Helper to infer root from absolute export/crop path strings in CSV
    function inferRootFromAbsPath(p) {
      if (!p) return null;
      const s = sanitizePath(p);
      const i = s.toLowerCase().lastIndexOf('/.kestrel/');
      if (i > 0) return s.substring(0, i);
      return null;
    }

    // Display names for each editor key used by the preferred-editor setting.
    // Mirrors the <option> list in visualizer.html so the button label reads
    // naturally (e.g. "Open in Lightroom") instead of raw keys like "lightroom".
    const _EDITOR_DISPLAY_NAMES = {
      system: 'Default App',
      darktable: 'Darktable',
      lightroom: 'Lightroom',
      photoshop: 'Photoshop',
      capture_one: 'Capture One',
      affinity: 'Affinity',
      gimp: 'GIMP',
      rawtherapee: 'RawTherapee',
      luminar: 'Luminar',
      dxo: 'DxO PhotoLab',
      on1: 'ON1',
      acdsee: 'ACDSee',
      paintshop: 'PaintShop',
      faststone: 'FastStone',
      xnview: 'XnView',
      irfanview: 'IrfanView',
      custom: 'Editor',
    };
    function _editorDisplayName(key) {
      if (!key) return 'Editor';
      return _EDITOR_DISPLAY_NAMES[key] || 'Editor';
    }

    async function openInEditor(row) {
      const origRel = (row.filename || '').replace(/^[\\/]+/, '');
      const settings = loadSettings();

      // Use the same root-finding logic as getBlobUrlForPath (which successfully loads thumbnails)
      // PRIORITY 1: Row-specific root (set when loaded from a folder or multi-load)
      let rootToSend = (row.__rootPath || '').trim();

      // PRIORITY 2: Global rootPath (set when loading CSV from a folder)
      if (!rootToSend && rootPath) {
        rootToSend = rootPath;
      }

      // PRIORITY 3: Settings hint (explicit user configuration)
      if (!rootToSend) {
        rootToSend = (settings.rootHint || '').trim();
      }

      // PRIORITY 4: Infer from absolute paths in CSV
      if (!rootToSend) {
        rootToSend = inferRootFromAbsPath(row.export_path) || inferRootFromAbsPath(row.crop_path) || '';
      }

      if (!origRel) { setStatus('No filename available for this row.'); return; }
      if (!rootToSend) { setStatus('Set Local Root in Settings to enable launching originals.'); showSettings(); return; }
      const editor = getSetting('editor', 'system');
      try {
        if (!window.pywebview?.api?.open_in_editor) {
          throw new Error('Desktop API unavailable: open_in_editor');
        }
        const data = await window.pywebview.api.open_in_editor(rootToSend, origRel, editor);
        if (data && data.success) {
          setStatus('Opened in editor');
          showToast('Opened in ' + editor, 5000, () => showSettings());
        } else throw new Error(data && data.error || 'Launch failed');
      } catch (e) {
        setStatus('Failed to open in editor. Check Settings and Local Root.');
      }
    }

    // ── Folder Tree ──────────────────────────────────────────────────────────────
    let folderTreeRoot = null;       // absolute path of the scanned tree root
    let folderTreeData = null;       // raw children array from API
    let folderTreeRootNode = null;   // synthetic root node {name, path, has_kestrel, children}
    let folderTreeRootHasKestrel = false;
    let treeExpandedPaths = new Set();
    let treeActivePath = null;       // currently single-loaded folder
    let checkedFolderPaths = new Set(); // folders checked for multi-load
    let _checkedFolderPathSnapshot = new Map(); // normalized path -> original path
    let queuedFolderPaths = new Set(); // folders queued for analysis (dialog selection)
    let _treeFlatOrder = [];           // flat ordered list of visible tree paths for range-select
    let _appVersion = '';              // current app version, fetched once
    let _isFrozenApp = false;          // whether running as frozen (PyInstaller) build
    let _appPlatform = 'windows';      // 'windows' | 'macos' | 'linux'

    async function scanFolderTree(rootPath) {
      if (!hasPywebviewApi || !window.pywebview?.api?.list_subfolders) return false;
      if (!rootPath) return false;

      // Fetch app version once (for outdated-version detection)
      if (!_appVersion && window.pywebview?.api?.get_app_version) {
        try {
          const vr = await window.pywebview.api.get_app_version();
          if (vr && vr.success) _appVersion = vr.version || '';
        } catch (e) { /* ignore */ }
      }
      // Fetch frozen status once
      if (!_isFrozenApp && window.pywebview?.api?.is_frozen_app) {
        try {
          const fr = await window.pywebview.api.is_frozen_app();
          _isFrozenApp = !!(fr && fr.frozen);
        } catch (e) { /* ignore */ }
      }
      // Fetch platform once
      if (_appPlatform === 'windows') {
        try {
          _appPlatform = await getPlatformInfo();
        } catch (e) { /* ignore */ }
      }

      folderTreeRoot = rootPath;
      const depth = getSetting('treeScanDepth', 3);
      setStatus('Scanning folder tree…');
      try {
        const result = await window.pywebview.api.list_subfolders(rootPath, depth);
        if (!result.success) {
          console.warn('[tree] list_subfolders failed:', result.error);
          return false;
        }
        folderTreeData = result.tree;
        folderTreeRootHasKestrel = !!result.root_has_kestrel;
        // Build a synthetic root node so the tree shows the top-level folder too
        const rootName = rootPath.replace(/\\/g, '/').split('/').filter(Boolean).pop() || rootPath;
        folderTreeRootNode = {
          name: rootName,
          path: rootPath,
          has_kestrel: folderTreeRootHasKestrel,
          kestrel_version: result.root_kestrel_version || '',
          children: folderTreeData,
        };
        // Auto-expand the root
        treeExpandedPaths.add(rootPath);
        renderFolderTree();
        // Enable folder tree controls and remove empty placeholder state
        const treeWrap = document.getElementById('folderTreeWrap');
        treeWrap.classList.remove('folder-tree-empty');
        treeWrap.querySelectorAll('button[disabled]').forEach(b => b.removeAttribute('disabled'));
        return true;
      } catch (e) {
        console.error('[tree] scanFolderTree error:', e);
        return false;
      }
    }

    /** Compare two semver strings. Returns -1 if a < b, 0 if equal, 1 if a > b. */
    function compareVersions(a, b) {
      if (!a || !b) return 0;
      const pa = a.split('.').map(Number), pb = b.split('.').map(Number);
      for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
        const na = pa[i] || 0, nb = pb[i] || 0;
        if (na < nb) return -1;
        if (na > nb) return 1;
      }
      return 0;
    }

    /** Check if a node's kestrel_version is older than the current app version. */
    function isVersionOutdated(node) {
      if (!node || !node.has_kestrel || !node.kestrel_version || !_appVersion) return false;
      return compareVersions(node.kestrel_version, _appVersion) < 0;
    }

    /** Show a custom context menu at (x, y) with given items. */
    function showContextMenu(x, y, items) {
      dismissContextMenu();
      const menu = document.createElement('div');
      menu.className = 'kestrel-ctx-menu';
      menu.id = '_kestrelCtxMenu';
      for (const item of items) {
        const el = document.createElement('div');
        el.className = 'kestrel-ctx-menu-item' + (item.danger ? ' danger' : '');
        el.textContent = item.label;
        el.addEventListener('click', (e) => {
          e.stopPropagation();
          dismissContextMenu();
          item.action();
        });
        menu.appendChild(el);
      }
      menu.style.left = x + 'px';
      menu.style.top = y + 'px';
      document.body.appendChild(menu);
      // Adjust if off-screen
      const rect = menu.getBoundingClientRect();
      if (rect.right > window.innerWidth) menu.style.left = (window.innerWidth - rect.width - 8) + 'px';
      if (rect.bottom > window.innerHeight) menu.style.top = (window.innerHeight - rect.height - 8) + 'px';
      // Dismiss on any click outside
      setTimeout(() => document.addEventListener('click', dismissContextMenu, { once: true }), 0);
    }

    function dismissContextMenu() {
      const old = document.getElementById('_kestrelCtxMenu');
      if (old) old.remove();
    }

    /** Clear kestrel analysis data for a folder (with confirmation). */
    async function clearKestrelDataForFolder(folderPath, folderName, refreshCallback) {
      const confirmed = confirm(
        `Are you sure you want to delete all Kestrel analysis data for "${folderName}"?\n\n` +
        `This will permanently remove the .kestrel folder and all its contents (database, exports, thumbnails) ` +
        `in:\n${folderPath}\n\nThis action cannot be undone.`
      );
      if (!confirmed) return;
      try {
        const result = await window.pywebview.api.clear_kestrel_data(folderPath);
        if (result && result.success) {
          showToast('Kestrel analysis data cleared for ' + folderName);
          if (refreshCallback) refreshCallback();
        } else {
          alert('Failed to clear analysis data:\n\n' + (result?.error || 'Unknown error'));
        }
      } catch (e) {
        alert('Failed to clear analysis data:\n\n' + (e.message || e));
      }
    }

    function renderFolderTree() {
      const container = document.getElementById('folderTree');
      if (!container || !folderTreeRootNode) return;
      // Rebuild the flat visible order for shift-range selection
      _treeFlatOrder = [];
      container.innerHTML = '';
      container.appendChild(buildTreeNode(folderTreeRootNode, _treeFlatOrder));
      // Note: Do NOT populate counts for main folder tree
      // The main tree is only for selecting which analyzed folders to LOAD
      // Colors and counts are only for the Analyze dialog tree
    }

    /** Update a single main-folder-tree row for `path` without re-rendering whole tree.
     *  Makes the node appear as having kestrel data (icon + checkbox) but does not
     *  change selection or checked state. This avoids disturbing the user's view.
     */
    function updateFolderTreeNode(path) {
      try {
        const norm = p => (p || '').replace(/\\/g, '/');
        const target = norm(path);
        // Find rows in the main folder tree matching this path
        const rows = Array.from(document.querySelectorAll('#folderTree .tree-node-row'));
        for (const row of rows) {
          const rp = norm(row.dataset.path || '');
          if (rp !== target) continue;
          // Update classes
          row.classList.remove('no-kestrel');
          row.classList.add('has-kestrel');
          // Persist a transient marker so future rescans don't immediately clear it
          try { _tempKestrelPaths.add(norm(path)); } catch (e) { }
          // Update icon
          const icon = row.querySelector('.tree-icon');
          if (icon) icon.textContent = '📂';
          // Ensure checkbox exists (do not auto-check it)
          if (!row.querySelector('.tree-cb')) {
            const cb = document.createElement('input');
            cb.type = 'checkbox';
            cb.className = 'tree-cb';
            cb.title = 'Include in multi-folder view';
            cb.checked = _isPathChecked(row.dataset.path);
            cb.addEventListener('change', (e) => {
              e.stopPropagation();
              if (cb.checked) checkedFolderPaths.add(row.dataset.path);
              else checkedFolderPaths.delete(row.dataset.path);
              debouncedAutoLoad();
            });
            // Insert before the icon element if present
            if (icon && icon.parentNode) icon.parentNode.insertBefore(cb, icon);
            else row.insertBefore(cb, row.firstChild);
          }
        }
      } catch (e) { /* failsafe */ }
    }

    // Build a single tree node DOM element.
    // flatOrder is mutated to collect visible paths in order (for range-select).
    function buildTreeNode(node, flatOrder) {
      flatOrder.push(node.path);

      const wrap = document.createElement('div');
      wrap.className = 'tree-node';

      const row = document.createElement('div');

      function subtreeHasKestrel(n) {
        if (!n) return false;
        if (n.has_kestrel) return true;
        if (!n.children) return false;
        for (const c of n.children) if (subtreeHasKestrel(c)) return true;
        return false;
      }

      const norm = p => (p || '').replace(/\\/g, '/');
      const normPath = norm(node.path);
      const isInProgress = _inProgressFolderPaths.has(normPath);
      
      const effectiveHasKestrel = subtreeHasKestrel(node) || isInProgress; // Show checkbox for in-progress too
      const outdated = isVersionOutdated(node);
      row.className = 'tree-node-row ' + (effectiveHasKestrel ? 'has-kestrel' : 'no-kestrel') + (outdated ? ' version-outdated' : '') + (isInProgress ? ' in-progress' : '');
      if (node.path === treeActivePath) row.classList.add('active');
      if (isInProgress) row.title = 'Currently analyzing...';
      else if (outdated) row.title = `Analyzed on Kestrel v${node.kestrel_version} (current: v${_appVersion})`;

      // Arrow toggle
      const arrow = document.createElement('span');
      arrow.className = 'tree-arrow';
      const hasChildren = node.children && node.children.length > 0;
      if (hasChildren) {
        arrow.textContent = '▶';
        if (treeExpandedPaths.has(node.path)) arrow.classList.add('open');
      } else {
        arrow.classList.add('leaf');
        arrow.textContent = '▶';
      }

      // Checkbox for loading ANALYZED folders OR in-progress folders (blue accent)
      let loadCheckbox = null;
      if (node.has_kestrel || isInProgress) {
        loadCheckbox = document.createElement('input');
        loadCheckbox.type = 'checkbox';
        loadCheckbox.className = 'tree-cb';
        loadCheckbox.title = isInProgress ? 'Include in multi-folder view (analyzing now)' : 'Include in multi-folder view';
        loadCheckbox.checked = _isPathChecked(node.path);
        loadCheckbox.addEventListener('change', (e) => {
          e.stopPropagation();
          if (loadCheckbox.checked) checkedFolderPaths.add(node.path);
          else checkedFolderPaths.delete(node.path);
          _updateAutoRefreshTimers();
          debouncedAutoLoad();
        });
      }

      // Folder icon
      const icon = document.createElement('span');
      icon.className = 'tree-icon';
      icon.textContent = node.has_kestrel ? '📂' : '📁';

      // Label
      const label = document.createElement('span');
      label.className = 'tree-label';
      label.textContent = node.name;
      label.title = node.path;

      // Attach path to the row for async inspection
      row.dataset.path = node.path;

      // Count placeholder (populated asynchronously)
      const countSpan = document.createElement('span');
      countSpan.className = 'tree-count';
      countSpan.textContent = '';

      row.appendChild(arrow);
      if (loadCheckbox) {
        row.appendChild(loadCheckbox);
      } else {
        // Always keep a fixed-width spacer so folder icons at every level align
        const spacer = document.createElement('span');
        spacer.className = 'tree-cb-spacer';
        row.appendChild(spacer);
      }
      row.appendChild(icon);
      row.appendChild(label);
      row.appendChild(countSpan);
      wrap.appendChild(row);

      // Children container
      let childWrap = null;
      if (hasChildren) {
        childWrap = document.createElement('div');
        childWrap.className = 'tree-children';
        if (!treeExpandedPaths.has(node.path)) childWrap.classList.add('hidden');
        node.children.forEach(child => childWrap.appendChild(buildTreeNode(child, flatOrder)));
        wrap.appendChild(childWrap);

        arrow.addEventListener('click', (e) => {
          e.stopPropagation();
          const open = treeExpandedPaths.has(node.path);
          if (open) {
            treeExpandedPaths.delete(node.path);
            arrow.classList.remove('open');
            childWrap.classList.add('hidden');
          } else {
            treeExpandedPaths.add(node.path);
            arrow.classList.add('open');
            childWrap.classList.remove('hidden');
          }
        });
      }

      // Click label/icon to load (only if this node has kestrel data)
      if (node.has_kestrel) {
        label.addEventListener('click', async () => {
          treeActivePath = node.path;
          renderFolderTree();
          await loadFolderFromPath(node.path);
        });
        icon.addEventListener('click', async () => {
          treeActivePath = node.path;
          renderFolderTree();
          await loadFolderFromPath(node.path);
        });
        // Right-click context menu for clearing analysis data
        row.addEventListener('contextmenu', (e) => {
          e.preventDefault();
          e.stopPropagation();
          showContextMenu(e.clientX, e.clientY, [
            {
              label: '🗑 Clear Kestrel Analysis Data',
              danger: true,
              action: () => {
                clearKestrelDataForFolder(node.path, node.name, () => {
                  node.has_kestrel = false;
                  node.kestrel_version = '';
                  renderFolderTree();
                });
              }
            }
          ]);
        });
      }

      return wrap;
    }

    // Populate folder counts ONLY for the Analyze Folders dialog tree.
    // Two-pass approach: (1) inspect all folders, (2) apply subtree-aware fading & colors.
    // Colors: green=finished, purple=started-not-finished, blue=not-started-but-has-images.
    // Fading: no-photos-deep = folder+all descendants have 0 images (full row faded).
    //         no-photos-shallow = folder has 0 images but some descendant has images (checkbox faded only).
    async function populateAnalyzeFolderCounts() {
      if (!hasPywebviewApi || !window.pywebview?.api?.inspect_folder) return;
      try {
        // Query ONLY the Analyze dialog tree nodes
        const rows = Array.from(document.querySelectorAll('#analyzeDlgTree .adlg-node-row'));
        const norm = p => (p || '').replace(/\\/g, '/');
        const pathToRows = new Map(); // normalized path → [row, ...]
        const normToOriginal = new Map();
        const paths = [];

        for (const row of rows) {
          const origPath = row.dataset.path;
          if (!origPath) continue;
          const np = norm(origPath);
          if (!pathToRows.has(np)) {
            pathToRows.set(np, []);
            normToOriginal.set(np, origPath);
            paths.push(np);
          }
          pathToRows.get(np).push(row);
          const span = row.querySelector('.tree-count');
          if (span) span.textContent = '';
        }
        if (paths.length === 0) return;

        const uniq = Array.from(new Set(paths));
        uniq.sort((a, b) => {
          const da = a.split('/').length, db = b.split('/').length;
          return da !== db ? da - db : a.length - b.length;
        });

        const total = uniq.length;
        let completed = 0;
        // Store inspection results for the second pass
        const inspectionMap = new Map(); // normalized path → { total, processed } | null

        const dlgProgWrap = document.getElementById('analyzeScanProgress');
        const dlgProgFill = document.getElementById('analyzeScanFill');
        const dlgProgLabel = document.getElementById('analyzeScanLabel');
        if (dlgProgWrap) dlgProgWrap.classList.remove('hidden');

        // ── Pass 1: Inspect all folders concurrently ──
        const concurrency = Math.min(8, Math.max(2, Math.ceil(total / 8)));
        let idx = 0;

        async function worker() {
          while (true) {
            const i = idx++;
            if (i >= total) break;
            const np = uniq[i];
            const origPath = normToOriginal.get(np) || np;
            try {
              for (const r of (pathToRows.get(np) || [])) {
                const s = r.querySelector('.tree-count');
                if (s) s.textContent = ' …';
              }
              const res = await window.pywebview.api.inspect_folder(origPath);
              const info = res && res.success ? res.info : null;
              inspectionMap.set(np, info ? { total: info.total || 0, processed: info.processed || 0 } : null);
            } catch (e) {
              console.warn('[populateAnalyzeFolderCounts] error for', origPath, e);
              inspectionMap.set(np, null);
            }
            completed++;
            const pct = Math.round((completed / total) * 100);
            if (dlgProgFill) dlgProgFill.style.width = pct + '%';
            if (dlgProgLabel) dlgProgLabel.textContent = `Scanning folders… (${completed}/${total})`;
          }
        }

        const workers = [];
        for (let w = 0; w < concurrency; w++) workers.push(worker());
        await Promise.all(workers);

        // ── Pass 2: Apply colors and subtree-aware fading ──
        // Helper: does any descendant of `prefix` have images?
        function subtreeHasImages(prefix) {
          const pfx = prefix.endsWith('/') ? prefix : prefix + '/';
          for (const [p, info] of inspectionMap) {
            if (p !== prefix && p.startsWith(pfx) && info && info.total > 0) return true;
          }
          return false;
        }

        // Helper: look up kestrel_version from tree node by path
        function findNodeVersion(node, targetPath) {
          if (!node) return '';
          if (node.path === targetPath) return node.kestrel_version || '';
          if (node.children) {
            for (const c of node.children) {
              const v = findNodeVersion(c, targetPath);
              if (v) return v;
            }
          }
          return '';
        }

        for (const np of uniq) {
          const info = inspectionMap.get(np);
          const related = pathToRows.get(np) || [];
          for (const row of related) {
            const span = row.querySelector('.tree-count');
            row.classList.remove('analyzed-full', 'analyzed-partial', 'analyzed-none',
                                 'no-photos', 'no-photos-deep', 'no-photos-shallow', 'version-outdated',
                                 'has-errored-images');
            row.title = '';
            if (span) { span.title = ''; span.textContent = ''; }

            if (!info) continue;

            const totalImgs = info.total;
            const processedImgs = info.processed;
            const erroredImgs = info.errored || 0;

            if (totalImgs > 0) {
              const countText = erroredImgs > 0
                ? ` ${processedImgs}/${totalImgs} (${erroredImgs} errored)`
                : ` ${processedImgs}/${totalImgs}`;
              if (span) span.textContent = countText;
              if (erroredImgs > 0) {
                row.classList.add('has-errored-images');
              }
              if (processedImgs >= totalImgs && erroredImgs === 0) {
                row.classList.add('analyzed-full');          // green: finished, no errors
                // Check if analyzed on an outdated version
                const origPath = normToOriginal.get(np) || np;
                const nodeVer = findNodeVersion(folderTreeRootNode, origPath);
                if (nodeVer && _appVersion && compareVersions(nodeVer, _appVersion) < 0) {
                  row.classList.add('version-outdated');
                  row.title = `Analyzed on Kestrel v${nodeVer} (current: v${_appVersion}). Consider re-analyzing.`;
                }
              } else if (processedImgs > 0) {
                row.classList.add('analyzed-partial');       // purple: started not finished, OR has errors
              } else {
                row.classList.add('analyzed-none');          // blue: has images, not started
              }
              if (erroredImgs > 0) {
                row.title = `${erroredImgs} image(s) errored during the previous analysis. Tick "Re-attempt errored images" before queuing to retry just those.`;
              }
            } else {
              // This folder has 0 images — determine deep vs shallow fading
              const hasDescendantImages = subtreeHasImages(np);
              if (hasDescendantImages) {
                // Shallow: only fade the checkbox, not the name/arrow (descendant has images)
                row.classList.add('no-photos-shallow');
              } else {
                // Deep: entire row faded (no images anywhere in subtree)
                row.classList.add('no-photos-deep');
              }
              const cb = row.querySelector('.adlg-cb');
              if (cb) { cb.disabled = true; cb.checked = false; }
              const tip = hasDescendantImages
                ? 'No photos in this folder, but subfolders contain images.'
                : 'No supported photos found in this folder or any subfolder.';
              if (span) span.title = tip;
              row.title = tip;
            }
          }
        }

        // Hide progress after brief delay
        setTimeout(() => {
          if (dlgProgWrap) dlgProgWrap.classList.add('hidden');
          if (dlgProgFill) dlgProgFill.style.width = '0%';
          if (dlgProgLabel) dlgProgLabel.textContent = 'Scanning folders…';
        }, 400);
      } catch (e) {
        console.warn('[populateAnalyzeFolderCounts] failed', e);
        const dlgWrap = document.getElementById('analyzeScanProgress');
        if (dlgWrap) dlgWrap.classList.add('hidden');
      }
    }

    // ── End Folder Tree ───────────────────────────────────────────────────────────

    // ── Analyze Folders Dialog ───────────────────────────────────────────────────

    let _dlgSelected = new Set();
    let _dlgExpandedPaths = new Set();
    let _dlgReanalyze = new Set(); // paths confirmed for re-analysis (fully analyzed folders)

    /** Build a tree node for the Analyze dialog (amber checkboxes, no load-cb). */
    function buildAnalyzeDlgNode(node, selectedSet, onChangeCallback) {
      const wrap = document.createElement('div');
      wrap.className = 'tree-node';

      const row = document.createElement('div');
      const hasChildren = node.children && node.children.length > 0;
      const isExpanded = _dlgExpandedPaths.has(node.path);
      const outdated = isVersionOutdated(node);
      row.className = 'adlg-node-row' + (selectedSet.has(node.path) ? ' queue-sel' : '') + (node.has_kestrel ? ' has-kestrel' : '') + (outdated ? ' version-outdated' : '');
      if (outdated) {
        row.title = `Analyzed on Kestrel v${node.kestrel_version} (current: v${_appVersion}). Consider re-analyzing.`;
      }

      const arrow = document.createElement('span');
      arrow.className = 'tree-arrow' + (hasChildren ? (isExpanded ? ' open' : '') : ' leaf');
      arrow.textContent = hasChildren ? '▶' : '';

      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.className = 'adlg-cb';
      cb.checked = selectedSet.has(node.path);
      cb.addEventListener('change', (e) => {
        e.stopPropagation();
        if (cb.checked) {
          // Prompt before re-queuing a fully analyzed folder
          if (row.classList.contains('analyzed-full')) {
            const confirmed = confirm(
              `"${node.name}" has already been fully analyzed.\n\n` +
              `Re-analyzing will delete the existing analysis data (.kestrel folder) and process it again.\n\n` +
              `Continue?`
            );
            if (!confirmed) { cb.checked = false; return; }
            _dlgReanalyze.add(node.path);
          }
          selectedSet.add(node.path);
        } else {
          selectedSet.delete(node.path);
          _dlgReanalyze.delete(node.path);
        }
        row.classList.toggle('queue-sel', cb.checked);
        onChangeCallback();
      });

      const icon = document.createElement('span');
      icon.className = 'tree-icon';
      icon.textContent = node.has_kestrel ? '\uD83D\uDCC2' : '\uD83D\uDCC1';

      const label = document.createElement('span');
      label.className = 'tree-label';
      label.textContent = node.name;
      if (!outdated) label.title = node.path;
      else label.title = `v${node.kestrel_version} → v${_appVersion} (outdated)`;

      // Version badge for outdated folders
      const versionBadge = document.createElement('span');
      if (outdated) {
        versionBadge.style.cssText = 'font-size:10px;color:var(--ok);opacity:0.7;margin-left:4px;font-style:italic;';
        versionBadge.textContent = `v${node.kestrel_version}`;
      }

      // Attach path for async inspection and add count placeholder
      row.dataset.path = node.path;
      const countSpan = document.createElement('span');
      countSpan.className = 'tree-count';
      countSpan.textContent = '';

      row.appendChild(arrow);
      row.appendChild(cb);
      row.appendChild(icon);
      row.appendChild(label);
      if (outdated) row.appendChild(versionBadge);
      row.appendChild(countSpan);

      // Right-click context menu for clearing analysis data
      if (node.has_kestrel) {
        row.addEventListener('contextmenu', (e) => {
          e.preventDefault();
          e.stopPropagation();
          const folderName = node.name;
          showContextMenu(e.clientX, e.clientY, [
            {
              label: '🗑 Clear Kestrel Analysis Data',
              danger: true,
              action: () => {
                clearKestrelDataForFolder(node.path, folderName, () => {
                  // Update the node state in-memory
                  node.has_kestrel = false;
                  node.kestrel_version = '';
                  // Re-render the dialog tree
                  const treeEl = document.getElementById('analyzeDlgTree');
                  if (treeEl && folderTreeRootNode) {
                    treeEl.innerHTML = '';
                    treeEl.appendChild(buildAnalyzeDlgNode(folderTreeRootNode, _dlgSelected, onChangeCallback));
                    populateAnalyzeFolderCounts();
                  }
                });
              }
            }
          ]);
        });
      }

      wrap.appendChild(row);

      if (hasChildren) {
        const childWrap = document.createElement('div');
        childWrap.className = 'tree-children';
        if (!isExpanded) childWrap.classList.add('hidden');
        node.children.forEach(child => childWrap.appendChild(buildAnalyzeDlgNode(child, selectedSet, onChangeCallback)));
        wrap.appendChild(childWrap);

        arrow.addEventListener('click', (e) => {
          e.stopPropagation();
          const open = _dlgExpandedPaths.has(node.path);
          if (open) { _dlgExpandedPaths.delete(node.path); arrow.classList.remove('open'); childWrap.classList.add('hidden'); }
          else { _dlgExpandedPaths.add(node.path); arrow.classList.add('open'); childWrap.classList.remove('hidden'); }
        });
      }
      return wrap;
    }

    /** Render the right-side queue preview panel in the Analyze dialog.
     *  Shows: running items, pending items (draggable + removable), and "will be added" selection. */
    function _refreshAnalyzeDlgQueuePreview() {
      const runningEl = document.getElementById('adlgQueueRunning');
      const willAddEl = document.getElementById('adlgQueueWillAdd');
      const emptyEl = document.getElementById('adlgQueueEmpty');
      if (!runningEl || !willAddEl || !emptyEl) return;

      runningEl.innerHTML = '';
      willAddEl.innerHTML = '';

      let hasActiveQueue = false;

      try {
        const status = window._lastQueueStatus;
        if (status && status.items && status.items.length > 0) {
          const runningItems = status.items.filter(i => i.status === 'running');
          const pendingItems = status.items.filter(i => i.status === 'pending');

          // ── Running items ──
          if (runningItems.length > 0) {
            hasActiveQueue = true;
            const title = document.createElement('div');
            title.className = 'adlg-queue-section-title';
            title.textContent = '⚙ Analyzing';
            runningEl.appendChild(title);
            for (const item of runningItems) {
              const row = document.createElement('div');
              row.className = 'adlg-queue-item';
              const nameEl = document.createElement('span');
              nameEl.className = 'adlg-qi-name';
              nameEl.textContent = item.name;
              nameEl.title = item.path;
              const statusEl = document.createElement('span');
              statusEl.className = 'adlg-qi-status';
              statusEl.textContent = item.total > 0 ? `${item.processed}/${item.total}` : 'starting…';
              row.appendChild(nameEl);
              row.appendChild(statusEl);
              runningEl.appendChild(row);
            }
          }

          // ── Pending items (drag-to-reorder + cancel) ──
          if (pendingItems.length > 0) {
            hasActiveQueue = true;
            const pendTitle = document.createElement('div');
            pendTitle.className = 'adlg-queue-section-title';
            pendTitle.textContent = `⏳ In Queue (${pendingItems.length})`;
            runningEl.appendChild(pendTitle);

            let _dragSrcPath = null;
            const pendContainer = document.createElement('div');
            pendContainer.dataset.role = 'pending-list';

            for (const item of pendingItems) {
              const row = document.createElement('div');
              row.className = 'adlg-queue-item';
              row.draggable = true;
              row.dataset.queuePath = item.path;

              const grip = document.createElement('span');
              grip.className = 'adlg-qi-grip';
              grip.textContent = '⠿';
              grip.title = 'Drag to reorder';

              const nameEl = document.createElement('span');
              nameEl.className = 'adlg-qi-name';
              nameEl.textContent = item.name;
              nameEl.title = item.path;

              const statusEl = document.createElement('span');
              statusEl.className = 'adlg-qi-status';
              statusEl.textContent = 'pending';

              const removeBtn = document.createElement('button');
              removeBtn.className = 'adlg-qi-remove';
              removeBtn.textContent = '✕';
              removeBtn.title = 'Remove from queue';
              removeBtn.addEventListener('click', async (e) => {
                e.stopPropagation();
                if (hasPywebviewApi && window.pywebview?.api?.remove_queue_item) {
                  await window.pywebview.api.remove_queue_item(item.path);
                  const s = await apiGetQueueStatus();
                  renderQueuePanel(s);
                  _refreshAnalyzeDlgQueuePreview();
                }
              });

              // Drag events
              row.addEventListener('dragstart', (e) => {
                _dragSrcPath = item.path;
                row.classList.add('dragging');
                e.dataTransfer.effectAllowed = 'move';
                e.dataTransfer.setData('text/plain', item.path);
              });
              row.addEventListener('dragend', () => {
                _dragSrcPath = null;
                row.classList.remove('dragging');
                pendContainer.querySelectorAll('.drag-over').forEach(el => el.classList.remove('drag-over'));
              });
              row.addEventListener('dragover', (e) => {
                e.preventDefault();
                e.dataTransfer.dropEffect = 'move';
                if (row.dataset.queuePath !== _dragSrcPath) {
                  row.classList.add('drag-over');
                }
              });
              row.addEventListener('dragleave', () => {
                row.classList.remove('drag-over');
              });
              row.addEventListener('drop', async (e) => {
                e.preventDefault();
                row.classList.remove('drag-over');
                const srcPath = e.dataTransfer.getData('text/plain');
                if (!srcPath || srcPath === item.path) return;
                const currentOrder = Array.from(pendContainer.querySelectorAll('[data-queue-path]'))
                  .map(el => el.dataset.queuePath);
                const filtered = currentOrder.filter(p => p !== srcPath);
                const targetIdx = filtered.indexOf(item.path);
                filtered.splice(targetIdx, 0, srcPath);
                if (hasPywebviewApi && window.pywebview?.api?.reorder_queue) {
                  await window.pywebview.api.reorder_queue(JSON.stringify(filtered));
                  const s = await apiGetQueueStatus();
                  renderQueuePanel(s);
                  _refreshAnalyzeDlgQueuePreview();
                }
              });

              row.appendChild(grip);
              row.appendChild(nameEl);
              row.appendChild(statusEl);
              row.appendChild(removeBtn);
              pendContainer.appendChild(row);
            }
            runningEl.appendChild(pendContainer);
          }
        }
      } catch (_) { }

      // ── Will be added (selected but not yet queued) ──
      const selected = Array.from(_dlgSelected);
      if (selected.length > 0) {
        const title = document.createElement('div');
        title.className = 'adlg-queue-section-title';
        title.textContent = `➕ Will Be Added (${selected.length})`;
        willAddEl.appendChild(title);
        for (const path of selected) {
          const name = path.replace(/\\/g, '/').split('/').pop() || path;
          const row = document.createElement('div');
          row.className = 'adlg-queue-item';
          const nameEl = document.createElement('span');
          nameEl.className = 'adlg-qi-name';
          nameEl.textContent = name;
          nameEl.title = path;
          const removeBtn = document.createElement('button');
          removeBtn.className = 'adlg-qi-remove';
          removeBtn.textContent = '✕';
          removeBtn.title = 'Remove from selection';
          removeBtn.addEventListener('click', () => {
            _dlgSelected.delete(path);
            _dlgReanalyze.delete(path);
            const treeRows = document.querySelectorAll('#analyzeDlgTree .adlg-node-row');
            for (const r of treeRows) {
              if (r.dataset.path === path) {
                const cb = r.querySelector('.adlg-cb');
                if (cb) cb.checked = false;
                r.classList.remove('queue-sel');
              }
            }
            const countEl = document.getElementById('analyzeDlgCount');
            const addBtn = document.getElementById('analyzeDlgAdd');
            if (countEl) countEl.textContent = _dlgSelected.size + ' folder' + (_dlgSelected.size === 1 ? '' : 's') + ' selected';
            if (addBtn) addBtn.disabled = _dlgSelected.size === 0;
            _refreshAnalyzeDlgQueuePreview();
          });
          row.appendChild(nameEl);
          if (_dlgReanalyze.has(path)) {
            const badge = document.createElement('span');
            badge.className = 'adlg-qi-status';
            badge.style.color = '#f0a040';
            badge.style.fontStyle = 'italic';
            badge.textContent = 'Will be Re-analyzed';
            row.appendChild(badge);
          }
          row.appendChild(removeBtn);
          willAddEl.appendChild(row);
        }
      }

      emptyEl.classList.toggle('hidden', hasActiveQueue || selected.length > 0);
    }

    /** Open the 'Analyze Folders…' dialog. */
    async function openAnalyzeDialog() {
      if (!hasPywebviewApi) {
        alert('Analysis queue is only available in the desktop (pywebview) mode.\n\nRun kestrel_visualizer as a desktop app to use this feature.');
        return;
      }
      // Make sure we have a tree to browse
      if (!folderTreeRootNode) {
        const fp = await window.pywebview.api.choose_directory();
        if (!fp) return;
        await scanFolderTree(fp);
        if (!folderTreeRootNode) return;
      }
      // GPU is always available: DirectML (Windows) and CoreML (macOS) are bundled
      // with the frozen build, so no platform-specific hiding is needed.
      // Seed the dialog's selected set from any previously-queued paths
      _dlgSelected = new Set(queuedFolderPaths);
      
      // Try to restore last queue state if available
      const savedQueue = getSetting('lastQueueState', null);
      if (savedQueue && Array.isArray(savedQueue) && savedQueue.length > 0) {
        const restoreBtn = document.getElementById('analyzeDlgRestoreQueue');
        if (restoreBtn) {
          restoreBtn.style.display = '';
          restoreBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            _dlgSelected = new Set(savedQueue);
            // Restore queue in settings so it looks restored
            const s = loadSettings();
            delete s.lastQueueState;
            saveSettings(s);
            restoreBtn.style.display = 'none';
            refreshDlg();
          }, { once: true }); // only trigger once per dialog open
        }
      }
      
      _dlgExpandedPaths = new Set([folderTreeRootNode.path]);
      _dlgReanalyze = new Set();

      function refreshDlg() {
        const countEl = document.getElementById('analyzeDlgCount');
        const addBtn = document.getElementById('analyzeDlgAdd');
        if (countEl) countEl.textContent = _dlgSelected.size + ' folder' + (_dlgSelected.size === 1 ? '' : 's') + ' selected';
        if (addBtn) addBtn.disabled = _dlgSelected.size === 0;
        _refreshAnalyzeDlgQueuePreview();
      }

      // Hydrate advanced analysis settings from persisted values
      const _adlgDt = document.getElementById('adlgDetectionThreshold');
      if (_adlgDt) _adlgDt.value = getSetting('detection_threshold', 0.25);
      const _adlgMbc = document.getElementById('adlgMaxBirdCrops');
      if (_adlgMbc) _adlgMbc.value = getSetting('max_bird_crops', 10);
      const _adlgEq = document.getElementById('adlgExposureQuality');
      if (_adlgEq) {
        const savedEq = String(getSetting('exposure_quality', 'balanced') || 'balanced').toLowerCase();
        _adlgEq.value = ['lenient', 'balanced', 'aggressive'].includes(savedEq) ? savedEq : 'balanced';
      }
      const _adlgModelMode = document.getElementById('adlgWildlifeModelMode');
      if (_adlgModelMode) {
        const savedMode = String(getSetting('wildlife_model_mode', 'accurate') || 'accurate').toLowerCase();
        _adlgModelMode.value = (savedMode === 'fast') ? 'fast' : 'accurate';
      }
      const _adlgSt = document.getElementById('adlgSceneTime');
      if (_adlgSt) _adlgSt.value = getSetting('scene_time_threshold', 1.0);
      const _adlgPp = document.getElementById('adlgParallelPrefetch');
      if (_adlgPp) _adlgPp.value = getSetting('parallel_prefetch', 3);
      const _adlgThumbW = document.getElementById('adlgThumbnailMaxWidth');
      if (_adlgThumbW) {
        const savedW = parseInt(getSetting('thumbnail_max_width', 1200), 10);
        _adlgThumbW.value = Math.max(400, Math.min(2400, Number.isFinite(savedW) ? savedW : 1200));
      }
      const _adlgThumbComp = document.getElementById('adlgThumbnailJpegCompression');
      if (_adlgThumbComp) {
        let compression = parseFloat(getSetting('thumbnail_jpeg_compression', Number.NaN));
        if (!Number.isFinite(compression)) {
          const legacyQuality = parseInt(getSetting('thumbnail_jpeg_quality', 75), 10);
          compression = (Number.isFinite(legacyQuality) ? legacyQuality : 75) / 100;
        }
        compression = Math.max(0.5, Math.min(1.0, compression));
        _adlgThumbComp.value = compression.toFixed(2);
      }

      const treeEl = document.getElementById('analyzeDlgTree');
      treeEl.innerHTML = '';
      treeEl.appendChild(buildAnalyzeDlgNode(folderTreeRootNode, _dlgSelected, refreshDlg));
      // Populate counts for dialog nodes with colors and progress bar
      populateAnalyzeFolderCounts();
      refreshDlg();
      document.getElementById('analyzeQueueDlg').showModal();
    }

    // ── Analysis Queue Panel / Polling ───────────────────────────────────────────

    let _queuePollingTimer = null;
    let _queuePanelExpanded = true;
    let _queueLastDoneSet = new Set(); // track newly-done folders to auto-refresh tree
    let _queueLastRunningSet = new Set(); // track newly-running folders to update tree
    let _tempKestrelPaths = new Set(); // transiently-marked paths to prevent flicker
    let _analyticsConsentPending = false; // guard against showing consent dialog multiple times
    let _queueCountsTimer = null; // interval for updating folder counts from queue
    
    // In-progress folder tracking and auto-refresh for live updates
    let _inProgressFolderPaths = new Set(); // folders with pending/running status
    let _autoRefreshTimers = new Map(); // path -> intervalId for auto-refresh listeners
    let _inProgressFoldersCheckedCount = 0; // count of in-progress folders that are checked
    let _isFirstQueueStart = true; // used to detect Case 1 vs Case 2 for auto-load
    
    // Session state for ETA calculations: track baseline state from folder inspection
    let _queueSessionStartState = new Map(); // path -> { initialProcessed: int, totalImages: int, toAnalyze: int }
    let _queueFolderInspections = new Map(); // path -> full inspection data from inspect_folder/inspect_folders
    // ETA smoothing: exponential moving average to prevent wild per-image swings
    let _etaSmoothed = null;   // smoothed secs/image
    let _etaLastPath = null;   // reset EMA when folder changes
    const _thumbCache = new Map();    // relPath+'|'+rootPath → dataUrl (avoids reload flash)
    let _liveAnalysisDlgOpen = false;
    let _liveLastThumbKey = '';
    let _liveLastOverlayKey = '';
    let _liveLastCropKeys = [];
    const CONF_HIGH = 0.75;
    const CONF_LOW = 0.30;

    /** Call the backend queue API (desktop pywebview mode only). */
    async function apiStartQueue(paths, useGpu = true, wildlifeEnabled = true, retryErrored = false, speciesDetectionEnabled = true) {
      if (!window.pywebview?.api?.start_analysis_queue) {
        throw new Error('Desktop API unavailable: start_analysis_queue');
      }
      return window.pywebview.api.start_analysis_queue(JSON.stringify(paths), useGpu, wildlifeEnabled, retryErrored, speciesDetectionEnabled);
    }

    async function apiQueueControl(action) {
      if (!window.pywebview?.api) {
        throw new Error('Desktop API unavailable: queue control');
      }
      const fn = { pause: 'pause_analysis_queue', resume: 'resume_analysis_queue', cancel: 'cancel_analysis_queue', clear: 'clear_queue_done' }[action];
      if (!fn || typeof window.pywebview.api[fn] !== 'function') {
        throw new Error(`Desktop API missing queue action: ${action}`);
      }
      return window.pywebview.api[fn]();
    }

    async function apiGetQueueStatus() {
      if (!window.pywebview?.api?.get_queue_status) {
        throw new Error('Desktop API unavailable: get_queue_status');
      }
      return window.pywebview.api.get_queue_status();
    }

    async function apiGetRecoveryStatus() {
      if (!window.pywebview?.api?.get_recovery_status) {
        throw new Error('Desktop API unavailable: get_recovery_status');
      }
      return window.pywebview.api.get_recovery_status();
    }

    async function apiRestoreRecoveryQueue() {
      if (!window.pywebview?.api?.restore_analysis_queue) {
        throw new Error('Desktop API unavailable: restore_analysis_queue');
      }
      return window.pywebview.api.restore_analysis_queue();
    }

    async function apiClearRecoveryState(clearQueueState = true) {
      if (!window.pywebview?.api?.clear_recovery_state) {
        throw new Error('Desktop API unavailable: clear_recovery_state');
      }
      return window.pywebview.api.clear_recovery_state(!!clearQueueState);
    }

    async function apiSendRecoveryCrashReport() {
      if (!window.pywebview?.api?.send_recovery_crash_report) {
        throw new Error('Desktop API unavailable: send_recovery_crash_report');
      }
      return window.pywebview.api.send_recovery_crash_report();
    }

    let _startupRecoveryHandled = false;

    async function maybeHandleStartupRecovery() {
      if (_startupRecoveryHandled) return;
      _startupRecoveryHandled = true;

      if (!hasPywebviewApi) { const ready = await waitForPywebview(); if (!ready) return; }

      let recovery = null;
      try {
        recovery = await apiGetRecoveryStatus();
      } catch (_) {
        return;
      }
      if (!recovery || recovery.success === false) return;

      const restorePaths = Array.isArray(recovery.queue_recovery?.restore_paths)
        ? recovery.queue_recovery.restore_paths
        : [];
      const hasQueueRecovery = restorePaths.length > 0;
      const exitReason = String(recovery.exit_reason || '').toLowerCase();
      // 'os_shutdown' (PC reboot/logoff) and 'clean' never warrant a dialog.
      // 'crash' is a real unhandled exception. 'unknown' is ambiguous
      // (SIGKILL, power loss, or pre-upgrade install) and gets a soft prompt.
      const hadUncleanShutdown = !!recovery.unclean_shutdown
        && (exitReason === 'crash' || exitReason === 'unknown' || exitReason === '');
      if (!hasQueueRecovery && !hadUncleanShutdown) return;

      let queueDismissed = false;
      if (hasQueueRecovery) {
        const doRestore = confirm(
          `Kestrel detected an interrupted analysis queue with ${restorePaths.length} folder${restorePaths.length === 1 ? '' : 's'}.\n\nRestore it now?`
        );
        if (doRestore) {
          try {
            const restoreResult = await apiRestoreRecoveryQueue();
            if (restoreResult && restoreResult.success !== false) {
              startPollingQueue();
              const status = await apiGetQueueStatus();
              renderQueuePanel(status);
              setStatus(`Recovered previous analysis queue (${restoreResult.restored || restorePaths.length} folder(s)).`);
            } else {
              const msg = restoreResult?.error || 'Unknown error';
              alert('Could not restore the previous queue:\n\n' + msg);
              queueDismissed = true;
            }
          } catch (e) {
            alert('Could not restore the previous queue:\n\n' + (e.message || e));
            queueDismissed = true;
          }
        } else {
          queueDismissed = true;
        }
      }

      if (hadUncleanShutdown) {
        const promptText = exitReason === 'crash'
          ? 'Kestrel detected that the previous session crashed.\n\nSend a crash report now?'
          : 'Kestrel did not exit cleanly. This is sometimes caused by a system shutdown or power loss — would you still like to send a report?';
        const sendReport = confirm(promptText);
        if (sendReport) {
          try {
            const reportResult = await apiSendRecoveryCrashReport();
            if (reportResult && reportResult.success === false) {
              alert('Could not send crash report:\n\n' + (reportResult.error || 'Unknown error'));
            } else {
              showToast('Crash report sent. Thank you.', 3500);
            }
          } catch (e) {
            alert('Could not send crash report:\n\n' + (e.message || e));
          }
        }
      }

      try {
        if (queueDismissed) {
          await apiClearRecoveryState(true);
        } else if (hadUncleanShutdown) {
          await apiClearRecoveryState(false);
        }
      } catch (_) { }
    }

    /** Format a duration in seconds to a readable string like "2m 30s". */
    function formatDuration(secs) {
      if (!isFinite(secs) || secs < 0) return '–';
      secs = Math.round(secs);
      if (secs < 60) return secs + 's';
      const m = Math.floor(secs / 60), s = secs % 60;
      if (m < 60) return m + 'm ' + (s > 0 ? s + 's' : '');
      const h = Math.floor(m / 60), rm = m % 60;
      return h + 'h ' + (rm > 0 ? rm + 'm' : '');
    }

    /** Render the queue panel from a status object. */
    function renderQueuePanel(status) {
      window._lastQueueStatus = status; // store for analyze dialog queue preview
      const panel = document.getElementById('queuePanel');
      const badge = document.getElementById('queuePanelBadge');
      const body = document.getElementById('queuePanelBody');
      const controls = document.getElementById('queuePanelControls');
      const pauseBtn = document.getElementById('queuePauseBtn');
      const overallEtaEl = document.getElementById('queueOverallEta');
      if (!panel || !badge || !body) return;

      const items = status.items || [];
      const running = !!status.running;
      const paused = !!status.paused;
      const hasItems = items.length > 0;

      // While the queue is running, keep a short-lived poll to update folder rows
      if (running) startQueueCountsPoll(); else stopQueueCountsPoll();

      if (!hasItems && !running) {
        panel.classList.add('hidden');
        if (overallEtaEl) overallEtaEl.classList.add('hidden');
        stopPollingQueue();
        return;
      }

      panel.classList.remove('hidden');

      // Badge
      const runningItems = items.filter(i => i.status === 'running');
      const pendingItems = items.filter(i => i.status === 'pending');
      const doneItems = items.filter(i => i.status === 'done');
      if (paused) {
        badge.textContent = 'Paused'; badge.className = 'queue-panel-badge paused';
      } else if (running) {
        const cur = runningItems[0];
        if (cur && cur.total > 0) {
          badge.textContent = `${cur.processed} / ${cur.total}`; badge.className = 'queue-panel-badge';
        } else {
          badge.textContent = `${pendingItems.length + runningItems.length} pending`; badge.className = 'queue-panel-badge';
        }
      } else if (doneItems.length === items.length && items.length > 0) {
        badge.textContent = 'Done'; badge.className = 'queue-panel-badge done';
      } else {
        badge.textContent = `${pendingItems.length} pending`; badge.className = 'queue-panel-badge';
      }

      // Pause/resume button label
      if (pauseBtn) pauseBtn.textContent = paused ? '▶ Resume' : '⏸ Pause';

      if (!_queuePanelExpanded) { body.classList.add('hidden'); if (controls) controls.classList.add('hidden'); return; }
      body.classList.remove('hidden'); if (controls) controls.classList.remove('hidden');

      // ETA computation: secs/image from running item using TRUE baseline from folder inspection
      const cur = runningItems[0];
      let secsPerImage = null;
      // inspectionReady: inspection data exists for the running folder.
      // Until it arrives, suppress ETA entirely (show "Calculating ETA…") so the early
      // incorrect progress_cb(alreadyDone, total) call cannot produce a near-zero ETA.
      const normCurPath = normalizePath(cur?.path);
      const inspectionReady = cur && _queueSessionStartState.has(normCurPath);
      if (cur && inspectionReady && cur.elapsed_seconds > 0) {
        const sess = _queueSessionStartState.get(normCurPath);
        const initialProcessed = sess.initialProcessed || 0;
        const processedThisSession = Math.max(0, (cur.processed || 0) - initialProcessed);
        if (processedThisSession > 0) {
          const rawSecsPerImage = cur.elapsed_seconds / processedThisSession;
          // Reset EMA if we moved to a different folder (compare normalized paths)
          if (_etaLastPath !== normCurPath) { _etaSmoothed = null; _etaLastPath = normCurPath; }
          // Exponential moving average (α=0.15) — smooths per-image jitter without
          // lagging too far behind the true rate
          const alpha = 0.15;
          _etaSmoothed = _etaSmoothed === null ? rawSecsPerImage : alpha * rawSecsPerImage + (1 - alpha) * _etaSmoothed;
          secsPerImage = _etaSmoothed;
        }
      }

      // Show loading message if models are being loaded (early in run)
      if (running && cur) {
        const overallEl = overallEtaEl;
        const loadingMsg = (cur.current_status_msg || '').toLowerCase().includes('load');
        if (loadingMsg || (cur.processed === 0 && cur.current_status_msg)) {
          if (overallEl) { overallEl.textContent = `⏳ ${cur.current_status_msg || 'Loading analyzer... please wait'}`; overallEl.classList.remove('hidden'); }
          try { showLoadingAnalyzer(); } catch (e) { }
        } else {
          try { hideLoadingAnalyzer(); } catch (e) { }
        }
      }

      // Overall ETA: aggregate remaining images across queue using inspection data for accuracy
      if (overallEtaEl && running && cur) {
        if (!inspectionReady) {
          // Inspection data still in flight — show placeholder so user isn't misled
          overallEtaEl.textContent = '⏳ Calculating ETA…';
          overallEtaEl.classList.remove('hidden');
        } else if (secsPerImage !== null) {
          let totalRemaining = 0;
          for (const item of items) {
            const sess = _queueSessionStartState.get(item.path);
            if (item.status === 'running' && item.total > 0) {
              const remaining = Math.max(0, (item.total || 0) - (item.processed || 0));
              totalRemaining += secsPerImage * remaining;
            } else if (item.status === 'pending') {
              const toAnalyze = sess && typeof sess.toAnalyze === 'number' ? sess.toAnalyze : 200;
              totalRemaining += secsPerImage * toAnalyze;
            }
          }
          if (totalRemaining > 5) {
            overallEtaEl.textContent = `⏱ Overall est. remaining: ${formatDuration(totalRemaining)}`;
            overallEtaEl.classList.remove('hidden');
          } else {
            overallEtaEl.classList.add('hidden');
          }
        } else {
          overallEtaEl.classList.add('hidden');
        }
      } else if (overallEtaEl) {
        overallEtaEl.classList.add('hidden');
      }

      // Queue items
      const frag = document.createDocumentFragment();
      for (const item of items) {
        const div = document.createElement('div');
        const isDone = item.status === 'done';
        const isAlreadyAnalyzed = isDone &&
          (item.current_status_msg || '').toLowerCase().includes('no new files');
        div.className = 'queue-item' + (isDone || item.status === 'cancelled' ? ' done-item' : '');

        // Header row: name + status badge
        const hdr = document.createElement('div');
        hdr.className = 'queue-item-header';
        const nameEl = document.createElement('span');
        nameEl.className = 'queue-item-name';
        nameEl.textContent = item.name;
        nameEl.title = item.path;
        const statusEl = document.createElement('span');
        statusEl.className = `queue-item-status ${item.status}`;
        const labels = {
          pending: '⏳ In Queue',
          running: '⚙ Analyzing',
          done: isAlreadyAnalyzed ? '✓ Already analyzed' : '✓ Done',
          error: '✗ Error',
          cancelled: '— Cancelled',
        };
        statusEl.textContent = labels[item.status] || item.status;
        if (item.status === 'error' && item.error) statusEl.title = item.error;
        hdr.appendChild(nameEl); hdr.appendChild(statusEl);
        div.appendChild(hdr);

        // Progress bar
        if (item.status === 'running' && item.total > 0) {
          const prog = document.createElement('div'); prog.className = 'queue-item-progress';
          const fill = document.createElement('div'); fill.className = 'queue-item-progress-fill';
          fill.style.width = Math.round((item.processed / item.total) * 100) + '%';
          prog.appendChild(fill); div.appendChild(prog);

          // ETA / paused row
          {
            const etaEl = document.createElement('div');
            etaEl.className = 'queue-item-eta';
            if (item.is_paused) {
              etaEl.textContent = `${item.processed} / ${item.total} — ⏸ Paused`;
            } else if (!inspectionReady) {
              etaEl.textContent = `${item.processed} / ${item.total} — ⏳ Calculating ETA…`;
            } else if (secsPerImage !== null && item.total > item.processed) {
              const remaining = secsPerImage * (item.total - item.processed);
              etaEl.textContent = `${item.processed} / ${item.total} — est. ${formatDuration(remaining)} left`;
            } else {
              etaEl.textContent = `${item.processed} / ${item.total}`;
            }
            div.appendChild(etaEl);
          }

          // Current filename
          if (item.current_filename) {
            const fileEl = document.createElement('div');
            fileEl.className = 'queue-item-file';
            fileEl.textContent = item.current_filename;
            div.appendChild(fileEl);
          }

          // Live preview thumbnail (async load deferred after DOM insert)
          if (item.current_export_path && hasPywebviewApi) {
            const preview = document.createElement('div');
            preview.className = 'queue-live-preview';
            const thumb = document.createElement('img');
            thumb.className = 'queue-live-thumb';
            thumb.alt = '';
            // Store paths as data attributes; loaded after DOM insert
            thumb.dataset.thumbRel = item.current_export_path;
            thumb.dataset.thumbRoot = item.path;
            preview.appendChild(thumb);
            div.appendChild(preview);
          }
        } else if (item.status === 'done' && item.total > 0) {
          const prog = document.createElement('div'); prog.className = 'queue-item-progress';
          const fill = document.createElement('div'); fill.className = 'queue-item-progress-fill';
          fill.style.width = '100%'; fill.style.background = '#2ecc71';
          prog.appendChild(fill); div.appendChild(prog);
        }

        frag.appendChild(div);
      }
      body.innerHTML = '';
      body.appendChild(frag);

      // Async: load thumbnails for any img[data-thumb-rel] elements (cache avoids reload flash)
      body.querySelectorAll('img[data-thumb-rel]').forEach(async img => {
        try {
          const rel = img.dataset.thumbRel || '';
          const root = img.dataset.thumbRoot || '';
          const key = rel + '|' + root;
          const isLive = rel.indexOf('__live_') >= 0;
          if (!isLive && _thumbCache.has(key)) { img.src = _thumbCache.get(key); return; }
          const result = await window.pywebview.api.read_image_file(rel, root);
          if (result && result.success && result.data) {
            const url = _base64ToBlobUrl(result.data, result.mime);
            if (!isLive) _thumbCache.set(key, url);
            img.src = url;
          }
        } catch (_) { }
      });

      // Check if any folder newly finished — refresh tree + auto-reload CSV data
      const nowDone = new Set(items.filter(i => i.status === 'done').map(i => i.path));
      let treeRescanNeeded = false;
      for (const p of nowDone) {
        if (!_queueLastDoneSet.has(p)) {
          treeRescanNeeded = true;
          scheduleAutoRefresh(p);
          // First-time folder completion → offer analytics consent if not yet asked
          if (!getSetting('analytics_consent_shown', false)) showAnalyticsConsentDialog();
        }
      }
      if (treeRescanNeeded) {
        setTimeout(() => { if (folderTreeRootNode) rescanFolderTree(folderTreeRootNode.path); }, 1200);
      }
      _queueLastDoneSet = nowDone;

      // Update in-progress folder tracking and UI (pending + running folders)
      try {
        const norm = p => (p || '').replace(/\\/g, '/');
        const inProgressNow = new Set();
        for (const item of items) {
          if (item.status === 'pending' || item.status === 'running') {
            inProgressNow.add(norm(item.path));
          }
        }
        
        // Detect newly-running items (first time moving from pending to running)
        const runningNow = new Set(items.filter(i => i.status === 'running').map(i => norm(i.path)));
        const runningRawPaths = {};
        items.filter(i => i.status === 'running').forEach(i => { runningRawPaths[norm(i.path)] = i.path; });
        for (const p of runningNow) {
          if (!_queueLastRunningSet.has(p)) {
            _handleFirstFolderAnalysisStart(runningRawPaths[p] || p);
          }
        }
        const prevRunningSet = _queueLastRunningSet;
        _queueLastRunningSet = runningNow;
        
        // Update in-progress set and refresh tree styling
        _inProgressFolderPaths = inProgressNow;
        updateInProgressFoldersInTree();
        _updateAutoRefreshTimers();
        
        // Newly-starting items: update the main folder tree after 500ms delay
        for (const p of inProgressNow) {
          if (!prevRunningSet.has(p)) {
            setTimeout(() => {
              try {
                if (!folderTreeRootNode) return;
                updateFolderTreeNode(p);
              } catch (e) { /* ignore */ }
            }, 500);
          }
        }
      } catch (e) { console.warn('[queue] in-progress tracking error:', e); }

      // Remove auto-refresh timers for finished folders
      try {
        const norm = p => (p || '').replace(/\\/g, '/');
        const nowDone = new Set(items.filter(i => i.status === 'done').map(i => norm(i.path)));
        for (const p of nowDone) {
          if (_autoRefreshTimers.has(p)) {
            clearInterval(_autoRefreshTimers.get(p));
            _autoRefreshTimers.delete(p);
          }
        }
      } catch (e) { console.warn('[timer] cleanup error:', e); }

      // Update live dialog if open
      if (_liveAnalysisDlgOpen) {
        const runningItem = items.find(i => i.status === 'running') || null;
        updateLiveAnalysisDlg(runningItem || items[items.length - 1] || null);
      }
    }

    // Normalize paths consistently: strip trailing slashes
    function normalizePath(p) {
      if (!p) return '';
      let pp = String(p).trim();
      while (pp && pp[pp.length - 1] in {'\\': 1, '/': 1}) pp = pp.slice(0, -1);
      return pp;
    }

    function startPollingQueue() {
      if (_queuePollingTimer) return;
      startAutoRefresh();

      // Initialize session state by inspecting all folders in the queue
      // This gives us TRUE baselines for accurate ETA calculations
      (async () => {
        try {
          const status = await apiGetQueueStatus();
          if (status && status.items && status.items.length > 0) {
            // Batch-inspect all folders to get true processed/total counts
            const paths = status.items.map(item => item.path);
            if (hasPywebviewApi && window.pywebview?.api?.inspect_folders) {
              try {
                const inspectRes = await window.pywebview.api.inspect_folders(paths);
                if (inspectRes && inspectRes.success && inspectRes.results) {
                  for (const [path, info] of Object.entries(inspectRes.results)) {
                    if (info) {
                      const normPath = normalizePath(path);
                      _queueFolderInspections.set(normPath, info);
                      const initialProcessed = info.processed || 0;
                      const totalImages = info.total || 0;
                      const toAnalyze = Math.max(0, totalImages - initialProcessed);
                      _queueSessionStartState.set(normPath, {
                        initialProcessed,
                        totalImages,
                        toAnalyze
                      });
                    }
                  }
                }
              } catch (e) { /* ignore */ }
            }
          }
        } catch (e) { /* ignore */ }
      })();

      // Poll more frequently to reflect per-image progress (500ms)
      _queuePollingTimer = setInterval(async () => {
        try {
          const status = await apiGetQueueStatus();
          renderQueuePanel(status);
          // Update auto-refresh timers based on pause state
          _updateAutoRefreshTimers();

          // When new items appear, inspect and capture their baseline state
          if (status && status.items) {
            const newPaths = [];
            for (const item of status.items) {
              const normPath = normalizePath(item.path);
              if (!_queueSessionStartState.has(normPath)) {
                newPaths.push(item.path);
              }
            }
            if (newPaths.length > 0 && hasPywebviewApi && window.pywebview?.api?.inspect_folders) {
              try {
                const inspectRes = await window.pywebview.api.inspect_folders(newPaths);
                if (inspectRes && inspectRes.success && inspectRes.results) {
                  for (const [path, info] of Object.entries(inspectRes.results)) {
                    if (info) {
                      const normPath = normalizePath(path);
                      _queueFolderInspections.set(normPath, info);
                      const initialProcessed = info.processed || 0;
                      const totalImages = info.total || 0;
                      const toAnalyze = Math.max(0, totalImages - initialProcessed);
                      _queueSessionStartState.set(normPath, {
                        initialProcessed,
                        totalImages,
                        toAnalyze
                      });
                    }
                  }
                }
              } catch (e) { /* ignore */ }
            }
          }

          if (!status.running && (status.items || []).every(i => i.status !== 'pending' && i.status !== 'running')) {
            stopPollingQueue();
          }
        } catch (_) { }
      }, 500);
    }

    function stopPollingQueue() {
      if (_queuePollingTimer) { clearInterval(_queuePollingTimer); _queuePollingTimer = null; }
      stopAutoRefresh();
      // Cleanup auto-refresh timers for in-progress folders
      for (const timerId of _autoRefreshTimers.values()) {
        clearInterval(timerId);
      }
      _autoRefreshTimers.clear();
      _inProgressFolderPaths.clear();
      // Cleanup session state
      _queueSessionStartState.clear();
      _queueFolderInspections.clear();
      _etaSmoothed = null;
      _etaLastPath = null;
    }

    // Poll the queue status frequently and update folder rows in the ANALYZE DIALOG ONLY
    // with the running item's processed/total. This keeps per-folder counts live
    // while analysis is in progress.
    function startQueueCountsPoll() {
      if (_queueCountsTimer) return;
      _queueCountsTimer = setInterval(async () => {
        try {
          const status = await apiGetQueueStatus();
          if (!status || !status.items) return;
          const items = status.items;
          // Normalize helper
          const norm = p => (p || '').replace(/\\/g, '/');
          // Update ONLY the Analyze dialog tree rows (not the main folder tree)
          const rows = Array.from(document.querySelectorAll('#analyzeDlgTree .adlg-node-row'));
          for (const it of items) {
            const ip = norm(it.path);
            const related = rows.filter(r => norm(r.dataset.path) === ip);
            for (const row of related) {
              const span = row.querySelector('.tree-count');
              if (span) {
                if (it.total && it.total > 0) span.textContent = ` ${it.processed}/${it.total}`;
                else span.textContent = '';
              }
              // Update analysis classes (partial/full/none) - only for analyze dialog
              row.classList.remove('analyzed-full', 'analyzed-partial', 'analyzed-none');
              if (it.total && it.total > 0) {
                if ((it.processed || 0) === 0) row.classList.add('analyzed-none');
                else if ((it.processed || 0) >= it.total) row.classList.add('analyzed-full');
                else row.classList.add('analyzed-partial');
              }
            }
          }
          // Stop polling if queue no longer running
          if (!status.running) stopQueueCountsPoll();
        } catch (_) { }
      }, 500);
    }

    function stopQueueCountsPoll() {
      if (_queueCountsTimer) { clearInterval(_queueCountsTimer); _queueCountsTimer = null; }
    }

    // ── Live Analysis Details dialog ──────────────────────────────────────────

    function openLiveAnalysisDlg() {
      _liveAnalysisDlgOpen = true;
      document.getElementById('liveAnalysisDlg').showModal();
    }

    /**
     * Load an image by relative path + root into an <img> element, using _thumbCache.
     * Only issues a network/IPC call on cache miss.
     */
    async function _loadImg(imgEl, relPath, rootPath) {
      if (!relPath || !rootPath || !hasPywebviewApi) return;
      const key = relPath + '|' + rootPath;
      const isLive = String(relPath).indexOf('__live_') >= 0;
      if (!isLive && _thumbCache.has(key)) { imgEl.src = _thumbCache.get(key); return; }
      try {
        const r = await window.pywebview.api.read_image_file(relPath, rootPath);
        if (r && r.success && r.data) {
          const url = _base64ToBlobUrl(r.data, r.mime);
          if (!isLive) _thumbCache.set(key, url);
          imgEl.src = url;
        }
      } catch (_) { }
    }

    /** Update the live dialog with data from a running (or recently-finished) queue item. */
    function updateLiveAnalysisDlg(item) {
      const dlg = document.getElementById('liveAnalysisDlg');
      if (!dlg || !dlg.open) { _liveAnalysisDlgOpen = false; return; }

      // Header
      const folderEl = document.getElementById('liveDlgFolderName');
      const fnameEl = document.getElementById('liveDlgFilename');
      const statusEl = document.getElementById('liveDlgStatus');
      if (folderEl) folderEl.textContent = item ? item.name : '–';
      if (fnameEl) fnameEl.textContent = item ? (item.current_filename || '') : '';
      if (statusEl) {
        const msg = item ? (item.current_status_msg || '') : '';
        const paused = item && item.is_paused;
        statusEl.textContent = paused ? '⏸ Paused — ' + msg : msg;
      }

      if (!item) return;

      // Thumbnail
      const thumbEl = document.getElementById('liveDlgThumb');
      if (thumbEl && item.current_export_path) {
        const k = item.current_export_path + '|' + item.path;
        if (_liveLastThumbKey !== k) { _liveLastThumbKey = k; _loadImg(thumbEl, item.current_export_path, item.path); }
      }

      // Detection overlay
      const overlayEl = document.getElementById('liveDlgOverlay');
      if (overlayEl) {
        if (item.current_overlay_rel) {
          const k = item.current_overlay_rel + '|' + item.path;
          // Always reload live overlay images (they are overwritten in place).
          const isLiveOverlay = String(item.current_overlay_rel).indexOf('__live_') >= 0;
          if (isLiveOverlay) {
            _liveLastOverlayKey = k + '|' + Date.now();
            _loadImg(overlayEl, item.current_overlay_rel, item.path);
          } else if (_liveLastOverlayKey !== k) {
            _liveLastOverlayKey = k; _loadImg(overlayEl, item.current_overlay_rel, item.path);
          }
          overlayEl.style.visibility = '';
        } else {
          overlayEl.style.visibility = 'hidden';
        }
      }

      // Crop cards
      _updateLiveCropCards(item);
    }

    function _formatStars(rating) {
      const r = Math.max(0, Math.min(5, Math.round(rating || 0)));
      return '★'.repeat(r) + '☆'.repeat(5 - r);
    }

    function _rawQualityToRating(quality) {
      const q = Number(quality);
      if (!Number.isFinite(q) || q < 0) return 0;
      if (q < 0.15) return 1;
      if (q < 0.3) return 2;
      if (q < 0.6) return 3;
      if (q < 0.9) return 4;
      return 5;
    }

    function _updateLiveCropCards(item) {
      const row = document.getElementById('liveDlgCrops');
      if (!row) return;
      const crops = item.current_crops_rel || [];
      const dets = item.current_detections || [];
      const quality = item.current_quality_results || [];
      const species = item.current_species_results || [];
      const cardCount = Math.max(crops.length, dets.length, quality.length, species.length);

      // Keep card count in sync with current live detections.
      while (row.children.length < cardCount) {
        const card = document.createElement('div');
        card.className = 'live-dlg-crop-card';
        card.innerHTML = `
        <img class="live-dlg-crop-img" alt="" />
        <div class="ldc-conf">–</div>
        <div class="ldc-quality">Quality: —</div>
        <div class="ldc-stars">☆☆☆☆☆</div>
        <div class="ldc-species">–</div>
        <div class="ldc-family">–</div>`;
        row.appendChild(card);
      }
      while (row.children.length > cardCount) {
        row.removeChild(row.lastChild);
      }

      for (let i = 0; i < cardCount; i++) {
        const card = row.children[i];
        const imgEl = card.querySelector('.live-dlg-crop-img');
        const confEl = card.querySelector('.ldc-conf');
        const qualityEl = card.querySelector('.ldc-quality');
        const starsEl = card.querySelector('.ldc-stars');
        const spEl = card.querySelector('.ldc-species');
        const fmEl = card.querySelector('.ldc-family');
        const hasCrop = i < crops.length && crops[i];

        card.style.opacity = hasCrop ? '1' : '0.3';

        if (hasCrop) {
          const k = crops[i] + '|' + item.path;
          const prev = _liveLastCropKeys[i] || '';
          const isLiveCrop = String(crops[i]).indexOf('__live_') >= 0;
          if (isLiveCrop) {
            _liveLastCropKeys[i] = k + '|' + Date.now();
            _loadImg(imgEl, crops[i], item.path);
          } else if (prev !== k) { _liveLastCropKeys[i] = k; _loadImg(imgEl, crops[i], item.path); }
        } else {
          if (imgEl.src) imgEl.removeAttribute('src');
          _liveLastCropKeys[i] = '';
          confEl.textContent = '–';
          qualityEl.textContent = 'Quality: —';
          starsEl.textContent = '☆☆☆☆☆';
          spEl.textContent = '–'; spEl.className = 'ldc-species';
          fmEl.textContent = '–'; fmEl.className = 'ldc-family';
          continue;
        }

        // Detection confidence
        confEl.textContent = i < dets.length
          ? `Conf: ${dets[i].confidence.toFixed(2)}`
          : '–';

        const qVal = i < quality.length ? Number(quality[i].quality) : NaN;
        if (Number.isFinite(qVal) && qVal >= 0) {
          qualityEl.textContent = `Quality: ${qVal.toFixed(3)}`;
        } else {
          qualityEl.textContent = i < crops.length ? 'Quality: …' : 'Quality: —';
        }

        // Live dialog intentionally uses raw quality thresholds (not normalized ratings).
        const rawRating = Number.isFinite(qVal) ? _rawQualityToRating(qVal) : 0;
        starsEl.textContent = i < quality.length
          ? _formatStars(rawRating)
          : (i < crops.length ? '…' : '☆☆☆☆☆');

        // Species
        if (i < species.length) {
          const sp = species[i];
          const spConf = sp.species_confidence ?? 0;
          const fmConf = sp.family_confidence ?? 0;
          spEl.textContent = `${sp.species || '–'} (${spConf.toFixed(2)})`;
          spEl.className = 'ldc-species ' + (spConf >= CONF_HIGH ? 'high-conf' : spConf < CONF_LOW ? 'low-conf' : '');
          fmEl.textContent = sp.family ? `${sp.family} (${fmConf.toFixed(2)})` : '–';
          fmEl.className = 'ldc-family ' + (fmConf >= CONF_HIGH ? 'high-conf' : fmConf < CONF_LOW ? 'low-conf' : '');
        } else {
          spEl.textContent = i < crops.length ? 'Classifying…' : '–';
          spEl.className = i < crops.length ? 'ldc-species low-conf' : 'ldc-species';
          fmEl.textContent = '–'; fmEl.className = 'ldc-family';
        }
      }

      _liveLastCropKeys.length = cardCount;
    }

    // ── End Live Analysis Dialog ─────────────────────────────────────────────────

    // ── Auto-refresh: silently reload CSV data for newly-analyzed folders ─────────

    let _autoRefreshTimer = null;
    let _autoRefreshPendingPaths = new Set(); // paths that need a quiet reload
    let _silentRefreshRunning = false;        // guard against concurrent silentRefreshPending

    /** Queue a silent reload for `path` (called when a queue item becomes done). */
    function scheduleAutoRefresh(path) {
      _autoRefreshPendingPaths.add(path);
    }

    function startAutoRefresh() {
      if (_autoRefreshTimer) return;
      _autoRefreshTimer = setInterval(silentRefreshPending, 7000);
    }

    function stopAutoRefresh() {
      if (_autoRefreshTimer) { clearInterval(_autoRefreshTimer); _autoRefreshTimer = null; }
    }

    // User-editable row fields that must survive auto-refresh merges.
    // Pipeline-written columns are updated from disk; these are kept from memory.
    const _USER_EDITABLE_FIELDS = [
      'rating', 'rating_origin', 'normalized_rating',
      'culled', 'culled_origin',
      'scene_name',
    ];

    /** Silently reload CSV data for any paths in _autoRefreshPendingPaths that are checked.
     *
     *  Merges new pipeline data into existing in-memory rows instead of replacing
     *  them wholesale, so unsaved user edits (ratings, culling, scene names) are
     *  preserved.  Only genuinely new images (not yet in memory) are appended.
     *  Scenedata is merged additively: new scenes are added but user-edited
     *  scenes are never overwritten.
     */
    async function silentRefreshPending() {
      if (_autoRefreshPendingPaths.size === 0) return;
      if (_silentRefreshRunning) return;
      _silentRefreshRunning = true;
      try {
        const toRefresh = Array.from(_autoRefreshPendingPaths).filter(p => _isPathChecked(p));
        _autoRefreshPendingPaths.clear();
        if (toRefresh.length === 0) return;

        const normPath = p => (p || '').replace(/\\/g, '/');
        let hasNewRows = false;
        let hasUpdates = false;
        for (const p of toRefresh) {
          try {
            if (!hasPywebviewApi || !window.pywebview?.api?.read_kestrel_csv) continue;
            const result = await window.pywebview.api.read_kestrel_csv(p);
            if (!result.success) continue;
            const parsed = parseCsvText(result.data);
            const diskRows = parsed.data || [];
            const newFields = parsed.meta.fields || [];
            const root = result.root || p;
            const rootN = normPath(root);
            for (const f of newFields) if (!header.includes(f)) header.push(f);

            // Build a lookup of existing in-memory rows for this folder by filename
            const existingByName = new Map();
            for (const r of rows) {
              if (normPath(r.__rootPath) === rootN && r.filename) {
                existingByName.set(r.filename, r);
              }
            }

            const sample = rows.find(r => normPath(r.__rootPath) === rootN);
            const slot = sample ? sample.__folderSlot : rows.length;
            const addedRows = [];

            for (const diskRow of diskRows) {
              const fname = diskRow.filename;
              if (!fname) continue;
              const existing = existingByName.get(fname);
              if (existing) {
                // Merge: update pipeline-written fields from disk, keep user edits
                const savedEdits = {};
                for (const field of _USER_EDITABLE_FIELDS) {
                  if (field in existing) savedEdits[field] = existing[field];
                }
                // Also preserve internal UI fields
                const savedInternal = {
                  __rootPath: existing.__rootPath,
                  __folderSlot: existing.__folderSlot,
                  __normalized_rating: existing.__normalized_rating,
                };

                // Update pipeline fields from disk
                for (const key of Object.keys(diskRow)) {
                  if (key.startsWith('__')) continue;
                  if (!_USER_EDITABLE_FIELDS.includes(key)) {
                    existing[key] = diskRow[key];
                  }
                }

                // Restore user edits
                for (const [field, val] of Object.entries(savedEdits)) {
                  // Only restore if the user actually set something
                  if (val !== undefined && val !== '') existing[field] = val;
                }
                // Restore internal fields
                Object.assign(existing, savedInternal);
                hasUpdates = true;
              } else {
                // Genuinely new row from pipeline — append it
                diskRow.__rootPath = root;
                diskRow.__folderSlot = slot;
                addedRows.push(diskRow);
              }
            }

            if (addedRows.length > 0) {
              rows = rows.concat(addedRows);
              hasNewRows = true;
            }

            // Merge scenedata: add new scenes from disk without overwriting user-edited ones
            if (hasPywebviewApi && window.pywebview?.api?.read_kestrel_scenedata) {
              try {
                const sdRes = await window.pywebview.api.read_kestrel_scenedata(root);
                if (sdRes?.success && sdRes.data) {
                  const diskSd = sdRes.data;
                  const memSd = _scenedata[root] || { version: '2.0', image_ratings: {}, scenes: {} };

                  // Merge image_ratings: disk values only for images without user edits
                  if (diskSd.image_ratings) {
                    if (!memSd.image_ratings) memSd.image_ratings = {};
                    for (const [fname, rating] of Object.entries(diskSd.image_ratings)) {
                      if (!(fname in memSd.image_ratings)) {
                        memSd.image_ratings[fname] = rating;
                      }
                    }
                  }

                  // Merge scenes: add new scenes, update non-user-edited scenes
                  if (diskSd.scenes) {
                    if (!memSd.scenes) memSd.scenes = {};
                    for (const [sceneId, diskScene] of Object.entries(diskSd.scenes)) {
                      const memScene = memSd.scenes[sceneId];
                      if (!memScene) {
                        // New scene from pipeline — add it
                        memSd.scenes[sceneId] = diskScene;
                      } else {
                        // Existing scene — only update image list, keep user edits
                        const userFinalized = memScene.user_tags && memScene.user_tags.finalized;
                        const userRenamed = memScene.name && memScene.name.trim() !== '';
                        const userAccepted = memScene.status === 'accepted';
                        if (!userFinalized && !userRenamed && !userAccepted) {
                          // No user edits — safe to update from disk
                          // But still merge image_filenames additively
                          const existingFiles = new Set(memScene.image_filenames || []);
                          for (const f of (diskScene.image_filenames || [])) {
                            if (!existingFiles.has(f)) {
                              memScene.image_filenames.push(f);
                            }
                          }
                        } else {
                          // User has edited this scene — only add new filenames
                          const existingFiles = new Set(memScene.image_filenames || []);
                          for (const f of (diskScene.image_filenames || [])) {
                            if (!existingFiles.has(f)) {
                              memScene.image_filenames.push(f);
                            }
                          }
                        }
                      }
                    }
                  }

                  _scenedata[root] = memSd;
                }
              } catch (_) {}
            }

            // Apply normalization to new rows only
            if (addedRows.length > 0 && hasPywebviewApi && window.pywebview?.api?.apply_normalization) {
              try {
                const normRes = await window.pywebview.api.apply_normalization(root);
                if (normRes?.success && normRes?.normalized_ratings) {
                  const mapping = normRes.normalized_ratings;
                  for (const r of addedRows) {
                    if (r.filename in mapping) r.__normalized_rating = mapping[r.filename];
                  }
                }
              } catch (_) {}
            }
          } catch (e) {
            console.warn('[autorefresh]', p, e);
          }
        }

        if (hasNewRows || hasUpdates) {
          ensureSceneNameColumn();
          ensureRatingColumns();
          await renderScenes();
          // If a scene dialog is open, its filmstrip is stale after renderScenes
          // rebuilt the scenes array with fresh row objects — re-render it now.
          if (_currentScene) {
            renderFilmstrip(_currentScene);
            const safeIdx = Math.max(0, Math.min(currentImageIndex, _currentScene.images.length - 1));
            await selectFilmstripImage(safeIdx, _currentScene);
          }
          if (hasNewRows) {
            setStatus(`Auto-refreshed: new images added from analysis`);
          }
        }
      } finally {
        _silentRefreshRunning = false;
      }
    }

    /** Re-scan the folder tree root without resetting the expanded/checked state. */
    async function rescanFolderTree(rootPath) {
      if (!hasPywebviewApi || !window.pywebview?.api?.list_subfolders) return;
      try {
        const depth = getSetting('treeScanDepth', 3);
        const result = await window.pywebview.api.list_subfolders(rootPath, depth);
        if (!result.success) return;
        folderTreeData = result.tree;
        folderTreeRootHasKestrel = !!result.root_has_kestrel;
        const rootName = rootPath.replace(/\\/g, '/').split('/').filter(Boolean).pop() || rootPath;
        folderTreeRootNode = { name: rootName, path: rootPath, has_kestrel: folderTreeRootHasKestrel, children: folderTreeData };
        // Apply any transient kestrel markings so nodes recently queued/started
        // are shown as having kestrel until the real scan state differs.
        try {
          const norm = p => (p || '').replace(/\\/g, '/');
          function applyTemp(n) {
            if (!n) return;
            const p = norm(n.path || '');
            if (_tempKestrelPaths.has(p)) n.has_kestrel = true;
            (n.children || []).forEach(c => applyTemp(c));
          }
          applyTemp(folderTreeRootNode);
        } catch (e) { /* ignore */ }
        renderFolderTree();
      } catch (_) { }
    }

    /** Update UI to reflect in-progress folders with special styling and always-present checkboxes. */
    function updateInProgressFoldersInTree() {
      try {
        const norm = p => (p || '').replace(/\\/g, '/');
        for (const inProgPath of _inProgressFolderPaths) {
          const normPath = norm(inProgPath);
          const rows = Array.from(document.querySelectorAll('#folderTree .tree-node-row'));
          for (const row of rows) {
            const rp = norm(row.dataset.path || '');
            if (rp !== normPath) continue;
            
            // Mark as in-progress with purple styling
            row.classList.add('in-progress');
            _tempKestrelPaths.add(normPath); // prevent checkbox removal on next rescan
            
            // Ensure checkbox exists (even if .kestrel doesn't)
            if (!row.querySelector('.tree-cb')) {
              const cb = document.createElement('input');
              cb.type = 'checkbox';
              cb.className = 'tree-cb';
              cb.title = 'Include in multi-folder view (analyzing now)';
              cb.checked = _isPathChecked(row.dataset.path);
              cb.addEventListener('change', (e) => {
                e.stopPropagation();
                if (cb.checked) checkedFolderPaths.add(row.dataset.path);
                else checkedFolderPaths.delete(row.dataset.path);
                _updateAutoRefreshTimers();
                debouncedAutoLoad();
              });
              // Find icon and insert before it
              const icon = row.querySelector('.tree-icon');
              if (icon && icon.parentNode) icon.parentNode.insertBefore(cb, icon);
              else row.insertBefore(cb, row.firstChild);
            }
          }
        }
      } catch (e) { console.warn('[tree] updateInProgressFoldersInTree error:', e); }
    }

    // Path-insensitive check: does checkedFolderPaths contain a path matching p?
    function _isPathChecked(p) {
      const n = (p || '').replace(/\\/g, '/');
      for (const cp of checkedFolderPaths) {
        if (cp.replace(/\\/g, '/') === n) return true;
      }
      return false;
    }

    function _normalizeFolderPathForCache(path) {
      return String(path || '').trim().replace(/\\/g, '/').replace(/\/+$/, '');
    }

    function _snapshotCheckedFolderPathMap() {
      const snapshot = new Map();
      for (const rawPath of checkedFolderPaths) {
        const normalized = _normalizeFolderPathForCache(rawPath);
        if (!normalized || snapshot.has(normalized)) continue;
        snapshot.set(normalized, rawPath);
      }
      return snapshot;
    }

    async function _cleanupCullingCachesForPaths(paths, reason = '') {
      if (!paths || paths.length === 0) return;

      // Also clear in-memory caches so the UI can't show stale previews.
      try { blobUrlCache.clear(); } catch (_) {}
      try { sceneRawCache.clear(); } catch (_) {}
      try { sceneRawLoading.clear(); } catch (_) {}

      if (!hasPywebviewApi || !window.pywebview?.api?.cleanup_culling_cache) return;
      await Promise.all(paths.map(async (rootPath) => {
        try {
          const res = await window.pywebview.api.cleanup_culling_cache(rootPath);
          if (!res?.success) {
            console.warn('[cache] cleanup_culling_cache failed:', rootPath, res?.error || 'Unknown error', reason);
          }
        } catch (e) {
          console.warn('[cache] cleanup_culling_cache error:', rootPath, reason, e);
        }
      }));
    }

    async function _cleanupUncheckedFolderCaches() {
      const current = _snapshotCheckedFolderPathMap();
      const removed = [];
      for (const [normalized, rawPath] of _checkedFolderPathSnapshot.entries()) {
        if (!current.has(normalized)) removed.push(rawPath);
      }
      _checkedFolderPathSnapshot = current;
      if (removed.length > 0) {
        await _cleanupCullingCachesForPaths(removed, 'folder_unchecked');
      }
    }

    function _collectLoadedRootsForCleanup() {
      const roots = new Set();
      try {
        for (const p of checkedFolderPaths) if (p) roots.add(p);
      } catch (_) {}
      try {
        for (const r of rows) {
          if (r && r.__rootPath) roots.add(r.__rootPath);
        }
      } catch (_) {}
      if (rootPath) roots.add(rootPath);
      return Array.from(roots);
    }

    function _cleanupCachesOnAppClose() {
      const roots = _collectLoadedRootsForCleanup();
      if (!roots.length) return;
      _cleanupCullingCachesForPaths(roots, 'app_close').catch(() => {});
    }

    /** Start or stop auto-refresh timers for checked in-progress folders. */
    function _updateAutoRefreshTimers() {
      try {
        const queueStatus = window._lastQueueStatus;
        const isPaused = queueStatus && queueStatus.paused;
        
        if (isPaused) {
          for (const timerId of _autoRefreshTimers.values()) {
            clearInterval(timerId);
          }
          _autoRefreshTimers.clear();
          return;
        }
        
        for (const [path, timerId] of _autoRefreshTimers.entries()) {
          const isStillInProgress = _inProgressFolderPaths.has(path);
          const isStillChecked = _isPathChecked(path);
          if (!isStillInProgress || !isStillChecked) {
            clearInterval(timerId);
            _autoRefreshTimers.delete(path);
          }
        }
        
        for (const inProgPath of _inProgressFolderPaths) {
          if (_isPathChecked(inProgPath) && !_autoRefreshTimers.has(inProgPath)) {
            const capturedPath = inProgPath;
            const timerId = setInterval(async () => {
              try {
                _autoRefreshPendingPaths.add(capturedPath);
                silentRefreshPending();
              } catch (e) { console.warn('[refresh] auto-refresh error:', e); }
            }, 10000);
            _autoRefreshTimers.set(inProgPath, timerId);
          }
        }
      } catch (e) { console.warn('[timer] _updateAutoRefreshTimers error:', e); }
    }

    /** Count how many analyzed (non-in-progress) folders exist in the tree. */
    function countAnalyzedFolders() {
      try {
        let count = 0;
        function traverse(n) {
          if (!n) return;
          const np = (n.path || '').replace(/\\/g, '/');
          if (n.has_kestrel && !_inProgressFolderPaths.has(np)) count++;
          (n.children || []).forEach(c => traverse(c));
        }
        traverse(folderTreeRootNode);
        return count;
      } catch (e) { return 0; }
    }

    /** Implement Case 1 logic: if first folder starts analysis and no other analyzed folders exist, auto-load it. */
    async function _handleFirstFolderAnalysisStart(folderPath) {
      try {
        if (!_isFirstQueueStart) return; // only on first start
        _isFirstQueueStart = false;
        
        const analyzedCount = countAnalyzedFolders();
        if (analyzedCount === 0) {
          // Case 1: Auto-check and auto-load the in-progress folder
          checkedFolderPaths.add(folderPath);
          renderFolderTree();
          await debouncedAutoLoad();
          setStatus('Auto-loaded in-progress folder (Case 1: no other analyzed folders)');
        }
      } catch (e) { console.warn('[case1] error:', e); }
    }

    // ── End Analysis Queue ────────────────────────────────────────────────────────

    // Helper function to load folder using native path (for pywebview API)
    // Loads a single folder. For multi-folder loading, see loadMultipleFolders().

    // Auto-load: fires after a short debounce whenever checkboxes change.
    // If nothing is checked, clears the view gracefully.
    const debouncedAutoLoad = debounce(async () => {
      await _cleanupUncheckedFolderCaches();
      if (checkedFolderPaths.size > 0) {
        await loadMultipleFolders(Array.from(checkedFolderPaths));
      } else {
        ++_loadFoldersVersion; // cancel any in-progress load
        rows = []; header = []; scenes = [];
        sceneGrid.innerHTML = '';
        setStatus('No folders selected — check folders in the tree to load scenes');
      }
    }, 400);

    // Collect all kestrel paths from the tree (recursively) for check-all
    function collectKestrelPaths(node, out = []) {
      if (!node) return out;
      if (node.has_kestrel) out.push(node.path);
      (node.children || []).forEach(c => collectKestrelPaths(c, out));
      return out;
    }

    function checkAllTreeFolders() {
      const all = collectKestrelPaths(folderTreeRootNode);
      all.forEach(p => checkedFolderPaths.add(p));
      renderFolderTree();
      debouncedAutoLoad();
    }

    function checkNoneTreeFolders() {
      checkedFolderPaths.clear();
      renderFolderTree();
      debouncedAutoLoad();
    }

    // Progress bar helpers
    function showProgress(label, pct) {
      const row = document.getElementById('loadProgressRow');
      const lbl = document.getElementById('loadProgressLabel');
      const fill = document.getElementById('loadProgressFill');
      if (row) row.classList.remove('hidden');
      if (lbl) lbl.textContent = label;
      if (fill) fill.style.width = Math.round(Math.max(0, Math.min(100, pct))) + '%';
    }
    function hideProgress() {
      const row = document.getElementById('loadProgressRow');
      if (row) row.classList.add('hidden');
    }

    async function loadMultipleFolders(paths) {
      if (!paths || paths.length === 0) return;
      const myVer = ++_loadFoldersVersion;
      blobUrlCache.clear();
      _sceneActiveCropIndexByImage.clear();
      rows = [];
      header = [];
      let loadedCount = 0;
      let slot = 0;
      const total = paths.length;
      showProgress(`Loading 0 / ${total} folders…`, 0);
      for (let i = 0; i < paths.length; i++) {
        if (myVer !== _loadFoldersVersion) { hideProgress(); return; }
        const p = paths[i];
        const folderName = p.replace(/.*[/\\]/, '');
        showProgress(`Loading ${i + 1} / ${total}: ${folderName}`, (i / total) * 90);
        try {
          const result = await window.pywebview.api.read_kestrel_csv(p);
          if (myVer !== _loadFoldersVersion) { hideProgress(); return; }
          if (!result.success) continue;
          const parsed = parseCsvText(result.data);
          const newRows = parsed.data || [];
          const newFields = parsed.meta.fields || [];
          for (const f of newFields) if (!header.includes(f)) header.push(f);
          const root = result.root || p;
          const currentSlot = slot++;
          for (const r of newRows) { r.__rootPath = root; r.__folderSlot = currentSlot; }
          rows = rows.concat(newRows);
          // Load scenedata for this folder
          if (hasPywebviewApi && window.pywebview?.api?.read_kestrel_scenedata) {
            try {
              const sdRes = await window.pywebview.api.read_kestrel_scenedata(root);
              if (sdRes?.success) _scenedata[root] = sdRes.data;
            } catch (_) {}
          }
          // Apply normalization (in-memory: sets r.__normalized_rating)
          if (hasPywebviewApi && window.pywebview?.api?.apply_normalization) {
            try {
              const normRes = await window.pywebview.api.apply_normalization(root);
              if (normRes?.success && normRes?.normalized_ratings) {
                const mapping = normRes.normalized_ratings;
                for (const r of newRows) {
                  if (r.filename in mapping) r.__normalized_rating = mapping[r.filename];
                }
              }
            } catch (_) {}
          }
          loadedCount++;
        } catch (e) {
          console.warn('[multi] Failed to load', p, e);
        }
      }
      if (myVer !== _loadFoldersVersion) { hideProgress(); return; }
      if (loadedCount === 0) { hideProgress(); setStatus('No folders could be loaded'); return; }
      showProgress(`Building scenes from ${loadedCount} folder${loadedCount === 1 ? '' : 's'}…`, 95);
      // For single-folder image-loading compat: set rootPath to first loaded root.
      // Per-row __rootPath handles multi-folder image loading in getBlobUrlForPath.
      const firstRow = rows.find(r => r.__rootPath);
      if (firstRow) rootPath = firstRow.__rootPath;
      ensureSceneNameColumn();
      ensureRatingColumns();
      _clearDirtyRoots();
      _setDirtyUi(false);
      takeSnapshot();
      const mergeBtn = document.getElementById('openMerge');
      if (mergeBtn) mergeBtn.disabled = true;
      treeActivePath = paths.length === 1 ? paths[0] : null;
      renderFolderTree();
      await renderScenes();
      showProgress('Done', 100);
      await sleep(400);
      hideProgress();
      const label = loadedCount === 1 ? paths[0].replace(/.*[/\\]/, '') : `${loadedCount} folders`;
      setStatus(`Loaded ${label} — ${rows.length} images`);
    }

    async function loadFolderFromPath(folderPath) {
      if (!folderPath) return;

      try {
        // Use pywebview API to read the CSV file
        const result = await window.pywebview.api.read_kestrel_csv(folderPath);

        if (!result.success) {
          throw new Error(result.error || 'Failed to read CSV');
        }

        // Parse the CSV data
        const parsed = parseCsvText(result.data);
        header = parsed.meta.fields || [];
        const loadedRoot = result.root || folderPath;
        rows = (parsed.data || []).map(r => ({ ...r, __rootPath: loadedRoot, __folderSlot: 0 }));
        _sceneActiveCropIndexByImage.clear();
        
        // Load scenedata for this folder
        if (hasPywebviewApi && window.pywebview?.api?.read_kestrel_scenedata) {
          try {
            const sdRes = await window.pywebview.api.read_kestrel_scenedata(loadedRoot);
            if (sdRes?.success) _scenedata[loadedRoot] = sdRes.data;
          } catch (_) {}
        }
        
        // Apply normalization (in-memory: sets r.__normalized_rating)
        if (hasPywebviewApi && window.pywebview?.api?.apply_normalization) {
          try {
            const normRes = await window.pywebview.api.apply_normalization(loadedRoot);
            if (normRes?.success && normRes?.normalized_ratings) {
              const mapping = normRes.normalized_ratings;
              for (const r of rows) {
                if (r.filename in mapping) r.__normalized_rating = mapping[r.filename];
              }
            }
          } catch (_) {}
        }
        
        ensureSceneNameColumn();
        ensureRatingColumns();
        blobUrlCache.clear(); // new folder — clear stale cache entries

        // IMPORTANT: Set rootPath BEFORE renderScenes so image loading works
        rootPath = loadedRoot;
        _clearDirtyRoots();
        _setDirtyUi(false);
        takeSnapshot();

        // Now render with rootPath set
        await renderScenes();

        // Also save in settings for file opening (use rootHint for consistency)
        const settings = loadSettings();
        settings.rootHint = rootPath;
        saveSettings(settings);

        setStatus(`Loaded from: ${result.path}`);
        const mergeBtn = document.getElementById('openMerge');
        if (mergeBtn) mergeBtn.disabled = true; // Can't save in pywebview mode

        // Update active selection in tree if tree is open
        if (folderTreeData) {
          const loadedPath = result.root || folderPath;
          treeActivePath = loadedPath;
          checkedFolderPaths.clear();
          checkedFolderPaths.add(loadedPath);
          _checkedFolderPathSnapshot = _snapshotCheckedFolderPathMap();
          renderFolderTree();
        }
      } catch (e) {
        const errorMsg = (e.message || String(e)).replace(/^Error: /, '');
        // If the folder tree is already visible the user may have clicked a parent folder
        // intentionally (no .kestrel there). Show a soft status message instead of an alert.
        if (folderTreeData) {
          setStatus(`No Kestrel database in this folder — select one that shows 📂 in the tree`);
        } else {
          alert(`Could not load Kestrel database from this folder.\n\nMake sure:\n1. The folder has been analyzed with Kestrel Analyzer\n2. The .kestrel folder exists (it may be hidden on macOS)\n3. You selected the correct folder\n\nTip: On macOS, .kestrel folders are hidden by default. You can:\n• Press Cmd+Shift+. (period) to show hidden files in Finder\n• Or select the parent folder that contains the .kestrel folder\n\nError: ${errorMsg}`);
          setStatus('Failed to load database');
        }
      }
    }

    // Event wiring
    el('#pickFolder').addEventListener('click', async () => {
      kdebug('[pickFolder] clicked');
      kdebug('[pickFolder] hasPywebviewApi:', hasPywebviewApi);
      kdebug('[pickFolder] window.pywebview:', window.pywebview);
      kdebug('[pickFolder] window.pywebview?.api:', window.pywebview?.api);

      // Wait for pywebview API if it's not ready yet
      if (!hasPywebviewApi) {
        kdebug('[pickFolder] Waiting for pywebview API...');
        const ready = await waitForPywebview();
        kdebug('[pickFolder] Pywebview API ready:', ready);
      }
      // When user opens a folder, reset any checked folders in the main tree
      // (acts like pressing "Check none") so we don't accidentally load
      // folders from a previous root selection.
      try {
        checkedFolderPaths.clear();
        renderFolderTree();
        debouncedAutoLoad(); // Unload current folder (same as Check None)
      } catch (e) { /* ignore */ }

      try {
        // PRIORITY 1: Python API (desktop app - all platforms)
        // When available, ALWAYS use this for consistency
        if (hasPywebviewApi && window.pywebview?.api?.choose_directory) {
          kdebug('[pickFolder] Using Python API for folder picker');
          try {
            setStatus('Opening folder picker...');
            const folderPath = await window.pywebview.api.choose_directory();
            if (folderPath) {
              // Scan the selected folder as the tree root (user may have picked a parent
              // folder with multiple analyzed sub-folders, or a leaf folder directly).
              treeExpandedPaths.clear();
              const treeScanned = await scanFolderTree(folderPath);
              // Use the root_has_kestrel flag returned directly by scanFolderTree
              // (folderTreeRootHasKestrel is set inside scanFolderTree).
              // Only attempt CSV load if the root itself is an analyzed folder.
              if (treeScanned && !folderTreeRootHasKestrel) {
                // Tree scan succeeded but root has no .kestrel — it's a parent folder.
                setStatus('Select a folder from the tree below to load its scenes');
              } else {
                // Either scan wasn't available, or the root itself has .kestrel — load it.
                await loadFolderFromPath(folderPath);
              }
              return; // Success - Python API handled everything
            } else {
              setStatus('Folder selection cancelled');
              return; // User cancelled
            }
          } catch (e) {
            console.error('Python API folder picker failed:', e);
            alert(`Desktop folder picker failed: ${e.message || e}\n\nPlease restart the application and try again.`);
            setStatus('Folder picker failed');
            return; // Don't fall through - Python API should always work in desktop app
          }
        }

        setStatus('Folder picker unavailable: Desktop API missing.');
      } catch (e) {
        console.error('Unexpected error in pickFolder:', e);
        setStatus('An unexpected error occurred');
      }
    });

    el('#saveCsv').addEventListener('click', saveCsv);
    el('#search').addEventListener('input', debounce(() => renderScenes(), 250));
    el('#speciesConf').addEventListener('change', () => renderScenes());
    el('#sortBy').addEventListener('change', () => {
      const s = loadSettings();
      s.sortBy = el('#sortBy').value;
      saveSettings(s);
      renderScenes();
    });

    (function initSortBy() {
      const sortSel = document.getElementById('sortBy');
      if (!sortSel) return;
      try { sortSel.value = getSetting('sortBy', 'captureTime'); } catch { sortSel.value = 'captureTime'; }
    })();

    // Apply initial auto-save visibility from cached localStorage settings
    (function initAutoSaveVisibility() {
      _autoSaveEnabled = getSetting('auto_save_enabled', true) !== false;
      if (!_autoSaveEnabled) {
        clearTimeout(_autoSaveTimer);
        _autoSaveTimer = null;
      }
      _updateSaveRevertVisibility();
    })();

    // Group-by-folder toggle
    (function initGroupByFolder() {
      const t = document.getElementById('groupByFolder');
      if (!t) return;
      try { t.checked = getSetting('groupByFolder', true); } catch { }
      t.addEventListener('change', () => {
        const s = loadSettings(); s.groupByFolder = !!t.checked; saveSettings(s); renderScenes();
      });
    })();

    // Group-by-capture-time toggle
    (function initGroupByTime() {
      const t = document.getElementById('groupByTime');
      if (!t) return;
      try { t.checked = getSetting('groupByTime', true); } catch { }
      t.addEventListener('change', () => {
        const s = loadSettings(); s.groupByTime = !!t.checked; saveSettings(s); renderScenes();
      });
    })();

    // Show bird thumbnails toggle
    (function initShowBirdThumbs() {
      const t = document.getElementById('showBirdThumbs');
      if (!t) return;
      try { t.checked = getSetting('showBirdThumbs', false); } catch { }
      t.addEventListener('change', () => {
        const s = loadSettings(); s.showBirdThumbs = !!t.checked; saveSettings(s); renderScenes();
      });
    })();

    // Scroll position indicator — shows current folder/time-group while scrolling
    (function initScrollPositionIndicator() {
      const mainEl = document.querySelector('main');
      const indicator = document.getElementById('scrollPositionIndicator');
      if (!mainEl || !indicator) return;
      let hideTimer = null;
      mainEl.addEventListener('scroll', () => {
        // Track both folder headers and timeline day banners
        const headers = [...sceneGrid.querySelectorAll('.folder-group-header, .timeline-day-banner')];
        if (!headers.length) { indicator.style.opacity = '0'; return; }
        const mainRect = mainEl.getBoundingClientRect();
        const thresholdY = mainRect.top + mainRect.height * 0.25;
        let bestHeader = null;
        for (const h of headers) {
          const r = h.getBoundingClientRect();
          if (r.top <= thresholdY) bestHeader = h;
          else if (!bestHeader) { bestHeader = h; break; }
        }
        if (!bestHeader) { indicator.style.opacity = '0'; return; }
        const nameEl = bestHeader.querySelector('.folder-group-name');
        const text = nameEl ? nameEl.textContent.trim() : bestHeader.textContent.trim();
        if (!text) { indicator.style.opacity = '0'; return; }
        indicator.textContent = text;
        indicator.style.opacity = '1';
        clearTimeout(hideTimer);
        hideTimer = setTimeout(() => { indicator.style.opacity = '0'; }, 1800);
      }, { passive: true });
    })();

    // Multi-select merge action bar
    const selectMergeBtn = document.getElementById('selectMergeBtn');
    if (selectMergeBtn) selectMergeBtn.addEventListener('click', executeSelectionMerge);
    const selectClearBtn = document.getElementById('selectClearBtn');
    if (selectClearBtn) selectClearBtn.addEventListener('click', () => { selectedSceneIds.clear(); _lastSelectedIdx = -1; updateSelectionUI(); });
    document.addEventListener('keydown', ev => { if (ev.key === 'Escape' && !document.querySelector('dialog[open]')) { if (selectedSceneIds.size > 0) { selectedSceneIds.clear(); _lastSelectedIdx = -1; updateSelectionUI(); } _clearGridFocus(); } });
    // Revert button
    const revertBtn = el('#revertCsv');
    if (revertBtn) revertBtn.addEventListener('click', () => {
      if (!_cleanSnapshot) return;
      if (!confirm('Discard all unsaved changes and revert to the last saved state?')) return;
      applySnapshot();
    });

    const zoomInBtn = el('#zoomIn');
    const zoomOutBtn = el('#zoomOut');
    if (zoomInBtn) zoomInBtn.addEventListener('click', () => { uiZoom = Math.min(1.4, uiZoom + 0.1); applyZoom(); });
    if (zoomOutBtn) zoomOutBtn.addEventListener('click', () => { uiZoom = Math.max(0.7, uiZoom - 0.1); applyZoom(); });

    // Initialize toolbar toggle for scene-level manual-reviewed filter
    (function initScenesManualFilter() {
      const t = document.getElementById('filterScenesManualRated');
      if (!t) return;
      try { t.checked = !!getSetting('onlyManualRatedScenes', false); } catch { }
      t.addEventListener('change', () => {
        const s = loadSettings(); s.onlyManualRatedScenes = !!t.checked; saveSettings(s);
        renderScenes();
      });
    })();

    // Initialize secondary species/families inclusion toggle
    (function initIncludeSecondary() {
      const t = document.getElementById('includeSecondarySpecies');
      if (!t) return;
      try { t.checked = getSetting('includeSecondarySpecies', false); } catch { }
      t.addEventListener('change', () => {
        const s = loadSettings(); s.includeSecondarySpecies = !!t.checked; saveSettings(s); renderScenes();
      });
    })();

    // Merge scenes feature
    function computeAllScenesForMerge() {
      // Group rows by scene_count; keep simple stats and representative
      const groups = new Map();
      for (const r of rows) {
        const id = r.scene_count;
        if (!groups.has(id)) groups.set(id, []);
        groups.get(id).push(r);
      }
      const list = [];
      for (const [id, arr] of groups) {
        // pick representative by max quality
        let rep = arr[0];
        for (const r of arr) if (parseNumber(r.quality) > parseNumber(rep.quality)) rep = r;
        const maxQ = Math.max(...arr.map(a => parseNumber(a.quality)));
        const rowRp = arr[0]?.__rootPath || rootPath || '';
        const sdScene = rowRp ? _scenedata[rowRp]?.scenes?.[id] : null;
        const name = sdScene?.name || (arr.find(a => (a.scene_name || '').trim().length)?.scene_name || '').trim();
        list.push({ id, imageCount: arr.length, maxQuality: maxQ, sceneName: name, repPath: (rep.export_path || rep.crop_path || ''), repFilename: rep.filename || '' });
      }
      // Sort numerically by id where possible
      return list.sort((a, b) => parseNumber(a.id) - parseNumber(b.id));
    }

    function openMergeDialog() {
      const dlg = document.getElementById('mergeDlg');
      const listEl = document.getElementById('mergeList');
      const summary = document.getElementById('mergeSummary');
      const applyBtn = document.getElementById('mergeApply');
      const targetInput = document.getElementById('mergeTargetId');
      const modeRadios = Array.from(document.querySelectorAll('input[name="mergeTargetMode"]'));

      const sceneList = computeAllScenesForMerge();
      listEl.innerHTML = '';

      const sel = new Set();

      function updateSummary() {
        const ids = Array.from(sel);
        const n = ids.length;
        const targetMode = modeRadios.find(r => r.checked)?.value || 'min';
        const targetId = targetMode === 'manual' && targetInput.value ? String(targetInput.value) : (n ? String(ids.map(x => parseNumber(x)).sort((a, b) => a - b)[0]) : '');
        const totalImgs = sceneList.filter(s => ids.includes(String(s.id))).reduce((acc, s) => acc + s.imageCount, 0);
        summary.textContent = n < 2 ? 'Select at least two scenes to merge.' : `Merging ${n} scenes into Scene ${targetId} (${totalImgs} images).`;
        applyBtn.disabled = n < 2 || !targetId;
      }

      // Build rows: [thumb] [checkbox + title] [count]
      for (const s of sceneList) {
        const row = document.createElement('div');
        row.style.display = 'contents';

        // Thumb cell
        const cThumb = document.createElement('div');
        const thumb = document.createElement('div'); thumb.className = 'thumb'; thumb.style.aspectRatio = '16/10';
        const img = document.createElement('img'); img.alt = s.repFilename || 'No preview'; img.loading = 'lazy';
        (async () => { const url = await getBlobUrlForPath(s.repPath); if (url) img.src = url; })();
        thumb.appendChild(img); cThumb.appendChild(thumb);

        // Title + checkbox cell
        const cTitle = document.createElement('div');
        const cb = document.createElement('input'); cb.type = 'checkbox'; cb.dataset.id = String(s.id); cb.style.marginRight = '8px';
        cb.addEventListener('change', () => { if (cb.checked) sel.add(cb.dataset.id); else sel.delete(cb.dataset.id); updateSummary(); });
        const title = document.createElement('span'); title.textContent = `Scene ${s.id}${s.sceneName ? ` — ${s.sceneName}` : ''}`; title.title = title.textContent;
        cTitle.appendChild(cb); cTitle.appendChild(title);

        // Count cell
        const cCount = document.createElement('div'); cCount.className = 'muted'; cCount.style.textAlign = 'right'; cCount.textContent = `${s.imageCount} images`;

        row.appendChild(cThumb); row.appendChild(cTitle); row.appendChild(cCount);
        listEl.appendChild(row);
      }

      // Wire radios
      modeRadios.forEach(r => r.onchange = updateSummary);
      targetInput.oninput = updateSummary;

      document.getElementById('mergeCancel').onclick = () => dlg.close();
      document.getElementById('mergeApply').onclick = () => {
        const ids = Array.from(sel).map(String);
        if (ids.length < 2) return;
        const targetMode = modeRadios.find(r => r.checked)?.value || 'min';
        let targetId = targetMode === 'manual' && targetInput.value ? String(targetInput.value) : String(ids.map(x => parseNumber(x)).sort((a, b) => a - b)[0]);
        if (!targetId) return;
        let changed = 0;
        for (const r of rows) {
          const idStr = String(r.scene_count);
          if (ids.includes(idStr) && idStr !== targetId) { r.scene_count = targetId; changed++; }
        }
        // Update scenedata: move filenames from non-target scenes into target scene
        let rpForMerge = '';
        if (hasPywebviewApi && changed > 0) {
          const rowSample = rows.find(r => ids.includes(String(r.scene_count)));
          rpForMerge = rowSample?.__rootPath || rootPath || '';
          if (rpForMerge) {
            const sd = _initScenedata(rpForMerge);
            const allMovedFiles = new Set();
            for (const id of ids) {
              if (id !== targetId && sd.scenes[id]) {
                for (const f of sd.scenes[id].image_filenames || []) allMovedFiles.add(f);
                delete sd.scenes[id];
              }
            }
            if (!sd.scenes[targetId]) {
              sd.scenes[targetId] = { scene_id: targetId, image_filenames: [], name: '', status: 'pending', user_tags: { species: [], families: [], finalized: false } };
            }
            for (const f of allMovedFiles) {
              if (!sd.scenes[targetId].image_filenames.includes(f)) sd.scenes[targetId].image_filenames.push(f);
            }
          }
        }
        if (changed) { markDirty(rpForMerge); setStatus(`Merged scenes into ${targetId}. ${changed} rows updated.`); }
        renderScenes();
        dlg.close();
      };

      updateSummary();
      dlg.showModal();
    }

    // Init
    loadVersionBadge();
    setStatus('Open your photo folder (the one that contains .kestrel) or select kestrel_database.csv');
    hydrateSettingsFromServer();

    // If a queue was running before this page loaded (e.g. page refresh), re-attach the polling
    (async () => {
      try {
        const status = await apiGetQueueStatus();
        if (status && (status.items || []).length > 0) {
          renderQueuePanel(status);
          if (status.running) startPollingQueue();
        }
      } catch (_) { }
      try {
        await maybeHandleStartupRecovery();
      } catch (_) { }
    })();

    // Wire "Change root…" button in the tree panel
    const treeChangeRootBtn = document.getElementById('treeChangeRoot');
    if (treeChangeRootBtn) {
      treeChangeRootBtn.addEventListener('click', async () => {
        if (!hasPywebviewApi) { const ready = await waitForPywebview(); if (!ready) return; }
        if (!window.pywebview?.api?.choose_directory) return;
        setStatus('Opening folder picker…');
        const folderPath = await window.pywebview.api.choose_directory();
        if (folderPath) {
          treeExpandedPaths.clear();
          checkedFolderPaths.clear();
          const treeScanned = await scanFolderTree(folderPath);
          if (treeScanned && !folderTreeRootHasKestrel) {
            setStatus('Select a folder from the tree below to load its scenes');
          } else {
            await loadFolderFromPath(folderPath);
          }
        } else {
          setStatus('Folder selection cancelled');
        }
      });
    }

    // Wire "Check all / Check none" buttons
    const treeCheckAllBtn = document.getElementById('treeCheckAll');
    if (treeCheckAllBtn) treeCheckAllBtn.addEventListener('click', checkAllTreeFolders);
    const treeCheckNoneBtn = document.getElementById('treeCheckNone');
    if (treeCheckNoneBtn) treeCheckNoneBtn.addEventListener('click', checkNoneTreeFolders);

    // Wire "Load checked" button (removed from HTML; kept as no-op guard)
    const treeLoadSelectedBtn = document.getElementById('treeLoadSelected');
    if (treeLoadSelectedBtn) {
      treeLoadSelectedBtn.addEventListener('click', async () => {
        if (checkedFolderPaths.size === 0) return;
        await loadMultipleFolders(Array.from(checkedFolderPaths));
      });
    }

    // ── Analysis Queue event wiring ───────────────────────────────────────────────

    // "Analyze Folders…" button opens the dialog
    const analyzeQueueBtn = document.getElementById('analyzeQueueBtn');
    if (analyzeQueueBtn) {
      analyzeQueueBtn.addEventListener('click', openAnalyzeDialog);
    }

    // Analyze dialog Cancel
    const analyzeDlgCancel = document.getElementById('analyzeDlgCancel');
    if (analyzeDlgCancel) {
      analyzeDlgCancel.addEventListener('click', () => {
        // Save the current selection so user can restore it on next dialog open
        if (_dlgSelected && _dlgSelected.size > 0) {
          const s = loadSettings();
          s.lastQueueState = Array.from(_dlgSelected);
          saveSettings(s);
        }
        document.getElementById('analyzeQueueDlg').close();
      });
    }

    // Analyze dialog Add to Queue
    const analyzeDlgAdd = document.getElementById('analyzeDlgAdd');
    if (analyzeDlgAdd) {
      analyzeDlgAdd.addEventListener('click', async () => {
        const paths = Array.from(_dlgSelected);
        if (paths.length === 0) return;
        const useGpu = document.getElementById('analyzeUseGpu')?.checked ?? true;
        const wildlifeEnabled = document.getElementById('analyzeWildlife')?.checked ?? false;
        const speciesDetectionEnabled = document.getElementById('analyzeSpeciesDetection')?.checked ?? true;
        const retryErrored = document.getElementById('adlgRetryErrored')?.checked ?? true;

        // Check for outdated-version folders not already confirmed for re-analysis
        const outdatedPaths = [];
        function findNode(node, targetPath) {
          if (node.path === targetPath) return node;
          if (node.children) {
            for (const c of node.children) {
              const found = findNode(c, targetPath);
              if (found) return found;
            }
          }
          return null;
        }
        for (const p of paths) {
          if (_dlgReanalyze.has(p)) continue; // already confirmed at selection time
          const node = folderTreeRootNode ? findNode(folderTreeRootNode, p) : null;
          if (node && isVersionOutdated(node)) {
            outdatedPaths.push({ path: p, name: node.name, version: node.kestrel_version });
          }
        }

        if (outdatedPaths.length > 0) {
          const names = outdatedPaths.map(o => `  • ${o.name} (v${o.version})`).join('\n');
          const confirmed = confirm(
            `The following folder(s) were analyzed on an older version of Kestrel:\n\n${names}\n\n` +
            `Current version: v${_appVersion}\n\n` +
            `Re-analyzing will DELETE existing analysis data (.kestrel folder) before proceeding.\n\n` +
            `Continue?`
          );
          if (!confirmed) return;
          // Clear .kestrel for outdated folders before re-analysis
          for (const o of outdatedPaths) {
            try {
              await window.pywebview.api.clear_kestrel_data(o.path);
              // Update in-memory node
              const node = findNode(folderTreeRootNode, o.path);
              if (node) { node.has_kestrel = false; node.kestrel_version = ''; }
            } catch (e) {
              console.warn('Failed to clear kestrel data for', o.path, e);
            }
          }
        }

        // Clear .kestrel for fully-analyzed re-queue folders (confirmed at selection time)
        for (const p of _dlgReanalyze) {
          if (!paths.includes(p)) continue;
          try {
            await window.pywebview.api.clear_kestrel_data(p);
            const node = folderTreeRootNode ? findNode(folderTreeRootNode, p) : null;
            if (node) { node.has_kestrel = false; node.kestrel_version = ''; }
          } catch (e) {
            console.warn('Failed to clear kestrel data for re-analyze', p, e);
          }
        }

        // Persist advanced analysis settings before starting the queue
        {
          const dtVal = Math.max(0.1, Math.min(0.99, parseFloat(document.getElementById('adlgDetectionThreshold')?.value) || 0.25));
          const mbcRaw = parseInt(document.getElementById('adlgMaxBirdCrops')?.value, 10);
          const mbcVal = Math.max(1, Math.min(20, Number.isFinite(mbcRaw) ? mbcRaw : 10));
          const eqRaw = String(document.getElementById('adlgExposureQuality')?.value || 'balanced').toLowerCase();
          const eqVal = ['lenient', 'balanced', 'aggressive'].includes(eqRaw) ? eqRaw : 'balanced';
          const modelRaw = String(document.getElementById('adlgWildlifeModelMode')?.value || 'fast').toLowerCase();
          const modelVal = modelRaw === 'accurate' ? 'accurate' : 'fast';
          const detectorName = modelVal === 'accurate' ? 'mdv5a' : 'mdv1000-cedar';
          const stVal = Math.max(0, parseFloat(document.getElementById('adlgSceneTime')?.value) || 1.0);
          const ppRaw = parseInt(document.getElementById('adlgParallelPrefetch')?.value, 10);
          const ppVal = Math.max(1, Math.min(5, Number.isFinite(ppRaw) ? ppRaw : 3));
          const twRaw = parseInt(document.getElementById('adlgThumbnailMaxWidth')?.value, 10);
          const twVal = Math.max(400, Math.min(2400, Number.isFinite(twRaw) ? twRaw : 1200));
          const tcRaw = parseFloat(document.getElementById('adlgThumbnailJpegCompression')?.value);
          const tcVal = Math.max(0.5, Math.min(1.0, Number.isFinite(tcRaw) ? tcRaw : 0.75));
          const tqVal = Math.max(50, Math.min(100, Math.round(tcVal * 100)));
          const adlgSettings = loadSettings();
          adlgSettings.detection_threshold = dtVal;
          adlgSettings.max_bird_crops = mbcVal;
          adlgSettings.exposure_quality = eqVal;
          adlgSettings.wildlife_model_mode = modelVal;
          adlgSettings.detector_name = detectorName;
          adlgSettings.scene_time_threshold = stVal;
          adlgSettings.parallel_prefetch = ppVal;
          adlgSettings.thumbnail_max_width = twVal;
          adlgSettings.thumbnail_jpeg_compression = tcVal;
          adlgSettings.thumbnail_jpeg_quality = tqVal;
          saveSettings(adlgSettings);
          if (hasPywebviewApi && window.pywebview?.api?.save_settings_data) {
            try { await window.pywebview.api.save_settings_data(adlgSettings); } catch (_) { }
          }
        }

        document.getElementById('analyzeQueueDlg').close();
        analyzeDlgAdd.disabled = true;
        try {
          // Show loading overlay while analyzer imports models (lazy-load)
          showLoadingAnalyzer();
          const result = await apiStartQueue(paths, useGpu, wildlifeEnabled, retryErrored, speciesDetectionEnabled);
          if (result && result.success) {
            queuedFolderPaths.clear();
            _dlgSelected.clear();
            _isFirstQueueStart = true; // reset for Case 1 logic on next queue start
            // Clear saved queue state since we're starting a new queue
            const s = loadSettings();
            delete s.lastQueueState;
            saveSettings(s);
            // Clear session state for new queue start, so ETA calculations use fresh folder inspections
            _queueSessionStartState.clear();
            _queueFolderInspections.clear();
            startPollingQueue();
            const status = await apiGetQueueStatus();
            renderQueuePanel(status);
            setStatus(`Analysis queue started — ${result.added || paths.length} folder(s) queued`);
            // Start polling; renderQueuePanel will hide the loader when processing begins.
            // As a safety, hide the loader after 30s if nothing starts.
            setTimeout(() => { try { hideLoadingAnalyzer(); } catch (e) { } }, 30000);
          } else {
            hideLoadingAnalyzer();
            alert('Failed to start analysis queue:\n\n' + (result?.error || 'Unknown error'));
          }
        } catch (e) {
          hideLoadingAnalyzer();
          alert('Failed to start analysis queue:\n\n' + (e.message || e));
        } finally {
          analyzeDlgAdd.disabled = false;
        }
      });
    }

    // Analyze dialog: Change Folder button
    document.getElementById('analyzeDlgChangeRoot')?.addEventListener('click', async () => {
      if (!hasPywebviewApi) { alert('Directory browsing is only available in the desktop app.'); return; }
      const fp = await window.pywebview.api.choose_directory();
      if (!fp) return;
      await scanFolderTree(fp);
      if (!folderTreeRootNode) return;
      _dlgExpandedPaths = new Set([folderTreeRootNode.path]);
      _dlgSelected.clear();
      function refreshDlg2() {
        const countEl = document.getElementById('analyzeDlgCount');
        const addBtn = document.getElementById('analyzeDlgAdd');
        if (countEl) countEl.textContent = _dlgSelected.size + ' folder' + (_dlgSelected.size === 1 ? '' : 's') + ' selected';
        if (addBtn) addBtn.disabled = _dlgSelected.size === 0;
        _refreshAnalyzeDlgQueuePreview();
      }
      const treeEl = document.getElementById('analyzeDlgTree');
      treeEl.innerHTML = '';
      treeEl.appendChild(buildAnalyzeDlgNode(folderTreeRootNode, _dlgSelected, refreshDlg2));
      populateAnalyzeFolderCounts();
      refreshDlg2();
    });

    // ── Welcome Panel action wiring ──────────────────────────────────────────────

    // ── Legal Agreement Logic ──────────────────────────────────────────────
    let _pendingLegalEffectiveDate = '';
    const DEFAULT_TERMS_URL = 'https://projectkestrel.org/terms-of-use';
    const DEFAULT_PRIVACY_URL = 'https://projectkestrel.org/privacy-policy';

    function renderLegalBanner(status) {
      const banner = document.getElementById('legalNotice');
      const msgEl = document.getElementById('legalNoticeMsg');
      const termsLink = document.getElementById('legalViewTerms');
      const privacyLink = document.getElementById('legalViewPrivacy');
      if (!banner || !msgEl) return;

      const termsUrl = status?.terms_url || DEFAULT_TERMS_URL;
      const privacyUrl = status?.privacy_url || DEFAULT_PRIVACY_URL;
      if (termsLink) termsLink.href = termsUrl;
      if (privacyLink) privacyLink.href = privacyUrl;

      if (status?.reason === 'terms_updated') {
        msgEl.textContent = 'The Privacy Policy and/or Terms of Use have been updated. Please review and accept to continue using Project Kestrel.';
      } else {
        msgEl.textContent = 'By using Project Kestrel you agree to our Terms of Use and Privacy Policy.';
      }
      banner.classList.remove('hidden');
    }

    async function checkLegalAgreement() {
      if (!hasPywebviewApi || !window.pywebview?.api?.get_legal_status) return;
      try {
        const status = await window.pywebview.api.get_legal_status();
        _pendingLegalEffectiveDate = status?.effective_date || '';
        if (!status?.agreed) {
          renderLegalBanner(status);
        } else {
          document.getElementById('legalNotice')?.classList.add('hidden');
        }
      } catch (e) {
        console.error('Failed to check legal status', e);
      }
    }

    const legalAgreeBtn = document.getElementById('legalAgreeBtn');
    if (legalAgreeBtn) {
      legalAgreeBtn.addEventListener('click', async () => {
        try {
          if (hasPywebviewApi && window.pywebview?.api?.agree_to_legal) {
            await window.pywebview.api.agree_to_legal(_pendingLegalEffectiveDate || '');
            // Keep local settings in sync immediately after consent so later
            // UI-driven settings writes include the persisted legal flags.
            await hydrateSettingsFromServer();
            document.getElementById('legalNotice').classList.add('hidden');
            showToast('Terms accepted. Welcome to Project Kestrel!', 4000);
          }
        } catch (e) {
          console.error('Failed to agree to legal terms', e);
        }
      });
    }

    // Initial checks
    if (hasPywebviewApi) {
      checkLegalAgreement();
    }

    // Queue panel header: toggle expand / collapse
    const queuePanelHeader = document.getElementById('queuePanelHeader');
    if (queuePanelHeader) {
      queuePanelHeader.addEventListener('click', () => {
        _queuePanelExpanded = !_queuePanelExpanded;
        const toggle = document.getElementById('queuePanelToggle');
        const body = document.getElementById('queuePanelBody');
        const controls = document.getElementById('queuePanelControls');
        if (toggle) toggle.classList.toggle('open', _queuePanelExpanded);
        if (body) body.classList.toggle('hidden', !_queuePanelExpanded);
        if (controls) controls.classList.toggle('hidden', !_queuePanelExpanded);
      });
    }

    // Pause / Resume button
    const queuePauseBtn = document.getElementById('queuePauseBtn');
    if (queuePauseBtn) {
      queuePauseBtn.addEventListener('click', async () => {
        try {
          const status = await apiGetQueueStatus();
          if (status.paused) {
            // When resuming, re-inspect folders to get accurate baselines
            if (status.items && status.items.length > 0 && hasPywebviewApi && window.pywebview?.api?.inspect_folders) {
              try {
                const paths = status.items.map(item => item.path);
                const inspectRes = await window.pywebview.api.inspect_folders(paths);
                if (inspectRes && inspectRes.success && inspectRes.results) {
                  for (const [path, info] of Object.entries(inspectRes.results)) {
                    if (info) {
                      const initialProcessed = info.processed || 0;
                      const totalImages = info.total || 0;
                      const toAnalyze = Math.max(0, totalImages - initialProcessed);
                      _queueSessionStartState.set(path, {
                        initialProcessed,
                        totalImages,
                        toAnalyze
                      });
                    }
                  }
                }
              } catch (e) { /* ignore */ }
            }
            await apiQueueControl('resume');
          } else {
            await apiQueueControl('pause');
          }
        } catch (_) { }
      });
    }

    // Cancel button
    const queueCancelBtn = document.getElementById('queueCancelBtn');
    if (queueCancelBtn) {
      queueCancelBtn.addEventListener('click', async () => {
        if (!confirm('Cancel the analysis queue? Pending folders will not be analyzed.')) return;
        try { await apiQueueControl('cancel'); } catch (_) { }
      });
    }

    // Clear done button
    const queueClearBtn = document.getElementById('queueClearBtn');
    if (queueClearBtn) {
      queueClearBtn.addEventListener('click', async () => {
        try {
          await apiQueueControl('clear');
          const status = await apiGetQueueStatus();
          if (!(status.items || []).some(i => i.status === 'pending' || i.status === 'running')) {
            document.getElementById('queuePanel')?.classList.add('hidden');
            stopPollingQueue();
          } else {
            renderQueuePanel(status);
          }
        } catch (_) { }
      });
    }

    // ---- Culling Assistant launcher ----
    async function openCullingAssistant(rootPath) {
      if (!window.pywebview?.api) {
        showToast('Culling Assistant requires desktop mode', 4000);
        return;
      }
      // Prompt to save unsaved changes before opening (using custom dialog)
      if (dirty) {
        const userChoice = await showCullingAssistantPrompt();
        if (userChoice === 'cancel') {
          return;
        }
        if (userChoice === 'save') {
          await saveCsv();
        }
      }
      try {
        showToast('Opening Culling Assistant...', 2000);
        const res = await window.pywebview.api.open_culling_window(rootPath);
        if (res && !res.success) {
          showToast('Failed to open Culling Assistant: ' + (res.error || 'Unknown error'), 5000);
        }
      } catch (e) {
        console.error('openCullingAssistant error', e);
        showToast('Error opening Culling Assistant', 4000);
      }
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

    // ---- Adjust Capture Time dialog ----
    //
    // Shifts every row's `capture_time` in the given folder by a user-supplied
    // number of hours (can be fractional and/or negative). Values are parsed
    // as ISO strings, offset in milliseconds, and re-serialised as ISO without
    // timezone suffixes so the shift stays consistent on reload. Rows that
    // lack a parseable capture time are left untouched.
    function _shiftIsoTime(iso, offsetMs) {
      if (!iso) return '';
      const trimmed = String(iso).trim();
      if (!trimmed) return '';
      let d = new Date(trimmed);
      if (isNaN(d)) d = new Date(trimmed.replace(' ', 'T'));
      if (isNaN(d)) return trimmed;
      const shifted = new Date(d.getTime() + offsetMs);
      if (isNaN(shifted)) return trimmed;
      const p = (n) => String(n).padStart(2, '0');
      // Mirror the pipeline format: local-ish ISO without timezone suffix,
      // matching `datetime.isoformat()` output on capture_time writes.
      return `${shifted.getFullYear()}-${p(shifted.getMonth()+1)}-${p(shifted.getDate())}T${p(shifted.getHours())}:${p(shifted.getMinutes())}:${p(shifted.getSeconds())}`;
    }

    // Format a ms timestamp as a friendly local string like
    //   "Tue, Oct 14, 2025, 14:32:47"
    function _formatPrettyLocalTime(ms) {
      if (!Number.isFinite(ms)) return '—';
      const d = new Date(ms);
      try {
        return d.toLocaleString(undefined, {
          weekday: 'short', year: 'numeric', month: 'short', day: 'numeric',
          hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
        });
      } catch (_) {
        return d.toISOString();
      }
    }

    // Convert a ms timestamp to a "YYYY-MM-DDTHH:MM:SS" string in local time,
    // suitable for a <input type="datetime-local" step="1"> value.
    function _msToDatetimeLocalValue(ms) {
      if (!Number.isFinite(ms)) return '';
      const d = new Date(ms);
      const p = (n) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }

    // Parse a <input type="datetime-local"> value back into a ms timestamp
    // (interpreted in local time, same convention the pipeline uses).
    function _datetimeLocalValueToMs(val) {
      if (!val) return Number.NaN;
      const m = String(val).match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/);
      if (!m) return Number.NaN;
      const d = new Date(
        Number(m[1]), Number(m[2]) - 1, Number(m[3]),
        Number(m[4]), Number(m[5]), Number(m[6] || 0),
      );
      const ms = d.getTime();
      return Number.isFinite(ms) ? ms : Number.NaN;
    }

    // Pretty-print a signed ms offset as "+2h 30m" / "-45m 12s" / "0".
    function _formatOffsetMs(offsetMs) {
      if (!Number.isFinite(offsetMs) || offsetMs === 0) return '0';
      const sign = offsetMs > 0 ? '+' : '−';
      const totalSec = Math.round(Math.abs(offsetMs) / 1000);
      const h = Math.floor(totalSec / 3600);
      const m = Math.floor((totalSec % 3600) / 60);
      const s = totalSec % 60;
      const parts = [];
      if (h) parts.push(`${h}h`);
      if (m) parts.push(`${m}m`);
      if (s || !parts.length) parts.push(`${s}s`);
      return `${sign}${parts.join(' ')}`;
    }

    function showAdjustCaptureTimeDialog(folderPath) {
      if (!folderPath) { showToast('No folder selected', 2500); return; }
      const folderName = folderBaseName(folderPath) || folderPath;
      const targetRows = rows.filter(r => r.__rootPath === folderPath);
      if (!targetRows.length) { showToast('No images loaded for this folder', 2500); return; }

      // Sort all rows that have a parseable capture time chronologically so
      // "the first photo" the user sees is the actual earliest capture — not
      // whatever happens to be first in the CSV.
      const timedRows = targetRows
        .filter(r => Number.isFinite(parseCaptureTimeMs(r.capture_time)))
        .slice()
        .sort((a, b) => parseCaptureTimeMs(a.capture_time) - parseCaptureTimeMs(b.capture_time));
      const anchorRow = timedRows[0] || null;
      const anchorOriginalIso = anchorRow ? String(anchorRow.capture_time || '') : '';
      const anchorOriginalMs = anchorRow ? parseCaptureTimeMs(anchorOriginalIso) : Number.NaN;
      const withTime = timedRows.length;

      const dlg = document.createElement('dialog');
      dlg.style.cssText = [
        'border:1px solid #303a52', 'border-radius:12px', 'background:#141a24',
        'color:#e8f0f8', 'padding:0', 'width:min(540px,96vw)',
        // `fit-content` rather than `auto` — on Chromium a dialog with
        // `height:auto` + `max-height:Xvh` + `overflow-y:auto` can render
        // at the full max-height instead of shrinking to content. Using
        // `fit-content` consistently hugs the content.
        'height:fit-content', 'max-height:92vh',
        'overflow-x:hidden', 'overflow-y:auto',
        'box-shadow:0 8px 40px rgba(0,0,0,0.6)',
      ].join(';');

      const initialValue = _msToDatetimeLocalValue(anchorOriginalMs);

      dlg.innerHTML = `
        <div style="padding:18px 22px 12px;border-bottom:1px solid #222e45;">
          <div style="font-size:16px;font-weight:700;margin-bottom:3px;">Adjust Capture Time</div>
          <div style="color:#7a90b8;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${escapeHtml(folderPath)}">${escapeHtml(folderName)} · ${targetRows.length} image${targetRows.length === 1 ? '' : 's'} · ${withTime} with capture time</div>
        </div>
        <div style="padding:16px 22px;">
          <div style="font-size:12px;color:#9fb0cc;line-height:1.55;margin-bottom:14px;">
            Use this when your camera clock was set to the wrong time zone — for example, travelling abroad with a body still on home time, or a daylight-savings change that didn't get picked up. Correcting the time on the photo below will apply the same offset to every photo in this folder.
          </div>
          ${anchorRow ? `
            <div style="display:flex;gap:14px;align-items:flex-start;background:#0f1422;border:1px solid #1e2638;border-radius:8px;padding:12px;margin-bottom:14px;">
              <div style="flex:0 0 auto;width:104px;height:104px;background:#0a0d15;border:1px solid #1c2438;border-radius:6px;overflow:hidden;display:flex;align-items:center;justify-content:center;">
                <img id="actThumb" alt="" style="max-width:100%;max-height:100%;object-fit:contain;display:none;" />
                <div id="actThumbFallback" style="color:#475670;font-size:10px;">loading…</div>
              </div>
              <div style="flex:1 1 auto;min-width:0;">
                <div style="font-size:11px;color:#7a90b8;margin-bottom:2px;">Earliest photo in this folder</div>
                <div style="font-size:12px;color:#cbd2dc;word-break:break-all;margin-bottom:10px;" title="${escapeHtml(anchorRow.filename || '')}">${escapeHtml(anchorRow.filename || '(unknown file)')}</div>
                <label style="display:flex;flex-direction:column;gap:4px;">
                  <span style="font-size:11px;color:#a9c9ee;font-weight:600;">This photo was taken at:</span>
                  <input id="actDateInput" type="datetime-local" step="1" value="${escapeHtml(initialValue)}"
                    style="padding:7px 10px;border:1px solid #2a3040;background:#0e1320;color:#e8f0f8;border-radius:6px;font-size:13px;font-family:inherit;" />
                </label>
              </div>
            </div>
            <div id="actSummary" style="background:#15192a;border:1px solid #243043;border-radius:6px;padding:10px 12px;margin-bottom:16px;font-size:12px;color:#a9c9ee;line-height:1.6;"></div>
          ` : `
            <div style="background:#2a1a1a;border:1px solid #52323a;border-radius:6px;padding:12px;margin-bottom:16px;font-size:12px;color:#d0a0a0;line-height:1.5;">
              No images in this folder have a readable capture time, so there's nothing to shift.
            </div>
          `}
          <div style="display:flex;gap:8px;justify-content:flex-end;">
            <button id="actCancel" style="padding:8px 16px;border:1px solid #3a465f;background:#1c2433;color:#e8f0f8;border-radius:6px;cursor:pointer;font-size:13px;">Cancel</button>
            <button id="actApply" ${anchorRow ? '' : 'disabled'} style="padding:8px 16px;border:1px solid #2a5fa8;background:#1a3a6a;color:#7eb8e0;border-radius:6px;cursor:${anchorRow ? 'pointer' : 'not-allowed'};font-size:13px;font-weight:600;opacity:${anchorRow ? '1' : '.55'};">Save</button>
          </div>
        </div>
      `;
      document.body.appendChild(dlg);
      const closeAndRemove = () => { try { dlg.close(); } catch (_) {} if (dlg.parentNode) dlg.parentNode.removeChild(dlg); };

      // Load the anchor photo thumbnail asynchronously (desktop only).
      //
      // Prefer `export_path` (the cached JPEG preview Kestrel generates for
      // every image, including raws) and fall back to `crop_path`. Using
      // `filename` alone fails silently for raw formats like CR3/NEF/ARW
      // because the browser can't decode them directly — that's why the
      // placeholder was stuck on "loading…".
      if (anchorRow) {
        const thumbRel = anchorRow.export_path || anchorRow.crop_path || anchorRow.filename || '';
        if (thumbRel) {
          (async () => {
            const imgEl = dlg.querySelector('#actThumb');
            const fbEl = dlg.querySelector('#actThumbFallback');
            try {
              const url = await getBlobUrlForPath(thumbRel, folderPath);
              if (url && imgEl) {
                imgEl.addEventListener('load', () => {
                  imgEl.style.display = 'block';
                  if (fbEl) fbEl.style.display = 'none';
                });
                imgEl.addEventListener('error', () => {
                  if (fbEl) fbEl.textContent = '—';
                });
                imgEl.src = url;
              } else if (fbEl) {
                fbEl.textContent = '—';
              }
            } catch (_) {
              if (fbEl) fbEl.textContent = '—';
            }
          })();
        }
      }

      const dateInput = dlg.querySelector('#actDateInput');
      const summaryEl = dlg.querySelector('#actSummary');
      const applyBtn = dlg.querySelector('#actApply');

      const setApplyEnabled = (enabled) => {
        if (!applyBtn) return;
        applyBtn.disabled = !enabled;
        applyBtn.style.opacity = enabled ? '1' : '.55';
        applyBtn.style.cursor = enabled ? 'pointer' : 'not-allowed';
      };

      const renderSummary = () => {
        if (!dateInput || !summaryEl) return;
        const newMs = _datetimeLocalValueToMs(dateInput.value);
        if (!Number.isFinite(newMs) || !Number.isFinite(anchorOriginalMs)) {
          summaryEl.innerHTML = '<span style="color:#d08080;">Enter a valid date &amp; time.</span>';
          setApplyEnabled(false);
          return;
        }
        const offsetMs = newMs - anchorOriginalMs;
        const prettyOriginal = _formatPrettyLocalTime(anchorOriginalMs);
        if (offsetMs === 0) {
          summaryEl.innerHTML = `
            <div style="color:#7a90b8;">No change — the new time matches what was originally reported.</div>
            <div style="margin-top:4px;"><span style="color:#7a90b8;">Original reported time:</span> <code style="background:#1c2438;padding:1px 5px;border-radius:3px;">${escapeHtml(prettyOriginal)}</code></div>
          `;
          setApplyEnabled(false);
          return;
        }
        setApplyEnabled(true);
        summaryEl.innerHTML = `
          <div><span style="color:#7a90b8;">Original reported time:</span> <code style="background:#1c2438;padding:1px 5px;border-radius:3px;">${escapeHtml(prettyOriginal)}</code></div>
          <div style="margin-top:4px;"><span style="color:#7a90b8;">This will shift all ${withTime} timed photo${withTime === 1 ? '' : 's'} by:</span> <b style="color:#8fc4ff;">${escapeHtml(_formatOffsetMs(offsetMs))}</b></div>
        `;
      };

      if (dateInput) {
        dateInput.addEventListener('input', renderSummary);
        dateInput.addEventListener('change', renderSummary);
        renderSummary();
      }

      dlg.querySelector('#actCancel').addEventListener('click', closeAndRemove);
      if (anchorRow && applyBtn) {
        applyBtn.addEventListener('click', () => {
          const newMs = _datetimeLocalValueToMs(dateInput.value);
          if (!Number.isFinite(newMs) || !Number.isFinite(anchorOriginalMs)) { closeAndRemove(); return; }
          const offsetMs = newMs - anchorOriginalMs;
          if (offsetMs === 0) { closeAndRemove(); return; }
          let changed = 0;
          for (const r of targetRows) {
            const next = _shiftIsoTime(r.capture_time, offsetMs);
            if (next && next !== r.capture_time) {
              r.capture_time = next;
              changed++;
            }
          }
          if (changed > 0) {
            markDirty(folderPath);
            try { renderScenes(); } catch (_) {}
            showToast(`Shifted ${changed} capture time${changed === 1 ? '' : 's'} by ${_formatOffsetMs(offsetMs)}`, 3500);
          } else {
            showToast('No capture times changed', 2500);
          }
          closeAndRemove();
        });
      }
      dlg.addEventListener('close', () => { if (dlg.parentNode) dlg.parentNode.removeChild(dlg); });
      dlg.showModal();
    }

    // ---- Write Metadata launcher ----
    async function writeMetadataForFolder(rootPath) {
      if (!window.pywebview?.api) {
        showToast('Write Metadata requires desktop mode', 4000);
        return;
      }
      const folderRows = rows.filter(r => r.__rootPath === rootPath);
      if (!folderRows.length) {
        showToast('No images found for this folder', 3000);
        return;
      }

      const folderName = folderBaseName(rootPath) || rootPath;
      const imageCount = folderRows.length;

      const dlg = document.createElement('dialog');
      dlg.style.cssText = [
        'border:1px solid #303a52', 'border-radius:12px', 'background:#141a24',
        'color:#e8f0f8', 'padding:0', 'min-width:440px', 'max-width:560px',
        'width:90vw',
        // `fit-content` hugs the actual content (Chromium quirk: `auto` +
        // `max-height` + `overflow-y:auto` can render at the max-height).
        'height:fit-content', 'max-height:92vh',
        'overflow-x:hidden', 'overflow-y:auto',
        'box-shadow:0 8px 40px rgba(0,0,0,0.6)',
      ].join(';');

      const xmpFieldDefaults = {
        rating: true,
        label: true,
        species: true,
        family: true,
        quality: true,
      };
      const savedXmpFields = getSetting('xmp_fields', xmpFieldDefaults) || {};
      const xmpFields = { ...xmpFieldDefaults, ...savedXmpFields };
      const fieldRow = (key, title, desc) => `
        <label style="display:flex;align-items:flex-start;gap:8px;padding:5px 8px;border-radius:5px;cursor:pointer;" class="wm-field-row">
          <input type="checkbox" data-xmp-field="${key}" ${xmpFields[key] ? 'checked' : ''} style="margin-top:2px;flex-shrink:0;" />
          <span style="display:flex;flex-direction:column;gap:1px;min-width:0;">
            <span style="font-size:12px;font-weight:600;color:#e8f0f8;">${title}</span>
            <span style="font-size:11px;color:#7a90b8;line-height:1.35;">${desc}</span>
          </span>
        </label>`;

      dlg.innerHTML = `
        <div style="padding:20px 22px 14px;border-bottom:1px solid #222e45;">
          <div style="font-size:17px;font-weight:700;margin-bottom:4px;">Write Photo Metadata</div>
          <div style="color:#7a90b8;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${escapeHtml(rootPath)}">${escapeHtml(folderName)} &middot; ${imageCount} image${imageCount === 1 ? '' : 's'}</div>
        </div>

        <div id="wmOptView" style="padding:16px 22px;">
          <div style="background:#1a2235;border:1px solid #263045;border-radius:8px;padding:12px 14px;margin-bottom:12px;display:flex;gap:12px;align-items:flex-start;">
            <div style="font-size:18px;margin-top:2px;">📝</div>
            <div style="flex:1;min-width:0;">
              <div style="font-size:13px;font-weight:600;margin-bottom:4px;">XMP Sidecar Files</div>
              <div style="font-size:12px;color:#7a90b8;line-height:1.5;">Writes a <code style="background:#1c2438;padding:1px 4px;border-radius:3px;">.xmp</code> sidecar file next to each original. Embeds star ratings, Accept/Reject decisions, and species tags in a format readable by Lightroom, Capture One, darktable, and other editors.</div>
            </div>
          </div>
          <div style="background:#15192a;border:1px solid #243043;border-radius:8px;padding:10px 14px;margin-bottom:12px;">
            <div style="font-size:12px;font-weight:600;color:#a9c9ee;margin-bottom:6px;display:flex;align-items:center;gap:6px;">
              <span style="font-size:13px;">⚙️</span> Fields to write
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:2px;">
              ${fieldRow('rating', 'Star Rating', 'xmp:Rating (0–5 stars).')}
              ${fieldRow('label', 'Color Label', 'xmp:Label — Green/Red for Accept/Reject.')}
              ${fieldRow('species', 'Species Tag', 'kestrel:Species + species keyword.')}
              ${fieldRow('family', 'Family Tag', 'kestrel:Family + family keyword.')}
              ${fieldRow('quality', 'Quality Score', 'kestrel:QualityScore (0–1).')}
            </div>
          </div>
          <div style="background:#1a1f10;border:1px solid #3a4020;border-radius:6px;padding:10px 14px;margin-bottom:16px;font-size:12px;color:#b0c070;line-height:1.5;">
            &#9888; <b>Write metadata before importing into your photo editor.</b> Most catalogues ignore new sidecar files once a photo is already imported. Write first, then import, for best results.<br>Kestrel will not overwrite XMP files generated by other software without your permission.
          </div>
          <div style="display:flex;gap:8px;justify-content:flex-end;">
            <button id="wmCancel" style="padding:8px 16px;border:1px solid #3a465f;background:#1c2433;color:#e8f0f8;border-radius:6px;cursor:pointer;font-size:13px;">Cancel</button>
            <button id="wmOk" style="padding:8px 16px;border:1px solid #2a5fa8;background:#1a3a6a;color:#7eb8e0;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600;">Write Metadata &#10003;</button>
          </div>
        </div>

        <div id="wmProgressView" style="display:none;padding:16px 22px;">
          <ul id="wmStepsList" style="list-style:none;margin:0 0 16px;padding:0;display:flex;flex-direction:column;gap:6px;"></ul>
          <div id="wmProgressActions" style="display:none;justify-content:flex-end;">
            <button id="wmDone" style="padding:8px 16px;border:1px solid #2a5fa8;background:#1a3a6a;color:#7eb8e0;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600;">Done</button>
          </div>
        </div>

        <div id="wmConflictView" style="display:none;padding:16px 22px;">
          <p id="wmConflictDesc" style="font-size:13px;line-height:1.5;margin:0 0 10px;color:#9fb0cc;"></p>
          <ul id="wmConflictList" style="max-height:160px;overflow-y:auto;list-style:none;padding:0;margin:0 0 16px;font-size:12px;color:#7a90b8;border:1px solid #222e45;border-radius:6px;"></ul>
          <div style="display:flex;gap:8px;justify-content:flex-end;">
            <button id="wmSkip" style="padding:8px 16px;border:1px solid #3a465f;background:#1c2433;color:#e8f0f8;border-radius:6px;cursor:pointer;font-size:13px;">Skip these files</button>
            <button id="wmOverwrite" style="padding:8px 12px;border:1px solid #7f3f3f;background:#5c2a2a;color:#ffdede;border-radius:6px;cursor:pointer;font-size:13px;">Overwrite Anyway</button>
          </div>
        </div>
      `;
      document.body.appendChild(dlg);

      const closeAndRemove = () => {
        try { dlg.close(); } catch (_) {}
        if (dlg.parentNode) dlg.parentNode.removeChild(dlg);
      };

      const showView = (id) => {
        ['wmOptView', 'wmProgressView', 'wmConflictView'].forEach(v => {
          const el = dlg.querySelector('#' + v);
          if (el) el.style.display = (v === id) ? 'block' : 'none';
        });
      };

      const addStep = (id, label, state) => {
        const icons  = { pending:'○', running:'⟳', done:'✓', failed:'✗', skipped:'–' };
        const colors = { pending:'#7a90b8', running:'#6aa0ff', done:'#50c878', failed:'#ff6b6b', skipped:'#555' };
        const li = document.createElement('li');
        li.id = 'wm-step-' + id;
        li.style.cssText = 'display:flex;align-items:center;gap:10px;font-size:13px;padding:6px 0;border-bottom:1px solid #1a2235;';
        li.innerHTML =
          `<span id="wm-step-icon-${id}" style="font-size:15px;width:18px;text-align:center;flex-shrink:0;color:${colors[state]}">${icons[state]}</span>` +
          `<span style="flex:1;color:#e8f0f8;">${label}</span>` +
          `<span id="wm-step-detail-${id}" style="font-size:11px;color:#7a90b8;"></span>`;
        dlg.querySelector('#wmStepsList').appendChild(li);
      };

      const setStep = (id, state, detail = '') => {
        const icons  = { pending:'○', running:'⟳', done:'✓', failed:'✗', skipped:'–' };
        const colors = { pending:'#7a90b8', running:'#6aa0ff', done:'#50c878', failed:'#ff6b6b', skipped:'#555' };
        const iconEl   = dlg.querySelector('#wm-step-icon-' + id);
        const detailEl = dlg.querySelector('#wm-step-detail-' + id);
        if (iconEl)   { iconEl.textContent = icons[state]; iconEl.style.color = colors[state]; }
        if (detailEl && detail) detailEl.textContent = detail;
      };

      const payload = folderRows.map(r => ({
        filename: r.filename,
        rating: getRating(r),
        culled: getRawCullStatus(r),
        culled_origin: normalizeCullOrigin(r),
        species: r.species || '',
        family: r.family || '',
        quality: r.quality != null ? r.quality : null,
      }));

      dlg.querySelector('#wmCancel').addEventListener('click', closeAndRemove);

      // Read the current field selection out of the dialog and persist it so
      // the next write defaults to whatever the user picked last.
      const collectFieldSelection = () => {
        const sel = { ...xmpFieldDefaults };
        dlg.querySelectorAll('input[data-xmp-field]').forEach(cb => {
          sel[cb.dataset.xmpField] = !!cb.checked;
        });
        return sel;
      };
      const persistFieldSelection = (sel) => {
        try {
          const cur = loadSettings();
          cur.xmp_fields = sel;
          saveSettings(cur);
          if (window.pywebview?.api?.save_settings_data) {
            window.pywebview.api.save_settings_data({ xmp_fields: sel }).catch(() => {});
          }
        } catch (_) {}
      };

      dlg.querySelector('#wmOk').addEventListener('click', async () => {
        const fieldSelection = collectFieldSelection();
        persistFieldSelection(fieldSelection);
        showView('wmProgressView');
        addStep('write', 'Writing XMP sidecar files', 'running');
        try {
          const res = await window.pywebview.api.write_xmp_metadata(rootPath, payload, false, false, fieldSelection);
          if (!res.success) {
            setStep('write', 'failed', res.error || 'Unknown error');
            dlg.querySelector('#wmProgressActions').style.display = 'flex';
            return;
          }
          if (res.skipped_conflicts && res.skipped_conflicts.length > 0) {
            const n = res.skipped_conflicts.length;
            setStep('write', 'done', `${res.written} written, ${n} conflict${n === 1 ? '' : 's'}`);
            dlg.querySelector('#wmConflictDesc').textContent =
              `${n} existing XMP file${n === 1 ? '' : 's'} appear to have been created by another application (such as Lightroom or darktable). Overwriting them may interfere with metadata managed by that software.`;
            const conflictList = dlg.querySelector('#wmConflictList');
            res.skipped_conflicts.slice(0, 10).forEach(f => {
              const li = document.createElement('li');
              li.style.cssText = 'padding:5px 8px;border-bottom:1px solid #1a2235;';
              li.textContent = f;
              conflictList.appendChild(li);
            });
            if (res.skipped_conflicts.length > 10) {
              const li = document.createElement('li');
              li.style.cssText = 'padding:5px 8px;color:#7a90b8;';
              li.textContent = `\u2026and ${res.skipped_conflicts.length - 10} more`;
              conflictList.appendChild(li);
            }
            showView('wmConflictView');

            dlg.querySelector('#wmSkip').addEventListener('click', () => {
              showToast(`Metadata written: ${res.written} written, ${n} skipped`, 4000);
              closeAndRemove();
            });
            dlg.querySelector('#wmOverwrite').addEventListener('click', async () => {
              showView('wmProgressView');
              addStep('overwrite', 'Overwriting conflicting XMP files', 'running');
              try {
                const res2 = await window.pywebview.api.write_xmp_metadata(rootPath, payload, true, false, fieldSelection);
                if (!res2.success) {
                  setStep('overwrite', 'failed', res2.error || 'Unknown error');
                } else {
                  setStep('overwrite', 'done', `${res2.written} file${res2.written === 1 ? '' : 's'} written`);
                  showToast(`Metadata written: ${res2.written} file${res2.written === 1 ? '' : 's'}`, 4000);
                }
              } catch (e) {
                setStep('overwrite', 'failed', 'Error overwriting');
              }
              dlg.querySelector('#wmProgressActions').style.display = 'flex';
            });
          } else {
            setStep('write', 'done', `${res.written} file${res.written === 1 ? '' : 's'} written`);
            showToast(`Metadata written: ${res.written} file${res.written === 1 ? '' : 's'}`, 4000);
            dlg.querySelector('#wmProgressActions').style.display = 'flex';
          }
        } catch (e) {
          console.error('writeMetadataForFolder error', e);
          setStep('write', 'failed', 'Unexpected error');
          dlg.querySelector('#wmProgressActions').style.display = 'flex';
        }
      });

      dlg.querySelector('#wmDone').addEventListener('click', closeAndRemove);
      dlg.addEventListener('close', () => { if (dlg.parentNode) dlg.parentNode.removeChild(dlg); });
      dlg.showModal();
    }

    // Reload current folders (called from Python via evaluate_js after culling completes)
    async function reloadCurrentFolders() {
      const loadedPaths = [...new Set(rows.map(r => r.__rootPath).filter(Boolean))];
      if (loadedPaths.length === 0) return;
      if (loadedPaths.length === 1) {
        await loadFolderFromPath(loadedPaths[0]);
      } else {
        await loadMultipleFolders(loadedPaths);
      }
    }
    // Expose globally for evaluate_js calls from Python
    window.reloadCurrentFolders = reloadCurrentFolders;

    // Periodically broadcast queue running state to window (for beforeunload guard)
    setInterval(async () => {
      try {
        if (hasPywebviewApi && window.pywebview?.api?.is_analysis_running) {
          const r = await window.pywebview.api.is_analysis_running();
          window.__queueRunning = !!(r && r.running);
        }
      } catch (_) { }
    }, 3000);

    // 👁 Live Analysis button
    const queueLiveBtn = document.getElementById('queueLiveBtn');
    if (queueLiveBtn) {
      queueLiveBtn.addEventListener('click', openLiveAnalysisDlg);
    }

    // Live dialog close button + Escape handling
    const liveDlgClose = document.getElementById('liveDlgClose');
    if (liveDlgClose) {
      liveDlgClose.addEventListener('click', () => {
        _liveAnalysisDlgOpen = false;
        document.getElementById('liveAnalysisDlg').close();
      });
    }
    const liveAnalysisDlg = document.getElementById('liveAnalysisDlg');
    if (liveAnalysisDlg) {
      liveAnalysisDlg.addEventListener('close', () => { _liveAnalysisDlgOpen = false; });
    }

    // ====================================================================
    // Tutorial System  (Basics = short onboarding, Advanced = full tour)
    // Interactive: some steps require user action before advancing.
    // Engine supports: waitFor, waitForChain, inDialog, setupAction,
    // highlightAlso[], customBody, loadSamplesOnNext, inlineFooter.
    // ====================================================================

    const _tutWelcomeStep = {
      title: 'Welcome to Project Kestrel!',
      body: 'Project Kestrel uses machine learning to organize your bird photography \u2014 helping you review photos more efficiently, search through your library, and quickly find the ones you want to edit and share.<br><br>Click <b>Next</b> and we\u2019ll auto-load some <b>sample bird photos</b> so you can see Kestrel in action.',
      target: null,
      loadSamplesOnNext: true,
    };

    const _tutWorkflowStep = {
      title: 'Fit Kestrel into your workflow',
      body: '',
      target: '.write-metadata-btn',
      position: 'bottom',
      customBody: 'workflowCard',
      highlightAlso: ['.culling-assistant-btn'],
    };

    const _tutTryYourOwnStepBase = {
      title: 'Now try it with your own photos',
      body: 'You\u2019re ready!<br><br>\u2022 Click <b>Analyze Folders\u2026</b> to process a new folder of photos.<br>\u2022 Click <b>Open Folder\u2026</b> (or drop into the Folder Tree) to browse photos Kestrel has already analyzed.<br><br><b>Tip:</b> open a parent folder once to load your whole library \u2014 then search across every outing, every year.',
      target: '#analyzeQueueBtn',
      position: 'right',
      highlightAlso: ['#pickFolder'],
    };

    const TUTORIAL_BASICS = [
      _tutWelcomeStep,
      {
        title: 'Your photos, organized by scene',
        body: 'Kestrel organizes your photos into <b>scenes</b> \u2014 groups of similar images captured in the same burst. The scene grid shows these scenes in the order they were taken.',
        nudge: 'Click on a scene to open it!',
        target: '#sceneGrid .card',
        position: 'right',
        waitFor: 'clickScene',
      },
      {
        title: 'Explore your scene',
        body: 'Within each scene, your photos are automatically <b>sorted by quality</b> \u2014 from sharpest to blurriest. Focus your attention on the best shots first.<br><br>Click on a photo in the filmstrip to view its details.',
        nudge: 'Click a photo in the filmstrip below!',
        target: '#imageGrid',
        position: 'top',
        inDialog: true,
        waitFor: 'clickFilmstrip',
      },
      {
        title: 'Make a culling decision, then close the scene',
        body: 'Kestrel computes <b>star ratings</b> based on each image\u2019s quality score. Click the stars to set your own. <span style="color:#6aa0ff">Blue stars</span> = AI rating \u00b7 <span style="color:#f5c542">Gold stars</span> = your manual override.<br><br>Use the <b>Accept \u00b7 Undecided \u00b7 Reject</b> buttons to make a culling decision. These decisions power the Culling Assistant later!<br><br>When you\u2019re done, close the scene to continue.',
        nudge: 'Mark a photo Accept/Reject, then close the scene.',
        target: '#sceneInfoBar',
        position: 'top-left',
        inDialog: true,
        waitForChain: ['clickCullToggle', 'closeDialog'],
      },
      {
        title: 'Search and filter',
        body: 'Use the <b>Filter &amp; Sort</b> panel to narrow your scenes down:<br><br>\u2022 <b>Search</b> by bird species or family \u2014 the grid filters instantly.<br>\u2022 <b>Sort</b> by Capture Time, Quality, or Image Count.<br>\u2022 Toggle <b>Group by folder</b> / <b>capture time</b> to reorganize.<br><br>You can always tweak further in the options below.',
        target: '.filter-panel',
        position: 'right',
      },
      _tutWorkflowStep,
      {
        title: 'Which photo editor do you use?',
        body: 'Pick your preferred photo editor below. Kestrel will use it when you press <b>Space</b> or click the <b>Open</b> button (top-right of the scene info bar) to launch a photo.',
        target: null,
        customBody: 'editorPicker',
      },
      Object.assign({}, _tutTryYourOwnStepBase, {
        inlineFooter: 'advancedLink',
      }),
    ];

    const TUTORIAL_ADVANCED = [
      _tutWelcomeStep,
      {
        title: 'Your photos, organized by scene',
        body: 'Kestrel organizes your photos into <b>scenes</b> \u2014 groups of similar images captured in the same burst. The scene grid shows these scenes in the order they were taken.',
        nudge: 'Click on a scene to open it!',
        target: '#sceneGrid .card',
        position: 'right',
        waitFor: 'clickScene',
      },
      {
        title: 'Explore your scene',
        body: 'Within each scene, your photos are automatically <b>sorted by quality</b> \u2014 from sharpest to blurriest. Click on a photo in the filmstrip to view its details.',
        nudge: 'Click a photo in the filmstrip below!',
        target: '#imageGrid',
        position: 'top',
        inDialog: true,
        waitFor: 'clickFilmstrip',
      },
      {
        title: 'Ratings and culling decisions',
        body: 'Kestrel computes <b>star ratings</b> based on each image\u2019s quality score. Click the stars to set your own. <span style="color:#6aa0ff">Blue stars</span> = AI rating \u00b7 <span style="color:#f5c542">Gold stars</span> = your manual override.<br><br>Use the <b>Accept \u00b7 Undecided \u00b7 Reject</b> buttons to make a culling decision for each photo. These come in handy with the Culling Assistant later!',
        nudge: 'Mark a photo as Accepted or Rejected to continue!',
        target: '#sceneInfoBar',
        position: 'top-left',
        inDialog: true,
        waitFor: 'clickCullToggle',
      },
      {
        title: 'Multiple birds in a scene? Switch crops.',
        body: 'When a scene has more than one bird, the <b>\u25c2 / \u25b8 crop buttons</b> appear next to the filename. Click them to cycle through each bird crop.<br><br>Equivalent keyboard shortcuts:<br>\u2022 <kbd>\u2191</kbd> / <kbd>\u2193</kbd> \u2014 previous / next crop<br>\u2022 <kbd>Enter</kbd> \u2014 promote the active crop as the scene\u2019s primary bird<br><br>(If the current sample scene only has one bird, the crop buttons stay hidden.)',
        target: '#sceneInfoCropNav',
        position: 'top',
        inDialog: true,
      },
      {
        title: 'Open in your photo editor',
        body: 'Click this <b>Open</b> button (or press <kbd>Space</kbd>) to launch the original photo in your chosen photo editor. You can change which editor Kestrel uses in <b>Settings</b>.',
        target: '#sceneInfoEditorBtn',
        position: 'top-left',
        inDialog: true,
      },
      {
        title: 'RAW zoom',
        body: 'Want to pixel-peep the RAW? <b>Click and hold</b> on the full image to load the original RAW file and zoom in at the cursor. <b>Scroll</b> while holding to change zoom level, or use the <b>RAW Zoom</b> slider to set a default.<br><br>Release to return to the normal preview.',
        target: '#sceneZoomWrap',
        position: 'top',
        inDialog: true,
      },
      {
        title: 'Keyboard shortcuts',
        body: 'Kestrel has keyboard shortcuts to make reviewing photos faster. The shortcuts are listed above \u2014 try some out before continuing!',
        target: '#sceneShortcutLegend',
        position: 'bottom',
        inDialog: true,
        setupAction: 'expandShortcuts',
      },
      {
        title: 'A few more scene features',
        body: 'A few more things you can do once you\u2019re browsing your <b>own photos</b> (some won\u2019t work on the sample images):<br><br>\u2022 Edit the <b>scene name</b> and <b>tags</b> at the top<br>\u2022 Use <b>\u2702 Split Scene</b> if Kestrel accidentally merged two different scenes<br>\u2022 <b>Copy</b> (clipboard) the full image or bird crop straight from the preview<br><br>Close the scene dialog to continue.',
        nudge: 'Close the scene dialog to continue.',
        target: '#closeDlg',
        position: 'bottom',
        inDialog: true,
        waitFor: 'closeDialog',
      },
      {
        title: 'Filtering options',
        body: '\u2022 <b>Search</b> for any bird species or family \u2014 the grid filters instantly as you type.<br>\u2022 Don\u2019t see scenes after searching? Lower the <b>Confidence</b> threshold to see more results.<br>\u2022 Enable <b>Multi-subject mode</b> if your scenes contain multiple species.<br>\u2022 <b>Sort</b> by Capture Time, Quality, Scene ID, or Image Count.<br>\u2022 Toggle <b>Group by folder</b> / <b>capture time</b> to reorganize the grid.',
        target: '.filter-panel',
        position: 'right',
      },
      {
        title: 'Merging scenes',
        body: 'The two highlighted scenes above were actually one continuous burst that Kestrel split in two. Hold <kbd>Ctrl</kbd> and click both cards to select them, then click <b>Merge selected scenes</b> to combine them back into one.<br><br>You can also <kbd>Shift</kbd>+click to range-select a group of scenes at once.',
        target: '#sceneGrid .card:nth-child(2)',
        highlightAlso: ['#sceneGrid .card:nth-child(1)'],
        position: 'bottom',
      },
      {
        title: 'Search across your whole library',
        body: 'Kestrel searches across <b>every loaded folder</b> by species or family.<br><br><b>Tip:</b> open a <b>parent folder</b> once (via <b>Open Folder\u2026</b> or the Folder Tree) and Kestrel loads your entire library at once \u2014 then you can search across every outing and every year without re-loading anything.',
        target: '#search',
        position: 'right',
      },
      _tutWorkflowStep,
      {
        title: 'Write Photo Metadata',
        body: 'Click <b>Write Photo Metadata</b> to export Kestrel\u2019s star ratings and Accept/Reject decisions into XMP sidecar files alongside your photos. These <code>.xmp</code> files are understood natively by <b>Adobe Lightroom</b>, <b>darktable</b>, <b>Capture One</b>, and other editors.<br><br>\u26a0\ufe0f <b>Write photo metadata <em>before</em> importing into your photo editor</b> \u2014 most catalogues ignore new sidecar files once a photo is already imported. If a sidecar was already created by another application, Kestrel will ask before overwriting it.',
        target: '.write-metadata-btn',
        position: 'bottom',
      },
      {
        title: 'Culling Assistant',
        body: 'The <b>Culling Assistant</b> helps you automatically assign photos as Accepted or Rejected based on star ratings \u2014 and can even move rejected photos into a dedicated folder.<br><br>Click <b>Open Culling Assistant</b> to open a dedicated Accept/Reject workspace for the folder.',
        target: '.culling-assistant-btn',
        position: 'bottom',
      },
      {
        title: 'Options',
        body: 'Click <b>Settings</b> to choose your preferred <b>photo editor</b> (Lightroom, Darktable, or system default). Opening a photo with <kbd>Space</kbd> will launch it there. You can also tweak several other options \u2014 including the experimental <b>wildlife mode</b> that detects non-bird wildlife.',
        target: '#openSettings',
        position: 'bottom',
      },
      {
        title: 'You\u2019re all set!',
        body: 'That\u2019s the full tour! Quick recap:<br><br>\u2022 <b>Analyze Folders</b> to process new photos<br>\u2022 <b>Open Folder</b> to browse analyzed photos<br>\u2022 <b>Click scenes</b> to view &amp; rate photos<br>\u2022 <b>Culling Assistant</b> for bulk Accept/Reject workflow<br>\u2022 <b>Write Photo Metadata</b> to export to Lightroom, darktable, etc.<br><br>\u26a0\ufe0f <b>Remember:</b> Write photo metadata <em>before</em> importing into Lightroom or Capture One for best results!<br><br>Click the <b>\uD83D\uDCD6 Tutorial</b> button anytime to replay this tour. Happy birding!',
        target: null,
      },
      {
        title: 'Please send feedback!',
        body: 'I (the person who made Project Kestrel) would really love to hear from you! Please tell me if you found the app useful, or if you find any bugs or have suggestions for improvements.<br><br>Thank you for trying Kestrel!',
        target: '#openFeedback',
        position: 'top',
      },
      _tutTryYourOwnStepBase,
    ];

    let _tutStep = 0;
    let _tutSteps = [];
    let _tutBranch = '';             // '' = not started, 'basics', 'advanced'
    let _tutSampleLoaded = false;    // track if we auto-loaded sample sets
    let _tutCleanupFn = null;        // cleanup function for current waitFor listeners
    let _tutInDialog  = false;       // true while tutorial card is inside the scene dialog
    let _tutChainIdx  = 0;           // index into step.waitForChain

    function _tutEl(sel) { return document.querySelector(sel); }

    async function checkMainTutorialSeen() {
      if (!hasPywebviewApi) return true;
      try {
        var res = await window.pywebview.api.get_settings();
        return res && res.settings && res.settings.main_tutorial_seen === true;
      } catch (e) { return false; }
    }

    async function markMainTutorialSeen() {
      if (!hasPywebviewApi) return;
      try {
        var res = await window.pywebview.api.get_settings();
        var s = (res && res.success ? res.settings : {}) || {};
        s.main_tutorial_seen = true;
        await window.pywebview.api.save_settings_data(s);
      } catch (e) { console.warn('markMainTutorialSeen:', e); }
    }

    function _tutCleanup() {
      document.querySelectorAll('.highlight-target').forEach(function(el) {
        el.classList.remove('highlight-target');
      });
      if (_tutCleanupFn) { _tutCleanupFn(); _tutCleanupFn = null; }
      if (_tutInDialog) {
        _tutInDialog = false;
        var _ovl = _tutEl('#tutorialOverlay');
        var _crd = _tutEl('#tutorialCard');
        if (_crd && _ovl && _crd.parentElement !== _ovl) { _ovl.appendChild(_crd); }
        if (_ovl) { _ovl.style.display = ''; }
      }
    }

    function startMainTutorial(branch, fromStep) {
      _tutCleanup();
      // Backward-compat: legacy callers passed 1 or 2
      if (branch === 1) branch = 'basics';
      else if (branch === 2) branch = 'advanced';
      _tutBranch = (branch === 'advanced') ? 'advanced' : 'basics';
      _tutSteps = (_tutBranch === 'advanced') ? TUTORIAL_ADVANCED : TUTORIAL_BASICS;
      _tutStep = fromStep || 0;
      _tutChainIdx = 0;
      _tutEl('#tutorialOverlay').classList.add('active');
      _showMainTutStep(_tutStep);
    }

    function _closeMainTutorial() {
      _tutCleanup();
      _tutEl('#tutorialOverlay').classList.remove('active', 'has-backdrop');
      _tutEl('#tutorialHighlight').style.display = 'none';
      _tutEl('#tutorialNudge').style.display = 'none';
      if (_tutBranch) markMainTutorialSeen();
      _tutBranch = '';
    }

    // Install a single waitFor listener. Returns a cleanup fn that removes it.
    // onDone is called (with no args) when the gesture is detected.
    function _installTutWaitFor(key, onDone) {
      if (key === 'clickScene') {
        var sceneGridEl = document.getElementById('sceneGrid');
        if (!sceneGridEl) return function() {};
        var handler = function(ev) {
          var cardEl = ev.target.closest('.card');
          if (cardEl) setTimeout(onDone, 400);
        };
        sceneGridEl.addEventListener('click', handler, true);
        return function() { sceneGridEl.removeEventListener('click', handler, true); };
      }
      if (key === 'clickStar') {
        var onStarClick = function(ev) {
          var starEl = ev.target.closest('.star, .stars span');
          if (starEl) setTimeout(onDone, 300);
        };
        document.addEventListener('click', onStarClick, true);
        return function() { document.removeEventListener('click', onStarClick, true); };
      }
      if (key === 'clickFilmstrip') {
        var filmstripEl = document.getElementById('imageGrid');
        if (!filmstripEl) return function() {};
        var onFilmstripClick = function(ev) {
          var cardEl = ev.target.closest('.filmstrip-card, .card');
          if (cardEl) setTimeout(onDone, 350);
        };
        filmstripEl.addEventListener('click', onFilmstripClick, true);
        return function() { filmstripEl.removeEventListener('click', onFilmstripClick, true); };
      }
      if (key === 'clickCullToggle') {
        var onCullClick = function(ev) {
          var cullBtn = ev.target.closest('.cull-btn[data-cull="accept"], .cull-btn[data-cull="reject"]');
          if (cullBtn) setTimeout(onDone, 400);
        };
        document.addEventListener('click', onCullClick, true);
        return function() { document.removeEventListener('click', onCullClick, true); };
      }
      if (key === 'closeDialog') {
        var sceneDlgEl = document.getElementById('sceneDlg');
        if (!sceneDlgEl) return function() {};
        var onDlgClose = function() {
          sceneDlgEl.removeEventListener('close', onDlgClose);
          setTimeout(onDone, 250);
        };
        sceneDlgEl.addEventListener('close', onDlgClose);
        return function() { sceneDlgEl.removeEventListener('close', onDlgClose); };
      }
      return function() {};
    }

    function _renderTutWorkflowCard(bodyEl) {
      bodyEl.innerHTML =
        '<div class="tut-workflow-intro">Pick the workflow that fits how you already edit \u2014 Kestrel plugs into any of them.</div>' +
        '<div class="tut-workflow-tabs" role="tablist">' +
          '<button type="button" class="tut-wf-tab active" data-tab="none">No workflow changes</button>' +
          '<button type="button" class="tut-wf-tab" data-tab="cull">Cut the blurry bulk</button>' +
          '<button type="button" class="tut-wf-tab" data-tab="favs">Just my favorites</button>' +
        '</div>' +
        '<div class="tut-workflow-panels">' +
          '<div class="tut-wf-panel active" data-panel="none">' +
            '<div class="tut-wf-flow">' +
              '<div class="tut-wf-node">Your Photos</div>' +
              '<div class="tut-wf-arrow">\u2192</div>' +
              '<div class="tut-wf-node accent">Kestrel Analyzes</div>' +
              '<div class="tut-wf-arrow">\u2192</div>' +
              '<div class="tut-wf-node highlight">Write Metadata</div>' +
            '</div>' +
            '<div class="tut-wf-caption">Just export Kestrel\u2019s analysis as XMP sidecars, then browse them in the photo editor you already use.</div>' +
          '</div>' +
          '<div class="tut-wf-panel" data-panel="cull">' +
            '<div class="tut-wf-flow">' +
              '<div class="tut-wf-node">Your Photos</div>' +
              '<div class="tut-wf-arrow">\u2192</div>' +
              '<div class="tut-wf-node accent">Kestrel Analyzes</div>' +
              '<div class="tut-wf-arrow">\u2192</div>' +
              '<div class="tut-wf-node highlight">Culling Assistant</div>' +
              '<div class="tut-wf-arrow">\u2192</div>' +
              '<div class="tut-wf-node">Accepts / Rejects</div>' +
            '</div>' +
            '<div class="tut-wf-caption">Use the <b>Culling Assistant</b> to cut the blurry bulk \u2014 keep the sharp photos and archive the rest in one pass.</div>' +
          '</div>' +
          '<div class="tut-wf-panel" data-panel="favs">' +
            '<div class="tut-wf-flow">' +
              '<div class="tut-wf-node">Your Photos</div>' +
              '<div class="tut-wf-arrow">\u2192</div>' +
              '<div class="tut-wf-node accent">Kestrel Analyzes</div>' +
              '<div class="tut-wf-arrow">\u2192</div>' +
              '<div class="tut-wf-node">Pick Favorites</div>' +
              '<div class="tut-wf-arrow">\u2192</div>' +
              '<div class="tut-wf-node highlight">Open in Editor</div>' +
            '</div>' +
            '<div class="tut-wf-caption">Browse your scenes in Kestrel and press <kbd>Space</kbd> on the ones you love \u2014 they open straight in your photo editor.</div>' +
          '</div>' +
        '</div>' +
        '<div class="tut-workflow-hint">We\u2019ve highlighted the two buttons you\u2019ll use most: <b>Write Photo Metadata</b> and <b>Open Culling Assistant</b>.</div>';

      var tabs = bodyEl.querySelectorAll('.tut-wf-tab');
      var panels = bodyEl.querySelectorAll('.tut-wf-panel');
      tabs.forEach(function(tab) {
        tab.addEventListener('click', function() {
          var key = tab.getAttribute('data-tab');
          tabs.forEach(function(t) { t.classList.toggle('active', t === tab); });
          panels.forEach(function(p) { p.classList.toggle('active', p.getAttribute('data-panel') === key); });
        });
      });
    }

    function _renderTutEditorPicker(bodyEl, onChosen) {
      var intro = document.createElement('div');
      intro.className = 'tut-editor-intro';
      intro.innerHTML = 'Inside any scene, press <kbd>Space</kbd> or click the <b>Open</b> button (top-right of the info bar) to launch the original in your chosen editor.';
      bodyEl.innerHTML = '';
      bodyEl.appendChild(intro);

      var grid = document.createElement('div');
      grid.className = 'tut-editor-grid';
      var choices = [
        { key: 'lightroom',   label: 'Adobe Lightroom Classic' },
        { key: 'darktable',   label: 'Darktable' },
        { key: 'capture_one', label: 'Capture One' },
        { key: 'photoshop',   label: 'Adobe Photoshop' },
        { key: 'system',      label: 'System Default' },
        { key: '__other',     label: 'Other\u2026 (open Settings)' },
      ];
      choices.forEach(function(c) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'tut-editor-btn';
        b.setAttribute('data-key', c.key);
        b.innerHTML = '<span class="tut-editor-label">' + c.label + '</span>';
        b.addEventListener('click', function() { onChosen(c.key, b); });
        grid.appendChild(b);
      });
      bodyEl.appendChild(grid);

      var foot = document.createElement('div');
      foot.className = 'tut-editor-foot';
      foot.textContent = 'You can change this anytime in Settings.';
      bodyEl.appendChild(foot);
    }

    async function _applyChosenEditor(key) {
      try {
        var s = loadSettings();
        s.editor = key;
        saveSettings(s);
        if (hasPywebviewApi && window.pywebview?.api?.save_settings_data) {
          try { await window.pywebview.api.save_settings_data(s); } catch (_) {}
        }
      } catch (e) { console.warn('[tutorial] apply editor failed', e); }
    }

    function _showMainTutStep(idx) {
      _tutCleanup();
      var step = _tutSteps[idx];
      if (!step) { _closeMainTutorial(); return; }

      var overlay = _tutEl('#tutorialOverlay');
      var hl      = _tutEl('#tutorialHighlight');
      var card    = _tutEl('#tutorialCard');
      var nudge   = _tutEl('#tutorialNudge');
      var nextBtn = _tutEl('#tutorialNext');
      var bodyEl  = _tutEl('#tutorialBody');

      card.classList.remove('tut-card-workflow', 'tut-card-editor');

      if (step.setupAction === 'expandShortcuts') {
        var legend = document.getElementById('sceneShortcutLegend');
        if (legend && legend.classList.contains('hidden')) {
          var shortcutToggleBtn = document.getElementById('sceneShortcutBtn');
          if (shortcutToggleBtn) shortcutToggleBtn.click();
        }
      }

      _tutEl('#tutorialCounter').textContent = 'Step ' + (idx + 1) + ' of ' + _tutSteps.length;
      _tutEl('#tutorialTitle').innerHTML = step.title;

      if (step.customBody === 'workflowCard') {
        card.classList.add('tut-card-workflow');
        _renderTutWorkflowCard(bodyEl);
      } else if (step.customBody === 'editorPicker') {
        card.classList.add('tut-card-editor');
        _renderTutEditorPicker(bodyEl, async function(key, btnEl) {
          bodyEl.querySelectorAll('.tut-editor-btn').forEach(function(b) {
            b.classList.toggle('selected', b === btnEl);
            b.disabled = true;
          });
          if (key === '__other') {
            try { showSettings(); } catch (_) {}
            var dlg2 = document.getElementById('settingsDlg');
            var onClose = function() {
              if (dlg2) dlg2.removeEventListener('close', onClose);
              setTimeout(function() { _tutAdvance(); }, 250);
            };
            if (dlg2) dlg2.addEventListener('close', onClose);
            else setTimeout(function() { _tutAdvance(); }, 400);
          } else {
            await _applyChosenEditor(key);
            setTimeout(function() { _tutAdvance(); }, 350);
          }
        });
      } else {
        bodyEl.innerHTML = step.body || '';
      }

      // Optional inline footer link (e.g. Basics → Advanced)
      if (step.inlineFooter === 'advancedLink') {
        var foot = document.createElement('div');
        foot.className = 'tut-inline-foot';
        foot.innerHTML = 'Want the deep dive? <a href="#" id="tutStartAdvancedLink">Open the Advanced Tutorial</a>';
        bodyEl.appendChild(foot);
        var ln = foot.querySelector('#tutStartAdvancedLink');
        if (ln) ln.addEventListener('click', function(ev) {
          ev.preventDefault();
          _closeMainTutorial();
          setTimeout(function() { startMainTutorial('advanced', 0); }, 120);
        });
      }

      if (step.nudge) { nudge.textContent = step.nudge; nudge.style.display = ''; }
      else nudge.style.display = 'none';

      var dotsCont = _tutEl('#tutorialProgress');
      dotsCont.innerHTML = '';
      _tutSteps.forEach(function(_, i) {
        var d = document.createElement('div');
        d.className = 'tutorial-dot' + (i === idx ? ' active' : '');
        dotsCont.appendChild(d);
      });

      _tutEl('#tutorialBack').disabled = (idx === 0);
      var isLast = (idx === _tutSteps.length - 1);
      nextBtn.textContent = isLast ? 'Finish \u2713' : 'Next \u2192';

      var hasWaitFor = !!step.waitFor || !!(step.waitForChain && step.waitForChain.length);
      // customBody === 'editorPicker' manages its own advance via buttons.
      var customManagesAdvance = (step.customBody === 'editorPicker');
      nextBtn.style.display = (hasWaitFor || customManagesAdvance) ? 'none' : '';

      var target = step.target ? document.querySelector(step.target) : null;

      var _inDialogActive = false;
      if (step.inDialog) {
        var dlg = document.getElementById('sceneDlg');
        if (!dlg || !dlg.open) {
          bodyEl.insertAdjacentHTML('beforeend', '<br><br><span style="color:var(--brand);font-weight:600">Open a scene first, then this step will highlight the right element.</span>');
          nudge.style.display = 'none';
          nextBtn.style.display = '';  // show Next even if waitFor was set
          target = null;
          hasWaitFor = false;
        } else {
          _inDialogActive = true;
          _tutInDialog = true;
          if (card.parentElement !== dlg) { dlg.appendChild(card); }
          overlay.style.display = 'none';
        }
      }

      // Generalized multi-target highlight (replaces the old single highlightFirst)
      var extraTargets = [];
      if (step.highlightFirst) extraTargets.push(step.highlightFirst);
      if (Array.isArray(step.highlightAlso)) extraTargets = extraTargets.concat(step.highlightAlso);
      if (!_inDialogActive) {
        extraTargets.forEach(function(sel) {
          var el = document.querySelector(sel);
          if (el) el.classList.add('highlight-target');
        });
      }

      if (!target || (target.offsetWidth === 0 && target.offsetHeight === 0)) {
        hl.style.display = 'none';
        overlay.classList.add('has-backdrop');
        card.style.transform = 'translate(-50%, -50%)';
        card.style.top  = '50%';
        card.style.left = '50%';
      } else {
        hl.style.display = _inDialogActive ? 'none' : '';
        overlay.classList.remove('has-backdrop');
        card.style.transform = '';

        target.classList.add('highlight-target');

        var pad = 8;
        var r = target.getBoundingClientRect();
        if (!_inDialogActive) {
          hl.style.top    = (r.top  - pad) + 'px';
          hl.style.left   = (r.left - pad) + 'px';
          hl.style.width  = (r.width  + pad * 2) + 'px';
          hl.style.height = (r.height + pad * 2) + 'px';
        }

        var pos    = step.position || 'right';
        var margin = 18;
        var vw     = window.innerWidth;
        var vh     = window.innerHeight;
        var cw     = (card.offsetWidth || 380) + margin;
        var ch     = card.offsetHeight || 220;
        var topV, leftV;
        if (pos === 'right')        { leftV = r.right + margin;                  topV = r.top + r.height / 2 - ch / 2; }
        else if (pos === 'left')    { leftV = r.left - cw - margin;              topV = r.top + r.height / 2 - ch / 2; }
        else if (pos === 'bottom')  { leftV = r.left + r.width / 2 - cw / 2;     topV = r.bottom + margin; }
        else if (pos === 'top-left'){ leftV = r.left;                            topV = r.top - ch - margin; }
        else                         { leftV = r.left + r.width / 2 - cw / 2;    topV = r.top - ch - margin; } // 'top'
        leftV = Math.max(margin, Math.min(leftV, vw - cw - margin));
        topV  = Math.max(margin, Math.min(topV,  vh - ch - margin));
        card.style.left = leftV + 'px';
        card.style.top  = topV  + 'px';
      }

      // ---- Interactive waitFor / waitForChain ----
      if (hasWaitFor) {
        if (step.waitForChain && step.waitForChain.length) {
          _tutChainIdx = 0;
          var installChain;
          installChain = function() {
            if (_tutChainIdx >= step.waitForChain.length) { _tutAdvance(); return; }
            var key = step.waitForChain[_tutChainIdx];
            _tutCleanupFn = _installTutWaitFor(key, function() {
              if (_tutCleanupFn) { _tutCleanupFn(); _tutCleanupFn = null; }
              _tutChainIdx++;
              installChain();
            });
          };
          installChain();
        } else if (step.waitFor) {
          _tutCleanupFn = _installTutWaitFor(step.waitFor, function() { _tutAdvance(); });
        }
      }
    }

    async function _handleLoadSamplesOnNext() {
      var bodyEl = _tutEl('#tutorialBody');
      var nextBtn = _tutEl('#tutorialNext');
      var backBtn = _tutEl('#tutorialBack');
      var skipBtn = _tutEl('#tutorialSkip');
      nextBtn.disabled = true; backBtn.disabled = true; skipBtn.disabled = true;
      var loading = document.createElement('div');
      loading.className = 'tut-loading';
      loading.innerHTML = '<span class="tut-spinner"></span> Loading sample photos\u2026';
      bodyEl.appendChild(loading);
      try {
        await _autoLoadSamples();
      } finally {
        nextBtn.disabled = false; backBtn.disabled = false; skipBtn.disabled = false;
      }
      _tutStep++;
      _showMainTutStep(_tutStep);
    }

    function _tutAdvance() {
      var cur = _tutSteps[_tutStep];
      if (cur && cur.loadSamplesOnNext && !_tutSampleLoaded) {
        _handleLoadSamplesOnNext();
        return;
      }
      _tutStep++;
      if (_tutStep >= _tutSteps.length) {
        _closeMainTutorial();
      } else {
        _showMainTutStep(_tutStep);
      }
    }

    function _tutGoBack() {
      if (_tutStep > 0) { _tutStep--; _showMainTutStep(_tutStep); }
    }

    // Load sample photos (once per session). Returns when samples are ready.
    async function _autoLoadSamples() {
      if (_tutSampleLoaded) return;
      if (!hasPywebviewApi) { _tutSampleLoaded = true; return; }
      try {
        var res = await window.pywebview.api.get_sample_sets_paths();
        if (res && res.success && res.paths && res.paths.length > 0) {
          _tutSampleLoaded = true;
          var sampleParent = res.paths[0].replace(/[/\\][^/\\]+$/, '');
          try { await scanFolderTree(sampleParent); } catch (e) { console.warn('[tutorial] scanFolderTree:', e); }
          try { await loadMultipleFolders(res.paths); }
          catch (e) { console.warn('[tutorial] loadMultipleFolders:', e); }
          await new Promise(function(r) { setTimeout(r, 500); });
        } else {
          console.warn('[tutorial] No sample sets found');
        }
      } catch (e) {
        console.warn('[tutorial] _autoLoadSamples error:', e);
      }
    }

    // Chooser dialog: click Tutorial button (or Welcome Start Tutorial) to pick branch.
    function openTutorialChooser() {
      var dlg = document.getElementById('tutorialChooserDlg');
      if (!dlg) { startMainTutorial('basics', 0); return; }
      try { dlg.showModal(); } catch (_) { try { dlg.show(); } catch(__) {} }
    }
    // Expose on window so inline onclick handlers can reach it.
    window.openTutorialChooser = openTutorialChooser;
    window.startMainTutorial = startMainTutorial;

    var _tutChooserDlg = document.getElementById('tutorialChooserDlg');
    if (_tutChooserDlg) {
      var _chooseBasics = _tutChooserDlg.querySelector('#tutChooseBasics');
      var _chooseAdv    = _tutChooserDlg.querySelector('#tutChooseAdvanced');
      var _chooseCancel = _tutChooserDlg.querySelector('#tutChooseCancel');
      if (_chooseBasics) _chooseBasics.addEventListener('click', function() {
        try { _tutChooserDlg.close(); } catch (_) {}
        startMainTutorial('basics', 0);
      });
      if (_chooseAdv) _chooseAdv.addEventListener('click', function() {
        try { _tutChooserDlg.close(); } catch (_) {}
        startMainTutorial('advanced', 0);
      });
      if (_chooseCancel) _chooseCancel.addEventListener('click', function() {
        try { _tutChooserDlg.close(); } catch (_) {}
      });
    }

    var helpBtnMain = document.getElementById('helpBtnMain');
    if (helpBtnMain) {
      helpBtnMain.addEventListener('click', function() { openTutorialChooser(); });
    }

    _tutEl('#tutorialNext').addEventListener('click', _tutAdvance);
    _tutEl('#tutorialBack').addEventListener('click', _tutGoBack);
    _tutEl('#tutorialSkip').addEventListener('click', function() {
      _closeMainTutorial();
    });

    // Escape closes tutorial
    document.addEventListener('keydown', function(ev) {
      if (!_tutEl('#tutorialOverlay').classList.contains('active')) return;
      if (!_tutBranch) return;
      if (ev.key === 'Escape') { _closeMainTutorial(); }
    });

    // Welcome panel Tutorial link
    var welcomeTutLink = document.getElementById('welcomeTutorialLink');
    if (welcomeTutLink) {
      welcomeTutLink.addEventListener('click', function(e) {
        e.preventDefault();
        openTutorialChooser();
      });
    }

    // Auto-start tutorial on first launch (pywebview mode only) — Basics branch.
    (async function() {
      if (!hasPywebviewApi) return;
      await new Promise(function(r) { setTimeout(r, 800); });
      var seen = await checkMainTutorialSeen();
      if (!seen) {
        startMainTutorial('basics', 0);
      }
    })();

    // ====================================================================
    // Welcome panel: "What's New" banner + rotating tip carousel
    // ====================================================================

    // Bump this whenever you author a new changelog. Clients with a matching
    // `last_seen_whats_new_version` will not see the banner again.
    const WHATS_NEW = {
      version: 'nelsons-sparrow',
      headline: "New in v(Nelson\u2019s Sparrow) \u2014 resilient analysis, smarter shutdowns!",
      items: [
        'New <b>Resilient GPU pipeline</b> automatically falls back to CPU when your graphics driver hiccups, and a new <b>"Re-attempt analysis on errored images"</b> button re-runs just the failures without touching successful results.',
        'Faster and more accurate wildlife detection with the new <b>MegaDetector v1000-cedar</b> "Fast" model, plus a fix that <b>doubles SpeciesNet batch speed</b> by removing a duplicate inference pass.',
        'Kestrel now <b>tells the difference between a real crash and a normal PC shutdown</b>, so reboots and logoffs no longer trigger the alarming "did not shut down cleanly" dialog.',
        'New <b>quality normalization curve (v3)</b> rebalances star ratings across your library, and the <b>command-line interface</b> now exposes every Advanced Analysis Setting for power users and scripted workflows.',
      ],
    };

    const WELCOME_TIPS = [
      {
        icon: '\uD83E\uDD8B',
        title: 'Try wildlife mode',
        badge: 'New!',
        body: 'Kestrel can detect squirrels, bears, and other wildlife in addition to birds. Enable it in <b>Settings &rarr; Analysis</b>.',
        action: { label: 'Open Settings', onClick: function() { try { showSettings(); } catch (_) {} } },
      },
      {
        icon: '\u2795',
        title: 'Merge scenes',
        body: 'Hold <kbd>Ctrl</kbd> and click scene cards to select multiple, then click <b>Merge selected scenes</b> to combine them into one.',
      },
      {
        icon: '\uD83D\uDDBC\uFE0F',
        title: 'Multiple birds in one scene?',
        body: 'Use the <b>\u25C2 / \u25B8 crop buttons</b> in the scene info bar (or <kbd>\u2191</kbd> / <kbd>\u2193</kbd>) to flip through each bird, and press <kbd>Enter</kbd> to promote one as the scene\u2019s primary.',
      },
      {
        icon: '\uD83D\uDCC2',
        title: 'Open a parent folder once',
        body: 'Kestrel searches across <b>every loaded folder</b>. Open a parent folder and browse your entire library by species or family without re-loading anything.',
      },
      {
        icon: '\u2328\uFE0F',
        title: 'Space = open in editor',
        body: 'Press <kbd>Space</kbd> on any photo to open the original in your chosen photo editor. Set your preference in Settings.',
        action: { label: 'Open Settings', onClick: function() { try { showSettings(); } catch (_) {} } },
      },
      {
        icon: '\uD83D\uDDD1\uFE0F',
        title: 'Cull faster with the Culling Assistant',
        body: 'Batch Accept / Reject photos by your own quality rules \u2014 and optionally move rejects into an archive folder.',
      },
      {
        icon: '\u26A0\uFE0F',
        title: 'Write metadata before importing',
        body: '<b>Write Photo Metadata</b> before importing into Lightroom or Capture One \u2014 most catalogues ignore sidecar files added <i>after</i> import.',
      },
      {
        icon: '\u2B50',
        title: 'Not satisfied with the star ratings?',
        body: 'Tune the rating thresholds and profile in Settings. Your manual overrides are always saved as gold stars.',
        action: { label: 'Open Settings', onClick: function() { try { showSettings(); } catch (_) {} } },
      },
    ];

    (function setupWelcomeWhatsNew() {
      var banner = document.getElementById('welcomeWhatsNew');
      if (!banner) return;
      (async function() {
        var lastSeen = null;
        if (hasPywebviewApi) {
          try {
            var res = await window.pywebview.api.get_settings();
            lastSeen = res && res.settings && res.settings.last_seen_whats_new_version;
          } catch (_) {}
        }
        if (lastSeen === WHATS_NEW.version) return;
        var items = WHATS_NEW.items.map(function(it) {
          return '<li>' + it + '</li>';
        }).join('');
        banner.innerHTML =
          '<div class="wwn-head">' +
            '<span class="wwn-badge">New</span>' +
            '<span class="wwn-title">' + WHATS_NEW.headline + '</span>' +
            '<button type="button" class="wwn-dismiss" id="wwnDismiss" aria-label="Dismiss">\u2715</button>' +
          '</div>' +
          '<ul class="wwn-list">' + items + '</ul>';
        banner.classList.remove('hidden');
        var dismiss = banner.querySelector('#wwnDismiss');
        if (dismiss) dismiss.addEventListener('click', async function() {
          banner.classList.add('hidden');
          if (hasPywebviewApi) {
            try {
              var r2 = await window.pywebview.api.get_settings();
              var s = (r2 && r2.success ? r2.settings : {}) || {};
              s.last_seen_whats_new_version = WHATS_NEW.version;
              await window.pywebview.api.save_settings_data(s);
            } catch (_) {}
          }
        });
      })();
    })();

    (function setupWelcomeTipCarousel() {
      var root = document.getElementById('welcomeTipCarousel');
      if (!root || !WELCOME_TIPS.length) return;
      var iconEl   = root.querySelector('#wtcIcon');
      var titleEl  = root.querySelector('#wtcTitle');
      var badgeEl  = root.querySelector('#wtcBadge');
      var bodyEl   = root.querySelector('#wtcBody');
      var actionEl = root.querySelector('#wtcAction');
      var dotsEl   = root.querySelector('#wtcDots');
      var cardEl   = root.querySelector('#wtcCard');
      var prevBtn  = root.querySelector('.wtc-prev');
      var nextBtn  = root.querySelector('.wtc-next');

      var idx = 0;
      var paused = false;
      var rotateTimer = null;

      dotsEl.innerHTML = '';
      WELCOME_TIPS.forEach(function(_, i) {
        var d = document.createElement('button');
        d.type = 'button';
        d.className = 'wtc-dot' + (i === 0 ? ' active' : '');
        d.setAttribute('aria-label', 'Tip ' + (i + 1));
        d.addEventListener('click', function() { goTo(i, true); });
        dotsEl.appendChild(d);
      });

      function render(i) {
        var t = WELCOME_TIPS[i];
        if (!t) return;
        iconEl.textContent = t.icon || '\uD83D\uDCA1';
        titleEl.innerHTML = t.title || '';
        if (t.badge) { badgeEl.textContent = t.badge; badgeEl.classList.remove('hidden'); }
        else badgeEl.classList.add('hidden');
        bodyEl.innerHTML = t.body || '';
        if (t.action && t.action.label) {
          actionEl.innerHTML = '';
          var btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'wtc-link';
          btn.textContent = t.action.label;
          btn.addEventListener('click', function(ev) { ev.preventDefault(); if (typeof t.action.onClick === 'function') t.action.onClick(); });
          actionEl.appendChild(btn);
          actionEl.classList.remove('hidden');
        } else {
          actionEl.classList.add('hidden');
          actionEl.innerHTML = '';
        }
        Array.prototype.forEach.call(dotsEl.children, function(d, di) {
          d.classList.toggle('active', di === i);
        });
      }

      function goTo(i, manual) {
        idx = (i + WELCOME_TIPS.length) % WELCOME_TIPS.length;
        cardEl.classList.remove('wtc-in');
        cardEl.classList.add('wtc-out');
        setTimeout(function() {
          render(idx);
          cardEl.classList.remove('wtc-out');
          cardEl.classList.add('wtc-in');
        }, 180);
        if (manual) resetTimer();
      }

      function next() { goTo(idx + 1, false); }
      function prev() { goTo(idx - 1, false); }

      function resetTimer() {
        if (rotateTimer) clearInterval(rotateTimer);
        rotateTimer = setInterval(function() { if (!paused) next(); }, 7000);
      }

      prevBtn.addEventListener('click', function() { goTo(idx - 1, true); });
      nextBtn.addEventListener('click', function() { goTo(idx + 1, true); });
      root.addEventListener('mouseenter', function() { paused = true; });
      root.addEventListener('mouseleave', function() { paused = false; });

      render(0);
      resetTimer();
    })();


    // Wire donation dialog buttons — this script runs after the dialog HTML is in the DOM
    (function() {
      var dlg = document.getElementById('donateDlg');
      document.getElementById('donateDlgGoBtn').addEventListener('click', function() {
        dlg.close();
        openDonateLink();
      });
      document.getElementById('donateDlgFeedbackBtn').addEventListener('click', function() {
        dlg.close();
        setTimeout(function() {
          document.getElementById('feedbackDlg').showModal();
        }, 150);
      });
      document.getElementById('donateDlgClose').addEventListener('click', function() {
        dlg.close();
      });
    })();
