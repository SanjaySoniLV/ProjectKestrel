"""Port selection for the app's local HTTP server (``local_server.py``).

The case that matters is another local program already listening on the
preferred port with a stock Python ``http.server`` (which sets
``SO_REUSEADDR``). On Windows a second ``SO_REUSEADDR`` bind to that port
succeeds, so the app must bind exclusively for the fallback to notice the port
is taken.
"""

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import pytest

from local_server import HOST, bind_server_with_fallback

pytestmark = pytest.mark.unit


def _free_port():
    probe = ThreadingHTTPServer((HOST, 0), SimpleHTTPRequestHandler)
    port = probe.server_address[1]
    probe.server_close()
    return port


def test_free_preferred_port_is_used():
    port = _free_port()
    server, bound = bind_server_with_fallback(port, SimpleHTTPRequestHandler)
    try:
        assert bound == port
    finally:
        server.server_close()


def test_port_held_by_another_http_server_is_skipped():
    # A stock http.server listener, the same as another local Python tool.
    other = ThreadingHTTPServer((HOST, 0), SimpleHTTPRequestHandler)
    busy = other.server_address[1]
    try:
        server, bound = bind_server_with_fallback(busy, SimpleHTTPRequestHandler)
        try:
            assert bound != busy
        finally:
            server.server_close()
    finally:
        other.server_close()


def test_our_bound_port_blocks_a_second_instance():
    port = _free_port()
    first, first_port = bind_server_with_fallback(port, SimpleHTTPRequestHandler)
    try:
        second, second_port = bind_server_with_fallback(first_port, SimpleHTTPRequestHandler)
        try:
            assert second_port != first_port
        finally:
            second.server_close()
    finally:
        first.server_close()
