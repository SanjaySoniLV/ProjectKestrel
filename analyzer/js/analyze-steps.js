// Analyze dialog: the three steps (folders, settings, where to run), the
// footer, the Essentials / All settings view and the (?) help tips.
//
// analyze-dialog.js owns the folder tree, the per-folder plan and the time
// estimates; it calls KestrelAnalyzeSteps.setSummary() after every change.
// cloud-compute.js asks startLabel() for the Start button's text.
(function () {
  const TEXT = {
    noFolders: 'Check the folders you want to analyze.',
    photos: n => `${n.toLocaleString()} ${n === 1 ? 'photo' : 'photos'} to analyze`,
    from: n => `From ${n} ${n === 1 ? 'folder' : 'folders'}`,
    upgrade: n => `${n} to upgrade`,
    skip: n => `${n} already done, skipped`,
    about: d => `About ${d}`,
    asFastAs: d => `As fast as ${d}`,
    onLocal: 'on this computer',
    onCloud: 'on Cloud Compute',
    cancel: 'Cancel',
    back: 'Back',
    next: { 1: 'Next: check settings', 2: 'Next: choose where to run' },
    start: n => `Analyze ${n} ${n === 1 ? 'folder' : 'folders'}`,
    startCloud: n => `Analyze ${n} ${n === 1 ? 'folder' : 'folders'} in the cloud`,
    signIn: 'Sign in to use Cloud Compute',
    allRecommended: 'All other settings are at recommended values.',
    changed: n => `${n} other ${n === 1 ? 'setting differs' : 'settings differ'} from recommended.`,
  };
  const VIEW_KEY = 'analyze_settings_view';   // 'essentials' | 'all'

  let step = 1;
  let summary = { folders: 0, images: 0, upgrade: 0, skip: 0, eta: null };
  const $ = id => document.getElementById(id);
  const dlg = () => $('analyzeQueueDlg');

  function render() {
    const d = dlg();
    if (!d) return;
    const any = summary.folders > 0;
    if (!any && step > 1) step = 1;
    for (const pane of d.querySelectorAll('.adlg-pane')) pane.classList.toggle('hidden', Number(pane.dataset.step) !== step);
    for (const b of d.querySelectorAll('.adlg-step')) {
      const n = Number(b.dataset.step);
      b.classList.toggle('on', n === step);
      b.classList.toggle('done', n < step);
      b.disabled = !any && n > 1;
      if (n === step) b.setAttribute('aria-current', 'step'); else b.removeAttribute('aria-current');
    }
    $('adlgBack').textContent = step === 1 ? TEXT.cancel : TEXT.back;
    const next = $('adlgNext');
    next.classList.toggle('hidden', step === 3);
    next.textContent = TEXT.next[step] || '';
    next.disabled = !any;
    const start = $('analyzeDlgAdd');
    start.classList.toggle('hidden', step !== 3);
    start.disabled = !any;
    renderTotals(any);
  }

  function renderTotals(any) {
    const l1 = $('adlgTotLine1'), l2 = $('adlgTotLine2');
    if (!any) {
      l1.textContent = '';
      l2.textContent = TEXT.noFolders;
    } else {
      l1.textContent = TEXT.photos(summary.images);
      const parts = [TEXT.from(summary.folders)];
      if (summary.upgrade) parts.push(TEXT.upgrade(summary.upgrade));
      if (summary.skip) parts.push(TEXT.skip(summary.skip));
      l2.textContent = parts.join(' · ');
    }
    const e = summary.eta;
    const showEta = any && step === 3 && !!e && e.secs > 0;
    $('adlgEta').classList.toggle('hidden', !showEta);
    if (showEta) {
      const d = KestrelUtil.formatDuration(e.secs);
      $('adlgEtaTime').textContent = e.bestCase ? TEXT.asFastAs(d) : TEXT.about(d);
      $('adlgEtaWhere').textContent = e.cloud ? TEXT.onCloud : TEXT.onLocal;
    }
  }

  function go(n) {
    const target = Math.max(1, Math.min(3, n));
    if (target > 1 && summary.folders === 0) return;
    step = target;
    hideTip();
    render();
    const pane = dlg()?.querySelector(`.adlg-pane[data-step="${step}"]`);
    if (pane) pane.scrollTop = 0;
  }

  // ── Settings: Essentials or All settings ─────────────────────────────────
  function applyView(view) {
    const all = view === 'all';
    $('adlgAllSettings').classList.toggle('hidden', !all);
    $('adlgShowAll').classList.toggle('hidden', all);
    $('adlgViewEssentials').classList.toggle('on', !all);
    $('adlgViewAll').classList.toggle('on', all);
    $('adlgViewEssentials').setAttribute('aria-pressed', String(!all));
    $('adlgViewAll').setAttribute('aria-pressed', String(all));
  }

  function setView(view) {
    applyView(view);
    if (typeof mergeSetting === 'function') mergeSetting(VIEW_KEY, view === 'all' ? 'all' : 'essentials');
  }

  /** A row differs when its control's value isn't its data-rec value. */
  function isChanged(el) {
    const rec = el.dataset.rec;
    if (el.type === 'checkbox') return el.checked !== (rec === 'on');
    if (el.type === 'number') return Number(el.value) !== Number(rec);
    return String(el.value) !== rec;
  }

  function refreshChanged() {
    const box = $('adlgAllSettings');
    if (!box) return;
    let n = 0;
    for (const el of box.querySelectorAll('[data-rec]')) {
      const changed = isChanged(el);
      if (changed) n++;
      el.closest('.k-row')?.classList.toggle('is-changed', changed);
    }
    $('adlgChangedText').textContent = n ? TEXT.changed(n) : TEXT.allRecommended;
  }

  // ── Help tips: one bubble, filled from the (?) button's data-tip ─────────
  let tipEl = null;
  function showTip(btn) {
    if (!tipEl) {
      tipEl = document.createElement('div');
      tipEl.id = 'adlgTip';
      tipEl.className = 'adlg-tip';
      tipEl.setAttribute('role', 'tooltip');
      dlg().appendChild(tipEl);
    }
    tipEl.textContent = btn.dataset.tip || '';
    btn.setAttribute('aria-describedby', 'adlgTip');
    tipEl.classList.add('on');
    const r = btn.getBoundingClientRect();
    const w = tipEl.offsetWidth, h = tipEl.offsetHeight;
    const left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left + r.width / 2 - w / 2));
    const below = r.bottom + 8 + h < window.innerHeight;
    tipEl.style.left = `${left}px`;
    tipEl.style.top = `${below ? r.bottom + 8 : r.top - h - 8}px`;
  }
  function hideTip() {
    if (tipEl) tipEl.classList.remove('on');
  }

  // ── Wiring ───────────────────────────────────────────────────────────────
  function onKeydown(e) {
    if (e.key !== 'Enter' || e.isComposing || step === 3) return;
    if (e.target.closest('button, select, a, textarea, [role="radio"]')) return;
    e.preventDefault();
    go(step + 1);
  }

  function wire() {
    const d = dlg();
    if (!d || d.dataset.stepsWired) return;
    d.dataset.stepsWired = '1';
    for (const b of d.querySelectorAll('.adlg-step')) b.addEventListener('click', () => go(Number(b.dataset.step)));
    $('adlgNext').addEventListener('click', () => go(step + 1));
    $('adlgBack').addEventListener('click', () => (step === 1 ? d.close() : go(step - 1)));
    $('adlgViewEssentials').addEventListener('click', () => setView('essentials'));
    $('adlgViewAll').addEventListener('click', () => setView('all'));
    $('adlgShowAll').addEventListener('click', () => setView('all'));
    $('adlgHideAll').addEventListener('click', () => {
      setView('essentials');
      $('adlgViewEssentials').focus();
    });
    $('adlgAllSettings').addEventListener('input', refreshChanged);
    $('adlgAllSettings').addEventListener('change', refreshChanged);
    const legendBtn = $('adlgLegendBtn');
    legendBtn.addEventListener('click', () => {
      const open = $('adlgLegend').classList.toggle('hidden') === false;
      legendBtn.setAttribute('aria-expanded', String(open));
    });
    d.addEventListener('keydown', onKeydown);
    for (const btn of d.querySelectorAll('.adlg-help')) {
      btn.addEventListener('mouseenter', () => showTip(btn));
      btn.addEventListener('focus', () => showTip(btn));
      btn.addEventListener('mouseleave', hideTip);
      btn.addEventListener('blur', hideTip);
      // A click (or tap) keeps the tip open instead of doing anything else.
      btn.addEventListener('click', (e) => { e.preventDefault(); showTip(btn); });
    }
    d.querySelector('.adlg-body').addEventListener('scroll', hideTip, true);
    d.addEventListener('close', hideTip);
  }

  /** Called on every open: back to step 1, settings view as last used. */
  function reset() {
    wire();
    step = 1;
    const saved = typeof getSetting === 'function' ? getSetting(VIEW_KEY, 'essentials') : 'essentials';
    applyView(saved === 'all' ? 'all' : 'essentials');
    $('adlgLegend').classList.add('hidden');
    $('adlgLegendBtn').setAttribute('aria-expanded', 'false');
    refreshChanged();
    render();
  }

  /** s: { folders, images, upgrade, skip, eta: null | { secs, cloud, bestCase } } */
  function setSummary(s) {
    summary = Object.assign({ folders: 0, images: 0, upgrade: 0, skip: 0, eta: null }, s);
    if (dlg()) render();
    if (typeof _ccUpdateAddButtonLabel === 'function') _ccUpdateAddButtonLabel();
  }

  function startLabel({ cloud, needsSignIn, folders }) {
    if (needsSignIn) return TEXT.signIn;
    return cloud ? TEXT.startCloud(folders) : TEXT.start(folders);
  }

  window.KestrelAnalyzeSteps = { reset, setSummary, startLabel, refreshChanged, go };
})();
