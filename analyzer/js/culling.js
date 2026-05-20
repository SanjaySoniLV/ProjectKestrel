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
              li.textContent = `…and ${res.skipped_conflicts.length - 10} more`;
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

