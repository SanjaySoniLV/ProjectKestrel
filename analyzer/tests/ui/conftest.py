"""UI test guard: skip the whole module cleanly when pywebview can't run.

Without a real display / WebView2 backend, every UI test would error out with
a useless traceback. This conftest detects unavailability up front and emits
a pytest.skip with a readable reason.
"""

import os
import sys

import pytest


def _can_run_pywebview() -> tuple[bool, str]:
    try:
        import webview  # noqa: F401
    except Exception as exc:
        return False, f"pywebview import failed: {exc}"
    # On Linux without DISPLAY/WAYLAND_DISPLAY/xvfb, GTK will error at runtime.
    if sys.platform.startswith("linux"):
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return False, "No display server (DISPLAY/WAYLAND_DISPLAY) — pywebview needs xvfb on headless Linux"
    return True, ""


@pytest.fixture(scope="session", autouse=True)
def _skip_if_no_pywebview():
    ok, reason = _can_run_pywebview()
    if not ok:
        pytest.skip(reason, allow_module_level=False)
    yield
