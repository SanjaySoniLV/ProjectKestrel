    // Simple UI zoom controls (Chromium webviews support CSS zoom)
    let uiZoom = 1;
    function applyZoom() {
      const zoomEl = document.getElementById('mainZoom');
      if (!zoomEl) return;
      const z = uiZoom;
      // Use non-transform zoom so position:sticky on headers continues to work.
      // Prefer CSS `zoom` when available; fall back to transform-only if not supported.
      try {
        zoomEl.style.zoom = z.toFixed(2);
        // Ensure no transform is set (transform can break sticky behavior)
        zoomEl.style.transform = '';
        zoomEl.style.width = '';
        zoomEl.style.height = '';
      } catch (e) {
        // Fallback: use transform if browser doesn't support zoom (sticky may be affected)
        const s = uiZoom.toFixed(2);
        zoomEl.style.transform = `scale(${s})`;
        zoomEl.style.width = `calc(100% / ${s})`;
        zoomEl.style.height = `calc(100% / ${s})`;
      }
    }

    function sanitizePath(p) {
      if (!p) return '';
      // Normalize to forward slashes, trim quotes
      return String(p).replace(/^\"|\"$/g, '').replace(/\\/g, '/');
    }

    function joinPath(a, b) {
      a = sanitizePath(a); b = sanitizePath(b);
      if (!a) return b; if (!b) return a;
      return a.replace(/\/$/, '') + '/' + b.replace(/^\//, '');
    }

    function parseCsvText(text) {
      if (!window.KestrelCsv || typeof window.KestrelCsv.parse !== 'function') {
        throw new Error('CSV parser unavailable (csv_parser.js not loaded)');
      }
      return window.KestrelCsv.parse(text, { header: true, skipEmptyLines: true });
    }


    // Blob URL cache per path.
    //
    // createObjectURL keeps the underlying image bytes alive until
    // revokeObjectURL is called, so a plain Map would leak: entries were never
    // revoked on eviction, and clear() (called on folder change from
    // multi-folder.js / scene-dialog.js / queue.js) dropped the map entries but
    // left the blobs allocated. Over a long session browsing many images this
    // grows unbounded. This Map subclass keeps the same has()/get()/set()/clear()
    // API used across the app, but additionally:
    //   - bounds the cache to the most-recently-used BLOB_URL_CACHE_MAX entries
    //     (get() bumps recency; set() evicts the oldest beyond the cap), and
    //   - revokes the blob: URL of every entry it drops (on eviction and clear).
    // The cap is generous enough to cover everything on screen at once, and
    // evicted entries are by definition not currently displayed, so revoking
    // them is safe (an already-loaded <img> keeps rendering after revoke).
    const BLOB_URL_CACHE_MAX = 512;

    function _revokeBlobUrl(url) {
      if (typeof url === 'string' && url.startsWith('blob:')) {
        try { URL.revokeObjectURL(url); } catch (_) { /* already revoked */ }
      }
    }

    class BlobUrlCache extends Map {
      get(key) {
        if (!super.has(key)) return undefined;
        // Refresh recency: re-insert so this key becomes most-recently-used.
        const val = super.get(key);
        super.delete(key);
        super.set(key, val);
        return val;
      }
      set(key, val) {
        if (super.has(key)) {
          // Replacing an existing key: revoke the URL we're dropping so a
          // duplicate load (e.g. two concurrent getBlobUrlForPath calls for the
          // same uncached image) doesn't orphan the first blob: URL. Guard
          // against the no-op case where the same URL is re-set.
          const prev = super.get(key);
          if (prev !== val) _revokeBlobUrl(prev);
          super.delete(key);
        }
        super.set(key, val);
        // Evict least-recently-used entries beyond the cap, freeing their blobs.
        while (super.size > BLOB_URL_CACHE_MAX) {
          const oldestKey = super.keys().next().value;
          _revokeBlobUrl(super.get(oldestKey));
          super.delete(oldestKey);
        }
        return this;
      }
      clear() {
        for (const url of super.values()) _revokeBlobUrl(url);
        super.clear();
      }
    }

    const blobUrlCache = new BlobUrlCache();

    /** Convert a base64 string to a Blob Object URL.  Unlike data: URIs, blob:
     *  URLs are decoded asynchronously by the browser's image-decode thread,
     *  keeping the main thread free during scroll. */
    function _base64ToBlobUrl(b64, mime) {
      const bin = atob(b64);
      const buf = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
      return URL.createObjectURL(new Blob([buf], { type: mime || 'image/jpeg' }));
    }

    async function getBlobUrlForPath(relOrAbsPath, rootOverride) {
      if (!relOrAbsPath) return null;
      const effectiveRoot = rootOverride || rootPath;

      // Normalize separators immediately (cheap, matches culling.html behaviour)
      const rel = String(relOrAbsPath).replace(/\\/g, '/');

      // Cache check first — avoids ALL further work on already-loaded images
      // (this is the hot path during scroll-back through cached thumbnails)
      const cacheKey = `${effectiveRoot}:${rel}`;
      if (blobUrlCache.has(cacheKey)) return blobUrlCache.get(cacheKey);

      // PRIORITY 1: Python API (desktop app - all platforms)
      if (hasPywebviewApi && window.pywebview?.api?.read_image_file && effectiveRoot) {
        try {
          const result = await window.pywebview.api.read_image_file(rel, effectiveRoot);
          if (result && result.success && result.data) {
            // Use a blob: URL instead of a data: URL so the browser can decode
            // the image asynchronously on its decode thread rather than blocking
            // the main thread with synchronous base64 + JPEG/PNG parsing.
            const blobUrl = _base64ToBlobUrl(result.data, result.mime);
            blobUrlCache.set(cacheKey, blobUrl);
            return blobUrl;
          }
        } catch (e) {
          console.error('Python API image read failed:', e);
          return null;
        }
      }

      return null;
    }

    function parseNumber(v) {
      const n = parseFloat(v);
      return Number.isFinite(n) ? n : -1;
    }

    function parseCaptureTimeMs(v) {
      if (v == null) return Number.NaN;
      const raw = String(v).trim();
      if (!raw) return Number.NaN;
      let d = new Date(raw);
      if (isNaN(d)) d = new Date(raw.replace(' ', 'T'));
      const ms = d.getTime();
      return Number.isFinite(ms) ? ms : Number.NaN;
    }

    // Parse secondary species columns.
    // New format: JSON array strings e.g. '["Greater Yellowlegs","Vaux\'s Swift"]'.
    // Legacy format: numpy str() repr e.g. "[\'Greater Yellowlegs\' \"Vaux\'s Swift\"]".
