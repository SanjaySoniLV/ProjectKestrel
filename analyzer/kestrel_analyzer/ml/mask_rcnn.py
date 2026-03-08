import logging
from pathlib import Path

import cv2
import numpy as np
import torch
import torchvision.models.detection as detection_models
import torchvision.transforms as T

from ..config import MASK_RCNN_WEIGHTS_PATH
from ..gpu_utils import get_torch_device

logger = logging.getLogger(__name__)


class MaskRCNNWrapper:
    def __init__(self, use_gpu: bool = False):
        self.COCO_INSTANCE_CATEGORY_NAMES = [
            "__background__", "person", "bicycle", "car", "motorcycle", "airplane", "bus",
            "train", "truck", "boat", "traffic light", "fire hydrant", "N/A", "stop sign",
            "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
            "elephant", "bear", "zebra", "giraffe", "N/A", "backpack", "umbrella", "N/A", "N/A",
            "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
            "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
            "bottle", "N/A", "wine glass", "cup", "fork", "knife", "spoon", "bowl",
            "banana", "apple", "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza",
            "donut", "cake", "chair", "couch", "potted plant", "bed", "N/A", "dining table",
            "N/A", "N/A", "toilet", "N/A", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
            "microwave", "oven", "toaster", "sink", "refrigerator", "N/A", "book",
            "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
        ]
        weights_path = Path(MASK_RCNN_WEIGHTS_PATH)
        if not weights_path.exists():
            raise FileNotFoundError(
                f"Mask R-CNN weights not found at: {weights_path}\n"
                "The weights file should be bundled with the application."
            )

        self.device = get_torch_device(use_gpu)
        logger.info("Mask R-CNN using device: %s", self.device)

        self.model = detection_models.maskrcnn_resnet50_fpn_v2(weights=None)
        state_dict = torch.load(weights_path, map_location="cpu", weights_only=True)
        self.model.load_state_dict(state_dict)
        self.model.to(self.device)
        self.model.eval()

        # Warm up GPU with a small dummy tensor
        if self.device.type != "cpu":
            try:
                dummy = torch.randn(3, 64, 64, device=self.device)
                with torch.no_grad():
                    self.model([dummy])
                logger.info("Mask R-CNN GPU warm-up complete")
            except Exception as e:
                logger.warning("GPU warm-up failed, falling back to CPU: %s", e)
                self.device = torch.device("cpu")
                self.model.to(self.device)

    def get_prediction(self, image_data, threshold=0.5):
        try:
            transform = T.Compose([T.ToTensor()])
            img = transform(image_data).to(self.device)
            with torch.no_grad():
                pred = self.model([img])
            pred_score = pred[0]["scores"].detach().cpu().numpy().tolist()
            if not any(s > threshold for s in pred_score):
                return None, None, None, None
            pred_t = max(i for i, s in enumerate(pred_score) if s > threshold)
            masks = (pred[0]["masks"] > 0.5).squeeze(1).detach().cpu().numpy()
            if len(masks.shape) == 2:
                masks = np.expand_dims(masks, axis=0)
            pred_class = [self.COCO_INSTANCE_CATEGORY_NAMES[i] for i in pred[0]["labels"].cpu().numpy()]
            pred_boxes = [[(i[0], i[1]), (i[2], i[3])] for i in pred[0]["boxes"].detach().cpu().numpy()]
            masks = masks[: pred_t + 1]
            pred_boxes = pred_boxes[: pred_t + 1]
            pred_class = pred_class[: pred_t + 1]
            return masks, pred_boxes, pred_class, pred_score[: pred_t + 1]
        except Exception as e:
            logger.error("Mask R-CNN prediction failed: %s", e)
            return [], [], [], []

    @staticmethod
    def _center_of_mass(mask):
        y, x = np.where(mask > 0)
        if len(x) == 0:
            # Empty mask — return center of image
            return (mask.shape[1] // 2, mask.shape[0] // 2)
        return (int(np.mean(x)), int(np.mean(y)))

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
        center = self._center_of_mass(mask)

        def fraction_inside(center_of_mass, S):
            x_min = int(center_of_mass[0] - S / 2)
            x_max = int(center_of_mass[0] + S / 2)
            y_min = int(center_of_mass[1] - S / 2)
            y_max = int(center_of_mass[1] + S / 2)
            x_min2 = max(0, x_min)
            x_max2 = min(mask.shape[1], x_max)
            y_min2 = max(0, y_min)
            y_max2 = min(mask.shape[0], y_max)
            total = np.sum(mask)
            if total == 0:
                return 1.0
            return np.sum(mask[y_min2:y_max2, x_min2:x_max2]) / total

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
        x_min, x_max, y_min, y_max = self._get_bounding_box(mask)
        crop = img[y_min:y_max, x_min:x_max]
        mask_crop = mask[y_min:y_max, x_min:x_max]
        if resize:
            crop = cv2.resize(crop, (1024, 1024))
            mask_crop = cv2.resize(mask_crop.astype(np.uint8), (1024, 1024))
        return crop, mask_crop

    @staticmethod
    def get_species_crop(box, img):
        xmin = int(box[0][0])
        ymin = int(box[0][1])
        xmax = int(box[1][0])
        ymax = int(box[1][1])
        return img[ymin:ymax, xmin:xmax]

    @staticmethod
    def filter_overlapping_detections(masks, pred_boxes, pred_class, pred_score, iou_threshold=0.5):
        """Remove lower-confidence detections that overlap significantly with higher-confidence ones."""
        if masks is None or len(masks) == 0:
            return masks, pred_boxes, pred_class, pred_score

        n = len(pred_score)
        keep = [True] * n
        # Sort indices by score descending
        sorted_indices = sorted(range(n), key=lambda i: pred_score[i], reverse=True)

        for i_idx, i in enumerate(sorted_indices):
            if not keep[i]:
                continue
            for j in sorted_indices[i_idx + 1:]:
                if not keep[j]:
                    continue
                # Compute mask IoU
                intersection = np.logical_and(masks[i], masks[j]).sum()
                union = np.logical_or(masks[i], masks[j]).sum()
                if union > 0 and intersection / union > iou_threshold:
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
