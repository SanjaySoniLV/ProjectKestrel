"""Legacy upgrade must not strip CSV ratings if scenedata save fails.

Repro (FINDINGS S0-02): ``_perform_db_upgrade`` caught ``save_scenedata``
errors, logged them, then still renamed the CSV and dropped ``rating`` /
``scene_name``. Ratings were then in neither JSON nor CSV.
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.config import DATABASE_NAME, SCENEDATA_FILENAME
from kestrel_analyzer.database import (
    LEGACY_USER_COLUMNS,
    _perform_db_upgrade,
    load_database,
    load_scenedata,
)


pytestmark = pytest.mark.unit


def _legacy_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "filename": ["IMG_001.CR3", "IMG_002.CR3"],
            "scene_count": [1, 1],
            "rating": [5, 4],
            "normalized_rating": [1.0, 0.8],
            "scene_name": ["Keepers", "Keepers"],
            "rating_origin": ["manual", "manual"],
        }
    )


def _write_legacy_csv(kestrel_dir: Path, database: pd.DataFrame) -> Path:
    db_path = kestrel_dir / DATABASE_NAME
    database.to_csv(db_path, index=False)
    return db_path


class TestUpgradeAbortsOnScenedataSaveFailure:
    def test_csv_ratings_unchanged_when_save_scenedata_raises(
        self, tmp_path, monkeypatch
    ):
        kestrel_dir = tmp_path / ".kestrel"
        kestrel_dir.mkdir()
        database = _legacy_frame()
        db_path = _write_legacy_csv(kestrel_dir, database)
        original = db_path.read_bytes()

        def _boom(_scenedata, _kestrel_dir):
            raise OSError("disk full")

        monkeypatch.setattr(
            "kestrel_analyzer.database.save_scenedata", _boom
        )

        with pytest.raises(OSError, match="disk full"):
            _perform_db_upgrade(
                database.copy(), str(kestrel_dir), str(db_path), None
            )

        assert db_path.read_bytes() == original
        on_disk = pd.read_csv(db_path)
        assert list(on_disk["rating"]) == [5, 4]
        assert list(on_disk["scene_name"]) == ["Keepers", "Keepers"]
        assert not (kestrel_dir / SCENEDATA_FILENAME).exists()
        assert list(kestrel_dir.glob("OLD_kestrel_database_*.csv")) == []

    def test_load_database_propagates_and_does_not_strip(
        self, tmp_path, monkeypatch
    ):
        """Neighbor: production caller is load_database → _perform_db_upgrade."""
        kestrel_dir = tmp_path / ".kestrel"
        kestrel_dir.mkdir()
        db_path = _write_legacy_csv(kestrel_dir, _legacy_frame())
        original = db_path.read_bytes()

        def _boom(_scenedata, _kestrel_dir):
            raise OSError("disk full")

        monkeypatch.setattr(
            "kestrel_analyzer.database.save_scenedata", _boom
        )

        with pytest.raises(OSError, match="disk full"):
            load_database(str(kestrel_dir), "test_analyzer", None)

        assert db_path.read_bytes() == original


class TestUpgradeNeighborSuccess:
    def test_successful_save_still_strips_legacy_columns(self, tmp_path):
        kestrel_dir = tmp_path / ".kestrel"
        kestrel_dir.mkdir()
        database = _legacy_frame()
        db_path = _write_legacy_csv(kestrel_dir, database)

        result = _perform_db_upgrade(
            database.copy(), str(kestrel_dir), str(db_path), None
        )

        for col in LEGACY_USER_COLUMNS:
            assert col not in result.columns
        live = pd.read_csv(db_path)
        for col in LEGACY_USER_COLUMNS:
            assert col not in live.columns
        scenedata = load_scenedata(str(kestrel_dir))
        assert scenedata["image_ratings"]["IMG_001.CR3"] == 5
        assert scenedata["image_ratings"]["IMG_002.CR3"] == 4
        assert scenedata["scenes"]["1"]["name"] == "Keepers"
        assert list(kestrel_dir.glob("OLD_kestrel_database_*.csv"))
