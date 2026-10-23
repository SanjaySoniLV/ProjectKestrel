import io
import os

import numpy as np
import rawpy
from PIL import Image, ImageOps

from .config import RAW_EXTENSIONS
from .logging_utils import warn as _warn

_RAW_EXTENSION_SET = {ext.lower() for ext in RAW_EXTENSIONS}


def _shorten_path_in_exception(exc: BaseException, path: str) -> None:
    """Rewrite ``exc`` in place so its message names the file, not its path.

    The runtime stderr log and the Live Analysis status line both render
    ``str(exc)`` and today carry only the file name; the per-folder JSON log
    records the full path separately in its own ``image_path`` field.
    Shortening here keeps a propagated cause from putting a full user path
    into the two channels that do not already hold one.

    The exception's *type* is never changed — the caller still sees
    ``PermissionError`` rather than something generic — because ``OSError``
    renders ``filename`` into its ``str()``, and the single-argument
    exceptions that embed a path in prose (PIL's ``UnidentifiedImageError``)
    render ``args[0]``. rawpy's errors carry a ``bytes`` message and no path,
    so both branches leave them alone.
    """
    if isinstance(exc, OSError):
        if exc.filename:
            exc.filename = os.path.basename(exc.filename)
        if exc.filename2:
            exc.filename2 = os.path.basename(exc.filename2)
    if exc.args and isinstance(exc.args[0], str) and path in exc.args[0]:
        exc.args = (
            (exc.args[0].replace(path, os.path.basename(path)),) + exc.args[1:]
        )


def _diagnose_unreadable(path: str) -> OSError | None:
    """Return the OS-level error explaining why ``path`` cannot be read.

    LibRaw reports a missing file, a permission denial and a read error on a
    failing drive all as the same ``LibRawIOError: b'Input/output error'``,
    so rawpy's exception on its own cannot tell those cases apart. Re-opening
    the file through Python recovers the errno that does: ``FileNotFoundError``
    (ENOENT), ``PermissionError`` (EACCES), ``OSError`` (EIO), and so on.

    Returns ``None`` when the file opens and reads fine, which means the
    decoder's own error — an unsupported or corrupt RAW variant — was the
    real cause and should be reported as-is.

    Only ever called on a path whose decode already failed, so the extra open
    costs nothing on the success path.
    """
    try:
        with open(path, "rb") as handle:
            # Read a byte as well as opening: a drive that is failing rather
            # than absent typically accepts the open and raises EIO here.
            handle.read(1)
    except OSError as os_exc:
        return os_exc
    except Exception:
        return None
    return None


def _decode_standard_image(path: str) -> np.ndarray:
    """Decode a non-RAW image with PIL. Raises on failure.

    The body is the non-RAW branch that ``read_image`` has always run; it is
    factored out so ``read_image_for_pipeline`` can let the failure reach its
    caller while ``read_image`` keeps swallowing it.
    """
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)

    if img.mode != 'RGB':
        img = img.convert('RGB')

    return np.array(img)


