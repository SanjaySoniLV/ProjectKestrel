# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Portions of this file are adapted from google/cameratrapai
# (https://github.com/google/cameratrapai) under the Apache 2.0 licence.
# Modifications: standalone extraction with no torch/PyTorch dependency.
# Reads taxonomy and geofence data directly from the bundled model directory.

"""Standalone SpeciesNet ensemble logic — no torch/ML dependencies.

Provides Classification/Detection constants, the BBox dataclass, and
LocalSpeciesNetEnsemble (a drop-in for SpeciesNetEnsemble) using only
the standard library plus the taxonomy/geofence JSON bundled with the model.
"""

from __future__ import annotations

import enum
import json
import os
from dataclasses import dataclass
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Constants  (adapted from speciesnet/constants.py)
# ---------------------------------------------------------------------------

class Classification(str, enum.Enum):
    BLANK   = "f1856211-cfb7-4a5b-9158-c0f72fd09ee6;;;;;;blank"
    ANIMAL  = "1f689929-883d-4dae-958c-3d57ab5b6c16;;;;;;animal"
    HUMAN   = "990ae9dd-7a59-4344-afcb-1b7b21368000;mammalia;primates;hominidae;homo;sapiens;human"
    VEHICLE = "e2895ed5-780b-48f6-8a11-9e27cb594511;;;;;;vehicle"
    UNKNOWN = (
        "f2efdae9-efb8-48fb-8a91-eccf79ab4ffb;"
        "no cv result;no cv result;no cv result;"
        "no cv result;no cv result;no cv result"
    )


class Detection(str, enum.Enum):
    ANIMAL  = "animal"
    HUMAN   = "human"
    VEHICLE = "vehicle"


class Failure(enum.Flag):
    CLASSIFIER  = enum.auto()
    DETECTOR    = enum.auto()
    GEOLOCATION = enum.auto()


# ---------------------------------------------------------------------------
# BBox dataclass  (adapted from speciesnet/utils.py)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BBox:
    """Normalised bounding box — all values in [0, 1]."""
    xmin:   float
    ymin:   float
    width:  float
    height: float


# ---------------------------------------------------------------------------
# Taxonomy utils  (adapted from speciesnet/taxonomy_utils.py)
# ---------------------------------------------------------------------------

def _get_ancestor_at_level(
    label: str, taxonomy_level: str, taxonomy_map: dict
) -> Optional[str]:
    """Return the label of *label*'s ancestor at *taxonomy_level*, or None."""
    label_parts = label.split(";")
    if len(label_parts) != 7:
        return None  # gracefully ignore malformed labels

    if taxonomy_level == "species":
        ancestor_parts = label_parts[1:6]
        if not ancestor_parts[4]:
            return None
    elif taxonomy_level == "genus":
        ancestor_parts = label_parts[1:5] + [""]
        if not ancestor_parts[3]:
            return None
    elif taxonomy_level == "family":
        ancestor_parts = label_parts[1:4] + ["", ""]
        if not ancestor_parts[2]:
            return None
    elif taxonomy_level == "order":
        ancestor_parts = label_parts[1:3] + ["", "", ""]
        if not ancestor_parts[1]:
            return None
    elif taxonomy_level == "class":
        ancestor_parts = label_parts[1:2] + ["", "", "", ""]
        if not ancestor_parts[0]:
            return None
    elif taxonomy_level == "kingdom":
        ancestor_parts = ["", "", "", "", ""]
        if not label_parts[1] and label != Classification.ANIMAL:
            return None
    else:
        return None

    return taxonomy_map.get(";".join(ancestor_parts))


# ---------------------------------------------------------------------------
# Geofence helpers  (adapted from speciesnet/geofence_utils.py)
# ---------------------------------------------------------------------------

def _should_geofence(
    label: str,
    country: Optional[str],
    admin1_region: Optional[str],
    geofence_map: dict,
    enable_geofence: bool,
) -> bool:
    if not enable_geofence:
        return False
    rules = geofence_map.get(label)
    if not rules:
        return False
    allow_rules = rules.get("allow", [])
    block_rules = rules.get("block", [])
    if not allow_rules and not block_rules:
        return False
    for rule in block_rules:
        if country and rule.get("country") == country:
            return True
        if admin1_region and rule.get("admin1_region") == admin1_region:
            return True
    if allow_rules:
        for rule in allow_rules:
            if country and rule.get("country") == country:
                return False
            if admin1_region and rule.get("admin1_region") == admin1_region:
                return False
        return True
    return False


