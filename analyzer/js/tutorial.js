    // ====================================================================
    // Tutorial System  (Basics = short onboarding, Advanced = full tour)
    // Interactive: some steps require user action before advancing.
    // Engine supports: waitFor, waitForChain, inDialog, setupAction,
    // highlightAlso[], customBody, loadSamplesOnNext, inlineFooter.
    // ====================================================================

    const _tutWelcomeStep = {
      title: 'Welcome to Project Kestrel!',
      body: 'Project Kestrel uses machine learning to organize your bird photography \u2014 helping you review photos more efficiently, search through your library, and quickly find the ones you want to edit and share.<br><br>Click <b>Next</b> and we\u2019ll auto-load some <b>sample bird photos</b> so you can see Kestrel in action.',
      target: null,
      loadSamplesOnNext: true,
    };

    const _tutWorkflowStep = {
      title: 'Fit Kestrel into your workflow',
      body: '',
      target: '.write-metadata-btn',
      position: 'bottom',
      customBody: 'workflowCard',
      highlightAlso: ['.culling-assistant-btn'],
    };

    const _tutTryYourOwnStepBase = {
      title: 'Now try it with your own photos',
      body: 'You\u2019re ready!<br><br>\u2022 Click <b>Analyze Folders\u2026</b> to process a new folder of photos.<br>\u2022 Click <b>Open Folder\u2026</b> (or drop into the Folder Tree) to browse photos Kestrel has already analyzed.<br><br><b>Tip:</b> open a parent folder once to load your whole library \u2014 then search across every outing, every year.',
      target: '#analyzeQueueBtn',
      position: 'right',
      highlightAlso: ['#pickFolder'],
    };

    const TUTORIAL_BASICS = [
      _tutWelcomeStep,
      {
        title: 'Your photos, organized by scene',
        body: 'Kestrel organizes your photos into <b>scenes</b> \u2014 groups of similar images captured in the same burst. The scene grid shows these scenes in the order they were taken.',
        nudge: 'Click on a scene to open it!',
        target: '#sceneGrid .card',
        position: 'right',
        waitFor: 'clickScene',
      },
      {
        title: 'Explore your scene',
        body: 'Within each scene, your photos are automatically <b>sorted by quality</b> \u2014 from sharpest to blurriest. Focus your attention on the best shots first.<br><br>Click on a photo in the filmstrip to view its details.',
        nudge: 'Click a photo in the filmstrip below!',
        target: '#imageGrid',
        position: 'top',
        inDialog: true,
        waitFor: 'clickFilmstrip',
      },
      {
        title: 'Make a culling decision, then close the scene',
        body: 'Kestrel computes <b>star ratings</b> based on each image\u2019s quality score. Click the stars to set your own. <span style="color:#6aa0ff">Blue stars</span> = AI rating \u00b7 <span style="color:#f5c542">Gold stars</span> = your manual override.<br><br>Use the <b>Accept \u00b7 Undecided \u00b7 Reject</b> buttons to make a culling decision. These decisions power the Culling Assistant later!<br><br>When you\u2019re done, close the scene to continue.',
        nudge: 'Mark a photo Accept/Reject, then close the scene.',
        target: '#sceneInfoBar',
        position: 'top-left',
        inDialog: true,
        waitForChain: ['clickCullToggle', 'closeDialog'],
      },
      {
        title: 'Search and filter',
        body: 'Use the <b>Filter &amp; Sort</b> panel to narrow your scenes down:<br><br>\u2022 <b>Search</b> by bird species or family \u2014 the grid filters instantly.<br>\u2022 <b>Sort</b> by Capture Time, Quality, or Image Count.<br>\u2022 Toggle <b>Group by folder</b> / <b>capture time</b> to reorganize.<br><br>You can always tweak further in the options below.',
        target: '.filter-panel',
        position: 'right',
      },
      _tutWorkflowStep,
      {
        title: 'Which photo editor do you use?',
        body: 'Pick your preferred photo editor below. Kestrel will use it when you press <b>Space</b> or click the <b>Open</b> button (top-right of the scene info bar) to launch a photo.',
        target: null,
        customBody: 'editorPicker',
      },
      Object.assign({}, _tutTryYourOwnStepBase, {
        inlineFooter: 'advancedLink',
      }),
    ];

    const TUTORIAL_ADVANCED = [
      _tutWelcomeStep,
      {
        title: 'Your photos, organized by scene',
        body: 'Kestrel organizes your photos into <b>scenes</b> \u2014 groups of similar images captured in the same burst. The scene grid shows these scenes in the order they were taken.',
        nudge: 'Click on a scene to open it!',
        target: '#sceneGrid .card',
        position: 'right',
        waitFor: 'clickScene',
      },
      {
        title: 'Explore your scene',
        body: 'Within each scene, your photos are automatically <b>sorted by quality</b> \u2014 from sharpest to blurriest. Click on a photo in the filmstrip to view its details.',
        nudge: 'Click a photo in the filmstrip below!',
        target: '#imageGrid',
        position: 'top',
        inDialog: true,
        waitFor: 'clickFilmstrip',
      },
      {
        title: 'Ratings and culling decisions',
        body: 'Kestrel computes <b>star ratings</b> based on each image\u2019s quality score. Click the stars to set your own. <span style="color:#6aa0ff">Blue stars</span> = AI rating \u00b7 <span style="color:#f5c542">Gold stars</span> = your manual override.<br><br>Use the <b>Accept \u00b7 Undecided \u00b7 Reject</b> buttons to make a culling decision for each photo. These come in handy with the Culling Assistant later!',
        nudge: 'Mark a photo as Accepted or Rejected to continue!',
        target: '#sceneInfoBar',
        position: 'top-left',
        inDialog: true,
        waitFor: 'clickCullToggle',
      },
      {
        title: 'Multiple birds in a scene? Switch crops.',
        body: 'When a scene has more than one bird, the <b>\u25c2 / \u25b8 crop buttons</b> appear next to the filename. Click them to cycle through each bird crop.<br><br>Equivalent keyboard shortcuts:<br>\u2022 <kbd>\u2191</kbd> / <kbd>\u2193</kbd> \u2014 previous / next crop<br>\u2022 <kbd>Enter</kbd> \u2014 promote the active crop as the scene\u2019s primary bird<br><br>(If the current sample scene only has one bird, the crop buttons stay hidden.)',
        target: '#sceneInfoCropNav',
        position: 'top',
        inDialog: true,
      },
      {
        title: 'Open in your photo editor',
        body: 'Click this <b>Open</b> button (or press <kbd>Space</kbd>) to launch the original photo in your chosen photo editor. You can change which editor Kestrel uses in <b>Settings</b>.',
        target: '#sceneInfoEditorBtn',
        position: 'top-left',
        inDialog: true,
      },
      {
        title: 'RAW zoom',
        body: 'Want to pixel-peep the RAW? <b>Click and hold</b> on the full image to load the original RAW file and zoom in at the cursor. <b>Scroll</b> while holding to change zoom level, or use the <b>RAW Zoom</b> slider to set a default.<br><br>Release to return to the normal preview.',
        target: '#sceneZoomWrap',
        position: 'top',
        inDialog: true,
      },
      {
        title: 'Keyboard shortcuts',
        body: 'Kestrel has keyboard shortcuts to make reviewing photos faster. The shortcuts are listed above \u2014 try some out before continuing!',
        target: '#sceneShortcutLegend',
        position: 'bottom',
        inDialog: true,
        setupAction: 'expandShortcuts',
      },
      {
        title: 'A few more scene features',
        body: 'A few more things you can do once you\u2019re browsing your <b>own photos</b> (some won\u2019t work on the sample images):<br><br>\u2022 Edit the <b>scene name</b> and <b>tags</b> at the top<br>\u2022 Use <b>\u2702 Split Scene</b> if Kestrel accidentally merged two different scenes<br>\u2022 <b>Copy</b> (clipboard) the full image or bird crop straight from the preview<br><br>Close the scene dialog to continue.',
        nudge: 'Close the scene dialog to continue.',
        target: '#closeDlg',
        position: 'bottom',
        inDialog: true,
        waitFor: 'closeDialog',
      },
      {
        title: 'Filtering options',
        body: '\u2022 <b>Search</b> for any bird species or family \u2014 the grid filters instantly as you type.<br>\u2022 Don\u2019t see scenes after searching? Lower the <b>Confidence</b> threshold to see more results.<br>\u2022 Enable <b>Multi-subject mode</b> if your scenes contain multiple species.<br>\u2022 <b>Sort</b> by Capture Time, Quality, Scene ID, or Image Count.<br>\u2022 Toggle <b>Group by folder</b> / <b>capture time</b> to reorganize the grid.',
        target: '.filter-panel',
        position: 'right',
      },
      {
        title: 'Merging scenes',
        body: 'The two highlighted scenes above were actually one continuous burst that Kestrel split in two. Hold <kbd>Ctrl</kbd> and click both cards to select them, then click <b>Merge selected scenes</b> to combine them back into one.<br><br>You can also <kbd>Shift</kbd>+click to range-select a group of scenes at once.',
        target: '#sceneGrid .card:nth-child(2)',
        highlightAlso: ['#sceneGrid .card:nth-child(1)'],
        position: 'bottom',
      },
      {
        title: 'Search across your whole library',
        body: 'Kestrel searches across <b>every loaded folder</b> by species or family.<br><br><b>Tip:</b> open a <b>parent folder</b> once (via <b>Open Folder\u2026</b> or the Folder Tree) and Kestrel loads your entire library at once \u2014 then you can search across every outing and every year without re-loading anything.',
        target: '#search',
        position: 'right',
      },
      _tutWorkflowStep,
      {
        title: 'Write Photo Metadata',
        body: 'Click <b>Write Photo Metadata</b> to export Kestrel\u2019s star ratings and Accept/Reject decisions into XMP sidecar files alongside your photos. These <code>.xmp</code> files are understood natively by <b>Adobe Lightroom</b>, <b>darktable</b>, <b>Capture One</b>, and other editors.<br><br>\u26a0\ufe0f <b>Write photo metadata <em>before</em> importing into your photo editor</b> \u2014 most catalogues ignore new sidecar files once a photo is already imported. If a sidecar was already created by another application, Kestrel will ask before overwriting it.',
        target: '.write-metadata-btn',
        position: 'bottom',
      },
      {
        title: 'Culling Assistant',
        body: 'The <b>Culling Assistant</b> helps you automatically assign photos as Accepted or Rejected based on star ratings \u2014 and can even move rejected photos into a dedicated folder.<br><br>Click <b>Open Culling Assistant</b> to open a dedicated Accept/Reject workspace for the folder.',
        target: '.culling-assistant-btn',
        position: 'bottom',
      },
      {
        title: 'Options',
        body: 'Click <b>Settings</b> to choose your preferred <b>photo editor</b> (Lightroom, Darktable, or system default). Opening a photo with <kbd>Space</kbd> will launch it there. You can also tweak several other options \u2014 including the experimental <b>wildlife mode</b> that detects non-bird wildlife.',
        target: '#openSettings',
        position: 'bottom',
      },
      {
        title: 'You\u2019re all set!',
        body: 'That\u2019s the full tour! Quick recap:<br><br>\u2022 <b>Analyze Folders</b> to process new photos<br>\u2022 <b>Open Folder</b> to browse analyzed photos<br>\u2022 <b>Click scenes</b> to view &amp; rate photos<br>\u2022 <b>Culling Assistant</b> for bulk Accept/Reject workflow<br>\u2022 <b>Write Photo Metadata</b> to export to Lightroom, darktable, etc.<br><br>\u26a0\ufe0f <b>Remember:</b> Write photo metadata <em>before</em> importing into Lightroom or Capture One for best results!<br><br>Click the <b>\uD83D\uDCD6 Tutorial</b> button anytime to replay this tour. Happy birding!',
        target: null,
      },
      {
        title: 'Please send feedback!',
        body: 'I (the person who made Project Kestrel) would really love to hear from you! Please tell me if you found the app useful, or if you find any bugs or have suggestions for improvements.<br><br>Thank you for trying Kestrel!',
        target: '#openFeedback',
        position: 'top',
      },
      _tutTryYourOwnStepBase,
    ];

    let _tutStep = 0;
    let _tutSteps = [];
    let _tutBranch = '';             // '' = not started, 'basics', 'advanced'
    let _tutSampleLoaded = false;    // track if we auto-loaded sample sets
    let _tutCleanupFn = null;        // cleanup function for current waitFor listeners
    let _tutInDialog  = false;       // true while tutorial card is inside the scene dialog
    let _tutChainIdx  = 0;           // index into step.waitForChain

    function _tutEl(sel) { return document.querySelector(sel); }

    async function checkMainTutorialSeen() {
      if (!hasPywebviewApi) return true;
      try {
        var res = await window.pywebview.api.get_settings();
        return res && res.settings && res.settings.main_tutorial_seen === true;
      } catch (e) { return false; }
    }

    async function markMainTutorialSeen() {
      if (!hasPywebviewApi) return;
      try {
        var res = await window.pywebview.api.get_settings();
        var s = (res && res.success ? res.settings : {}) || {};
        s.main_tutorial_seen = true;
        await window.pywebview.api.save_settings_data(s);
      } catch (e) { console.warn('markMainTutorialSeen:', e); }
    }

    function _tutCleanup() {
      document.querySelectorAll('.highlight-target').forEach(function(el) {
        el.classList.remove('highlight-target');
      });
      if (_tutCleanupFn) { _tutCleanupFn(); _tutCleanupFn = null; }
      if (_tutInDialog) {
        _tutInDialog = false;
        var _ovl = _tutEl('#tutorialOverlay');
        var _crd = _tutEl('#tutorialCard');
        if (_crd && _ovl && _crd.parentElement !== _ovl) { _ovl.appendChild(_crd); }
        if (_ovl) { _ovl.style.display = ''; }
      }
    }

    function startMainTutorial(branch, fromStep) {
      _tutCleanup();
      // Backward-compat: legacy callers passed 1 or 2
      if (branch === 1) branch = 'basics';
      else if (branch === 2) branch = 'advanced';
      _tutBranch = (branch === 'advanced') ? 'advanced' : 'basics';
      _tutSteps = (_tutBranch === 'advanced') ? TUTORIAL_ADVANCED : TUTORIAL_BASICS;
      _tutStep = fromStep || 0;
      _tutChainIdx = 0;
      _tutEl('#tutorialOverlay').classList.add('active');
      _showMainTutStep(_tutStep);
    }

    function _closeMainTutorial() {
      _tutCleanup();
      _tutEl('#tutorialOverlay').classList.remove('active', 'has-backdrop');
      _tutEl('#tutorialHighlight').style.display = 'none';
      _tutEl('#tutorialNudge').style.display = 'none';
      if (_tutBranch) markMainTutorialSeen();
      _tutBranch = '';
    }

    // Install a single waitFor listener. Returns a cleanup fn that removes it.
    // onDone is called (with no args) when the gesture is detected.
    function _installTutWaitFor(key, onDone) {
      if (key === 'clickScene') {
        var sceneGridEl = document.getElementById('sceneGrid');
        if (!sceneGridEl) return function() {};
        var handler = function(ev) {
          var cardEl = ev.target.closest('.card');
          if (cardEl) setTimeout(onDone, 400);
        };
        sceneGridEl.addEventListener('click', handler, true);
        return function() { sceneGridEl.removeEventListener('click', handler, true); };
      }
      if (key === 'clickStar') {
        var onStarClick = function(ev) {
          var starEl = ev.target.closest('.star, .stars span');
          if (starEl) setTimeout(onDone, 300);
        };
        document.addEventListener('click', onStarClick, true);
        return function() { document.removeEventListener('click', onStarClick, true); };
      }
      if (key === 'clickFilmstrip') {
        var filmstripEl = document.getElementById('imageGrid');
        if (!filmstripEl) return function() {};
        var onFilmstripClick = function(ev) {
          var cardEl = ev.target.closest('.filmstrip-card, .card');
          if (cardEl) setTimeout(onDone, 350);
        };
        filmstripEl.addEventListener('click', onFilmstripClick, true);
        return function() { filmstripEl.removeEventListener('click', onFilmstripClick, true); };
      }
      if (key === 'clickCullToggle') {
        var onCullClick = function(ev) {
          var cullBtn = ev.target.closest('.cull-btn[data-cull="accept"], .cull-btn[data-cull="reject"]');
          if (cullBtn) setTimeout(onDone, 400);
        };
        document.addEventListener('click', onCullClick, true);
        return function() { document.removeEventListener('click', onCullClick, true); };
      }
      if (key === 'closeDialog') {
        var sceneDlgEl = document.getElementById('sceneDlg');
        if (!sceneDlgEl) return function() {};
        var onDlgClose = function() {
          sceneDlgEl.removeEventListener('close', onDlgClose);
          setTimeout(onDone, 250);
        };
        sceneDlgEl.addEventListener('close', onDlgClose);
        return function() { sceneDlgEl.removeEventListener('close', onDlgClose); };
      }
      return function() {};
    }

    function _renderTutWorkflowCard(bodyEl) {
      bodyEl.innerHTML =
        '<div class="tut-workflow-intro">Pick the workflow that fits how you already edit \u2014 Kestrel plugs into any of them.</div>' +
        '<div class="tut-workflow-tabs" role="tablist">' +
          '<button type="button" class="tut-wf-tab active" data-tab="none">No workflow changes</button>' +
          '<button type="button" class="tut-wf-tab" data-tab="cull">Cut the blurry bulk</button>' +
          '<button type="button" class="tut-wf-tab" data-tab="favs">Just my favorites</button>' +
        '</div>' +
        '<div class="tut-workflow-panels">' +
          '<div class="tut-wf-panel active" data-panel="none">' +
            '<div class="tut-wf-flow">' +
              '<div class="tut-wf-node">Your Photos</div>' +
              '<div class="tut-wf-arrow">\u2192</div>' +
              '<div class="tut-wf-node accent">Kestrel Analyzes</div>' +
              '<div class="tut-wf-arrow">\u2192</div>' +
              '<div class="tut-wf-node highlight">Write Metadata</div>' +
            '</div>' +
            '<div class="tut-wf-caption">Just export Kestrel\u2019s analysis as XMP sidecars, then browse them in the photo editor you already use.</div>' +
          '</div>' +
          '<div class="tut-wf-panel" data-panel="cull">' +
            '<div class="tut-wf-flow">' +
              '<div class="tut-wf-node">Your Photos</div>' +
              '<div class="tut-wf-arrow">\u2192</div>' +
              '<div class="tut-wf-node accent">Kestrel Analyzes</div>' +
              '<div class="tut-wf-arrow">\u2192</div>' +
              '<div class="tut-wf-node highlight">Culling Assistant</div>' +
              '<div class="tut-wf-arrow">\u2192</div>' +
              '<div class="tut-wf-node">Accepts / Rejects</div>' +
            '</div>' +
            '<div class="tut-wf-caption">Use the <b>Culling Assistant</b> to cut the blurry bulk \u2014 keep the sharp photos and archive the rest in one pass.</div>' +
          '</div>' +
          '<div class="tut-wf-panel" data-panel="favs">' +
            '<div class="tut-wf-flow">' +
              '<div class="tut-wf-node">Your Photos</div>' +
              '<div class="tut-wf-arrow">\u2192</div>' +
              '<div class="tut-wf-node accent">Kestrel Analyzes</div>' +
              '<div class="tut-wf-arrow">\u2192</div>' +
              '<div class="tut-wf-node">Pick Favorites</div>' +
              '<div class="tut-wf-arrow">\u2192</div>' +
              '<div class="tut-wf-node highlight">Open in Editor</div>' +
            '</div>' +
            '<div class="tut-wf-caption">Browse your scenes in Kestrel and press <kbd>Space</kbd> on the ones you love \u2014 they open straight in your photo editor.</div>' +
          '</div>' +
        '</div>' +
        '<div class="tut-workflow-hint">We\u2019ve highlighted the two buttons you\u2019ll use most: <b>Write Photo Metadata</b> and <b>Open Culling Assistant</b>.</div>';

      var tabs = bodyEl.querySelectorAll('.tut-wf-tab');
      var panels = bodyEl.querySelectorAll('.tut-wf-panel');
      tabs.forEach(function(tab) {
        tab.addEventListener('click', function() {
          var key = tab.getAttribute('data-tab');
          tabs.forEach(function(t) { t.classList.toggle('active', t === tab); });
          panels.forEach(function(p) { p.classList.toggle('active', p.getAttribute('data-panel') === key); });
        });
      });
    }

    function _renderTutEditorPicker(bodyEl, onChosen) {
      var intro = document.createElement('div');
      intro.className = 'tut-editor-intro';
      intro.innerHTML = 'Inside any scene, press <kbd>Space</kbd> or click the <b>Open</b> button (top-right of the info bar) to launch the original in your chosen editor.';
      bodyEl.innerHTML = '';
      bodyEl.appendChild(intro);

      var grid = document.createElement('div');
      grid.className = 'tut-editor-grid';
      var choices = [
        { key: 'lightroom',   label: 'Adobe Lightroom Classic' },
        { key: 'darktable',   label: 'Darktable' },
        { key: 'capture_one', label: 'Capture One' },
        { key: 'photoshop',   label: 'Adobe Photoshop' },
        { key: 'system',      label: 'System Default' },
        { key: '__other',     label: 'Other\u2026 (open Settings)' },
      ];
      choices.forEach(function(c) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'tut-editor-btn';
        b.setAttribute('data-key', c.key);
        b.innerHTML = '<span class="tut-editor-label">' + c.label + '</span>';
        b.addEventListener('click', function() { onChosen(c.key, b); });
        grid.appendChild(b);
      });
      bodyEl.appendChild(grid);

      var foot = document.createElement('div');
      foot.className = 'tut-editor-foot';
      foot.textContent = 'You can change this anytime in Settings.';
      bodyEl.appendChild(foot);
    }

    async function _applyChosenEditor(key) {
      try {
        var s = loadSettings();
        s.editor = key;
        saveSettings(s);
        if (hasPywebviewApi && window.pywebview?.api?.save_settings_data) {
          try { await window.pywebview.api.save_settings_data(s); } catch (_) {}
        }
      } catch (e) { console.warn('[tutorial] apply editor failed', e); }
    }

    function _showMainTutStep(idx) {
      _tutCleanup();
      var step = _tutSteps[idx];
      if (!step) { _closeMainTutorial(); return; }

      var overlay = _tutEl('#tutorialOverlay');
      var hl      = _tutEl('#tutorialHighlight');
      var card    = _tutEl('#tutorialCard');
      var nudge   = _tutEl('#tutorialNudge');
      var nextBtn = _tutEl('#tutorialNext');
      var bodyEl  = _tutEl('#tutorialBody');

      card.classList.remove('tut-card-workflow', 'tut-card-editor');

      if (step.setupAction === 'expandShortcuts') {
        var legend = document.getElementById('sceneShortcutLegend');
        if (legend && legend.classList.contains('hidden')) {
          var shortcutToggleBtn = document.getElementById('sceneShortcutBtn');
          if (shortcutToggleBtn) shortcutToggleBtn.click();
        }
      }

      _tutEl('#tutorialCounter').textContent = 'Step ' + (idx + 1) + ' of ' + _tutSteps.length;
      _tutEl('#tutorialTitle').innerHTML = step.title;

      if (step.customBody === 'workflowCard') {
        card.classList.add('tut-card-workflow');
        _renderTutWorkflowCard(bodyEl);
      } else if (step.customBody === 'editorPicker') {
        card.classList.add('tut-card-editor');
        _renderTutEditorPicker(bodyEl, async function(key, btnEl) {
          bodyEl.querySelectorAll('.tut-editor-btn').forEach(function(b) {
            b.classList.toggle('selected', b === btnEl);
            b.disabled = true;
          });
          if (key === '__other') {
            try { showSettings(); } catch (_) {}
            var dlg2 = document.getElementById('settingsDlg');
            var onClose = function() {
              if (dlg2) dlg2.removeEventListener('close', onClose);
              setTimeout(function() { _tutAdvance(); }, 250);
            };
            if (dlg2) dlg2.addEventListener('close', onClose);
            else setTimeout(function() { _tutAdvance(); }, 400);
          } else {
            await _applyChosenEditor(key);
            setTimeout(function() { _tutAdvance(); }, 350);
          }
        });
      } else {
        bodyEl.innerHTML = step.body || '';
      }

      // Optional inline footer link (e.g. Basics → Advanced)
      if (step.inlineFooter === 'advancedLink') {
        var foot = document.createElement('div');
        foot.className = 'tut-inline-foot';
        foot.innerHTML = 'Want the deep dive? <a href="#" id="tutStartAdvancedLink">Open the Advanced Tutorial</a>';
        bodyEl.appendChild(foot);
        var ln = foot.querySelector('#tutStartAdvancedLink');
        if (ln) ln.addEventListener('click', function(ev) {
          ev.preventDefault();
          _closeMainTutorial();
          setTimeout(function() { startMainTutorial('advanced', 0); }, 120);
        });
      }

      if (step.nudge) { nudge.textContent = step.nudge; nudge.style.display = ''; }
      else nudge.style.display = 'none';

      var dotsCont = _tutEl('#tutorialProgress');
      dotsCont.innerHTML = '';
      _tutSteps.forEach(function(_, i) {
        var d = document.createElement('div');
        d.className = 'tutorial-dot' + (i === idx ? ' active' : '');
        dotsCont.appendChild(d);
      });

      _tutEl('#tutorialBack').disabled = (idx === 0);
      var isLast = (idx === _tutSteps.length - 1);
      nextBtn.textContent = isLast ? 'Finish \u2713' : 'Next \u2192';

      var hasWaitFor = !!step.waitFor || !!(step.waitForChain && step.waitForChain.length);
      // customBody === 'editorPicker' manages its own advance via buttons.
      var customManagesAdvance = (step.customBody === 'editorPicker');
      nextBtn.style.display = (hasWaitFor || customManagesAdvance) ? 'none' : '';

      var target = step.target ? document.querySelector(step.target) : null;

      var _inDialogActive = false;
      if (step.inDialog) {
        var dlg = document.getElementById('sceneDlg');
        if (!dlg || !dlg.open) {
          bodyEl.insertAdjacentHTML('beforeend', '<br><br><span style="color:var(--brand);font-weight:600">Open a scene first, then this step will highlight the right element.</span>');
          nudge.style.display = 'none';
          nextBtn.style.display = '';  // show Next even if waitFor was set
          target = null;
          hasWaitFor = false;
        } else {
          _inDialogActive = true;
          _tutInDialog = true;
          if (card.parentElement !== dlg) { dlg.appendChild(card); }
          overlay.style.display = 'none';
        }
      }

      // Generalized multi-target highlight (replaces the old single highlightFirst)
      var extraTargets = [];
      if (step.highlightFirst) extraTargets.push(step.highlightFirst);
      if (Array.isArray(step.highlightAlso)) extraTargets = extraTargets.concat(step.highlightAlso);
      if (!_inDialogActive) {
        extraTargets.forEach(function(sel) {
          var el = document.querySelector(sel);
          if (el) el.classList.add('highlight-target');
        });
      }

      if (!target || (target.offsetWidth === 0 && target.offsetHeight === 0)) {
        hl.style.display = 'none';
        overlay.classList.add('has-backdrop');
        card.style.transform = 'translate(-50%, -50%)';
        card.style.top  = '50%';
        card.style.left = '50%';
      } else {
        hl.style.display = _inDialogActive ? 'none' : '';
        overlay.classList.remove('has-backdrop');
        card.style.transform = '';

        target.classList.add('highlight-target');

        var pad = 8;
        var r = target.getBoundingClientRect();
        if (!_inDialogActive) {
          hl.style.top    = (r.top  - pad) + 'px';
          hl.style.left   = (r.left - pad) + 'px';
          hl.style.width  = (r.width  + pad * 2) + 'px';
          hl.style.height = (r.height + pad * 2) + 'px';
        }

        var pos    = step.position || 'right';
        var margin = 18;
        var vw     = window.innerWidth;
        var vh     = window.innerHeight;
        var cw     = (card.offsetWidth || 380) + margin;
        var ch     = card.offsetHeight || 220;
        var topV, leftV;
        if (pos === 'right')        { leftV = r.right + margin;                  topV = r.top + r.height / 2 - ch / 2; }
        else if (pos === 'left')    { leftV = r.left - cw - margin;              topV = r.top + r.height / 2 - ch / 2; }
        else if (pos === 'bottom')  { leftV = r.left + r.width / 2 - cw / 2;     topV = r.bottom + margin; }
        else if (pos === 'top-left'){ leftV = r.left;                            topV = r.top - ch - margin; }
        else                         { leftV = r.left + r.width / 2 - cw / 2;    topV = r.top - ch - margin; } // 'top'
        leftV = Math.max(margin, Math.min(leftV, vw - cw - margin));
        topV  = Math.max(margin, Math.min(topV,  vh - ch - margin));
        card.style.left = leftV + 'px';
        card.style.top  = topV  + 'px';
      }

      // ---- Interactive waitFor / waitForChain ----
      if (hasWaitFor) {
        if (step.waitForChain && step.waitForChain.length) {
          _tutChainIdx = 0;
          var installChain;
          installChain = function() {
            if (_tutChainIdx >= step.waitForChain.length) { _tutAdvance(); return; }
            var key = step.waitForChain[_tutChainIdx];
            _tutCleanupFn = _installTutWaitFor(key, function() {
              if (_tutCleanupFn) { _tutCleanupFn(); _tutCleanupFn = null; }
              _tutChainIdx++;
              installChain();
            });
          };
          installChain();
        } else if (step.waitFor) {
          _tutCleanupFn = _installTutWaitFor(step.waitFor, function() { _tutAdvance(); });
        }
      }
    }

    async function _handleLoadSamplesOnNext() {
      var bodyEl = _tutEl('#tutorialBody');
      var nextBtn = _tutEl('#tutorialNext');
      var backBtn = _tutEl('#tutorialBack');
      var skipBtn = _tutEl('#tutorialSkip');
      nextBtn.disabled = true; backBtn.disabled = true; skipBtn.disabled = true;
      var loading = document.createElement('div');
      loading.className = 'tut-loading';
      loading.innerHTML = '<span class="tut-spinner"></span> Loading sample photos\u2026';
      bodyEl.appendChild(loading);
      try {
        await _autoLoadSamples();
      } finally {
        nextBtn.disabled = false; backBtn.disabled = false; skipBtn.disabled = false;
      }
      _tutStep++;
      _showMainTutStep(_tutStep);
    }

    function _tutAdvance() {
      var cur = _tutSteps[_tutStep];
      if (cur && cur.loadSamplesOnNext && !_tutSampleLoaded) {
        _handleLoadSamplesOnNext();
        return;
      }
      _tutStep++;
      if (_tutStep >= _tutSteps.length) {
        _closeMainTutorial();
      } else {
        _showMainTutStep(_tutStep);
      }
    }

    function _tutGoBack() {
      if (_tutStep > 0) { _tutStep--; _showMainTutStep(_tutStep); }
    }

    // Load sample photos (once per session). Returns when samples are ready.
    async function _autoLoadSamples() {
      if (_tutSampleLoaded) return;
      if (!hasPywebviewApi) { _tutSampleLoaded = true; return; }
      try {
        var res = await window.pywebview.api.get_sample_sets_paths();
        if (res && res.success && res.paths && res.paths.length > 0) {
          _tutSampleLoaded = true;
          var sampleParent = res.paths[0].replace(/[/\\][^/\\]+$/, '');
          try { await scanFolderTree(sampleParent); } catch (e) { console.warn('[tutorial] scanFolderTree:', e); }
          try { await loadMultipleFolders(res.paths); }
          catch (e) { console.warn('[tutorial] loadMultipleFolders:', e); }
          await new Promise(function(r) { setTimeout(r, 500); });
        } else {
          console.warn('[tutorial] No sample sets found');
        }
      } catch (e) {
        console.warn('[tutorial] _autoLoadSamples error:', e);
      }
    }

    // Chooser dialog: click Tutorial button (or Welcome Start Tutorial) to pick branch.
    function openTutorialChooser() {
      var dlg = document.getElementById('tutorialChooserDlg');
      if (!dlg) { startMainTutorial('basics', 0); return; }
      try { dlg.showModal(); } catch (_) { try { dlg.show(); } catch(__) {} }
    }
    // Expose on window so inline onclick handlers can reach it.
    window.openTutorialChooser = openTutorialChooser;
    window.startMainTutorial = startMainTutorial;

    var _tutChooserDlg = document.getElementById('tutorialChooserDlg');
    if (_tutChooserDlg) {
      var _chooseBasics = _tutChooserDlg.querySelector('#tutChooseBasics');
      var _chooseAdv    = _tutChooserDlg.querySelector('#tutChooseAdvanced');
      var _chooseCancel = _tutChooserDlg.querySelector('#tutChooseCancel');
      if (_chooseBasics) _chooseBasics.addEventListener('click', function() {
        try { _tutChooserDlg.close(); } catch (_) {}
        startMainTutorial('basics', 0);
      });
      if (_chooseAdv) _chooseAdv.addEventListener('click', function() {
        try { _tutChooserDlg.close(); } catch (_) {}
        startMainTutorial('advanced', 0);
      });
      if (_chooseCancel) _chooseCancel.addEventListener('click', function() {
        try { _tutChooserDlg.close(); } catch (_) {}
      });
    }

    var helpBtnMain = document.getElementById('helpBtnMain');
    if (helpBtnMain) {
      helpBtnMain.addEventListener('click', function() { openTutorialChooser(); });
    }

    _tutEl('#tutorialNext').addEventListener('click', _tutAdvance);
    _tutEl('#tutorialBack').addEventListener('click', _tutGoBack);
    _tutEl('#tutorialSkip').addEventListener('click', function() {
      _closeMainTutorial();
    });

    // Escape closes tutorial
    document.addEventListener('keydown', function(ev) {
      if (!_tutEl('#tutorialOverlay').classList.contains('active')) return;
      if (!_tutBranch) return;
      if (ev.key === 'Escape') { _closeMainTutorial(); }
    });

    // Welcome panel Tutorial link
    var welcomeTutLink = document.getElementById('welcomeTutorialLink');
    if (welcomeTutLink) {
      welcomeTutLink.addEventListener('click', function(e) {
        e.preventDefault();
        openTutorialChooser();
      });
    }

    // Auto-start tutorial on first launch (pywebview mode only) — Basics branch.
    (async function() {
      if (!hasPywebviewApi) return;
      await new Promise(function(r) { setTimeout(r, 800); });
      var seen = await checkMainTutorialSeen();
      if (!seen) {
        startMainTutorial('basics', 0);
      }
    })();

