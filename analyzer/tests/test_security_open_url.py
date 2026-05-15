"""Regression tests for FINDING-01: RCE via ``Api.open_url``.

Background
----------
The XSS sink in ``visualizer.js`` (sceneName rendered via
``decodeEntities(escapeHtml(...))`` into ``.innerHTML``) lets attacker JS run
in the webview's context, from which it can call any method on
``window.pywebview.api``.  ``Api.open_url`` forwarded its argument directly to
``webbrowser.open``; on Windows that falls through to ``os.startfile`` /
``ShellExecute``, allowing ``file:///C:/.../calc.exe``, UNC paths, ``.lnk``
files, etc. to be executed.

Expected fix
------------
``api_bridge`` must expose a pure predicate ``_is_safe_external_url(url)``
that accepts only http(s) / mailto URLs with no embedded control characters,
no backslashes, and no UNC prefixes.  ``Api.open_url`` must call the predicate
and refuse to forward unsafe URLs to ``webbrowser.open``.

If the helper is not yet defined, ``TestOpenUrlAllowlist`` is skipped with a
message pointing at the fix location, so the suite can be committed before
the patch lands.

Run with::

    cd analyzer
    python -m unittest tests.test_security_open_url
"""

from __future__ import annotations

import os
import sys
import unittest


_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_ANALYZER_DIR = os.path.dirname(_THIS_DIR)
if _ANALYZER_DIR not in sys.path:
    sys.path.insert(0, _ANALYZER_DIR)


try:
    import api_bridge  # noqa: E402
    _IMPORT_ERROR: Exception | None = None
except Exception as exc:  # pragma: no cover — surface import errors as skip
    api_bridge = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


_HELPER_AVAILABLE = api_bridge is not None and hasattr(api_bridge, "_is_safe_external_url")
_SKIP_MSG = (
    "api_bridge._is_safe_external_url is not defined — apply the FINDING-01 "
    "URL-allowlist patch to analyzer/api_bridge.py, then re-run."
)


@unittest.skipUnless(api_bridge is not None, f"api_bridge import failed: {_IMPORT_ERROR}")
@unittest.skipUnless(_HELPER_AVAILABLE, _SKIP_MSG)
class TestOpenUrlAllowlist(unittest.TestCase):
    """Unit-level tests of the ``_is_safe_external_url`` predicate."""

    # ---- Allowed ----
    def test_https_is_allowed(self) -> None:
        self.assertTrue(api_bridge._is_safe_external_url("https://projectkestrel.org/"))
        self.assertTrue(
            api_bridge._is_safe_external_url("https://projectkestrel.org/download")
        )
        self.assertTrue(
            api_bridge._is_safe_external_url("https://github.com/owner/repo/issues/1")
        )

    def test_http_is_allowed(self) -> None:
        self.assertTrue(
            api_bridge._is_safe_external_url("http://127.0.0.1:8765/health")
        )

    def test_mailto_is_allowed(self) -> None:
        self.assertTrue(
            api_bridge._is_safe_external_url("mailto:support@projectkestrel.org")
        )

    # ---- RCE vectors — must be rejected ----
    def test_file_scheme_is_rejected(self) -> None:
        self.assertFalse(
            api_bridge._is_safe_external_url("file:///C:/Windows/System32/calc.exe")
        )
        self.assertFalse(api_bridge._is_safe_external_url("file:///etc/passwd"))
        # Scheme matching must be case-insensitive
        self.assertFalse(
            api_bridge._is_safe_external_url("FILE:///C:/Windows/System32/calc.exe")
        )

    def test_javascript_scheme_is_rejected(self) -> None:
        self.assertFalse(api_bridge._is_safe_external_url("javascript:alert(1)"))
        self.assertFalse(api_bridge._is_safe_external_url("JavaScript:alert(1)"))
        # Whitespace/tab obfuscation should not bypass
        self.assertFalse(api_bridge._is_safe_external_url("  javascript:alert(1)"))
        self.assertFalse(api_bridge._is_safe_external_url("java\tscript:alert(1)"))

    def test_vbscript_and_data_schemes_are_rejected(self) -> None:
        self.assertFalse(api_bridge._is_safe_external_url("vbscript:msgbox(1)"))
        self.assertFalse(
            api_bridge._is_safe_external_url(
                "data:text/html,<script>alert(1)</script>"
            )
        )

    def test_unc_paths_are_rejected(self) -> None:
        self.assertFalse(
            api_bridge._is_safe_external_url(r"\\attacker\share\payload.exe")
        )
        self.assertFalse(
            api_bridge._is_safe_external_url("//attacker/share/payload.exe")
        )

    def test_bare_windows_path_is_rejected(self) -> None:
        self.assertFalse(
            api_bridge._is_safe_external_url(r"C:\Windows\System32\calc.exe")
        )
        self.assertFalse(
            api_bridge._is_safe_external_url("C:/Windows/System32/calc.exe")
        )

    def test_windows_custom_uri_schemes_are_rejected(self) -> None:
        for url in (
            "ms-cxh-full:",
            "ms-settings:privacy",
            "search-ms:displayname=x",
            "shell:startup",
            "ms-appinstaller:?source=https://evil.example/app.appinstaller",
        ):
            self.assertFalse(
                api_bridge._is_safe_external_url(url),
                f"Scheme should have been rejected: {url!r}",
            )

    def test_control_characters_are_rejected(self) -> None:
        self.assertFalse(
            api_bridge._is_safe_external_url(
                "https://projectkestrel.org/\r\nX-Injected: 1"
            )
        )
        self.assertFalse(
            api_bridge._is_safe_external_url("https://projectkestrel.org/\x00")
        )
        self.assertFalse(
            api_bridge._is_safe_external_url("https://projectkestrel.org/\x1b[2J")
        )

    def test_empty_and_non_string_are_rejected(self) -> None:
        self.assertFalse(api_bridge._is_safe_external_url(""))
        self.assertFalse(api_bridge._is_safe_external_url("   "))
        self.assertFalse(api_bridge._is_safe_external_url(None))  # type: ignore[arg-type]
        self.assertFalse(api_bridge._is_safe_external_url(123))  # type: ignore[arg-type]


