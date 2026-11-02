// Settings: "Enhanced GPU performance" (opt-in vendor GPU acceleration through Windows ML).
// The Python side (api_bridge.enhanced_gpu_*) does all the work; this only shows status, starts or
// cancels setup, polls its progress, and offers a bug report when setup does not succeed.
(function () {
  'use strict';

  const MODEL_LABELS = { detector: 'Detector', sam_enc: 'Segmentation', bird_species: 'Species ID' };
  let pollTimer = null;
  let wasRunning = false;

  function api() { return window.pywebview && window.pywebview.api; }
  function el(id) { return document.getElementById(id); }

  function describeModels(models) {
    const parts = [];
    Object.keys(models || {}).forEach((k) => {
      const m = models[k];
      const name = MODEL_LABELS[k] || k;
      if (m.enabled) parts.push(name + ': on' + (m.speedup ? ' (' + m.speedup + '× faster)' : ''));
      else parts.push(name + ': DirectML' + (m.reason ? ' — ' + m.reason : ''));
    });
    return parts.join('; ');
  }

  function sentence(t) { t = String(t || '').trim(); return t && !/[.!?]$/.test(t) ? t + '.' : t; }

  function render(st) {
    const section = el('egSection');
    if (!section) return;
    if (!st || st.available === undefined) { section.classList.add('hidden'); return; }
    // macOS (and anything that is not Windows) never sees this setting.
    if (!st.available && /only available on windows/i.test(st.unavailable_reason || '')) {
      section.classList.add('hidden');
      return;
    }
    section.classList.remove('hidden');
    const job = st.job || {};
    const running = !!job.running;
    const box = el('settingsEnhancedGpu');
    const status = el('egStatus');
    const setup = el('egSetupBtn');
    const cancel = el('egCancelBtn');
    const report = el('egReportRow');

    box.disabled = !st.available || !st.set_up || st.disabled || running;
    box.checked = !!st.enabled && st.set_up && !st.disabled;
    setup.disabled = !st.available || running;
    setup.textContent = st.set_up ? 'Run setup again' : 'Set up enhanced GPU performance';
    cancel.classList.toggle('hidden', !running);

    let text = '';
    if (!st.available) text = 'Not available: ' + sentence(st.unavailable_reason);
    else if (running) text = job.message || 'Working…';
    else if (st.disabled) text = 'Switched off automatically (' + (st.disabled_reason || 'problems detected') + '). Using DirectML. You can run setup again.';
    else if (st.set_up) {
      text = (st.enabled ? 'On' : 'Off') + ' — ' + (st.ep_label || st.ep) + (st.measured ? '' : ' (not yet tested on this kind of GPU)') +
        '. ' + describeModels(st.models) + '.';
      if (st.stale) text += ' Kestrel was updated since the last check; run setup again to refresh it.';
    } else if (job.result && !job.result.ok) text = 'Not set up: ' + sentence(job.result.reason || st.reason || 'unknown reason') + ' Using DirectML.';
    else if (st.reason) text = 'Not set up: ' + sentence(st.reason) + ' Using DirectML.';
    else text = 'Not set up. Using DirectML.';
    status.textContent = text;

    // Offer a bug report once, right after a setup run that did not succeed (not when the user cancelled
    // or the platform is simply too old).
    const failed = !running && wasRunning && job.result && !job.result.ok && !/cancelled/i.test(job.result.reason || '')
      && st.available;
    if (failed) report.classList.remove('hidden');
    if (running) report.classList.add('hidden');
    wasRunning = running;
  }

  async function refresh() {
    if (!api() || !api().enhanced_gpu_status) return;
    try {
      const res = await api().enhanced_gpu_status();
      render(res && res.success ? res.status : null);
      const running = !!(res && res.status && res.status.job && res.status.job.running);
      if (running && !pollTimer) pollTimer = setInterval(refresh, 800);
      if (!running && pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    } catch (_) { /* leave the section as it was */ }
  }
  window.egRefreshSettings = refresh;

  async function startSetup() {
    if (!api()) return;
    el('egReportRow').classList.add('hidden');
    wasRunning = true;
    const r = await api().enhanced_gpu_start_setup();
    if (!r || !r.success) { el('egStatus').textContent = (r && r.error) || 'Could not start setup.'; wasRunning = false; return; }
    refresh();
  }

  async function sendReport() {
    el('egReportRow').classList.add('hidden');
    let text = 'Enhanced GPU performance setup did not work.';
    try {
      const r = await api().enhanced_gpu_report_text();
      if (r && r.text) text = r.text;
    } catch (_) { /* fall back to the short text */ }
    if (window.openFeedbackWithPrefill) window.openFeedbackWithPrefill('bug', text, true);
  }

  document.addEventListener('DOMContentLoaded', () => {
    const setup = el('egSetupBtn');
    if (!setup) return;
    setup.addEventListener('click', startSetup);
    el('egCancelBtn').addEventListener('click', () => api() && api().enhanced_gpu_cancel_setup());
    el('egReportBtn').addEventListener('click', sendReport);
    el('egReportDismiss').addEventListener('click', () => el('egReportRow').classList.add('hidden'));
    el('settingsEnhancedGpu').addEventListener('change', async (e) => {
      if (api()) { await api().enhanced_gpu_set_enabled(!!e.target.checked); refresh(); }
    });
  });
})();
