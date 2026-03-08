"""Quality score to star rating conversion.

Rating philosophy:
- 5 stars: Exceptional sharpness AND detail. Only the very best images.
  The subject must be tack-sharp with fine feather detail visible.
- 4 stars: Very good quality. Sharp with good detail.
- 3 stars: Acceptable quality. Usable but not outstanding.
- 2 stars: Below average. Noticeable softness or limited detail.
- 1 star: Poor quality. Significant blur, noise, or lack of detail.
- 0: Unrated / error during processing.

The rating combines the ML quality score with the detail/sharpness analysis
to ensure that 5-star ratings are reserved for truly sharp images.
"""


def quality_to_rating(quality_score: float, sharpness_score: float = -1.0, detail_score: float = -1.0) -> int:
    """Convert quality and sharpness scores to a 1-5 star rating.

    Args:
        quality_score: ML model quality score (0.0 to 1.0, or -1 for error)
        sharpness_score: Sharpness analysis score (0.0 to 1.0, or -1 if unavailable)
        detail_score: Detail analysis score (0.0 to 1.0, or -1 if unavailable)

    Returns:
        Star rating from 0 (unrated) to 5 (exceptional).
    """
    if quality_score == -1:
        return 0

    # If we have sharpness data, use combined scoring
    if sharpness_score >= 0 and detail_score >= 0:
        return _combined_rating(quality_score, sharpness_score, detail_score)

    # Fallback: quality-only rating (stricter thresholds for 5 stars)
    return _quality_only_rating(quality_score)


def _combined_rating(quality: float, sharpness: float, detail: float) -> int:
    """Rating using all three metrics. 5 stars requires excellence across all."""
    # Weighted composite: quality model is primary, sharpness gates the top rating
    composite = 0.50 * quality + 0.30 * sharpness + 0.20 * detail

    # 5 stars: composite must be very high AND sharpness must be excellent
    # This ensures only genuinely sharp images reach the top tier
    if composite >= 0.82 and sharpness >= 0.70 and quality >= 0.85:
        return 5

    if composite >= 0.65 and sharpness >= 0.45:
        return 4

    if composite >= 0.40:
        return 3

    if composite >= 0.20:
        return 2

    return 1


def _quality_only_rating(quality: float) -> int:
    """Fallback rating using only the ML quality score.

    Stricter than the original thresholds - 5 stars requires near-perfect score.
    """
    if quality >= 0.92:
        return 5
    if quality >= 0.70:
        return 4
    if quality >= 0.40:
        return 3
    if quality >= 0.18:
        return 2
    return 1
