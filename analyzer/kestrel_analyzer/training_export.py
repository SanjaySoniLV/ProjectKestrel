"""Per-crop training-data export for quality-model retraining.

Opt-in via the analyzer CLI's --export-training-data flag. Writes, for each
real detection, the post-Sobel input tensor (.npy), the 1024x1024 RGB crop
(.jpg), and the 1024x1024 binary mask (.png), plus one row in manifest.csv.

The .npy is computed by re-calling QualityClassifier._preprocess so the
training data is, by construction, bit-identical to what the production
quality model sees at inference time.
"""

import csv
import os
import threading
from typing import Any, Mapping, Optional

import cv2
import numpy as np

from .ml.quality import QualityClassifier


MANIFEST_FILENAME = "manifest.csv"

MANIFEST_COLUMNS = [
    "crop_id",
    "source_folder",
    "source_filename",
    "crop_index",
    "detection_index",
    "detection_confidence",
    "species",
    "species_confidence",
    "family",
    "family_confidence",
    "quality_legacy_model",
    "rating_legacy_model",
    "exposure_correction",
    "exposure_pipeline",
    "exposure_subject_stops",
    "exposure_meter_scale",
    "bbox_x_min",
    "bbox_x_max",
    "bbox_y_min",
    "bbox_y_max",
    "capture_time",
    "orientation",
    "input_npy_path",
    "rgb_jpg_path",
    "mask_png_path",
]

# Higher than the production crop quality (75) — training data should not be
# polluted by JPEG compression artifacts that the model would then learn.
_RGB_JPEG_QUALITY = 95

_MANIFEST_LOCK = threading.Lock()


def make_crop_id(source_folder: str, source_filename: str, crop_index: int) -> str:
    folder_basename = os.path.basename(os.path.normpath(source_folder)) or "root"
    stem = os.path.splitext(source_filename)[0]
    return f"{folder_basename}__{stem}_crop_{int(crop_index)}"


def write_crop_artifacts(
    out_dir: str,
    crop_id: str,
    quality_crop_rgb: np.ndarray,
    quality_mask: np.ndarray,
) -> dict:
    """Write the three per-crop files. Returns dict of relative paths."""
    os.makedirs(out_dir, exist_ok=True)

    npy_name = f"{crop_id}_input.npy"
    jpg_name = f"{crop_id}_rgb.jpg"
    png_name = f"{crop_id}_mask.png"

    preprocessed = QualityClassifier._preprocess(quality_crop_rgb, quality_mask)
    np.save(os.path.join(out_dir, npy_name), preprocessed.astype(np.float32))

    bgr = cv2.cvtColor(quality_crop_rgb, cv2.COLOR_RGB2BGR)
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), _RGB_JPEG_QUALITY])
    if not ok:
        raise RuntimeError(f"cv2.imencode failed for {jpg_name}")
    buf.tofile(os.path.join(out_dir, jpg_name))

    mask_u8 = (quality_mask.astype(np.uint8) > 0).astype(np.uint8) * 255
    ok, buf = cv2.imencode(".png", mask_u8)
    if not ok:
        raise RuntimeError(f"cv2.imencode failed for {png_name}")
    buf.tofile(os.path.join(out_dir, png_name))

    return {"input_npy_path": npy_name, "rgb_jpg_path": jpg_name, "mask_png_path": png_name}


def append_manifest_rows(out_dir: str, rows: list) -> None:
    """Append rows to manifest.csv, creating the file with header if needed."""
    if not rows:
        return
    manifest_path = os.path.join(out_dir, MANIFEST_FILENAME)
    with _MANIFEST_LOCK:
        is_new = not os.path.exists(manifest_path)
        with open(manifest_path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS, extrasaction="ignore")
            if is_new:
                writer.writeheader()
            for row in rows:
                writer.writerow(row)


def build_manifest_row(
    *,
    crop_id: str,
    source_folder: str,
    source_filename: str,
    crop_index: int,
    crop_item: Mapping[str, Any],
    serialized_crop: Mapping[str, Any],
    capture_time: str,
    orientation: str,
    artifact_paths: Mapping[str, str],
) -> dict:
    bbox = serialized_crop.get("bbox") or {}
    return {
        "crop_id": crop_id,
        "source_folder": os.path.basename(os.path.normpath(source_folder)) or "root",
        "source_filename": source_filename,
        "crop_index": int(crop_index),
        "detection_index": int(crop_item.get("index", -1)),
        "detection_confidence": float(crop_item.get("confidence", 0.0)),
        "species": str(crop_item.get("species", "Unknown") or "Unknown"),
        "species_confidence": float(crop_item.get("species_confidence", 0.0)),
        "family": str(crop_item.get("family", "Unknown") or "Unknown"),
        "family_confidence": float(crop_item.get("family_confidence", 0.0)),
        "quality_legacy_model": float(crop_item.get("quality", -1.0)),
        "rating_legacy_model": int(crop_item.get("rating", 0)),
        "exposure_correction": float(crop_item.get("exposure_correction", 0.0)),
        "exposure_pipeline": str(crop_item.get("exposure_pipeline", "") or ""),
        "exposure_subject_stops": float(crop_item.get("exposure_subject_stops", 0.0)),
        "exposure_meter_scale": float(crop_item.get("exposure_meter_scale", 1.0)),
        "bbox_x_min": int(bbox.get("x_min", 0)),
        "bbox_x_max": int(bbox.get("x_max", 0)),
        "bbox_y_min": int(bbox.get("y_min", 0)),
        "bbox_y_max": int(bbox.get("y_max", 0)),
        "capture_time": str(capture_time or ""),
        "orientation": str(orientation or ""),
        "input_npy_path": artifact_paths["input_npy_path"],
        "rgb_jpg_path": artifact_paths["rgb_jpg_path"],
        "mask_png_path": artifact_paths["mask_png_path"],
    }
