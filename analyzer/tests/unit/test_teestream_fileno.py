"""Regression test: _TeeStream.fileno must work when the wrapped stream is None.

PyInstaller --windowed builds start with sys.stdout/stderr == None.
_enable_runtime_log_capture wraps them in _TeeStream, and _TeeStream.fileno()
did ``self._original_stream.fileno()`` -> AttributeError on None. faulthandler's
default target is sys.stderr, so faulthandler.enable() then raised and was
swallowed -- silently disabling native crash diagnostics in the shipped build.
fileno() now falls back to the runtime log file's descriptor.
"""

import errno
import faulthandler
import io
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    import visualizer
except Exception as e:  # pragma: no cover - environment-dependent
    pytest.skip(f"visualizer module not importable in this env: {e}", allow_module_level=True)

pytestmark = pytest.mark.unit


def test_fileno_falls_back_to_log_handle_when_stream_is_none(tmp_path):
    with open(tmp_path / "log.txt", "w") as logf:
        tee = visualizer._TeeStream(None, logf)
        assert tee.fileno() == logf.fileno()


def test_fileno_prefers_original_stream_when_present(tmp_path):
    # Context managers close both files even if the second open raises, so a
    # partial-initialization failure can't leak the first handle.
    with open(tmp_path / "log.txt", "w") as logf, \
            open(tmp_path / "real.txt", "w") as realf:
        tee = visualizer._TeeStream(realf, logf)
        assert tee.fileno() == realf.fileno()


class _FilenoRaises:
    """Minimal stand-in whose fileno() raises a chosen exception."""

    def __init__(self, exc):
        self._exc = exc

    def fileno(self):
        raise self._exc


def test_fileno_falls_back_on_unsupported_operation(tmp_path):
    """io.UnsupportedOperation is the genuine 'this stream has no fd' case."""
    with open(tmp_path / "log.txt", "w") as logf:
        tee = visualizer._TeeStream(
            _FilenoRaises(io.UnsupportedOperation("fileno")), logf
        )
        assert tee.fileno() == logf.fileno()


def test_fileno_falls_back_on_closed_stream_valueerror(tmp_path):
    with open(tmp_path / "log.txt", "w") as logf:
        tee = visualizer._TeeStream(
            _FilenoRaises(ValueError("I/O operation on closed file")), logf
        )
        assert tee.fileno() == logf.fileno()


def test_fileno_propagates_unexpected_oserror(tmp_path):
    """EIO/EBADF must not be swallowed and redirected to the log handle."""
    with open(tmp_path / "log.txt", "w") as logf:
        tee = visualizer._TeeStream(
            _FilenoRaises(OSError(errno.EIO, "input/output error")), logf
        )
        with pytest.raises(OSError) as excinfo:
            tee.fileno()
        assert excinfo.value.errno == errno.EIO


def test_faulthandler_enable_does_not_raise_with_none_stream(tmp_path):
    was_enabled = faulthandler.is_enabled()
    old_err = sys.stderr
    # Single open immediately before the try can't leak (nothing between the
    # open and the try can raise), and this lets the finally repoint
    # faulthandler off the temp file BEFORE the file is closed.
    logf = open(tmp_path / "log.txt", "w")
    try:
        sys.stderr = visualizer._TeeStream(None, logf)
        # Previously raised AttributeError via fileno(); must succeed now.
        faulthandler.enable(file=sys.stderr)
        assert faulthandler.is_enabled()
    finally:
        sys.stderr = old_err
        # Restore faulthandler's state so pytest's own faulthandler keeps
        # working, before closing the temp log it was pointed at.
        if was_enabled:
            faulthandler.enable()
        else:
            faulthandler.disable()
        logf.close()
