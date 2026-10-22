"""MegaDetector + SAM-HQ segmentation for the analysis pipeline.

Historical module name: until pipeline 3.0.0 this also ran the SpeciesNet
classifier + ensemble on every MegaDetector box (routing bird vs wildlife and
gating false positives). v3 removed SpeciesNet; the pipeline now gates and
routes each box from its BioCLIP 2 crop embedding (``ml/animal_gate.py``,
``BioClipSpeciesClassifier.route``), so this wrapper only proposes animal
boxes and segments them. ``SpeciesNetSAMHQWrapper`` is kept as an alias of
``DetectorSAMHQWrapper`` for existing callers.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Optional

import cv2
import numpy as np
from PIL import Image

from ..config import (
    DEFAULT_DETECTOR_NAME,
    DETECTOR_ONNX_PATHS,
    SAM_DEC_ONNX_PATH,
    SAM_ENC_ONNX_PATH,
)
from ..logging_utils import debug, warn
from . import is_gpu_active
from .provider_coordinator import (
    FailureAction,
    ProviderCoordinator,
    ResilienceConfig,
)
from .resilient_session import ResilientOnnxSession

_DEFAULT_MAX_BIRD_CROPS = 5
_MIN_MAX_BIRD_CROPS = 1
_MAX_MAX_BIRD_CROPS = 20
_HEAVY_OVERLAP_IOU = 0.75
_HEAVY_OVERLAP_CONTAINMENT = 0.90

# Pre-segmentation dedupe thresholds (boxes only, before SAM runs).
# Deliberately TIGHTER than the mask-based final filter above: we only drop a
# MegaDetector proposal here when it almost perfectly coincides with a
# higher-confidence proposal, so there is no realistic chance the gate
# would have disagreed with the winner. Anything ambiguous falls through and
# is handled by ``filter_overlapping_detections`` at the end, which uses true
# mask IoU and is the authoritative pass.
_PRE_CLASSIFIER_IOU = 0.85
_PRE_CLASSIFIER_CONTAINMENT = 0.95
_SUPPORTED_DETECTOR_NAMES = tuple(DETECTOR_ONNX_PATHS.keys())
_MDV1000_CEDAR_DETECTOR_NAMES = {"mdv1000-cedar"}


def _coerce_max_bird_crops(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = _DEFAULT_MAX_BIRD_CROPS
    return max(_MIN_MAX_BIRD_CROPS, min(_MAX_MAX_BIRD_CROPS, n))


def _coerce_detector_name(value: str | None) -> str:
    if value is None:
        return DEFAULT_DETECTOR_NAME
    norm = str(value).strip().lower()
    if norm in DETECTOR_ONNX_PATHS:
        return norm
    supported = ", ".join(_SUPPORTED_DETECTOR_NAMES)
    raise ValueError(f"Unsupported detector name '{value}'. Supported values: {supported}")


def _resolve_detector_onnx_path(detector_name: str) -> Path:
    name = _coerce_detector_name(detector_name)
    path = DETECTOR_ONNX_PATHS[name]
    if not path.is_file():
        raise FileNotFoundError(
            f"Detector ONNX not found for '{name}': {path}\n"
            f"Place the selected detector .onnx and .onnx.data files under: {path.parent}"
        )
    return path


def _box_iou(box_a, box_b) -> float:
    """Compute IoU between pipeline-format boxes ``((x1, y1), (x2, y2))``."""
    (ax1, ay1), (ax2, ay2) = box_a
    (bx1, by1), (bx2, by2) = box_b

    inter_w = max(0.0, min(float(ax2), float(bx2)) - max(float(ax1), float(bx1)))
    inter_h = max(0.0, min(float(ay2), float(by2)) - max(float(ay1), float(by1)))
    inter = inter_w * inter_h
    if inter <= 0.0:
        return 0.0

    area_a = max(0.0, float(ax2) - float(ax1)) * max(0.0, float(ay2) - float(ay1))
    area_b = max(0.0, float(bx2) - float(bx1)) * max(0.0, float(by2) - float(by1))
    union = area_a + area_b - inter
    if union <= 0.0:
        return 0.0
    return float(inter / union)


def _box_intersection_over_min_area(box_a, box_b) -> float:
    """Compute containment overlap: intersection / min(area_a, area_b)."""
    (ax1, ay1), (ax2, ay2) = box_a
    (bx1, by1), (bx2, by2) = box_b

    inter_w = max(0.0, min(float(ax2), float(bx2)) - max(float(ax1), float(bx1)))
    inter_h = max(0.0, min(float(ay2), float(by2)) - max(float(ay1), float(by1)))
    inter = inter_w * inter_h
    if inter <= 0.0:
        return 0.0

    area_a = max(0.0, float(ax2) - float(ax1)) * max(0.0, float(ay2) - float(ay1))
    area_b = max(0.0, float(bx2) - float(bx1)) * max(0.0, float(by2) - float(by1))
    min_area = min(area_a, area_b)
    if min_area <= 0.0:
        return 0.0
    return float(inter / min_area)


def filter_overlapping_detections(
    masks,
    pred_boxes,
    pred_class,
    pred_score,
    heavy_overlap_iou: float = _HEAVY_OVERLAP_IOU,
    heavy_overlap_containment: float = _HEAVY_OVERLAP_CONTAINMENT,
    overlap_rank_scores: Optional[list[float]] = None,
):
    """Remove lower-confidence detections when boxes/masks heavily overlap.

    ``overlap_rank_scores`` controls suppression priority only. Returned
    ``pred_score`` values are preserved for downstream reporting.
    """
    if masks is None or len(masks) == 0:
        return masks, pred_boxes, pred_class, pred_score

    n = len(pred_score)
    keep = [True] * n
    rank_scores = overlap_rank_scores if overlap_rank_scores is not None else pred_score
    if len(rank_scores) != n:
        raise ValueError("overlap_rank_scores length must match pred_score length")

    sorted_indices = sorted(
        range(n),
        key=lambda i: (float(rank_scores[i]), float(pred_score[i])),
        reverse=True,
    )

    for i_idx, i in enumerate(sorted_indices):
        if not keep[i]:
            continue
        for j in sorted_indices[i_idx + 1 :]:
            if not keep[j]:
                continue
            intersection = np.logical_and(masks[i], masks[j]).sum()
            union = np.logical_or(masks[i], masks[j]).sum()
            min_mask_area = min(float(masks[i].sum()), float(masks[j].sum()))
            mask_iou = float(intersection / union) if union > 0 else 0.0
            mask_containment = float(intersection / min_mask_area) if min_mask_area > 0.0 else 0.0
            box_iou = _box_iou(pred_boxes[i], pred_boxes[j])
            box_containment = _box_intersection_over_min_area(pred_boxes[i], pred_boxes[j])
            if (
                max(mask_iou, box_iou) >= heavy_overlap_iou
                or max(mask_containment, box_containment) >= heavy_overlap_containment
            ):
                keep[j] = False

    indices = [i for i in range(n) if keep[i]]
    if not indices:
        return masks, pred_boxes, pred_class, pred_score

    return (
        masks[indices],
        [pred_boxes[i] for i in indices],
        [pred_class[i] for i in indices],
        [pred_score[i] for i in indices],
    )


def _md_bbox_corners(md_bbox: list) -> tuple[tuple[float, float], tuple[float, float]]:
    """MegaDetector normalized xywh -> ``((x1, y1), (x2, y2))`` in the same
    normalized units. IoU/containment ratios are scale-invariant, so the
    pre-classifier dedupe does not need image dimensions.
    """
    x, y, bw, bh = [float(v) for v in md_bbox]
    return (x, y), (x + bw, y + bh)


def prefilter_overlapping_md_boxes(
    animal_dets: list,
    iou_thresh: float = _PRE_CLASSIFIER_IOU,
    containment_thresh: float = _PRE_CLASSIFIER_CONTAINMENT,
) -> list:
    """Drop MegaDetector proposals that heavily overlap a higher-confidence
    proposal, *before* SAM runs on each of them.

    Assumes ``animal_dets`` is already sorted by ``conf`` descending (which is
    how ``get_prediction`` produces it). Greedy O(n²) — n is the number of
    ``animal`` detections on one image, typically small.

    Uses tighter thresholds than the final mask-based overlap filter, so we
    only drop what is clearly a duplicate at the box level. Anything
    borderline is deferred to the mask-based pass at the end of
    ``get_prediction``, which sees the true subject shapes.
    """
    n = len(animal_dets)
    if n <= 1:
        return list(animal_dets)

    corners = [_md_bbox_corners(d.get("bbox", [0.0, 0.0, 0.0, 0.0])) for d in animal_dets]
    keep = [True] * n
    for i in range(n):
        if not keep[i]:
            continue
        for j in range(i + 1, n):
            if not keep[j]:
                continue
            iou = _box_iou(corners[i], corners[j])
            containment = _box_intersection_over_min_area(corners[i], corners[j])
            if iou >= iou_thresh or containment >= containment_thresh:
                keep[j] = False
    return [animal_dets[i] for i in range(n) if keep[i]]


def _md_bbox_to_pixel_box(md_bbox: list, img_w: int, img_h: int) -> tuple[float, float, float, float]:
    """MegaDetector normalized xywh -> pixel xyxy corners."""
    x_min_n, y_min_n, bw_n, bh_n = [float(v) for v in md_bbox]
    x1 = np.clip(x_min_n * img_w, 0, img_w)
    y1 = np.clip(y_min_n * img_h, 0, img_h)
    x2 = np.clip((x_min_n + bw_n) * img_w, 0, img_w)
    y2 = np.clip((y_min_n + bh_n) * img_h, 0, img_h)
    return x1, y1, x2, y2


def _pixel_box_to_pipeline_box(
    x1: float, y1: float, x2: float, y2: float
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Pipeline expects ``pred_boxes[i]`` as ``((xmin, ymin), (xmax, ymax))`` in pixels."""
    return (float(x1), float(y1)), (float(x2), float(y2))


