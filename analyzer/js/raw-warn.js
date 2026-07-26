    // ── Unsupported-RAW Warning Banner ──────────────────────────────────────
    // The analysis pipeline falls back to a file's embedded JPEG preview when
    // LibRaw can open the RAW container but cannot decompress the sensor data
    // — most commonly Nikon's High Efficiency (HE / HE*) NEFs, which use the
    // proprietary TicoRAW codec. That fallback is recorded per row as
    // exposure_pipeline == 'embedded_preview_jpeg' and, until now, was only
    // visible as a transient line in the Live Analysis status area and a
    // warning in the run log. Users who analysed a whole shoot of HE NEFs got
    // materially worse results with no durable signal as to why.
    //
    // This surfaces it with the same top-of-window banner treatment as the
    // legal consent notice, in a warning red. Dismissible for the session via
    // the × button, or permanently via "Don't Show Again".

    const RAW_WARN_LEARN_MORE_URL = 'https://www.projectkestrel.org/notes/nikon-unsupported-raws';
    const RAW_WARN_SUPPRESS_KEY = 'raw_unsupported_warn_suppressed';
    const RAW_WARN_FALLBACK_PIPELINE = 'embedded_preview_jpeg';

    // Extension → the wording used when that brand's RAWs hit the fallback.
    // Only Nikon ships a compression mode common enough to warrant calling
    // out by name; add entries here if another vendor's codec starts showing
    // up in the wild.
    const RAW_WARN_MESSAGES = {
      '.nef': 'Your Nikon .NEF RAW files are encoded using an unsupported '
        + 'compression type and will lead to poor results. Consider changing '
        + 'your compression type.',
    };

    // Suppressed for this session only (× button). Cleared on next launch.
    let _rawWarnDismissedThisSession = false;

    function _rawWarnExtOf(name) {
      const s = String(name || '');
      const dot = s.lastIndexOf('.');
      return dot === -1 ? '' : s.slice(dot).toLowerCase();
    }

    /** Return the extension whose warning should show, or '' if none. */
    function detectUnsupportedRawExt(rowList) {
      if (!Array.isArray(rowList)) return '';
      for (const r of rowList) {
        if (!r) continue;
        if (String(r.exposure_pipeline || '') !== RAW_WARN_FALLBACK_PIPELINE) continue;
        const ext = _rawWarnExtOf(r.filename);
        if (ext in RAW_WARN_MESSAGES) return ext;
      }
      return '';
    }

    function _rawWarnIsSuppressed() {
      try { return getSetting(RAW_WARN_SUPPRESS_KEY, false) === true; } catch { return false; }
    }

    function _rawWarnSuppressForever() {
      const settings = { ...loadSettings(), [RAW_WARN_SUPPRESS_KEY]: true };
      saveSettings(settings);
      // Mirror to settings.json so the choice survives a localStorage reset.
      if (hasPywebviewApi && window.pywebview?.api?.save_settings_data) {
        try { window.pywebview.api.save_settings_data(settings); } catch (_) { }
      }
    }

    function hideRawWarnBanner() {
      document.getElementById('rawWarnNotice')?.classList.add('hidden');
    }

    /**
     * Show or hide the banner based on the currently loaded rows.
     * Safe to call repeatedly — every folder load re-evaluates.
     */
    function refreshRawWarnBanner() {
      const banner = document.getElementById('rawWarnNotice');
      const msgEl = document.getElementById('rawWarnMsg');
      if (!banner || !msgEl) return;

      if (_rawWarnDismissedThisSession || _rawWarnIsSuppressed()) {
        banner.classList.add('hidden');
        return;
      }

      const ext = detectUnsupportedRawExt(rows);
      if (!ext) {
        banner.classList.add('hidden');
        return;
      }

      msgEl.textContent = RAW_WARN_MESSAGES[ext];
      // The legal banner owns the top strip when it is up; stack beneath it.
      // Measure rather than assume a height — it wraps to two lines on a
      // narrow window.
      const legal = document.getElementById('legalNotice');
      const legalVisible = !!legal && !legal.classList.contains('hidden');
      if (legalVisible) {
        banner.style.setProperty('--raw-warn-offset', `${legal.offsetHeight}px`);
      } else {
        banner.style.removeProperty('--raw-warn-offset');
      }
      banner.classList.toggle('stacked', legalVisible);
      banner.classList.remove('hidden');
    }

    (function wireRawWarnBanner() {
      const closeBtn = document.getElementById('rawWarnCloseBtn');
      if (closeBtn) {
        closeBtn.addEventListener('click', () => {
          _rawWarnDismissedThisSession = true;
          hideRawWarnBanner();
        });
      }

      const dontShowBtn = document.getElementById('rawWarnDontShowBtn');
      if (dontShowBtn) {
        dontShowBtn.addEventListener('click', () => {
          _rawWarnDismissedThisSession = true;
          try { _rawWarnSuppressForever(); } catch (e) {
            console.error('Failed to persist RAW warning suppression', e);
          }
          hideRawWarnBanner();
        });
      }

      // pywebview has no real browser context, so target="_blank" opens a
      // dead window. Route through open_url like every other external link.
      const learnMore = document.getElementById('rawWarnLearnMore');
      if (learnMore) {
        learnMore.addEventListener('click', ev => {
          if (hasPywebviewApi && window.pywebview?.api?.open_url) {
            ev.preventDefault();
            try { window.pywebview.api.open_url(RAW_WARN_LEARN_MORE_URL); } catch (_) { }
          }
        });
      }
    })();
