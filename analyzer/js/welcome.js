    // ====================================================================
    // Welcome panel: "What's New" banner + rotating tip carousel
    // ====================================================================

    // The in-app "What's New" note shown on the welcome screen. It ships baked
    // into the build: bump `version` to re-surface it to everyone, and it is
    // dismissed per-version via the `last_seen_whats_new_version` setting. It
    // is never fetched from the network.
    const WHATS_NEW = {
      version: 'arctic-tern-fp-3',
      eyebrow: 'v(Arctic Tern (FP 3))',
      // The release's namesake bird. `whatsnew-bird.jpg` is a STABLE filename
      // whose contents are replaced each release, so the three PyInstaller
      // specs list it once and never need touching again. Update `heroAlt`
      // with the new bird — it is the only per-release part.
      heroImg: 'whatsnew-bird.jpg',
      heroAlt: 'Arctic Tern',
      title: 'What’s New in Project Kestrel',
      subtitle: 'Updates across Project Kestrel, Perch & Cloud Compute',
      cards: [
        {
          // Desktop app logo — bundled at the static-server root (see .spec datas).
          iconImg: 'logo.png',
          name: 'Project Kestrel',
          tag: 'New in v(Arctic Tern (FP 3))',
          bullets: [
            '<b>Cull mode</b> takes you through every scene full screen. Kestrel’s starting picks show dotted until you confirm them: press Enter to confirm and move on, or Tab to skip.',
            'Cull a folder as many times as you like. <b>Finish</b> moves the rejects aside and offers a stricter next pass, and <b>Culling history</b> can undo a move or revert to any earlier pass.',
            'Press <b>F</b> to favorite a photo. When a photo has several birds, click one to see its close-up, and press <b>S</b> to focus the whole scene on it.',
            '<b>Custom keyboard shortcuts</b>: change your keys in Settings, or pick a right-hand or left-hand layout so you can cull with one hand on the keyboard.',
            'A <b>fresh new look</b>: a cleaner main window and new icons, plus simpler Settings, a three-step Analyze dialog and clearer Folder settings and Feedback dialogs.',
          ],
        },
        {
          // Perch logo (rufous hummingbird) — bundled at the static-server root.
          iconImg: 'perch-logo.png',
          name: 'Perch',
          tag: 'What’s new',
          bullets: [
            'Changes to Perch are coming soon. There’s a new <b>feedback button</b> in Perch, and I’d love to hear your feedback on how we can make Perch better!',
          ],
          links: [
            { label: 'Learn about Perch →', href: 'https://perch.projectkestrel.org/' },
          ],
        },
        {
          icon: 'bolt',
          name: 'Cloud Compute',
          tag: 'What’s new',
          bullets: [
            'Cloud Compute has been updated to <b>analysis version 3.0</b>.',
            // The Mac App Store build hides in-app prices, so it doesn't get this bullet.
            { html: 'Cloud Compute <b>timing and pricing</b> options are now available in the app.', hideOnChannel: 'appstore' },
          ],
          hint: 'Find it in the Analyze Folders dialog.',
        },
      ],
      note: 'Thank you for supporting the solo dev of Project Kestrel.',
      sign: '— Sanjay',
      blogLink: { label: 'Read the full v(Arctic Tern (FP 3)) release notes →', href: 'https://projectkestrel.org/notes/arctic-tern-fp-3/' },
    };

    const WELCOME_TIPS = [
      {
        icon: 'paw',
        title: 'Try wildlife mode',
        badge: 'New!',
        body: 'Kestrel can detect squirrels, bears, and other wildlife as well as birds. Turn it on when you analyze a folder: the <b>Analyze</b> dialog asks what to look for.',
        action: { label: 'Analyze folders…', onClick: function() { document.getElementById('analyzeQueueBtn')?.click(); } },
      },
      {
        icon: 'plus',
        title: 'Merge scenes',
        body: 'Hold <kbd>Ctrl</kbd> and click scene cards to select multiple, then click <b>Merge into one scene</b> to combine them into one.',
      },
      {
        icon: 'image',
        title: 'Multiple birds in one scene?',
        body: 'Use the <b>◂ / ▸ crop buttons</b> in the scene info bar (or <kbd>↑</kbd> / <kbd>↓</kbd>) to flip through each bird, and press <kbd>Enter</kbd> to promote one as the scene’s primary.',
      },
      {
        icon: 'folder-open',
        title: 'Open a parent folder once',
        body: 'Kestrel searches across <b>every loaded folder</b>. Open a parent folder and browse your entire library by species or family without re-loading anything.',
      },
      {
        icon: 'keyboard',
        title: 'Space = open in editor',
        body: 'Press <kbd>Space</kbd> on any photo to open the original in your photo editor. Choose the editor in <b>Settings &rarr; General</b>.',
        action: { label: 'Open Settings', onClick: function() { try { showSettings('general'); } catch (_) {} } },
      },
      {
        icon: 'trash',
        title: 'Cull faster in Cull mode',
        body: 'Accept or reject every photo scene by scene, starting from picks Kestrel makes by quality, and move the rejects aside.',
      },
      {
        icon: 'warning',
        title: 'Write metadata before importing',
        body: '<b>Write Photo Metadata</b> before importing into Lightroom or Capture One — most catalogues ignore sidecar files added <i>after</i> import.',
      },
      {
        icon: 'star',
        title: 'Not satisfied with the star ratings?',
        body: 'Choose a stricter or more lenient rating style in <b>Settings &rarr; Star ratings</b>. Stars you set yourself are always kept, shown in gold.',
        action: { label: 'Open Settings', onClick: function() { try { showSettings('ratings'); } catch (_) {} } },
      },
    ];

    // Sanitize a developer-authored rich-text string so it is safe to drop into
    // innerHTML. Only a tiny formatting allowlist survives; everything else is
    // downgraded to text, and <a> is forced to https + safe rel/target. The note
    // content is build-local, so this is defense-in-depth rather than a hard
    // boundary — but it keeps the one innerHTML path honest.
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
    // Attribute values go through escapeHtml (it escapes quotes), never through
    // sanitizeRichText, which is a content sanitizer and leaves quotes alone: a
    // remote href like `https://x" onmouseover="..."` would inject a handler.

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

    // A card icon image must be a bundled asset served from the static-server
    // root: a bare filename with an image extension, no path separators, scheme,
    // or traversal. Intentionally strict — only same-origin bundled logos load.
    function safeAssetFile(f) {
      var s = String(f == null ? '' : f);
      return /^[a-z0-9][a-z0-9._-]*\.(png|svg|webp|jpe?g)$/i.test(s) ? s : null;
    }

    // Build the note's inner HTML from the baked-in WHATS_NEW shape. A bullet is
    // either an HTML string or {html, hideOnChannel}, which is left out on that
    // distribution channel ('appstore' = the Mac App Store build).
    function buildNoteHTML(note, channel) {
      var cards = (note.cards || []).map(function(c) {
        var cta = '';
        // A card may carry a single `link` or a `links[]` array (one or more CTAs).
        var linkList = Array.isArray(c.links) ? c.links : (c.link ? [c.link] : []);
        var linkHtml = linkList.map(function(lk) {
          var href = lk && lk.href ? safeHttpsUrl(lk.href) : null;
          if (!href) return '';
          return '<a class="wln-card-link" href="' + escapeHtml(href) +
            '" target="_blank" rel="noopener noreferrer">' + sr(lk.label || 'Learn more →') + '</a>';
        }).filter(Boolean).join('');
        cta = (c.hint ? '<div class="wln-card-hint">' + sr(c.hint) + '</div>' : '') +
              (linkHtml ? '<div class="wln-card-links">' + linkHtml + '</div>' : '');
        var bodyHtml = '';
        if (Array.isArray(c.bullets) && c.bullets.length) {
          bodyHtml = '<ul class="wln-card-list">' +
            c.bullets
              .filter(function(b) { return !(b && typeof b === 'object' && b.hideOnChannel && b.hideOnChannel === channel); })
              .map(function(b) { return '<li>' + sr(b && typeof b === 'object' ? b.html : b) + '</li>'; }).join('') +
            '</ul>';
        } else if (c.body) {
          bodyHtml = '<p class="wln-card-body">' + sr(c.body) + '</p>';
        }
        var iconFile = safeAssetFile(c.iconImg);
        var iconHtml = iconFile
          ? '<span class="wln-card-icon wln-card-icon-img"><img src="' + escapeHtml(iconFile) + '" alt="" /></span>'
          : '<span class="wln-card-icon">' + (/^[a-z-]+$/.test(c.icon || '') ? KestrelIcon(c.icon) : sr(c.icon || '•')) + '</span>';
        return '' +
          '<div class="wln-card">' +
            '<div class="wln-card-top">' +
              iconHtml +
              '<div class="wln-card-heading">' +
                '<div class="wln-card-name">' + sr(c.name || '') + '</div>' +
                '<div class="wln-card-tag">' + sr(c.tag || '') + '</div>' +
              '</div>' +
            '</div>' +
            bodyHtml +
            cta +
          '</div>';
      }).join('');

      var heroFile = safeAssetFile(note.heroImg);
      var heroHtml = heroFile
        ? '<div class="wln-banner"><img src="' + escapeHtml(heroFile) +
            '" alt="' + escapeHtml(note.heroAlt || '') + '" /></div>'
        : '';

      var blogBtn = '';
      var blogHref = note.blogLink && note.blogLink.href ? safeHttpsUrl(note.blogLink.href) : null;
      if (blogHref) {
        blogBtn = '<a class="wln-blog-link" href="' + escapeHtml(blogHref) +
          '" target="_blank" rel="noopener noreferrer">' + sr(note.blogLink.label || 'Read more →') + '</a>';
      }

      return '' +
        '<div class="wln">' +
          '<button type="button" class="wln-dismiss" id="wwnDismiss" aria-label="Dismiss">✕</button>' +
          heroHtml +
          '<span class="wln-eyebrow">' + KestrelIcon('sparkle') + '&nbsp; ' + sr(note.eyebrow || '') + '</span>' +
          '<h2 class="wln-title">' + sr(note.title || '') + '</h2>' +
          (note.subtitle ? '<div class="wln-subtitle">' + sr(note.subtitle) + '</div>' : '') +
          '<div class="wln-cards">' + cards + '</div>' +
          ((note.note || note.sign)
            ? '<div class="wln-note">' + sr(note.note || '') +
                (note.sign ? ' <span class="wln-sign">' + sr(note.sign) + '</span>' : '') +
              '</div>'
            : '') +
          blogBtn +
        '</div>';
    }

    (function setupWelcomeWhatsNew() {
      var banner = document.getElementById('welcomeWhatsNew');
      if (!banner) return;

      function showNote(note, channel) {
        banner.innerHTML = buildNoteHTML(note, channel);
        banner.classList.remove('hidden');
        var dismiss = banner.querySelector('#wwnDismiss');
        if (dismiss) dismiss.addEventListener('click', async function() {
          banner.classList.add('hidden');
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

      (async function() {
        // Gate on the per-version "last seen" setting (a fast LOCAL bridge call).
        // The note is baked into the build — bump WHATS_NEW.version to re-show it.
        var lastSeen = null;
        if (hasPywebviewApi) {
          try {
            var res = await window.pywebview.api.get_settings();
            lastSeen = res && res.settings && res.settings.last_seen_whats_new_version;
          } catch (_) {}
        }
        if (WHATS_NEW.version === lastSeen) return;
        var channel = 'direct';
        if (hasPywebviewApi && window.pywebview?.api?.get_dist_channel) {
          try {
            var ch = await window.pywebview.api.get_dist_channel();
            channel = (ch && ch.channel) || 'direct';
          } catch (_) {}
        }
        showNote(WHATS_NEW, channel);
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
        KestrelIcon.set(iconEl, t.icon || 'lightbulb');
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
      // noteDonatePromptChoice() records the click before close() fires the
      // dialog's 'close' event, which otherwise counts as a dismissal.
      document.getElementById('donateDlgGoBtn').addEventListener('click', function() {
        noteDonatePromptChoice('support');
        dlg.close();
        openSupportLink();
      });
      document.getElementById('donateDlgFeedbackBtn').addEventListener('click', function() {
        noteDonatePromptChoice('feedback');
        dlg.close();
        setTimeout(function() {
          document.getElementById('feedbackDlg').showModal();
        }, 150);
      });
      document.getElementById('donateDlgClose').addEventListener('click', function() {
        dlg.close();
      });
    })();
