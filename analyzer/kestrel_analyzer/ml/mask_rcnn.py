import time
from pathlib import Path

import cv2
import numpy as np
import torch
import torchvision.models.detection as detection_models
import torchvision.transforms.v2 as T

from ..config import MASK_RCNN_WEIGHTS_PATH


class MaskRCNNWrapper:
    def __init__(self):
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

        self.model = detection_models.maskrcnn_resnet50_fpn_v2(weights=None)
        state_dict = torch.load(weights_path, map_location="cpu", weights_only=True)
        self.model.load_state_dict(state_dict)
        self.model.eval()

    def get_prediction(self, image_data, threshold=0.75, mask_threshold=0.5):
        """Get predictions from the model.
        
        Args:
            image_data: Input image array (RGB).
            threshold: Detection confidence threshold (0.1-0.99). Objects with lower confidence are filtered.
            mask_threshold: Pixel confidence threshold for mask segmentation (0.5-0.95). 
                          Controls tightness of bird masks. Higher = tighter masks.
        """
        mask_threshold = max(0.5, min(0.95, float(mask_threshold)))
        for attempt in range(3):
            try:
                transform = T.Compose([T.ToImage(), T.ToDtype(torch.float32, scale=True)])
                img = transform(image_data)
                with torch.no_grad():
                    pred = self.model([img])
                pred_score = list(pred[0]["scores"].detach().numpy())
                if (np.array(pred_score) > threshold).sum() == 0:
                    return None, None, None, None
                pred_t = [pred_score.index(x) for x in pred_score if x > threshold][-1]
                masks = (pred[0]["masks"] > mask_threshold).squeeze().detach().cpu().numpy()
                if len(masks.shape) == 2:
                    masks = np.expand_dims(masks, axis=0)
                pred_class = [self.COCO_INSTANCE_CATEGORY_NAMES[i] for i in list(pred[0]["labels"].numpy())]
                pred_boxes = [[(i[0], i[1]), (i[2], i[3])] for i in list(pred[0]["boxes"].detach().numpy())]
                masks = masks[: pred_t + 1]
                pred_boxes = pred_boxes[: pred_t + 1]
                pred_class = pred_class[: pred_t + 1]
                pred_score = pred_score[: pred_t + 1]
                return self.filter_overlapping_detections(masks, pred_boxes, pred_class, pred_score)
            except Exception as e:
                if attempt < 2:
                    print(f"Prediction attempt {attempt + 1} failed: {e}. Retrying...")
                    time.sleep(0.1)
                else:
                    print("Error occurred while getting prediction after 3 attempts:", e)
        return [], [], [], []

    @staticmethod
    def _center_of_mass(mask):
        y, x = np.where(mask > 0)
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
            return np.sum(mask[y_min2:y_max2, x_min2:x_max2]) / np.sum(mask)

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
