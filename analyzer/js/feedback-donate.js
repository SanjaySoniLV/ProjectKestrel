    // ─── Feedback dialog ──────────────────────────────────────────────────────

    /** Resolve the current account display label (e.g. "Jane Doe" / "@jane"),
     *  falling back to "my account" when no specific handle is available. The
     *  startup auth hydration already paints this into #accountBtnLabel. */
    function _feedbackAccountLabel() {
      const labelEl = document.getElementById('accountBtnLabel');
      const txt = (labelEl?.textContent || '').trim();
      // #accountBtnLabel doubles as the signed-out "Sign In" affordance, so
      // ignore those sentinel values.
      if (txt && !/^sign\s*in$/i.test(txt) && txt.toLowerCase() !== 'signed in') {
        return txt;
      }
      return 'my account';
    }

    /** Sync the "Send as …" checkbox + send-button label to the current
     *  sign-in state. The checkbox is disabled (and forced off) when signed
     *  out — you can't report as an account you aren't. */
    function _syncFeedbackSendAs() {
      const cb = document.getElementById('feedbackSendAsUser');
      const cbLabel = document.getElementById('feedbackSendAsUserLabel');
      const sendBtn = document.getElementById('feedbackSend');
      const accountBtn = document.getElementById('accountBtn');
      const signedIn = !!_perchToken || !!accountBtn?.classList.contains('signed-in');
      const who = _feedbackAccountLabel();
      if (cbLabel) cbLabel.textContent = signedIn ? `Send as ${who}` : 'Send as my account (sign in to enable)';
      if (cb) {
        cb.disabled = !signedIn;
        if (!signedIn) cb.checked = false;
      }
      if (sendBtn) {
        sendBtn.textContent = (signedIn && cb?.checked) ? `Send as ${who}` : 'Send anonymously';
      }
    }

    function openFeedbackDialog() {
      document.getElementById('feedbackDesc').value = '';
      document.getElementById('feedbackContact').value = '';
      document.getElementById('feedbackStatus').textContent = '';
      document.getElementById('feedbackIncludeLogs').checked = false;
      document.getElementById('feedbackIncludeScreenshot').checked = false;
      document.getElementById('feedbackScreenshotFile').value = '';
      document.getElementById('feedbackSendAsUser').checked = false;
      const preview = document.getElementById('feedbackSsPreview');
      preview.src = ''; preview.style.display = 'none'; preview.dataset.b64 = '';
      _syncFeedbackSendAs();
      document.getElementById('feedbackDlg').showModal();
    }

    document.getElementById('feedbackSendAsUser').addEventListener('change', _syncFeedbackSendAs);

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
        send_as_user: document.getElementById('feedbackSendAsUser').checked
          && !document.getElementById('feedbackSendAsUser').disabled,
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
    const DONATE_URL = 'https://donate.stripe.com/aFa28kbrLeb45mFgFE1ZS00';
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
