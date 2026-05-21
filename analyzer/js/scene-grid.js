    // Folder Actions dropdown — module-scoped state so only one menu is open
    // at a time across multiple folder groups. The trigger element is tracked
    // so re-clicking the same trigger closes (toggle behavior).
    let _openFolderActionsMenu = null;
    let _openFolderActionsTrigger = null;

    function _closeFolderActionsMenu() {
      if (_openFolderActionsMenu && _openFolderActionsMenu.parentNode) {
        _openFolderActionsMenu.parentNode.removeChild(_openFolderActionsMenu);
      }
      _openFolderActionsMenu = null;
      _openFolderActionsTrigger = null;
    }

    // One-time global listeners for outside-click and Escape.
    document.addEventListener('mousedown', (ev) => {
      if (!_openFolderActionsMenu) return;
      if (_openFolderActionsMenu.contains(ev.target)) return;
      if (_openFolderActionsTrigger && _openFolderActionsTrigger.contains(ev.target)) return;
      _closeFolderActionsMenu();
    });
    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && _openFolderActionsMenu) _closeFolderActionsMenu();
    });

    function _buildFolderActionsMenu(folderPath, refreshCallback) {
      const menu = document.createElement('div');
      menu.className = 'folder-group-actions-menu';

      const items = [
        { icon: '📂', label: 'Open in File Explorer',
          run: () => window.pywebview.api.open_file_explorer(folderPath) },
        { icon: '⏱', label: 'Adjust Capture Time',
          run: () => showAdjustCaptureTimeDialog(folderPath) },
        { icon: '↺', label: 'Reset Culling Decisions',
          run: () => showFolderOptionsDialog(folderPath) },
        { icon: '📝', label: 'Write Photo Metadata',
          run: () => writeMetadataForFolder(folderPath) },
        { divider: true },
        { icon: '🗑', label: 'Clear Kestrel Analysis Data', danger: true,
          run: () => {
            const folderName = (folderPath || '').split(/[\\/]/).filter(Boolean).pop() || folderPath;
            clearKestrelDataForFolder(folderPath, folderName, refreshCallback);
          } },
      ];

      for (const it of items) {
        if (it.divider) {
          const d = document.createElement('div');
          d.className = 'folder-group-actions-divider';
          menu.appendChild(d);
          continue;
        }
        const row = document.createElement('div');
        row.className = 'folder-group-actions-item' + (it.danger ? ' folder-group-actions-item--danger' : '');
        row.innerHTML = `<i>${it.icon}</i> ${it.label}`;
        row.addEventListener('click', (ev) => {
          ev.stopPropagation();
          _closeFolderActionsMenu();
          try { it.run(); } catch (e) { console.error('[folder-actions]', it.label, e); }
        });
        menu.appendChild(row);
      }
      return menu;
    }

    async function renderScenes() {
      const myVer = ++_renderScenesVersion;
      const minC = parseFloat(el('#speciesConf').value) || 0;
      const search = el('#search').value;
      const sortBy = el('#sortBy').value;
      const onlyReviewedScenes = !!document.getElementById('filterScenesManualRated')?.checked;
      const groupByFolder = document.getElementById('groupByFolder')?.checked ?? getSetting('groupByFolder', true);
      const groupByTime = document.getElementById('groupByTime')?.checked ?? getSetting('groupByTime', true);
      const showBirdThumbs = document.getElementById('showBirdThumbs')?.checked ?? getSetting('showBirdThumbs', false);
      // Widen each card's grid track when bird-crop thumbs are shown, so the
      // main thumb keeps its "no-crop" height and the crop slots into the
      // extra width rather than stealing height from the main image.
      sceneGrid.classList.toggle('grid--bird-thumbs', !!showBirdThumbs);
      const includeSecondaryCheckbox = document.getElementById('includeSecondarySpecies');
      const includeSecondary = includeSecondaryCheckbox ? includeSecondaryCheckbox.checked : !!getSetting('includeSecondarySpecies', false);
      const includeFamilies = true;
      scenes = aggregateScenes(minC, search, sortBy, includeSecondary, includeFamilies);

      // Re-resolve _currentScene so the open scene dialog keeps working
      // after the scenes array is regenerated with new objects.
      if (_currentScene) {
        const openId = String(_currentScene.id);
        const refreshed = scenes.find(s => String(s.id) === openId);
        if (refreshed) _currentScene = refreshed;
      }

      // Apply scene-level manual-reviewed filter without mutating global scenes.
      // Reviewed means any of:
      //  - scene tags finalized by user, or scene renamed by user
      //  - manual/verified accept-reject culling decision on any image
      //  - manual star rating on any image
      const visibleScenes = onlyReviewedScenes ? scenes.filter(isManuallyReviewedScene) : scenes;

      // Batched hydration of bird-catalog records for every species pill we're
      // about to paint when the show-scientific-names toggle is on. Collect
      // the missing names up-front, fire one IPC call, and re-render once if
      // any new records arrived. Without this batching the per-card loop
      // below would fan out N requests on first paint of a large folder.
      const _showSciOnCards = _getShowSciNames();
      if (_showSciOnCards && !_sceneCardHydrationPending) {
        const need = new Set();
        for (const s of visibleScenes) {
          // Hydrate every species and family term on the scene. The
          // card's family-fallback scan needs a cached species record
          // per family pill so it can borrow family_sci.
          for (const sp of (s.species || [])) {
            if (sp && !_isBirdRecordKnown(sp)) need.add(sp);
          }
          for (const fm of (s.families || [])) {
            if (fm && !_isBirdRecordKnown(fm)) need.add(fm);
          }
        }
        if (need.size > 0) {
          _sceneCardHydrationPending = true;
          _hydrateBirdRecords(Array.from(need)).then(() => {
            _sceneCardHydrationPending = false;
            // Always queue a repaint -- the cache (and miss-set) just gained
            // entries that change what the cards show. We can't bail on a
            // newer renderScenes call here, because that newer call was
            // gated out of starting its own hydration by the pending flag
            // and built its DOM against the still-empty cache.
            requestAnimationFrame(() => { try { renderScenes(); } catch (_) {} });
          }).catch(() => { _sceneCardHydrationPending = false; });
        }
      }

      updateStatusBar(visibleScenes);

      // Prevent flash-of-empty-content: lock the grid's current height as a
      // minimum so the page layout doesn't collapse while we rebuild the DOM,
      // and save the scroll position of the main container for restoration.
      const mainEl = document.querySelector('main');
      const savedScrollTop = mainEl ? mainEl.scrollTop : 0;
      const currentHeight = sceneGrid.offsetHeight;
      if (currentHeight > 0) {
        sceneGrid.style.minHeight = currentHeight + 'px';
      }
      sceneGrid.innerHTML = '';

      // Show welcome panel when no data is loaded; hide it once a folder is open.
      // The timeline filter bar is the inverse: shown only when scenes exist.
      const _welcomePanel = document.getElementById('welcomePanel');
      if (_welcomePanel) _welcomePanel.classList.toggle('hidden', rows.length > 0);
      const _tfb = document.getElementById('timelineFilterBar');
      if (_tfb) _tfb.classList.toggle('hidden', rows.length === 0);

      // Flat index for shift-click range selection
      _visibleSceneOrder = visibleScenes.map(s => String(s.id));

      // ---- Two-level grouping: folder → adaptive time clusters ----
      //
      // The timeline previously used a fixed 1-hour grid (YYYY-MM-DDTHH) which
      // both over-segmented long sessions (e.g. 55 minutes → 2 nodes straddling
      // the hour boundary) and under-segmented bursty ones (50 shots in 3
      // minutes became a single node identical to 1 shot in 30 minutes).
      // We now cluster by actual gaps between successive scenes so a "session"
      // of continuous shooting is one node, and a quiet pause cuts a new node
      // regardless of clock alignment.
      //
      // The gap threshold is derived from the folder itself: we take the
      // median inter-scene gap and multiply by a constant so anything notably
      // quieter than the folder's typical rhythm starts a new cluster. This
      // keeps dense burst folders (gap of ~1s → ~10s threshold) and casual
      // days (gap of ~30s → ~5min threshold) from needing different settings.
      // Hard floor/ceiling keep pathological data (single-scene folders,
      // multi-week bundles) from producing absurd thresholds.
      const CLUSTER_GAP_MIN_MS = 45 * 1000;       // 45 s — never split bursts
      const CLUSTER_GAP_MAX_MS = 10 * 60 * 1000;  // 10 min — never merge sessions
      const CLUSTER_GAP_FALLBACK_MS = 3 * 60 * 1000; // fallback if we can't infer
      const CLUSTER_GAP_MULTIPLIER = 10;

      function computeDynamicClusterGapMs(scenes) {
        const times = [];
        for (const s of scenes) {
          if (Number.isFinite(s.captureTimeMs)) times.push(s.captureTimeMs);
        }
        if (times.length < 3) return CLUSTER_GAP_FALLBACK_MS;
        times.sort((a, b) => a - b);
        const gaps = [];
        for (let i = 1; i < times.length; i++) {
          const g = times[i] - times[i - 1];
          if (g > 0) gaps.push(g);
        }
        if (!gaps.length) return CLUSTER_GAP_FALLBACK_MS;
        gaps.sort((a, b) => a - b);
        const median = gaps[Math.floor(gaps.length / 2)];
        const threshold = median * CLUSTER_GAP_MULTIPLIER;
        return Math.max(CLUSTER_GAP_MIN_MS, Math.min(CLUSTER_GAP_MAX_MS, threshold));
      }

      function _pad2(n) { return String(n).padStart(2, '0'); }
      function _dayKeyFromMs(ms) {
        if (!Number.isFinite(ms)) return '';
        const d = new Date(ms);
        if (isNaN(d)) return '';
        return `${d.getFullYear()}-${_pad2(d.getMonth()+1)}-${_pad2(d.getDate())}`;
      }
      function formatClusterTime(ms) {
        if (!Number.isFinite(ms)) return '';
        try {
          return new Date(ms).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
        } catch (_) { return ''; }
      }
      function formatClusterDay(ms) {
        if (!Number.isFinite(ms)) return '';
        try {
          return new Date(ms).toLocaleDateString(undefined, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' });
        } catch (_) { return ''; }
      }

      // Walk scenes in ascending time order, cutting a new cluster each time
      // the gap between the last scene's time and the next exceeds the
      // threshold. Scenes lacking capture time are dropped into a final
      // "untimed" cluster so they remain visible but don't distort ranges.
      function buildTimeClusters(scenes, gapMs) {
        const timed = [];
        const untimed = [];
        for (const s of scenes) {
          if (Number.isFinite(s.captureTimeMs)) timed.push(s);
          else untimed.push(s);
        }
        timed.sort((a, b) => a.captureTimeMs - b.captureTimeMs);
        const clusters = [];
        for (const s of timed) {
          const last = clusters[clusters.length - 1];
          if (!last || s.captureTimeMs - last.endMs > gapMs) {
            clusters.push({
              scenes: [s],
              startMs: s.captureTimeMs,
              endMs: s.captureTimeMs,
              untimed: false,
            });
          } else {
            last.scenes.push(s);
            if (s.captureTimeMs > last.endMs) last.endMs = s.captureTimeMs;
          }
        }
        if (untimed.length) {
          clusters.push({ scenes: untimed, startMs: null, endMs: null, untimed: true });
        }
        return clusters;
      }

      // Build folderMap: folderKey → { folderPath, scenes: [ordered scene list] }
      const folderOrder = [];
      const folderMap = new Map();
      for (const s of visibleScenes) {
        const rp = groupByFolder ? (s.representative?.__rootPath || '') : '';
        const fk = rp || '__single__';
        if (!folderMap.has(fk)) { folderMap.set(fk, { folderPath: rp, scenes: [] }); folderOrder.push(fk); }
        folderMap.get(fk).scenes.push(s);
      }
      const showFolderHeaders = groupByFolder;

      function buildCard(s) {
        const card = document.createElement('article');
        card.className = 'card';
        card.dataset.sceneId = String(s.id);
        if (selectedSceneIds.has(String(s.id))) card.classList.add('selected');

        const th = document.createElement('div');
        th.className = 'thumb';
        const img = document.createElement('img');
        img.alt = s.representative?.filename || '';
        applyThumbnailExposureToImg(img, s.representative);
        lazyLoadImg(img, () => getBlobUrlForPath(
          s.representative?.export_path || s.representative?.crop_path,
          s.representative?.__rootPath
        ));
        th.appendChild(img);
        if (showBirdThumbs && s.representative?.crop_path && s.representative?.export_path) {
          const row = document.createElement('div');
          row.className = 'thumb-row';
          const cropWrap = document.createElement('div');
          cropWrap.className = 'thumb-bird-crop';
          const cropImg = document.createElement('img');
          cropImg.alt = 'Bird crop';
          lazyLoadImg(cropImg, () => getBlobUrlForPath(
            s.representative.crop_path,
            s.representative.__rootPath
          ));
          cropWrap.appendChild(cropImg);
          row.appendChild(th);
          row.appendChild(cropWrap);
          card.appendChild(row);
        } else {
          card.appendChild(th);
        }

        const body = document.createElement('div');
        body.className = 'body';
        const _folderName = folderBaseName(s.representative?.__rootPath || '');

        // Secondary title row -- only shown when the scene has a folder
        // prefix (sub-folder name visible because group-by-folder header is
        // off) or a user-set scene name. Otherwise the body collapses to
        // just the chip/meta strip. ``#N`` is no longer rendered: the
        // image and grid position carry the identity.
        // Build any user-controlled substring with textContent only, never
        // innerHTML, per FINDING-01.
        const needTitleRow = !!(s.sceneName || (_folderName && !showFolderHeaders));
        const title = needTitleRow ? document.createElement('div') : null;
        if (title) {
          title.className = 'title';
          if (_folderName && !showFolderHeaders) {
            const folderEl = document.createElement('i');
            folderEl.className = 'folder-name';
            folderEl.textContent = _folderName;
            title.appendChild(folderEl);
            if (s.sceneName) {
              const sep = document.createElement('span');
              sep.className = 'title-sep';
              sep.textContent = ' / ';
              title.appendChild(sep);
            }
          }
          if (s.sceneName) {
            const nameSpan = document.createElement('span');
            nameSpan.className = 'name';
            nameSpan.textContent = String(s.sceneName);
            title.appendChild(nameSpan);
          }
          title.title = (s.representative?.__rootPath || String(s.id)) + (s.sceneName ? ` — ${s.sceneName}` : '');
          body.appendChild(title);
        }
        if (s.isApproved) card.classList.add('scene-approved');

        // Pill list -- prefer species over families. Family pills only
        // appear on the card when the scene has no species tags at all;
        // the dialog still surfaces both tiers. aggregateScenes is the
        // single source of truth for the search predicate (which checks
        // both arrays), so dropping families from the card doesn't hide
        // matches.
        const cardPills = (s.species && s.species.length) ? s.species : (s.families || []);
        const firstPill = cardPills[0] || null;
        const overflowCount = Math.max(0, cardPills.length - 1);

        const metaRow = document.createElement('div');
        metaRow.className = 'card-meta-row';
        if (s.isApproved) metaRow.classList.add('reviewed-tags');

        const pillsWrap = document.createElement('div');
        pillsWrap.className = 'card-pills';
        if (firstPill) {
          const c = document.createElement('span');
          c.className = s.isApproved ? 'chip manual-approved' : 'chip';
          if (_showSciOnCards) c.classList.add('chip--with-sci');
          const primary = document.createElement('span');
          primary.className = 'chip-primary';
          primary.textContent = firstPill;
          c.appendChild(primary);
          let titleStr = firstPill;
          if (_showSciOnCards) {
            const sciText = _resolvePillSci(firstPill, cardPills);
            if (sciText) {
              const sci = document.createElement('span');
              sci.className = 'chip-sci';
              const em = document.createElement('em');
              em.textContent = sciText;
              sci.appendChild(em);
              c.appendChild(sci);
              titleStr = `${firstPill} — ${sciText}`;
            }
          }
          c.title = titleStr;
          pillsWrap.appendChild(c);
        }
        if (overflowCount > 0) {
          const plus = document.createElement('button');
          plus.type = 'button';
          plus.className = 'chip chip-more';
          // Down chevron (▾, U+25BE) signals "reveal more" without
          // overloading the '+' affordance that other parts of the UI
          // (scene-chip-add, chip-add-btn) use for "create new tag".
          plus.textContent = '▾';
          plus.title = `Show ${overflowCount} more`;
          plus.setAttribute('aria-label', `Show ${overflowCount} more tags`);
          plus.addEventListener('click', (ev) => {
            ev.stopPropagation();
            _openMorePillsPopover(plus, cardPills.slice(1), _showSciOnCards, cardPills);
          });
          pillsWrap.appendChild(plus);
        }
        metaRow.appendChild(pillsWrap);

        const meta = document.createElement('div');
        meta.className = 'card-meta';
        // Score + image count on one line, no background pill. Two-decimal
        // quality matches the user-visible precision elsewhere in the UI.
        const scoreEl = document.createElement('span');
        scoreEl.className = 'meta-score';
        // Negative quality is the pipeline's sentinel for "no animal
        // detected" (defaults to -1.00 in that case). Render as an em-dash
        // pair instead of a misleading numeric score.
        scoreEl.textContent = (s.maxQuality >= 0)
          ? `★ ${s.maxQuality.toFixed(2)}`
          : `★ --`;
        meta.appendChild(scoreEl);
        const metaSep = document.createElement('span');
        metaSep.className = 'meta-sep';
        metaSep.textContent = ' | ';
        meta.appendChild(metaSep);
        const countEl = document.createElement('span');
        countEl.className = 'meta-count';
        countEl.textContent = `📸 ${s.imageCount}`;
        meta.appendChild(countEl);
        metaRow.appendChild(meta);

        body.appendChild(metaRow);
        card.appendChild(body);

        card.addEventListener('click', (ev) => {
          const sid = String(s.id);
          _focusGridCard(sid);
          if (ev.shiftKey && _lastSelectedIdx >= 0) {
            const idx = _visibleSceneOrder.indexOf(sid);
            if (idx >= 0) {
              const lo = Math.min(_lastSelectedIdx, idx);
              const hi = Math.max(_lastSelectedIdx, idx);
              for (let i = lo; i <= hi; i++) selectedSceneIds.add(_visibleSceneOrder[i]);
            }
            updateSelectionUI();
            ev.preventDefault(); return;
          }
          if (ev.ctrlKey || ev.metaKey) {
            if (selectedSceneIds.has(sid)) selectedSceneIds.delete(sid); else selectedSceneIds.add(sid);
            _lastSelectedIdx = _visibleSceneOrder.indexOf(sid);
            updateSelectionUI();
            ev.preventDefault(); return;
          }
          if (selectedSceneIds.size > 0) {
            if (selectedSceneIds.has(sid)) selectedSceneIds.delete(sid); else selectedSceneIds.add(sid);
            _lastSelectedIdx = _visibleSceneOrder.indexOf(sid);
            updateSelectionUI();
            return;
          }
          // Normal: open scene dialog
          _lastSelectedIdx = _visibleSceneOrder.indexOf(sid);
          openSceneDialog(sid);
        });
        return card;
      }

      const batch = 24;

      // ---- Timeline builder (used when groupByTime is on) ----
      //
      // Each cluster renders as a timeline node with:
      //   • rail dot (sized by image count) + connecting line
      //   • a header showing the time range, scene count, and image count
      //   • a grid of scene cards
      //
      // The rail dot scales with the cluster's image count so a quick scroll
      // down the left edge makes shooting bursts immediately visible — bigger
      // dot = denser cluster. Sizing uses sqrt so a 200-shot burst isn't 10×
      // the size of a 20-shot one.
      const _DOT_MIN_PX = 8;
      const _DOT_MAX_PX = 26;

      function _clusterImageCount(cluster) {
        let n = 0;
        for (const s of cluster.scenes) n += s.imageCount || 0;
        return n;
      }

      function _dotSizePx(imageCount, maxInFolder) {
        if (!Number.isFinite(imageCount) || imageCount <= 0) return _DOT_MIN_PX;
        if (!Number.isFinite(maxInFolder) || maxInFolder <= 1) return _DOT_MIN_PX + 3;
        const frac = Math.sqrt(imageCount) / Math.sqrt(maxInFolder);
        return Math.round(_DOT_MIN_PX + frac * (_DOT_MAX_PX - _DOT_MIN_PX));
      }

      function buildTimeline(fd, containerEl) {
        const timelineEl = document.createElement('div');
        timelineEl.className = 'timeline-body';
        const gapMs = computeDynamicClusterGapMs(fd.scenes);
        const clusters = buildTimeClusters(fd.scenes, gapMs);
        let prevDay = null;

        // Pre-compute the max image count across all clusters so dot sizes
        // are proportional within this folder (different folders can have
        // wildly different scales and shouldn't compete on the same axis).
        let maxImgCountInFolder = 1;
        for (const c of clusters) {
          const n = _clusterImageCount(c);
          if (n > maxImgCountInFolder) maxImgCountInFolder = n;
        }

        for (let ni = 0; ni < clusters.length; ni++) {
          const cluster = clusters[ni];
          const isLast = ni === clusters.length - 1;
          const thisDay = cluster.untimed ? '' : _dayKeyFromMs(cluster.startMs);

          // Day banner when the calendar date changes between clusters
          if (thisDay && thisDay !== prevDay) {
            const banner = document.createElement('div');
            banner.className = 'timeline-day-banner';
            banner.textContent = formatClusterDay(cluster.startMs);
            timelineEl.appendChild(banner);
            prevDay = thisDay;
          }

          const nodeEl = document.createElement('div');
          nodeEl.className = 'timeline-node' + (cluster.untimed ? ' timeline-node-untimed' : '');

          // Rail column: dot (sized by image count) + connecting line
          const railCol = document.createElement('div');
          railCol.className = 'timeline-rail-col';
          const dot = document.createElement('div');
          dot.className = 'timeline-dot';
          const imgCount = _clusterImageCount(cluster);
          const dotSize = _dotSizePx(imgCount, maxImgCountInFolder);
          dot.style.width = dotSize + 'px';
          dot.style.height = dotSize + 'px';
          // Title gives a fallback tooltip for folks who want the raw number.
          dot.title = `${cluster.scenes.length} scene${cluster.scenes.length === 1 ? '' : 's'} · ${imgCount} image${imgCount === 1 ? '' : 's'}`;
          const line = document.createElement('div');
          line.className = 'timeline-line' + (isLast ? ' last' : '');
          railCol.appendChild(dot);
          railCol.appendChild(line);

          // Content column: header + grid
          const contentCol = document.createElement('div');
          contentCol.className = 'timeline-content-col';

          const hdr = document.createElement('div');
          hdr.className = 'timeline-node-header';

          const timeSpan = document.createElement('span');
          timeSpan.className = 'timeline-node-time';
          if (cluster.untimed) {
            timeSpan.textContent = 'Unknown time';
          } else {
            const spanMs = cluster.endMs - cluster.startMs;
            // Collapse clusters that span less than two minutes to a single
            // time (otherwise the header reads "10:42 AM – 10:42 AM").
            timeSpan.textContent = spanMs < 2 * 60 * 1000
              ? formatClusterTime(cluster.startMs)
              : `${formatClusterTime(cluster.startMs)} – ${formatClusterTime(cluster.endMs)}`;
          }
          hdr.appendChild(timeSpan);

          const countSpan = document.createElement('span');
          countSpan.className = 'timeline-node-count muted';
          countSpan.textContent =
            `${cluster.scenes.length} scene${cluster.scenes.length === 1 ? '' : 's'} · ${imgCount} image${imgCount === 1 ? '' : 's'}`;
          hdr.appendChild(countSpan);

          contentCol.appendChild(hdr);

          const gridEl = document.createElement('div');
          gridEl.className = 'grid timeline-grid';
          contentCol.appendChild(gridEl);

          nodeEl.appendChild(railCol);
          nodeEl.appendChild(contentCol);
          timelineEl.appendChild(nodeEl);

          for (let i = 0; i < cluster.scenes.length; i += batch) {
            if (myVer !== _renderScenesVersion) { sceneGrid.style.minHeight = ''; return; }
            const slice = cluster.scenes.slice(i, i + batch);
            const frag = document.createDocumentFragment();
            for (const s of slice) frag.appendChild(buildCard(s));
            gridEl.appendChild(frag);
          }
        }
        containerEl.appendChild(timelineEl);
      }

      // ---- Main folder rendering loop ----
      for (const fk of folderOrder) {
        const fd = folderMap.get(fk);
        const allScenesInFolder = fd.scenes;
        let bodyEl; // receives the timeline or flat grid

        if (showFolderHeaders && fd.folderPath) {
          const folderName = folderBaseName(fd.folderPath) || fd.folderPath || '(unknown folder)';
          const collapsed = collapsedFolders.has(fk);

          const groupEl = document.createElement('div');
          groupEl.className = 'folder-group';

          const hdr = document.createElement('div');
          hdr.className = 'folder-group-header' + (collapsed ? ' collapsed' : '');
          hdr.innerHTML = `<span class="folder-group-toggle">▼</span><span class="folder-group-name">${escapeHtml(folderName)}</span><span class="folder-group-count muted">${allScenesInFolder.length} scene${allScenesInFolder.length === 1 ? '' : 's'}</span>`;

          // Spacer pushes the action group to the right
          const spacer = document.createElement('div');
          spacer.style.flex = '1';
          hdr.appendChild(spacer);

          // Action group: Folder Actions dropdown + Culling Assistant (primary).
          const rightActions = document.createElement('div');
          rightActions.className = 'folder-group-right-actions';

          // Folder Actions dropdown trigger — collapses the legacy 4 inline
          // buttons (Open, Adjust Capture Time, Reset Culling Decisions,
          // Write Photo Metadata) plus the new Clear Kestrel Analysis Data
          // item behind a single segmented entry. Position the menu relative
          // to this wrapper.
          const actionsWrap = document.createElement('div');
          actionsWrap.className = 'folder-group-actions-wrap';
          actionsWrap.style.position = 'relative';

          const actionsTrigger = document.createElement('button');
          actionsTrigger.className = 'action-btn folder-group-actions-trigger';
          actionsTrigger.innerHTML = '<i>⋯</i> Folder Actions <span class="caret">▾</span>';
          actionsTrigger.title = 'More actions for this folder';
          const _folderPathForMenu = fd.folderPath;
          actionsTrigger.addEventListener('click', (ev) => {
            ev.stopPropagation();
            // Toggle: re-clicking the same trigger closes.
            if (_openFolderActionsTrigger === actionsTrigger) {
              _closeFolderActionsMenu();
              return;
            }
            _closeFolderActionsMenu();
            const refreshCb = () => {
              // Conservative refresh: tell the scene grid to re-render and
              // also recompute the tree. clearKestrelDataForFolder already
              // toggles node.has_kestrel; renderFolderTree picks that up.
              try { if (typeof renderFolderTree === 'function') renderFolderTree(); } catch (_) {}
              try { if (typeof renderScenes === 'function') renderScenes(); } catch (_) {}
            };
            const menu = _buildFolderActionsMenu(_folderPathForMenu, refreshCb);
            actionsWrap.appendChild(menu);
            _openFolderActionsMenu = menu;
            _openFolderActionsTrigger = actionsTrigger;
          });
          actionsWrap.appendChild(actionsTrigger);
          rightActions.appendChild(actionsWrap);

          const cullingBtn = document.createElement('button');
          cullingBtn.className = 'action-btn culling-assistant-btn';
          cullingBtn.innerHTML = '<i>✂</i> Open Culling Assistant';
          cullingBtn.title = 'Open the AI-assisted culling workflow for this folder';
          cullingBtn.addEventListener('click', (ev) => { ev.stopPropagation(); openCullingAssistant(fd.folderPath); });
          rightActions.appendChild(cullingBtn);

          const perchBtn = document.createElement('button');
          perchBtn.className = 'action-btn share-perch-btn';
          perchBtn.innerHTML = '<i>🪶</i> Share with Perch';
          perchBtn.title = 'Create an Unfinished Perch on the web with this folder\u2019s Kestrel analysis (export and crop images)';
          perchBtn.addEventListener('click', (ev) => { ev.stopPropagation(); shareWithPerchFolder(fd.folderPath); });
          rightActions.appendChild(perchBtn);

          // Cloud Compute moved into the unified Analyze Folders dialog
          // (destination toggle: Local / Cloud). The folder-level shortcut
          // button has been retired so cloud and local share a single mental
          // model and entry point.

          // "Published" pill \u2014 only shown if .kestrel/perch_link.json exists.
          // Click opens the perch URL (after stale-link verification, see 1d).
          // Right-click \u2192 Unlink (local-only).
          const perchPill = document.createElement('button');
          perchPill.type = 'button';
          perchPill.className = 'folder-perch-pill hidden';
          perchPill.dataset.folderPath = fd.folderPath;
          perchPill.innerHTML = '<span class="folder-perch-pill-icon"></span><span class="folder-perch-pill-label">Published</span>';
          const _pillIcon = perchPill.querySelector('.folder-perch-pill-icon');
          if (_pillIcon) _pillIcon.textContent = '\u{1FAB6}'; // feather emoji
          perchPill.title = 'Folder published to Perch \u2014 click to open in browser';
          perchPill.addEventListener('click', (ev) => {
            ev.stopPropagation();
            handlePerchPillClick(fd.folderPath, perchPill);
          });
          perchPill.addEventListener('contextmenu', (ev) => {
            ev.preventDefault();
            ev.stopPropagation();
            handlePerchPillUnlink(fd.folderPath, perchPill);
          });
          rightActions.appendChild(perchPill);

          // Phase 3 "Sync to Perch" button is temporarily hidden — the
          // dirty/clean state detection is unreliable (the button greys out
          // when there ARE local edits, etc.). Once that's reworked, restore
          // by re-creating perchSyncBtn here and calling
          // applyPerchLinkToSyncBtn from the link loader below.
          const perchSyncBtn = null;

          // Async-populate from disk; show only if the file exists.
          (async () => {
            try {
              const res = await window.pywebview?.api?.read_perch_link?.(fd.folderPath);
              if (res && res.present && res.link) {
                applyPerchLinkToPill(perchPill, res.link);
              }
            } catch {}
          })();

          hdr.appendChild(rightActions);

          bodyEl = document.createElement('div');
          bodyEl.className = 'folder-group-body' + (collapsed ? ' hidden' : '');

          const _fk = fk, _bodyEl = bodyEl, _hdr = hdr;
          hdr.addEventListener('click', () => {
            if (collapsedFolders.has(_fk)) collapsedFolders.delete(_fk); else collapsedFolders.add(_fk);
            _hdr.classList.toggle('collapsed');
            _bodyEl.classList.toggle('hidden');
          });
          groupEl.appendChild(hdr);
          groupEl.appendChild(bodyEl);
          sceneGrid.appendChild(groupEl);
        } else {
          bodyEl = document.createElement('div');
          sceneGrid.appendChild(bodyEl);
        }

        if (groupByTime) {
          buildTimeline(fd, bodyEl);
        } else {
          const gridEl = document.createElement('div');
          gridEl.className = 'folder-group-grid grid';
          bodyEl.appendChild(gridEl);
          for (let i = 0; i < allScenesInFolder.length; i += batch) {
            if (myVer !== _renderScenesVersion) { sceneGrid.style.minHeight = ''; return; }
            const slice = allScenesInFolder.slice(i, i + batch);
            const frag = document.createDocumentFragment();
            for (const s of slice) frag.appendChild(buildCard(s));
            gridEl.appendChild(frag);
          }
        }
      }

      // Restore scroll position and release the minimum-height lock now that
      // the grid is rebuilt, preventing flash-of-empty-content.
      sceneGrid.style.minHeight = '';
      if (mainEl && savedScrollTop > 0) {
        mainEl.scrollTop = savedScrollTop;
      }
    }

    // Update card highlights and show/hide floating action bar based on current selection
    function updateSelectionUI() {
      const n = selectedSceneIds.size;
      document.querySelectorAll('.card[data-scene-id]').forEach(c => {
        c.classList.toggle('selected', selectedSceneIds.has(c.dataset.sceneId));
      });
      const bar = document.getElementById('selectActionBar');
      if (!bar) return;
      if (n >= 2) {
        bar.classList.remove('hidden');
        const lbl = document.getElementById('selectActionLabel');
        if (lbl) lbl.textContent = `${n} scene${n === 1 ? '' : 's'} selected`;
      } else {
        bar.classList.add('hidden');
      }
    }

    // Scroll to a scene card in the grid and give it keyboard focus
    function _focusGridCard(sceneId) {
      _focusedCardId = String(sceneId);
      document.querySelectorAll('.card.focused').forEach(c => c.classList.remove('focused'));
      const card = sceneGrid.querySelector(`.card[data-scene-id="${CSS.escape(_focusedCardId)}"]`);
      if (card) {
        card.classList.add('focused');
        card.scrollIntoView({ behavior: 'smooth', block: 'center' });
      }
    }

    // Clear the focused-card highlight
    function _clearGridFocus() {
      _focusedCardId = null;
      document.querySelectorAll('.card.focused').forEach(c => c.classList.remove('focused'));
    }

    // Get all visible card elements in DOM order
    function _getVisibleCards() {
      return Array.from(sceneGrid.querySelectorAll('.card[data-scene-id]'));
    }

    // Grid keyboard navigation: arrow keys move focus, Enter opens scene dialog
    function _gridKeyHandler(e) {
      if (document.querySelector('dialog[open]')) return;
      if (selectedSceneIds.size > 0) return;
      const tag = (e.target.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;

      const isArrow = ['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.key);
      const isEnter = e.key === 'Enter';
      if (!isArrow && !isEnter) return;
      if (!_focusedCardId) return;

      e.preventDefault();

      if (isEnter) {
        openSceneDialog(_focusedCardId);
        return;
      }

      const cards = _getVisibleCards();
      if (cards.length === 0) return;
      const curIdx = cards.findIndex(c => c.dataset.sceneId === _focusedCardId);
      if (curIdx < 0) return;
      const curCard = cards[curIdx];

      let nextIdx = -1;
      if (e.key === 'ArrowLeft') {
        nextIdx = curIdx - 1;
      } else if (e.key === 'ArrowRight') {
        nextIdx = curIdx + 1;
      } else if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        const curRect = curCard.getBoundingClientRect();
        const curCenterX = curRect.left + curRect.width / 2;
        const dir = e.key === 'ArrowDown' ? 1 : -1;
        let bestIdx = -1, bestDist = Infinity;
        for (let i = 0; i < cards.length; i++) {
          if (i === curIdx) continue;
          const r = cards[i].getBoundingClientRect();
          const rowDiff = dir > 0 ? r.top - curRect.top : curRect.top - r.top;
          if (rowDiff < 10) continue;
          const dist = Math.abs(r.left + r.width / 2 - curCenterX) + rowDiff * 2;
          if (dist < bestDist) { bestDist = dist; bestIdx = i; }
        }
        nextIdx = bestIdx;
      }

      if (nextIdx >= 0 && nextIdx < cards.length) {
        _focusGridCard(cards[nextIdx].dataset.sceneId);
      }
    }
    document.addEventListener('keydown', _gridKeyHandler);

    // Merge all currently selected scenes (must all be in same folder)
    async function executeSelectionMerge() {
      const ids = Array.from(selectedSceneIds);
      if (ids.length < 2) return;
      const parsed = ids.map(id => {
        const parts = String(id).split(':');
        const count = parts.pop();
        const slot = parts.length ? parseInt(parts[0], 10) : 0;
        return { id, slot, count };
      });
      const slots = new Set(parsed.map(p => p.slot));
      if (slots.size > 1) {
        alert('Cannot merge scenes from different folders.\nSelect scenes from the same folder only.');
        return;
      }
      const target = parsed.slice().sort((a, b) => parseNumber(a.count) - parseNumber(b.count))[0];
      const slot = target.slot;
      const targetCount = target.count;
      const mergedSceneId = String(slot != null ? slot + ':' + targetCount : targetCount);
      let changed = 0;
      for (const r of rows) {
        if ((r.__folderSlot ?? 0) !== slot) continue;
        if (parsed.some(p => p.count === String(r.scene_count)) && String(r.scene_count) !== targetCount) {
          r.scene_count = targetCount; changed++;
        }
      }
      const rpForMerge = rows.find(r => (r.__folderSlot ?? 0) === slot)?.__rootPath || rootPath || '';
      // Update scenedata: move filenames from non-target scenes into target scene
      if (hasPywebviewApi) {
        if (rpForMerge) {
          const sd = _initScenedata(rpForMerge);
          const allMovedFiles = new Set();
          for (const p of parsed) {
            if (p.count !== targetCount && sd.scenes[p.count]) {
              for (const f of sd.scenes[p.count].image_filenames || []) allMovedFiles.add(f);
              delete sd.scenes[p.count];
            }
          }
          if (!sd.scenes[targetCount]) {
            sd.scenes[targetCount] = { scene_id: targetCount, image_filenames: [], name: '', status: 'pending', user_tags: { species: [], families: [], finalized: false } };
          }
          for (const f of allMovedFiles) {
            if (!sd.scenes[targetCount].image_filenames.includes(f)) sd.scenes[targetCount].image_filenames.push(f);
          }
        }
      }
      if (changed) {
        markDirty(rpForMerge);
        setStatus(`Merged ${ids.length} scenes into #${targetCount}. ${changed} rows updated.`);
      }
      selectedSceneIds.clear();
      _lastSelectedIdx = -1;
      updateSelectionUI();
      await renderScenes();
      // Scroll to the merged scene card; fall back to current scroll position
      const mergedCard = document.querySelector(`.card[data-scene-id="${CSS.escape(mergedSceneId)}"]`);
      if (mergedCard) mergedCard.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    // Render images inside the scene dialog, honoring the manual-rated filter and stable ordering
