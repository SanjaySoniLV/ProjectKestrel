// Builders for the main scene grid: time clusters, scene cards, the timeline
// and folder group headers. renderScenes (scene-grid.js) decides what to show
// and calls these with a render context:
//   { smartActive, searchOn, showBirdThumbs, showFolderHeaders, showSciOnCards,
//     isStale() }  -- isStale() is true once a newer render has started.
// Card and header behaviour lives in scene-grid.js globals (selection,
// context menus, folder actions); these functions only build DOM.
(function () {
  'use strict';

  // Cards are appended in batches so a stale render can stop early.
  const BATCH = 24;

  // ---- Time clusters ----
  //
  // Scenes are clustered by the gaps between successive capture times, so a
  // run of continuous shooting is one node and a quiet pause starts a new one
  // regardless of clock alignment. The gap threshold comes from the folder
  // itself (a multiple of the median inter-scene gap), so dense burst folders
  // and casual days both cluster sensibly without a setting. The floor and
  // ceiling keep single-scene folders and multi-week bundles sane.
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

  // Card thumbnail with the smart-search badges and the hover "Show in timeline" button.
  function buildCardThumb(s, ctx) {
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
    if (ctx.smartActive && s.smartRank) {
      th.classList.add('has-smart-rank');
      const rank = document.createElement('span');
      rank.className = 'smart-search-rank';
      rank.textContent = `#${s.smartRank}`;
      rank.title = `Smart search rank ${s.smartRank} (score ${Number(s.smartScore).toFixed(3)})`;
      th.appendChild(rank);
      if (s.smartPercent != null) {
        const pct = document.createElement('span');
        pct.className = 'smart-search-pct';
        pct.textContent = `${s.smartPercent}% match`;
        pct.title = 'How far this photo sits above the typical photo in its folder for this search, '
          + 'scaled to 0-100%. A relative match strength, not a probability.';
        th.appendChild(pct);
      }
      const nImgs = (s.images || []).length;
      if (nImgs > 1 && s.smartMatchCount) {
        const m = document.createElement('span');
        m.className = 'smart-search-matches';
        m.textContent = `${s.smartMatchCount}/${nImgs}`;
        m.title = `${s.smartMatchCount} of ${nImgs} photos in this scene match; the card shows the best match`;
        th.appendChild(m);
      }
    }
    if (ctx.searchOn) {
      // Hover button; the right-click menu (below) does the same.
      const goBtn = document.createElement('button');
      goBtn.type = 'button';
      goBtn.className = 'card-goto-btn';
      goBtn.appendChild(KestrelIcon.el('clock'));
      goBtn.title = 'Show in timeline';
      goBtn.setAttribute('aria-label', 'Show in timeline');
      goBtn.addEventListener('click', (ev) => { ev.stopPropagation(); goToSceneInTimeline(s.id); });
      th.appendChild(goBtn);
    }
    return th;
  }

  // Secondary title row: shown only for a user-set scene name, or the folder
  // name when folder headers are off. The image and grid position identify the
  // scene, so no #N is shown. User text goes in with textContent only.
  function buildCardTitle(s, ctx) {
    const _folderName = folderBaseName(s.representative?.__rootPath || '');

    const needTitleRow = !!(s.sceneName || (_folderName && !ctx.showFolderHeaders));
    const title = needTitleRow ? document.createElement('div') : null;
    if (title) {
      title.className = 'title';
      if (_folderName && !ctx.showFolderHeaders) {
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
    }
    return title;
  }

  // Species pills and the quality / photo-count line.
  function buildCardMetaRow(s, ctx) {
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
      if (ctx.showSciOnCards) c.classList.add('chip--with-sci');
      const primary = document.createElement('span');
      primary.className = 'chip-primary';
      primary.textContent = firstPill;
      c.appendChild(primary);
      let titleStr = firstPill;
      if (ctx.showSciOnCards) {
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
        _openMorePillsPopover(plus, cardPills.slice(1), ctx.showSciOnCards, cardPills);
      });
      pillsWrap.appendChild(plus);
    }
    metaRow.appendChild(pillsWrap);

    const meta = document.createElement('div');
    meta.className = 'card-meta';
    // Reviewed check, the best photo's star rating, then the photo count, on
    // one line with no background pill. The quality score itself stays in the
    // scene viewer.
    if (typeof isManuallyReviewedScene === 'function' && isManuallyReviewedScene(s)) {
      const rv = document.createElement('span');
      rv.className = 'meta-reviewed';
      rv.title = 'Reviewed: you have accepted, rejected or rated photos in this scene';
      rv.setAttribute('aria-label', 'Reviewed');
      rv.appendChild(KestrelIcon.el('check'));
      meta.appendChild(rv);
    }
    const stars = Math.max(0, Math.min(5, s.maxRating || 0));
    const scoreEl = document.createElement('span');
    scoreEl.className = 'meta-stars';
    scoreEl.setAttribute('aria-label', `Best photo: ${stars} star${stars === 1 ? '' : 's'}`);
    scoreEl.title = (s.maxQuality >= 0)
      ? `Best photo: ${stars} star${stars === 1 ? '' : 's'} · Quality ${s.maxQuality.toFixed(2)}`
      : `Best photo: ${stars} star${stars === 1 ? '' : 's'}`;
    const on = document.createElement('span');
    on.textContent = '★'.repeat(stars);
    const off = document.createElement('span');
    off.className = 'meta-stars-off';
    off.textContent = '★'.repeat(5 - stars);
    scoreEl.append(on, off);
    meta.appendChild(scoreEl);
    const metaSep = document.createElement('span');
    metaSep.className = 'meta-sep';
    metaSep.setAttribute('aria-hidden', 'true');
    meta.appendChild(metaSep);
    const countEl = document.createElement('span');
    countEl.className = 'meta-count';
    countEl.append(KestrelIcon.el('stack'), `${s.imageCount}`);
    // Break the count down on hover rather than on the tile: the split is
    // useful when triaging but not worth the visual weight on every card.
    if (s.cullCounts) {
      const { accepted, rejected, undecided } = s.cullCounts;
      countEl.title = accepted || rejected
        ? `${accepted} accepted · ${undecided} undecided · ${rejected} rejected`
        : `${s.imageCount} photo${s.imageCount === 1 ? '' : 's'}, none decided yet`;
    }
    meta.appendChild(countEl);
    metaRow.appendChild(meta);
    return metaRow;
  }

  // Click selects (shift: range, ctrl/cmd: toggle, or toggle while a selection
  // exists); a plain click opens the scene viewer.
  function onCardClick(ev, s) {
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
  }

  function buildCard(s, ctx) {
    const card = document.createElement('article');
    card.className = 'card';
    card.dataset.sceneId = String(s.id);
    if (selectedSceneIds.has(String(s.id))) card.classList.add('selected');

    const th = buildCardThumb(s, ctx);
    if (ctx.showBirdThumbs && s.representative?.crop_path && s.representative?.export_path) {
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
    const title = buildCardTitle(s, ctx);
    if (title) body.appendChild(title);
    if (s.isApproved) card.classList.add('scene-approved');

    body.appendChild(buildCardMetaRow(s, ctx));
    card.appendChild(body);

    card.addEventListener('click', (ev) => onCardClick(ev, s));
    if (ctx.searchOn) {
      card.addEventListener('contextmenu', (ev) => {
        ev.preventDefault();
        _openCardContextMenu(ev.clientX, ev.clientY, [
          { icon: 'clock', label: 'Go to scene in timeline', run: () => goToSceneInTimeline(s.id) },
        ]);
      });
    }
    return card;
  }

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

  // One timeline node: a rail dot sized by image count, a time-range header
  // and an empty card grid, which is returned for the caller to fill.
  function buildTimelineNode(cluster, isLast, maxImgCountInFolder, timelineEl) {
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
    return gridEl;
  }

  function buildTimeline(fd, containerEl, ctx) {
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

      const gridEl = buildTimelineNode(cluster, isLast, maxImgCountInFolder, timelineEl);
      if (!appendCards(gridEl, cluster.scenes, ctx)) return false;
    }
    containerEl.appendChild(timelineEl);
    return true;
  }

  // ---- Folder group header ----

  // Folder actions dropdown; the menu is positioned relative to this wrapper.
  function buildFolderActionsMenu(folderPath) {
    const actionsWrap = document.createElement('div');
    actionsWrap.className = 'folder-group-actions-wrap';
    actionsWrap.style.position = 'relative';

    const actionsTrigger = document.createElement('button');
    actionsTrigger.className = 'action-btn folder-group-actions-trigger';
    actionsTrigger.innerHTML = '<i>⋯</i> Folder Actions <span class="caret">▾</span>';
    actionsTrigger.title = 'More actions for this folder';
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
        try { if (typeof renderFolderTree === 'function') renderFolderTree(); } catch (e) { console.warn('[scene-grid] folder tree refresh failed', e); }
        try { if (typeof renderScenes === 'function') renderScenes(); } catch (e) { console.warn('[scene-grid] re-render failed', e); }
      };
      // Status is cached for 30 s, so this rarely waits; build the menu once it is known.
      _openFolderActionsTrigger = actionsTrigger;
      folderPipelineStatus(folderPath).then((up) => {
        if (_openFolderActionsTrigger !== actionsTrigger) return; // closed or replaced meanwhile
        const menu = _buildFolderActionsMenu(folderPath, refreshCb, up);
        actionsWrap.appendChild(menu);
        _openFolderActionsMenu = menu;
      });
    });
    actionsWrap.appendChild(actionsTrigger);
    return actionsWrap;
  }

  // Analysis version label + Upgrade to 3.0. Filled in once the folder's
  // status arrives; the button stays hidden for folders already on 3.0 (or
  // needing a full re-analysis).
  function appendPipelineControls(folderPath, rightActions) {
    const pipelineLabel = document.createElement('span');
    pipelineLabel.className = 'folder-pipeline-label hidden';
    rightActions.appendChild(pipelineLabel);
    const upgradeBtn = document.createElement('button');
    upgradeBtn.type = 'button';
    upgradeBtn.className = 'action-btn folder-upgrade-btn hidden';
    upgradeBtn.dataset.folderPath = folderPath;
    upgradeBtn.innerHTML = `${KestrelIcon('sparkle')} Upgrade to 3.0`;
    upgradeBtn.title = 'Upgrade to analysis version 3.0 from the crops Kestrel already saved: new species IDs, the sharpness score and smart search. No re-analysis; ratings, culls and tags are kept.';
    upgradeBtn.addEventListener('click', (ev) => { ev.stopPropagation(); upgradeFolderToV3(folderPath); });
    rightActions.appendChild(upgradeBtn);
    folderPipelineStatus(folderPath).then((up) => {
      const label = _pipelineVersionLabel(up);
      pipelineLabel.textContent = label;
      pipelineLabel.title = up?.reason || '';
      pipelineLabel.classList.toggle('hidden', !label);
      pipelineLabel.classList.toggle('is-current', (Number(up?.pipeline_major) || 0) >= 3);
      upgradeBtn.dataset.upgradable = up?.status === 'upgradable' ? '1' : '0';
      syncFolderUpgradeButtons();
    });
  }

  // Created hidden: repair.js::syncRepairIndicators shows it once a background
  // diff finds drift, which arrives after this render completes.
  function buildRepairButton(folderPath) {
    const repairBtn = document.createElement('button');
    repairBtn.type = 'button';
    repairBtn.className = 'action-btn folder-repair-btn hidden';
    repairBtn.dataset.repairRoot = folderPath;
    repairBtn.innerHTML = `${KestrelIcon('warning')} Repair Kestrel Data`;
    repairBtn.title = 'Kestrel data and photos no longer match';
    repairBtn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      if (typeof openRepairDialog === 'function') openRepairDialog(folderPath);
    });
    return repairBtn;
  }

  function buildCullingButton(folderPath) {
    const cullingBtn = document.createElement('button');
    cullingBtn.className = 'action-btn culling-assistant-btn';
    cullingBtn.innerHTML = `${KestrelIcon('scissors')} Enter Cull mode`;
    cullingBtn.title = 'Accept or reject every photo in this folder, scene by scene';
    cullingBtn.addEventListener('click', (ev) => { ev.stopPropagation(); openCullMode(folderPath); });
    return cullingBtn;
  }

  function buildPerchButton(folderPath) {
    const perchBtn = document.createElement('button');
    perchBtn.type = 'button';
    perchBtn.className = 'action-btn folder-perch-btn';
    perchBtn.dataset.folderPath = folderPath;
    perchBtn.innerHTML = `${KestrelIcon('feather')} <span class="folder-perch-btn-label">Share with Perch</span>`;
    perchBtn.title = 'Share this folder to Perch (or manage existing perch)';
    perchBtn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      openPerchDialog(folderPath);
    });

    // Async-populate linked-state from disk; flip class + label if linked.
    (async () => {
      try {
        const res = await window.pywebview?.api?.read_perch_link?.(folderPath);
        if (res && res.present && res.link) {
          perchBtn.classList.add('is-linked');
          const lbl = perchBtn.querySelector('.folder-perch-btn-label');
          if (lbl) lbl.textContent = 'On Perch';
          perchBtn.dataset.perchUrl = String(res.link.perch_url || '');
          perchBtn.title = 'This folder is published to Perch (click to manage)';
        }
      } catch { /* link state is cosmetic; the button still opens the dialog */ }
    })();

    // Block sharing while this folder is still being analyzed — uploading
    // partial results to Perch would publish an incomplete timeline. The
    // disabled state is kept in sync as analysis starts/finishes by
    // updateInProgressFoldersInTree() (queue.js).
    try {
      const _np = String(folderPath || '').replace(/\\/g, '/');
      const _analyzing =
        (typeof _inProgressFolderPaths !== 'undefined' && _inProgressFolderPaths.has(_np))
        || (window._ccInProgressFolderPaths && window._ccInProgressFolderPaths.has(_np));
      if (_analyzing) {
        perchBtn.disabled = true;
        perchBtn.title = 'Wait for analysis to finish before uploading to Perch.';
      }
    } catch { /* the button stays enabled; Perch upload re-checks analysis state */ }
    return perchBtn;
  }

  // A collapsible folder group: header (name, scene count, actions) and an
  // empty body that the caller fills with the timeline or a flat grid.
  function buildFolderGroup(fd, fk) {
    const folderName = folderBaseName(fd.folderPath) || fd.folderPath || '(unknown folder)';
    const collapsed = collapsedFolders.has(fk);

    const groupEl = document.createElement('div');
    groupEl.className = 'folder-group';

    const hdr = document.createElement('div');
    hdr.className = 'folder-group-header' + (collapsed ? ' collapsed' : '');
    hdr.innerHTML = `<span class="folder-group-toggle">▼</span><span class="folder-group-name">${escapeHtml(folderName)}</span><span class="folder-group-count muted">${fd.scenes.length} scene${fd.scenes.length === 1 ? '' : 's'}</span>`;

    // Spacer pushes the action group to the right
    const spacer = document.createElement('div');
    spacer.style.flex = '1';
    hdr.appendChild(spacer);

    const rightActions = document.createElement('div');
    rightActions.className = 'folder-group-right-actions';
    appendPipelineControls(fd.folderPath, rightActions);
    rightActions.appendChild(buildFolderActionsMenu(fd.folderPath));
    rightActions.appendChild(buildRepairButton(fd.folderPath));
    rightActions.appendChild(buildCullingButton(fd.folderPath));
    rightActions.appendChild(buildPerchButton(fd.folderPath));
    hdr.appendChild(rightActions);

    const bodyEl = document.createElement('div');
    bodyEl.className = 'folder-group-body' + (collapsed ? ' hidden' : '');

    hdr.addEventListener('click', () => {
      if (collapsedFolders.has(fk)) collapsedFolders.delete(fk); else collapsedFolders.add(fk);
      hdr.classList.toggle('collapsed');
      bodyEl.classList.toggle('hidden');
    });
    groupEl.appendChild(hdr);
    groupEl.appendChild(bodyEl);
    return { groupEl, bodyEl };
  }

  // True while a hydration is in flight, so concurrent renders don't pile up
  // duplicate IPC calls before the first one returns and re-renders the grid.
  let hydrationPending = false;

  // With scientific names on, fetch every missing bird-catalog record for the
  // visible species and family pills in one IPC call, then re-render once.
  // Per-card lookups would fan out N requests on first paint of a big folder.
  function hydrateSpeciesRecords(scenes) {
    if (hydrationPending) return;
    const need = new Set();
    for (const s of scenes) {
      // Families too: the card's family fallback borrows family_sci from a
      // cached species record per family pill.
      for (const sp of (s.species || [])) {
        if (sp && !_isBirdRecordKnown(sp)) need.add(sp);
      }
      for (const fm of (s.families || [])) {
        if (fm && !_isBirdRecordKnown(fm)) need.add(fm);
      }
    }
    if (need.size === 0) return;
    hydrationPending = true;
    _hydrateBirdRecords(Array.from(need)).then(() => {
      hydrationPending = false;
      // Always repaint, even if a newer render ran meanwhile: that render was
      // kept from starting its own hydration and drew against the empty cache.
      requestAnimationFrame(() => { try { renderScenes(); } catch (e) { console.warn('[scene-grid] re-render failed', e); } });
    }).catch((e) => { hydrationPending = false; console.warn('[scene-grid] species record hydration failed', e); });
  }

  // Fill gridEl with one card per scene. Returns false if a newer render
  // started part-way through.
  function appendCards(gridEl, scenes, ctx) {
    for (let i = 0; i < scenes.length; i += BATCH) {
      if (ctx.isStale()) return false;
      const frag = document.createDocumentFragment();
      for (const s of scenes.slice(i, i + BATCH)) frag.appendChild(buildCard(s, ctx));
      gridEl.appendChild(frag);
    }
    return true;
  }

  window.SceneGridParts = {
    computeDynamicClusterGapMs,
    buildTimeClusters,
    buildCard,
    buildTimeline,
    buildFolderGroup,
    appendCards,
    hydrateSpeciesRecords,
  };
})();