def _roll_up_labels(
    labels: list,
    scores: list,
    country: Optional[str],
    admin1_region: Optional[str],
    target_taxonomy_levels: list,
    non_blank_threshold: float,
    taxonomy_map: dict,
    geofence_map: dict,
    enable_geofence: bool,
) -> Optional[tuple]:
    for taxonomy_level in target_taxonomy_levels:
        accumulated: dict = {}
        for label, score in zip(labels, scores):
            rollup_label = _get_ancestor_at_level(label, taxonomy_level, taxonomy_map)
            if rollup_label:
                accumulated[rollup_label] = accumulated.get(rollup_label, 0.0) + score

        max_label = None
        max_score = 0.0
        for rollup_label, rollup_score in accumulated.items():
            if rollup_score > max_score and not _should_geofence(
                rollup_label, country, admin1_region, geofence_map, enable_geofence
            ):
                max_label = rollup_label
                max_score = rollup_score

        if max_score > non_blank_threshold and max_label:
            return (max_label, max_score, f"classifier+rollup_to_{taxonomy_level}")
    return None


def _geofence_animal(
    *,
    labels: list,
    scores: list,
    country: Optional[str],
    admin1_region: Optional[str],
    taxonomy_map: dict,
    geofence_map: dict,
    enable_geofence: bool,
) -> tuple:
    if _should_geofence(labels[0], country, admin1_region, geofence_map, enable_geofence):
        rollup = _roll_up_labels(
            labels=labels, scores=scores,
            country=country, admin1_region=admin1_region,
            target_taxonomy_levels=["family", "order", "class", "kingdom"],
            non_blank_threshold=scores[0] - 1e-10,
            taxonomy_map=taxonomy_map, geofence_map=geofence_map,
            enable_geofence=enable_geofence,
        )
        if rollup:
            rollup_label, rollup_score, rollup_source = rollup
            return (
                rollup_label,
                rollup_score,
                "classifier+geofence+" + rollup_source[len("classifier+"):],
            )
        return (Classification.UNKNOWN, scores[0], "classifier+geofence+rollup_failed")
    return labels[0], scores[0], "classifier"


# ---------------------------------------------------------------------------
# Ensemble combiner  (adapted from speciesnet/ensemble_prediction_combiner.py)
# ---------------------------------------------------------------------------

def _combine_single(
    *,
    classifications: dict,
    detections: list,
    country: Optional[str],
    admin1_region: Optional[str],
    taxonomy_map: dict,
    geofence_map: dict,
    enable_geofence: bool,
) -> tuple:
    top_cls_class = classifications["classes"][0]
    top_cls_score = classifications["scores"][0]
    top_det_class = detections[0]["label"] if detections else Detection.ANIMAL
    top_det_score = detections[0]["conf"]  if detections else 0.0

    # Threshold #1: HUMAN detections
    if top_det_class == Detection.HUMAN:
        if top_det_score > 0.7:
            return Classification.HUMAN, top_det_score, "detector"
        if (
            top_det_score > 0.2
            and top_cls_class in {Classification.HUMAN, Classification.VEHICLE}
            and top_cls_score > 0.5
        ):
            return Classification.HUMAN, top_cls_score, "classifier"

    # Threshold #2: VEHICLE detections
    if top_det_class == Detection.VEHICLE:
        if top_det_score > 0.2 and top_cls_class == Classification.HUMAN and top_cls_score > 0.5:
            return Classification.HUMAN, top_cls_score, "classifier"
        if top_det_score > 0.7:
            return Classification.VEHICLE, top_det_score, "detector"
        if top_det_score > 0.2 and top_cls_class == Classification.VEHICLE and top_cls_score > 0.4:
            return Classification.VEHICLE, top_cls_score, "classifier"

    # Threshold #3: BLANK
    if top_det_score < 0.2 and top_cls_class == Classification.BLANK and top_cls_score > 0.5:
        return Classification.BLANK, top_cls_score, "classifier"
    if top_cls_class == Classification.BLANK and top_cls_score > 0.99:
        return Classification.BLANK, top_cls_score, "classifier"

    geofence_kwargs = dict(
        labels=classifications["classes"], scores=classifications["scores"],
        country=country, admin1_region=admin1_region,
        taxonomy_map=taxonomy_map, geofence_map=geofence_map,
        enable_geofence=enable_geofence,
    )

    # Threshold #4: ANIMAL species-level classification
    if top_cls_class not in {Classification.BLANK, Classification.HUMAN, Classification.VEHICLE}:
        if top_cls_score > 0.8:
            return _geofence_animal(**geofence_kwargs)
        if top_cls_score > 0.65 and top_det_class == Detection.ANIMAL and top_det_score > 0.2:
            return _geofence_animal(**geofence_kwargs)

    # Threshold #5a: taxonomy rollup
    rollup = _roll_up_labels(
        labels=classifications["classes"], scores=classifications["scores"],
        country=country, admin1_region=admin1_region,
        target_taxonomy_levels=["genus", "family", "order", "class", "kingdom"],
        non_blank_threshold=0.65,
        taxonomy_map=taxonomy_map, geofence_map=geofence_map,
        enable_geofence=enable_geofence,
    )
    if rollup:
        return rollup

    # Threshold #5b: mid-confidence detector
    if top_det_class == Detection.ANIMAL and top_det_score > 0.5:
        return Classification.ANIMAL, top_det_score, "detector"

    return Classification.UNKNOWN, top_cls_score, "classifier"


