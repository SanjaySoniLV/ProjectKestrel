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

    // ─── Support ─────────────────────────────────────────────────────────────
    // The destination is resolved by the backend (api_bridge.get_support_url),
    // never hardcoded here: /support-me carries the donate option, /support has
    // no payment path at all. Apple's anti-steering rule exempts the US
    // storefront and no other, so the App Store build has to ask StoreKit before
    // it's allowed to link out. Don't reintroduce a literal donate URL.
    const SUPPORT_URL_FALLBACK = 'https://projectkestrel.org/support';
    const DONATE_THRESHOLD_KEY = 'kestrel-donate-thresholds-shown-v1';

    // Cache ONLY a confident (donate) result. The no-payment page is also the
    // fail-closed answer the backend returns while the App Store storefront is
    // still resolving right after launch — so caching it permanently (as an
    // earlier version did) would strand a US user on /support for the whole
    // session even though the storefront later resolves to USA. By locking in
    // only the positive answer and re-asking otherwise, the click path recovers
    // as soon as the storefront is ready. A genuinely non-US user simply
    // re-queries (cheap) and keeps getting /support.
    let _supportUrlSticky = null;   // set once we get a donate:true result
    let _supportUrlInflight = null; // dedupe concurrent lookups

    /** Resolve this build's support URL. Fails closed to the no-payment page —
     *  a missing donate link costs a click, while a stray one on a non-US App
     *  Store build costs the listing. */
    function _ensureSupportUrl() {
      if (_supportUrlSticky) return Promise.resolve(_supportUrlSticky);
      if (_supportUrlInflight) return _supportUrlInflight;
      _supportUrlInflight = (async () => {
        try {
          if (hasPywebviewApi && window.pywebview?.api?.get_support_url) {
            const r = await window.pywebview.api.get_support_url();
            if (r?.success && r.url) {
              if (r.donate === true) _supportUrlSticky = r.url;  // lock in only the positive answer
              return r.url;
            }
          }
        } catch (_) { }
        return SUPPORT_URL_FALLBACK;
      })();
      const p = _supportUrlInflight;
      p.finally(() => { if (_supportUrlInflight === p) _supportUrlInflight = null; });
      return p;
    }

    async function openSupportLink() {
      const url = await _ensureSupportUrl();
      if (hasPywebviewApi && window.pywebview?.api?.open_url) {
        window.pywebview.api.open_url(url);
      } else {
        try { window.open(url, '_blank', 'noopener,noreferrer'); } catch (_) { }
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

    const DONATE_THRESHOLDS = [1000, 5000, 10000, 25000, 50000, 100000, 200000];
    // A run smaller than this reads oddly as a headline ("just sorted 12
    // photos"), so the prompt falls back to the lifetime-only copy.
    const DONATE_RUN_COPY_MIN_FILES = 50;

    /** Largest milestone at or below totalFiles (0 when below the first). */
    function donateMilestoneFor(totalFiles) {
      const total = Number(totalFiles) || 0;
      for (let i = DONATE_THRESHOLDS.length - 1; i >= 0; i--) {
        if (total >= DONATE_THRESHOLDS[i]) return DONATE_THRESHOLDS[i];
      }
      return 0;
    }

    /** "This run" numbers from a finished queue item (queue_manager to_dict).
     *  Scenes are only reported when the run analysed the whole folder, so the
     *  photo and scene counts describe the same set of photos. */
    function donateRunFromQueueItem(item) {
      if (!item) return null;
      const files = Number(item.files_this_run) || 0;
      const total = Number(item.total) || 0;
      const scenes = Number(item.scene_total);
      return {
        filesThisRun: files,
        scenes: (files > 0 && files === total && scenes > 0) ? scenes : 0,
      };
    }

    /** Heading + subline for the milestone prompt. Pure, so it can be tested
     *  outside the app. run is null at startup (there is no "this run"). */
    function buildDonatePromptCopy(milestone, run) {
      const fmt = (n) => Number(n).toLocaleString();
      const files = Number(run?.filesThisRun) || 0;
      if (files >= DONATE_RUN_COPY_MIN_FILES) {
        const scenes = Number(run.scenes) || 0;
        let heading = `Kestrel just sorted ${fmt(files)} photos`;
        if (scenes > 0) heading += ` into ${fmt(scenes)} scene${scenes === 1 ? '' : 's'}`;
        return { heading, subline: `That takes you past ${fmt(milestone)} photos organized with Kestrel.` };
      }
      return { heading: `You’ve organized over ${fmt(milestone)} photos with Kestrel!`, subline: '' };
    }

    // ── Donate click counting ──
    // Counts prompt shown / Support / Feedback / closed and the top-strip
    // Support button, so the prompt's conversion can be measured. The bridge
    // sends nothing unless the user opted in to analytics.
    let _donatePromptCtx = null;   // { milestone, trigger } while the prompt is open

    async function recordDonateEvent(event, milestone, trigger) {
      try {
        if (!(hasPywebviewApi && window.pywebview?.api?.record_donate_event)) return;
        await _ensureSupportUrl();
        window.pywebview.api.record_donate_event({
          event,
          milestone: milestone || 0,
          trigger: trigger || '',
          donate_link: !!_supportUrlSticky,
        });
      } catch (_) { /* failsafe */ }
    }

    /** Called by the dialog's buttons (welcome.js) before they close it, so
     *  the 'close' handler below knows the dialog wasn't simply dismissed. */
    function noteDonatePromptChoice(choice) {
      if (!_donatePromptCtx) return;
      const ctx = _donatePromptCtx;
      _donatePromptCtx = null;
      const event = choice === 'support' ? 'donate_prompt_support' : 'donate_prompt_feedback';
      recordDonateEvent(event, ctx.milestone, ctx.trigger);
    }

    function _onDonatePromptClosed() {
      if (!_donatePromptCtx) return;   // a button already recorded the outcome
      const ctx = _donatePromptCtx;
      _donatePromptCtx = null;
      recordDonateEvent('donate_prompt_close', ctx.milestone, ctx.trigger);
    }

    /** Show the milestone prompt. Returns true only if it was actually shown
     *  (it stays hidden while another dialog is open). */
    function showDonatePrompt(totalFiles, run, trigger) {
      const dlg = document.getElementById('donateDlg');
      if (!dlg || document.querySelector('dialog[open]')) return false;
      const milestone = donateMilestoneFor(totalFiles);
      const copy = buildDonatePromptCopy(milestone, run);
      const headingEl = document.getElementById('donateDlgHeading');
      const sublineEl = document.getElementById('donateDlgSubline');
      if (headingEl) headingEl.textContent = copy.heading;
      if (sublineEl) {
        sublineEl.textContent = copy.subline;
        sublineEl.hidden = !copy.subline;
      }
      dlg.showModal();
      _donatePromptCtx = { milestone, trigger };
      recordDonateEvent('donate_prompt_shown', milestone, trigger);
      return true;
    }

    /** Show the prompt for the first lifetime milestone that hasn't been shown
     *  yet. A milestone is marked shown only once the dialog really opened, so
     *  one blocked by another dialog is retried later. */
    function _showNextDonateMilestone(total, run, trigger) {
      const shown = _loadDonateThresholdsShown();
      kdebug('[donation] Thresholds already shown:', shown);
      for (const t of DONATE_THRESHOLDS) {
        if (total >= t && !shown.includes(t)) {
          kdebug('[donation] Milestone crossed:', t, '- showing dialog');
          if (showDonatePrompt(total, run, trigger)) {
            shown.push(t);
            _saveDonateThresholdsShown(shown);
          }
          return;
        }
      }
    }

    /** Check if the cumulative total crosses a donation milestone. Called by
     *  queue.js after a folder finishes, with that run's numbers. */
    async function checkDonationThresholdAsync(run) {
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
        _showNextDonateMilestone(total, run || null, 'analysis');
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
        // Let the UI settle before showing anything at launch.
        setTimeout(() => _showNextDonateMilestone(total, null, 'startup'), 1000);
      } catch (e) {
        console.error('[donation] checkDonationThresholdOnStartup error:', e);
      }
    }

    document.getElementById('donateBtnMain')?.addEventListener('click', () => {
      recordDonateEvent('support_button_click', 0, '');
      openSupportLink();
    });
    document.getElementById('donateDlg')?.addEventListener('close', _onDonatePromptClosed);
    // The dialog's three buttons are wired in welcome.js; each calls
    // noteDonatePromptChoice() before closing it.
    // Resolve the support URL up front so the click path doesn't wait on the bridge.
    _ensureSupportUrl();
    // ─── End Support ──────────────────────────────────────────────────────

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
