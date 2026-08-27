"""Regression test for legacy rating -> scenedata migration.

When a legacy CSV had a ``rating_origin`` column that was blank for a
manually-rated row, the migration dropped the rating: the condition required
``origin == "manual"`` OR ``(no origin column AND 1<=r<=5)``, so a blank origin
with a real rating fell through both branches and was lost. A blank origin is
now treated like "no origin column" (assumed user intent); explicit ``auto``
ratings are still left for recomputation.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.database import _build_scenedata_from_legacy_db

pytestmark = pytest.mark.unit


def test_blank_origin_rating_is_preserved_not_dropped():
    df = pd.DataFrame({
        "filename": ["A.CR3", "B.CR3", "C.CR3"],
        "rating": [4, 5, 3],
        "rating_origin": ["", "manual", "auto"],   # blank / manual / auto
        "scene_count": [1, 1, 2],
    })
    ratings = _build_scenedata_from_legacy_db(df)["image_ratings"]
    assert ratings.get("A.CR3") == 4, "blank-origin rating must be preserved"
    assert ratings.get("B.CR3") == 5, "manual rating must be preserved"
    assert "C.CR3" not in ratings, "explicit 'auto' rating must not be migrated"


def test_no_origin_column_preserves_nonzero_ratings():
    df = pd.DataFrame({
        "filename": ["A.CR3", "B.CR3"],
        "rating": [3, 0],
        "scene_count": [1, 1],
    })
    ratings = _build_scenedata_from_legacy_db(df)["image_ratings"]
    assert ratings.get("A.CR3") == 3
    assert "B.CR3" not in ratings   # a 0 rating is "unrated", not migrated
