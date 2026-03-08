import logging
import os
import warnings
from typing import Callable, Dict, Optional

import cv2
import numpy as np
import pandas as pd

from .config import (
    JPEG_EXTENSIONS,
    RAW_EXTENSIONS,
    SPECIESCLASSIFIER_LABELS,
    SPECIESCLASSIFIER_PATH,
    QUALITYCLASSIFIER_PATH,
    WILDLIFE_CATEGORIES,
    MODELS_DIR,
    KESTREL_DIR_NAME,
)
from .database import load_database, save_database
from .image_utils import read_image, extract_exif_metadata, normalise_exposure
from .ratings import quality_to_rating
from .similarity import compute_image_similarity_akaze
from .detail_analysis import analyse_detail
from .metadata_writer import write_xmp_sidecar
from .gpu_utils import get_gpu_summary
from .logging_utils import get_log_path, log_event, log_exception, log_warning

from .ml.mask_rcnn import MaskRCNNWrapper
from .ml.bird_species import BirdSpeciesClassifier
from .ml.quality import QualityClassifier

logger = logging.getLogger(__name__)


class AnalysisPipeline:
    def __init__(self, use_gpu: bool):
        self.use_gpu = use_gpu
        self.mask_rcnn: Optional[MaskRCNNWrapper] = None
        self.species_clf: Optional[BirdSpeciesClassifier] = None
        self.quality_clf: Optional[QualityClassifier] = None
        self._log_path: Optional[str] = None

    @staticmethod
    def _create_mask_overlay(
        thumbnail: np.ndarray,
        masks: Optional[np.ndarray],
        indices: Optional[list],
        color=(255, 64, 64),
        alpha: float = 0.45,
    ) -> Optional[np.ndarray]:
        if thumbnail is None:
            return None
        overlay = thumbnail.copy()
        if masks is None or not indices:
            return overlay
        h, w = overlay.shape[:2]
        for i in indices:
            mask = masks[i].astype(np.uint8)
            mask_small = cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)
            mask_bool = mask_small.astype(bool)
            if not np.any(mask_bool):
                continue
            overlay[mask_bool] = (
                overlay[mask_bool] * (1.0 - alpha) + np.array(color, dtype=np.uint8) * alpha
            ).astype(np.uint8)
        return overlay

    def load_models(self, status_cb: Optional[Callable[[str], None]] = None) -> None:
        if self.mask_rcnn and self.species_clf and self.quality_clf:
            return

        gpu_info = get_gpu_summary(self.use_gpu)
        if status_cb:
            status_cb(f"Loading models ({gpu_info})...")

        self.mask_rcnn = MaskRCNNWrapper(use_gpu=self.use_gpu)
        self.species_clf = BirdSpeciesClassifier(
            str(SPECIESCLASSIFIER_PATH),
            str(SPECIESCLASSIFIER_LABELS),
            self.use_gpu,
            models_dir=str(MODELS_DIR),
        )
        self.quality_clf = QualityClassifier(str(QUALITYCLASSIFIER_PATH), use_gpu=self.use_gpu)
        if status_cb:
            status_cb(f"Models loaded ({gpu_info}). Processing started.")

    def process_folder(
        self,
        folder: str,
        pause_event=None,
        cancel_event=None,
        callbacks: Optional[Dict[str, Callable]] = None,
        analyzer_name: str = "pipeline",
    ) -> None:
        callbacks = callbacks or {}
        status_cb = callbacks.get("on_status")
        progress_cb = callbacks.get("on_progress")
        image_cb = callbacks.get("on_image")
        thumbnail_cb = callbacks.get("on_thumbnail")
        detection_cb = callbacks.get("on_detection")
        crops_cb = callbacks.get("on_crops")
        quality_cb = callbacks.get("on_quality")
        species_cb = callbacks.get("on_species")
        error_cb = callbacks.get("on_error")

        self._log_path = get_log_path(folder)
        stage_ctx = {"stage": "startup", "file": None}

        original_showwarning = warnings.showwarning

        def _showwarning(message, category, filename, lineno, file=None, line=None):
            log_warning(
                self._log_path,
                message,
                category=category,
                filename=filename,
                lineno=lineno,
                stage=stage_ctx["stage"],
                context={"file": stage_ctx["file"], "folder": folder},
            )
            if original_showwarning:
                original_showwarning(message, category, filename, lineno, file=file, line=line)

        warnings.showwarning = _showwarning

        try:
            stage_ctx["stage"] = "list_files"
            files = [
                f
                for f in os.listdir(folder)
                if os.path.isfile(os.path.join(folder, f))
                and os.path.splitext(f)[1].lower() in RAW_EXTENSIONS
            ]
            if not files:
                files = [
                    f
                    for f in os.listdir(folder)
                    if os.path.isfile(os.path.join(folder, f))
                    and os.path.splitext(f)[1].lower() in JPEG_EXTENSIONS
                ]
            files.sort()
            if not files:
                if status_cb:
                    status_cb("No supported image files found.")
                log_event(
                    self._log_path,
                    {
                        "level": "warning",
                        "event": "no_supported_files",
                        "analyzer": analyzer_name,
                        "folder": folder,
                    },
                )
                return

            log_event(
                self._log_path,
                {
                    "level": "info",
                    "event": "analysis_start",
                    "analyzer": analyzer_name,
                    "folder": folder,
                    "file_count": len(files),
                    "gpu": get_gpu_summary(self.use_gpu),
                },
            )

            stage_ctx["stage"] = "create_kestrel_dirs"
            kestrel_dir = os.path.join(folder, KESTREL_DIR_NAME)
            export_dir = os.path.join(kestrel_dir, "export")
            crop_dir = os.path.join(kestrel_dir, "crop")
            os.makedirs(export_dir, exist_ok=True)
            os.makedirs(crop_dir, exist_ok=True)

            stage_ctx["stage"] = "load_database"
            database, db_path = load_database(kestrel_dir, analyzer_name, log_path=self._log_path)

            processed_set = set(database["filename"].values)
            new_files = [f for f in files if f not in processed_set]
            processed_count = len(files) - len(new_files)
            total = len(files)
            if progress_cb:
                progress_cb(processed_count, total)
            if processed_count > 0 and status_cb:
                status_cb("Picking up where Kestrel left off...")
            if not new_files:
                if status_cb:
                    status_cb("No new files to process.")
                if progress_cb:
                    progress_cb(total, total)
                return

            stage_ctx["stage"] = "load_models"
            self.load_models(status_cb=status_cb)

            previous_image = None
            if not database.empty:
                last_row = database.iloc[-1]
                last_filename = last_row["filename"]
                last_image_path = os.path.join(folder, last_filename)
                if os.path.exists(last_image_path):
                    img = read_image(last_image_path)
                    if img is not None:
                        previous_image = img
            scene_count = database["scene_count"].max() if not database.empty else 0

            for idx, raw_file in enumerate(new_files, start=1):
                if pause_event is not None:
                    while not pause_event.is_set():
                        if cancel_event is not None and cancel_event.is_set():
                            if status_cb:
                                status_cb('Cancelled')
                            return
                        pause_event.wait(timeout=0.5)
                if cancel_event is not None and cancel_event.is_set():
                    if status_cb:
                        status_cb('Cancelled')
                    return

                entry = {
                    "filename": raw_file,
                    "species": "Unknown",
                    "species_confidence": 0.0,
                    "family": "Unknown",
                    "family_confidence": 0.0,
                    "quality": -1.0,
                    "export_path": "N/A",
                    "crop_path": "N/A",
                    "scene_count": scene_count,
                    "rating": 0,
                    "feature_similarity": -1.0,
                    "feature_confidence": -1.0,
                    "color_similarity": -1.0,
                    "color_confidence": -1.0,
                    "similar": False,
                    "secondary_species_list": [],
                    "secondary_species_scores": [],
                    "secondary_family_list": [],
                    "secondary_family_scores": [],
                    "detail_score": -1.0,
                    "sharpness_score": -1.0,
                    "camera_model": "",
                }

                image_path = None
                try:
                    stage_ctx["stage"] = "read_image"
                    stage_ctx["file"] = raw_file
                    image_path = os.path.join(folder, raw_file)
                    img = read_image(image_path)
                    if img is None:
                        raise RuntimeError("Image read returned None")

                    # Extract EXIF metadata (camera model, lens, ISO, etc.)
                    stage_ctx["stage"] = "extract_metadata"
                    exif_meta = extract_exif_metadata(image_path)
                    entry["camera_model"] = exif_meta.camera_model

                    stage_ctx["stage"] = "compute_similarity"
                    similarity = compute_image_similarity_akaze(previous_image, img)
                    if not similarity["similar"]:
                        scene_count += 1
                    entry.update(
                        {
                            "feature_similarity": similarity["feature_similarity"],
                            "feature_confidence": similarity["feature_confidence"],
                            "color_similarity": similarity["color_similarity"],
                            "color_confidence": similarity["color_confidence"],
                            "scene_count": scene_count,
                            "similar": similarity["similar"],
                        }
                    )
                    previous_image = img.copy()

                    stage_ctx["stage"] = "export_image"
                    export_path = os.path.join(export_dir, f"{os.path.splitext(raw_file)[0]}_export.jpg")
                    img_small = cv2.resize(img, (1200, int(1200 * img.shape[0] / img.shape[1])))
                    cv2.imwrite(
                        export_path,
                        cv2.cvtColor(img_small, cv2.COLOR_RGB2BGR),
                        [cv2.IMWRITE_JPEG_QUALITY, 70],
                    )
                    export_path_rel = os.path.relpath(export_path, folder)
                    entry.update({"export_path": export_path_rel})
                    if thumbnail_cb:
                        thumbnail_cb({"filename": raw_file, "thumbnail": img_small, "export_path": export_path_rel})

                    stage_ctx["stage"] = "mask_rcnn_prediction"
                    masks, pred_boxes, pred_class, pred_score = self.mask_rcnn.get_prediction(img)

                    # Filter overlapping detections to reduce false positives
                    if masks is not None and len(masks) > 0:
                        masks, pred_boxes, pred_class, pred_score = (
                            MaskRCNNWrapper.filter_overlapping_detections(
                                masks, pred_boxes, pred_class, pred_score
                            )
                        )

                    if masks is None or len(masks) == 0:
                        if detection_cb:
                            detection_cb(
                                {
                                    "filename": raw_file,
                                    "overlay": self._create_mask_overlay(img_small, None, None),
                                    "bird_count": 0,
                                }
                            )
                        if crops_cb:
                            crops_cb({"filename": raw_file, "crops": [], "confidences": []})
                        if quality_cb:
                            quality_cb({"filename": raw_file, "results": []})
                        if species_cb:
                            species_cb({"filename": raw_file, "results": []})
                        if status_cb:
                            status_cb(f"No detections in {raw_file}")
                        stage_ctx["stage"] = "write_crop"
                        crop_path = os.path.join(crop_dir, f"{os.path.splitext(raw_file)[0]}_crop.jpg")
                        cv2.imwrite(
                            crop_path,
                            cv2.cvtColor(img_small, cv2.COLOR_RGB2BGR),
                            [cv2.IMWRITE_JPEG_QUALITY, 85],
                        )
                        crop_path_rel = os.path.relpath(crop_path, folder)
                        entry.update({"crop_path": crop_path_rel})
                        stage_ctx["stage"] = "save_database"
                        database = pd.concat([database, pd.DataFrame([entry])], ignore_index=True)
                        save_database(database, db_path)
                        # Write XMP sidecar (0 rating for no detection)
                        write_xmp_sidecar(
                            image_path,
                            rating=0,
                            camera_model=exif_meta.camera_model,
                        )
                        if image_cb:
                            image_cb(entry)
                        if progress_cb:
                            progress_cb(idx + processed_count, total)
                        continue

                    wildlife_indices = [i for i, c in enumerate(pred_class) if c in WILDLIFE_CATEGORIES]
                    bird_indices = [i for i, c in enumerate(pred_class) if c == "bird"]
                    bird_indices = sorted(bird_indices, key=lambda i: pred_score[i], reverse=True)[:5]

                    overlay_indices = bird_indices if bird_indices else wildlife_indices[:1]
                    if detection_cb:
                        detection_cb(
                            {
                                "filename": raw_file,
                                "overlay": self._create_mask_overlay(img_small, masks, overlay_indices),
                                "bird_count": len(bird_indices),
                            }
                        )

                    def process_nonbird(primary_mask_i):
                        stage_ctx["stage"] = "process_nonbird"
                        quality_crop, quality_mask = self.mask_rcnn.get_square_crop(
                            masks[primary_mask_i], img, resize=True
                        )
                        # Normalise exposure for backlit subjects
                        quality_crop_norm = normalise_exposure(quality_crop, quality_mask)
                        quality_score = self.quality_clf.classify(quality_crop_norm, quality_mask)

                        # Detail analysis
                        detail = analyse_detail(quality_crop, quality_mask)
                        rating = quality_to_rating(quality_score, detail.sharpness_score, detail.detail_score)
                        return {
                            "species": pred_class[primary_mask_i],
                            "species_confidence": float(pred_score[primary_mask_i]),
                            "family": "N/A",
                            "family_confidence": 0.0,
                            "quality": quality_score,
                            "rating": rating,
                            "quality_crop": quality_crop,
                            "detail_score": detail.detail_score,
                            "sharpness_score": detail.sharpness_score,
                        }

                    def process_bird_items(indices):
                        stage_ctx["stage"] = "process_bird"
                        items = []
                        for i in indices:
                            species_crop = self.mask_rcnn.get_species_crop(pred_boxes[i], img)
                            quality_crop, quality_mask = self.mask_rcnn.get_square_crop(masks[i], img, resize=True)
                            items.append(
                                {
                                    "index": i,
                                    "confidence": float(pred_score[i]),
                                    "species_crop": species_crop,
                                    "quality_crop": quality_crop,
                                    "quality_mask": quality_mask,
                                }
                            )
                        if crops_cb:
                            crops_cb(
                                {
                                    "filename": raw_file,
                                    "crops": [i["quality_crop"] for i in items],
                                    "confidences": [i["confidence"] for i in items],
                                }
                            )
                        for item in items:
                            i = item["index"]
                            if pred_class[i] == "bird":
                                species_result = self.species_clf.classify(item["species_crop"])
                                item["species"] = (
                                    species_result["top_species_labels"][0]
                                    if len(species_result["top_species_labels"])
                                    else "Unknown"
                                )
                                item["species_confidence"] = (
                                    float(species_result["top_species_scores"][0])
                                    if len(species_result["top_species_scores"])
                                    else 0.0
                                )
                                item["family"] = (
                                    species_result["top_family_labels"][0]
                                    if len(species_result["top_family_labels"])
                                    else "Unknown"
                                )
                                item["family_confidence"] = (
                                    float(species_result["top_family_scores"][0])
                                    if len(species_result["top_family_scores"])
                                    else 0.0
                                )
                            else:
                                item["species"] = pred_class[i]
                                item["species_confidence"] = float(pred_score[i])
                                item["family"] = "N/A"
                                item["family_confidence"] = 0.0

                            stage_ctx["stage"] = "quality_score"
                            # Normalise exposure for backlit subjects
                            crop_norm = normalise_exposure(item["quality_crop"], item["quality_mask"])
                            quality_score = self.quality_clf.classify(crop_norm, item["quality_mask"])
                            item["quality"] = quality_score

                            # Detail and sharpness analysis
                            stage_ctx["stage"] = "detail_analysis"
                            detail = analyse_detail(item["quality_crop"], item["quality_mask"])
                            item["detail_score"] = detail.detail_score
                            item["sharpness_score"] = detail.sharpness_score
                            item["rating"] = quality_to_rating(
                                quality_score, detail.sharpness_score, detail.detail_score
                            )

                        if quality_cb:
                            quality_cb(
                                {
                                    "filename": raw_file,
                                    "results": [
                                        {
                                            "quality": i["quality"],
                                            "rating": i["rating"],
                                            "detail_score": i["detail_score"],
                                            "sharpness_score": i["sharpness_score"],
                                        }
                                        for i in items
                                    ],
                                }
                            )
                        if species_cb:
                            species_cb(
                                {
                                    "filename": raw_file,
                                    "results": [
                                        {
                                            "species": i["species"],
                                            "species_confidence": i["species_confidence"],
                                            "family": i["family"],
                                            "family_confidence": i["family_confidence"],
                                        }
                                        for i in items
                                    ],
                                }
                            )
                        return items

                    if bird_indices:
                        bird_items = process_bird_items(bird_indices)
                        bird_data = [
                            {
                                "species": i["species"],
                                "species_confidence": i["species_confidence"],
                                "family": i["family"],
                                "family_confidence": i["family_confidence"],
                                "quality": i["quality"],
                                "rating": i["rating"],
                                "quality_crop": i["quality_crop"],
                                "detail_score": i["detail_score"],
                                "sharpness_score": i["sharpness_score"],
                            }
                            for i in bird_items
                        ]
                        # Use highest-confidence detection as primary (not highest quality)
                        # to avoid false positives from background objects scoring higher
                        primary_bird = max(bird_data, key=lambda x: x["species_confidence"])
                        entry.update(
                            {
                                "species": primary_bird["species"],
                                "species_confidence": primary_bird["species_confidence"],
                                "family": primary_bird["family"],
                                "family_confidence": primary_bird["family_confidence"],
                                "quality": primary_bird["quality"],
                                "rating": primary_bird["rating"],
                                "detail_score": primary_bird["detail_score"],
                                "sharpness_score": primary_bird["sharpness_score"],
                            }
                        )
                        all_species = np.array([b["species"] for b in bird_data])
                        all_species_conf = np.array([b["species_confidence"] for b in bird_data])
                        all_families = np.array([b["family"] for b in bird_data])
                        all_family_conf = np.array([b["family_confidence"] for b in bird_data])
                        entry.update(
                            {
                                "secondary_species_list": all_species,
                                "secondary_species_scores": all_species_conf,
                                "secondary_family_list": all_families,
                                "secondary_family_scores": all_family_conf,
                            }
                        )
                        crop_img = primary_bird["quality_crop"]
                    else:
                        if wildlife_indices:
                            primary_index = wildlife_indices[np.argmax([pred_score[i] for i in wildlife_indices])]
                            result = process_nonbird(primary_index)
                            if crops_cb:
                                crops_cb(
                                    {
                                        "filename": raw_file,
                                        "crops": [result["quality_crop"]],
                                        "confidences": [float(pred_score[primary_index])],
                                    }
                                )
                            if quality_cb:
                                quality_cb(
                                    {
                                        "filename": raw_file,
                                        "results": [{
                                            "quality": result["quality"],
                                            "rating": result["rating"],
                                            "detail_score": result["detail_score"],
                                            "sharpness_score": result["sharpness_score"],
                                        }],
                                    }
                                )
                            if species_cb:
                                species_cb(
                                    {
                                        "filename": raw_file,
                                        "results": [
                                            {
                                                "species": result["species"],
                                                "species_confidence": result["species_confidence"],
                                                "family": result["family"],
                                                "family_confidence": result["family_confidence"],
                                            }
                                        ],
                                    }
                                )
                            entry.update(
                                {
                                    "species": result["species"],
                                    "species_confidence": result["species_confidence"],
                                    "family": result["family"],
                                    "family_confidence": result["family_confidence"],
                                    "quality": result["quality"],
                                    "rating": result["rating"],
                                    "detail_score": result["detail_score"],
                                    "sharpness_score": result["sharpness_score"],
                                }
                            )
                            crop_img = result["quality_crop"]
                        else:
                            if crops_cb:
                                crops_cb({"filename": raw_file, "crops": [], "confidences": []})
                            if quality_cb:
                                quality_cb({"filename": raw_file, "results": []})
                            if species_cb:
                                species_cb({"filename": raw_file, "results": []})
                            crop_img = img_small

                    stage_ctx["stage"] = "write_crop"
                    crop_path = os.path.join(crop_dir, f"{os.path.splitext(raw_file)[0]}_crop.jpg")
                    cv2.imwrite(
                        crop_path,
                        cv2.cvtColor(crop_img, cv2.COLOR_RGB2BGR),
                        [cv2.IMWRITE_JPEG_QUALITY, 85],
                    )
                    crop_path_rel = os.path.relpath(crop_path, folder)
                    entry.update({"crop_path": crop_path_rel})

                    # Write XMP sidecar file for Lightroom
                    stage_ctx["stage"] = "write_xmp"
                    write_xmp_sidecar(
                        image_path,
                        rating=entry["rating"],
                        species=entry["species"],
                        family=entry["family"],
                        quality_score=entry["quality"],
                        detail_score=entry.get("detail_score", -1.0),
                        sharpness_score=entry.get("sharpness_score", -1.0),
                        camera_model=exif_meta.camera_model,
                    )

                    stage_ctx["stage"] = "save_database"
                    database = pd.concat([database, pd.DataFrame([entry])], ignore_index=True)
                    save_database(database, db_path)

                    if image_cb:
                        image_cb(entry)

                    if status_cb:
                        detail_info = ""
                        if entry.get("detail_score", -1) >= 0:
                            detail_info = f" D={entry['detail_score']:.3f} S={entry['sharpness_score']:.3f}"
                        status_cb(
                            f"Processed {raw_file}: {entry['species']} Q={entry['quality']:.3f} "
                            f"R={entry['rating']}{detail_info} ({idx + processed_count}/{total})"
                        )
                except Exception as e:
                    log_exception(
                        self._log_path,
                        e,
                        stage=stage_ctx["stage"],
                        context={
                            "file": raw_file,
                            "folder": folder,
                            "image_path": image_path,
                            "analyzer": analyzer_name,
                        },
                    )
                    if error_cb:
                        error_cb(raw_file, e)
                    if status_cb:
                        status_cb(f"Error {raw_file}: {e}")
                    entry["scene_count"] = scene_count
                    entry["species"] = "Error"
                    entry["similar"] = False
                    database = pd.concat([database, pd.DataFrame([entry])], ignore_index=True)
                    save_database(database, db_path)

                if progress_cb:
                    progress_cb(idx + processed_count, total)

        except Exception as e:
            log_exception(
                self._log_path,
                e,
                stage=stage_ctx["stage"],
                context={"folder": folder, "analyzer": analyzer_name},
            )
            if status_cb:
                status_cb(f"Fatal error: {e}")
            if error_cb:
                error_cb("fatal", e)
        finally:
            warnings.showwarning = original_showwarning
