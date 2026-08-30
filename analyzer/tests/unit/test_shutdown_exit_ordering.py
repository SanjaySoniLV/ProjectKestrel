"""Pin the order of the shutdown sequence in ``visualizer.main()``.

Once ``webview.start()`` has returned the UI window has closed and the exit is
clean. The teardown that follows in ``main()``'s ``finally`` (cloud-upload
signal, cache cleanups, ``server.shutdown()``) is best-effort and can block
indefinitely or be cut short by the OS reaping the process during quit.

While ``_mark_session_clean_exit()`` ran at the *end* of that block, any such
stall lost the 'clean' marker, leaving ``app_session_exit_reason`` at
'unknown' so the next launch raised a false unclean-shutdown recovery prompt.
Crash reports showed prior-session logs stopping partway through the teardown
with no 'Server stopped.' and no clean_exit lines.

The ``finally`` is *not* by itself proof of a clean exit: the ``try`` opens
long before ``webview.start()``, so it also runs when startup raises. Writing
'clean' early therefore has to be gated on that call having returned, or a
teardown hang after a genuine crash would leave 'clean' standing and suppress
the very report this machinery exists to collect.

``main()`` cannot be executed in a test — it binds a socket and then blocks in
``webview.start()`` — so the ordering is asserted against the parsed source
instead. (``visualizer`` itself imports fine headless; it guards ``import
webview``. Executing ``main()`` is the part that is out of reach.)
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


def _main_fn():
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
    return main_fn


def _main_try():
    """Return the ``try``/``finally`` holding ``main()``'s shutdown sequence."""
    main_fn = _main_fn()
    tries = [n for n in main_fn.body if isinstance(n, ast.Try) and n.finalbody]
    assert tries, 'main() has no try/finally'
    # If main() ever grows a second try/finally these assertions would silently
    # retarget, so make that a loud failure rather than a quiet mis-test.
    assert len(tries) == 1, (
        f'main() has {len(tries)} top-level try/finally blocks; this test can no '
        f'longer tell which one is the shutdown sequence'
    )
    return tries[0]


def _main_finally_body():
    """Return the statements in the ``finally`` block of ``main()``."""
    return _main_try().finalbody


def _assigned_names(nodes):
    """Every name assigned anywhere in ``nodes``."""
    out = set()
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                for target in sub.targets:
                    if isinstance(target, ast.Name):
                        out.add(target.id)
    return out


def _called_names(nodes):
    """Every called function/attribute name in ``nodes``, in source order.

    Sorted by (line, column) rather than (line, name) so two calls on one line
    keep their source order instead of being ordered alphabetically.
    """
    names = []
    for node in nodes:
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                fn = sub.func
                if isinstance(fn, ast.Name):
                    names.append((sub.lineno, sub.col_offset, fn.id))
                elif isinstance(fn, ast.Attribute):
                    names.append((sub.lineno, sub.col_offset, fn.attr))
    return [name for _, _, name in sorted(names)]


GATE_FLAG = '_webview_returned'


class TestCleanExitIsGatedOnAWindowHavingRun:
    """The finally also runs when startup raises; that is not a clean exit."""

    def test_the_mark_is_guarded_by_the_gate_flag(self):
        guarded = [
            stmt
            for stmt in _main_finally_body()
            if isinstance(stmt, ast.If)
            and GATE_FLAG in {
                n.id for n in ast.walk(stmt.test) if isinstance(n, ast.Name)
            }
            and '_mark_session_clean_exit' in _called_names(stmt.body)
        ]
        assert len(guarded) == 1, (
            f'_mark_session_clean_exit() must be called under `if {GATE_FLAG}:`. '
            f'Ungated, an exception from Api(), create_window() or '
            f'webview.start() records a startup crash as a clean exit — and '
            f'because the mark is now written before the teardown, a hang there '
            f'leaves it standing instead of being overwritten with "crash".'
        )

    def test_the_gate_is_set_only_after_webview_start_returns(self):
        try_node = _main_try()
        sets = [
            stmt
            for stmt in ast.walk(ast.Module(body=try_node.body, type_ignores=[]))
            if isinstance(stmt, ast.Assign)
            and any(
                isinstance(t, ast.Name) and t.id == GATE_FLAG for t in stmt.targets
            )
        ]
        assert len(sets) == 1, f'{GATE_FLAG} must be set exactly once in the try'
        starts = [
            node.lineno
            for node in ast.walk(ast.Module(body=try_node.body, type_ignores=[]))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == 'start'
        ]
        assert starts, 'webview.start() not found in main()'
        assert sets[0].lineno > max(starts), (
            f'{GATE_FLAG} is set before webview.start() returns, so a window '
            f'that never opened would still be recorded as a clean exit'
        )

    def test_the_gate_is_initialised_before_the_try(self):
        """Otherwise the finally raises NameError, masking the real exception."""
        main_fn = _main_fn()
        try_node = _main_try()
        before = [n for n in main_fn.body if n.lineno < try_node.lineno]
        assert GATE_FLAG in _assigned_names(before), (
            f'{GATE_FLAG} must be initialised before the try, or a startup '
            f'exception turns into a NameError inside the finally'
        )


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
