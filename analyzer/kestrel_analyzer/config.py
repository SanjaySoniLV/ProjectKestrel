from pathlib import Path

VERSION = "2.0.1"

ANALYZER_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = ANALYZER_DIR.parent
MODELS_DIR = ANALYZER_DIR / "models"

SPECIESCLASSIFIER_PATH = MODELS_DIR / "model.onnx"
SPECIESCLASSIFIER_LABELS = MODELS_DIR / "labels.txt"
QUALITYCLASSIFIER_PATH = MODELS_DIR / "quality.onnx"
QUALITY_NORMALIZATION_DATA_PATH = MODELS_DIR / "quality_normalization_data.csv"

# SpeciesNet: bundled Kaggle-style folder (info.json + .pt + taxonomy). Passed as local model_name to speciesnet.ModelInfo.
SPECIESNET_MODEL_DIR = MODELS_DIR / "speciesnet"

# Runtime-selectable MegaDetector ONNX variants (all require .onnx.data sidecar files).
# mdv5a (accurate) and mdv6-e (YOLOv9-E, fast) are bundled under models/speciesnet.
# mdv5a provides best accuracy for wildlife detection; mdv6-e is faster but less accurate.
DEFAULT_DETECTOR_NAME = "mdv5a"
DETECTOR_ONNX_PATHS = {
    "mdv5a": SPECIESNET_MODEL_DIR / "mdv5a.onnx",
    "mdv6-e": SPECIESNET_MODEL_DIR / "mdv6-mit-yolov9-e.onnx",
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
# Caveat: .raf (Fujifilm) and .x3f (Sigma) decode via rawpy but lack capture-time
# extraction in raw_exif.py. Scene grouping for these formats falls back to
# AKAZE feature similarity (no timestamp shortcut). See raw_exif.UNSUPPORTED_EXTENSIONS.
RAW_EXTENSIONS = [
    ".cr2", ".cr3",         # Canon
    ".nef",                 # Nikon
    ".arw", ".srw",         # Sony / Samsung NX
    ".dng",                 # Adobe / generic
    ".orf",                 # Olympus
    ".rw2",                 # Panasonic
    ".pef",                 # Pentax
    ".sr2",                 # Sony (older)
    ".raf",                 # Fujifilm
    ".x3f",                 # Sigma
]
JPEG_EXTENSIONS = [".jpg", ".jpeg", ".png", '.tiff', '.tif']

DATABASE_NAME = "kestrel_database.csv"
METADATA_FILENAME = "kestrel_metadata.json"
SCENEDATA_FILENAME = "kestrel_scenedata.json"
KESTREL_DIR_NAME = ".kestrel"
LOG_FILENAME_PREFIX = "kestrel_error"
LOG_FILE_EXTENSION = "json"
