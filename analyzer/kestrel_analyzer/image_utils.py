import io
import os

import numpy as np
import rawpy
from PIL import Image, ImageOps

from .config import RAW_EXTENSIONS

_RAW_EXTENSION_SET = {ext.lower() for ext in RAW_EXTENSIONS}


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
    except (rawpy.LibRawFileUnsupportedError,
            rawpy.LibRawIOError,
            rawpy.LibRawNoThumbnailError,
            rawpy.LibRawUnsupportedThumbnailError):
        return None
    except Exception:
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
    except Exception:
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
            img = Image.open(path)
            img = ImageOps.exif_transpose(img)

            if img.mode != 'RGB':
                img = img.convert('RGB')

            return np.array(img)

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
      - On failure:    (None, None)
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
            return read_image(path), None

    except rawpy.LibRawFileUnsupportedError:
        return None, None
    except rawpy.LibRawIOError:
        return None, None
    except Exception:
        return None, None
