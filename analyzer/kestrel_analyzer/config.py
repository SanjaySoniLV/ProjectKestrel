from pathlib import Path

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

DATABASE_NAME = "kestrel_database.csv"
METADATA_FILENAME = "kestrel_metadata.json"
SCENEDATA_FILENAME = "kestrel_scenedata.json"
KESTREL_DIR_NAME = ".kestrel"
LOG_FILENAME_PREFIX = "kestrel_error"
LOG_FILE_EXTENSION = "json"
