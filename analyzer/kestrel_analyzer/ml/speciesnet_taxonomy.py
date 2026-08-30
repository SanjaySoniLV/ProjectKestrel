"""Parse SpeciesNet semicolon taxonomy strings for pipeline routing."""

from __future__ import annotations

import math
from typing import Any

from ._speciesnet_ensemble import Classification

# Full label strings from SpeciesNet taxonomy (see speciesnet.constants.Classification).
_SPECIESNET_BLANK = "f1856211-cfb7-4a5b-9158-c0f72fd09ee6;;;;;;blank"
_SPECIESNET_VEHICLE = "e2895ed5-780b-48f6-8a11-9e27cb594511;;;;;;vehicle"
_SPECIESNET_HUMAN = (
    "990ae9dd-7a59-4344-afcb-1b7b21368000;mammalia;primates;hominidae;homo;sapiens;human"
)


def split_taxonomy(raw: str) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in raw.split(";")]


def format_species_display_name(name: str) -> str:
    """Word-level capitalization for SpeciesNet common names (e.g. pronghorn → Pronghorn, cane toad → Cane Toad)."""
    if not name or not str(name).strip():
        return (name or "").strip()

    def cap_segment(seg: str) -> str:
        if not seg:
            return seg
        return seg[0].upper() + seg[1:].lower() if len(seg) > 1 else seg.upper()

    def cap_token(tok: str) -> str:
        if not tok:
            return tok
        if "-" in tok:
            return "-".join(cap_segment(p) if p else p for p in tok.split("-"))
        return cap_segment(tok)

    s = str(name).strip()
    return " ".join(cap_token(t) for t in s.split())


def wildlife_display_name(raw: str) -> str:
    """Last non-empty semicolon segment (common name when present), title-cased for display."""
    parts = split_taxonomy(raw)
    for seg in reversed(parts):
        if seg:
            return format_species_display_name(seg)
    return format_species_display_name(raw.strip() or "unknown")


def is_ignored_prediction(raw: str) -> bool:
    """blank, vehicle, human — skip for downstream processing.

    ``no cv result`` is handled separately in :func:`route_prediction` so that
    the classifier-tiebreak in :func:`route_with_classifier_tiebreak` still has
    a chance to rescue it as a bird using the top-k classifier scores. When
    neither the ensemble nor the top-k commit to a species, the detection is
    routed to ignore rather than emitted as a wildlife "Unknown" crop, on the
    reasoning that a MegaDetector box SpeciesNet cannot classify is usually a
    false positive (branch, shadow, rock, sky artifact, etc.).
    """
    if not raw or not str(raw).strip():
        return True
    s = str(raw).strip()
    if s == _SPECIESNET_BLANK:
        return True
    if s == _SPECIESNET_VEHICLE:
        return True
    if s == _SPECIESNET_HUMAN:
        return True
    parts_lower = [p.lower() for p in split_taxonomy(s) if p]
    last = parts_lower[-1] if parts_lower else ""
    if last in ("blank", "vehicle", "human"):
        return True
    # Genus- or family-only human labels (e.g. "mammalia;primates;hominidae;homo;;")
    # omit the common-name segment, so the last-segment check above misses them.
    if "hominidae" in parts_lower or "homo" in parts_lower:
        return True
    return False


def is_no_cv_result(raw: str) -> bool:
    """True when SpeciesNet returned 'no cv result' — detected animal, unclassifiable."""
    if not raw or not str(raw).strip():
        return False
    s = str(raw).strip()
    if s == Classification.UNKNOWN.value:
        return True
    parts_lower = [p.lower() for p in split_taxonomy(s) if p]
    last = parts_lower[-1] if parts_lower else ""
    return last == "no cv result"


def should_skip_confident_no_cv_classifier(
    classifications: dict[str, Any],
    detector_threshold: float,
) -> bool:
    """True when the classifier's **highest-scoring** label is ``no cv result`` with
    score **strictly greater than** ``(1 - detector_threshold)``.

    MegaDetector can still fire on clutter; if SpeciesNet is very confident the crop
    is unclassifiable (``no cv result``), we skip SAM and downstream crops. The cutoff
    tracks the user-facing detection threshold (e.g. threshold 0.25 → ignore when
    no-cv score > 0.75).
    """
    classes = classifications.get("classes") or []
    scores = classifications.get("scores") or []
    n = min(len(classes), len(scores))
    if n <= 0:
        return False
    best_i = -1
    best_sc: float | None = None
    for i in range(n):
        try:
            sc = float(scores[i])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(sc):
            continue
        if best_sc is None or sc > best_sc:
            best_sc = sc
            best_i = i
    if best_i < 0 or best_sc is None:
        return False
    best_raw = str(classes[best_i])
    if not is_no_cv_result(best_raw):
        return False
    try:
        dt = float(detector_threshold)
    except (TypeError, ValueError):
        dt = 0.25
    dt = max(0.05, min(0.95, dt))
    score_floor = 1.0 - dt
    return best_sc > score_floor


