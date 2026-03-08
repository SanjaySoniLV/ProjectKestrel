"""Image detail and sharpness analysis for quality assessment.

Provides multiple metrics to evaluate image sharpness and detail level:
- Laplacian variance: measures edge sharpness (blur detection)
- Local contrast: measures micro-contrast in small patches
- Texture complexity: measures fine detail via frequency analysis
- Masked variants: compute metrics only within the subject mask

These metrics are combined into a composite detail score that feeds
into the rating system, ensuring only truly sharp images earn 5 stars.
"""

import logging
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class DetailMetrics:
    """Container for image detail analysis results."""
    laplacian_variance: float     # Edge sharpness (higher = sharper)
    local_contrast: float         # Micro-contrast score (0-1)
    texture_complexity: float     # Fine detail via high-frequency energy (0-1)
    edge_density: float           # Proportion of strong edges (0-1)
    detail_score: float           # Combined normalised score (0-1)
    sharpness_score: float        # Final sharpness score for rating (0-1)


def compute_laplacian_variance(gray: np.ndarray, mask: np.ndarray = None) -> float:
    """Compute variance of Laplacian as a sharpness metric.

    High variance = sharp image with well-defined edges.
    Low variance = blurry image.
    """
    laplacian = cv2.Laplacian(gray, cv2.CV_64F)
    if mask is not None and mask.any():
        mask_bool = mask.astype(bool)
        values = laplacian[mask_bool]
        if len(values) == 0:
            return 0.0
        return float(np.var(values))
    return float(np.var(laplacian))


def compute_local_contrast(gray: np.ndarray, mask: np.ndarray = None, patch_size: int = 16) -> float:
    """Compute local contrast as the mean standard deviation of small patches.

    Measures micro-contrast - how much tonal variation exists in local regions.
    High values indicate good detail and texture.
    """
    h, w = gray.shape[:2]
    stds = []

    for y in range(0, h - patch_size, patch_size):
        for x in range(0, w - patch_size, patch_size):
            patch = gray[y:y + patch_size, x:x + patch_size]
            if mask is not None:
                mask_patch = mask[y:y + patch_size, x:x + patch_size]
                if mask_patch.sum() < patch_size * patch_size * 0.5:
                    continue
            std = np.std(patch.astype(np.float32))
            stds.append(std)

    if not stds:
        return 0.0

    mean_std = float(np.mean(stds))
    # Normalise: typical sharp wildlife photo has mean local std ~25-40
    return min(1.0, mean_std / 40.0)


def compute_texture_complexity(gray: np.ndarray, mask: np.ndarray = None) -> float:
    """Compute texture complexity via high-frequency energy in DCT domain.

    Applies DCT on small blocks and measures the proportion of energy
    in high-frequency coefficients. High values = fine detail present.
    """
    h, w = gray.shape[:2]
    block_size = 32

    # Work on a resized version for speed
    target_size = 512
    scale = target_size / max(h, w)
    if scale < 1.0:
        gray_small = cv2.resize(gray, (int(w * scale), int(h * scale)))
        if mask is not None:
            mask_small = cv2.resize(mask.astype(np.uint8), (int(w * scale), int(h * scale)))
        else:
            mask_small = None
    else:
        gray_small = gray
        mask_small = mask

    h2, w2 = gray_small.shape[:2]
    hf_ratios = []

    for y in range(0, h2 - block_size, block_size):
        for x in range(0, w2 - block_size, block_size):
            if mask_small is not None:
                mp = mask_small[y:y + block_size, x:x + block_size]
                if mp.sum() < block_size * block_size * 0.5:
                    continue

            block = gray_small[y:y + block_size, x:x + block_size].astype(np.float32)
            dct = cv2.dct(block)
            total_energy = np.sum(dct ** 2)
            if total_energy < 1e-6:
                continue

            # High-frequency: bottom-right quadrant of DCT
            hf_energy = np.sum(dct[block_size // 2:, block_size // 2:] ** 2)
            hf_ratios.append(hf_energy / total_energy)

    if not hf_ratios:
        return 0.0

    # Normalise: typical sharp detail has HF ratio ~0.05-0.15
    mean_hf = float(np.mean(hf_ratios))
    return min(1.0, mean_hf / 0.12)


def compute_edge_density(gray: np.ndarray, mask: np.ndarray = None) -> float:
    """Compute the density of strong edges as a proportion of total pixels.

    Uses Canny edge detection. Higher density suggests more detail and structure.
    """
    edges = cv2.Canny(gray, 50, 150)

    if mask is not None and mask.any():
        edges = cv2.bitwise_and(edges, edges, mask=mask.astype(np.uint8))
        total_pixels = mask.sum()
    else:
        total_pixels = edges.size

    if total_pixels == 0:
        return 0.0

    edge_pixels = (edges > 0).sum()
    # Normalise: typical sharp wildlife photo has edge density ~0.05-0.15
    density = edge_pixels / total_pixels
    return min(1.0, float(density) / 0.12)


def analyse_detail(
    image: np.ndarray,
    mask: np.ndarray = None,
    crop_size: int = 1024,
) -> DetailMetrics:
    """Perform comprehensive detail analysis on an image crop.

    Args:
        image: RGB image array (H, W, 3), ideally the subject crop
        mask: Optional binary mask of the subject (same size as image)
        crop_size: resize dimension for analysis consistency

    Returns:
        DetailMetrics with individual and combined scores
    """
    # Resize for consistent analysis
    h, w = image.shape[:2]
    if max(h, w) != crop_size:
        scale = crop_size / max(h, w)
        image = cv2.resize(image, (int(w * scale), int(h * scale)))
        if mask is not None:
            mask = cv2.resize(mask.astype(np.uint8), (int(w * scale), int(h * scale)))

    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)

    # Compute individual metrics
    lap_var = compute_laplacian_variance(gray, mask)
    local_con = compute_local_contrast(gray, mask)
    texture = compute_texture_complexity(gray, mask)
    edge_den = compute_edge_density(gray, mask)

    # Normalise Laplacian variance (typical range for wildlife: 50-2000+)
    lap_normalised = min(1.0, lap_var / 1500.0)

    # Combined detail score: weighted average of all metrics
    # Sharpness (Laplacian) weighted highest as it's the best blur detector
    detail_score = (
        0.35 * lap_normalised +
        0.25 * local_con +
        0.20 * texture +
        0.20 * edge_den
    )

    # Sharpness score: heavily emphasises actual sharpness over detail quantity
    # This is used for the "only sharpest get 5 stars" requirement
    sharpness_score = (
        0.50 * lap_normalised +
        0.30 * local_con +
        0.20 * edge_den
    )

    return DetailMetrics(
        laplacian_variance=lap_var,
        local_contrast=local_con,
        texture_complexity=texture,
        edge_density=edge_den,
        detail_score=round(detail_score, 4),
        sharpness_score=round(sharpness_score, 4),
    )
