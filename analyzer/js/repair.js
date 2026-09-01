    // ── Folder Drift Detection & Repair ─────────────────────────────────────
    //
    // Kestrel keys a database row to a photo by name and location, and the
    // filesystem lets the user change both without telling the app. Moving
    // images out of a folder leaves their analysis behind; renaming one orphans
    // its row. None of that used to be visible — the folder simply showed the
    // wrong thing, silently.
    //
    // This module runs a diff after every folder load (off the critical path:
    // the CSV renders first, the diff arrives later), badges the affected
    // folders, and offers a repair the user confirms explicitly.
    //
    // THE SAFETY RULE: no repair action is EVER offered unless
    // diff.scan_status === 'ok'. An unreadable folder — a stale macOS
    // security-scoped bookmark, an unresponsive network share, an external
    // drive mid-reconnect — produces an empty listing that is indistinguishable
    // from "every photo was deleted", and that is precisely the state in which
    // offering to delete a database would destroy a library that is perfectly
    // fine. See folder_inspector.scan_folder_images for why the distinction
    // cannot be recovered after the fact.

    // rootPath → diff payload from api.compute_folder_diff
    const _repairDiffs = new Map();
    // Roots the user dismissed this session; cleared on next launch.
    const _repairDismissed = new Set();
    let _repairDialogRoot = '';
    let _repairScanVersion = 0;

    function _repairNorm(p) {
        return String(p || '').replace(/\\/g, '/').replace(/\/+$/, '');
    }

    function _repairBaseName(p) {
        const s = _repairNorm(p);
        const i = s.lastIndexOf('/');
        return i === -1 ? s : s.slice(i + 1);
    }

    function _repairPlural(n, one, many) {
        return `${n} ${n === 1 ? one : many}`;
    }

    /**
     * Find a stored diff for a path, tolerating case differences.
     *
     * The diff is keyed by the path the caller passed in, but the folder tree
     * and the scene-grid headers can hold the bridge's *resolved* realpath for
     * the same folder — and on Windows those routinely differ in case
     * (`E:/Photos` vs `E:/photos`), which would silently lose the badge. Exact
     * match is tried first so a genuinely case-sensitive filesystem still
     * behaves correctly; the caseless pass only ever runs when nothing matched.
     */
    function _repairLookup(rootPath) {
        const key = _repairNorm(rootPath);
        if (!key) return null;
        if (_repairDiffs.has(key)) return { key, diff: _repairDiffs.get(key) };
        const lower = key.toLowerCase();
        for (const [k, v] of _repairDiffs) {
            if (k.toLowerCase() === lower) return { key: k, diff: v };
        }
        return null;
    }

    /** True if this folder has drift worth showing the user. */
    function folderHasDrift(rootPath) {
        const hit = _repairLookup(rootPath);
        if (!hit || hit.diff.scan_status !== 'ok') return false;
        if (_repairDismissed.has(hit.key)) return false;
        return !!hit.diff.has_drift;
    }

    /** Short label for the tree badge / button, or '' when there is nothing to say. */
    function folderDriftSummary(rootPath) {
        const diff = _repairLookup(rootPath)?.diff;
        if (!diff || diff.scan_status !== 'ok' || !diff.has_drift) return '';
        const parts = [];
        if (diff.missing?.length) parts.push(`${diff.missing.length} not found`);
        if (diff.renamed?.length) parts.push(`${diff.renamed.length} renamed`);
        if (diff.new?.length) parts.push(`${diff.new.length} unanalyzed`);
        return parts.join(' · ');
    }

    /**
     * Diff every given root against its database, then update the UI.
     *
     * Fire-and-forget by design: the folder view has already rendered from the
     * CSV by the time this runs, and a slow or failing scan must never hold up
     * or break that. In the steady state each root costs one directory
     * enumeration, which the app already pays for elsewhere.
     */
    async function refreshRepairState(paths) {
        if (!hasPywebviewApi || !window.pywebview?.api?.compute_folder_diff) return;
        const roots = Array.from(new Set((paths || []).map(_repairNorm).filter(Boolean)));
        if (!roots.length) return;

        const myVer = ++_repairScanVersion;
        for (const root of roots) {
            try {
                const res = await window.pywebview.api.compute_folder_diff(root);
                // A newer load superseded this scan; its results are for a view
                // that is no longer on screen.
                if (myVer !== _repairScanVersion) return;
                if (res?.success && res.diff) {
                    _repairDiffs.set(root, res.diff);
                    if (res.diff.scan_status === 'unreadable') {
                        console.warn('[repair] Could not read', root, '—', res.diff.scan_error);
                    }
                }
            } catch (e) {
                console.warn('[repair] diff failed for', root, e);
            }
        }
        syncRepairIndicators();
    }

    /**
     * Patch the already-rendered tree rows and folder headers.
     *
     * The diff arrives after render, so this updates the DOM in place rather
     * than forcing a full re-render of the scene grid, which would be both
     * expensive and visually disruptive mid-browse.
     */
    function syncRepairIndicators() {
        // Folder-group headers in the scene grid.
        document.querySelectorAll('[data-repair-root]').forEach(btn => {
            const root = _repairNorm(btn.dataset.repairRoot);
            const show = folderHasDrift(root);
            btn.classList.toggle('hidden', !show);
            if (show) {
                const summary = folderDriftSummary(root);
                btn.title = summary
                    ? `Kestrel data and photos no longer match: ${summary}`
                    : 'Repair this folder’s Kestrel data';
            }
        });

        // Sidebar tree rows.
        document.querySelectorAll('#folderTree [data-path]').forEach(row => {
            const root = _repairNorm(row.dataset.path);
            const existing = row.querySelector('.tree-repair-warn');
            if (!folderHasDrift(root)) {
                if (existing) existing.remove();
                return;
            }
            const summary = folderDriftSummary(root);
            if (existing) {
                existing.title = summary;
                return;
            }
            const warn = document.createElement('span');
            warn.className = 'tree-repair-warn';
            warn.textContent = '⚠';
            warn.title = summary;
            // Sits on the right edge beside the other status glyphs
            // (tree-perch-feather, tree-in-progress-hourglass).
            const cbCol = row.querySelector('.tree-cb-col');
            if (cbCol) row.insertBefore(warn, cbCol);
            else row.appendChild(warn);
        });
    }

    // ---- Dialog ------------------------------------------------------------

    function _repairSection(titleText) {
        const box = document.createElement('div');
        box.className = 'row';
        box.style.cssText = 'flex-direction:column;align-items:stretch;gap:6px;'
            + 'border:1px solid #1f2533;border-radius:10px;padding:10px;'
            + 'background:#10131a;margin-bottom:10px';
        const h = document.createElement('div');
        h.style.cssText = 'font-weight:600';
        h.textContent = titleText;
        box.appendChild(h);
        return box;
    }

    function _repairText(box, text, muted = true) {
        const d = document.createElement('div');
        d.className = muted ? 'muted' : '';
        d.style.cssText = 'font-size:12px;line-height:1.45';
        d.textContent = text;
        box.appendChild(d);
        return d;
    }

    function _repairFileList(box, names, limit = 12) {
        const list = document.createElement('div');
        list.style.cssText = 'font-family:monospace;font-size:11px;max-height:120px;'
            + 'overflow:auto;color:#8fa3c8;margin-top:2px';
        const shown = names.slice(0, limit);
        for (const n of shown) {
            const line = document.createElement('div');
            line.textContent = n;
            list.appendChild(line);
        }
        if (names.length > shown.length) {
            const more = document.createElement('div');
            more.textContent = `… and ${names.length - shown.length} more`;
            more.style.color = '#6a7d9c';
            list.appendChild(more);
        }
        box.appendChild(list);
        return list;
    }

    function _repairActions(box) {
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin-top:4px';
        box.appendChild(row);
        return row;
    }

    function _repairButton(row, label, onClick, opts = {}) {
        const b = document.createElement('button');
        b.textContent = label;
        if (opts.primary) b.className = 'primary';
        if (opts.danger) b.classList.add('danger-item');
        b.addEventListener('click', onClick);
        row.appendChild(b);
        return b;
    }

    async function _repairReload() {
        try {
            if (typeof checkedFolderPaths !== 'undefined' && checkedFolderPaths.size > 0
                && typeof loadMultipleFolders === 'function') {
                await loadMultipleFolders(Array.from(checkedFolderPaths));
            }
        } catch (e) {
            console.warn('[repair] reload after repair failed', e);
        }
        try { if (typeof renderFolderTree === 'function') renderFolderTree(); } catch (_) { }
    }

    async function _repairAfterAction(root, message) {
        showToast(message, 4000);
        _repairDialogRoot = '';
        document.getElementById('repairDlg')?.close();
        await _repairReload();
        await refreshRepairState([root]);
    }

    function openRepairDialog(rootPath) {
        const dlg = document.getElementById('repairDlg');
        const body = document.getElementById('repairDlgBody');
        const folderEl = document.getElementById('repairDlgFolder');
        if (!dlg || !body) return;

        // Act on the key the diff is actually stored under, so every repair call
        // targets the same path the scan examined.
        const hit = _repairLookup(rootPath);
        const root = hit ? hit.key : _repairNorm(rootPath);
        const diff = hit?.diff;

        _repairDialogRoot = root;
        folderEl.textContent = root;
        body.innerHTML = '';

        // ---- The safety gate --------------------------------------------------
        // Anything other than a clean read means we do not know what is in this
        // folder, so we describe the problem and offer no actions at all.
        if (!diff || diff.scan_status !== 'ok') {
            const box = _repairSection('Kestrel couldn’t read this folder');
            _repairText(box,
                'The folder is there, but its contents could not be listed. That usually '
                + 'means a drive or network share is disconnected or still reconnecting, or '
                + 'that Kestrel’s permission to read the folder needs to be granted again.');
            _repairText(box, 'No analysis data has been changed. Reconnect the drive or reopen '
                + 'the folder, and Kestrel will check again.');
            if (diff?.scan_error) {
                const detail = document.createElement('div');
                detail.style.cssText = 'font-family:monospace;font-size:11px;color:#6a7d9c;margin-top:4px';
                detail.textContent = diff.scan_error;
                box.appendChild(detail);
            }
            body.appendChild(box);
            if (typeof dlg.showModal === 'function') dlg.showModal();
            return;
        }

        let sections = 0;

        // ---- Renamed ---------------------------------------------------------
        if (diff.renamed?.length) {
            sections++;
            const box = _repairSection(
                `${_repairPlural(diff.renamed.length, 'photo appears', 'photos appear')} to have been renamed`);
            _repairText(box, 'Kestrel matched these by file size. Check the pairs below before '
                + 'confirming — applying a wrong match would move a photo’s rating and '
                + 'culling decision onto a different photo.');
            const list = document.createElement('div');
            list.style.cssText = 'font-family:monospace;font-size:11px;max-height:140px;'
                + 'overflow:auto;color:#8fa3c8;margin-top:2px';
            for (const pair of diff.renamed) {
                const line = document.createElement('div');
                line.textContent = `${pair.from}  →  ${pair.to}`;
                list.appendChild(line);
            }
            box.appendChild(list);
            const actions = _repairActions(box);
            _repairButton(actions, 'Update Kestrel Data', async () => {
                const res = await window.pywebview.api.repair_apply_renames(root, diff.renamed);
                if (!res?.success) { showToast('Repair failed: ' + (res?.error || ''), 6000); return; }
                await _repairAfterAction(root, `Updated ${_repairPlural(res.renamed, 'renamed photo', 'renamed photos')}.`);
            }, { primary: true });
            body.appendChild(box);
        }

        // ---- Missing, with a located subfolder --------------------------------
        for (const hit of (diff.subfolder_hits || [])) {
            sections++;
            const box = _repairSection(`Found in “${hit.name}”`);
            _repairText(box,
                `${_repairPlural(hit.count, 'photo', 'photos')} Kestrel couldn’t find here `
                + `appear to be in the “${hit.name}” subfolder.`);
            _repairFileList(box, hit.filenames);
            const actions = _repairActions(box);
            _repairButton(actions, `Move Analysis Data to “${hit.name}”`, async () => {
                const res = await window.pywebview.api.repair_relocate(root, hit.path, hit.filenames);
                if (!res?.success) { showToast('Move failed: ' + (res?.error || ''), 6000); return; }
                await _repairAfterAction(root,
                    `Moved analysis data for ${_repairPlural(res.moved, 'photo', 'photos')} to “${hit.name}”.`);
            }, { primary: true });
            body.appendChild(box);
        }

        // ---- Missing ---------------------------------------------------------
        if (diff.missing?.length) {
            sections++;
            const total = diff.db_rows || 0;
            const allGone = total > 0 && diff.missing.length >= total;
            const box = _repairSection(
                `Kestrel couldn’t find ${_repairPlural(diff.missing.length, 'photo', 'photos')} on the disk`);
            if (allGone) {
                // Every photo gone with nothing new in its place is far more
                // often a folder the user emptied on purpose than a deletion
                // they want Kestrel to act on, so lead with the cautious reading.
                _repairText(box, 'Every photo this folder had analysis data for is gone. If you '
                    + 'moved them somewhere else, point Kestrel at their new home rather than '
                    + 'deleting the data — that keeps your ratings and culling decisions.');
            }
            _repairFileList(box, diff.missing);
            const actions = _repairActions(box);
            _repairButton(actions, 'I moved them…', async () => {
                // choose_directory returns a list of paths (empty if cancelled).
                const picked = await window.pywebview.api.choose_directory();
                const dest = Array.isArray(picked) ? picked[0] : picked;
                if (!dest) return;
                const res = await window.pywebview.api.repair_relocate(root, dest, diff.missing);
                if (!res?.success) {
                    showToast(res?.reason === 'destination_has_kestrel'
                        ? 'That folder already has Kestrel analysis data. Moving into an already-analyzed folder isn’t supported yet.'
                        : 'Move failed: ' + (res?.error || ''), 7000);
                    return;
                }
                if (!res.moved) {
                    showToast('None of those photos are in that folder.', 5000);
                    return;
                }
                await _repairAfterAction(root,
                    `Moved analysis data for ${_repairPlural(res.moved, 'photo', 'photos')}.`);
            }, { primary: true });
            _repairButton(actions, 'I deleted them', async () => {
                const res = await window.pywebview.api.repair_forget_missing(root, diff.missing);
                if (!res?.success) { showToast('Repair failed: ' + (res?.error || ''), 6000); return; }
                await _repairAfterAction(root,
                    `Removed analysis data for ${_repairPlural(res.removed, 'photo', 'photos')}.`);
            }, { danger: true });
            body.appendChild(box);
        }

        // ---- New -------------------------------------------------------------
        if (diff.new?.length) {
            sections++;
            const box = _repairSection(
                `${_repairPlural(diff.new.length, 'photo has', 'photos have')} not been analyzed`);
            _repairText(box, 'These are in the folder but have no Kestrel analysis data yet. '
                + 'Analyze the folder to include them — already-analyzed photos are skipped.');
            _repairFileList(box, diff.new);
            body.appendChild(box);
        }

        // ---- Rejected (interrupted cull) -------------------------------------
        if (diff.rejected?.length) {
            sections++;
            const box = _repairSection('Rejected photos');
            _repairText(box,
                `${_repairPlural(diff.rejected.length, 'photo is', 'photos are')} in the `
                + '_KESTREL_Rejects folder but still has analysis data here. This normally '
                + 'clears itself; it can linger if a culling move was interrupted.');
            _repairFileList(box, diff.rejected);
            body.appendChild(box);
        }

        if (!sections) {
            const box = _repairSection('Nothing to repair');
            _repairText(box, 'This folder’s photos and Kestrel data are in sync.');
            body.appendChild(box);
        }

        if (typeof dlg.showModal === 'function') dlg.showModal();
    }

    (function wireRepairDialog() {
        // Close leaves the badge up. Looking at the drift does not resolve it,
        // and hiding the only route back to the dialog because the user glanced
        // at it would be worse than a small persistent marker. Silencing it is
        // a separate, explicit choice — the same split raw-warn.js uses.
        const closeBtn = document.getElementById('repairDlgClose');
        if (closeBtn) {
            closeBtn.addEventListener('click', () => {
                _repairDialogRoot = '';
                document.getElementById('repairDlg')?.close();
            });
        }

        const dismissBtn = document.getElementById('repairDlgDismiss');
        if (dismissBtn) {
            dismissBtn.addEventListener('click', () => {
                // Session-scoped only: the drift is real and unfixed, so this
                // choice should not outlive the session that made it.
                if (_repairDialogRoot) _repairDismissed.add(_repairDialogRoot);
                _repairDialogRoot = '';
                document.getElementById('repairDlg')?.close();
                syncRepairIndicators();
            });
        }
    })();
