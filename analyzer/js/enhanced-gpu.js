// Enhanced GPU performance (opt-in vendor GPU acceleration through Windows ML).
//
// Two surfaces, both driven by the Python side (api_bridge.enhanced_gpu_*, queue status `gpu_mode`):
//  1. Settings: setup / cancel / on-off, and a status block that says whether setup worked, how much
//     faster each model was when it was checked, what this session is using right now, and what fell back.
//  2. The "what is analysis running on" strip in the analysis queue panel and the Live Analysis dialog:
//     enhanced GPU, standard GPU (DirectML), CPU, or fell back to CPU (restart + report a problem).
(function () {
  'use strict';

  const MODEL_LABELS = { detector: 'detector', sam_enc: 'segmentation', bird_species: 'species ID' };
  let pollTimer = null;
  let wasRunning = false;
  let lastStatus = null;

  function api() { return window.pywebview && window.pywebview.api; }
  function el(id) { return document.getElementById(id); }
  function sentence(t) { t = String(t || '').trim(); return t && !/[.!?]$/.test(t) ? t + '.' : t; }
  function label(k) { return MODEL_LABELS[k] || k; }
  function list(kinds) { return (kinds || []).map(label).join(', '); }

  // ── Report a problem (opens the feedback dialog with the GPU details and logs attached) ──
  async function reportProblem(setupFailed) {
    let text = 'GPU mode problem.';
    try {
      const r = setupFailed ? await api().enhanced_gpu_report_text() : await api().gpu_mode_report_text();
      if (r && r.text) text = r.text;
    } catch (_) { /* fall back to the short text */ }
    if (window.openFeedbackWithPrefill) window.openFeedbackWithPrefill('bug', text, true);
  }

  // ── Live strip (queue panel + Live Analysis dialog) ──
  const STRIP_CLASS = {
    enhanced: 'enhanced', enhanced_partial: 'warn', standard: 'standard',
    cpu_selected: 'cpu', cpu_fallback: 'bad', gpu_lost: 'bad',
  };

  window.egRenderGpuMode = function (mode) {
    document.querySelectorAll('[data-gpu-mode-strip]').forEach((strip) => {
      if (!mode || !mode.mode) { strip.classList.add('hidden'); return; }
      strip.className = 'gpu-mode-strip gpu-mode-strip--' + (STRIP_CLASS[mode.mode] || 'standard');
      strip.querySelector('.gms-label').textContent = mode.label || '';
      let detail = mode.detail || '';
      if (mode.needs_restart && !/restart/i.test(detail)) detail += ' Restart Kestrel to use the GPU again.';
      strip.querySelector('.gms-detail').textContent = detail;
      const btn = strip.querySelector('.gms-report');
      if (btn) {
        btn.classList.toggle('hidden', !mode.report);
        if (!btn.dataset.wired) { btn.dataset.wired = '1'; btn.addEventListener('click', () => reportProblem(false)); }
      }
    });
  };

  // ── Settings status ──
  function setupLine(st) {
    if (!st.available) return 'Not available: ' + sentence(st.unavailable_reason);
    if (st.disabled) return 'Switched off automatically (' + (st.disabled_reason || 'problems detected') + '). Using the standard GPU (DirectML). You can run setup again.';
    if (st.set_up) {
      const on = st.enabled ? 'Set up and on' : 'Set up, but turned off';
      return on + ' — ' + (st.ep_label || st.ep) + (st.measured ? '' : ' (not yet tested on this kind of GPU)') + '.';
    }
    return null;
  }

  function speedLine(st) {
    const parts = [];
    Object.keys(st.models || {}).forEach((k) => {
      const m = st.models[k];
      if (m.enabled) parts.push(label(k) + ' ' + (m.speedup ? m.speedup + '× faster' : 'on'));
      else parts.push(label(k) + ' stays on DirectML' + (m.reason ? ' (' + m.reason + ')' : ''));
    });
    return parts.length ? 'Speed when checked (model time vs DirectML): ' + parts.join('; ') + '.' : '';
  }

  function runtimeLine(st) {
    const rt = st.runtime || {};
    const on = rt.on_vendor || [];
    const fb = rt.fallbacks || [];
    const out = [];
    if (on.length) out.push('In use now: ' + list(on) + '.');
    else if (st.set_up && st.enabled && !st.disabled) out.push('Not used yet this session (it starts with the next analysis).');
    fb.forEach((f) => out.push('Fell back to ' + (f.to === 'CPU' ? 'the CPU' : 'the standard GPU (DirectML)') + ': ' + label(f.kind) + ' — ' + sentence(f.reason)));
    return out.join('\n');
  }

  function render(st) {
    lastStatus = st;
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
    const setup = el('egSetupBtn');

    box.disabled = !st.available || !st.set_up || st.disabled || running;
    box.checked = !!st.enabled && st.set_up && !st.disabled;
    setup.disabled = !st.available || running;
    setup.textContent = st.set_up ? 'Run setup again' : 'Set up enhanced GPU performance';
    el('egCancelBtn').classList.toggle('hidden', !running);

    const lines = [];
    const head = setupLine(st);
    if (running) lines.push(job.message || 'Working…');
    else if (head) {
      lines.push(head);
      const sp = speedLine(st);
      if (sp) lines.push(sp);
      const rt = runtimeLine(st);
      if (rt) lines.push(rt);
      if (st.stale) lines.push('Kestrel or its GPU component was updated since the last check; run setup again to refresh it.');
      if (st.finished) lines.push('Checked ' + new Date(st.finished * 1000).toLocaleString() + '.');
    } else {
      const why = (job.result && !job.result.ok && job.result.reason) || st.reason;
      lines.push(why ? 'Not set up: ' + sentence(why) + ' Using the standard GPU (DirectML).' : 'Not set up. Using the standard GPU (DirectML).');
    }
    el('egStatus').textContent = lines.join('\n');

    // A problem report is offered after a setup run that did not succeed, and whenever a model fell back at runtime.
    const failed = !running && wasRunning && job.result && !job.result.ok && !/cancelled/i.test(job.result.reason || '')
      && st.available;
    if (failed) el('egReportRow').classList.remove('hidden');
    if (running) el('egReportRow').classList.add('hidden');
    const hadFallback = ((st.runtime || {}).fallbacks || []).length > 0 || st.disabled;
    el('egReportNowBtn').classList.toggle('hidden', !hadFallback || running);
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

  document.addEventListener('DOMContentLoaded', () => {
    const setup = el('egSetupBtn');
    if (!setup) return;
    setup.addEventListener('click', startSetup);
    el('egCancelBtn').addEventListener('click', () => api() && api().enhanced_gpu_cancel_setup());
    el('egReportBtn').addEventListener('click', () => { el('egReportRow').classList.add('hidden'); reportProblem(true); });
    el('egReportNowBtn').addEventListener('click', () => reportProblem(!(lastStatus && lastStatus.set_up)));
    el('egReportDismiss').addEventListener('click', () => el('egReportRow').classList.add('hidden'));
    el('settingsEnhancedGpu').addEventListener('change', async (e) => {
      if (api()) { await api().enhanced_gpu_set_enabled(!!e.target.checked); refresh(); }
    });
  });
})();
