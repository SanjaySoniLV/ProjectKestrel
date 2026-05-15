"""End-to-end pipeline test: AnalysisPipeline.process_folder() on real fixtures.

Copies set_a_fresh/ to a tmp_path and runs the full pipeline. Verifies the
.kestrel/ directory is created, the database has rows for each input image,
scenedata is correct, and key columns are populated. Tagged @e2e in addition
to @integration because this is the slow, full-stack test.
"""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.config import (
    DATABASE_NAME,
    DEFAULT_DETECTOR_NAME,
    DETECTOR_ONNX_PATHS,
    KESTREL_DIR_NAME,
    QUALITYCLASSIFIER_PATH,
    SAM_DEC_ONNX_PATH,
    SAM_ENC_ONNX_PATH,
    SCENEDATA_FILENAME,
    SPECIESCLASSIFIER_PATH,
    SPECIESNET_MODEL_DIR,
)
from kestrel_analyzer.database import BASE_COLUMNS, load_database, load_scenedata
from kestrel_analyzer.pipeline import AnalysisPipeline


pytestmark = [pytest.mark.integration, pytest.mark.e2e]


_REQUIRED_MODELS = [
    DETECTOR_ONNX_PATHS[DEFAULT_DETECTOR_NAME],
    SPECIESNET_MODEL_DIR / "speciesNet_v4.0.1a.onnx",
    Path(SAM_ENC_ONNX_PATH),
    Path(SAM_DEC_ONNX_PATH),
    Path(SPECIESCLASSIFIER_PATH),
    Path(QUALITYCLASSIFIER_PATH),
]

_skip_no_models = pytest.mark.skipif(
    not all(p.is_file() for p in _REQUIRED_MODELS),
    reason="Required ML model files missing for E2E pipeline",
)


@pytest.fixture
def cr3_workdir(tmp_path, set_a_path):
    """Copy set_a_fresh/ into a writable temp dir."""
    if not set_a_path.exists():
        pytest.skip(f"Fixture {set_a_path} not present")
    cr3_files = sorted(set_a_path.glob("*.CR3"))
    if len(cr3_files) < 2:
        pytest.skip("Need at least 2 CR3 fixtures in set_a_fresh")

    work = tmp_path / "set_a"
    work.mkdir()
    for f in cr3_files:
        shutil.copy2(f, work / f.name)
    return work


@pytest.fixture
def jpeg_workdir(tmp_path, set_d_path):
    if not set_d_path.exists():
        pytest.skip(f"Fixture {set_d_path} not present")
    jpgs = sorted(set_d_path.glob("*.JPG"))
    if len(jpgs) < 2:
        pytest.skip("Need at least 2 JPEG fixtures in set_d")

    work = tmp_path / "set_d"
    work.mkdir()
    for f in jpgs:
        shutil.copy2(f, work / f.name)
    return work


@pytest.fixture
def raw_jpg_mix_workdir(tmp_path, set_e_path):
    if not set_e_path.exists():
        pytest.skip(f"Fixture {set_e_path} not present")
    work = tmp_path / "set_e"
    work.mkdir()
    files = sorted(list(set_e_path.glob("*.CR2")) + list(set_e_path.glob("*.JPG")))
    if not files:
        pytest.skip("Need fixtures in set_e_raw_jpg_mix")
    for f in files:
        shutil.copy2(f, work / f.name)
    return work


