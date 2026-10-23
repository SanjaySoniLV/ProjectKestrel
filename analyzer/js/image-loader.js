    const sleep = (ms) => new Promise(r => setTimeout(r, ms));

    // ── Lazy image loader (throttled) ────────────────────────────────────────────
    // Concurrency-limited to avoid flooding the Python IPC bridge with dozens of
    // simultaneous read_image_file calls when a large section of the grid scrolls
    // into view.  Excess loads are queued and drained as earlier ones finish.
    const _IMG_LOAD_QUEUE_MAX = 256;
    const _imgLoadThrottle = { active: 0, max: 100, queue: [] };
    function _scheduleLoad(fn) {
      if (_imgLoadThrottle.active < _imgLoadThrottle.max) {
        _imgLoadThrottle.active++;
        fn().finally(() => {
          _imgLoadThrottle.active--;
          if (_imgLoadThrottle.queue.length) _scheduleLoad(_imgLoadThrottle.queue.shift());
        });
      } else {
        while (_imgLoadThrottle.queue.length >= _IMG_LOAD_QUEUE_MAX) {
          _imgLoadThrottle.queue.shift();
        }
        _imgLoadThrottle.queue.push(fn);
      }
    }

    const _lazyObserver = new IntersectionObserver((entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const img = entry.target;
        const loader = img._lazyLoader;
        if (loader) { _scheduleLoad(loader); delete img._lazyLoader; }
        _lazyObserver.unobserve(img);
      }
    }, { rootMargin: '300px' });

    function lazyLoadImg(img, resolverFn) {
      img._lazyLoader = async () => {
        // Skip stale loads. When a grid/filmstrip is rebuilt (e.g. on every
        // rating keypress the scene dialog does grid.innerHTML='' and recreates
        // every card) the old <img> nodes are detached from the document. Their
        // loaders may still be waiting in the shared FIFO concurrency queue;
        // running them wastes one read_image_file IPC round-trip each.
        // _scheduleLoad drops the oldest queued loaders beyond
        // _IMG_LOAD_QUEUE_MAX so a rebuild cannot grow the FIFO without bound.
        if (!img.isConnected) return;
        const url = await resolverFn();
        // The element may have been detached while we awaited the IPC read; if so
        // don't set src / decode an image that is no longer on screen.
        if (!img.isConnected) return;
        if (url) {
          img.src = url;
          // Let the browser decode the image off the main thread before the
          // next paint, preventing jank from synchronous decode.
          try { await img.decode(); } catch (_) { /* broken/aborted image */ }
        }
      };
      _lazyObserver.observe(img);
    }
    // ── End lazy image loader ────────────────────────────────────────────────────

    // Generic debounce helper
    function debounce(fn, ms) {
      let timer;
      return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
    }

    // Version counter so concurrent renderScenes calls can bail out early
    let _renderScenesVersion = 0;
    // True while a batched bird-catalog hydration kicked off by renderScenes
    // is still in flight, so concurrent renders don't pile up duplicate IPC
    // calls before the first one returns and re-renders the grid.
    let _sceneCardHydrationPending = false;
    // Version counter so loadMultipleFolders can be cancelled mid-flight
    let _loadFoldersVersion = 0;

