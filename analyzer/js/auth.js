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

