    // ─── Quality feedback ──────────────────────────────────────────────────────
    // "Did Kestrel sort this burst's sharpness well? 👍 👎" in the scene viewer.
    // 👎 opens a dialog where the user drags 2–5 bird crops of the burst into
    // order from sharpest to blurriest; 👍 offers the same dialog as optional.
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
        t.textContent = 'Thanks for the feedback!';
        box.appendChild(t);
        return;
      }
      const label = document.createElement('span');
      label.className = 'scene-qf-label';
      label.textContent = "Did Kestrel sort this burst's sharpness well?";
      box.appendChild(label);
      box.appendChild(_qfButton('👍', 'Yes, the sharpness sort looks right', () => {
        _qfAnswered.add(_qfSceneKey(scene));
        box.replaceChildren();
        const t = document.createElement('span');
        t.className = 'scene-qf-label';
        t.textContent = 'Thanks! Want to help more? ';
        const a = document.createElement('a');
        a.href = '#';
        a.className = 'scene-qf-link';
        a.textContent = 'Sort a few frames';
        a.addEventListener('click', (e) => { e.preventDefault(); a.blur(); qfOpenSortDialog(scene, 'up'); });
        t.appendChild(a);
        box.appendChild(t);
      }));
      box.appendChild(_qfButton('👎', 'No — help Kestrel by sorting a few frames', () => {
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
        items: res.frames.map(f => ({ index: f.index, tie: false })),  // current order
        selected: null,
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
      const groups = [];
      for (const it of _qf.items) {
        if (it.tie && groups.length) groups[groups.length - 1].push(it.index);
        else groups.push([it.index]);
      }
      return groups;
    }

    function _qfMove(from, to) {
      const items = _qf.items;
      if (from === to || from < 0 || to < 0 || from >= items.length || to >= items.length) return;
      // Ties are relative to the card above, so clear the ones a move breaks.
      if (items[from + 1]) items[from + 1].tie = false;
      const [it] = items.splice(from, 1);
      it.tie = false;
      items.splice(to, 0, it);
      if (items[to + 1]) items[to + 1].tie = false;
      if (items[0]) items[0].tie = false;
    }

    function _qfRenderSort() {
      const title = document.getElementById('qfTitle');
      const sub = document.getElementById('qfSubtitle');
      const body = document.getElementById('qfBody');
      const footer = document.getElementById('qfFooter');
      body.replaceChildren();
      footer.replaceChildren();
      sub.textContent = 'Judge focus only — the sharpest, most in-focus bird first. Pose and composition don’t count.';
      const skip = _qfEl('button', '', 'Skip');
      skip.type = 'button';
      skip.addEventListener('click', () => _qfClose(true));

      if (_qf.items.length === 2) {
        title.textContent = 'Which is sharper?';
        const row = _qfEl('div', 'qf-pair');
        for (const it of _qf.items) {
          const card = _qfEl('button', 'qf-card qf-card--choice');
          card.type = 'button';
          card.title = 'This one is sharper';
          card.setAttribute('aria-label', 'This one is sharper');
          card.appendChild(_qfImg(it.index));
          card.appendChild(_qfEl('span', 'qf-card-cta', 'This one is sharper'));
          card.addEventListener('click', () => {
            const other = _qf.items.find(o => o.index !== it.index);
            _qfFinishSort([[it.index], [other.index]]);
          });
          row.appendChild(card);
        }
        body.appendChild(row);
        const tie = _qfEl('button', '', 'Too close to call');
        tie.type = 'button';
        tie.addEventListener('click', () => _qfFinishSort([_qf.items.map(i => i.index)]));
        footer.append(skip, _qfEl('span', 'qf-spacer'), tie);
        return;
      }

      title.textContent = 'Help Kestrel learn: drag these into order from sharpest to blurriest';
      const ends = _qfEl('div', 'qf-ends');
      ends.append(_qfEl('span', '', '◂ Sharpest'), _qfEl('span', '', 'Blurriest ▸'));
      const list = _qfEl('div', 'qf-list');
      list.setAttribute('role', 'list');
      list.setAttribute('aria-label', 'Frames, sharpest first');
      const groups = _qfRankingFromItems();
      const rankOf = new Map();
      groups.forEach((g, gi) => g.forEach(ix => rankOf.set(ix, gi + 1)));

      _qf.items.forEach((it, pos) => {
        const card = _qfEl('div', 'qf-card');
        card.setAttribute('role', 'listitem');
        card.tabIndex = 0;
        card.draggable = true;
        card.dataset.pos = String(pos);
        if (_qf.selected === pos) card.classList.add('is-selected');
        if (it.tie) card.classList.add('is-tied');
        card.setAttribute('aria-label', `Position ${pos + 1} of ${_qf.items.length}${it.tie ? ', tied with the previous frame' : ''}. Arrow keys move it; T ties it with the previous frame.`);

        const head = _qfEl('div', 'qf-card-head');
        head.appendChild(_qfEl('span', 'qf-rank', it.tie ? `= ${rankOf.get(it.index)}` : String(rankOf.get(it.index))));
        const moves = _qfEl('span', 'qf-moves');
        const left = _qfEl('button', 'qf-mini', '◂');
        left.type = 'button'; left.title = 'Move toward sharpest'; left.setAttribute('aria-label', left.title);
        left.disabled = pos === 0;
        left.addEventListener('click', (e) => { e.stopPropagation(); _qfMove(pos, pos - 1); _qf.selected = null; _qfRenderSort(); _qfFocus(pos - 1); });
        const right = _qfEl('button', 'qf-mini', '▸');
        right.type = 'button'; right.title = 'Move toward blurriest'; right.setAttribute('aria-label', right.title);
        right.disabled = pos === _qf.items.length - 1;
        right.addEventListener('click', (e) => { e.stopPropagation(); _qfMove(pos, pos + 1); _qf.selected = null; _qfRenderSort(); _qfFocus(pos + 1); });
        moves.append(left, right);
        head.appendChild(moves);
        card.appendChild(head);
        card.appendChild(_qfImg(it.index));

        if (pos > 0) {
          const tie = _qfEl('button', 'qf-tie', it.tie ? '= Too close to call' : 'Too close to call');
          tie.type = 'button';
          tie.title = 'Tie this frame with the one before it';
          tie.setAttribute('aria-pressed', it.tie ? 'true' : 'false');
          tie.addEventListener('click', (e) => { e.stopPropagation(); it.tie = !it.tie; _qfRenderSort(); _qfFocus(pos); });
          card.appendChild(tie);
        } else {
          card.appendChild(_qfEl('div', 'qf-tie-spacer'));
        }

        // Click to pick up, click another card to place it there.
        card.addEventListener('click', () => {
          if (_qf.selected === null) _qf.selected = pos;
          else if (_qf.selected === pos) _qf.selected = null;
          else { _qfMove(_qf.selected, pos); _qf.selected = null; }
          _qfRenderSort();
          _qfFocus(pos);
        });
        card.addEventListener('keydown', (e) => {
          const k = e.key;
          if (k === 'ArrowLeft' || k === 'ArrowUp') { e.preventDefault(); if (pos > 0) { _qfMove(pos, pos - 1); _qfRenderSort(); _qfFocus(pos - 1); } }
          else if (k === 'ArrowRight' || k === 'ArrowDown') { e.preventDefault(); if (pos < _qf.items.length - 1) { _qfMove(pos, pos + 1); _qfRenderSort(); _qfFocus(pos + 1); } }
          else if ((k === 't' || k === 'T') && pos > 0) { e.preventDefault(); it.tie = !it.tie; _qfRenderSort(); _qfFocus(pos); }
          else if (k === 'Enter' || k === ' ') { e.preventDefault(); card.click(); }
        });
        // HTML5 drag and drop.
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
          _qf.selected = null;
          _qfRenderSort();
        });
        list.appendChild(card);
      });
      body.append(ends, list);
      body.appendChild(_qfEl('div', 'qf-hint', 'Drag a frame, or click one and then click where it goes. Arrow keys move the focused frame.'));

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
      document.getElementById('qfTitle').textContent = 'Share these crops and your ranking to improve Kestrel’s quality sorting?';
      document.getElementById('qfSubtitle').textContent = '';
      const body = document.getElementById('qfBody');
      const footer = document.getElementById('qfFooter');
      body.replaceChildren();
      footer.replaceChildren();
      const p1 = _qfEl('p', 'qf-consent-text',
        'Sent: the bird crops only (not your full photos, no location or EXIF data), your ranking, Kestrel’s scores, the species, the app version and an anonymous install id.');
      const p2 = _qfEl('p', 'qf-consent-text muted', 'It isn’t linked to your account, and you can delete it any time from Settings. ');
      const link = _qfEl('a', 'qf-link', 'Privacy policy');
      link.href = QF_PRIVACY_URL;
      link.addEventListener('click', (e) => {
        e.preventDefault();
        try { window.pywebview?.api?.open_url?.(QF_PRIVACY_URL); } catch (_) { }
      });
      p2.appendChild(link);
      const thumbs = _qfEl('div', 'qf-consent-thumbs');
      for (const f of _qf.frames) {
        const t = _qfImg(f.index);
        t.className = 'qf-crop qf-crop--thumb';
        thumbs.appendChild(t);
      }
      const remember = _qfEl('label', 'inline qf-remember');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.id = 'qfRemember';
      remember.append(cb, _qfEl('span', 'muted', ' Remember my choice'));
      body.append(thumbs, p1, p2, remember);

      const notNow = _qfEl('button', '', 'Not now');
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
          const what = _qfEl('span', 'qf-log-what', `${sp} — ${en.n || '?'} crops, ${en.thumbs === 'up' ? '👍' : '👎'}`);
          const rank = Array.isArray(en.ranking)
            ? en.ranking.map(g => Array.isArray(g) ? g.join(' = ') : String(g)).join(' > ')
            : '';
          row.append(when, what, _qfEl('span', 'qf-log-rank muted', rank ? `Ranking (crop #): ${rank}` : ''));
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