def _clip_xyxy(x1: float, y1: float, x2: float, y2: float, w: int, h: int) -> tuple[int, int, int, int]:
    x1i = int(max(0, min(w - 1, x1)))
    y1i = int(max(0, min(h - 1, y1)))
    x2i = int(max(0, min(w, x2)))
    y2i = int(max(0, min(h, y2)))
    if x2i <= x1i:
        x2i = min(w, x1i + 1)
    if y2i <= y1i:
        y2i = min(h, y1i + 1)
    return x1i, y1i, x2i, y2i


class OnnxMDv5Detector:
    """
    MegaDetector v5a (YOLO-style) via ONNX Runtime.

    Interface matches other detectors:
        preprocess(img_pil)          → (img_tensor, orig_w, orig_h)
        predict(filepath, det_input) → {"filepath": str,
                                         "detections": [{"label": str,
                                                          "conf": float,
                                                          "bbox": [xmin,ymin,w,h]}]}

    Preprocessing resizes image to 1280×1280 (simple resize, not letterbox).
    Category map:  0 → "animal"   1 → "person"   2 → "vehicle"
    """

    _LABEL_MAP: dict[int, str] = {0: "animal", 1: "person", 2: "vehicle"}
    _INPUT_SIZE = 1280
    _MIN_CONF = 0.01
    _NMS_IOU = 0.5
    _PRE_NMS_LIMIT = 4000

    def __init__(self, onnx_path: Path, coord: ProviderCoordinator) -> None:
        onnx_path = Path(onnx_path)
        if not onnx_path.is_file():
            raise FileNotFoundError(
                f"MDv5a weights not found: {onnx_path}\n"
                "Place mdv5a.onnx (and mdv5a.onnx.data) under models/speciesnet/."
            )
        self._session = ResilientOnnxSession("detector", onnx_path, coord)
        _provs = self._session.get_providers()
        self.device = "ONNX/GPU" if is_gpu_active(_provs) else "ONNX/CPU"
        debug(f"[OnnxMDv5Detector] Loaded {onnx_path.name}  providers={_provs}")

    def preprocess(self, img_pil: "Image.Image") -> tuple:
        """Resize image to 1280x1280 (simple resize, not letterbox)."""
        orig_w, orig_h = img_pil.size
        img_1280 = np.array(
            img_pil.resize((self._INPUT_SIZE, self._INPUT_SIZE), Image.BILINEAR),
            dtype=np.float32,
        ) / 255.0
        img_tensor = img_1280.transpose(2, 0, 1)[np.newaxis]
        return (img_tensor, orig_w, orig_h)

    @staticmethod
    def _nms_xyxy(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> np.ndarray:
        if boxes.size == 0:
            return np.empty((0,), dtype=np.int64)

        x1 = boxes[:, 0]
        y1 = boxes[:, 1]
        x2 = boxes[:, 2]
        y2 = boxes[:, 3]
        areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
        order = np.argsort(scores)[::-1]

        keep: list[int] = []
        while order.size > 0:
            i = int(order[0])
            keep.append(i)
            if order.size == 1:
                break

            rest = order[1:]
            xx1 = np.maximum(x1[i], x1[rest])
            yy1 = np.maximum(y1[i], y1[rest])
            xx2 = np.minimum(x2[i], x2[rest])
            yy2 = np.minimum(y2[i], y2[rest])

            inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
            union = areas[i] + areas[rest] - inter
            iou = np.where(union > 0.0, inter / union, 0.0)
            order = rest[iou <= iou_threshold]

        return np.array(keep, dtype=np.int64)

    def predict(self, filepath: str, det_input: tuple) -> dict:
        """ONNX inference + decode YOLO-style predictions to normalized xywh."""
        img_tensor, _orig_w, _orig_h = det_input
        raw = self._session.run(None, {"images": img_tensor})
        if not raw:
            return {"filepath": filepath, "detections": []}

        preds = raw[0]
        if preds.ndim != 3 or preds.shape[2] < 8:
            raise RuntimeError(f"Unexpected mdv5a output shape: {preds.shape}")

        pred = preds[0]
        obj = pred[:, 4]
        cls_scores = pred[:, 5:8]
        cls_idx = np.argmax(cls_scores, axis=1).astype(np.int64)
        best_cls = cls_scores[np.arange(cls_scores.shape[0]), cls_idx]
        conf = obj * best_cls

        keep = conf >= self._MIN_CONF
        if not np.any(keep):
            return {"filepath": filepath, "detections": []}

        pred = pred[keep]
        cls_idx = cls_idx[keep]
        conf = conf[keep]

        max_coord = float(np.max(pred[:, :4]))
        coord_scale = float(self._INPUT_SIZE if max_coord > 2.0 else 1.0)
        cx = pred[:, 0] / coord_scale
        cy = pred[:, 1] / coord_scale
        bw = pred[:, 2] / coord_scale
        bh = pred[:, 3] / coord_scale

        x1 = np.clip(cx - (bw / 2.0), 0.0, 1.0)
        y1 = np.clip(cy - (bh / 2.0), 0.0, 1.0)
        x2 = np.clip(cx + (bw / 2.0), 0.0, 1.0)
        y2 = np.clip(cy + (bh / 2.0), 0.0, 1.0)
        boxes = np.stack([x1, y1, x2, y2], axis=1).astype(np.float32)

        selected: list[int] = []
        for class_id in np.unique(cls_idx):
            class_indices = np.where(cls_idx == class_id)[0]
            class_scores = conf[class_indices]
            if class_scores.size == 0:
                continue

            if class_scores.size > self._PRE_NMS_LIMIT:
                top_local = np.argsort(class_scores)[-self._PRE_NMS_LIMIT:]
                class_indices = class_indices[top_local]
                class_scores = conf[class_indices]

            keep_local = self._nms_xyxy(
                boxes[class_indices],
                class_scores.astype(np.float32),
                iou_threshold=self._NMS_IOU,
            )
            selected.extend(class_indices[keep_local].tolist())

        if not selected:
            return {"filepath": filepath, "detections": []}

        selected_arr = np.array(selected, dtype=np.int64)
        order = np.argsort(conf[selected_arr])[::-1]
        selected_arr = selected_arr[order]

        detections: list[dict] = []
        for i in selected_arr:
            cls_id = int(cls_idx[i])
            label = self._LABEL_MAP.get(cls_id, "unknown")
            if label == "unknown":
                continue

            bx1, by1, bx2, by2 = [float(v) for v in boxes[i]]
            detections.append(
                {
                    "label": label,
                    "conf": float(conf[i]),
                    "bbox": [
                        bx1,
                        by1,
                        max(0.0, bx2 - bx1),
                        max(0.0, by2 - by1),
                    ],
                }
            )

        detections.sort(key=lambda d: float(d.get("conf", 0.0)), reverse=True)
        return {"filepath": filepath, "detections": detections}


class OnnxMDv1000CedarDetector:
    """
    MegaDetector v1000 ``cedar`` variant (YOLOv9 gelan-c head) via ONNX Runtime.

    Cedar's ONNX is a single-file export (no `.onnx.data` sidecar) produced via
    ``torch.onnx.export(dynamo=False)``. That export path embeds weights inline
    and — critically — emits Reshape ops the DirectML execution provider accepts,
    so cedar runs on the GPU on Windows.

    Interface matches the other detectors:
        preprocess(img_pil)          → (img_tensor, scale, pad_left, pad_top, orig_w, orig_h)
        predict(filepath, det_input) → {"filepath": str,
                                         "detections": [{"label": str,
                                                          "conf": float,
                                                          "bbox": [xmin,ymin,w,h]}]}

    Preprocessing letterboxes the image to 640×640 (aspect-preserving + grey-114
    pad) with [0,1] RGB float input. The ONNX output is shape ``(1, 7, 8400)``
    channels-first — 4 box channels (cx, cy, w, h in network pixel space) + 3
    sigmoid-activated class scores (animal, person, vehicle). The decoder
    inverse-letterboxes boxes back to original-image space and runs per-class
    greedy NMS at IoU 0.5.

    Category map: 0 → "animal"   1 → "person"   2 → "vehicle"
    """

    _LABEL_MAP: dict[int, str] = {0: "animal", 1: "person", 2: "vehicle"}
    _INPUT_SIZE = 640
    _MIN_CONF = 0.01
    _NMS_IOU = 0.5
    _PRE_NMS_LIMIT = 4000
    _PAD_COLOR = (114, 114, 114)

    def __init__(self, onnx_path: Path, coord: ProviderCoordinator) -> None:
        onnx_path = Path(onnx_path)
        if not onnx_path.is_file():
            raise FileNotFoundError(
                f"mdv1000-cedar weights not found: {onnx_path}\n"
                "Place mdv1000-cedar.onnx under models/speciesnet/ (single file — no .onnx.data sidecar)."
            )
        self._session = ResilientOnnxSession("detector", onnx_path, coord)
        _provs = self._session.get_providers()
        self.device = "ONNX/GPU" if is_gpu_active(_provs) else "ONNX/CPU"
        debug(f"[OnnxMDv1000CedarDetector] Loaded {onnx_path.name}  providers={_provs}")

    @staticmethod
    def _nms_xyxy(boxes: np.ndarray, scores: np.ndarray, iou_threshold: float) -> np.ndarray:
        if boxes.size == 0:
            return np.empty((0,), dtype=np.int64)

        x1 = boxes[:, 0]
        y1 = boxes[:, 1]
        x2 = boxes[:, 2]
        y2 = boxes[:, 3]
        areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
        order = np.argsort(scores)[::-1]

        keep: list[int] = []
        while order.size > 0:
            i = int(order[0])
            keep.append(i)
            if order.size == 1:
                break

            rest = order[1:]
            xx1 = np.maximum(x1[i], x1[rest])
            yy1 = np.maximum(y1[i], y1[rest])
            xx2 = np.minimum(x2[i], x2[rest])
            yy2 = np.minimum(y2[i], y2[rest])

            inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
            union = areas[i] + areas[rest] - inter
            iou = np.where(union > 0.0, inter / union, 0.0)
            order = rest[iou <= iou_threshold]

        return np.array(keep, dtype=np.int64)

    def preprocess(self, img_pil: "Image.Image") -> tuple:
        """Letterbox the image into the fixed 640×640 input. Returns the
        normalized RGB tensor plus the (scale, pad_left, pad_top) needed to
        inverse-transform output boxes back to original-image space.
        """
        orig_w, orig_h = img_pil.size

        scale = min(self._INPUT_SIZE / float(orig_w), self._INPUT_SIZE / float(orig_h))
        new_w = max(1, int(round(orig_w * scale)))
        new_h = max(1, int(round(orig_h * scale)))

        resized = img_pil.resize((new_w, new_h), Image.Resampling.BILINEAR)
        pad_left = (self._INPUT_SIZE - new_w) // 2
        pad_top = (self._INPUT_SIZE - new_h) // 2

        canvas = Image.new("RGB", (self._INPUT_SIZE, self._INPUT_SIZE), self._PAD_COLOR)
        canvas.paste(resized, (pad_left, pad_top))

        img_np = np.asarray(canvas, dtype=np.float32) / 255.0
        img_tensor = img_np.transpose(2, 0, 1)[np.newaxis]  # [1, 3, 640, 640]
        return (img_tensor, float(scale), int(pad_left), int(pad_top), int(orig_w), int(orig_h))

    def predict(self, filepath: str, det_input: tuple) -> dict:
        """ONNX inference + decode (1,7,N) channels-first output → normalized xywh detections."""
        img_tensor, scale, pad_left, pad_top, orig_w, orig_h = det_input
        raw = self._session.run(None, {"images": img_tensor})
        if not raw:
            return {"filepath": filepath, "detections": []}

        preds = raw[0]
        if preds.ndim != 3 or preds.shape[1] != 7:
            raise RuntimeError(f"Unexpected mdv1000-cedar output shape: {preds.shape}")

        pred = preds[0].T  # (N, 7) — 4 box + 3 sigmoid class scores
        cls_scores = pred[:, 4:7]
        cls_idx = np.argmax(cls_scores, axis=1).astype(np.int64)
        conf = cls_scores[np.arange(cls_scores.shape[0]), cls_idx]

        keep = conf >= self._MIN_CONF
        if not np.any(keep):
            return {"filepath": filepath, "detections": []}

        pred = pred[keep]
        cls_idx = cls_idx[keep]
        conf = conf[keep]

        # Box coords are in network-pixel space (0..INPUT_SIZE). If the model ever
        # emits normalized coords (rare), scale up.
        max_coord = float(np.max(pred[:, :4]))
        if max_coord <= 2.0:
            cx = pred[:, 0] * self._INPUT_SIZE
            cy = pred[:, 1] * self._INPUT_SIZE
            bw = pred[:, 2] * self._INPUT_SIZE
            bh = pred[:, 3] * self._INPUT_SIZE
        else:
            cx = pred[:, 0]
            cy = pred[:, 1]
            bw = pred[:, 2]
            bh = pred[:, 3]

        # Inverse letterbox: undo the (scale, pad) transform, then normalize to original image.
        x1 = ((cx - bw / 2.0) - pad_left) / scale
        y1 = ((cy - bh / 2.0) - pad_top) / scale
        x2 = ((cx + bw / 2.0) - pad_left) / scale
        y2 = ((cy + bh / 2.0) - pad_top) / scale
        x1 = np.clip(x1 / float(orig_w), 0.0, 1.0)
        y1 = np.clip(y1 / float(orig_h), 0.0, 1.0)
        x2 = np.clip(x2 / float(orig_w), 0.0, 1.0)
        y2 = np.clip(y2 / float(orig_h), 0.0, 1.0)
        boxes = np.stack([x1, y1, x2, y2], axis=1).astype(np.float32)

        selected: list[int] = []
        for class_id in np.unique(cls_idx):
            class_indices = np.where(cls_idx == class_id)[0]
            class_scores = conf[class_indices]
            if class_scores.size == 0:
                continue

            if class_scores.size > self._PRE_NMS_LIMIT:
                top_local = np.argsort(class_scores)[-self._PRE_NMS_LIMIT:]
                class_indices = class_indices[top_local]
                class_scores = conf[class_indices]

            keep_local = self._nms_xyxy(
                boxes[class_indices],
                class_scores.astype(np.float32),
                iou_threshold=self._NMS_IOU,
            )
            selected.extend(class_indices[keep_local].tolist())

        if not selected:
            return {"filepath": filepath, "detections": []}

        selected_arr = np.array(selected, dtype=np.int64)
        order = np.argsort(conf[selected_arr])[::-1]
        selected_arr = selected_arr[order]

        detections: list[dict] = []
        for i in selected_arr:
            cls_id = int(cls_idx[i])
            label = self._LABEL_MAP.get(cls_id, "unknown")
            if label == "unknown":
                continue

            bx1, by1, bx2, by2 = [float(v) for v in boxes[i]]
            detections.append(
                {
                    "label": label,
                    "conf": float(conf[i]),
                    "bbox": [
                        bx1,
                        by1,
                        max(0.0, bx2 - bx1),
                        max(0.0, by2 - by1),
                    ],
                }
            )

        detections.sort(key=lambda d: float(d.get("conf", 0.0)), reverse=True)
        return {"filepath": filepath, "detections": detections}


class OnnxSamPredictor:
    """
    SAM-HQ ViT-Tiny via ONNX Runtime (split encoder + decoder).

    Usage:
        predictor = OnnxSamPredictor(enc_path, dec_path)
        emb, interm, resized_hw, orig_hw = predictor.encode(img_np)
        # For each detection box on the same image:
        mask, iou = predictor.decode_box(emb, interm, (x1, y1, x2, y2), resized_hw, orig_hw)
    """

    _IMG_SIZE = 1024

    def __init__(self, enc_path: Path, dec_path: Path, coord: ProviderCoordinator) -> None:
        self._enc_session = ResilientOnnxSession("sam_enc", enc_path, coord)
        self._dec_session = ResilientOnnxSession("sam_dec", dec_path, coord)
        self._decoder_input_shapes = {
            inp.name: inp.shape for inp in self._dec_session.get_inputs()
        }
        self._decoder_requires_padded_im_size = "padded_im_size" in self._decoder_input_shapes
        self._supports_prompt_batching = self._detect_prompt_batch_support()
        self._batch_unsupported_logged = False
        # Some execution providers (notably CoreMLExecutionProvider on macOS)
        # advertise dynamic batch shapes via the ONNX graph but reject batched
        # prompt input at runtime with "Unable to compute the prediction".
        # When that happens we permanently downgrade to per-box decode for the
        # rest of the session — otherwise every multi-detection image pays a
        # failed batch attempt before falling back.
        self._batch_decode_runtime_failed = False
        _provs = self._enc_session.get_providers()
        self.device = "ONNX/GPU" if is_gpu_active(_provs) else "ONNX/CPU"
        _active = _provs[0] if _provs else "unknown"
        debug(f"[OnnxSamPredictor] Loaded encoder+decoder  Active provider: {_active}  all providers: {_provs}")
        debug(f"[OnnxSamPredictor] Prompt batching support: {self._supports_prompt_batching}")
        debug(f"[OnnxSamPredictor] Decoder requires padded_im_size: {self._decoder_requires_padded_im_size}")
        debug(f"[OnnxSamPredictor] Encoder fixed input HW: {self._encoder_fixed_hw()}")

    def _detect_prompt_batch_support(self) -> bool:
        """
        Infer whether decoder graph supports prompt batching (N > 1) by checking
        input tensor batch dimensions. If any prompt-related input has a fixed
        first dimension of 1, treat batching as unsupported.
        """
        try:
            inputs = {inp.name: inp.shape for inp in self._dec_session.get_inputs()}
        except Exception:
            return False

        def _first_dim(name: str):
            shape = inputs.get(name)
            if not shape or len(shape) == 0:
                return None
            return shape[0]

        for name in ("point_coords", "point_labels", "mask_input", "has_mask_input", "orig_im_size"):
            d0 = _first_dim(name)
            if isinstance(d0, int) and d0 == 1:
                return False
        return True

    def _decoder_input_rank(self, name: str) -> int:
        shape = self._decoder_input_shapes.get(name)
        return len(shape) if shape is not None else 0

    def _build_decoder_inputs(
        self,
        image_embeddings: np.ndarray,
        interm_embeddings: np.ndarray,
        point_coords: np.ndarray,
        point_labels: np.ndarray,
        batch: int,
        resized_hw: tuple[int, int],
        original_hw: tuple[int, int],
    ) -> dict[str, np.ndarray]:
        orig_h, orig_w = original_hw
        resized_h, resized_w = resized_hw

        feed: dict[str, np.ndarray] = {
            "image_embeddings": image_embeddings,
            "interm_embeddings": interm_embeddings,
            "point_coords": point_coords.astype(np.float32),
            "point_labels": point_labels.astype(np.float32),
            "mask_input": np.zeros((batch, 1, 256, 256), dtype=np.float32),
        }

        # Old exports often accept (G,), newer exports require (G,1).
        has_mask_rank = self._decoder_input_rank("has_mask_input")
        if has_mask_rank == 2:
            feed["has_mask_input"] = np.zeros((batch, 1), dtype=np.float32)
        else:
            feed["has_mask_input"] = np.zeros((batch,), dtype=np.float32)

        # Old exports often accept (2,), newer exports require (G,2).
        orig_rank = self._decoder_input_rank("orig_im_size")
        if orig_rank == 1:
            feed["orig_im_size"] = np.array([orig_h, orig_w], dtype=np.float32)
        else:
            feed["orig_im_size"] = np.tile(
                np.array([[orig_h, orig_w]], dtype=np.float32),
                (batch, 1),
            )

        # New decoder export requires resized (pre-pad) H,W for each prompt group.
        if self._decoder_requires_padded_im_size:
            feed["padded_im_size"] = np.tile(
                np.array([[resized_h, resized_w]], dtype=np.float32),
                (batch, 1),
            )

        return feed

    @staticmethod
    def _resize_longest_side(image: np.ndarray, target: int) -> np.ndarray:
        h, w = image.shape[:2]
        scale = target / max(h, w)
        new_h, new_w = int(round(h * scale)), int(round(w * scale))
        return cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR)

    def _encoder_fixed_hw(self) -> tuple[int, int] | None:
        """Return fixed encoder (H, W) if the ONNX input shape is static, else None."""
        try:
            shape = self._enc_session.get_inputs()[0].shape
        except Exception:
            return None
        if not shape or len(shape) != 4:
            return None
        h = shape[2]
        w = shape[3]
        if isinstance(h, int) and isinstance(w, int):
            return (h, w)
        return None

    def encode(self, img_np: np.ndarray) -> tuple:
        """
        Encode an image to SAM embeddings. Called once per image; reuse
        the returned embeddings for all per-detection decode_box() calls.

        The ONNX encoder normalizes internally, so input must be float32
        in [0, 255] — do NOT pre-normalize.

        Args:
            img_np: uint8 HxWxC RGB numpy array.

        Returns:
            (image_embeddings, interm_embeddings, resized_hw, original_hw)
        """
        orig_h, orig_w = img_np.shape[:2]
        fixed_hw = self._encoder_fixed_hw()
        if fixed_hw is not None:
            # New exports may pin encoder input to a fixed non-square size.
            # Feed exactly what the graph declares.
            fixed_h, fixed_w = fixed_hw
            resized = cv2.resize(img_np, (fixed_w, fixed_h), interpolation=cv2.INTER_LINEAR)
            resized_h, resized_w = resized.shape[:2]
            img = resized.astype(np.float32)
            img = img.transpose(2, 0, 1)[np.newaxis]  # [1, 3, H_fixed, W_fixed]
        else:
            resized = self._resize_longest_side(img_np, self._IMG_SIZE)
            resized_h, resized_w = resized.shape[:2]
            img = resized.astype(np.float32)
            pad_h = self._IMG_SIZE - resized_h
            pad_w = self._IMG_SIZE - resized_w
            img = np.pad(img, ((0, pad_h), (0, pad_w), (0, 0)))  # HxWx3
            img = img.transpose(2, 0, 1)[np.newaxis]             # [1, 3, 1024, 1024]

        image_embeddings, interm_embeddings = self._enc_session.run(None, {"input_image": img})
        return image_embeddings, interm_embeddings, (resized_h, resized_w), (orig_h, orig_w)

    def decode_box(
        self,
        image_embeddings: np.ndarray,
        interm_embeddings: np.ndarray,
        box_xyxy: tuple,
        resized_hw: tuple,
        original_hw: tuple,
    ) -> tuple[np.ndarray, float]:
        """
        Decode a bounding-box prompt to a mask.

        Args:
            image_embeddings, interm_embeddings: from encode()
            box_xyxy: (x1, y1, x2, y2) in original pixel space (absolute coords)
            resized_hw, original_hw: from encode()

        Returns:
            (mask bool HxW at original resolution, iou float)
        """
        orig_h, orig_w = original_hw
        resized_h, resized_w = resized_hw
        x1, y1, x2, y2 = box_xyxy

        # Transform box corners from original space to the resized (apply_image) space
        box_pts = np.array([
            [x1 * resized_w / orig_w, y1 * resized_h / orig_h],
            [x2 * resized_w / orig_w, y2 * resized_h / orig_h],
        ], dtype=np.float32)

        point_coords = box_pts[np.newaxis]                        # (1, 2, 2)
        point_labels = np.array([[2.0, 3.0]], dtype=np.float32)   # TL=2, BR=3
        feed = self._build_decoder_inputs(
            image_embeddings=image_embeddings,
            interm_embeddings=interm_embeddings,
            point_coords=point_coords,
            point_labels=point_labels,
            batch=1,
            resized_hw=resized_hw,
            original_hw=original_hw,
        )
        masks_out, iou_out, _ = self._dec_session.run(None, feed)
        mask = masks_out[0, 0] > 0.0
        iou  = float(iou_out[0, 0])
        return mask, iou

    def decode_boxes(
        self,
        image_embeddings: np.ndarray,
        interm_embeddings: np.ndarray,
        boxes_xyxy: list[tuple[int, int, int, int]],
        resized_hw: tuple,
        original_hw: tuple,
    ) -> list[tuple[np.ndarray, float]]:
        """
        Batch decode multiple bounding-box prompts to masks for one image.

        Returns:
            List of (mask bool HxW at original resolution, iou float), one per box.
        """
        if not boxes_xyxy:
            return []
        if len(boxes_xyxy) == 1:
            return [self.decode_box(image_embeddings, interm_embeddings, boxes_xyxy[0], resized_hw, original_hw)]
        if not self._supports_prompt_batching or self._batch_decode_runtime_failed:
            if not self._batch_unsupported_logged:
                debug(
                    "[SAM-HQ] decoder ONNX export has fixed batch=1 on prompt inputs; "
                    "using per-box decode path."
                )
                self._batch_unsupported_logged = True
            return [
                self.decode_box(image_embeddings, interm_embeddings, box_xyxy, resized_hw, original_hw)
                for box_xyxy in boxes_xyxy
            ]

        orig_h, orig_w = original_hw
        resized_h, resized_w = resized_hw
        batch = len(boxes_xyxy)

        box_pts = np.zeros((batch, 2, 2), dtype=np.float32)
        for i, (x1, y1, x2, y2) in enumerate(boxes_xyxy):
            box_pts[i, 0, 0] = x1 * resized_w / orig_w
            box_pts[i, 0, 1] = y1 * resized_h / orig_h
            box_pts[i, 1, 0] = x2 * resized_w / orig_w
            box_pts[i, 1, 1] = y2 * resized_h / orig_h

        point_coords = box_pts
        point_labels = np.tile(np.array([[2.0, 3.0]], dtype=np.float32), (batch, 1))
        # Decoder repeats one image embedding across prompt groups internally.
        feed = self._build_decoder_inputs(
            image_embeddings=image_embeddings,
            interm_embeddings=interm_embeddings,
            point_coords=point_coords,
            point_labels=point_labels,
            batch=batch,
            resized_hw=resized_hw,
            original_hw=original_hw,
        )
        masks_out, iou_out, _ = self._dec_session.run(None, feed)

        results: list[tuple[np.ndarray, float]] = []
        for i in range(batch):
            mask = masks_out[i, 0] > 0.0
            iou = float(iou_out[i, 0])
            results.append((mask, iou))
        return results


