import cv2
import numpy as np


def compute_capture_time_gap(path1, path2):
    """
    Return the absolute capture-time gap between two image files in seconds,
    or None if the timestamp could not be read for either file.

    Both scene rules keyed on capture time — the "same burst" merge and the
    long-gap scene break — read the same pair of EXIF timestamps, so they share
    this one call rather than decoding the headers twice per image.
    """
    try:
        try:
            from .raw_exif import get_capture_time
        except ImportError:
            from raw_exif import get_capture_time
        t1 = get_capture_time(path1)
        t2 = get_capture_time(path2)
        return abs((t1 - t2).total_seconds())
    except Exception:
        return None


def compute_similarity_timestamp(path1, path2, threshold_seconds: float = 1.0):
    """
    Return True if two image files were captured within threshold_seconds of each other,
    False if they were not, or None if timestamps could not be read for either file.
    """
    gap = compute_capture_time_gap(path1, path2)
    if gap is None:
        return None
    return gap <= threshold_seconds


def compute_image_similarity_akaze(img1, img2, max_dim=1600):
    if img1 is None or img2 is None or img1.shape != img2.shape:
        return {
            "feature_similarity": -1,
            "feature_confidence": -1,
            "color_similarity": -1,
            "color_confidence": -1,
            "similar": False,
            "confidence": 0,
        }
    try:
        def clamp(value, low, high):
            return float(max(low, min(high, value)))

        def resize(img):
            h, w = img.shape[:2]
            scale = max_dim / max(h, w)
            if scale < 1.0:
                img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
            return img

        def color_similarity_score(a, b):
            # Histogram-based color similarity is much more stable under blur than mean-RGB deltas.
            target_w = 320
            ah, aw = a.shape[:2]
            bh, bw = b.shape[:2]
            a_small = cv2.resize(a, (target_w, max(1, int(target_w * ah / max(aw, 1)))), interpolation=cv2.INTER_AREA)
            b_small = cv2.resize(b, (target_w, max(1, int(target_w * bh / max(bw, 1)))), interpolation=cv2.INTER_AREA)

            hsv1 = cv2.cvtColor(a_small, cv2.COLOR_RGB2HSV)
            hsv2 = cv2.cvtColor(b_small, cv2.COLOR_RGB2HSV)

            hist1 = cv2.calcHist([hsv1], [0, 1, 2], None, [18, 16, 16], [0, 180, 0, 256, 0, 256])
            hist2 = cv2.calcHist([hsv2], [0, 1, 2], None, [18, 16, 16], [0, 180, 0, 256, 0, 256])
            cv2.normalize(hist1, hist1, alpha=1.0, norm_type=cv2.NORM_L1)
            cv2.normalize(hist2, hist2, alpha=1.0, norm_type=cv2.NORM_L1)

            # Bhattacharyya distance in [0, 1] for normalized histograms.
            bhatta = float(cv2.compareHist(hist1, hist2, cv2.HISTCMP_BHATTACHARYYA))
            hist_sim = clamp(1.0 - bhatta, 0.0, 1.0)

            lab1 = cv2.cvtColor(a_small, cv2.COLOR_RGB2LAB)
            lab2 = cv2.cvtColor(b_small, cv2.COLOR_RGB2LAB)
            l1 = np.mean(lab1.reshape(-1, 3), axis=0)
            l2 = np.mean(lab2.reshape(-1, 3), axis=0)
            mean_delta = float(np.linalg.norm(l1 - l2))
            mean_sim = clamp(1.0 - (mean_delta / 130.0), 0.0, 1.0)

            sat_std = max(float(np.std(hsv1[:, :, 1])), float(np.std(hsv2[:, :, 1])))
            val_std = max(float(np.std(hsv1[:, :, 2])), float(np.std(hsv2[:, :, 2])))
            color_conf = clamp((0.65 * sat_std + 0.35 * val_std) / 55.0, 0.25, 1.0)

            color_sim = 0.75 * hist_sim + 0.25 * mean_sim
            return float(color_sim), float(color_conf)

        img1 = resize(img1)
        img2 = resize(img2)
        gray1 = cv2.cvtColor(img1, cv2.COLOR_RGB2GRAY) if img1.ndim == 3 else img1
        gray2 = cv2.cvtColor(img2, cv2.COLOR_RGB2GRAY) if img2.ndim == 3 else img2
        akaze = cv2.AKAZE_create()
        kp1, des1 = akaze.detectAndCompute(gray1, None)
        kp2, des2 = akaze.detectAndCompute(gray2, None)
        if des1 is not None and len(kp1) > 300:
            kp1, des1 = zip(*sorted(zip(kp1, des1), key=lambda x: x[0].response, reverse=True)[:300])
            kp1 = list(kp1)
            des1 = np.array(des1)
        if des2 is not None and len(kp2) > 300:
            kp2, des2 = zip(*sorted(zip(kp2, des2), key=lambda x: x[0].response, reverse=True)[:300])
            kp2 = list(kp2)
            des2 = np.array(des2)
        feature_confidence = min(len(kp1), len(kp2)) / 300 if kp1 and kp2 else 0
        if feature_confidence < 0.25 or des1 is None or des2 is None or len(kp1) == 0 or len(kp2) == 0:
            color_similarity, color_confidence = color_similarity_score(img1, img2)
            return {
                "feature_similarity": 0,
                "feature_confidence": 0,
                "color_similarity": float(color_similarity),
                "color_confidence": float(color_confidence),
                "similar": bool(color_similarity >= 0.82),
                "confidence": float(color_confidence),
            }

        bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        matches = bf.knnMatch(des1, des2, k=2)
        valid_pairs = [pair for pair in matches if len(pair) == 2]
        if not valid_pairs:
            color_similarity, color_confidence = color_similarity_score(img1, img2)
            return {
                "feature_similarity": 0,
                "feature_confidence": 0,
                "color_similarity": float(color_similarity),
                "color_confidence": float(color_confidence),
                "similar": bool(color_similarity >= 0.82),
                "confidence": float(color_confidence),
            }

        m_arr = np.array([m.distance for m, n in valid_pairs])
        n_arr = np.array([n.distance for m, n in valid_pairs])
        good_mask = m_arr < 0.7 * n_arr
        feature_similarity = np.sum(good_mask) / ((len(kp1) + len(kp2)) / 2) if (len(kp1) + len(kp2)) > 0 else 0
        similar = feature_similarity >= 0.05
        return {
            "feature_similarity": float(feature_similarity),
            "feature_confidence": float(feature_confidence),
            "color_similarity": 0,
            "color_confidence": 0,
            "similar": bool(similar),
            "confidence": float(feature_confidence),
        }
    except Exception:
        return {
            "feature_similarity": -1,
            "feature_confidence": -1,
            "color_similarity": -1,
            "color_confidence": -1,
            "similar": False,
            "confidence": 0,
        }
