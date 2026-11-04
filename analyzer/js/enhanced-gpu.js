// Enhanced GPU performance (opt-in vendor GPU acceleration through Windows ML).
//
// Two surfaces, both driven by the Python side (api_bridge.enhanced_gpu_*, queue status `gpu_mode`):
//  1. Settings: setup / cancel / on-off, and a status block that says whether setup worked, how much
//     faster each model was when it was checked, what this session is using right now, and what fell back.
//  2. The "what is analysis running on" strip in the analysis queue panel and the Live Analysis dialog:
//     enhanced GPU, standard GPU (DirectML), CPU, or fell back to CPU (restart + report a problem).
(function () {
  'use strict';

  let pollTimer = null;
  let wasRunning = false;
  let lastStatus = null;

  function api() { return window.pywebview && window.pywebview.api; }
  function el(id) { return document.getElementById(id); }
  function sentence(t) { t = String(t || '').trim(); return t && !/[.!?]$/.test(t) ? t + '.' : t; }

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
    cpu_selected: 'cpu', component_cpu: 'bad', cpu_fallback: 'bad', gpu_lost: 'bad',
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
  // Plain language only: no model, provider or DirectML names and no speed multipliers here. Those
  // details are in the analysis log and in the bug report text.
  function setupLine(st) {
    if (!st.available) return 'Not available: ' + sentence(st.unavailable_reason);
    if (st.disabled) return 'Switched off automatically after problems were detected. Standard GPU acceleration is used. You can run setup again.';
    if (st.set_up) {
      const on = st.enabled ? 'Set up and on' : 'Set up, but turned off';
      return on + ' \u2014 ' + (st.ep_label || 'vendor acceleration') + (st.measured ? '' : ' (not yet tested on this kind of GPU)') + '.';
    }
    return null;
  }

  function speedLine(st) {
    const models = Object.keys(st.models || {}).map((k) => st.models[k]);
    if (!models.length) return '';
    const on = models.filter((m) => m.enabled).length;
    if (!on) return 'Setup found no speed-up on this computer, so standard GPU acceleration is used.';
    return on === models.length
      ? 'Setup confirmed that Enhanced GPU Performance speeds up analysis on this computer.'
      : 'Setup confirmed a speed-up for part of the analysis; the rest uses standard GPU acceleration.';
  }

  function runtimeLine(st) {
    const rt = st.runtime || {};
    const out = [];
    if ((rt.cpu_components || []).length) {
      out.push('Part of the GPU stopped responding, so some analysis steps now run on the CPU (slower). Restart Kestrel to restore full speed.');
    }
    if ((rt.on_vendor || []).length) out.push('In use now.');
    else if (st.set_up && st.enabled && !st.disabled) out.push('Not used yet this session (it starts with the next analysis).');
    if ((rt.fallbacks || []).length) out.push('Some steps switched to standard GPU acceleration after a problem.');
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
      if (st.stale) lines.push('Kestrel or its GPU software was updated since the last check; run setup again to refresh it.');
      if (st.finished) lines.push('Checked ' + new Date(st.finished * 1000).toLocaleString() + '.');
    } else {
      const why = (job.result && !job.result.ok && job.result.reason) || st.reason;
      if (/cancel/i.test(why || '')) lines.push('Setup was cancelled. Standard GPU acceleration is used.');
      else if (why) lines.push('Setup did not finish. Standard GPU acceleration is used.');
      else lines.push('Not set up. Standard GPU acceleration is used.');
    }
    el('egStatus').textContent = lines.join('\n');

    // A problem report is offered after a setup run that did not succeed, and whenever a model fell back at runtime.
    const failed = !running && wasRunning && job.result && !job.result.ok && !/cancelled/i.test(job.result.reason || '')
      && st.available;
    if (failed) el('egReportRow').classList.remove('hidden');
    if (running) el('egReportRow').classList.add('hidden');
    const rtNow = st.runtime || {};
    const hadFallback = (rtNow.fallbacks || []).length > 0 || (rtNow.cpu_components || []).length > 0 || st.disabled;
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
