"""Regression test for the macOS App Sandbox MIME bug (blank-white-screen).

Under the App Sandbox (the Mac App Store build), Python's ``http.server`` default
``guess_type()`` runs ``mimetypes.init()``, which reads system MIME files such as
``/etc/apache2/mime.types``. The sandbox DENIES that read
(``PermissionError: [Errno 1] Operation not permitted``), which raised on *every*
GET and left the WebView blank white. ``visualizer.Handler.guess_type`` now
resolves Content-Type from a static table and never touches ``mimetypes`` or the
filesystem.

These tests simulate the sandbox by making ``mimetypes`` explode the way it does
under the sandbox, then assert the handler still serves the right types — i.e. it
never depends on the system MIME files. They fail on the pre-fix code and pass on
the fix. This is a fast, cross-platform stand-in for "run the signed app under the
real sandbox," which only the Mac runner can do.
"""

import mimetypes

from visualizer import Handler


def _handler():
    # guess_type only reads the class-level _MIME_TYPES table plus its path arg,
    # so we can bypass __init__ (which would need a live socket/request).
    return Handler.__new__(Handler)


EXPECTED = {
    "/static/visualizer.html": "text/html; charset=utf-8",
    "/static/js/state.js": "text/javascript; charset=utf-8",
    "/static/js/state.mjs": "text/javascript; charset=utf-8",
    "/static/css/base.css": "text/css; charset=utf-8",
    "/data/kestrel_scenedata.json": "application/json; charset=utf-8",
    "/crop/IMG_0001_crop_0.webp": "image/webp",
    "/assets/logo.png": "image/png",
    "/assets/photo.jpg": "image/jpeg",
    "/assets/photo.jpeg": "image/jpeg",
    "/assets/icon.svg": "image/svg+xml",
    "/fonts/inter.woff2": "font/woff2",
}


def test_guess_type_resolves_served_types_from_static_table():
    h = _handler()
    for path, ctype in EXPECTED.items():
        assert h.guess_type(path) == ctype, path
    # unknown / extension-less paths fall back safely and never raise
    assert h.guess_type("/x/thing.unknownext") == "application/octet-stream"
    assert h.guess_type("/x/noext") == "application/octet-stream"


def test_guess_type_never_touches_system_mime_files(monkeypatch):
    """Simulate the App Sandbox: reading the system MIME files raises PermissionError.

    The pre-fix guess_type() reached mimetypes.guess_type() -> mimetypes.init()
    -> read('/etc/apache2/mime.types'), which is exactly what blanked the App
    Store build. Force every mimetypes entry point to blow up the sandbox way; the
    handler must still return correct types without raising.
    """
    def _boom(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted", "/etc/apache2/mime.types")

    monkeypatch.setattr(mimetypes, "inited", False, raising=False)
    monkeypatch.setattr(mimetypes, "init", _boom)
    monkeypatch.setattr(mimetypes, "guess_type", _boom)

    h = _handler()
    # Each of these would raise PermissionError on the pre-fix handler.
    assert h.guess_type("/static/visualizer.html") == "text/html; charset=utf-8"
    assert h.guess_type("/static/js/state.js").startswith("text/javascript")
    assert h.guess_type("/static/css/base.css").startswith("text/css")
    assert h.guess_type("/crop/x.webp") == "image/webp"
    assert h.guess_type("/x/unknown.zzz") == "application/octet-stream"
