    // ====================================================================
    // Welcome panel: "What's New" banner + rotating tip carousel
    // ====================================================================

    // Bump this whenever you author a new changelog. Clients with a matching
    // `last_seen_whats_new_version` will not see the banner again.
    const WHATS_NEW = {
      version: 'great-horned-owl',
      headline: "New in v(Great-Horned Owl) — Auto-Complete Bird Tags, cleaner UI, crash fixes!",
      items: [
        'New <b>regional bird catalog</b> covers ~11,250 species (up from 500). Type a name, partial, or <b>4-letter alpha code</b> (AMGO, NOCA) into the species or family box — region-filterable and fuzzy-matched. Toggle <b>Show scientific names</b> in Settings to see the Latin binomial under each pill.',
        'Completely <b>redesigned Analyze Folders dialog</b> with a 3-column layout — queue builder, settings, and live queue summary + analysis time estimates.',
        'Redesigned homepage with a focus on simplicity and intuitiveness.',
        'Bug Fixes for MacOS and Windows, particularly for users analyzing on external drives, and for MacOS users using GPU support.',
        '📣 <b>A NOTE FROM THE DEVELOPER:</b> Hello! Project Kestrel is moving towards the launch of <b>Perch</b>, a new sharing platform that lets others view your entire birding outing on the web, and <b>Cloud Compute</b>, a Kestrel add-on for faster analysis powered by cloud GPUs. (I’m especially excited about Perch! Take a sneak peek by visiting <a href="https://perch.projectkestrel.org/" target="_blank" rel="noopener noreferrer">this link</a>.) <b>I need beta testers!</b> If you love Project Kestrel and would be willing to test and provide feedback on these new platforms, please contact me via the in-app "Feedback" form!',
      ],
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

    (function setupWelcomeWhatsNew() {
      var banner = document.getElementById('welcomeWhatsNew');
      if (!banner) return;
      (async function() {
        var lastSeen = null;
        if (hasPywebviewApi) {
          try {
            var res = await window.pywebview.api.get_settings();
            lastSeen = res && res.settings && res.settings.last_seen_whats_new_version;
          } catch (_) {}
        }
        if (lastSeen === WHATS_NEW.version) return;
        var items = WHATS_NEW.items.map(function(it) {
          return '<li>' + it + '</li>';
        }).join('');
        banner.innerHTML =
          '<div class="wwn-head">' +
            '<span class="wwn-badge">New</span>' +
            '<span class="wwn-title">' + WHATS_NEW.headline + '</span>' +
            '<button type="button" class="wwn-dismiss" id="wwnDismiss" aria-label="Dismiss">✕</button>' +
          '</div>' +
          '<ul class="wwn-list">' + items + '</ul>';
        banner.classList.remove('hidden');
        var dismiss = banner.querySelector('#wwnDismiss');
        if (dismiss) dismiss.addEventListener('click', async function() {
          banner.classList.add('hidden');
          if (hasPywebviewApi) {
            try {
              var r2 = await window.pywebview.api.get_settings();
              var s = (r2 && r2.success ? r2.settings : {}) || {};
              s.last_seen_whats_new_version = WHATS_NEW.version;
              await window.pywebview.api.save_settings_data(s);
            } catch (_) {}
          }
        });
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
