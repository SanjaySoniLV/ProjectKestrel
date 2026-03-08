"""Image reading, EXIF extraction, and preprocessing utilities.

Supports RAW files (Nikon NEF, Canon CR2/CR3, Sony ARW, etc.) via rawpy
and standard formats (JPEG, PNG) via PIL. Extracts camera metadata from
EXIF for Nikon Z8 and other cameras. Provides exposure normalisation
to improve analysis of backlit subjects.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import rawpy
from PIL import Image, ImageOps, ExifTags

logger = logging.getLogger(__name__)

# EXIF tag IDs we care about
_EXIF_TAGS = {v: k for k, v in ExifTags.TAGS.items()}


@dataclass
class ImageMetadata:
    """Camera and exposure metadata extracted from EXIF."""
    camera_make: str = ""
    camera_model: str = ""
    lens_model: str = ""
    focal_length: float = 0.0
    aperture: float = 0.0
    shutter_speed: str = ""
    iso: int = 0
    width: int = 0
    height: int = 0


def extract_exif_metadata(path: str) -> ImageMetadata:
    """Extract camera metadata from EXIF tags.

    Works with both JPEG and RAW files (via PIL for JPEG,
    rawpy for RAW basic metadata).
    """
    meta = ImageMetadata()
    ext = os.path.splitext(path)[1].lower()

    raw_extensions = {'.cr2', '.cr3', '.nef', '.arw', '.dng', '.raf', '.orf', '.rw2', '.srw', '.pef', '.sr2', '.x3f'}

    if ext in raw_extensions:
        try:
            with rawpy.imread(path) as raw:
                meta.width = raw.sizes.width
                meta.height = raw.sizes.height
            # Try PIL for EXIF from embedded JPEG preview
            try:
                img = Image.open(path)
                _populate_exif(img, meta)
            except Exception:
                pass
        except Exception as e:
            logger.debug("RAW EXIF extraction failed for %s: %s", path, e)
    else:
        try:
            img = Image.open(path)
            meta.width = img.width
            meta.height = img.height
            _populate_exif(img, meta)
        except Exception as e:
            logger.debug("EXIF extraction failed for %s: %s", path, e)

    return meta


def _populate_exif(img: Image.Image, meta: ImageMetadata) -> None:
    """Populate metadata from PIL Image EXIF data."""
    try:
        exif_data = img._getexif()
        if exif_data is None:
            return

        tag_map = {v: k for k, v in ExifTags.TAGS.items()}

        if tag_map.get("Make") in exif_data:
            meta.camera_make = str(exif_data[tag_map["Make"]]).strip()
        if tag_map.get("Model") in exif_data:
            meta.camera_model = str(exif_data[tag_map["Model"]]).strip()
        if tag_map.get("FocalLength") in exif_data:
            fl = exif_data[tag_map["FocalLength"]]
            meta.focal_length = float(fl) if not hasattr(fl, 'numerator') else fl.numerator / fl.denominator
        if tag_map.get("FNumber") in exif_data:
            fn = exif_data[tag_map["FNumber"]]
            meta.aperture = float(fn) if not hasattr(fn, 'numerator') else fn.numerator / fn.denominator
        if tag_map.get("ExposureTime") in exif_data:
            et = exif_data[tag_map["ExposureTime"]]
            if hasattr(et, 'numerator'):
                if et.denominator > 1:
                    meta.shutter_speed = f"{et.numerator}/{et.denominator}"
                else:
                    meta.shutter_speed = f"{et.numerator}"
            else:
                meta.shutter_speed = str(et)
        if tag_map.get("ISOSpeedRatings") in exif_data:
            meta.iso = int(exif_data[tag_map["ISOSpeedRatings"]])

        # Lens info - may be in LensModel tag or MakerNote
        if tag_map.get("LensModel") in exif_data:
            meta.lens_model = str(exif_data[tag_map["LensModel"]]).strip()

    except Exception as e:
        logger.debug("EXIF tag parsing error: %s", e)


def normalise_exposure(img: np.ndarray, mask: np.ndarray = None) -> np.ndarray:
    """Normalise exposure of the subject area to handle backlit images.

    Uses CLAHE (Contrast Limited Adaptive Histogram Equalisation) on the
    luminance channel to bring out detail in underexposed subject regions
    without blowing out highlights.

    Args:
        img: RGB image array (H, W, 3)
        mask: Optional binary mask of the subject

    Returns:
        Exposure-normalised RGB image array
    """
    lab = cv2.cvtColor(img, cv2.COLOR_RGB2LAB)
    l_channel = lab[:, :, 0]

    # Check if the subject is significantly underexposed
    if mask is not None and mask.any():
        subject_mean = l_channel[mask.astype(bool)].mean()
        overall_mean = l_channel.mean()
        # Only normalise if subject is notably darker than surroundings
        if subject_mean >= overall_mean * 0.75:
            return img

    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    lab[:, :, 0] = clahe.apply(l_channel)

    return cv2.cvtColor(lab, cv2.COLOR_LAB2RGB)


def read_image(path: str) -> Optional[np.ndarray]:
    """Read an image using rawpy for RAW files or PIL for standard formats.

    Returns a numpy array in RGB format (H, W, 3) or None on failure.
    """
    try:
        ext = os.path.splitext(path)[1].lower()

        raw_extensions = {'.cr2', '.cr3', '.nef', '.arw', '.dng', '.raf', '.orf', '.rw2', '.srw', '.pef', '.sr2', '.x3f'}

        if ext in raw_extensions:
            with rawpy.imread(path) as raw:
                rgb = raw.postprocess(
                    use_camera_wb=True,
                    half_size=False,
                    no_auto_bright=False,
                    output_bps=8,
                )
            logger.debug("Read RAW image %s, shape=%s", os.path.basename(path), rgb.shape)
            return rgb
        else:
            img = Image.open(path)
            img = ImageOps.exif_transpose(img)

            if img.mode != 'RGB':
                img = img.convert('RGB')

            rgb = np.array(img)
            logger.debug("Read image %s, shape=%s", os.path.basename(path), rgb.shape)
            return rgb

    except rawpy.LibRawFileUnsupportedError:
        logger.warning("RAW format not supported for %s", path)
        return None
    except rawpy.LibRawIOError as e:
        logger.warning("I/O error reading RAW file %s: %s", path, e)
        return None
    except Exception as e:
        logger.error("Error reading image %s: %s", path, e)
        return None


# Import cv2 here to avoid circular imports with detail_analysis
import cv2