@_skip_no_models
class TestPipelineE2EOnCR3:
    """Full pipeline on real CR3 wildlife images."""

    @pytest.fixture(scope="class")
    def pipeline_result_dir(self, request, tmp_path_factory):
        """Run the pipeline once for this test class, cache the result dir."""
        set_a = (
            Path(__file__).parent.parent / "fixtures" / "test_sets" / "set_a_fresh"
        )
        if not set_a.exists():
            pytest.skip(f"Fixture {set_a} not present")
        cr3_files = sorted(set_a.glob("*.CR3"))
        if len(cr3_files) < 2:
            pytest.skip("Need at least 2 CR3 fixtures")

        work = tmp_path_factory.mktemp("pipeline_e2e")
        for f in cr3_files:
            shutil.copy2(f, work / f.name)

        pipeline = AnalysisPipeline(use_gpu=False, detector_name=DEFAULT_DETECTOR_NAME)
        pipeline.process_folder(
            folder=str(work),
            analyzer_name="pytest_e2e",
            wildlife_enabled=True,
            species_detection_enabled=True,
            detection_threshold=0.25,
            scene_time_threshold=60.0,
            max_bird_crops=5,
            parallel_prefetch=1,
        )
        return work, len(cr3_files)

    def test_kestrel_dir_created(self, pipeline_result_dir):
        work, _ = pipeline_result_dir
        kestrel_dir = work / KESTREL_DIR_NAME
        assert kestrel_dir.is_dir()
        assert (kestrel_dir / "export").is_dir()
        assert (kestrel_dir / "crop").is_dir()

    def test_database_csv_exists(self, pipeline_result_dir):
        work, _ = pipeline_result_dir
        csv_path = work / KESTREL_DIR_NAME / DATABASE_NAME
        assert csv_path.is_file()
        assert csv_path.stat().st_size > 0

    def test_database_has_row_per_image(self, pipeline_result_dir):
        work, n_files = pipeline_result_dir
        kestrel_dir = str(work / KESTREL_DIR_NAME)
        db, _ = load_database(kestrel_dir, "pytest_e2e")
        assert len(db) == n_files

    def test_database_columns_match_schema(self, pipeline_result_dir):
        work, _ = pipeline_result_dir
        kestrel_dir = str(work / KESTREL_DIR_NAME)
        db, _ = load_database(kestrel_dir, "pytest_e2e")
        for col in BASE_COLUMNS:
            assert col in db.columns, f"Missing column: {col}"

    def test_filenames_recorded(self, pipeline_result_dir):
        work, _ = pipeline_result_dir
        kestrel_dir = str(work / KESTREL_DIR_NAME)
        db, _ = load_database(kestrel_dir, "pytest_e2e")
        disk_files = {f.name for f in work.glob("*.CR3")}
        db_files = set(db["filename"].astype(str))
        assert disk_files == db_files

    def test_capture_time_populated(self, pipeline_result_dir):
        work, _ = pipeline_result_dir
        kestrel_dir = str(work / KESTREL_DIR_NAME)
        db, _ = load_database(kestrel_dir, "pytest_e2e")
        # capture_time should be non-empty for real CR3 images with EXIF
        non_empty = db["capture_time"].astype(str).str.len() > 0
        assert non_empty.all(), "Some rows have empty capture_time"

    def test_scenedata_json_exists(self, pipeline_result_dir):
        work, _ = pipeline_result_dir
        scenedata_path = work / KESTREL_DIR_NAME / SCENEDATA_FILENAME
        assert scenedata_path.is_file()
        data = json.loads(scenedata_path.read_text(encoding="utf-8"))
        assert "scenes" in data or "version" in data  # current schema

    def test_scene_grouping_present(self, pipeline_result_dir):
        work, _ = pipeline_result_dir
        kestrel_dir = str(work / KESTREL_DIR_NAME)
        db, _ = load_database(kestrel_dir, "pytest_e2e")
        # Every row should have a non-null scene_count
        assert db["scene_count"].notna().all()
        # Scene counts should be small positive integers
        scenes = db["scene_count"].astype(int)
        assert (scenes >= 1).all()

    def test_exposure_correction_populated(self, pipeline_result_dir):
        work, _ = pipeline_result_dir
        kestrel_dir = str(work / KESTREL_DIR_NAME)
        db, _ = load_database(kestrel_dir, "pytest_e2e")
        ec = db["exposure_correction"].dropna()
        assert len(ec) == len(db), "Some rows missing exposure_correction"

    def test_export_thumbnails_created(self, pipeline_result_dir):
        work, _ = pipeline_result_dir
        export_dir = work / KESTREL_DIR_NAME / "export"
        thumbs = list(export_dir.glob("*.jpg"))
        assert len(thumbs) >= 1, "Pipeline produced no export thumbnails"


@_skip_no_models
class TestPipelineE2EOnJPEG:
    """Pipeline should also handle JPEG-only folders."""

    def test_process_jpeg_only_folder(self, jpeg_workdir):
        pipeline = AnalysisPipeline(use_gpu=False, detector_name=DEFAULT_DETECTOR_NAME)
        pipeline.process_folder(
            folder=str(jpeg_workdir),
            analyzer_name="pytest_e2e_jpeg",
            wildlife_enabled=True,
            species_detection_enabled=True,
            detection_threshold=0.25,
            scene_time_threshold=60.0,
            max_bird_crops=5,
            parallel_prefetch=1,
        )
        kestrel_dir = jpeg_workdir / KESTREL_DIR_NAME
        assert kestrel_dir.is_dir()
        csv_path = kestrel_dir / DATABASE_NAME
        assert csv_path.is_file()

        db, _ = load_database(str(kestrel_dir), "pytest_e2e_jpeg")
        disk_jpgs = {f.name for f in jpeg_workdir.glob("*.JPG")}
        assert len(db) == len(disk_jpgs)


@_skip_no_models
class TestPipelineE2ERAWPreferredOverJPEG:
    """When both RAW and JPEG are present, the pipeline picks RAW only."""

    def test_raw_preferred(self, raw_jpg_mix_workdir):
        pipeline = AnalysisPipeline(use_gpu=False, detector_name=DEFAULT_DETECTOR_NAME)
        pipeline.process_folder(
            folder=str(raw_jpg_mix_workdir),
            analyzer_name="pytest_e2e_mix",
            wildlife_enabled=True,
            species_detection_enabled=True,
            detection_threshold=0.25,
            scene_time_threshold=60.0,
            max_bird_crops=5,
            parallel_prefetch=1,
        )
        kestrel_dir = raw_jpg_mix_workdir / KESTREL_DIR_NAME
        db, _ = load_database(str(kestrel_dir), "pytest_e2e_mix")
        # Pipeline should pick RAW exclusively when both are present
        cr2_count = len(list(raw_jpg_mix_workdir.glob("*.CR2")))
        assert len(db) == cr2_count, (
            f"Expected {cr2_count} RAW rows, got {len(db)}: {list(db['filename'])}"
        )
        # No row's filename should end in .jpg
        for fn in db["filename"].astype(str):
            assert not fn.lower().endswith(".jpg"), f"Got JPEG row when RAW present: {fn}"
