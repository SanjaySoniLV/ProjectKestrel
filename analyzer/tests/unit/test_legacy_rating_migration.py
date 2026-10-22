"""Regression test for legacy rating -> scenedata migration.

When a legacy CSV had a ``rating_origin`` column that was blank for a
manually-rated row, the migration dropped the rating: the condition required
``origin == "manual"`` OR ``(no origin column AND 1<=r<=5)``, so a blank origin
with a real rating fell through both branches and was lost. A blank origin is
now treated like "no origin column" (assumed user intent); explicit ``auto``
ratings are still left for recomputation.
"""

import io
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.database import _build_scenedata_from_legacy_db

pytestmark = pytest.mark.unit


def test_blank_origin_via_read_csv_is_preserved_not_dropped():
    """Reproduces the real path: a blank CSV field becomes NaN via pd.read_csv."""
    csv_text = (
        "filename,rating,rating_origin,scene_count\n"
        "A.CR3,4,,1\n"          # blank rating_origin -> NaN
        "B.CR3,5,manual,1\n"
        "C.CR3,3,auto,2\n"
    )
    df = pd.read_csv(io.StringIO(csv_text))
    # Sanity: the blank field really is NaN, not '' (this is what broke the fix).
    assert pd.isna(df.loc[0, "rating_origin"])

    ratings = _build_scenedata_from_legacy_db(df)["image_ratings"]
    assert ratings.get("A.CR3") == 4, "blank/NaN-origin rating must be preserved"
    assert ratings.get("B.CR3") == 5, "manual rating must be preserved"
    assert "C.CR3" not in ratings, "explicit 'auto' rating must not be migrated"


def test_blank_and_nan_origins_preserved_directly():
    """Both an empty string and a real NaN origin count as user intent."""
    df = pd.DataFrame({
        "filename": ["A.CR3", "B.CR3"],
        "rating": [4, 2],
        "rating_origin": ["", float("nan")],
        "scene_count": [1, 1],
    })
    ratings = _build_scenedata_from_legacy_db(df)["image_ratings"]
    assert ratings.get("A.CR3") == 4
    assert ratings.get("B.CR3") == 2


def test_no_origin_column_preserves_nonzero_ratings():
    df = pd.DataFrame({
        "filename": ["A.CR3", "B.CR3"],
        "rating": [3, 0],
        "scene_count": [1, 1],
    })
    ratings = _build_scenedata_from_legacy_db(df)["image_ratings"]
    assert ratings.get("A.CR3") == 3
    assert "B.CR3" not in ratings   # a 0 rating is "unrated", not migrated
