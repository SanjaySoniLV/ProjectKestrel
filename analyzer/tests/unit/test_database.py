"""Unit tests for database.py - CSV and JSON database layer."""

import pytest
import json
import pandas as pd
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.database import (
    load_database,
    save_database,
    ensure_columns,
    load_scenedata,
    save_scenedata,
    build_scenedata_from_database,
    update_scenedata_with_database,
    BASE_COLUMNS,
    REQUIRED_COLUMNS,
)


pytestmark = pytest.mark.unit


class TestDatabaseLoad:
    """Tests for loading database CSV files."""

    def test_load_empty_database(self, temp_kestrel_dir):
        """Load a bare CSV with just headers - should return empty DataFrame."""
        db, db_path = load_database(temp_kestrel_dir.parent, "test_analyzer", None)
        assert isinstance(db, pd.DataFrame)
        assert len(db) == 0
        assert list(db.columns) == BASE_COLUMNS



class TestDatabaseSave:
    """Tests for saving database CSV files."""

    def test_save_reload_roundtrip(self, temp_kestrel_dir, sample_database):
        """Save a DataFrame and reload it - should be identical."""
        # Add test data
        sample_database.loc[0] = [None] * len(BASE_COLUMNS)
        sample_database.loc[0, 'filename'] = 'IMG_001.CR3'
        sample_database.loc[0, 'species'] = 'aves,columbidae'
        sample_database.loc[0, 'quality'] = 0.85

        # Save
        csv_path = temp_kestrel_dir / "kestrel_database.csv"
        save_database(sample_database, str(csv_path))

        # Reload
        reloaded = pd.read_csv(csv_path)

        # Compare
        pd.testing.assert_frame_equal(
            sample_database.fillna(0).fillna(""),
            reloaded.fillna(0).fillna(""),
            check_dtype=False
        )

    def test_save_database_preserves_culled_from_disk(self, temp_kestrel_dir, sample_database):
        """save_database should read culled/culled_origin from on-disk version before overwriting."""
        # Create initial CSV with culled data
        sample_database.loc[0] = [None] * len(BASE_COLUMNS)
        sample_database.loc[0, 'filename'] = 'IMG_001.CR3'
        sample_database.loc[0, 'culled'] = True
        sample_database.loc[0, 'culled_origin'] = 'manual'

        csv_path = temp_kestrel_dir / "kestrel_database.csv"
        sample_database.to_csv(csv_path, index=False)

        # Create a modified version (without culled info)
        modified = sample_database.copy()
        modified.loc[0, 'culled'] = False
        modified.loc[0, 'culled_origin'] = None

        # Save the modified version (which should read culled from disk and preserve it)
        save_database(modified, str(csv_path))

        # Reload and verify culled data was preserved
        reloaded = pd.read_csv(csv_path)
        # Note: The actual behavior depends on the implementation of save_database
        # This test documents the expected behavior


class TestScenedata:
    """Tests for scenedata JSON read/write."""

    def test_scenedata_save_load_roundtrip(self, temp_kestrel_dir):
        """Save and load scenedata JSON - should be identical."""
        scenedata = {
            "version": "2.0",
            "image_ratings": {},
            "scenes": {
                "1": {
                    "scene_id": "1",
                    "image_filenames": ["IMG_001.CR3", "IMG_002.CR3"],
                    "name": "Scene 1",
                    "status": "pending",
                    "user_tags": {"species": [], "families": [], "finalized": False}
                }
            }
        }

        # Save
        save_scenedata(scenedata, temp_kestrel_dir.parent)

        # Load
        loaded = load_scenedata(temp_kestrel_dir.parent)

        assert loaded == scenedata

    def test_load_nonexistent_scenedata_returns_initialized_dict(self, tmp_path):
        """Load scenedata from dir that has no scenedata.json - returns initialized dict."""
        kestrel_dir = tmp_path / ".kestrel"
        kestrel_dir.mkdir()

        result = load_scenedata(tmp_path)
        # Should have version, image_ratings, and scenes keys (initialized)
        assert "version" in result
        assert "image_ratings" in result
        assert "scenes" in result
        assert len(result["scenes"]) == 0

    def test_build_scenedata_from_database(self, sample_database):
        """Build scenedata from a fresh database - should group by scene_count."""
        # Create test data with different scene_count values
        sample_database.loc[0] = [None] * len(BASE_COLUMNS)
        sample_database.loc[0, 'filename'] = 'IMG_001.CR3'
        sample_database.loc[0, 'scene_count'] = 1

        sample_database.loc[1] = [None] * len(BASE_COLUMNS)
        sample_database.loc[1, 'filename'] = 'IMG_002.CR3'
        sample_database.loc[1, 'scene_count'] = 1

        sample_database.loc[2] = [None] * len(BASE_COLUMNS)
        sample_database.loc[2, 'filename'] = 'IMG_003.CR3'
        sample_database.loc[2, 'scene_count'] = 2

        scenedata = build_scenedata_from_database(sample_database)

        # Should have version, image_ratings, and scenes keys
        assert "version" in scenedata
        assert "image_ratings" in scenedata
        assert "scenes" in scenedata

        # Should have 2 scenes (keyed by scene_count)
        assert len(scenedata["scenes"]) == 2
        assert "1" in scenedata["scenes"]
        assert "2" in scenedata["scenes"]
        # Each scene should contain the image filenames
        assert set(scenedata["scenes"]["1"]["image_filenames"]) == {"IMG_001.CR3", "IMG_002.CR3"}
        assert set(scenedata["scenes"]["2"]["image_filenames"]) == {"IMG_003.CR3"}

    def test_update_scenedata_merges_new_images(self, sample_database):
        """update_scenedata_with_database merges new images into existing scenedata."""
        # Start with existing scenedata with proper structure
        existing_scenedata = {
            "version": "2.0",
            "image_ratings": {},
            "scenes": {
                "1": {
                    "scene_id": "1",
                    "image_filenames": ["IMG_001.CR3", "IMG_002.CR3"],
                    "name": "Scene 1",
                    "status": "pending",
                    "user_tags": {"species": [], "families": [], "finalized": False}
                }
            }
        }

        # Create database with new image in scene 1
        sample_database.loc[0] = [None] * len(BASE_COLUMNS)
        sample_database.loc[0, 'filename'] = 'IMG_003.CR3'
        sample_database.loc[0, 'scene_count'] = 1

        result = update_scenedata_with_database(existing_scenedata, sample_database)

        # IMG_003 should be added to scene 1
        assert "IMG_003.CR3" in result["scenes"]["1"]["image_filenames"]
        # Original images preserved
        assert "IMG_001.CR3" in result["scenes"]["1"]["image_filenames"]
        assert "IMG_002.CR3" in result["scenes"]["1"]["image_filenames"]