@unittest.skipUnless(api_bridge is not None, f"api_bridge import failed: {_IMPORT_ERROR}")
class TestOpenUrlEndToEnd(unittest.TestCase):
    """Verify the full ``Api.open_url`` code path refuses to dispatch dangerous
    URLs to ``webbrowser.open``.  We monkeypatch ``webbrowser.open`` so no real
    browser/ShellExecute ever runs, even if the fix regresses."""

    def setUp(self) -> None:
        self.calls: list[str] = []
        self._orig_open = api_bridge.webbrowser.open
        api_bridge.webbrowser.open = (
            lambda url, *a, **kw: (self.calls.append(url) or True)
        )
        self.api = api_bridge.Api()

    def tearDown(self) -> None:
        api_bridge.webbrowser.open = self._orig_open

    def test_forwards_https(self) -> None:
        res = self.api.open_url("https://projectkestrel.org/")
        self.assertTrue(res.get("success"), f"Expected success, got {res!r}")
        self.assertEqual(self.calls, ["https://projectkestrel.org/"])

    def test_refuses_file_scheme(self) -> None:
        res = self.api.open_url("file:///C:/Windows/System32/calc.exe")
        self.assertFalse(
            res.get("success"),
            "Api.open_url must return success=False for file:// URLs",
        )
        self.assertEqual(
            self.calls,
            [],
            "Api.open_url must NOT forward file:// URLs to webbrowser.open",
        )

    def test_refuses_unc_path(self) -> None:
        res = self.api.open_url(r"\\attacker\share\evil.exe")
        self.assertFalse(res.get("success"))
        self.assertEqual(self.calls, [])

    def test_refuses_javascript(self) -> None:
        res = self.api.open_url("javascript:alert(1)")
        self.assertFalse(res.get("success"))
        self.assertEqual(self.calls, [])

    def test_refuses_data_html(self) -> None:
        res = self.api.open_url("data:text/html,<script>alert(1)</script>")
        self.assertFalse(res.get("success"))
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
