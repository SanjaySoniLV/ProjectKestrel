"""Shared TLS context for the desktop's stdlib-``urllib`` HTTP clients.

Why this exists: a frozen macOS ``.app`` ships an OpenSSL whose *default*
certificate path points into the build machine's filesystem, which does not
exist on the end user's Mac. So ``urllib.request.urlopen`` with the default
context fails **every** HTTPS request with ``CERTIFICATE_VERIFY_FAILED`` once
bundled. Routing verification through certifi's CA bundle — the same one
``requests`` uses by default, and the one PyInstaller already collects — fixes
this on macOS while remaining correct on Windows/Linux.

The ``requests``-based clients (``perch_uploader`` and the ``api_bridge``
version/legal fetchers) already get certifi for free. This module is for the
stdlib-``urllib`` clients — ``cloud_compute_client``, ``auth_client``,
``oauth_client`` — so they all share one source of truth and a future client
can't silently regress on macOS by hand-rolling a bare ``urlopen`` again.
"""

from __future__ import annotations

import ssl
import threading

_lock = threading.Lock()
_ctx: "ssl.SSLContext | None" = None


def ssl_context() -> "ssl.SSLContext":
    """Return a process-wide cached TLS context backed by certifi's CA bundle.

    Cached because building a context (and reading the CA file) is not free and
    an ``SSLContext`` is safe to share across threads/connections. Falls back to
    the stdlib default context if certifi is somehow unavailable (e.g. a source
    checkout without the dependency) so dev environments still work.
    """
    global _ctx
    with _lock:
        if _ctx is not None:
            return _ctx
        try:
            import certifi
            _ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _ctx = ssl.create_default_context()
        return _ctx
