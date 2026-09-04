import csv

import cv2
import numpy as np

from ..logging_utils import debug
from .provider_coordinator import ProviderCoordinator
from .resilient_session import ResilientOnnxSession


class QualityClassifier:
    def __init__(
        self,
        model_path: str,
        normalization_data_path: str = None,
        *,
        coord: ProviderCoordinator,
    ):
        # Registered with the coordinator so demote/promote rebuilds this
        # session alongside the wrapper's. See note in BirdSpeciesClassifier.
        self.session = ResilientOnnxSession("quality", model_path, coord)
        self.providers_used = list(self.session.get_providers())
        _active = self.providers_used[0] if self.providers_used else "unknown"
        debug(f"[QualityClassifier] Active provider: {_active}  all providers: {self.providers_used}")

        self._input_name = self.session.get_inputs()[0].name

        self._norm_qualities = None
        self._norm_percentiles = None
        if normalization_data_path:
            self._load_normalization_data(normalization_data_path)

    def _load_normalization_data(self, normalization_data_path: str) -> None:
        """Load the percentile curve. A given path must yield at least one row.

        Missing, unreadable, or header-only files used to be swallowed in
        ``__init__``, leaving ``_norm_qualities`` as None. ``classify`` then
        returned the raw model score instead of a 0–1 percentile, so star
        ratings were wrong with no error.
        """
        rows = []
        with open(normalization_data_path, "r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    p = float(row.get("percentile", ""))
                    q = float(row.get("quality", ""))
                except (TypeError, ValueError):
                    continue
                if not np.isfinite(p) or not np.isfinite(q):
                    continue
                if p < 0.0 or p > 100.0:
                    continue
                rows.append((q, p / 100.0))
        if not rows:
            raise ValueError(
                "quality normalization CSV has no valid percentile/quality "
                f"rows: {normalization_data_path}"
            )
        rows.sort(key=lambda x: x[0])
        self._norm_qualities = np.array([q for q, _ in rows], dtype=np.float32)
        self._norm_percentiles = np.array([p for _, p in rows], dtype=np.float32)

    def _normalize_quality_to_percentile(self, quality: float) -> float:
        if quality < 0:
            return quality
        if self._norm_qualities is None or self._norm_percentiles is None:
            return quality

        q = float(quality)
        qualities = self._norm_qualities
        percentiles = self._norm_percentiles

        if q <= qualities[0]:
            return float(percentiles[0])
        if q >= qualities[-1]:
            return float(percentiles[-1])

        idx = int(np.searchsorted(qualities, q, side="right"))
        q0 = float(qualities[idx - 1])
        q1 = float(qualities[idx])
        p0 = float(percentiles[idx - 1])
        p1 = float(percentiles[idx])

        if q1 <= q0:
            return p1
        t = (q - q0) / (q1 - q0)
        return p0 + t * (p1 - p0)

    @staticmethod
    def _preprocess(cropped_img, cropped_mask):
        img = cv2.cvtColor(cropped_img, cv2.COLOR_RGB2GRAY)
        sobel_x = cv2.Sobel(img, cv2.CV_32F, 1, 0, ksize=5)
        sobel_y = cv2.Sobel(img, cv2.CV_32F, 0, 1, ksize=5)
        img = np.sqrt(sobel_x ** 2 + sobel_y ** 2)
        img1 = cv2.bitwise_and(img, img, mask=cropped_mask.astype(np.uint8))
        images = np.array([img1]).transpose(1, 2, 0)
        return images

    def classify(self, cropped_image, cropped_mask):
        try:
            input_data = self._preprocess(cropped_image, cropped_mask)
            input_tensor = np.expand_dims(input_data, axis=0).astype(np.float32)
        except Exception:
            # Malformed crop/mask (None, unexpected shape/dtype) is a per-image
            # data problem, not a model failure: return the "unrated" sentinel
            # and let the pipeline continue. An all-zero mask is valid input
            # to _preprocess (bitwise_and zeros the image) and does not raise.
            return -1.0

        # Do NOT wrap session.run() in a blanket except. A dead GPU/ONNX session
        # (e.g. DirectML/CoreML device lost) must propagate so the pipeline's
        # per-image handler records the failure and the provider coordinator can
        # demote to CPU on the next image. Swallowing it here returned -1.0 for
        # every remaining image and silently corrupted quality scores while the
        # run still reported "complete".
        outputs = self.session.run(None, {self._input_name: input_tensor})

        try:
            raw_quality = float(outputs[0][0][0])
            return self._normalize_quality_to_percentile(raw_quality)
        except Exception:
            # Unexpected output shape / normalization issue -> unrated, but not a
            # reason to abort the whole image.
            return -1.0
