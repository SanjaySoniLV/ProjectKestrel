RATING_PROFILES = {
    'very_strict': {'five': 0.93, 'four': 0.82, 'three': 0.58, 'two': 0.28},
    'strict':      {'five': 0.90, 'four': 0.72, 'three': 0.48, 'two': 0.20},
    'balanced':    {'five': 0.85, 'four': 0.60, 'three': 0.40, 'two': 0.15},
    'lenient':     {'five': 0.78, 'four': 0.53, 'three': 0.32, 'two': 0.12},
    'very_lenient':{'five': 0.70, 'four': 0.45, 'three': 0.25, 'two': 0.10},
}


def get_profile_thresholds(profile: str) -> dict:
    """Return threshold dict for a named rating profile, defaulting to 'balanced'."""
    return RATING_PROFILES.get(str(profile).lower(), RATING_PROFILES['balanced'])


def quality_to_rating(q: float, thresholds: dict = None) -> int:
    """Map a percentile-normalized quality score (0.0-1.0) to 1-5 stars.

    Thresholds use absolute quality-score cutoffs:
        {'five': 0.85, 'four': 0.60, 'three': 0.40, 'two': 0.15}
    """
    try:
        q_f = float(q)
    except (TypeError, ValueError):
        return 0
    if q_f < 0:
        return 0

    if thresholds is None:
        thresholds = RATING_PROFILES['balanced']

    t5 = float(thresholds.get('five', 0.85))
    t4 = float(thresholds.get('four', 0.60))
    t3 = float(thresholds.get('three', 0.40))
    t2 = float(thresholds.get('two', 0.15))

    if q_f >= t5:
        return 5
    if q_f >= t4:
        return 4
    if q_f >= t3:
        return 3
    if q_f >= t2:
        return 2
    return 1


def get_image_display_rating(
    filename: str,
    quality: float,
    user_image_ratings: dict,
    thresholds: dict = None,
) -> tuple:
    """Return (rating, origin) for display, preferring user-specified over auto-computed.

    Args:
        filename: Image filename (key into user_image_ratings).
        quality: Raw quality score from analysis pipeline.
        user_image_ratings: Dict mapping filename -> int (from kestrel_scenedata.json).
        thresholds: Optional threshold dict (see quality_to_rating). Defaults to 'balanced'.

    Returns:
        (rating: int 0-5, origin: str 'manual' | 'auto')
    """
    if filename in user_image_ratings:
        r = user_image_ratings[filename]
        try:
            return max(0, min(5, int(r))), "manual"
        except (TypeError, ValueError):
            pass
    return quality_to_rating(quality, thresholds), "auto"
