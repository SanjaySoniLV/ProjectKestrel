"""Re-group an already-analyzed folder into scenes without re-running analysis.

The pipeline decides scene boundaries as it walks the folder, and the values it
based each decision on are written to ``kestrel_database.csv`` alongside the
result: ``capture_time``, ``orientation`` and the raw AKAZE / colour similarity
scores. That means the boundaries can be recomputed from the database alone —
no image decode, no GPU, and no dependence on where the analysis ran. A folder
analyzed in the cloud regroups exactly like a folder analyzed locally.

The rule order here mirrors ``AnalysisPipeline.process_folder`` exactly, so
regrouping with the settings a folder was analyzed with is a no-op:

  1. Orientation changed (both known, and different)   -> break
  2. Capture gap longer than ``split_after_seconds``   -> break
  3. Capture gap within ``group_within_seconds``       -> same scene
  4. A stored similarity score exists                  -> compare to the cut
  5. No stored score                                   -> same scene

Step 5 is the one place regrouping is weaker than a fresh analysis, and it is
worth understanding. The pipeline skips AKAZE whenever the burst rule (step 3)
already decided the pair, and writes ``-1`` sentinels for that row. Those pairs
therefore have no appearance score to re-threshold. Treating them as "same
scene" keeps the grouping the analysis produced: regrouping never invents a
split it has no evidence for. :func:`count_scored_pairs` exists so the UI can
say how many pairs the similarity control actually reaches.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable, Sequence

# Cuts the pipeline hard-codes in ``compute_image_similarity_akaze``. A
# similarity control of 0.5 reproduces them, so "as analyzed" sits at the
# middle of the range.
DEFAULT_FEATURE_CUT = 0.05
DEFAULT_COLOR_CUT = 0.82

# Top of each scale at control = 1.0. The feature ceiling is set from the
# observed spread of stored scores: pairs the pipeline called different land
# at 0.00-0.02, pairs it called the same run 0.11-0.61, so 0.60 is "only
# near-duplicates stay together" without being unreachable. Colour similarity
# is a normalized 0-1 score, so its ceiling is simply 1.0.
MAX_FEATURE_CUT = 0.60
MAX_COLOR_CUT = 1.0

# Break reasons, returned per image so the preview can explain each boundary.
REASON_FIRST = "first"
REASON_ORIENTATION = "orientation"
REASON_TIME_GAP = "time-gap"
REASON_APPEARANCE = "appearance"
REASON_CONTINUES = ""


def map_similarity_threshold(control: float) -> tuple[float, float]:
    """Map a 0-1 UI control onto the two similarity cuts the pipeline uses.

    The control is piecewise-linear with the pipeline's own cuts pinned at
    0.5, so the midpoint means "group exactly the way the analysis did":

        0.0  -> 0.00 / 0.00   never split on appearance
        0.5  -> 0.05 / 0.82   as analyzed
        1.0  -> 0.60 / 1.00   only near-duplicates stay together

    Higher control = images must look more alike to stay in one scene = more
    scenes, which is the direction the label in the dialog promises.
    """
    try:
        t = float(control)
    except (TypeError, ValueError):
        t = 0.5
    if t != t:  # NaN
        t = 0.5
    t = max(0.0, min(1.0, t))

    if t <= 0.5:
        scale = t / 0.5
        return DEFAULT_FEATURE_CUT * scale, DEFAULT_COLOR_CUT * scale
    scale = (t - 0.5) / 0.5
    feature = DEFAULT_FEATURE_CUT + scale * (MAX_FEATURE_CUT - DEFAULT_FEATURE_CUT)
    color = DEFAULT_COLOR_CUT + scale * (MAX_COLOR_CUT - DEFAULT_COLOR_CUT)
    return feature, color


def _as_float(value: Any, default: float = -1.0) -> float:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return f


def parse_capture_time(value: Any) -> datetime | None:
    """Parse a ``capture_time`` cell. Returns None for blank/unparseable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in ("nan", "none"):
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def capture_gap_seconds(prev: Any, cur: Any) -> float | None:
    """Absolute gap in seconds between two ``capture_time`` cells, or None."""
    a = parse_capture_time(prev)
    b = parse_capture_time(cur)
    if a is None or b is None:
        return None
    # Mixed naive/aware timestamps cannot be subtracted; a folder shot on one
    # camera never mixes them, but a repaired or hand-edited CSV might.
    if (a.tzinfo is None) != (b.tzinfo is None):
        return None
    return abs((b - a).total_seconds())


