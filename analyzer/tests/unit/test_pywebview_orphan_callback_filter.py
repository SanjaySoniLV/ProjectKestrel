"""Unit tests for the pywebview orphan-callback filter in the crash handler.

When pywebview's JS-side promise callback is gone (because the page reloaded
or navigated between the API call and Python's return), it raises
``JavascriptException`` with a "_returnValuesCallbacks.<fn>.<id> is not a
function" message on its background dispatch thread. Kestrel's
``threading.excepthook`` must treat this as a no-op rather than a crash.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    from visualizer import _is_pywebview_orphan_callback
except Exception as e:  # pragma: no cover - environment-dependent
    pytest.skip(f'visualizer module not importable in this env: {e}', allow_module_level=True)


pytestmark = pytest.mark.unit


# Stand-in matching pywebview's ``webview.errors.JavascriptException`` by
# name only — the filter uses ``exc_type.__name__`` for portability across
# pywebview versions and avoids importing the GUI library at test time.
JavascriptException = type('JavascriptException', (Exception,), {})


class TestIsPywebviewOrphanCallback:
    def test_matches_inspect_folders_orphan(self):
        msg = (
            "{'name': 'TypeError', "
            "'stack': 'TypeError: window.pywebview._returnValuesCallbacks."
            "inspect_folders.3212165498894174 is not a function\\n', "
            "'message': 'window.pywebview._returnValuesCallbacks."
            "inspect_folders.3212165498894174 is not a function'}"
        )
        exc = JavascriptException(msg)
        assert _is_pywebview_orphan_callback(JavascriptException, exc) is True

    def test_matches_any_api_function_orphan(self):
        msg = (
            "window.pywebview._returnValuesCallbacks.choose_directories."
            "987654321 is not a function"
        )
        exc = JavascriptException(msg)
        assert _is_pywebview_orphan_callback(JavascriptException, exc) is True

    def test_rejects_other_js_exception(self):
        exc = JavascriptException("ReferenceError: foo is not defined")
        assert _is_pywebview_orphan_callback(JavascriptException, exc) is False

    def test_rejects_non_js_exception(self):
        exc = RuntimeError(
            "window.pywebview._returnValuesCallbacks.x.1 is not a function"
        )
        assert _is_pywebview_orphan_callback(RuntimeError, exc) is False

    def test_rejects_none(self):
        assert _is_pywebview_orphan_callback(None, None) is False

    def test_rejects_partial_message(self):
        exc = JavascriptException("_returnValuesCallbacks is missing")
        assert _is_pywebview_orphan_callback(JavascriptException, exc) is False
