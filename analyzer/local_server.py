"""Binding the app's local-only HTTP server to a port that is really free.

``visualizer.py`` serves its static files from 127.0.0.1 and must not end up
sharing a port with some other local program. This module picks the port.
"""

from __future__ import annotations

import socket
import sys
from http.server import ThreadingHTTPServer

from settings_utils import log

HOST = '127.0.0.1'

# How many sequential ports past the requested one to try before giving up and
# letting the OS pick a free port (bind to 0). Keeps the URL predictable in the
# common case while surviving a busy port (e.g. a stale instance still exiting).
PORT_FALLBACK_TRIES = 8


class ExclusiveBindHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer whose bind fails when another process holds the port.

    ``http.server.HTTPServer`` sets ``allow_reuse_address = 1``, so socketserver
    applies ``SO_REUSEADDR`` before ``bind()``. On Windows that option lets a
    second socket bind a port another process is already LISTENING on: the bind
    succeeds, both servers share the port, and Windows hands each connection to
    either one, so the fallback below would never see an error. On Windows we
    set ``SO_EXCLUSIVEADDRUSE`` instead, which makes a busy port raise
    WSAEADDRINUSE (10048). On POSIX, ``SO_REUSEADDR`` only permits reusing a
    port in TIME_WAIT (a quick restart), never one with a live listener, so the
    default behaviour stays there.
    """

    if sys.platform == 'win32':
        allow_reuse_address = False

        def server_bind(self):
            opt = getattr(socket, 'SO_EXCLUSIVEADDRUSE', None)
            if opt is not None:
                self.socket.setsockopt(socket.SOL_SOCKET, opt, 1)
            super().server_bind()


def bind_server_with_fallback(preferred_port, handler):
    """Bind an HTTP server on 127.0.0.1, trying a few ports.

    Tries ``preferred_port`` first, then the next ``PORT_FALLBACK_TRIES`` ports,
    and finally falls back to an OS-assigned free port (bind to 0). Returns
    ``(server, port)`` where ``port`` is the port actually bound. Raises
    ``OSError`` only if every candidate — including the OS-assigned one — fails.
    """
    candidates = [preferred_port + offset for offset in range(PORT_FALLBACK_TRIES + 1)]
    candidates.append(0)  # last resort: let the OS hand us any free port
    last_exc = None
    for candidate in candidates:
        try:
            server = ExclusiveBindHTTPServer((HOST, candidate), handler)
        except OSError as exc:
            last_exc = exc
            continue
        bound_port = server.server_address[1]
        if candidate not in (preferred_port, 0):
            log(f'Port {preferred_port} unavailable; using {bound_port} instead.')
        elif candidate == 0:
            log(f'Ports {preferred_port}-{preferred_port + PORT_FALLBACK_TRIES} '
                f'unavailable; OS assigned port {bound_port}.')
        return server, bound_port
    raise OSError(
        f'Could not bind an HTTP server on {HOST}: no free port found near '
        f'{preferred_port}'
    ) from last_exc
