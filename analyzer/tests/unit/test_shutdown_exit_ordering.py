"""Pin the order of the shutdown sequence in ``visualizer.main()``.

Reaching ``main()``'s ``finally`` block means ``webview.start()`` returned,
which only happens once the UI window has closed — the exit is clean at that
moment. The teardown that follows (cloud-upload signal, cache cleanups,
``server.shutdown()``) is best-effort and can block indefinitely or be cut
short by the OS reaping the process during quit.

While ``_mark_session_clean_exit()`` ran at the *end* of that block, any such
stall lost the 'clean' marker, leaving ``app_session_exit_reason`` at
'unknown' so the next launch raised a false unclean-shutdown recovery prompt.
Crash reports showed prior-session logs stopping partway through the teardown
with no 'Server stopped.' and no clean_exit lines.

These tests read the source with ``ast`` rather than importing it, so they run
in environments without the GUI dependencies ``visualizer`` pulls in at import.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

VISUALIZER_SRC = Path(__file__).parent.parent.parent / 'visualizer.py'

# Teardown calls that must not be able to strand the clean-exit write.
TEARDOWN_CALLS = (
    'stop_cloud_uploads_for_shutdown',
    'cleanup_tracked_culling_caches',
    'cleanup_sample_set_mirrors',
    'shutdown',
    'server_close',
)


def _main_finally_body():
    """Return the statements in the ``finally`` block of ``main()``."""
    tree = ast.parse(VISUALIZER_SRC.read_text(encoding='utf-8'))
    main_fn = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == 'main'
        ),
        None,
    )
    assert main_fn is not None, 'visualizer.main() not found'

    tries = [n for n in main_fn.body if isinstance(n, ast.Try) and n.finalbody]
    assert tries, 'main() has no try/finally'
    # The shutdown sequence is the last top-level try/finally in main().
    return tries[-1].finalbody


def _called_names(nodes):
    """Every called function/attribute name in ``nodes``, in source order."""
    names = []
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                if isinstance(fn, ast.Name):
                    names.append((sub.lineno, fn.id))
                elif isinstance(fn, ast.Attribute):
                    names.append((sub.lineno, fn.attr))
    return [name for _, name in sorted(names)]


class TestCleanExitOrdering:
    def test_clean_exit_is_marked_in_the_finally_block(self):
        assert '_mark_session_clean_exit' in _called_names(_main_finally_body())

    def test_clean_exit_is_marked_before_any_teardown_step(self):
        names = _called_names(_main_finally_body())
        mark_at = names.index('_mark_session_clean_exit')

        for call in TEARDOWN_CALLS:
            assert call in names, f'{call} missing from the shutdown sequence'
            assert mark_at < names.index(call), (
                f'_mark_session_clean_exit() runs after {call}(). A stall or kill '
                f'in the teardown would lose the clean marker and raise a false '
                f'unclean-shutdown prompt on the next launch.'
            )

    def test_clean_exit_is_marked_exactly_once(self):
        names = _called_names(_main_finally_body())
        assert names.count('_mark_session_clean_exit') == 1

    def test_exit_path_completion_is_still_logged_last(self):
        """The end-of-teardown line distinguishes a completed shutdown from a
        hang, which stays worth diagnosing even once it no longer misreports."""
        body = _main_finally_body()
        source = ast.unparse(ast.Module(body=body, type_ignores=[]))
        assert 'main: exit path complete' in source

        names = _called_names(body)
        assert names.index('_mark_session_clean_exit') < names.index('shutdown')
        # The completion log sits after server shutdown.
        log_lines = [
            node.lineno
            for node in ast.walk(ast.Module(body=body, type_ignores=[]))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == '_log_shutdown_state'
        ]
        shutdown_lines = [
            node.lineno
            for node in ast.walk(ast.Module(body=body, type_ignores=[]))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'shutdown'
        ]
        assert log_lines and shutdown_lines
        assert max(log_lines) > max(shutdown_lines)
