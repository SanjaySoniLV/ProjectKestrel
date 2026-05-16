    // Threshold below which a stored Kestrel JWT is considered "too short to
    // survive a normal restart cycle". We refuse to store anything < 1 hour
    // on the sign-in side (desktop-signin/index.html), so seeing a TTL under
    // this threshold here means either the dashboard template was downgraded
    // after the token was minted, or the desktop-signin protection was
    // bypassed somehow — either way the user should know loudly.
    const AUTH_SHORT_TTL_SEC = 3600;

    function _decodeJwtExpUnverified(token) {
      try {
        const parts = String(token || '').split('.');
        if (parts.length < 2) return null;
        const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
        const pad = b64.length % 4 ? '='.repeat(4 - (b64.length % 4)) : '';
        const payload = JSON.parse(atob(b64 + pad));
        return typeof payload.exp === 'number' ? payload.exp : null;
      } catch (_) { return null; }
    }

    function warnIfShortTokenTtl(ttlSec, source) {
      if (!(ttlSec > 0) || ttlSec >= AUTH_SHORT_TTL_SEC) return false;
      const msg = `Kestrel auth token has TTL ${Math.round(ttlSec)}s ` +
        `(expected ≥ ${AUTH_SHORT_TTL_SEC}s). The 'kestrel_api' Clerk JWT ` +
        `template is likely misconfigured — your sign-in will not survive ` +
        `the next app restart. Please contact support.`;
      console.error('[auth]', msg, `(source=${source})`);
      try {
        if (typeof showToast === 'function') {
          showToast(`⚠ Short auth token TTL (${Math.round(ttlSec)}s). Sign-in will not persist — see console.`, 30000);
        }
      } catch (_) {}
      const accountBtn = el('#accountBtn');
      if (accountBtn) {
        accountBtn.classList.add('short-ttl-warning');
        accountBtn.title = `Perch — Warning: token TTL ${Math.round(ttlSec)}s (will not survive restart)`;
      }
      return true;
    }

    // Perch Authentication Handler
    async function initPerchAuth() {
      const accountBtn = el('#accountBtn');
      if (!accountBtn) return;

      // Wait for the pywebview JS↔Py bridge to be ready before doing any API
      // call. Without this, on cold starts `hasPywebviewApi` is still false
      // at the moment initPerchAuth() runs (the IIFE that flips it to true
      // is racing with us), so every API check below silently no-ops.
      try {
        if (!hasPywebviewApi) {
          await waitForPywebview();
        }
      } catch (_) { /* fall through and try anyway */ }

      // Token validity drives the indicator. The /v1/me call only ENRICHES
      // with the user's handle — if it fails (network blip, transient 401),
      // we still want the indicator on so the user knows their token is good.
      let signedIn = false;
      let expired = false;          // token stored but past exp
      let handle = null;
      let displayName = null;

      if (window.pywebview?.api?.get_auth_token) {
        try {
          const result = await window.pywebview.api.get_auth_token();
          if (result?.token) {
            _perchToken = result.token;
            signedIn = true;
            const exp = Number(result.expiry) || _decodeJwtExpUnverified(result.token);
            if (exp) {
              warnIfShortTokenTtl(exp - Date.now() / 1000, 'startup');
            }
          }
        } catch (e) {
          console.warn('Failed to get auth token on startup:', e);
        }
      }

      // Only fetch the handle if we already know the token is good. Failure
      // here doesn't roll back the signed-in state — we just don't show the
      // handle. If the worker explicitly says the token is expired, that
      // overrides the local-only check.
      if (signedIn && window.pywebview?.api?.get_perch_account) {
        try {
          const accountRes = await window.pywebview.api.get_perch_account();
          if (accountRes?.success && accountRes.account) {
            const a = accountRes.account;
            handle = a.username || a.userId || null;
            displayName = a.displayName || a.display_name || a.first_name || null;
          } else if (accountRes?.error === 'auth_token_expired') {
            expired = true;
            signedIn = false;
          }
        } catch (e) {
          console.warn('Failed to get Perch account info:', e);
        }
      }

      const labelEl = el('#accountBtnLabel');
      if (signedIn) {
        accountBtn.classList.add('signed-in');
        accountBtn.classList.remove('session-expired');
        const label = handle ? `@${handle}` : (displayName || 'Signed in');
        accountBtn.title = `Perch — Signed in as ${label}`;
        accountBtn.setAttribute('aria-label', `Perch account: signed in as ${label}`);
        if (labelEl) {
          labelEl.textContent = label;
          labelEl.classList.remove('hidden');
        }
      } else if (expired) {
        accountBtn.classList.remove('signed-in');
        accountBtn.classList.add('session-expired');
        accountBtn.title = 'Perch — Session expired, click to sign in again';
        accountBtn.setAttribute('aria-label', 'Perch account: session expired, click to sign in again');
        if (labelEl) {
          labelEl.textContent = 'Sign in';
          labelEl.classList.remove('hidden');
        }
      } else {
        if (labelEl) {
          labelEl.textContent = '';
          labelEl.classList.add('hidden');
        }
      }

      accountBtn.addEventListener('click', () => {
        const signInUrl = `${MYACCOUNT_ORIGIN}/desktop-signin`;
        if (hasPywebviewApi && window.pywebview?.api?.open_auth_sign_in) {
          window.pywebview.api.open_auth_sign_in(signInUrl);
        } else {
          // Fallback: open web sign-in in browser
          window.open(`${MYACCOUNT_ORIGIN}/signin`, '_blank');
        }
      });

      // Fire-and-forget silent refresh on every app launch. Python skips
      // this entirely if no token is stored; otherwise it spins up a hidden
      // pywebview window that reuses Clerk's persisted session cookie to
      // mint a fresh JWT, then calls back into store_auth_token (which in
      // turn invokes window.onAuthSignIn to re-hydrate this UI).
      if (window.pywebview?.api?.refresh_auth_token_silently) {
        try { window.pywebview.api.refresh_auth_token_silently(); }
        catch (e) { console.warn('refresh_auth_token_silently failed:', e); }
      }
    }

    // Called by Python after store_auth_token completes
    window.onAuthSignIn = async (token) => {
      _perchToken = token;
      const accountBtn = el('#accountBtn');
      const labelEl = el('#accountBtnLabel');
      if (accountBtn) {
        accountBtn.classList.add('signed-in');
        accountBtn.classList.remove('session-expired');
        // Clear any stale short-TTL warning; warnIfShortTokenTtl below
        // re-adds it if the new token is also short-lived.
        accountBtn.classList.remove('short-ttl-warning');
      }
      if (labelEl) {
        labelEl.textContent = 'Signed in';
        labelEl.classList.remove('hidden');
      }

      // Catches the case where desktop-signin's TTL guard was bypassed (e.g.,
      // an older deployed version of the page) or the Clerk template config
      // was downgraded after a previous successful sign-in.
      const exp = _decodeJwtExpUnverified(token);
      if (exp) warnIfShortTokenTtl(exp - Date.now() / 1000, 'onAuthSignIn');
      // Fetch account info so the handle shows up on the button without
      // requiring a Kestrel restart. The cache was just cleared in
      // store_auth_token, so this hits the network and gets fresh data.
      try {
        if (window.pywebview?.api?.get_perch_account) {
          const accountRes = await window.pywebview.api.get_perch_account();
          if (accountRes?.success && accountRes.account && accountBtn) {
            const a = accountRes.account;
            const handle = a.username || null;
            const displayName = a.displayName || a.display_name || a.first_name || null;
            const label = handle ? `@${handle}` : (displayName || 'Signed in');
            accountBtn.title = `Perch — Signed in as ${label}`;
            accountBtn.setAttribute('aria-label', `Perch account: signed in as ${label}`);
            if (labelEl) labelEl.textContent = label;
          }
        }
      } catch (e) { /* ignore — indicator is on regardless */ }
    };

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

