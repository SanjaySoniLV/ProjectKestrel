"""Corrupt kestrel_scenedata.json must not be treated as an empty folder.

Repro (FINDINGS S0-01): a file with ratings is overwritten by invalid JSON
``{``. The old load_scenedata swallowed the parse error and returned
``image_ratings={}`` / ``scenes={}``. pipeline.py then saw ``not scenes``
and rebuilt from the CSV, wiping ratings and tags.
"""

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.config import SCENEDATA_FILENAME
from kestrel_analyzer.database import (
    SCENEDATA_VERSION,
    ScenedataCorruptError,
    build_scenedata_from_database,
    load_scenedata,
    save_scenedata,
    update_scenedata_with_database,
)


pytestmark = pytest.mark.unit


_RATED = {
    "version": SCENEDATA_VERSION,
    "image_ratings": {"IMG_001.CR3": 5},
    "scenes": {
        "1": {
            "scene_id": "1",
            "image_filenames": ["IMG_001.CR3"],
            "name": "Keepers",
            "status": "accepted",
            "user_tags": {
                "species": ["Parus major"],
                "families": [],
                "finalized": True,
            },
        }
    },
}


def _kestrel(tmp_path: Path) -> Path:
    kestrel_dir = tmp_path / ".kestrel"
    kestrel_dir.mkdir()
    return kestrel_dir


class TestCorruptScenedataLoad:
    def test_invalid_json_raises_and_leaves_file(self, tmp_path):
        kestrel_dir = _kestrel(tmp_path)
        path = kestrel_dir / SCENEDATA_FILENAME
        path.write_text(json.dumps(_RATED), encoding="utf-8")
        path.write_text("{", encoding="utf-8")

        with pytest.raises(ScenedataCorruptError):
            load_scenedata(str(kestrel_dir))

        assert path.read_text(encoding="utf-8") == "{"
        assert [p.name for p in kestrel_dir.iterdir()] == [SCENEDATA_FILENAME]

    def test_non_object_json_raises(self, tmp_path):
        kestrel_dir = _kestrel(tmp_path)
        (kestrel_dir / SCENEDATA_FILENAME).write_text("[]", encoding="utf-8")

        with pytest.raises(ScenedataCorruptError, match="not a JSON object"):
            load_scenedata(str(kestrel_dir))

    def test_pipeline_post_analysis_does_not_rebuild_over_corrupt_file(self, tmp_path):
        """Neighbor: pipeline.py post-analysis treats empty scenes as fresh.

        The handler wraps load/save in ``except Exception`` and must skip the
        save when load raises, so the corrupt bytes stay on disk.
        """
        kestrel_dir = _kestrel(tmp_path)
        path = kestrel_dir / SCENEDATA_FILENAME
        path.write_text("{", encoding="utf-8")
        database = pd.DataFrame(
            {"filename": ["IMG_001.CR3"], "scene_count": [1]}
        )

        try:
            existing_scenedata = load_scenedata(str(kestrel_dir))
            if not existing_scenedata.get("scenes"):
                save_scenedata(
                    build_scenedata_from_database(database), str(kestrel_dir)
                )
            else:
                update_scenedata_with_database(existing_scenedata, database)
                save_scenedata(existing_scenedata, str(kestrel_dir))
        except ScenedataCorruptError:
            pass

        assert path.read_text(encoding="utf-8") == "{"


class TestScenedataLoadNeighbors:
    def test_missing_file_still_returns_initialized_dict(self, tmp_path):
        kestrel_dir = _kestrel(tmp_path)

        result = load_scenedata(str(kestrel_dir))

        assert result["version"] == SCENEDATA_VERSION
        assert result["image_ratings"] == {}
        assert result["scenes"] == {}

    def test_valid_rated_file_roundtrip(self, tmp_path):
        kestrel_dir = _kestrel(tmp_path)
        save_scenedata(_RATED, str(kestrel_dir))

        loaded = load_scenedata(str(kestrel_dir))

        assert loaded == _RATED
        assert loaded["image_ratings"]["IMG_001.CR3"] == 5
        assert loaded["scenes"]["1"]["name"] == "Keepers"