class DetectorSAMHQWrapper:
    """MegaDetector animal proposals + SAM-HQ (ViT-Tiny ONNX) masks from box prompts.

    ``get_prediction`` returns every MegaDetector ``animal`` box at or above the
    detection threshold (after box-level dedupe), segmented, capped at
    ``candidate_cap`` boxes, then the mask-based overlap filter. Every returned
    ``pred_class`` is ``"animal"`` and ``pred_score`` is the detector
    confidence: the pipeline decides bird / wildlife / false positive from the
    BioCLIP embedding of each crop and then applies the per-class
    ``max_bird_crops`` cap.
    """

    def __init__(
        self,
        max_bird_crops: int = _DEFAULT_MAX_BIRD_CROPS,
        use_gpu: bool = True,
        detector_name: str = DEFAULT_DETECTOR_NAME,
        *,
        status_cb: Optional[Callable[[str], None]] = None,
        resilience_cfg: Optional[ResilienceConfig] = None,
    ):
        self.max_bird_crops = _coerce_max_bird_crops(max_bird_crops)
        self.use_gpu = bool(use_gpu)
        self.detector_name = _coerce_detector_name(detector_name)
        self.predictor: Optional[OnnxSamPredictor] = None
        self.detector: Optional[Any] = None
        self._status_cb = status_cb
        self._coord = ProviderCoordinator(
            user_gpu_enabled=self.use_gpu,
            cfg=resilience_cfg or ResilienceConfig(),
            status_cb=status_cb,
        )
        self._coord.register_recreate_callback(self.recreate_sessions)

    @property
    def coord(self) -> ProviderCoordinator:
        return self._coord

    @property
    def candidate_cap(self) -> int:
        """Boxes segmented per image: room for a full bird AND wildlife quota
        (each capped at ``max_bird_crops`` after routing) so a false positive
        that the gate later drops cannot crowd out a real subject."""
        return 2 * self.max_bird_crops

    def _ensure_detector(self) -> None:
        if self.detector is not None:
            return
        detector_path = _resolve_detector_onnx_path(self.detector_name)
        if self.detector_name == "mdv5a":
            self.detector = OnnxMDv5Detector(detector_path, self._coord)
        elif self.detector_name in _MDV1000_CEDAR_DETECTOR_NAMES:
            self.detector = OnnxMDv1000CedarDetector(detector_path, self._coord)
        else:
            raise ValueError(f"Unsupported detector name: {self.detector_name!r}")
        debug(f"[DetectorSAMHQ] Detector model    : {self.detector_name} ({detector_path.name})")
        debug(f"[DetectorSAMHQ] Detector          : {self.detector.device}")

    # Old name, kept for callers/tests written against the SpeciesNet wrapper.
    _ensure_speciesnet = _ensure_detector

    def ensure_loaded(self) -> None:
        """Eagerly load the detector + SAM-HQ models for this wrapper instance."""
        self._ensure_detector()
        self._ensure_sam()

    def _ensure_sam(self) -> None:
        if self.predictor is not None:
            return
        if not Path(SAM_ENC_ONNX_PATH).is_file():
            raise FileNotFoundError(
                f"SAM-HQ encoder ONNX not found at: {SAM_ENC_ONNX_PATH}\n"
                "Place sam_hq_vit_tiny_encoder.onnx under models/speciesnet/."
            )
        if not Path(SAM_DEC_ONNX_PATH).is_file():
            raise FileNotFoundError(
                f"SAM-HQ decoder ONNX not found at: {SAM_DEC_ONNX_PATH}\n"
                "Place sam_hq_vit_tiny_decoder.onnx under models/speciesnet/."
            )
        try:
            self.predictor = OnnxSamPredictor(SAM_ENC_ONNX_PATH, SAM_DEC_ONNX_PATH, self._coord)
        except Exception as e:
            # If init failed on GPU, demote and try once on CPU. This preserves
            # the behavior of the old SAM-only fallback for any device that
            # can build a CPU session even when GPU init throws.
            if self._coord.on_run_failure(e) == FailureAction.RECREATE_AND_RETRY:
                warn(f"[DetectorSAMHQ] SAM-HQ GPU init failed, falling back to CPU: {e}")
                self.predictor = OnnxSamPredictor(SAM_ENC_ONNX_PATH, SAM_DEC_ONNX_PATH, self._coord)
            else:
                raise
        debug(f"[DetectorSAMHQ] SAM-HQ            : {self.predictor.device}")

    def recreate_sessions(self, target_use_gpu: bool) -> None:
        """Rebuild every ONNX session registered with the coordinator on its
        current provider. Called by ``ProviderCoordinator`` when demoting
        GPU→CPU or promoting CPU→GPU.

        Walks the coordinator's session registry, which covers BOTH the
        wrapper's detector/SAM sessions AND the pipeline-owned sessions
        (BioCLIP, SigLIP, legacy quality) — they all share the same coord, so
        no session is left behind on a dead provider.
        """
        self.use_gpu = bool(target_use_gpu)
        self._coord.recreate_all()

    def update_status_cb(self, status_cb: Optional[Callable[[str], None]]) -> None:
        """Rebind the coordinator's status callback when a wrapper is reused
        across folders so notifications target the active folder, not the
        first folder that constructed the wrapper.
        """
        self._status_cb = status_cb
        self._coord.update_status_cb(status_cb)

    def get_prediction(
        self,
        image_data: np.ndarray,
        image_path: str | Path,
        *,
        wildlife_enabled: bool = True,
        threshold: float = 0.75,
        mask_threshold: float = 0.5,
    ):
        """Run MegaDetector + SAM-HQ with provider-resilience retry.

        On a known "session is now corpse" error (DML device-removed, CoreML
        cold-start path loss, etc.) the coordinator demotes GPU→CPU, the
        wrapper rebuilds every loaded session on the new provider, and the
        same image is retried once. Any other exception propagates immediately
        so the pipeline's per-image catcher marks it errored as before.
        """
        if self._coord.should_try_promote():
            self._coord.attempt_promotion()

        last_exc: Optional[BaseException] = None
        for attempt in range(self._coord.cfg.max_attempts_per_image):
            try:
                result = self._get_prediction_inner(
                    image_data,
                    image_path,
                    wildlife_enabled=wildlife_enabled,
                    threshold=threshold,
                    mask_threshold=mask_threshold,
                )
                self._coord.on_run_success()
                return result
            except Exception as e:
                last_exc = e
                if attempt + 1 >= self._coord.cfg.max_attempts_per_image:
                    raise
                action = self._coord.on_run_failure(e)
                if action != FailureAction.RECREATE_AND_RETRY:
                    raise
                try:
                    self.recreate_sessions(target_use_gpu=False)
                except Exception:
                    raise last_exc
        if last_exc is not None:
            raise last_exc
        return [], [], [], []

    def run_with_resilience(self, fn: Callable[[], Any]) -> Any:
        """Run ``fn`` (one pipeline-owned model call, e.g. BioCLIP/SigLIP on a
        crop) with the same demote-and-retry-once policy as ``get_prediction``."""
        last_exc: Optional[BaseException] = None
        for attempt in range(self._coord.cfg.max_attempts_per_image):
            try:
                return fn()
            except Exception as e:
                last_exc = e
                if attempt + 1 >= self._coord.cfg.max_attempts_per_image:
                    raise
                if self._coord.on_run_failure(e) != FailureAction.RECREATE_AND_RETRY:
                    raise
                try:
                    self.recreate_sessions(target_use_gpu=False)
                except Exception:
                    raise last_exc
        if last_exc is not None:
            raise last_exc
        return None

    def _get_prediction_inner(
        self,
        image_data: np.ndarray,
        image_path: str | Path,
        *,
        wildlife_enabled: bool = True,
        threshold: float = 0.75,
        mask_threshold: float = 0.5,
    ):
        """Body of ``get_prediction``. Single attempt, no retry."""
        _ = mask_threshold  # SAM-HQ ignores the old Mask R-CNN pixel threshold; UI keeps the knob.
        _ = wildlife_enabled  # routing happens in the pipeline from the BioCLIP embedding

        self._ensure_detector()
        self._ensure_sam()

        fp = str(Path(image_path).resolve())
        h, w = image_data.shape[:2]
        img_pil = Image.fromarray(image_data)

        detector_input = self.detector.preprocess(img_pil)
        det_result = self.detector.predict(fp, detector_input)
        detections = det_result.get("detections", []) or []

        detector_threshold = float(threshold)
        animal_dets: list[dict[str, Any]] = [
            det for det in detections
            if str(det.get("label", "")) == "animal" and float(det.get("conf", 0.0)) >= detector_threshold
        ]
        animal_dets.sort(key=lambda d: float(d.get("conf", 0.0)), reverse=True)

        pre_nms_count = len(animal_dets)
        animal_dets = prefilter_overlapping_md_boxes(animal_dets)
        pre_nms_dropped = pre_nms_count - len(animal_dets)
        if len(animal_dets) > self.candidate_cap:
            debug(
                f"[DetectorSAMHQ] candidate cap: segmenting {self.candidate_cap} of"
                f" {len(animal_dets)} MegaDetector animal boxes"
            )
            animal_dets = animal_dets[: self.candidate_cap]
        debug(
            f"[DetectorSAMHQ] {os.path.basename(fp)}  animals -> SAM: {len(animal_dets)}"
            f"  (detector_threshold={detector_threshold:.2f}, total proposals={len(detections)}"
            f"{f', pre-NMS dropped {pre_nms_dropped}' if pre_nms_dropped else ''})"
        )
        if not animal_dets or self.predictor is None:
            return [], [], [], []

        image_embeddings, interm_embeddings, resized_hw, original_hw = self.predictor.encode(image_data)

        candidates: list[dict[str, Any]] = []
        for det in animal_dets:
            x1, y1, x2, y2 = _md_bbox_to_pixel_box(det.get("bbox", [0.0, 0.0, 0.0, 0.0]), w, h)
            xi1, yi1, xi2, yi2 = _clip_xyxy(x1, y1, x2, y2, w, h)
            candidates.append({
                "prompt_box": (xi1, yi1, xi2, yi2),
                "pred_boxes": _pixel_box_to_pipeline_box(x1, y1, x2, y2),
                "detector_confidence": float(det.get("conf", 0.0)),
            })

        sam_results: list[tuple[np.ndarray, float]] = []
        try:
            sam_results = self.predictor.decode_boxes(
                image_embeddings,
                interm_embeddings,
                [c["prompt_box"] for c in candidates],
                resized_hw,
                original_hw,
            )
        except Exception as e:
            if not getattr(self.predictor, "_batch_decode_runtime_failed", False):
                warn(
                    f"[SAM-HQ] batch decode failed at runtime; downgrading to "
                    f"per-box decode for the rest of this session: {e}"
                )
                try:
                    self.predictor._batch_decode_runtime_failed = True
                except Exception:
                    pass
            sam_results = []
            for c in candidates:
                try:
                    sam_results.append(
                        self.predictor.decode_box(
                            image_embeddings, interm_embeddings, c["prompt_box"], resized_hw, original_hw,
                        )
                    )
                except Exception as e2:
                    warn(f"[SAM-HQ] mask failed for one box: {e2}")
                    sam_results.append((None, 0.0))

        rows = [
            (sam_out[0], c) for c, sam_out in zip(candidates, sam_results) if sam_out[0] is not None
        ]
        if not rows:
            return [], [], [], []
        masks_arr = np.stack([m for m, _ in rows], axis=0)
        pred_boxes = [c["pred_boxes"] for _, c in rows]
        pred_class = ["animal"] * len(rows)
        pred_score = [c["detector_confidence"] for _, c in rows]
        pre_overlap_count = len(rows)
        result = filter_overlapping_detections(
            masks_arr,
            pred_boxes,
            pred_class,
            pred_score,
            heavy_overlap_iou=_HEAVY_OVERLAP_IOU,
            heavy_overlap_containment=_HEAVY_OVERLAP_CONTAINMENT,
        )
        post_overlap_count = len(result[2]) if result[2] is not None else 0
        if post_overlap_count < pre_overlap_count:
            debug(
                f"[DetectorSAMHQ] overlap filter: removed {pre_overlap_count - post_overlap_count}"
                f" of {pre_overlap_count} detections (IoU>={_HEAVY_OVERLAP_IOU}"
                f" or containment>={_HEAVY_OVERLAP_CONTAINMENT})"
            )
        return result

    # --- Geometry helpers for square crops and species bbox ---

    @staticmethod
    def _fsolve(func, xmin, xmax):
        x_min, x_max = xmin, xmax
        while x_max - x_min > 10:
            x_mid = (x_min + x_max) / 2
            if func(x_mid) < 0:
                x_min = x_mid
            else:
                x_max = x_mid
        return (x_min + x_max) / 2

    def _get_bounding_box(self, mask):
        # Compute marginal sums once (two O(N) passes) instead of materialising
        # all nonzero coordinates with np.where (allocates ~19 MB for a 30% mask).
        # Also caches mask_sum in the bisection closure, avoiding repeated full-image
        # scans (was 8+ np.sum(mask) calls, each ~5-10 ms on a 12 MP mask).
        cols_sum = mask.sum(axis=0, dtype=np.int64)   # (W,)
        rows_sum = mask.sum(axis=1, dtype=np.int64)   # (H,)
        mask_sum = int(cols_sum.sum())
        if mask_sum == 0:
            h, w = mask.shape[:2]
            return 0, w, 0, h
        cx = int(np.dot(cols_sum.astype(np.float64), np.arange(mask.shape[1], dtype=np.float64)) / mask_sum)
        cy = int(np.dot(rows_sum.astype(np.float64), np.arange(mask.shape[0], dtype=np.float64)) / mask_sum)
        center = (cx, cy)

        def fraction_inside(center_of_mass, S):
            x_min2 = max(0, int(center_of_mass[0] - S / 2))
            x_max2 = min(mask.shape[1], int(center_of_mass[0] + S / 2))
            y_min2 = max(0, int(center_of_mass[1] - S / 2))
            y_max2 = min(mask.shape[0], int(center_of_mass[1] + S / 2))
            return int(mask[y_min2:y_max2, x_min2:x_max2].sum()) / mask_sum  # cached

        S = self._fsolve(lambda S: fraction_inside(center, S) - 0.8, 10, 3000)
        S = int(S * 1 / 0.5)
        x_min = int(center[0] - S / 2)
        x_max = int(center[0] + S / 2)
        y_min = int(center[1] - S / 2)
        y_max = int(center[1] + S / 2)
        x_min = max(0, x_min)
        x_max = min(mask.shape[1], x_max)
        y_min = max(0, y_min)
        y_max = min(mask.shape[0], y_max)
        slx = x_max - x_min
        sly = y_max - y_min
        if slx > sly:
            center = (int((x_min + x_max) / 2), int((y_min + y_max) / 2))
            s_new = sly
        else:
            center = (int((x_min + x_max) / 2), int((y_min + y_max) / 2))
            s_new = slx
        x_min = int(center[0] - s_new / 2)
        x_max = int(center[0] + s_new / 2)
        y_min = int(center[1] - s_new / 2)
        y_max = int(center[1] + s_new / 2)
        return x_min, x_max, y_min, y_max

    def get_square_crop(self, mask, img, resize=True):
        bbox = self.get_square_crop_box(mask)
        x_min = bbox["x_min"]
        x_max = bbox["x_max"]
        y_min = bbox["y_min"]
        y_max = bbox["y_max"]
        crop = img[y_min:y_max, x_min:x_max]
        mask_crop = mask[y_min:y_max, x_min:x_max]
        if resize:
            crop = cv2.resize(crop, (1024, 1024))
            mask_crop = cv2.resize(mask_crop.astype(np.uint8), (1024, 1024))
        return crop, mask_crop

    def get_square_crop_box(self, mask):
        x_min, x_max, y_min, y_max = self._get_bounding_box(mask)
        h, w = mask.shape[:2]
        x_min = max(0, min(int(x_min), max(0, w - 1)))
        y_min = max(0, min(int(y_min), max(0, h - 1)))
        x_max = max(x_min + 1, min(int(x_max), w))
        y_max = max(y_min + 1, min(int(y_max), h))

        width = x_max - x_min
        height = y_max - y_min
        w_denom = float(max(1, w))
        h_denom = float(max(1, h))
        x_center = x_min + (width / 2.0)
        y_center = y_min + (height / 2.0)

        return {
            "x_min": int(x_min),
            "x_max": int(x_max),
            "y_min": int(y_min),
            "y_max": int(y_max),
            "width": int(width),
            "height": int(height),
            "x_min_norm": float(x_min / w_denom),
            "x_max_norm": float(x_max / w_denom),
            "y_min_norm": float(y_min / h_denom),
            "y_max_norm": float(y_max / h_denom),
            "x_center_norm": float(x_center / w_denom),
            "y_center_norm": float(y_center / h_denom),
        }

    @staticmethod
    def get_species_crop(box, img):
        xmin = int(box[0][0])
        ymin = int(box[0][1])
        xmax = int(box[1][0])
        ymax = int(box[1][1])
        return img[ymin:ymax, xmin:xmax]


# Pre-3.0.0 name.
SpeciesNetSAMHQWrapper = DetectorSAMHQWrapper
