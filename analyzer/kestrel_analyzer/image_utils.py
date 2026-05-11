import os
import numpy as np
import rawpy
from PIL import Image

from .config import RAW_EXTENSIONS

_RAW_EXTENSION_SET = {ext.lower() for ext in RAW_EXTENSIONS}


def read_image(path: str):
    """
    Read an image using rawpy for RAW files or PIL for standard formats.
    Returns a numpy array in RGB format (H, W, 3) or None on failure.
    """
    try:
        ext = os.path.splitext(path)[1].lower()

        if ext in _RAW_EXTENSION_SET:
            # Use rawpy for RAW files
            with rawpy.imread(path) as raw:
                # postprocess() applies demosaicing, white balance, color correction, etc.
                # Returns numpy array in RGB format
                rgb = raw.postprocess()
            return rgb
        else:
            # Use PIL for standard image formats (JPEG, PNG, TIFF, etc.)
            img = Image.open(path)

            # Handle EXIF orientation
            from PIL import ImageOps
            img = ImageOps.exif_transpose(img)

            # Convert to RGB (handles grayscale, RGBA, etc.)
            if img.mode != 'RGB':
                img = img.convert('RGB')

            # Convert to numpy array
            rgb = np.array(img)
            return rgb

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
