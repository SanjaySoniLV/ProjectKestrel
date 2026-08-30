RATING_PROFILES = {
    'very_strict': {'five': 0.93, 'four': 0.82, 'three': 0.58, 'two': 0.28},
    'strict':      {'five': 0.90, 'four': 0.72, 'three': 0.48, 'two': 0.20},
    'balanced':    {'five': 0.85, 'four': 0.60, 'three': 0.40, 'two': 0.15},
    'lenient':     {'five': 0.78, 'four': 0.53, 'three': 0.32, 'two': 0.12},
    'very_lenient':{'five': 0.70, 'four': 0.45, 'three': 0.25, 'two': 0.10},
}


# Profile name for user-defined thresholds. Its values do not live in
# RATING_PROFILES — they come from the ``rating_thresholds_custom`` setting and
# are resolved through ``resolve_thresholds``.
CUSTOM_PROFILE = 'custom'

# High-to-low, which is also the order quality_to_rating tests them in.
THRESHOLD_KEYS = ('five', 'four', 'three', 'two')

# Star bands must stay distinct: if two cutoffs collapse onto the same value a
# whole star level becomes unreachable, so dragging one handle past another
# pushes the neighbour along instead of swallowing it.
MIN_THRESHOLD_GAP = 0.01


def get_profile_thresholds(profile: str) -> dict:
    """Return threshold dict for a named rating profile, defaulting to 'balanced'."""
    return RATING_PROFILES.get(str(profile).lower(), RATING_PROFILES['balanced'])


def normalize_custom_thresholds(raw, fallback: dict = None) -> dict:
    """Coerce user-supplied thresholds into a valid, strictly-descending set.

    Accepts anything (including ``None`` or junk from a hand-edited
    settings.json) and always returns a usable dict. Values are clamped to
    [0, 1]; any that are missing or unparseable fall back to the corresponding
    entry in ``fallback`` (default: the 'balanced' profile). The result is then
    forced strictly descending by ``MIN_THRESHOLD_GAP`` from 'five' down, so
    every star band stays reachable.
    """
    base = dict(fallback or RATING_PROFILES['balanced'])
    src = raw if isinstance(raw, dict) else {}

    values = {}
    for key in THRESHOLD_KEYS:
        raw_v = src.get(key)
        # bool is a subclass of int, so float(True) would quietly become a 1.0
        # cutoff. Treat it as missing, matching the frontend's normalizer —
        # the two have to agree or the editor shows bands the pipeline won't use.
        if isinstance(raw_v, bool):
            raw_v = None
        try:
            v = float(raw_v)
        except (TypeError, ValueError):
            v = float(base.get(key, RATING_PROFILES['balanced'][key]))
        if v != v:  # NaN
            v = float(base.get(key, RATING_PROFILES['balanced'][key]))
        values[key] = max(0.0, min(1.0, v))

    # Walk high-to-low, pushing each cutoff below the one above it.
    ceiling = 1.0
    for key in THRESHOLD_KEYS:
        ceiling = round(min(values[key], ceiling), 4)
        values[key] = ceiling
        ceiling = round(ceiling - MIN_THRESHOLD_GAP, 4)

    # A run of collisions near 0 can drive later keys negative; lift the whole
    # tail back into range while keeping the gaps.
    floor = 0.0
    for key in reversed(THRESHOLD_KEYS):
        if values[key] < floor:
            values[key] = round(floor, 4)
        floor = round(values[key] + MIN_THRESHOLD_GAP, 4)

    return values


def resolve_thresholds(profile: str, custom=None) -> dict:
    """Return the thresholds actually in force for ``profile``.

    Named profiles come from ``RATING_PROFILES``; the 'custom' profile is
    resolved from ``custom`` (the ``rating_thresholds_custom`` setting).
    """
    if str(profile).lower() == CUSTOM_PROFILE:
        return normalize_custom_thresholds(custom)
    return get_profile_thresholds(profile)


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
