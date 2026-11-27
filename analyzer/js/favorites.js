// Favorites: a pink heart on a photo, kept in the folder database's
// `favorite` column ("1" or empty). Re-analysis keeps it the same way it
// keeps accept / reject (kestrel_analyzer/database.py _UI_PRESERVE_COLUMNS).
// A favorite is independent of accept / reject: setting one never flips the
// other. Used by the scene viewer (F, heart on filmstrip cards and the info
// bar) and by Cull mode.
(function () {
  const COL = 'favorite';
  const TEXT = {
    add: 'Favorite (F)',
    remove: 'Remove from favorites (F)',
    label: 'Favorite',
  };

  function isFavorite(row) {
    const v = String(row?.[COL] ?? '').trim().toLowerCase();
    return v === '1' || v === 'true' || v === 'yes';
  }

  function ensureColumn() {
    if (typeof header !== 'undefined' && Array.isArray(header) && !header.includes(COL)) header.push(COL);
  }

  function set(row, on) {
    if (!row) return false;
    ensureColumn();
    const next = on ? '1' : '';
    if ((row[COL] || '') === next) return false;
    row[COL] = next;
    if (typeof markDirty === 'function') markDirty(row.__rootPath || null);
    return true;
  }

  function toggle(row) {
    set(row, !isFavorite(row));
    return isFavorite(row);
  }

  function count(rowList) {
    let n = 0;
    for (const r of rowList || []) if (isFavorite(r)) n += 1;
    return n;
  }

  /** Sync a heart button's icon, colour and label to the row. */
  function syncButton(btn, row, withLabel = false) {
    if (!btn) return;
    const on = isFavorite(row);
    btn.classList.toggle('is-favorite', on);
    btn.title = on ? TEXT.remove : TEXT.add;
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    btn.replaceChildren(KestrelIcon.el(on ? 'heart-fill' : 'heart'));
    if (withLabel) {
      const span = document.createElement('span');
      span.textContent = TEXT.label;
      btn.appendChild(span);
    }
  }

  /**
   * Add the hover heart to a thumbnail card. Clicking it toggles the favorite
   * without selecting or flipping the photo; `onChange(row)` runs after.
   */
  function decorateCard(card, row, onChange) {
    if (!card || !row) return;
    card.classList.toggle('is-favorite', isFavorite(row));
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'fav-heart';
    syncButton(btn, row);
    const stop = (ev) => { ev.stopPropagation(); };
    btn.addEventListener('mousedown', stop);
    btn.addEventListener('dblclick', stop);
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      ev.preventDefault();
      toggle(row);
      card.classList.toggle('is-favorite', isFavorite(row));
      syncButton(btn, row);
      if (typeof onChange === 'function') onChange(row);
    });
    card.appendChild(btn);
  }

  // ── Scene viewer ──
  // The info-bar button is created here rather than in visualizer.html so the
  // feature stays in one file; it sits after the Quality readout.
  function _sceneViewerRow() {
    if (typeof _currentScene === 'undefined' || !_currentScene) return null;
    return _currentScene.images?.[currentImageIndex] || null;
  }

  function _sceneViewerButton() {
    let btn = document.getElementById('sceneInfoFavBtn');
    if (btn) return btn;
    const anchor = document.getElementById('sceneInfoQuality');
    if (!anchor) return null;
    btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'sceneInfoFavBtn';
    btn.className = 'scene-info-fav-btn';
    btn.addEventListener('click', (ev) => { ev.stopPropagation(); toggleSceneViewerCurrent(); });
    anchor.after(btn);
    return btn;
  }

  /** Refresh the info-bar heart and the active filmstrip card's heart. */
  function syncSceneViewerButton() {
    const row = _sceneViewerRow();
    syncButton(_sceneViewerButton(), row, true);
    const card = document.querySelector(`#imageGrid .filmstrip-card[data-idx="${currentImageIndex}"]`);
    if (card && row) {
      card.classList.toggle('is-favorite', isFavorite(row));
      syncButton(card.querySelector('.fav-heart'), row);
    }
  }

  function toggleSceneViewerCurrent() {
    const row = _sceneViewerRow();
    if (!row) return;
    toggle(row);
    syncSceneViewerButton();
  }

  window.KestrelFavorites = {
    COL, isFavorite, set, toggle, count, syncButton, decorateCard,
    syncSceneViewerButton, toggleSceneViewerCurrent,
  };
})();
