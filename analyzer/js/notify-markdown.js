// Tiny strict Markdown renderer for in-app notifications (H6).
//
// SECURITY: notification bodies are untrusted (defense-in-depth — a writer
// could embed an attacker-influenced username/title). This renderer:
//   1. escapes ALL HTML first (so any <img onerror>, <script>, raw tag is inert),
//   2. then linkifies ONLY `[text](url)` markdown, with the href re-validated
//      against the projectkestrel.org allowlist (https) or mailto — anything
//      else is left as its escaped literal text,
//   3. turns newlines into <br>.
// No other markdown (no bold/italic/images/raw HTML). Identical copy ships on
// the Perch website and in the desktop app — keep the three in lockstep.
//
// Exposes: window.KestrelNotifyMarkdown.render(md) -> safe HTML string.
(function () {
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[c];
    });
  }

  // https on a projectkestrel.org host (apex or any subdomain), or a mailto.
  function isAllowedHref(href) {
    var h = String(href == null ? "" : href).trim();
    if (/^mailto:[^\s]+@[^\s]+$/i.test(h)) return true;
    try {
      var u = new URL(h);
      if (u.protocol !== "https:") return false;
      var host = u.hostname.toLowerCase();
      return host === "projectkestrel.org" || host.endsWith(".projectkestrel.org");
    } catch (e) {
      return false;
    }
  }

  function render(md) {
    var escaped = escapeHtml(md);
    // `[ ] ( )` are NOT touched by escapeHtml, so the link syntax survives.
    var linked = escaped.replace(
      /\[([^\]]+)\]\(([^)\s]+)\)/g,
      function (whole, text, url) {
        // escapeHtml turned any `&` in the url into `&amp;`; decode for the
        // allowlist check, but emit the escaped form in the attribute.
        var rawUrl = url.replace(/&amp;/g, "&");
        if (!isAllowedHref(rawUrl)) return whole; // inert literal
        return (
          '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + text + "</a>"
        );
      }
    );
    return linked.replace(/\n/g, "<br>");
  }

  window.KestrelNotifyMarkdown = { render: render, isAllowedHref: isAllowedHref };
})();
