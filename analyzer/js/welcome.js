    // ====================================================================
    // Welcome panel: "What's New" banner + rotating tip carousel
    // ====================================================================

    // The in-app launch note. This is the BAKED-IN FALLBACK — at runtime the app
    // first tries to fetch the same shape from projectkestrel.org/whats-new.json
    // (via the fetch_remote_whats_new bridge method) so the copy can be edited
    // without shipping an update. Remote content is run through sanitizeRichText()
    // (tag allowlist) before it reaches innerHTML; the markup skeleton + welcome.css
    // always come from this build. Bump `version` to re-surface the note to
    // everyone (locally or by changing it in whats-new.json).
    //
    // Keep this object in sync with Kestrel Website/whats-new.json.
    const WHATS_NEW = {
      version: 'rufous-hummingbird',
      eyebrow: 'New in v(Rufous Hummingbird)',
      title: 'Introducing Perch &amp; Cloud Compute',
      subtitle: 'Celebrating 1.5M photos analyzed',
      greeting: 'Dear bird photographer,',
      intro: [
        "Five years ago, I realized almost all of my bird photography had been buried in an enormous, unsearchable archive.",
        "I’ve since finished an entire undergraduate engineering degree while building Project Kestrel in my afternoons and weekends. I never imagined it would analyze 1.5&nbsp;million photos — especially not within just three months.",
      ],
      announce: "Today, I’m announcing two new platforms.",
      cards: [
        {
          icon: '🪶',
          name: 'Perch',
          tag: 'Share entire birding outings with anyone',
          body: "Perch is a new way to share your bird photography, focused on the <i>experience</i> of your birding outing rather than the individual photos you capture. Click <b>“Share with Perch”</b> to upload your whole timeline, personalize it by adding notes, then share it with anyone you wish. Perch is free for everyone, with <b>3&nbsp;GB</b>, or ~15,000 photos, per account.",
          link: { label: 'See an example →', href: 'https://perch.projectkestrel.org/' },
        },
        {
          icon: '⚡',
          name: 'Cloud Compute',
          tag: 'Offload analysis to cloud GPUs',
          body: "Hand a big backlog to cloud GPUs and keep working while they crunch — ideal for slower laptops or high-volume shooters. It runs the exact same Kestrel pipeline, spread across up to 3 GPUs at once. New accounts start with <b>2,500 images</b> of credits to try.",
          hint: 'Find it in the Analyze Folders dialog.',
        },
      ],
      note: "I hope you’ll try them both. From one birder to another, thank you for using Project Kestrel — and <b>please</b> share your thoughts, stories, feedback, and bug reports through the in-app and in-account feedback forms.",
      blogLink: { label: 'Read the full blog →', href: 'https://projectkestrel.org/blogs/perch-cc-launch.html' },
      sign: '— Sanjay, Sole Developer &amp; Owner, Project Kestrel',
      // Kept for the website banner / changelog tooling.
      headline: "New in v(Rufous Hummingbird) — Perch & Cloud Compute are live!",
    };

    const WELCOME_TIPS = [
      {
        icon: '🦋',
        title: 'Try wildlife mode',
        badge: 'New!',
        body: 'Kestrel can detect squirrels, bears, and other wildlife in addition to birds. Enable it in <b>Settings &rarr; Analysis</b>.',
        action: { label: 'Open Settings', onClick: function() { try { showSettings(); } catch (_) {} } },
      },
      {
        icon: '➕',
        title: 'Merge scenes',
        body: 'Hold <kbd>Ctrl</kbd> and click scene cards to select multiple, then click <b>Merge selected scenes</b> to combine them into one.',
      },
      {
        icon: '🖼️',
        title: 'Multiple birds in one scene?',
        body: 'Use the <b>◂ / ▸ crop buttons</b> in the scene info bar (or <kbd>↑</kbd> / <kbd>↓</kbd>) to flip through each bird, and press <kbd>Enter</kbd> to promote one as the scene’s primary.',
      },
      {
        icon: '📂',
        title: 'Open a parent folder once',
        body: 'Kestrel searches across <b>every loaded folder</b>. Open a parent folder and browse your entire library by species or family without re-loading anything.',
      },
      {
        icon: '⌨️',
        title: 'Space = open in editor',
        body: 'Press <kbd>Space</kbd> on any photo to open the original in your chosen photo editor. Set your preference in Settings.',
        action: { label: 'Open Settings', onClick: function() { try { showSettings(); } catch (_) {} } },
      },
      {
        icon: '🗑️',
        title: 'Cull faster with the Culling Assistant',
        body: 'Batch Accept / Reject photos by your own quality rules — and optionally move rejects into an archive folder.',
      },
      {
        icon: '⚠️',
        title: 'Write metadata before importing',
        body: '<b>Write Photo Metadata</b> before importing into Lightroom or Capture One — most catalogues ignore sidecar files added <i>after</i> import.',
      },
      {
        icon: '⭐',
        title: 'Not satisfied with the star ratings?',
        body: 'Tune the rating thresholds and profile in Settings. Your manual overrides are always saved as gold stars.',
        action: { label: 'Open Settings', onClick: function() { try { showSettings(); } catch (_) {} } },
      },
    ];

    // Sanitize a developer-authored rich-text string so it is safe to drop into
    // innerHTML even when it arrives from the network (whats-new.json). Only a
    // tiny formatting allowlist survives; everything else is downgraded to text.
    // <a> is forced to https + safe rel/target. The markup *structure* around
    // these values is always local (see below), so this guards the one place
    // remote content reaches the DOM.
    function sanitizeRichText(html) {
      var allowed = { B: [], STRONG: [], I: [], EM: [], BR: [], A: ['href'] };
      var tpl = document.createElement('template');
      tpl.innerHTML = String(html == null ? '' : html);
      (function walk(node) {
        var child = node.firstChild;
        while (child) {
          var next = child.nextSibling;
          if (child.nodeType === 1) { // element
            var tag = child.tagName;
            if (!Object.prototype.hasOwnProperty.call(allowed, tag)) {
              child.replaceWith(document.createTextNode(child.textContent));
            } else {
              Array.prototype.slice.call(child.attributes).forEach(function(a) {
                if (allowed[tag].indexOf(a.name.toLowerCase()) === -1) child.removeAttribute(a.name);
              });
              if (tag === 'A') {
                var href = child.getAttribute('href') || '';
                if (!/^https:\/\//i.test(href)) child.removeAttribute('href');
                child.setAttribute('target', '_blank');
                child.setAttribute('rel', 'noopener noreferrer');
              }
              walk(child);
            }
          } else if (child.nodeType === 8) { // comment
            child.remove();
          }
          child = next;
        }
      })(tpl.content);
      return tpl.innerHTML;
    }
    var sr = sanitizeRichText;

    // Escape a string for use inside a DOUBLE-QUOTED HTML attribute. sr()/
    // sanitizeRichText is a *content* sanitizer (it does NOT escape quotes), so
    // it must never be used for attribute values — that would allow a remote
    // href like `https://x" onmouseover="..."` to inject an event handler.
    function escapeAttr(s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // Return a normalized https:// URL, or null if it is not valid https.
    // Parsing with URL() (not a prefix regex) rejects embedded quotes,
    // whitespace, and non-https schemes.
    function safeHttpsUrl(u) {
      try {
        var parsed = new URL(String(u == null ? '' : u));
        if (parsed.protocol === 'https:') return parsed.href;
      } catch (_) {}
      return null;
    }

    // Validate a candidate note (local or remote) has the minimum shape.
    function isValidNote(n) {
      return !!(n && typeof n.version === 'string' && n.version && n.title &&
                Array.isArray(n.cards) && n.cards.length);
    }

    // Build the note's inner HTML. Every value is sanitized; the surrounding
    // structure is always local, so this is safe for remote content too.
    function buildNoteHTML(note) {
      var intro = (note.intro || []).map(function(p) {
        return '<p class="wln-intro">' + sr(p) + '</p>';
      }).join('');

      var cards = (note.cards || []).map(function(c) {
        var cta = '';
        var cardHref = c.link && c.link.href ? safeHttpsUrl(c.link.href) : null;
        if (cardHref) {
          cta = '<a class="wln-card-link" href="' + escapeAttr(cardHref) +
            '" target="_blank" rel="noopener noreferrer">' + sr(c.link.label || 'Learn more →') + '</a>';
        } else if (c.hint) {
          cta = '<div class="wln-card-hint">' + sr(c.hint) + '</div>';
        }
        return '' +
          '<div class="wln-card">' +
            '<div class="wln-card-top">' +
              '<span class="wln-card-icon">' + sr(c.icon || '•') + '</span>' +
              '<div class="wln-card-heading">' +
                '<div class="wln-card-name">' + sr(c.name || '') + '</div>' +
                '<div class="wln-card-tag">' + sr(c.tag || '') + '</div>' +
              '</div>' +
            '</div>' +
            '<p class="wln-card-body">' + sr(c.body || '') + '</p>' +
            cta +
          '</div>';
      }).join('');

      var blogBtn = '';
      var blogHref = note.blogLink && note.blogLink.href ? safeHttpsUrl(note.blogLink.href) : null;
      if (blogHref) {
        blogBtn = '<a class="wln-blog-link" href="' + escapeAttr(blogHref) +
          '" target="_blank" rel="noopener noreferrer">' + sr(note.blogLink.label || 'Read more →') + '</a>';
      }

      return '' +
        '<div class="wln">' +
          '<button type="button" class="wln-dismiss" id="wwnDismiss" aria-label="Dismiss">✕</button>' +
          '<span class="wln-eyebrow">🎉&nbsp; ' + sr(note.eyebrow || '') + '</span>' +
          '<h2 class="wln-title">' + sr(note.title || '') + '</h2>' +
          (note.subtitle ? '<div class="wln-subtitle">' + sr(note.subtitle) + '</div>' : '') +
          (note.greeting ? '<p class="wln-greeting">' + sr(note.greeting) + '</p>' : '') +
          intro +
          (note.announce ? '<p class="wln-announce">' + sr(note.announce) + '</p>' : '') +
          '<div class="wln-cards">' + cards + '</div>' +
          '<div class="wln-note">' + sr(note.note || '') +
            (note.sign ? '<span class="wln-sign">' + sr(note.sign) + '</span>' : '') +
          '</div>' +
          blogBtn +
        '</div>';
    }

    (function setupWelcomeWhatsNew() {
      var banner = document.getElementById('welcomeWhatsNew');
      if (!banner) return;

      var userDismissed = false;   // once the user closes it, never re-show this session
      var renderedHTML = null;     // what's currently in the DOM (skip identical re-renders)
      var lastSeen = null;         // the dismissed version, read once below

      function showNote(note, html) {
        html = html || buildNoteHTML(note);
        banner.innerHTML = html;
        banner.classList.remove('hidden');
        renderedHTML = html;
        var dismiss = banner.querySelector('#wwnDismiss');
        if (dismiss) dismiss.addEventListener('click', async function() {
          userDismissed = true;
          banner.classList.add('hidden');
          renderedHTML = null;
          if (hasPywebviewApi) {
            try {
              var r2 = await window.pywebview.api.get_settings();
              var s = (r2 && r2.success ? r2.settings : {}) || {};
              s.last_seen_whats_new_version = note.version;
              await window.pywebview.api.save_settings_data(s);
            } catch (_) {}
          }
        });
      }

      function hideNote() {
        banner.classList.add('hidden');
        renderedHTML = null;
      }

      // Render (or hide) for a candidate note, honoring per-version gating.
      // No-ops if the user already dismissed, or if the rendered HTML is
      // identical (so a remote payload equal to the local one never flickers).
      // `allowHide` is only true for the local pass: a stale/rolled-back remote
      // payload must never hide a note the local copy legitimately showed.
      function reconcile(note, allowHide) {
        if (userDismissed || !isValidNote(note)) return;
        if (note.version !== lastSeen) {
          var html = buildNoteHTML(note);
          if (html !== renderedHTML) showNote(note, html);
        } else if (allowHide) {
          hideNote();
        }
      }

      (async function() {
        // 1) Read the gating setting (a fast LOCAL bridge call — no network).
        if (hasPywebviewApi) {
          try {
            var res = await window.pywebview.api.get_settings();
            lastSeen = res && res.settings && res.settings.last_seen_whats_new_version;
          } catch (_) {}
        }

        // 2) LOCAL-FIRST: render the baked-in note immediately so there is no
        //    flash of empty space while the network call is in flight.
        reconcile(WHATS_NEW, true);

        // 3) RECONCILE: fetch the remote copy in the background and only update
        //    the DOM if it differs (edited copy or a newer version). On any
        //    failure (slow/no wifi, bad endpoint, malformed) the local note
        //    simply stays put.
        if (hasPywebviewApi && window.pywebview.api.fetch_remote_whats_new) {
          window.pywebview.api.fetch_remote_whats_new().then(function(rr) {
            if (rr && rr.success) reconcile(rr.data);
          }).catch(function() {});
        }
      })();
    })();

    (function setupWelcomeTipCarousel() {
      var root = document.getElementById('welcomeTipCarousel');
      if (!root || !WELCOME_TIPS.length) return;
      var iconEl   = root.querySelector('#wtcIcon');
      var titleEl  = root.querySelector('#wtcTitle');
      var badgeEl  = root.querySelector('#wtcBadge');
      var bodyEl   = root.querySelector('#wtcBody');
      var actionEl = root.querySelector('#wtcAction');
      var dotsEl   = root.querySelector('#wtcDots');
      var cardEl   = root.querySelector('#wtcCard');
      var prevBtn  = root.querySelector('.wtc-prev');
      var nextBtn  = root.querySelector('.wtc-next');

      var idx = 0;
      var paused = false;
      var rotateTimer = null;

      dotsEl.innerHTML = '';
      WELCOME_TIPS.forEach(function(_, i) {
        var d = document.createElement('button');
        d.type = 'button';
        d.className = 'wtc-dot' + (i === 0 ? ' active' : '');
        d.setAttribute('aria-label', 'Tip ' + (i + 1));
        d.addEventListener('click', function() { goTo(i, true); });
        dotsEl.appendChild(d);
      });

      function render(i) {
        var t = WELCOME_TIPS[i];
        if (!t) return;
        iconEl.textContent = t.icon || '💡';
        titleEl.innerHTML = t.title || '';
        if (t.badge) { badgeEl.textContent = t.badge; badgeEl.classList.remove('hidden'); }
        else badgeEl.classList.add('hidden');
        bodyEl.innerHTML = t.body || '';
        if (t.action && t.action.label) {
          actionEl.innerHTML = '';
          var btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'wtc-link';
          btn.textContent = t.action.label;
          btn.addEventListener('click', function(ev) { ev.preventDefault(); if (typeof t.action.onClick === 'function') t.action.onClick(); });
          actionEl.appendChild(btn);
          actionEl.classList.remove('hidden');
        } else {
          actionEl.classList.add('hidden');
          actionEl.innerHTML = '';
        }
        Array.prototype.forEach.call(dotsEl.children, function(d, di) {
          d.classList.toggle('active', di === i);
        });
      }

      function goTo(i, manual) {
        idx = (i + WELCOME_TIPS.length) % WELCOME_TIPS.length;
        cardEl.classList.remove('wtc-in');
        cardEl.classList.add('wtc-out');
        setTimeout(function() {
          render(idx);
          cardEl.classList.remove('wtc-out');
          cardEl.classList.add('wtc-in');
        }, 180);
        if (manual) resetTimer();
      }

      function next() { goTo(idx + 1, false); }
      function prev() { goTo(idx - 1, false); }

      function resetTimer() {
        if (rotateTimer) clearInterval(rotateTimer);
        rotateTimer = setInterval(function() { if (!paused) next(); }, 7000);
      }

      prevBtn.addEventListener('click', function() { goTo(idx - 1, true); });
      nextBtn.addEventListener('click', function() { goTo(idx + 1, true); });
      root.addEventListener('mouseenter', function() { paused = true; });
      root.addEventListener('mouseleave', function() { paused = false; });

      render(0);
      resetTimer();
    })();


    // Wire donation dialog buttons — this script runs after the dialog HTML is in the DOM
    (function() {
      var dlg = document.getElementById('donateDlg');
      document.getElementById('donateDlgGoBtn').addEventListener('click', function() {
        dlg.close();
        openDonateLink();
      });
      document.getElementById('donateDlgFeedbackBtn').addEventListener('click', function() {
        dlg.close();
        setTimeout(function() {
          document.getElementById('feedbackDlg').showModal();
        }, 150);
      });
      document.getElementById('donateDlgClose').addEventListener('click', function() {
        dlg.close();
      });
    })();
