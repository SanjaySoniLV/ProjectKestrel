"""Lost-update: UI scenedata writes during analysis must survive post-analysis save.

The pipeline used to load scenedata (or treat it as empty) and then
``save_scenedata`` a built/updated copy. Ratings, scene names, approve
status, and ``user_tags`` the UI wrote to disk while analysis was running
were overwritten.

Filesystem only; no RAWs or weights.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from kestrel_analyzer.database import (
    SCENEDATA_VERSION,
    finalize_scenedata_after_analysis,
    load_scenedata,
    save_scenedata,
)


pytestmark = pytest.mark.unit

_UI_SCENE = {
    "scene_id": "1",
    "image_filenames": ["IMG_001.CR3"],
    "name": "Morning hawk",
    "status": "approved",
    "user_tags": {
        "species": ["Red-tailed Hawk"],
        "families": ["Accipitridae"],
        "finalized": True,
    },
}

_UI_SCENEDATA = {
    "version": SCENEDATA_VERSION,
    "image_ratings": {"IMG_001.CR3": 4},
    "scenes": {"1": dict(_UI_SCENE)},
}


def _kestrel(tmp_path: Path) -> Path:
    kestrel = tmp_path / ".kestrel"
    kestrel.mkdir()
    return kestrel


def _database() -> pd.DataFrame:
    return pd.DataFrame({"filename": ["IMG_001.CR3"], "scene_count": [1]})


def _assert_ui_fields_survived(got: dict) -> None:
    assert got["image_ratings"]["IMG_001.CR3"] == 4
    scene = got["scenes"]["1"]
    assert scene["name"] == "Morning hawk"
    assert scene["status"] == "approved"
    assert scene["user_tags"]["species"] == ["Red-tailed Hawk"]
    assert scene["user_tags"]["families"] == ["Accipitridae"]
    assert scene["user_tags"]["finalized"] is True


class TestScenedataLostUpdate:
    def test_ui_fields_survive_stale_empty_first_load(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pipeline held an empty in-memory copy; disk already has UI edits."""
        kestrel = _kestrel(tmp_path)
        save_scenedata(_UI_SCENEDATA, str(kestrel))

        import kestrel_analyzer.database as dbmod

        original_load = dbmod.load_scenedata
        calls = {"n": 0}

        def fake_load(kestrel_dir: str) -> dict:
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "version": SCENEDATA_VERSION,
                    "image_ratings": {},
                    "scenes": {},
                }
            return original_load(kestrel_dir)

        monkeypatch.setattr(dbmod, "load_scenedata", fake_load)
        finalize_scenedata_after_analysis(str(kestrel), _database())

        got = original_load(str(kestrel))
        _assert_ui_fields_survived(got)
        assert "IMG_001.CR3" in got["scenes"]["1"]["image_filenames"]

    def test_ratings_on_disk_survive_empty_scenes_rebuild(self, tmp_path: Path) -> None:
        """``not scenes`` took build_scenedata_from_database and dropped ratings."""
        kestrel = _kestrel(tmp_path)
        save_scenedata(
            {
                "version": SCENEDATA_VERSION,
                "image_ratings": {"IMG_001.CR3": 4},
                "scenes": {},
            },
            str(kestrel),
        )
        finalize_scenedata_after_analysis(str(kestrel), _database())
        got = load_scenedata(str(kestrel))
        assert got["image_ratings"]["IMG_001.CR3"] == 4
        assert "1" in got["scenes"]

    def test_update_path_keeps_ui_fields_and_adds_new_image(self, tmp_path: Path) -> None:
        kestrel = _kestrel(tmp_path)
        save_scenedata(_UI_SCENEDATA, str(kestrel))
        db = pd.DataFrame(
            {
                "filename": ["IMG_001.CR3", "IMG_002.CR3"],
                "scene_count": [1, 1],
            }
        )
        finalize_scenedata_after_analysis(str(kestrel), db)
        got = load_scenedata(str(kestrel))
        _assert_ui_fields_survived(got)
        assert "IMG_002.CR3" in got["scenes"]["1"]["image_filenames"]