def stored_similarity(row: Any) -> tuple[str, float] | None:
    """Return ``(kind, score)`` for a row's stored similarity, or None.

    ``kind`` is ``'feature'`` or ``'color'``, matching the two paths in
    ``compute_image_similarity_akaze``. The paths are told apart by
    ``color_confidence``: the feature path writes colour fields as a flat 0,
    and the colour fallback always writes a confidence of at least 0.25.
    ``-1`` in every field is the pipeline's "not computed" sentinel (a burst
    merge, the first image of a folder, or a decode error).
    """
    if row is None:
        return None
    get = row.get if hasattr(row, "get") else (lambda k, d=None: getattr(row, k, d))

    color_conf = _as_float(get("color_confidence"))
    color_sim = _as_float(get("color_similarity"))
    if color_conf > 0 and color_sim >= 0:
        return ("color", color_sim)

    feature_sim = _as_float(get("feature_similarity"))
    feature_conf = _as_float(get("feature_confidence"))
    if feature_sim >= 0 and feature_conf >= 0:
        return ("feature", feature_sim)

    return None


def _orientation(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in ("landscape", "portrait") else "unknown"


def count_scored_pairs(items: Sequence[Any]) -> tuple[int, int]:
    """Return ``(scored, unscored)`` over the adjacent pairs in ``items``.

    The first image has no predecessor and is excluded from both counts, so
    ``scored + unscored == max(0, len(items) - 1)``.
    """
    scored = 0
    unscored = 0
    for row in list(items)[1:]:
        if stored_similarity(row) is None:
            unscored += 1
        else:
            scored += 1
    return scored, unscored


def sort_items(items: Iterable[Any]) -> list[Any]:
    """Order rows the way the pipeline walked them: by filename.

    Capture time would be the more natural key for the time rules, but the
    pipeline's adjacency — and therefore every stored similarity score, which
    was computed against the *alphabetical* predecessor — is filename order.
    Regrouping on any other order would compare scores to the wrong neighbour.
    """
    def key(row: Any) -> str:
        get = row.get if hasattr(row, "get") else (lambda k, d=None: getattr(row, k, d))
        return str(get("filename", "") or "")
    return sorted(items, key=key)


def compute_groups(
    items: Sequence[Any],
    *,
    group_within_seconds: float = 1.0,
    split_after_seconds: float = 0.0,
    similarity_threshold: float = 0.5,
) -> list[dict]:
    """Assign a scene number to each row, in the given (filename) order.

    Returns one dict per input row: ``{'filename', 'scene', 'reason', 'gap'}``.
    ``scene`` counts from 1. ``reason`` is why this row *started* a new scene
    (one of the ``REASON_*`` constants) or ``''`` when it continues the
    previous one. ``gap`` is the capture gap to the predecessor in seconds, or
    None when either timestamp is unreadable.

    ``split_after_seconds`` of 0 disables the long-gap break, matching the
    pipeline's ``scene_break_gap_seconds`` default.
    """
    try:
        group_within = max(0.0, float(group_within_seconds))
    except (TypeError, ValueError):
        group_within = 1.0
    try:
        split_after = max(0.0, float(split_after_seconds))
    except (TypeError, ValueError):
        split_after = 0.0
    if split_after != split_after:  # NaN
        split_after = 0.0
    feature_cut, color_cut = map_similarity_threshold(similarity_threshold)

    out: list[dict] = []
    scene = 0
    prev = None
    for row in items:
        get = row.get if hasattr(row, "get") else (lambda k, d=None: getattr(row, k, d))
        filename = str(get("filename", "") or "")

        if prev is None:
            scene = 1
            out.append({"filename": filename, "scene": scene,
                        "reason": REASON_FIRST, "gap": None})
            prev = row
            continue

        prev_get = prev.get if hasattr(prev, "get") else (lambda k, d=None: getattr(prev, k, d))
        gap = capture_gap_seconds(prev_get("capture_time"), get("capture_time"))

        prev_o = _orientation(prev_get("orientation"))
        cur_o = _orientation(get("orientation"))
        orientation_changed = (
            prev_o != "unknown" and cur_o != "unknown" and prev_o != cur_o
        )

        reason = REASON_CONTINUES
        if orientation_changed:
            reason = REASON_ORIENTATION
        elif split_after > 0 and gap is not None and gap > split_after:
            reason = REASON_TIME_GAP
        elif gap is not None and gap <= group_within:
            reason = REASON_CONTINUES
        else:
            scored = stored_similarity(row)
            if scored is not None:
                kind, score = scored
                cut = color_cut if kind == "color" else feature_cut
                if score < cut:
                    reason = REASON_APPEARANCE
            # No stored score: the pipeline never measured this pair, so there
            # is nothing to split on. Keep them together (see module docstring).

        if reason:
            scene += 1
        out.append({"filename": filename, "scene": scene, "reason": reason, "gap": gap})
        prev = row

    return out


def summarize(items: Sequence[Any], assignment: Sequence[dict]) -> list[dict]:
    """Collapse a per-image assignment into one entry per resulting scene.

    Each entry carries the scene number, its size, the reason it began, and
    the first/last filename and capture time, which is everything the preview
    needs to render a row without shipping every filename back to the UI.
    """
    by_scene: dict[int, dict] = {}
    order: list[int] = []
    for row, a in zip(items, assignment):
        get = row.get if hasattr(row, "get") else (lambda k, d=None: getattr(row, k, d))
        scene = int(a["scene"])
        entry = by_scene.get(scene)
        if entry is None:
            entry = {
                "scene": scene,
                "size": 0,
                "reason": a.get("reason") or REASON_CONTINUES,
                "first_filename": a.get("filename") or "",
                "last_filename": a.get("filename") or "",
                "start_time": str(get("capture_time", "") or ""),
                "end_time": str(get("capture_time", "") or ""),
            }
            by_scene[scene] = entry
            order.append(scene)
        entry["size"] += 1
        entry["last_filename"] = a.get("filename") or entry["last_filename"]
        ct = str(get("capture_time", "") or "")
        if ct:
            entry["end_time"] = ct
            if not entry["start_time"]:
                entry["start_time"] = ct
    return [by_scene[s] for s in order]


def summarize_current(items: Sequence[Any]) -> list[dict]:
    """Summarize the grouping a folder already has, from its ``scene_count``.

    Used for the "current" side of the preview. Scenes are listed in the order
    their first image appears, so the two columns line up visually even though
    stored ``scene_count`` values need not be contiguous or sorted.
    """
    assignment = []
    seen: dict[str, int] = {}
    next_scene = 0
    for row in items:
        get = row.get if hasattr(row, "get") else (lambda k, d=None: getattr(row, k, d))
        raw = str(get("scene_count", "") or "").strip()
        if raw not in seen:
            next_scene += 1
            seen[raw] = next_scene
        assignment.append({
            "filename": str(get("filename", "") or ""),
            "scene": seen[raw],
            "reason": REASON_CONTINUES,
            "gap": None,
        })
    return summarize(items, assignment)


def plan_regroup(
    items: Sequence[Any],
    *,
    group_within_seconds: float = 1.0,
    split_after_seconds: float = 0.0,
    similarity_threshold: float = 0.5,
) -> dict:
    """Full preview payload: current grouping, proposed grouping, and stats."""
    ordered = sort_items(items)
    assignment = compute_groups(
        ordered,
        group_within_seconds=group_within_seconds,
        split_after_seconds=split_after_seconds,
        similarity_threshold=similarity_threshold,
    )
    proposed = summarize(ordered, assignment)
    current = summarize_current(ordered)
    scored, unscored = count_scored_pairs(ordered)
    feature_cut, color_cut = map_similarity_threshold(similarity_threshold)

    reason_counts: dict[str, int] = {}
    for a in assignment:
        r = a.get("reason") or REASON_CONTINUES
        if r and r != REASON_FIRST:
            reason_counts[r] = reason_counts.get(r, 0) + 1

    sizes = [s["size"] for s in proposed] or [0]
    current_sizes = [s["size"] for s in current] or [0]
    return {
        "image_count": len(ordered),
        "current": current,
        "proposed": proposed,
        "assignment": [{"filename": a["filename"], "scene": a["scene"]} for a in assignment],
        "stats": {
            "current_scene_count": len(current),
            "proposed_scene_count": len(proposed),
            "largest_current_scene": max(current_sizes),
            "largest_proposed_scene": max(sizes),
            "scored_pairs": scored,
            "unscored_pairs": unscored,
            "breaks_by_reason": reason_counts,
            "feature_cut": round(feature_cut, 4),
            "color_cut": round(color_cut, 4),
        },
    }
