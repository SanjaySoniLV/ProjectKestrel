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


    // Blob URL cache per path
    const blobUrlCache = new Map();

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