# ---------------------------------------------------------------------------
# Taxonomy loading  (adapted from speciesnet/ensemble.py)
# ---------------------------------------------------------------------------

def _load_taxonomy_from_file(taxonomy_file: str) -> dict:
    def _taxa(label: str) -> str:
        return ";".join(label.split(";")[1:6])

    with open(taxonomy_file, mode="r", encoding="utf-8") as fh:
        labels = [line.strip() for line in fh if line.strip()]

    taxonomy_map = {_taxa(label): label for label in labels}
    for label in (Classification.BLANK, Classification.VEHICLE, Classification.UNKNOWN):
        taxonomy_map.pop(_taxa(label), None)
    for label in (Classification.HUMAN, Classification.ANIMAL):
        taxonomy_map[_taxa(label)] = label
    return taxonomy_map


# ---------------------------------------------------------------------------
# LocalSpeciesNetEnsemble — drop-in for SpeciesNetEnsemble
# ---------------------------------------------------------------------------

class LocalSpeciesNetEnsemble:
    """SpeciesNet ensemble with no torch/PyTorch dependency.

    Reads taxonomy and geofence data directly from the bundled model directory
    (info.json → taxonomy_release.*.txt + geofence_release.*.json).

    API-compatible with speciesnet.SpeciesNetEnsemble.combine().
    """

    def __init__(self, model_name: str, geofence: bool = True) -> None:
        self.enable_geofence = geofence
        info_path = os.path.join(model_name, "info.json")
        with open(info_path, "r", encoding="utf-8") as fh:
            info = json.load(fh)
        self.taxonomy_map = _load_taxonomy_from_file(
            os.path.join(model_name, info["taxonomy"])
        )
        with open(os.path.join(model_name, info["geofence"]), "r", encoding="utf-8") as fh:
            self.geofence_map = json.load(fh)
        self.model_version = info.get("version", "unknown")

    def combine(
        self,
        filepaths: list[str],
        classifier_results: dict[str, Any],
        detector_results: dict[str, Any],
        geolocation_results: dict[str, Any],
        partial_predictions: dict[str, dict],
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for filepath in filepaths:
            if filepath in partial_predictions:
                results.append(partial_predictions[filepath])
                continue

            failure = Failure(0)
            if filepath in classifier_results and "failures" not in classifier_results[filepath]:
                classifications = classifier_results[filepath]["classifications"]
            else:
                classifications = None
                failure |= Failure.CLASSIFIER
            if filepath in detector_results and "failures" not in detector_results[filepath]:
                detections = detector_results[filepath]["detections"]
            else:
                detections = None
                failure |= Failure.DETECTOR
            geolocation = geolocation_results.get(filepath, {})
            if filepath not in geolocation_results:
                failure |= Failure.GEOLOCATION

            result: dict[str, Any] = {
                k: v for k, v in {
                    "filepath":     filepath,
                    "failures":     ([f.name for f in Failure if f in failure] if failure else None),
                    "country":      geolocation.get("country"),
                    "admin1_region": geolocation.get("admin1_region"),
                    "classifications": classifications,
                    "detections":   detections,
                }.items() if v is not None
            }

            if classifications is not None and detections is not None:
                prediction, score, source = _combine_single(
                    classifications=classifications,
                    detections=detections,
                    country=geolocation.get("country"),
                    admin1_region=geolocation.get("admin1_region"),
                    taxonomy_map=self.taxonomy_map,
                    geofence_map=self.geofence_map,
                    enable_geofence=self.enable_geofence,
                )
                result["prediction"] = (
                    prediction.value if isinstance(prediction, Classification) else prediction
                )
                result["prediction_score"] = score
                result["prediction_source"] = source

            result["model_version"] = self.model_version
            results.append(result)

        return results