def is_bird_taxon(raw: str) -> bool:
    """True if taxonomy includes class Aves (segment 'aves')."""
    parts = [p.lower() for p in split_taxonomy(raw) if p]
    return "aves" in parts


def route_prediction(
    raw_prediction: str,
    *,
    wildlife_enabled: bool,
) -> tuple[str, str | None]:
    """Return (route, pred_class_label).

    route: 'ignore' | 'bird' | 'wildlife'
    pred_class_label: 'bird' for aves, or display name for wildlife; None if ignore.
    """
    if is_ignored_prediction(raw_prediction):
        return "ignore", None
    if is_bird_taxon(raw_prediction):
        return "bird", "bird"
    # "no cv result" / Classification.UNKNOWN — SpeciesNet's ensemble could
    # not commit to a species. Route to ignore by default; the caller
    # (``route_with_classifier_tiebreak``) will still rescue this as a bird
    # when the top-k classifier scores show a stronger aves hypothesis than
    # any other animal. A MegaDetector box with no classifier commitment is
    # overwhelmingly a false positive (branch, shadow, rock, sky artifact),
    # so treating it as "Unknown wildlife" adds noise and disk churn for
    # essentially no signal.
    if is_no_cv_result(raw_prediction):
        return "ignore", None
    if not wildlife_enabled:
        return "ignore", None
    return "wildlife", wildlife_display_name(raw_prediction)


def bird_vs_wildlife_classifier_scores(classifications: dict[str, Any]) -> tuple[float, float]:
    """Max classifier score for an Aves label vs. max for any other non-ignored label.

    Used when the ensemble rolls up to a generic class (e.g. ``animal``): comparing
    these two indicates whether a bird-specific species hypothesis is stronger than
    other animal hypotheses in the top-k list.
    """
    classes = classifications.get("classes") or []
    scores = classifications.get("scores") or []
    best_bird = 0.0
    best_other = 0.0
    for raw, sc in zip(classes, scores):
        try:
            conf = float(sc)
        except (TypeError, ValueError):
            continue
        label = str(raw)
        if is_ignored_prediction(label):
            continue
        if is_bird_taxon(label):
            best_bird = max(best_bird, conf)
        else:
            best_other = max(best_other, conf)
    return best_bird, best_other


def is_ambiguous_generic_taxonomy(raw_prediction: str) -> bool:
    """True when ensemble did not commit to a species-level animal class."""
    if not raw_prediction or not str(raw_prediction).strip():
        return True
    s = str(raw_prediction).strip()
    if s == Classification.ANIMAL.value or s == Classification.UNKNOWN.value:
        return True
    if is_no_cv_result(s):
        return True
    last = wildlife_display_name(s).lower()
    return last == "animal"


def route_with_classifier_tiebreak(
    raw_prediction: str,
    pred_score: float,
    classifications: dict[str, Any],
    *,
    wildlife_enabled: bool,
) -> tuple[str, str | None, float]:
    """Apply ``route_prediction``, then optionally prefer bird using classifier top-k.

    When the ensemble prediction is *ambiguous* (generic ``animal``, ``unknown``, or
    display name ``animal`` only), we compare max bird vs max other non-ignored
    classifier scores. If the bird score is strictly higher, we route as ``bird`` and
    use that score—so a stronger aves hypothesis in the top-k list wins over a
    generic or conflicting rollup. Species-level non-bird predictions are unchanged.
    """
    route, pred_label = route_prediction(raw_prediction, wildlife_enabled=wildlife_enabled)
    if is_ignored_prediction(raw_prediction):
        return route, pred_label, pred_score
    if not is_ambiguous_generic_taxonomy(raw_prediction):
        return route, pred_label, pred_score
    best_bird, best_other = bird_vs_wildlife_classifier_scores(classifications)
    if best_bird > best_other and best_bird > 0.0:
        return "bird", "bird", best_bird
    return route, pred_label, pred_score
