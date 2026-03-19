import json
import os
import time
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
    QUALITY_NORMALIZATION_DATA_PATH,
    WILDLIFE_CATEGORIES,
    MODELS_DIR,
    KESTREL_DIR_NAME,
    METADATA_FILENAME,
    VERSION,
)
from .database import (
    load_database,
    save_database,
    load_scenedata,
    save_scenedata,
    build_scenedata_from_database,
    update_scenedata_with_database,
)
print("Importing read_image from image_utils...")
from .image_utils import read_image, read_image_for_pipeline
print("read_image imported successfully.")
from .ratings import quality_to_rating, get_profile_thresholds
print("Importing compute_image_similarity_akaze from similarity...")
from .similarity import compute_image_similarity_akaze, compute_similarity_timestamp
from .raw_exif import get_capture_time
from .logging_utils import get_log_path, log_event, log_exception, log_warning

try:
    from ..settings_utils import load_persisted_settings
except ImportError:
    try:
        from analyzer.settings_utils import load_persisted_settings
    except ImportError:
        load_persisted_settings = None

print("Utility functions imported successfully.")

print("Importing ML models... Starting with MaskRCNNWrapper...")
from .ml.mask_rcnn import MaskRCNNWrapper
print("MaskRCNNWrapper imported successfully. Now importing BirdSpeciesClassifier...")
from .ml.bird_species import BirdSpeciesClassifier
print("BirdSpeciesClassifier imported successfully. Now importing QualityClassifier...")
from .ml.quality import QualityClassifier
print("QualityClassifier imported successfully. All ML models imported.")


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

    @staticmethod
    def _compute_exposure_stops(img: np.ndarray, mask: np.ndarray) -> float:
        """Estimate exposure correction in stops for the masked subject region.

        This is histogram-aware (percentile based), not mean-only. It balances
        two goals:
        1) center subject mid-tones toward a neutral target, and
        2) protect highlight/shadow detail in mixed-contrast scenes.

        Returns a value in [-1.75, +2.5] where positive lifts exposure and
        negative pulls it down.
        """
        EPS = 1e-3        
        #TARGET_MID = 0.42
        #TARGET_HI_P90 = 0.90
        #TARGET_HI_P98 = 0.975
        TARGET_MID = 0.48
        TARGET_HI_P90 = 0.92
        TARGET_HI_P98 = 0.985
        TARGET_HI_P95_HOT = 0.88
        SHADOW_FLOOR_P10 = 0.06
        CLIP_THRESH = 0.985
        HOT_CLIP_RATIO = 0.02
        MAX_DARKEN = -1.75
        MAX_BRIGHTEN = 2.5
        try:
            img_f = img.astype(np.float32) / 255.0
            mask_bool = mask.astype(bool) if mask is not None else None
            if (
                mask_bool is not None
                and mask_bool.any()
                and img_f.ndim == 3
                and img_f.shape[:2] == mask_bool.shape
            ):
                pixels = img_f[mask_bool]
            else:
                pixels = img_f.reshape(-1, 3)
            # Perceptual luminance weights (Rec. 709)
            lum = 0.2126 * pixels[:, 0] + 0.7152 * pixels[:, 1] + 0.0722 * pixels[:, 2]
            lum = lum[np.isfinite(lum)]
            if lum.size == 0:
                return 0.0
            p10, p50, p90, p95, p98 = np.percentile(lum, [10, 50, 90, 95, 98])
            clip_ratio = float(np.mean(lum >= CLIP_THRESH))

            # Midtone intent (robust against small bright outliers).
            mid_stop = float(np.log2(TARGET_MID / max(float(p50), EPS)))

            # Highlight ceilings prevent over-bright outputs.
            hi90_stop = float(np.log2(TARGET_HI_P90 / max(float(p90), EPS)))
            hi98_stop = float(np.log2(TARGET_HI_P98 / max(float(p98), EPS)))
            highlight_ceiling = min(hi90_stop, hi98_stop, MAX_BRIGHTEN)

            # Shadow floor prevents excessive darkening in high dynamic-range masks.
            shadow_floor = max(MAX_DARKEN, float(np.log2(SHADOW_FLOOR_P10 / max(float(p10), EPS))))

            # Start from midtone intent, then clamp by highlight/shadow guardrails.
            stops = float(np.clip(mid_stop, shadow_floor, highlight_ceiling))

            # If a meaningful chunk is clipped/highlight-hot, bias a little darker
            # to recover structure in bright regions.
            if clip_ratio >= HOT_CLIP_RATIO:
                hot_stop = float(np.log2(TARGET_HI_P95_HOT / max(float(p95), EPS)))
                stops = min(stops, hot_stop)
                stops = float(np.clip(stops, MAX_DARKEN, MAX_BRIGHTEN))

            if not np.isfinite(stops):
                return 0.0
            return stops
        except Exception:
            return 0.0

    @staticmethod
    def _apply_exposure_correction(
        img: np.ndarray, stops: float, raw_obj=None
    ) -> np.ndarray:
        """Return a copy of *img* with exposure shifted by *stops* stops.

        When *raw_obj* is a live rawpy.RawPy instance (RAW files), the
        correction is applied via rawpy.postprocess(exp_shift=…) in the linear
        domain before demosaicing, which correctly recovers highlight detail
        on overexposed subjects rather than clipping.

        For non-RAW sources (JPEG/PNG, or if rawpy re-process fails) the
        method falls back to pixel-level multiplication.

        Positive stops lifts (brightens), negative stops pulls (darkens).
        Returns the original array unchanged when stops == 0.0.
        """
        if stops == 0.0:
            return img
        if raw_obj is not None:
            try:
                # exp_shift is in linear scale: 2^stops.
                # rawpy usable range: 0.25 (−2 stops) … 8.0 (+3 stops).
                linear_scale = float(np.clip(2.0 ** stops, 0.25, 8.0))
                # Preserve highlights when brightening to avoid blowing out
                # any remaining highlight detail.
                preserve = 0.8 if stops > 0 else 0.0
                return raw_obj.postprocess(
                    no_auto_bright=True,
                    exp_shift=linear_scale,
                    exp_preserve_highlights=preserve,
                )
            except Exception:
                pass  # fall through to pixel-level fallback below
        factor = 2.0 ** stops
        return (img.astype(np.float32) * factor).clip(0.0, 255.0).astype(np.uint8)

    @staticmethod
    def _get_image_orientation(img: np.ndarray) -> str:
        if img is None or img.ndim < 2:
            return "unknown"
        h, w = img.shape[:2]
        if h > w:
            return "portrait"
        if w > h:
            return "landscape"
        return "square"

    def load_models(self, status_cb: Optional[Callable[[str], None]] = None) -> None:
        if self.mask_rcnn and self.species_clf and self.quality_clf:
            return
        if status_cb:
            status_cb("Loading models... This may take a while on first run.")
        self.mask_rcnn = MaskRCNNWrapper()
        self.species_clf = BirdSpeciesClassifier(
            str(SPECIESCLASSIFIER_PATH),
            str(SPECIESCLASSIFIER_LABELS),
            self.use_gpu,
            models_dir=str(MODELS_DIR),
        )
        self.quality_clf = QualityClassifier(
            str(QUALITYCLASSIFIER_PATH),
            normalization_data_path=str(QUALITY_NORMALIZATION_DATA_PATH),
        )
        if status_cb:
            status_cb("Models loaded. Processing started.")

    def process_folder(
        self,
        folder: str,
        pause_event=None,
        cancel_event=None,
        callbacks: Optional[Dict[str, Callable]] = None,
        analyzer_name: str = "pipeline",
        wildlife_enabled: bool = True,
        detection_threshold: float = 0.75,
        scene_time_threshold: float = 1.0,
        mask_threshold: float = 0.5,
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

        rating_thresholds = None
        if callable(load_persisted_settings):
            try:
                sett = load_persisted_settings() or {}
                profile = sett.get('rating_profile', 'balanced')
                rating_thresholds = get_profile_thresholds(profile)
            except Exception:
                rating_thresholds = None

        active_wildlife_categories = WILDLIFE_CATEGORIES if wildlife_enabled else []

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
            raw_ext_set = {e.lower() for e in RAW_EXTENSIONS}
            jpeg_ext_set = {e.lower() for e in JPEG_EXTENSIONS}
            all_folder_files = [
                f
                for f in os.listdir(folder)
                if os.path.isfile(os.path.join(folder, f))
            ]
            raw_files = [f for f in all_folder_files if os.path.splitext(f)[1].lower() in raw_ext_set]
            jpeg_files = [f for f in all_folder_files if os.path.splitext(f)[1].lower() in jpeg_ext_set]
            if raw_files:
                # Include RAW files plus JPEG files that have no corresponding RAW counterpart.
                # This ensures JPEG-only shots in a mixed RAW+JPEG folder are not silently
                # dropped from pipeline analysis (the folder itself would not be greyed out —
                # the RAW files ensure a non-zero count — but standalone JPEG shots would be
                # missed without this logic).
                raw_bases = {os.path.splitext(f)[0].lower() for f in raw_files}
                jpeg_only = [f for f in jpeg_files if os.path.splitext(f)[0].lower() not in raw_bases]
                files = raw_files + jpeg_only
            else:
                files = jpeg_files
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
            previous_image_path = None
            previous_orientation = None
            if not database.empty:
                last_row = database.iloc[-1]
                last_filename = last_row["filename"]
                last_image_path = os.path.join(folder, last_filename)
                if os.path.exists(last_image_path):
                    img = read_image(last_image_path)
                    if img is not None:
                        previous_image = img
                        previous_image_path = last_image_path
                        previous_orientation = self._get_image_orientation(img)
            scene_count = database["scene_count"].max() if not database.empty else 0

            for idx, raw_file in enumerate(new_files, start=1):
                # Pause: wait until resume or until cancel_event is set.
                if pause_event is not None:
                    while not pause_event.is_set():
                        if cancel_event is not None and cancel_event.is_set():
                            if status_cb:
                                status_cb('Cancelled')
                            return
                        # Wait with timeout to be interruptible
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
                    "feature_similarity": -1.0,
                    "feature_confidence": -1.0,
                    "color_similarity": -1.0,
                    "color_confidence": -1.0,
                    "similar": False,
                    "secondary_species_list": [],
                    "secondary_species_scores": [],
                    "secondary_family_list": [],
                    "secondary_family_scores": [],
                    "exposure_correction": 0.0,
                    "detection_scores": [],
                    "capture_time": "",
                    "orientation": "unknown",
                }

                image_path = None
                raw_obj = None
                try:
                    stage_ctx["stage"] = "read_image"
                    stage_ctx["file"] = raw_file
                    image_path = os.path.join(folder, raw_file)
                    img, raw_obj = read_image_for_pipeline(image_path)
                    if img is None:
                        raise RuntimeError("Image read returned None")

                    current_orientation = self._get_image_orientation(img)
                    entry["orientation"] = current_orientation

                    try:
                        ct = get_capture_time(image_path)
                        entry["capture_time"] = ct.isoformat() if ct is not None else ""
                    except Exception:
                        pass

                    stage_ctx["stage"] = "compute_similarity"
                    timestamp_similar = None
                    try:
                        timestamp_similar = compute_similarity_timestamp(
                            previous_image_path, image_path,
                            threshold_seconds=scene_time_threshold
                        ) if previous_image_path else None
                    except Exception as e:
                        log_warning(
                            self._log_path,
                            f"Timestamp similarity check failed: {e}",
                            stage=stage_ctx["stage"],
                            context={"file": raw_file, "folder": folder},
                        )

                    orientation_changed = (
                        previous_orientation is not None
                        and current_orientation != "unknown"
                        and previous_orientation != "unknown"
                        and current_orientation != previous_orientation
                    )

                    if orientation_changed:
                        scene_count += 1
                        entry.update(
                            {
                                "feature_similarity": -1.0,
                                "feature_confidence": -1.0,
                                "color_similarity": -1.0,
                                "color_confidence": -1.0,
                                "scene_count": scene_count,
                                "similar": False,
                            }
                        )
                    elif timestamp_similar is True:
                        # Images captured within the same second — treat as similar, skip AKAZE
                        entry.update(
                            {
                                "feature_similarity": -1.0,
                                "feature_confidence": -1.0,
                                "color_similarity": -1.0,
                                "color_confidence": -1.0,
                                "scene_count": scene_count,
                                "similar": True,
                            }
                        )
                    else:
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
                    previous_image_path = image_path
                    previous_orientation = current_orientation

                    stage_ctx["stage"] = "export_image"
                    export_path = os.path.join(export_dir, f"{os.path.splitext(raw_file)[0]}_export.jpg")
                    img_small = cv2.resize(img, (1200, int(1200 * img.shape[0] / img.shape[1])))
                    cv2.imwrite(
                        export_path,
                        cv2.cvtColor(img_small, cv2.COLOR_RGB2BGR),
                        [cv2.IMWRITE_JPEG_QUALITY, 70],
                    )
                    # Store relative path for cross-platform compatibility
                    export_path_rel = os.path.relpath(export_path, folder)
                    entry.update({"export_path": export_path_rel})
                    if thumbnail_cb:
                        thumbnail_cb({"filename": raw_file, "thumbnail": img_small, "export_path": export_path_rel})

                    stage_ctx["stage"] = "mask_rcnn_prediction"
                    # MaskRCNN inference can take many seconds. Pause semantics are
                    # handled at the start of each image loop so we do not check
                    # repeatedly inside the image processing path.
                    masks, pred_boxes, pred_class, pred_score = self.mask_rcnn.get_prediction(img, threshold=detection_threshold, mask_threshold=mask_threshold)
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
                        # Store relative path for cross-platform compatibility
                        crop_path_rel = os.path.relpath(crop_path, folder)
                        entry.update({"crop_path": crop_path_rel})
                        stage_ctx["stage"] = "save_database"
                        database = pd.concat([database, pd.DataFrame([entry])], ignore_index=True)
                        save_database(database, db_path)
                        if image_cb:
                            image_cb(entry)
                        if progress_cb:
                            progress_cb(idx + processed_count, total)
                        continue

                    # Store top-5 raw MaskRCNN detection confidence scores
                    entry["detection_scores"] = json.dumps([float(s) for s in sorted(pred_score, reverse=True)[:5]])

                    wildlife_indices = [i for i, c in enumerate(pred_class) if c in active_wildlife_categories]
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
                        stops = self._compute_exposure_stops(img, masks[primary_mask_i])
                        img_src = self._apply_exposure_correction(img, stops, raw_obj)
                        quality_crop, quality_mask = self.mask_rcnn.get_square_crop(
                            masks[primary_mask_i], img_src, resize=True
                        )
                        quality_score = self.quality_clf.classify(quality_crop, quality_mask)
                        return {
                            "species": pred_class[primary_mask_i],
                            "species_confidence": float(pred_score[primary_mask_i]),
                            "family": "N/A",
                            "family_confidence": 0.0,
                            "quality": quality_score,
                            "rating": quality_to_rating(quality_score, rating_thresholds),
                            "quality_crop": quality_crop,
                            "exposure_correction": round(stops, 4),
                        }

                    def process_bird_items(indices):
                        stage_ctx["stage"] = "process_bird"
                        items = []
                        for i in indices:
                            # Process per-crop results. Pause is checked at the
                            # top of the image loop so we avoid pausing mid-image.
                            stops = self._compute_exposure_stops(img, masks[i])
                            img_src = self._apply_exposure_correction(img, stops, raw_obj)
                            species_crop = self.mask_rcnn.get_species_crop(pred_boxes[i], img_src)
                            quality_crop, quality_mask = self.mask_rcnn.get_square_crop(masks[i], img_src, resize=True)
                            items.append(
                                {
                                    "index": i,
                                    "confidence": float(pred_score[i]),
                                    "species_crop": species_crop,
                                    "quality_crop": quality_crop,
                                    "quality_mask": quality_mask,
                                    "stops": stops,
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
                            item["exposure_correction"] = round(item["stops"], 4)
                            stage_ctx["stage"] = "quality_score"
                            quality_score = self.quality_clf.classify(item["quality_crop"], item["quality_mask"])
                            item["quality"] = quality_score
                            item["rating"] = quality_to_rating(quality_score, rating_thresholds)
                        if quality_cb:
                            quality_cb(
                                {
                                    "filename": raw_file,
                                    "results": [
                                        {"quality": i["quality"], "rating": i["rating"]} for i in items
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
                                "exposure_correction": i.get("exposure_correction", 0.0),
                            }
                            for i in bird_items
                        ]
                        primary_bird = max(bird_data, key=lambda x: x["quality"])
                        entry.update(
                            {
                                "species": primary_bird["species"],
                                "species_confidence": primary_bird["species_confidence"],
                                "family": primary_bird["family"],
                                "family_confidence": primary_bird["family_confidence"],
                                "quality": primary_bird["quality"],
                                "exposure_correction": primary_bird["exposure_correction"],
                            }
                        )
                        all_species = [b["species"] for b in bird_data]
                        all_species_conf = [float(b["species_confidence"]) for b in bird_data]
                        all_families = [b["family"] for b in bird_data]
                        all_family_conf = [float(b["family_confidence"]) for b in bird_data]
                        entry.update(
                            {
                                "secondary_species_list": json.dumps(all_species),
                                "secondary_species_scores": json.dumps(all_species_conf),
                                "secondary_family_list": json.dumps(all_families),
                                "secondary_family_scores": json.dumps(all_family_conf),
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
                                        "results": [{"quality": result["quality"], "rating": result["rating"]}],
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
                                    "exposure_correction": result["exposure_correction"],
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
                    # Store relative path for cross-platform compatibility
                    crop_path_rel = os.path.relpath(crop_path, folder)
                    entry.update({"crop_path": crop_path_rel})

                    stage_ctx["stage"] = "save_database"
                    database = pd.concat([database, pd.DataFrame([entry])], ignore_index=True)
                    save_database(database, db_path)

                    if image_cb:
                        image_cb(entry)

                    if status_cb:
                        _q = entry.get('quality', -1)
                        _display_q = f"{float(_q):.3f}" if _q not in (None, 'N/A', -1) else '—'
                        status_cb(
                            f"Processed {raw_file}: {entry['species']} Q={_display_q}"
                            f" ({idx + processed_count}/{total})"
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
                    time.sleep(2)

                if progress_cb:
                    progress_cb(idx + processed_count, total)

                # Explicitly clear large temporary variables after each image
                # so that pausing between images doesn't retain large buffers.
                try:
                    # Close the rawpy object first to release the RAW file buffer.
                    try:
                        if raw_obj is not None:
                            raw_obj.close()
                        del raw_obj
                    except Exception: pass
                    try: del masks
                    except Exception: pass
                    try: del pred_boxes
                    except Exception: pass
                    try: del pred_class
                    except Exception: pass
                    try: del pred_score
                    except Exception: pass
                    try: del img
                    except Exception: pass
                    try: del crop_img
                    except Exception: pass
                    try: del items
                    except Exception: pass
                    try: del bird_items
                    except Exception: pass
                except Exception:
                    pass

            # === Post-analysis: compute quality distribution and normalized ratings ===
            stage_ctx["stage"] = "post_analysis_normalization"
            try:
                from .ratings import compute_quality_distribution
                if not database.empty and "quality" in database.columns:
                    quality_scores = database["quality"].tolist()
                    distribution = compute_quality_distribution(quality_scores)

                    # Save analysis results (no normalized_rating; computed at runtime)
                    save_database(database, db_path)

                    # Cache quality distribution in kestrel_metadata.json for runtime normalization
                    metadata_path = os.path.join(kestrel_dir, METADATA_FILENAME)
                    try:
                        import json as _json
                        if os.path.exists(metadata_path):
                            with open(metadata_path, "r", encoding="utf-8") as mf:
                                _meta = _json.load(mf)
                        else:
                            _meta = {"version": VERSION, "analyzer": analyzer_name}
                        _meta["quality_distribution"] = distribution
                        _meta["quality_distribution_stored"] = True
                        with open(metadata_path, "w", encoding="utf-8") as mf:
                            _json.dump(_meta, mf, indent=2)
                    except Exception as _meta_e:
                        log_warning(
                            self._log_path,
                            f"Failed to write quality distribution to metadata: {_meta_e}",
                            stage="post_analysis_normalization",
                        )

                    # Create or update kestrel_scenedata.json
                    try:
                        existing_scenedata = load_scenedata(kestrel_dir)
                        if not existing_scenedata.get("scenes"):
                            new_scenedata = build_scenedata_from_database(database)
                            save_scenedata(new_scenedata, kestrel_dir)
                        else:
                            update_scenedata_with_database(existing_scenedata, database)
                            save_scenedata(existing_scenedata, kestrel_dir)
                    except Exception as _sd_e:
                        log_warning(
                            self._log_path,
                            f"Failed to create/update kestrel_scenedata.json: {_sd_e}",
                            stage="post_analysis_normalization",
                        )

                log_event(
                    self._log_path,
                    {
                        "level": "info",
                        "event": "analysis_complete",
                        "folder": folder,
                        "total_files": len(database),
                    },
                )
                if status_cb:
                    status_cb("Analysis complete.")
            except Exception as _post_e:
                log_warning(
                    self._log_path,
                    f"Post-analysis normalization failed: {_post_e}",
                    stage="post_analysis_normalization",
                )

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
