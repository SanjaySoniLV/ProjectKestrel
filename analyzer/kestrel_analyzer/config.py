from pathlib import Path

VERSION = "2.0.0"

ANALYZER_DIR = Path(__file__).resolve().parents[1]
MODELS_DIR = ANALYZER_DIR / "models"

SPECIESCLASSIFIER_PATH = MODELS_DIR / "model.onnx"
SPECIESCLASSIFIER_LABELS = MODELS_DIR / "labels.txt"
QUALITYCLASSIFIER_PATH = MODELS_DIR / "quality.keras"
MASK_RCNN_WEIGHTS_PATH = MODELS_DIR / "mask_rcnn_resnet50_fpn_v2.pth"

WILDLIFE_CATEGORIES = [
    "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "bird"
]

RAW_EXTENSIONS = [".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".raf", ".rw2", ".pef", ".sr2", ".x3f"]
JPEG_EXTENSIONS = [".jpg", ".jpeg", ".png", ".tif", ".tiff"]

DATABASE_NAME = "kestrel_database.csv"
METADATA_FILENAME = "kestrel_metadata.json"
KESTREL_DIR_NAME = ".kestrel"
LOG_FILENAME_PREFIX = "kestrel_error"
LOG_FILE_EXTENSION = "json"