def decode_embedded_preview(path: str) -> np.ndarray | None:
    """Return the embedded JPEG preview as a (H, W, 3) uint8 RGB array, or None.

    Used as a graceful fallback when LibRaw can open the RAW container
    (reads metadata fine) but can't decompress the sensor data — most
    commonly Nikon's High Efficiency / HE* / NRAW formats on the Z8/Z9,
    which use the proprietary TicoRAW codec from intoPIX. The embedded
    JPEG preview is typically full sensor resolution; for ML inference
    at 640-1280 px input it's indistinguishable from a metered RAW
    decode. The only thing lost is sensor-level highlight recovery.

    Opens a fresh LibRaw handle on each call — once a postprocess()
    fails on a raw_obj, LibRaw rejects subsequent calls with
    LibRawOutOfOrderCallError, so the helper must not try to reuse a
    dirty handle.
    """
    try:
        with rawpy.imread(path) as raw:
            thumb = raw.extract_thumb()
    except Exception as exc:
        # Expected here: LibRawFileUnsupportedError, LibRawIOError,
        # LibRawNoThumbnailError, LibRawUnsupportedThumbnailError. All of
        # them, and anything else, stay non-fatal — the caller falls back to
        # a plain decode — but the reason is recorded, because it is
        # otherwise unrecoverable from a bug report.
        _warn(f"Embedded preview unavailable for {os.path.basename(path)}: "
              f"{type(exc).__name__}: {exc}")
        return None

    if thumb is None or not thumb.data:
        return None
    if thumb.format != rawpy.ThumbFormat.JPEG:
        return None
    try:
        with Image.open(io.BytesIO(thumb.data)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode != 'RGB':
                img = img.convert('RGB')
            return np.array(img)
    except Exception as exc:
        _warn(f"Embedded preview JPEG failed to decode for "
              f"{os.path.basename(path)}: {type(exc).__name__}: {exc}")
        return None


def read_image(path: str):
    """
    Read an image using rawpy for RAW files or PIL for standard formats.
    Returns a numpy array in RGB format (H, W, 3) or None on failure.
    """
    try:
        ext = os.path.splitext(path)[1].lower()

        if ext in _RAW_EXTENSION_SET:
            # Use rawpy for RAW files
            try:
                with rawpy.imread(path) as raw:
                    # postprocess() applies demosaicing, white balance, color
                    # correction, etc. Returns numpy array in RGB format.
                    return raw.postprocess()
            except rawpy.LibRawFileUnsupportedError:
                # LibRaw could parse the container but can't decompress the
                # sensor data (e.g. Nikon HE compression). Fall back to the
                # embedded JPEG preview, which is full sensor resolution on
                # modern Nikon bodies. Must reopen — the failed-postprocess
                # handle is in an out-of-order state.
                return decode_embedded_preview(path)
        else:
            # Use PIL for standard image formats (JPEG, PNG, TIFF, etc.)
            return _decode_standard_image(path)

    except rawpy.LibRawFileUnsupportedError:
        return None
    except rawpy.LibRawIOError:
        return None
    except Exception:
        return None


def read_image_for_pipeline(path: str):
    """
    Like read_image, but for RAW files returns the rawpy.RawPy object *open*
    alongside the postprocessed RGB array so that the pipeline can request a
    re-processed image with different exposure settings without re-reading the
    file from disk.

    Returns: (ndarray | None, rawpy.RawPy | None)
      - For RAW files: (rgb_array, raw_obj)  — caller must call raw_obj.close()
      - For non-RAW:   (rgb_array, None)

    Raises on failure rather than returning ``(None, None)``. The caller
    (``AnalysisPipeline._decode_image``) already runs this inside a
    ``try/except Exception`` that stores the exception on the per-image
    result, so the real cause reaches the JSON analysis log with a traceback,
    the runtime stderr log, and the Live Analysis dialog. Swallowing it here
    turned a permission denial, a missing file, an I/O error on a failing
    drive and an unsupported RAW variant into one indistinguishable
    ``RuntimeError``, which made those reports impossible to triage.

    The exception is the most specific one available: LibRaw reports the
    first three of those cases identically as ``LibRawIOError``, so a failed
    decode is re-checked with :func:`_diagnose_unreadable` and the OS-level
    error is raised instead when the file itself is the problem. Paths inside
    the message are shortened to the file name (see
    :func:`_shorten_path_in_exception`).
    """
    try:
        ext = os.path.splitext(path)[1].lower()

        if ext in _RAW_EXTENSION_SET:
            # Do NOT use a context manager — we intentionally keep the object open.
            # Do NOT call raw.postprocess() here — the pipeline immediately calls
            # build_metered_detection_image() which does its own decode.  The
            # default postprocess result would be discarded, wasting ~2 s per image.
            raw = rawpy.imread(path)
            return None, raw
        else:
            # Not read_image(): that one swallows the cause by contract, and
            # its other callers still depend on the None return.
            return _decode_standard_image(path), None

    except Exception as exc:
        precise = _diagnose_unreadable(path)
        if precise is not None:
            _shorten_path_in_exception(precise, path)
            raise precise from exc
        _shorten_path_in_exception(exc, path)
        raise
