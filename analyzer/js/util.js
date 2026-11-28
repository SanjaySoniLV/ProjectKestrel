// Shared formatting and escaping helpers for the main window.
//
// Loaded before every other module (see the <script> tags in visualizer.html).
// Exposes window.KestrelUtil; escapeHtml is also kept as a global because
// dozens of templates across the modules call it by that name.
(function () {
  const HTML_ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  /** Escape text for HTML content *and* quoted attribute values. null/undefined → "". */
  function escapeHtml(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => HTML_ESCAPES[c]);
  }

  /** "38s", "4m 12s", "1h 5m". Negative or non-finite → "—". */
  function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '—';
    const total = Math.round(seconds);
    if (total < 60) return `${total}s`;
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    if (h > 0) return m > 0 ? `${h}h ${m}m` : `${h}h`;
    return s > 0 ? `${m}m ${s}s` : `${m}m`;
  }

  /** "512 B", "3.4 MB", "120 GB" (binary units, one decimal below 100). */
  function formatBytes(n) {
    n = Number(n) || 0;
    if (n < 1024) return `${Math.max(0, Math.round(n))} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let v = n / 1024;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
    return `${v.toFixed(v >= 100 ? 0 : 1)} ${units[i]}`;
  }

  window.KestrelUtil = { escapeHtml, formatDuration, formatBytes };
  window.escapeHtml = escapeHtml;
})();
