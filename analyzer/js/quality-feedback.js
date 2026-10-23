    // ─── Quality feedback ──────────────────────────────────────────────────────
    // "Sharpness right? 👍 👎" in the scene viewer. 👎 opens a dialog where the
    // user drags 2–5 bird crops of the burst into order, sharpest first (no
    // ties, no extra controls: the ask should take seconds); 👍 offers it too.
    // Kestrel scores focus/sharpness only, so the dialog asks about focus only.
    // Frames are chosen (and shuffled) in Python — kestrel_analyzer/
    // quality_feedback.py — and the dialog never sees Kestrel's scores or order.
    // Sharing needs consent (mode "ask"), or the user's standing choice
    // ("always"); mode "never" hides the prompt. Upload runs on a Python thread
    // and reports back through window.onQualityFeedbackResult.

    const QF_PRIVACY_URL = 'https://projectkestrel.org/privacy-policy/';
    const QF_MODES = ['ask', 'always', 'never'];
    // Scenes the user already answered this session (no repeat prompt).
    const _qfAnswered = new Set();
    // Dialog state.
    let _qf = null;

    function qfMode() {
      const m = String(getSetting('quality_feedback_mode', 'ask') || 'ask');
      return QF_MODES.includes(m) ? m : 'ask';
    }

    function _qfNum(v) {
      const n = parseFloat(v);
      return Number.isFinite(n) && n >= 0 ? n : null;
    }

    function _qfHasCrop(r) {
      const cp = String(r?.crop_path ?? '').trim();
      return !!cp && cp.toLowerCase() !== 'nan';
    }

    /** ≥2 images with crops and at least one quality score. */
    function qfSceneEligible(scene) {
      const imgs = (scene && scene.images) || [];
      const withCrop = imgs.filter(_qfHasCrop);
      if (withCrop.length < 2) return false;
      return withCrop.some(r => _qfNum(r.quality) !== null || _qfNum(r.quality_siglip) !== null || _qfNum(r.quality_legacy) !== null);
    }

    function _qfSceneKey(scene) {
      const root = scene?.representative?.__rootPath || scene?.images?.[0]?.__rootPath || '';
      return root + '|' + String(scene?.id ?? '');
    }

    function _qfButton(label, title, onClick, extraClass) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'scene-qf-btn' + (extraClass ? ' ' + extraClass : '');
      b.textContent = label;
      b.title = title;
      b.setAttribute('aria-label', title);
      b.addEventListener('click', (e) => { e.stopPropagation(); b.blur(); onClick(); });
      return b;
    }

    /** Paint the thumbs control for the scene shown in the scene viewer. */
    function qfRenderSceneControl(scene) {
      const box = document.getElementById('sceneQf');
      if (!box) return;
      box.replaceChildren();
      const api = window.pywebview?.api;
      const show = !!(api && api.get_quality_feedback_candidates) && qfMode() !== 'never' && qfSceneEligible(scene);
      box.classList.toggle('hidden', !show);
      if (!show) return;
      if (_qfAnswered.has(_qfSceneKey(scene))) {
        const t = document.createElement('span');
        t.className = 'scene-qf-label';
        t.textContent = 'Thanks!';
        box.appendChild(t);
        return;
      }
      const label = document.createElement('span');
      label.className = 'scene-qf-label';
      label.textContent = 'Sharpness right?';
      box.appendChild(label);
      box.appendChild(_qfButton('👍', 'Yes', () => {
        _qfAnswered.add(_qfSceneKey(scene));
        box.replaceChildren();
        const t = document.createElement('span');
        t.className = 'scene-qf-label';
        t.textContent = 'Thanks! ';
        const a = document.createElement('a');
        a.href = '#';
        a.className = 'scene-qf-link';
        a.textContent = 'Sort a few';
        a.addEventListener('click', (e) => { e.preventDefault(); a.blur(); qfOpenSortDialog(scene, 'up'); });
        t.appendChild(a);
        box.appendChild(t);
      }));
      box.appendChild(_qfButton('👎', 'No — sort a few', () => {
        qfOpenSortDialog(scene, 'down');
      }));
    }

    // ── Sort dialog ──

    function _qfEl(tag, cls, text) {
      const e = document.createElement(tag);
      if (cls) e.className = cls;
      if (text != null) e.textContent = text;
      return e;
    }

    async function qfOpenSortDialog(scene, thumbs) {
      const api = window.pywebview?.api;
      if (!api?.get_quality_feedback_candidates) return;
      const root = scene?.representative?.__rootPath || scene?.images?.[0]?.__rootPath || '';
      const localId = String(scene?.id ?? '').split(':').pop();
      const filenames = (scene?.images || []).map(r => r.filename).filter(Boolean);
      let res;
      try {
        res = await api.get_quality_feedback_candidates(root, localId, filenames);
      } catch (e) {
        res = { success: false, error: String(e?.message || e) };
      }
      if (!res || !res.success || !Array.isArray(res.frames) || res.frames.length < 2) {
        showToast("Couldn't load this burst's crops for feedback.", 3500);
        return;
      }
      _qf = {
        scene, thumbs, token: res.token, mode: res.mode || qfMode(),
        frames: res.frames,                                  // [{index, data_url}] in shown order
        items: res.frames.map(f => ({ index: f.index })),  // current order
        started: performance.now(),
        durationMs: 0,
        ranking: null,
        submitted: false,
      };
      _qfRenderSort();
      const dlg = document.getElementById('qfDlg');
      if (!dlg.open) dlg.showModal();
      _qf.started = performance.now();
    }

    function _qfClose(discard) {
      const dlg = document.getElementById('qfDlg');
      if (discard && _qf && !_qf.submitted && _qf.token) {
        try { window.pywebview?.api?.discard_quality_feedback?.(_qf.token); } catch (_) { }
      }
      _qf = null;
      try { if (dlg.open) dlg.close(); } catch (_) { }
    }

    function _qfImg(index) {
      const f = _qf.frames.find(fr => fr.index === index);
      const img = document.createElement('img');
      img.className = 'qf-crop';
      img.alt = 'Bird crop';
      img.draggable = false;
      if (f) img.src = f.data_url;
      return img;
    }

    function _qfRankingFromItems() {
      return _qf.items.map(it => [it.index]);
    }

    function _qfMove(from, to) {
      const items = _qf.items;
      if (from === to || from < 0 || to < 0 || from >= items.length || to >= items.length) return;
      const [it] = items.splice(from, 1);
      items.splice(to, 0, it);
    }

    // One step, no explanations: drag the crops into order, then Done.
    function _qfRenderSort() {
      const body = document.getElementById('qfBody');
      const footer = document.getElementById('qfFooter');
      document.getElementById('qfTitle').textContent = 'Drag to sort: sharpest first';
      document.getElementById('qfSubtitle').textContent = 'Focus only.';
      body.replaceChildren();
      footer.replaceChildren();

      const list = _qfEl('div', 'qf-list');
      list.setAttribute('role', 'list');
      list.setAttribute('aria-label', 'Crops, sharpest first');
      _qf.items.forEach((it, pos) => {
        const card = _qfEl('div', 'qf-card');
        card.setAttribute('role', 'listitem');
        card.setAttribute('aria-label', `Position ${pos + 1} of ${_qf.items.length}`);
        card.tabIndex = 0;
        card.draggable = true;
        card.dataset.pos = String(pos);
        card.appendChild(_qfEl('span', 'qf-rank', String(pos + 1)));
        card.appendChild(_qfImg(it.index));
        // Arrow keys move the focused crop (keyboard fallback for drag).
        card.addEventListener('keydown', (e) => {
          const k = e.key;
          const to = (k === 'ArrowLeft' || k === 'ArrowUp') ? pos - 1
            : (k === 'ArrowRight' || k === 'ArrowDown') ? pos + 1 : null;
          if (to === null || to < 0 || to >= _qf.items.length) return;
          e.preventDefault();
          _qfMove(pos, to);
          _qfRenderSort();
          _qfFocus(to);
        });
        card.addEventListener('dragstart', (e) => {
          e.dataTransfer.effectAllowed = 'move';
          try { e.dataTransfer.setData('text/plain', String(pos)); } catch (_) { }
          card.classList.add('is-dragging');
        });
        card.addEventListener('dragend', () => card.classList.remove('is-dragging'));
        card.addEventListener('dragover', (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; card.classList.add('is-drop-target'); });
        card.addEventListener('dragleave', () => card.classList.remove('is-drop-target'));
        card.addEventListener('drop', (e) => {
          e.preventDefault();
          const from = parseInt(e.dataTransfer.getData('text/plain'), 10);
          if (!Number.isInteger(from)) return;
          _qfMove(from, pos);
          _qfRenderSort();
        });
        list.appendChild(card);
      });
      body.appendChild(list);

      const skip = _qfEl('button', '', 'Skip');
      skip.type = 'button';
      skip.addEventListener('click', () => _qfClose(true));
      const done = _qfEl('button', 'primary', 'Done');
      done.type = 'button';
      done.addEventListener('click', () => _qfFinishSort(_qfRankingFromItems()));
      footer.append(skip, _qfEl('span', 'qf-spacer'), done);
    }

    function _qfFocus(pos) {
      const c = document.querySelector(`#qfBody .qf-card[data-pos="${pos}"]`);
      if (c) try { c.focus(); } catch (_) { }
    }

    function _qfFinishSort(ranking) {
      _qf.ranking = ranking;
      _qf.durationMs = Math.max(0, Math.round(performance.now() - _qf.started));
      if (_qf.scene) _qfAnswered.add(_qfSceneKey(_qf.scene));
      const mode = qfMode();
      if (mode === 'always') { _qfSubmit(false); return; }
      if (mode === 'never') { _qfClose(true); return; }
      _qfRenderConsent();
    }

    function _qfRenderConsent() {
      document.getElementById('qfTitle').textContent = 'Share these crops with Kestrel?';
      document.getElementById('qfSubtitle').textContent = '';
      const body = document.getElementById('qfBody');
      const footer = document.getElementById('qfFooter');
      body.replaceChildren();
      footer.replaceChildren();
      const p = _qfEl('p', 'qf-consent-text muted', 'Just these crops, your order and Kestrel’s scores. Anonymous. ');
      const link = _qfEl('a', 'qf-link', 'Privacy');
      link.href = QF_PRIVACY_URL;
      link.addEventListener('click', (e) => {
        e.preventDefault();
        try { window.pywebview?.api?.open_url?.(QF_PRIVACY_URL); } catch (_) { }
      });
      p.appendChild(link);
      const remember = _qfEl('label', 'inline qf-remember');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.id = 'qfRemember';
      remember.append(cb, _qfEl('span', 'muted', ' Don’t ask again'));
      body.append(p, remember);

      const notNow = _qfEl('button', '', 'No');
      notNow.type = 'button';
      notNow.addEventListener('click', () => {
        if (cb.checked) _qfSetMode('never');
        _qfClose(true);
      });
      const share = _qfEl('button', 'primary', 'Share');
      share.type = 'button';
      share.addEventListener('click', () => {
        if (cb.checked) _qfSetMode('always');
        _qfSubmit(true);
      });
      footer.append(notNow, _qfEl('span', 'qf-spacer'), share);
      try { share.focus(); } catch (_) { }
    }

    function _qfSetMode(mode) {
      if (!QF_MODES.includes(mode)) return;
      try { mergeSetting('quality_feedback_mode', mode); } catch (_) { }
      if (mode === 'never' && typeof _currentScene !== 'undefined' && _currentScene) {
        qfRenderSceneControl(_currentScene);
      }
    }

    async function _qfSubmit(consent) {
      if (!_qf) return;
      const payload = {
        token: _qf.token,
        ranking: _qf.ranking,
        thumbs: _qf.thumbs,
        duration_ms: _qf.durationMs,
        consent: !!consent,
      };
      _qf.submitted = true;
      const scene = _qf.scene;
      _qfClose(false);
      if (scene && typeof _currentScene !== 'undefined' && _currentScene && String(_currentScene.id) === String(scene.id)) {
        qfRenderSceneControl(_currentScene);
      }
      try {
        const r = await window.pywebview.api.submit_quality_feedback(payload);
        if (!r || !r.success) showToast('Couldn’t send feedback' + (r?.error ? ` (${r.error})` : ''), 4000);
      } catch (e) {
        showToast('Couldn’t send feedback', 4000);
      }
    }

    window.onQualityFeedbackResult = function (res) {
      if (res && res.ok) showToast('Thanks — feedback sent', 3000);
      else showToast('Couldn’t send feedback' + (res?.error ? ` (${res.error})` : '') + ' — nothing was saved.', 5000);
    };

    // Keys typed in the sort dialog must not reach the scene viewer's
    // document-level shortcuts (Z/X/C cull, arrows, Space → editor …).
    (function wireQfDialog() {
      const dlg = document.getElementById('qfDlg');
      if (!dlg) return;
      dlg.addEventListener('keydown', (e) => { e.stopPropagation(); });
      dlg.addEventListener('cancel', () => {
        if (_qf && !_qf.submitted && _qf.token) {
          try { window.pywebview?.api?.discard_quality_feedback?.(_qf.token); } catch (_) { }
        }
        _qf = null;
      });
      const logDlg = document.getElementById('qfLogDlg');
      if (logDlg) logDlg.addEventListener('keydown', (e) => { e.stopPropagation(); });
      document.getElementById('qfLogClose')?.addEventListener('click', () => logDlg.close());
    })();

    // ── Settings: view / forget ──

    async function qfShowLog() {
      const dlg = document.getElementById('qfLogDlg');
      const list = document.getElementById('qfLogList');
      list.replaceChildren();
      let entries = [];
      try {
        const r = await window.pywebview?.api?.get_quality_feedback_log?.();
        entries = (r && Array.isArray(r.entries)) ? r.entries : [];
      } catch (_) { }
      if (!entries.length) {
        list.appendChild(_qfEl('div', 'muted', 'Nothing shared from this computer yet.'));
      } else {
        for (const en of entries) {
          const row = _qfEl('div', 'qf-log-row');
          const when = _qfEl('span', 'qf-log-time', String(en.time || '').replace('T', ' ').replace('Z', ' UTC'));
          const sp = Array.isArray(en.species) && en.species.length ? en.species.join(', ') : 'Unknown species';
          const what = _qfEl('span', 'qf-log-what', `${sp} · ${en.n || '?'} crops`);
          row.append(when, what);
          list.appendChild(row);
        }
      }
      dlg.showModal();
    }

    async function qfForget() {
      if (!window.confirm('Delete all the quality feedback this computer has shared? This also resets your anonymous feedback id.')) return;
      try {
        const r = await window.pywebview?.api?.forget_quality_feedback?.();
        if (r && r.success) showToast(`Shared feedback deleted (${r.deleted || 0} submission${r.deleted === 1 ? '' : 's'}).`, 4000);
        else showToast('Couldn’t delete shared feedback' + (r?.error ? ` (${r.error})` : '') + '. Try again later.', 5000);
      } catch (_) {
        showToast('Couldn’t delete shared feedback. Try again later.', 5000);
      }
    }

    document.getElementById('settingsQfViewLog')?.addEventListener('click', (e) => { e.preventDefault(); qfShowLog(); });
    document.getElementById('settingsQfForget')?.addEventListener('click', (e) => { e.preventDefault(); qfForget(); });
