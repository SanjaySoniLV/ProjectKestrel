import logging

import cv2
import numpy as np
import tensorflow as tf

from ..gpu_utils import configure_tensorflow_gpu

logger = logging.getLogger(__name__)


class QualityClassifier:
    def __init__(self, model_path: str, use_gpu: bool = False):
        configure_tensorflow_gpu(use_gpu)
        self.model = tf.keras.models.load_model(model_path)
        logger.info("Quality model loaded (GPU: %s)", use_gpu)

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
            output_value = self.model.predict(np.expand_dims(input_data, axis=0), verbose=0)
            return float(output_value[0][0])
        except Exception as e:
            logger.error("Quality classification failed: %s", e)
            return -1.0
