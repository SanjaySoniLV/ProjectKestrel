    // Render the account button's display label from a /v1/me response.
    // Preference order: "First Last" > "First" > displayName > "@username" >
    // generic "Signed in" fallback. Same logic is used by both the startup
    // hydration path and the post-sign-in onAuthSignIn handler so the UI is
    // consistent across both entry points.
    function _accountDisplayLabel(account) {
      if (!account) return 'Signed in';
      const firstName = (account.firstName || account.first_name || '').trim();
      const lastName  = (account.lastName  || account.last_name  || '').trim();
      if (firstName && lastName) return `${firstName} ${lastName}`;
      if (firstName) return firstName;
      const displayName = (account.displayName || account.display_name || '').trim();
      if (displayName) return displayName;
      const handle = account.username || null;
      if (handle) return `@${handle}`;
      return 'Signed in';
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

      if (window.pywebview?.api?.get_auth_token) {
        try {
          const result = await window.pywebview.api.get_auth_token();
          if (result?.token) {
            _perchToken = result.token;
            signedIn = true;
          }
        } catch (e) {
          console.warn('Failed to get auth token on startup:', e);
        }
      }

      // Only fetch the handle if we already know the token is good. Failure
      // here doesn't roll back the signed-in state — we just don't show the
      // handle. If the worker explicitly says the token is expired, that
      // overrides the local-only check.
      let accountObj = null;
      if (signedIn && window.pywebview?.api?.get_perch_account) {
        try {
          const accountRes = await window.pywebview.api.get_perch_account();
          if (accountRes?.success && accountRes.account) {
            accountObj = accountRes.account;
          } else if (accountRes?.error === 'auth_token_expired') {
            expired = true;
            signedIn = false;
          }
        } catch (e) {
          console.warn('Failed to get Perch account info:', e);
        }
      }

      // Cold-start retry: on Windows, the keyring read backing
      // get_perch_account can momentarily race the OS Credential Manager
      // warming up, so the first call after launch sometimes returns
      // success:false (or no account). Without this retry the indicator
      // sticks on the generic "Signed in" label until the next sign-in or
      // app restart, even though the token is valid. Try once more after a
      // short delay before painting the label.
      if (signedIn && !accountObj && window.pywebview?.api?.get_perch_account) {
        await new Promise(r => setTimeout(r, 1500));
        try {
          const accountRes = await window.pywebview.api.get_perch_account();
          if (accountRes?.success && accountRes.account) {
            accountObj = accountRes.account;
          } else if (accountRes?.error === 'auth_token_expired') {
            expired = true;
            signedIn = false;
          }
        } catch (e) {
          console.warn('Perch account retry failed:', e);
        }
      }

      const labelEl = el('#accountBtnLabel');
      if (signedIn) {
        accountBtn.classList.add('signed-in');
        accountBtn.classList.remove('session-expired');
        const label = _accountDisplayLabel(accountObj);
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
        // Signed out: show an explicit "Sign In" label (not just the person
        // icon) so the affordance is unambiguous (§4a).
        accountBtn.title = 'Perch — Sign in';
        accountBtn.setAttribute('aria-label', 'Perch account: sign in');
        if (labelEl) {
          labelEl.textContent = 'Sign In';
          labelEl.classList.remove('hidden');
        }
      }

      // H6: show + populate the notification bell when signed in.
      if (window.KestrelNotifications) {
        try { window.KestrelNotifications.onAuthState(signedIn); } catch (_) {}
      }

      // Distribution channel gates the sign-in UI: only the macOS App Store
      // build ('appstore') has the native Sign in with Apple transport, so only
      // it shows the chooser (Apple + web). Cached; prewarmed below.
      let _distChannel = null;
      async function _ensureDistChannel() {
        if (_distChannel !== null) return _distChannel;
        try {
          if (window.pywebview?.api?.get_dist_channel) {
            const r = await window.pywebview.api.get_dist_channel();
            _distChannel = (r && r.channel) || 'direct';
          } else {
            _distChannel = 'direct';
          }
        } catch (_) { _distChannel = 'direct'; }
        return _distChannel;
      }

      function _startWebSignIn() {
        // OAuth + PKCE: on Win/Linux Python opens the system browser + a loopback
        // callback server; on macOS it uses ASWebAuthenticationSession. Either
        // way it notifies us via window.onAuthSignIn(...). Covers Google + email.
        if (hasPywebviewApi && window.pywebview?.api?.start_oauth_sign_in) {
          try {
            window.pywebview.api.start_oauth_sign_in();
            if (typeof showToast === 'function') {
              showToast('Sign-in in progress — complete sign-in in the window that opens, then return here.', 8000);
            }
          } catch (e) {
            console.warn('start_oauth_sign_in failed:', e);
          }
        } else if (typeof showToast === 'function') {
          showToast('Sign-in unavailable in this environment.', 5000);
        }
      }

      function _startAppleSignIn() {
        // Native Sign in with Apple (ASAuthorizationController). App Store build
        // only; notifies via the same window.onAuthSignIn(...) callback.
        if (window.pywebview?.api?.start_apple_native_sign_in) {
          try {
            window.pywebview.api.start_apple_native_sign_in();
            if (typeof showToast === 'function') {
              showToast('Continue in the Apple sign-in window…', 8000);
            }
          } catch (e) {
            console.warn('start_apple_native_sign_in failed:', e);
          }
        }
      }

      async function openSignInUI() {
        let channel = 'direct';
        try { channel = await _ensureDistChannel(); } catch (_) {}
        // Guideline 4.8: present Sign in with Apple alongside the third-party
        // (Google) option. Only the App Store build can do native Apple, so only
        // it gets the chooser; every other build goes straight to the web flow.
        if (channel === 'appstore' && window.pywebview?.api?.start_apple_native_sign_in) {
          const dlg = document.getElementById('signInChooserDlg');
          if (dlg) {
            try { dlg.showModal(); return; } catch (_) { /* fall through */ }
          }
        }
        _startWebSignIn();
      }

      accountBtn.addEventListener('click', () => {
        // §4a: when signed in, the account button opens the Account & Cloud
        // Compute panel instead of re-triggering sign-in. Signed-out (incl.
        // session-expired) opens the sign-in UI.
        if (accountBtn.classList.contains('signed-in')) {
          if (typeof window.openCloudAccountPanel === 'function') {
            window.openCloudAccountPanel();
          } else if (typeof showToast === 'function') {
            showToast('Account panel unavailable in this build.', 4000);
          }
          return;
        }
        openSignInUI();
      });

      // Wire the sign-in chooser (present only in the App Store build markup,
      // but harmless to wire when absent).
      const _siDlg = document.getElementById('signInChooserDlg');
      const _siApple = document.getElementById('signInAppleBtn');
      const _siWeb = document.getElementById('signInWebBtn');
      const _siClose = document.getElementById('signInChooserClose');
      if (_siApple) _siApple.addEventListener('click', () => {
        try { _siDlg?.close(); } catch (_) {}
        _startAppleSignIn();
      });
      if (_siWeb) _siWeb.addEventListener('click', () => {
        try { _siDlg?.close(); } catch (_) {}
        _startWebSignIn();
      });
      if (_siClose) _siClose.addEventListener('click', () => {
        try { _siDlg?.close(); } catch (_) {}
      });

      // Prewarm the channel so the first sign-in click doesn't wait on a bridge
      // round-trip before deciding which UI to show.
      _ensureDistChannel();
    }

    // Called by Python after a successful OAuth sign-in or refresh that
    // produced a fresh access token. Python clears its account/usage caches
    // before this fires, so the get_perch_account call below hits the network.
    window.onAuthSignIn = async (token) => {
      _perchToken = token;
      const accountBtn = el('#accountBtn');
      const labelEl = el('#accountBtnLabel');
      if (accountBtn) {
        accountBtn.classList.add('signed-in');
        accountBtn.classList.remove('session-expired');
      }
      if (labelEl) {
        labelEl.textContent = 'Signed in';
        labelEl.classList.remove('hidden');
      }
      try {
        if (window.pywebview?.api?.get_perch_account) {
          const accountRes = await window.pywebview.api.get_perch_account();
          if (accountRes?.success && accountRes.account && accountBtn) {
            const label = _accountDisplayLabel(accountRes.account);
            accountBtn.title = `Perch — Signed in as ${label}`;
            accountBtn.setAttribute('aria-label', `Perch account: signed in as ${label}`);
            if (labelEl) labelEl.textContent = label;
          }
        }
      } catch (e) { /* ignore — indicator is on regardless */ }
      if (window.KestrelNotifications) {
        try { window.KestrelNotifications.onAuthState(true); } catch (_) {}
      }
    };

    // Called by Python when the OAuth flow fails (user closed the browser,
    // callback timed out, port collision, state mismatch, token-exchange
    // error). ``info`` is ``{error, description}``.
    window.onAuthSignInFailed = (info) => {
      const err = (info && info.error) || 'unknown';
      const desc = (info && info.description) || '';
      console.warn('[auth] sign-in failed:', err, desc);
      let msg;
      switch (err) {
        case 'timeout':         msg = 'Sign-in timed out. Click Sign In to try again.'; break;
        case 'port_in_use':     msg = 'The local sign-in ports are all in use. Close other apps and try again.'; break;
        case 'no_loopback_port': msg = 'Could not open a local sign-in port. If you run a VPN, firewall, or virtualization software (Hyper-V/WSL/Docker), restarting your PC usually clears this — then sign in again.'; break;
        case 'state_mismatch':  msg = 'Sign-in failed (state mismatch). Click Sign In to try again.'; break;
        case 'flow_in_progress': msg = 'Sign-in is already in progress. Complete it in your browser.'; break;
        case 'browser_open_failed': msg = 'Could not open your browser. Sign in manually at myaccount.projectkestrel.org and try again.'; break;
        // Native Sign in with Apple (App Store build).
        case 'apple_unavailable': msg = 'Sign in with Apple isn’t available on this build. Use Continue with Google or email.'; break;
        case 'apple_present_failed':
        case 'apple_auth_error':
        case 'apple_no_identity_token': msg = 'Apple sign-in didn’t complete. Please try again.'; break;
        case 'apple_signin_rejected': msg = 'Apple sign-in couldn’t be verified. Try again, or use Continue with Google or email.'; break;
        case 'apple_bridge_no_redirect':
        case 'apple_bridge_no_code':
        case 'apple_bridge_bad_redirect': msg = 'Couldn’t finish signing in after Apple. Please try again.'; break;
        default:                msg = `Sign-in failed: ${err}${desc ? ' — ' + desc : ''}`;
      }
      if (typeof showToast === 'function') showToast(msg, 8000);
    };

    // Called by Python after sign_out clears the keychain.
    window.onAuthSignOut = () => {
      _perchToken = null;
      const accountBtn = el('#accountBtn');
      const labelEl = el('#accountBtnLabel');
      if (accountBtn) {
        accountBtn.classList.remove('signed-in');
        accountBtn.classList.remove('session-expired');
        accountBtn.title = 'Perch — Sign in';
        accountBtn.setAttribute('aria-label', 'Perch account: sign in');
      }
      if (labelEl) {
        // §4a: keep the explicit "Sign In" affordance after sign-out.
        labelEl.textContent = 'Sign In';
        labelEl.classList.remove('hidden');
      }
      // If the account panel is open, close it — it's signed-in-only content.
      if (typeof window.closeCloudAccountPanel === 'function') {
        try { window.closeCloudAccountPanel(); } catch {}
      }
      // H6: hide the notification bell when signed out.
      if (window.KestrelNotifications) {
        try { window.KestrelNotifications.onAuthState(false); } catch (_) {}
      }
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

