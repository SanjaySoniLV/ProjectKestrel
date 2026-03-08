import json
import os
from datetime import datetime

import pandas as pd

from .config import DATABASE_NAME, METADATA_FILENAME, VERSION
from .logging_utils import log_warning

BASE_COLUMNS = [
    "filename",
    "species",
    "species_confidence",
    "family",
    "family_confidence",
    "quality",
    "export_path",
    "crop_path",
    "rating",
    "scene_count",
    "feature_similarity",
    "feature_confidence",
    "color_similarity",
    "color_confidence",
    "similar",
    "secondary_species_list",
    "secondary_species_scores",
    "secondary_family_list",
    "secondary_family_scores",
    "detail_score",
    "sharpness_score",
    "camera_model",
]

REQUIRED_COLUMNS = [
    "family",
    "family_confidence",
    "secondary_family_list",
    "secondary_family_scores",
    "detail_score",
    "sharpness_score",
    "camera_model",
]


def load_database(kestrel_dir: str, analyzer_name: str, log_path: str = None):
    db_path = os.path.join(kestrel_dir, DATABASE_NAME)
    metadata_path = os.path.join(kestrel_dir, METADATA_FILENAME)

    if os.path.exists(db_path):
        database = pd.read_csv(db_path)
    else:
        database = pd.DataFrame(columns=BASE_COLUMNS)
        try:
            if not os.path.exists(metadata_path):
                metadata = {
                    "version": VERSION,
                    "analyzer": analyzer_name,
                    "created_utc": datetime.utcnow().isoformat() + "Z",
                    "database_file": DATABASE_NAME,
                }
                with open(metadata_path, "w", encoding="utf-8") as mf:
                    json.dump(metadata, mf, indent=2)
        except Exception as e:
            if log_path:
                log_warning(
                    log_path,
                    f"Failed to write metadata file: {e}",
                    category=type(e),
                    stage="metadata_write",
                    context={"metadata_path": metadata_path},
                )
            else:
                print(f"Warning: failed to write metadata file: {e}")

    database = ensure_columns(database)
    return database, db_path


def ensure_columns(database: pd.DataFrame) -> pd.DataFrame:
    for col in REQUIRED_COLUMNS:
        if col not in database.columns:
            if col.endswith("_list"):
                database[col] = [[] for _ in range(len(database))]
            elif col.endswith("_scores"):
                database[col] = [[] for _ in range(len(database))]
            elif col == "camera_model":
                database[col] = ""
            elif "family" in col and not col.endswith("_score"):
                database[col] = "Unknown" if "confidence" not in col else 0.0
            else:
                database[col] = -1.0
    return database


def save_database(database: pd.DataFrame, db_path: str) -> None:
    database.to_csv(db_path, index=False)
