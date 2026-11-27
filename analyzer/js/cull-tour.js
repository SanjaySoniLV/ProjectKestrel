// Cull mode's own tour. It runs on the shared tutorial engine (js/tutorial.js,
// branch 'cull') with steps that point at Cull mode's controls, so it can only
// start while Cull mode is open: the first time a folder enters it, or from
// the "How it works" button in its top bar.
(function () {
  const SEEN_KEY = 'cull_tour_seen';

  const STEPS = [
    {
      title: 'Cull mode: one question per photo',
      body: 'Cull mode asks one thing of every photo: <b>keep it or not?</b> Stars, species and scene names wait for later; here you only <b>accept</b> or <b>reject</b>.<br><br>Kestrel does the first pass for you. You check its picks, scene by scene, and fix the ones it got wrong.<br><br><b>Nothing is deleted.</b> Rejects move to a <code>_KESTREL_Rejects</code> folder when you finish, and every step can be undone.',
      target: null,
    },
    {
      title: 'Starting picks',
      body: 'Kestrel starts by accepting photos at or above a <b>Quality</b> bar, keeping <b>at least</b> (and optionally <b>at most</b>) a set number per scene. Change the rules here any time.<br><br>A <b>dotted</b> border is Kestrel’s suggestion. It turns <b>solid</b> once you’ve decided or confirmed it. Your own decisions are never overwritten by the rules.',
      target: '.cm-top [data-act="picks"]',
      position: 'bottom',
    },
    {
      title: 'Decide fast',
      body: 'Hover or use <kbd>←</kbd> <kbd>→</kbd> to move through the scene. The arrows stop at the last photo, so you know you’ve seen them all.<br><br>• <kbd>Space</kbd> flips accept / reject, or use <kbd>Z</kbd> accept and <kbd>C</kbd> reject<br>• <kbd>Shift</kbd>+<kbd>Z</kbd> / <kbd>Shift</kbd>+<kbd>C</kbd> accepts or rejects the whole scene<br>• <kbd>F</kbd> marks a favorite. Favorites always start accepted<br>• Drag the handle between photos to move Kestrel’s cut',
      target: '.cm-strip',
      position: 'top',
    },
    {
      title: 'Look closer',
      body: 'Click and hold the full image to zoom into the RAW. The bird crop sits beside it.<br><br>When a photo has more than one bird, click a box to pick that bird, and press <kbd>S</kbd> to focus the whole scene on it. Kestrel then scores the scene for that bird and works out its starting picks again.',
      target: '.cm-pv--full',
      position: 'right',
    },
    {
      title: 'Confirm and move on',
      body: '<kbd>Enter</kbd> confirms the scene as it stands and opens the next one. <kbd>Tab</kbd> skips a scene and leaves its picks as suggestions; it stays in the <b>Unconfirmed</b> list until you come back to it.',
      target: '.cm-footact [data-act="next"]',
      position: 'top',
    },
    {
      title: 'Find your way',
      body: 'The left pane lists every scene <b>by species</b> or <b>by time</b>, with how many you’ve confirmed. <b>Unconfirmed</b> shows only the scenes you still need to check, and <b>Favorites</b> gathers every photo you’ve marked.',
      target: '.cm-rail',
      position: 'right',
    },
    {
      title: 'Finish a pass',
      body: '<b>Finish</b> first sends you to any scene you haven’t confirmed. Then it moves the rejects to <code>_KESTREL_Rejects</code>, can write the decisions into your photos’ metadata, and offers a <b>second pass</b>: a stricter look at only the photos you kept.',
      target: '.cm-top [data-act="finish"]',
      position: 'bottom',
    },
    {
      title: 'Change your mind any time',
      body: '<b>Culling history</b> lists every pass you’ve finished:<br><br>• <b>Undo move</b> puts the last pass’s rejects back in the folder and keeps your decisions<br>• <b>Revert to this pass</b> moves back every photo later passes rejected and returns all decisions to the end of that pass<br>• <b>Reset folder</b> moves every photo back and clears all decisions<br><br>Favorites are always kept. The <b>Pass</b> badge at top left also lets you look back at an earlier pass without changing it.',
      target: '.cm-top [data-act="history"]',
      position: 'bottom',
    },
    {
      title: 'Ready',
      body: 'Next you’ll set the starting picks for this folder. Then press <kbd>Enter</kbd> on each scene you agree with.<br><br>Replay this tour from <b>How it works</b> at the top of Cull mode.',
      target: null,
    },
  ];

  let onDone = null;

  function start(after) {
    onDone = typeof after === 'function' ? after : null;
    startMainTutorial('cull', 0);
  }

  /** Called by the tutorial engine when the cull tour closes (finished or skipped). */
  function closed() {
    try { const st = loadSettings(); st[SEEN_KEY] = true; saveSettings(st); } catch (e) { console.warn('[cull-tour] could not save seen flag', e); }
    const fn = onDone;
    onDone = null;
    if (fn) fn();
  }

  function seen() { return !!getSetting(SEEN_KEY, false); }

  function active() { return !!document.getElementById('tutorialOverlay')?.classList.contains('active'); }

  window.KestrelCullTour = { STEPS, start, closed, seen, active };
})();
