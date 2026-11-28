// Styled confirm and notice dialogs, used instead of the browser's native
// confirm() / alert() boxes (unstyled, OS-dependent and not translatable).
//
//   if (!(await KestrelDialog.confirm('cloudCancelJob'))) return;
//   await KestrelDialog.notice('mergeDifferentFolders');
//   await KestrelDialog.confirm('upgradeFolder', { name });
//
// All of the wording lives in MESSAGES below, keyed by id, so it can be moved
// into a translation file later. {placeholders} are filled from the params
// object; values are inserted as text, never as HTML.
(function () {
  const MESSAGES = {
    // Generic button labels.
    ok: 'OK',
    cancel: 'Cancel',

    desktopOnlyQueue: {
      title: 'Desktop app only',
      body: 'The analysis queue is only available in the Kestrel desktop app.',
    },
    desktopOnlyPicker: {
      title: 'Desktop app only',
      body: 'The folder picker is only available in the Kestrel desktop app.',
    },
    folderPickerFailed: {
      title: 'Folder picker failed',
      body: 'The folder picker could not open.\n\n{error}',
    },
    loadDatabaseFailed: {
      title: 'No Kestrel data in this folder',
      body: 'Kestrel could not load analysis data from this folder.\n\n'
        + 'Check that the folder has been analyzed with Kestrel and that you picked the folder '
        + 'itself, not a parent folder. On macOS the .kestrel folder is hidden; press '
        + 'Cmd+Shift+. in Finder to show it.\n\nError: {error}',
    },
    queueStartFailed: {
      title: 'Analysis did not start',
      body: 'The analysis queue could not start.\n\n{error}',
    },
    queueCancel: {
      title: 'Cancel the analysis queue?',
      body: 'Folders still waiting in the queue will not be analyzed.',
      confirm: 'Cancel queue',
      cancel: 'Keep running',
      danger: true,
    },
    reanalyzeErasesData: {
      title: 'Erase data and re-analyze?',
      body: 'These folders will lose all ratings, decisions and scene names:\n\n{names}',
      confirm: 'Erase data and analyze',
      danger: true,
    },
    revertChanges: {
      title: 'Revert unsaved changes?',
      body: 'All unsaved changes are discarded and the last saved state is restored.',
      confirm: 'Revert',
      danger: true,
    },
    cloudCancelJob: {
      title: 'Cancel this cloud job?',
      body: 'Uploaded images are deleted from the server. Results already downloaded stay on disk.',
      confirm: 'Cancel job',
      cancel: 'Keep running',
      danger: true,
    },
    cloudCancelJobs: {
      title: 'Cancel {count} cloud jobs?',
      body: 'Uploaded images are deleted from the server. Results already downloaded stay on disk.',
      confirm: 'Cancel jobs',
      cancel: 'Keep running',
      danger: true,
    },
    resetAllDecisions: {
      title: 'Reset all decisions?',
      body: 'Every manual and confirmed culling decision in “{name}” is reset. This cannot be undone.',
      confirm: 'Reset all',
      danger: true,
    },
    rescoreSpecies: {
      title: 'Re-score species?',
      body: 'Species in “{name}” are re-scored for {regions}.\n\n'
        + 'Kestrel reuses the data saved during analysis, so photos are not re-analyzed. '
        + 'Species you edited by hand and non-bird wildlife are not changed.',
      confirm: 'Re-score',
    },
    perchUnlink: {
      title: 'Unlink this folder from Perch?',
      body: 'The perch on projectkestrel.org is not deleted; delete it there to remove it from the web. '
        + 'This only removes the local link, so you can upload a fresh perch from this folder.',
      confirm: 'Unlink',
      danger: true,
    },
    perchDiscardPartial: {
      title: 'Discard the partial upload?',
      body: 'The unfinished perch is deleted from Perch and the local progress file is removed. '
        + 'Your folder is not changed.',
      confirm: 'Discard upload',
      danger: true,
    },
    qualityFeedbackForget: {
      title: 'Delete shared quality feedback?',
      body: 'All the quality feedback this computer has shared is deleted, and your anonymous '
        + 'feedback id is reset.',
      confirm: 'Delete feedback',
      danger: true,
    },
    crashReportAfterCrash: {
      title: 'Kestrel closed unexpectedly',
      body: 'The previous session crashed. Send a crash report now?',
      confirm: 'Send report',
      cancel: 'Not now',
    },
    crashReportAfterUncleanExit: {
      title: 'Kestrel did not close cleanly',
      body: 'This is sometimes caused by a system shutdown or power loss. Send a report anyway?',
      confirm: 'Send report',
      cancel: 'Not now',
    },
    crashReportFailed: {
      title: 'Report not sent',
      body: 'The crash report could not be sent.\n\n{error}',
    },
    upgradeFolder: {
      title: 'Upgrade “{name}” to analysis version 3.0?',
      body: 'Kestrel re-reads the bird crops and previews it already saved (no RAW decoding) to '
        + 'update species, add the new sharpness score and build the smart search index. '
        + 'Decisions, star ratings and edited tags are not changed, and the database is backed up '
        + 'first. Runs on this computer (uses the GPU when enabled).{skipped}',
      confirm: 'Upgrade',
    },
    upgradeFolders: {
      title: 'Upgrade {count} folders to analysis version 3.0?',
      body: 'They are upgraded one after another; follow progress in the analysis queue panel. '
        + 'Kestrel re-reads the bird crops and previews it already saved (no RAW decoding). '
        + 'Decisions, star ratings and edited tags are not changed, and each database is backed up first.'
        + '{skipped}',
      confirm: 'Upgrade',
    },
    upgradeFoldersSkipped: '\n\n{count} other folder(s) need a full re-analysis instead and are skipped.',
    mergeDifferentFolders: {
      title: 'Can’t merge these scenes',
      body: 'The selected scenes come from different folders. Select scenes from one folder only.',
    },
  };

  function fill(template, params) {
    return String(template).replace(/\{(\w+)\}/g, (m, key) =>
      (params && params[key] != null) ? String(params[key]) : m);
  }

  /** The text for a message id, with placeholders filled. */
  function text(id, params) {
    const msg = MESSAGES[id];
    if (msg == null) throw new Error(`KestrelDialog: unknown message "${id}"`);
    if (typeof msg === 'string') return fill(msg, params);
    const out = {};
    for (const k of Object.keys(msg)) out[k] = typeof msg[k] === 'string' ? fill(msg[k], params) : msg[k];
    return out;
  }

  function open(msg, withCancel) {
    return new Promise(resolve => {
      const dlg = document.createElement('dialog');
      dlg.className = 'kdlg kdlg--sm kdlg--flex k-ask';
      dlg.setAttribute('aria-labelledby', 'kAskTitle');

      const body = document.createElement('div');
      body.className = 'kdlg-body';
      const title = document.createElement('h3');
      title.className = 'k-dialog-title';
      title.id = 'kAskTitle';
      title.textContent = msg.title || '';
      const p = document.createElement('p');
      p.className = 'k-ask-body';
      p.textContent = msg.body || '';
      body.append(title, p);

      const footer = document.createElement('div');
      footer.className = 'kdlg-footer';
      const ok = document.createElement('button');
      ok.type = 'button';
      ok.className = msg.danger ? 'k-btn--danger-solid' : 'primary';
      ok.textContent = msg.confirm || MESSAGES.ok;
      if (withCancel) {
        const cancel = document.createElement('button');
        cancel.type = 'button';
        cancel.className = 'k-btn--quiet';
        cancel.textContent = msg.cancel || MESSAGES.cancel;
        cancel.addEventListener('click', () => dlg.close('cancel'));
        footer.append(cancel);
      }
      footer.append(ok);
      ok.addEventListener('click', () => dlg.close('ok'));

      dlg.append(body, footer);
      // Escape fires 'cancel' and then closes with an empty returnValue.
      dlg.addEventListener('close', () => {
        dlg.remove();
        resolve(dlg.returnValue === 'ok');
      });
      document.body.append(dlg);
      dlg.showModal();
      // Destructive questions start on Cancel so Enter never confirms them.
      const first = (withCancel && msg.danger) ? footer.firstElementChild : ok;
      first.focus();
    });
  }

  /** Ask a yes/no question. Resolves true only when the confirm button is used. */
  function confirm(id, params) {
    return open(text(id, params), true);
  }

  /** Show a message with a single OK button. Resolves when it is closed. */
  async function notice(id, params) {
    await open(text(id, params), false);
  }

  window.KestrelDialog = { confirm, notice, text, MESSAGES };
})();
