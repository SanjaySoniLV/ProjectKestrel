// Desktop notification bell (H6).
//
// The bell lives in the app header; the notification store lives on the Auth
// Worker. We proxy every call through Python (api_bridge.get_notifications /
// mark_notification_read / mark_all_notifications_read / hide_notification) so
// the Clerk JWT (in the OS keychain) is never handed to the webview. Bodies are
// rendered with the strict KestrelNotifyMarkdown renderer (escape all HTML, only
// [text](url) links); links open in the system browser via open_url.
//
// auth.js drives visibility: KestrelNotifications.onAuthState(signedIn) is called
// on startup, after sign-in, and after sign-out.
(function () {
  let _wired = false;
  let _signedIn = false;

  function api() { return window.pywebview && window.pywebview.api; }
  function $(id) { return document.getElementById(id); }
  function escAttr(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/"/g, '&quot;')
      .replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/'/g, '&#39;');
  }

  function relTime(sec) {
    if (!sec) return '';
    const d = Math.floor(Date.now() / 1000) - sec;
    if (d < 60) return 'just now';
    if (d < 3600) return Math.floor(d / 60) + 'm ago';
    if (d < 86400) return Math.floor(d / 3600) + 'h ago';
    if (d < 2592000) return Math.floor(d / 86400) + 'd ago';
    return new Date(sec * 1000).toLocaleDateString();
  }

  function setBadge(n) {
    const badge = $('notifBadge');
    if (!badge) return;
    if (n > 0) {
      badge.textContent = n > 99 ? '99+' : String(n);
      badge.classList.remove('hidden');
    } else {
      badge.classList.add('hidden');
    }
  }
  function bumpBadge(delta) {
    const badge = $('notifBadge');
    if (!badge) return;
    let n = parseInt(badge.textContent, 10);
    if (isNaN(n)) n = 0;
    setBadge(Math.max(0, n + delta));
  }

  function render(items, unread) {
    const list = $('notifList');
    setBadge(unread);
    const readAll = $('notifReadAll');
    if (readAll) readAll.style.display = unread > 0 ? '' : 'none';
    if (!list) return;
    if (!items.length) {
      list.innerHTML = '<div class="notif-empty">You’re all caught up.</div>';
      return;
    }
    const md = window.KestrelNotifyMarkdown;
    list.innerHTML = items.map((n) => {
      const body = md ? md.render(n.bodyMd) : '';
      const unreadCls = n.readAt ? '' : ' notif-unread';
      const safeId = String(n.id).replace(/[^A-Za-z0-9_-]/g, '');
      // Deep-link CTA (e.g. a support reply → the ticket thread). Reuse the
      // markdown renderer's allowlist (https projectkestrel.org / mailto only);
      // the existing a[href] click handler opens it in the system browser.
      const action = (n.actionUrl && md && md.isAllowedHref(n.actionUrl)) ? String(n.actionUrl) : '';
      const cta = action
        ? '<a class="notif-action" href="' + escAttr(action) + '">View &rarr;</a>'
        : '';
      return '<div class="notif-item' + unreadCls + '" data-id="' + safeId + '">'
        + '<div class="notif-bodywrap"><div class="notif-body">' + body + '</div>'
        + '<div class="notif-time">' + relTime(n.createdAt) + '</div>'
        + cta
        + '</div>'
        + '<button type="button" class="notif-hide" aria-label="Dismiss" title="Dismiss">×</button>'
        + '</div>';
    }).join('');
  }

  async function load(force) {
    const a = api();
    if (!a || !a.get_notifications) return;
    try {
      const res = await a.get_notifications();
      if (!res || !res.success) {
        if (force) console.warn('[notif] load failed', res && res.error);
        return;
      }
      render(res.notifications || [], res.unreadCount || 0);
    } catch (e) {
      if (force) console.warn('[notif] load error', e);
    }
  }

  // Anchor the fixed-position panel under the bell, keeping it on-screen.
  // Fixed positioning lets it escape the header sidebar's overflow:hidden clip.
  function positionPanel() {
    const p = $('notifPanel'), b = $('notifBell');
    if (!p || !b || p.classList.contains('hidden')) return;
    const rect = b.getBoundingClientRect();
    const margin = 16;
    const pw = p.offsetWidth || 340;
    // Right-align the panel to the bell, then clamp into the viewport.
    let left = rect.right - pw;
    left = Math.min(left, window.innerWidth - pw - margin);
    left = Math.max(margin, left);
    p.style.left = left + 'px';
    p.style.top = (rect.bottom + 8) + 'px';
  }

  function openPanel() {
    const p = $('notifPanel'), b = $('notifBell');
    if (!p) return;
    p.classList.remove('hidden');
    positionPanel();
    if (b) b.setAttribute('aria-expanded', 'true');
    load(true);
  }
  function closePanel() {
    const p = $('notifPanel'), b = $('notifBell');
    if (!p) return;
    p.classList.add('hidden');
    if (b) b.setAttribute('aria-expanded', 'false');
  }

  function wire() {
    if (_wired) return;
    const wrap = $('notifWrap'), bell = $('notifBell'), panel = $('notifPanel'), list = $('notifList');
    if (!wrap || !bell || !panel || !list) return;
    _wired = true;

    bell.addEventListener('click', (e) => {
      e.stopPropagation();
      if (panel.classList.contains('hidden')) openPanel();
      else closePanel();
    });
    document.addEventListener('click', (e) => {
      if (!wrap.contains(e.target)) closePanel();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closePanel();
    });
    window.addEventListener('focus', () => { if (_signedIn) load(false); });
    window.addEventListener('resize', positionPanel);

    const readAll = $('notifReadAll');
    if (readAll) {
      readAll.addEventListener('click', async (e) => {
        e.preventDefault();
        const a = api();
        if (!a || !a.mark_all_notifications_read) return;
        try {
          await a.mark_all_notifications_read();
          document.querySelectorAll('.notif-item.notif-unread')
            .forEach((el) => el.classList.remove('notif-unread'));
          setBadge(0);
          readAll.style.display = 'none';
        } catch (_) { /* best-effort */ }
      });
    }

    list.addEventListener('click', async (e) => {
      const a = api();
      const link = e.target.closest('a[href]');
      if (link) {
        // Open notification links in the system browser, not the webview.
        e.preventDefault();
        if (a && a.open_url) { try { a.open_url(link.getAttribute('href')); } catch (_) {} }
      }
      const item = e.target.closest('.notif-item');
      if (!item) return;
      const id = item.getAttribute('data-id');
      if (e.target.closest('.notif-hide')) {
        e.preventDefault();
        e.stopPropagation();
        const wasUnread = item.classList.contains('notif-unread');
        try { if (a && a.hide_notification) await a.hide_notification(id); } catch (_) {}
        item.remove();
        if (wasUnread) bumpBadge(-1);
        if (!list.querySelector('.notif-item')) {
          list.innerHTML = '<div class="notif-empty">You’re all caught up.</div>';
        }
        return;
      }
      if (item.classList.contains('notif-unread')) {
        try { if (a && a.mark_notification_read) await a.mark_notification_read(id); } catch (_) {}
        item.classList.remove('notif-unread');
        bumpBadge(-1);
      }
    });
  }

  // Called by auth.js on every auth-state transition.
  function onAuthState(signedIn) {
    _signedIn = !!signedIn;
    const wrap = $('notifWrap');
    if (wrap) wrap.classList.toggle('hidden', !_signedIn);
    if (_signedIn) {
      wire();
      load(false);
    } else {
      closePanel();
      setBadge(0);
    }
  }

  window.KestrelNotifications = { onAuthState: onAuthState, refresh: () => load(true) };
})();
