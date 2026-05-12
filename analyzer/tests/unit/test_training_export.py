"""Unit tests for training_export.py - per-crop training-data dump helpers."""

import csv
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer import training_export
from kestrel_analyzer.ml.quality import QualityClassifier


pytestmark = pytest.mark.unit


def _fake_crop_and_mask():
    rng = np.random.default_rng(42)
    crop = rng.integers(0, 256, size=(1024, 1024, 3), dtype=np.uint8)
    mask = np.zeros((1024, 1024), dtype=np.uint8)
    mask[200:800, 200:800] = 1
    return crop, mask


class TestMakeCropId:
    def test_basic(self):
        cid = training_export.make_crop_id(r"C:\photos\session_a", "IMG_1234.CR3", 0)
        assert cid == "session_a__IMG_1234_crop_0"

    def test_trailing_separator(self):
        cid = training_export.make_crop_id(r"C:\photos\session_a\\", "x.jpg", 2)
        assert cid == "session_a__x_crop_2"

    def test_root_folder(self):
        cid = training_export.make_crop_id("/", "x.jpg", 0)
        assert cid.endswith("__x_crop_0")


class TestWriteCropArtifacts:
    def test_writes_three_files(self, tmp_path):
        crop, mask = _fake_crop_and_mask()
        paths = training_export.write_crop_artifacts(str(tmp_path), "test_crop_0", crop, mask)

        assert (tmp_path / "test_crop_0_input.npy").exists()
        assert (tmp_path / "test_crop_0_rgb.jpg").exists()
        assert (tmp_path / "test_crop_0_mask.png").exists()
        assert paths == {
            "input_npy_path": "test_crop_0_input.npy",
            "rgb_jpg_path": "test_crop_0_rgb.jpg",
            "mask_png_path": "test_crop_0_mask.png",
        }

    def test_npy_shape_and_dtype(self, tmp_path):
        crop, mask = _fake_crop_and_mask()
        training_export.write_crop_artifacts(str(tmp_path), "x", crop, mask)
        loaded = np.load(tmp_path / "x_input.npy")
        assert loaded.shape == (1024, 1024, 1)
        assert loaded.dtype == np.float32

    def test_npy_matches_quality_classifier_preprocess(self, tmp_path):
        """The dumped .npy must equal what production inference computes."""
        crop, mask = _fake_crop_and_mask()
        training_export.write_crop_artifacts(str(tmp_path), "x", crop, mask)
        loaded = np.load(tmp_path / "x_input.npy")
        expected = QualityClassifier._preprocess(crop, mask).astype(np.float32)
        np.testing.assert_array_equal(loaded, expected)

    def test_creates_output_dir(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c"
        crop, mask = _fake_crop_and_mask()
        training_export.write_crop_artifacts(str(nested), "x", crop, mask)
        assert (nested / "x_input.npy").exists()


class TestAppendManifestRows:
    def _row(self, crop_id, source_filename="img.CR3", crop_index=0):
        return {
            "crop_id": crop_id,
            "source_folder": "folder_a",
            "source_filename": source_filename,
            "crop_index": crop_index,
            "detection_index": 0,
            "detection_confidence": 0.9,
            "species": "robin",
            "species_confidence": 0.8,
            "family": "Turdidae",
            "family_confidence": 0.75,
            "quality_legacy_model": 0.6,
            "rating_legacy_model": 3,
            "exposure_correction": 0.0,
            "exposure_pipeline": "numpy_linear_v2",
            "exposure_subject_stops": 0.0,
            "exposure_meter_scale": 1.0,
            "bbox_x_min": 0,
            "bbox_x_max": 100,
            "bbox_y_min": 0,
            "bbox_y_max": 100,
            "capture_time": "2026-01-01T00:00:00",
            "orientation": "landscape",
            "input_npy_path": f"{crop_id}_input.npy",
            "rgb_jpg_path": f"{crop_id}_rgb.jpg",
            "mask_png_path": f"{crop_id}_mask.png",
        }

    def test_creates_file_with_header(self, tmp_path):
        training_export.append_manifest_rows(str(tmp_path), [self._row("a")])
        manifest_path = tmp_path / training_export.MANIFEST_FILENAME
        assert manifest_path.exists()
        with open(manifest_path, newline="", encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader)
            assert header == training_export.MANIFEST_COLUMNS
            assert next(reader)[0] == "a"

    def test_appends_without_duplicating_header(self, tmp_path):
        training_export.append_manifest_rows(str(tmp_path), [self._row("a")])
        training_export.append_manifest_rows(str(tmp_path), [self._row("b"), self._row("c")])
        manifest_path = tmp_path / training_export.MANIFEST_FILENAME
        with open(manifest_path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        assert rows[0] == training_export.MANIFEST_COLUMNS
        assert [r[0] for r in rows[1:]] == ["a", "b", "c"]

    def test_empty_rows_is_noop(self, tmp_path):
        training_export.append_manifest_rows(str(tmp_path), [])
        assert not (tmp_path / training_export.MANIFEST_FILENAME).exists()

    def test_extra_keys_are_ignored(self, tmp_path):
        row = self._row("a")
        row["nonexistent_column"] = "ignored"
        training_export.append_manifest_rows(str(tmp_path), [row])
        manifest_path = tmp_path / training_export.MANIFEST_FILENAME
        with open(manifest_path, newline="", encoding="utf-8") as f:
            header = next(csv.reader(f))
        assert "nonexistent_column" not in header


class TestBuildManifestRow:
    def test_canonical_row(self):
        crop_item = {
            "index": 2,
            "confidence": 0.88,
            "species": "Cardinalis cardinalis",
            "species_confidence": 0.91,
            "family": "Cardinalidae",
            "family_confidence": 0.82,
            "quality": 0.65,
            "rating": 4,
            "exposure_correction": -0.3,
            "exposure_pipeline": "numpy_linear_v2",
            "exposure_subject_stops": 0.1,
            "exposure_meter_scale": 1.2,
        }
        serialized_crop = {
            "bbox": {"x_min": 10, "x_max": 110, "y_min": 20, "y_max": 120},
        }
        row = training_export.build_manifest_row(
            crop_id="folder_a__IMG_1_crop_0",
            source_folder=r"C:\photos\folder_a",
            source_filename="IMG_1.CR3",
            crop_index=0,
            crop_item=crop_item,
            serialized_crop=serialized_crop,
            capture_time="2026-05-11T10:00:00",
            orientation="landscape",
            artifact_paths={
                "input_npy_path": "x_input.npy",
                "rgb_jpg_path": "x_rgb.jpg",
                "mask_png_path": "x_mask.png",
            },
        )

        assert row["crop_id"] == "folder_a__IMG_1_crop_0"
        assert row["source_folder"] == "folder_a"
        assert row["source_filename"] == "IMG_1.CR3"
        assert row["crop_index"] == 0
        assert row["detection_index"] == 2
        assert row["detection_confidence"] == pytest.approx(0.88)
        assert row["species"] == "Cardinalis cardinalis"
        assert row["quality_legacy_model"] == pytest.approx(0.65)
        assert row["rating_legacy_model"] == 4
        assert row["bbox_x_min"] == 10
        assert row["bbox_x_max"] == 110
        assert row["bbox_y_min"] == 20
        assert row["bbox_y_max"] == 120
        assert row["capture_time"] == "2026-05-11T10:00:00"
        assert row["input_npy_path"] == "x_input.npy"

    def test_handles_missing_optional_fields(self):
        row = training_export.build_manifest_row(
            crop_id="cid",
            source_folder="f",
            source_filename="x.jpg",
            crop_index=0,
            crop_item={"index": 0},
            serialized_crop={},
            capture_time="",
            orientation="",
            artifact_paths={
                "input_npy_path": "x.npy",
                "rgb_jpg_path": "x.jpg",
                "mask_png_path": "x.png",
            },
        )
        assert row["detection_confidence"] == 0.0
        assert row["species"] == "Unknown"
        assert row["family"] == "Unknown"
        assert row["quality_legacy_model"] == -1.0
        assert row["bbox_x_min"] == 0
