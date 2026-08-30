import os
from pathlib import Path
from typing import Iterable

VERSION = "2.0.4"

ANALYZER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = ANALYZER_DIR.parent
MODELS_DIR = ANALYZER_DIR / "models"

SPECIESCLASSIFIER_PATH = MODELS_DIR / "model.onnx"
SPECIESCLASSIFIER_LABELS = MODELS_DIR / "labels.txt"
QUALITYCLASSIFIER_PATH = MODELS_DIR / "quality.onnx"
QUALITY_NORMALIZATION_DATA_PATH = MODELS_DIR / "quality_normalization_data.csv"

# SpeciesNet: bundled Kaggle-style folder (info.json + .pt + taxonomy). Passed as local model_name to speciesnet.ModelInfo.
SPECIESNET_MODEL_DIR = MODELS_DIR / "speciesnet"

# Runtime-selectable MegaDetector ONNX variants.
# mdv5a (accurate, YOLOv5x6 @ 1280) and mdv1000-cedar (fast, YOLOv9 gelan-c @ 640)
# are bundled under models/speciesnet. mdv5a uses a `.onnx` + `.onnx.data` sidecar
# pair; mdv1000-cedar is a single-file ONNX (no sidecar) because it was exported
# via torch.onnx.export(dynamo=False), which embeds weights inline. The legacy
# exporter path is also what makes cedar DirectML-compatible — the dynamo-exported
# mdv1000 variants hit a Reshape op DML rejects.
DEFAULT_DETECTOR_NAME = "mdv5a"
DETECTOR_ONNX_PATHS = {
    "mdv5a":         SPECIESNET_MODEL_DIR / "mdv5a.onnx",
    "mdv1000-cedar": SPECIESNET_MODEL_DIR / "mdv1000-cedar.onnx",
}

# SAM-HQ ViT-Tiny: split encoder + decoder ONNX files.
SAM_ENC_ONNX_PATH = SPECIESNET_MODEL_DIR / "sam_hq_vit_tiny_encoder.onnx"
SAM_DEC_ONNX_PATH = SPECIESNET_MODEL_DIR / "sam_hq_vit_tiny_decoder.onnx"

WILDLIFE_CATEGORIES = [
    "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "bird"
]

# Canonical RAW format list. Single source of truth — all other modules import
# from here rather than maintaining their own copies. Adding a format here
# automatically enables it for pipeline discovery, folder inspection, RAW
# preview routing, and editor-launch allowlisting.
#
# Every extension below has both rawpy/LibRaw decode support and an in-house
# capture-time extractor in raw_exif.py — no exiftool dependency.
RAW_EXTENSIONS = [
    ".cr2", ".cr3",         # Canon
    ".nef", ".nrw",         # Nikon (DSLR/Z + Coolpix)
    ".arw", ".srw",         # Sony / Samsung NX
    ".dng",                 # Adobe / generic
    ".orf",                 # Olympus
    ".rw2",                 # Panasonic
    ".pef",                 # Pentax
    ".sr2",                 # Sony (older)
    ".raf",                 # Fujifilm
    ".x3f",                 # Sigma
    ".3fr", ".fff",         # Hasselblad
    ".iiq",                 # Phase One
    ".mos",                 # Leaf
    ".rwl",                 # Leica
    ".erf",                 # Epson
    ".dcr", ".kdc",         # Kodak (DCS / EasyShare)
    ".mef",                 # Mamiya
    ".mrw",                 # Minolta / Konica Minolta
]
JPEG_EXTENSIONS = [".jpg", ".jpeg", ".png", '.tiff', '.tif']


def is_supported_image_file(name: str, allowed_exts: Iterable[str]) -> bool:
    """True if ``name`` is a real image filename that should be processed.

    Filters out:
      * Hidden files (anything starting with ``.``), including macOS
        ``.DS_Store`` and dot-prefixed app metadata.
      * macOS AppleDouble companion files (``._<filename>``), which are
        created automatically when macOS writes to a non-HFS/APFS volume
        (exFAT/NTFS external drives, SMB shares) to preserve extended
        attributes. They carry the same extension as the real file
        (e.g. ``._IMG_0142.ARW``) so an extension-only filter lets them
        through, but they contain ~4 KB of metadata — not image data —
        so LibRaw rejects every one of them with "Unsupported file
        format or not RAW file". Both prefixes are covered by the
        leading-dot test.

    Pass ``allowed_exts`` as an iterable of lowercase extensions
    (including the leading dot), e.g. ``RAW_EXTENSIONS`` or
    ``set(RAW_EXTENSIONS) | set(JPEG_EXTENSIONS)``.
    """
    if not name or name[0] == '.':
        return False
    dot = name.rfind('.')
    if dot < 0:
        return False
    return name[dot:].lower() in allowed_exts


def select_camera_images(names: Iterable[str]) -> list[str]:
    """Pick the canonical set of camera images from a directory listing.

    Prefers RAW originals over JPEG companions: if both ``IMG_0001.CR3`` and
    ``IMG_0001.JPG`` are present, the JPEG is dropped as a redundant in-camera
    sidecar. JPEGs (and other JPEG-family formats such as PNG / TIFF) that
    have no same-stem RAW partner are kept so mixed RAW+JPG/JPG-only shoots
    are not silently truncated.

    Stem matching is case-insensitive to handle Windows / macOS case-
    folding filesystems where ``IMG_0001.CR3`` and ``img_0001.jpg`` refer
    to the same capture. Hidden / AppleDouble files are filtered via
    :func:`is_supported_image_file`.
    """
    raw_names: list[str] = []
    jpeg_names: list[str] = []
    for name in names:
        if is_supported_image_file(name, RAW_EXTENSIONS):
            raw_names.append(name)
        elif is_supported_image_file(name, JPEG_EXTENSIONS):
            jpeg_names.append(name)
    raw_stems = {os.path.splitext(n)[0].lower() for n in raw_names}
    orphan_jpegs = [
        n for n in jpeg_names
        if os.path.splitext(n)[0].lower() not in raw_stems
    ]
    return raw_names + orphan_jpegs


DATABASE_NAME = "kestrel_database.csv"
METADATA_FILENAME = "kestrel_metadata.json"
SCENEDATA_FILENAME = "kestrel_scenedata.json"
KESTREL_DIR_NAME = ".kestrel"
LOG_FILENAME_PREFIX = "kestrel_error"
LOG_FILE_EXTENSION = "json"
