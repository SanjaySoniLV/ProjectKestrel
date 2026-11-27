"""Atomic file writes: the one temp-file + ``os.replace`` implementation.

Writing with ``open(path, "w")`` truncates the destination first, so a crash,
power loss or full disk mid-write leaves a partial file. Every writer here goes
to a unique temp file in the destination's directory, flushes and fsyncs it,
then ``os.replace``s it into place, so readers see either the whole previous
file or the whole new one. On failure the temp file is removed and the previous
file is left untouched; there is deliberately no fallback to a direct write.

Standard library only, so modules that must not pull in pandas (settings,
sidecars, cloud stores) can import it. ``kestrel_analyzer.database`` re-exports
these names.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from typing import Any, Callable, Optional

DEFAULT_PREFIX = ".kestrel_atomic_"
DEFAULT_SUFFIX = ".tmp"


def retry_on_file_lock(op, attempts: int = 12, delay: float = 0.02):
    """Run ``op()``, retrying briefly on Windows' transient file-sharing errors.

    ``os.replace`` is atomic on both POSIX and Windows, but Windows adds a
    constraint POSIX does not have: CPython opens files without
    ``FILE_SHARE_DELETE``, so while *any* handle is open on the destination the
    underlying ``MoveFileEx`` fails with ``ERROR_ACCESS_DENIED`` (5) or
    ``ERROR_SHARING_VIOLATION`` (32). Symmetrically, a reader that calls
    ``open()`` during the rename can catch the destination in a delete-pending
    state and get ``ERROR_ACCESS_DENIED``. Both surface as ``PermissionError``.

    So on Windows the atomic write guarantees all-or-nothing *content* but not a
    collision-free ``open()``: the pipeline saving after every image and the UI's
    auto-refresh reader will occasionally step on each other in both directions.
    Both sides retry through here rather than failing — the writer to avoid
    silently dropping a save, the reader to avoid a spurious load error.

    Escalating backoff caps the total wait at roughly 1.3s; if the file is still
    locked after that the error propagates to the caller. POSIX never raises
    here and always succeeds on the first attempt.

    This is a mitigation, not a guarantee. A reader that reopens the file in a
    zero-gap loop can starve the writer past the retry window — genuinely fixing
    that would mean opening the destination with ``FILE_SHARE_DELETE``, which
    CPython's ``open()`` cannot do. The app's readers poll on a UI timer, so the
    window is wide open in practice.
    """
    for attempt in range(attempts):
        try:
            return op()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay * (attempt + 1))


def write_atomic(
    path,
    write_fn: Callable[[Any], None],
    *,
    binary: bool = False,
    encoding: str = "utf-8",
    newline: Optional[str] = "",
    prefix: str = DEFAULT_PREFIX,
    suffix: str = DEFAULT_SUFFIX,
    fsync: bool = True,
    before_replace: Optional[Callable[[str], None]] = None,
) -> None:
    """Call ``write_fn(file)`` on a temp file beside ``path``, then replace ``path``.

    ``write_fn`` gets an open file (text unless ``binary``) and writes the whole
    payload. ``prefix``/``suffix`` name the temp file, so callers that reap
    orphans from crashed writes can find theirs. ``before_replace(tmp_path)``
    runs after the data is on disk and before the replace, for fixing
    permissions or promoting a backup. ``fsync=False`` is for frequent,
    disposable writes such as live-preview frames.
    """
    path = os.fspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    # mkstemp creates with O_EXCL, so concurrent writers never share a temp path.
    tmp_fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
    try:
        # If fdopen raises it has not taken ownership of the descriptor; close
        # it so it can't leak (on Windows a leaked handle keeps the temp locked).
        try:
            if binary:
                f = os.fdopen(tmp_fd, "wb")
            else:
                f = os.fdopen(tmp_fd, "w", encoding=encoding, newline=newline)
        except BaseException:
            os.close(tmp_fd)
            raise
        with f:
            write_fn(f)
            # flush() errors (ENOSPC, EIO) must propagate: a partial temp file
            # must not be os.replace'd over a good destination.
            f.flush()
            if fsync:
                try:
                    os.fsync(f.fileno())
                except OSError:
                    # fsync can legitimately fail on some network filesystems;
                    # the replace below still gives readers an all-or-nothing view.
                    pass
        if before_replace is not None:
            before_replace(tmp)
        retry_on_file_lock(lambda: os.replace(tmp, path))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass  # already gone or never created; the original error is what matters
        raise


def write_text_atomic(path, text: str, encoding: str = "utf-8", **kwargs) -> None:
    """Write ``text`` to ``path`` atomically. ``kwargs`` go to ``write_atomic``."""
    write_atomic(path, lambda f: f.write(text), encoding=encoding, **kwargs)


def write_bytes_atomic(path, data: bytes, **kwargs) -> None:
    """Write ``data`` to ``path`` atomically. ``kwargs`` go to ``write_atomic``."""
    write_atomic(path, lambda f: f.write(data), binary=True, **kwargs)


def write_json_atomic(path, obj, indent: Optional[int] = 2, sort_keys: bool = False, **kwargs) -> None:
    """Serialize ``obj`` to JSON at ``path`` atomically.

    Streams through ``json.dump`` rather than building the string first, which
    matters for large scenedata payloads.
    """
    write_atomic(path, lambda f: json.dump(obj, f, indent=indent, sort_keys=sort_keys), **kwargs)


def copy_file_atomic(src_path: str, dst_path: str) -> None:
    """Copy ``src_path`` over ``dst_path`` atomically, keeping mode and mtime."""
    def _copy(dst_f):
        with open(src_path, "rb") as src_f:
            shutil.copyfileobj(src_f, dst_f)

    def _copystat(tmp):
        try:
            shutil.copystat(src_path, tmp)
        except OSError:
            pass  # best-effort, as with shutil.copy2

    write_atomic(dst_path, _copy, binary=True, before_replace=_copystat)
