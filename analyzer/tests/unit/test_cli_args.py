"""Unit tests for analyzer/cli.py argument parsing.

These cover the full set of "Advanced Analysis Settings" flags that the CLI
exposes alongside the existing folder/--gpu/--detection-threshold/--parallel-
prefetch flags. They are deliberately argparse-only (no ML weights loaded) so
they run in the fast `unit` lane.

Wiring tests further down verify that ``main()`` actually forwards the parsed
values to ``AnalysisPipeline.process_folder``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cli as cli_module  # noqa: E402  (after sys.path mutation)
from cli import (  # noqa: E402
    WILDLIFE_MODEL_MODE_TO_DETECTOR,
    _resolve_detector_name,
    parse_args,
)
from kestrel_analyzer.config import DEFAULT_DETECTOR_NAME  # noqa: E402


pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# parse_args: defaults
# ---------------------------------------------------------------------------

class TestDefaults:
    def test_minimal_invocation_sets_defaults(self):
        args = parse_args(["/tmp/photos"])
        assert args.folder == "/tmp/photos"
        assert args.use_gpu is True
        assert args.detector_name is None
        assert args.wildlife_model_mode is None
        assert args.detection_threshold == pytest.approx(0.25)
        assert args.parallel_prefetch == 3
        assert args.max_bird_crops == 10
        assert args.exposure_quality is None
        assert args.scene_time_threshold == pytest.approx(1.0)
        assert args.thumbnail_max_width is None
        assert args.thumbnail_jpeg_compression is None
        assert args.wildlife_enabled is False
        assert args.species_detection_enabled is True
        assert args.retry_errored is False
        assert args.smoke is False
        assert args.validate is False

    def test_folder_optional_when_validate(self):
        args = parse_args(["--validate", "--validate-images", "test_imgs"])
        assert args.folder is None
        assert args.validate is True
        assert args.validate_images == "test_imgs"


# ---------------------------------------------------------------------------
# parse_args: each new flag in isolation
# ---------------------------------------------------------------------------

class TestNewFlagsAccepted:
    def test_max_bird_crops(self):
        args = parse_args(["/tmp/photos", "--max-bird-crops", "15"])
        assert args.max_bird_crops == 15

    def test_exposure_quality_lenient(self):
        args = parse_args(["/tmp/photos", "--exposure-quality", "lenient"])
        assert args.exposure_quality == "lenient"

    def test_exposure_quality_balanced(self):
        args = parse_args(["/tmp/photos", "--exposure-quality", "balanced"])
        assert args.exposure_quality == "balanced"

    def test_exposure_quality_aggressive(self):
        args = parse_args(["/tmp/photos", "--exposure-quality", "aggressive"])
        assert args.exposure_quality == "aggressive"

    def test_exposure_quality_rejects_bogus(self):
        with pytest.raises(SystemExit):
            parse_args(["/tmp/photos", "--exposure-quality", "yolo"])

    def test_scene_time_threshold(self):
        args = parse_args(["/tmp/photos", "--scene-time-threshold", "5.5"])
        assert args.scene_time_threshold == pytest.approx(5.5)

    def test_thumbnail_max_width(self):
        args = parse_args(["/tmp/photos", "--thumbnail-max-width", "1800"])
        assert args.thumbnail_max_width == 1800

    def test_thumbnail_jpeg_compression(self):
        args = parse_args(["/tmp/photos", "--thumbnail-jpeg-compression", "0.85"])
        assert args.thumbnail_jpeg_compression == pytest.approx(0.85)

    def test_wildlife_model_mode_fast(self):
        args = parse_args(["/tmp/photos", "--wildlife-model-mode", "fast"])
        assert args.wildlife_model_mode == "fast"

    def test_wildlife_model_mode_accurate(self):
        args = parse_args(["/tmp/photos", "--wildlife-model-mode", "accurate"])
        assert args.wildlife_model_mode == "accurate"

    def test_wildlife_model_mode_rejects_bogus(self):
        with pytest.raises(SystemExit):
            parse_args(["/tmp/photos", "--wildlife-model-mode", "ultra"])

    def test_wildlife_toggle(self):
        on = parse_args(["/tmp/photos", "--wildlife"])
        off = parse_args(["/tmp/photos", "--no-wildlife"])
        assert on.wildlife_enabled is True
        assert off.wildlife_enabled is False

    def test_species_detection_toggle(self):
        on = parse_args(["/tmp/photos", "--species-detection"])
        off = parse_args(["/tmp/photos", "--no-species-detection"])
        assert on.species_detection_enabled is True
        assert off.species_detection_enabled is False

    def test_retry_errored_toggle(self):
        on = parse_args(["/tmp/photos", "--retry-errored"])
        off = parse_args(["/tmp/photos", "--no-retry-errored"])
        assert on.retry_errored is True
        assert off.retry_errored is False


# ---------------------------------------------------------------------------
# parse_args: every flag at once still parses
# ---------------------------------------------------------------------------

class TestFullCommandLine:
    def test_kitchen_sink(self):
        args = parse_args([
            "/tmp/photos",
            "--no-gpu",
            "--wildlife-model-mode", "fast",
            "--detection-threshold", "0.4",
            "--parallel-prefetch", "2",
            "--max-bird-crops", "7",
            "--exposure-quality", "aggressive",
            "--scene-time-threshold", "2.5",
            "--thumbnail-max-width", "2000",
            "--thumbnail-jpeg-compression", "0.9",
            "--wildlife",
            "--no-species-detection",
            "--retry-errored",
        ])
        assert args.folder == "/tmp/photos"
        assert args.use_gpu is False
        assert args.wildlife_model_mode == "fast"
        assert args.detection_threshold == pytest.approx(0.4)
        assert args.parallel_prefetch == 2
        assert args.max_bird_crops == 7
        assert args.exposure_quality == "aggressive"
        assert args.scene_time_threshold == pytest.approx(2.5)
        assert args.thumbnail_max_width == 2000
        assert args.thumbnail_jpeg_compression == pytest.approx(0.9)
        assert args.wildlife_enabled is True
        assert args.species_detection_enabled is False
        assert args.retry_errored is True


# ---------------------------------------------------------------------------
# _resolve_detector_name: precedence between --detector-name and
# --wildlife-model-mode.
# ---------------------------------------------------------------------------

class TestDetectorResolution:
    def test_neither_flag_falls_back_to_default(self):
        args = parse_args(["/tmp/photos"])
        assert _resolve_detector_name(args) == DEFAULT_DETECTOR_NAME

    def test_wildlife_model_mode_fast_resolves_to_cedar(self):
        args = parse_args(["/tmp/photos", "--wildlife-model-mode", "fast"])
        assert _resolve_detector_name(args) == "mdv1000-cedar"

    def test_wildlife_model_mode_accurate_resolves_to_mdv5a(self):
        args = parse_args(["/tmp/photos", "--wildlife-model-mode", "accurate"])
        assert _resolve_detector_name(args) == "mdv5a"

    def test_detector_name_takes_precedence(self):
        args = parse_args([
            "/tmp/photos",
            "--detector-name", "mdv5a",
            "--wildlife-model-mode", "fast",
        ])
        # --detector-name wins even when --wildlife-model-mode says 'fast'
        assert _resolve_detector_name(args) == "mdv5a"

    def test_mode_map_matches_visualizer_js(self):
        # Guard against drift between this map and the JS mapping at
        # visualizer.js:8077 (modelVal === 'accurate' ? 'mdv5a' : 'mdv1000-cedar').
        assert WILDLIFE_MODEL_MODE_TO_DETECTOR == {
            "fast": "mdv1000-cedar",
            "accurate": "mdv5a",
        }


# ---------------------------------------------------------------------------
# Wiring: main() forwards clamped / resolved values to process_folder.
# ---------------------------------------------------------------------------

class TestMainForwardsToPipeline:
    """Patch AnalysisPipeline; assert process_folder is called with the
    expected kwargs (and clamped where the CLI clamps).
    """

    def _run_main(self, argv):
        with patch.object(cli_module, "AnalysisPipeline") as PipelineCls:
            instance = MagicMock()
            PipelineCls.return_value = instance
            cli_module.main(argv)
            return PipelineCls, instance

    def test_minimal_invocation_forwards_defaults(self, tmp_path):
        # Use a real (empty) folder so cli.main() reaches pipeline.process_folder
        folder = tmp_path / "photos"
        folder.mkdir()
        PipelineCls, instance = self._run_main([str(folder)])

        PipelineCls.assert_called_once()
        ctor_kwargs = PipelineCls.call_args.kwargs
        assert ctor_kwargs["use_gpu"] is True
        assert ctor_kwargs["detector_name"] == DEFAULT_DETECTOR_NAME

        instance.process_folder.assert_called_once()
        pf_kwargs = instance.process_folder.call_args.kwargs
        assert pf_kwargs["analyzer_name"] == "cli"
        assert pf_kwargs["wildlife_enabled"] is False
        assert pf_kwargs["species_detection_enabled"] is True
        assert pf_kwargs["retry_errored"] is False
        assert pf_kwargs["detection_threshold"] == pytest.approx(0.25)
        assert pf_kwargs["scene_time_threshold"] == pytest.approx(1.0)
        assert pf_kwargs["max_bird_crops"] == 10
        assert pf_kwargs["parallel_prefetch"] == 3
        # When the CLI flag is omitted, the override stays None so the
        # pipeline falls back to settings.json / defaults.
        assert pf_kwargs["exposure_quality"] is None
        assert pf_kwargs["thumbnail_max_width"] is None
        assert pf_kwargs["thumbnail_jpeg_compression"] is None

    def test_full_invocation_forwards_all_flags(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        PipelineCls, instance = self._run_main([
            str(folder),
            "--no-gpu",
            "--wildlife-model-mode", "fast",
            "--detection-threshold", "0.4",
            "--parallel-prefetch", "2",
            "--max-bird-crops", "7",
            "--exposure-quality", "aggressive",
            "--scene-time-threshold", "2.5",
            "--thumbnail-max-width", "2000",
            "--thumbnail-jpeg-compression", "0.9",
            "--wildlife",
            "--no-species-detection",
            "--retry-errored",
        ])

        ctor_kwargs = PipelineCls.call_args.kwargs
        assert ctor_kwargs["use_gpu"] is False
        # --wildlife-model-mode fast -> mdv1000-cedar
        assert ctor_kwargs["detector_name"] == "mdv1000-cedar"

        pf_kwargs = instance.process_folder.call_args.kwargs
        assert pf_kwargs["wildlife_enabled"] is True
        assert pf_kwargs["species_detection_enabled"] is False
        assert pf_kwargs["retry_errored"] is True
        assert pf_kwargs["detection_threshold"] == pytest.approx(0.4)
        assert pf_kwargs["scene_time_threshold"] == pytest.approx(2.5)
        assert pf_kwargs["max_bird_crops"] == 7
        assert pf_kwargs["parallel_prefetch"] == 2
        assert pf_kwargs["exposure_quality"] == "aggressive"
        assert pf_kwargs["thumbnail_max_width"] == 2000
        assert pf_kwargs["thumbnail_jpeg_compression"] == pytest.approx(0.9)

    def test_detector_name_overrides_wildlife_model_mode(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        PipelineCls, _ = self._run_main([
            str(folder),
            "--detector-name", "mdv5a",
            "--wildlife-model-mode", "fast",
        ])
        # --detector-name wins; should be mdv5a even though mode says fast
        assert PipelineCls.call_args.kwargs["detector_name"] == "mdv5a"

    # --- Clamping ---

    def test_detection_threshold_clamped_low(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        _, instance = self._run_main([str(folder), "--detection-threshold", "0.001"])
        assert instance.process_folder.call_args.kwargs["detection_threshold"] == pytest.approx(0.10)

    def test_detection_threshold_clamped_high(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        _, instance = self._run_main([str(folder), "--detection-threshold", "2.5"])
        assert instance.process_folder.call_args.kwargs["detection_threshold"] == pytest.approx(0.99)

    def test_max_bird_crops_clamped_low(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        _, instance = self._run_main([str(folder), "--max-bird-crops", "0"])
        assert instance.process_folder.call_args.kwargs["max_bird_crops"] == 1

    def test_max_bird_crops_clamped_high(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        _, instance = self._run_main([str(folder), "--max-bird-crops", "999"])
        assert instance.process_folder.call_args.kwargs["max_bird_crops"] == 20

    def test_thumbnail_max_width_clamped(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        _, instance = self._run_main([str(folder), "--thumbnail-max-width", "10"])
        # Floor is 400
        assert instance.process_folder.call_args.kwargs["thumbnail_max_width"] == 400
        _, instance = self._run_main([str(folder), "--thumbnail-max-width", "99999"])
        # Ceiling is 2400
        assert instance.process_folder.call_args.kwargs["thumbnail_max_width"] == 2400

    def test_thumbnail_jpeg_compression_clamped(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        _, instance = self._run_main([str(folder), "--thumbnail-jpeg-compression", "0.0"])
        assert instance.process_folder.call_args.kwargs["thumbnail_jpeg_compression"] == pytest.approx(0.5)
        _, instance = self._run_main([str(folder), "--thumbnail-jpeg-compression", "5.0"])
        assert instance.process_folder.call_args.kwargs["thumbnail_jpeg_compression"] == pytest.approx(1.0)

    def test_parallel_prefetch_clamped(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        _, instance = self._run_main([str(folder), "--parallel-prefetch", "99"])
        assert instance.process_folder.call_args.kwargs["parallel_prefetch"] == 5
        _, instance = self._run_main([str(folder), "--parallel-prefetch", "0"])
        assert instance.process_folder.call_args.kwargs["parallel_prefetch"] == 1

    def test_scene_time_threshold_clamped(self, tmp_path):
        folder = tmp_path / "photos"
        folder.mkdir()
        _, instance = self._run_main([str(folder), "--scene-time-threshold", "-1"])
        assert instance.process_folder.call_args.kwargs["scene_time_threshold"] == pytest.approx(0.0)
        _, instance = self._run_main([str(folder), "--scene-time-threshold", "9999"])
        assert instance.process_folder.call_args.kwargs["scene_time_threshold"] == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# Pipeline parameter wiring: the new override knobs reach pipeline state.
# ---------------------------------------------------------------------------

class TestPipelineOverrides:
    """Smoke-test that ``process_folder`` accepts and clamps the three new
    override parameters without exploding on an empty folder. The full
    pipeline doesn't run because there are no files to process."""

    @pytest.fixture
    def empty_folder(self, tmp_path):
        folder = tmp_path / "empty"
        folder.mkdir()
        return folder

    def test_overrides_accepted_on_empty_folder(self, empty_folder):
        from kestrel_analyzer.pipeline import AnalysisPipeline
        pipeline = AnalysisPipeline(use_gpu=False, detector_name=DEFAULT_DETECTOR_NAME)
        # No images -> bails out before model loading. We're checking that the
        # new signature accepts the kwargs without TypeError.
        pipeline.process_folder(
            folder=str(empty_folder),
            analyzer_name="unit_test",
            exposure_quality="aggressive",
            thumbnail_max_width=1500,
            thumbnail_jpeg_compression=0.65,
        )

    def test_invalid_exposure_quality_falls_through_to_default(self, empty_folder):
        from kestrel_analyzer.pipeline import AnalysisPipeline
        pipeline = AnalysisPipeline(use_gpu=False, detector_name=DEFAULT_DETECTOR_NAME)
        # Invalid string is silently ignored so we keep CLI/UI parity (the
        # caller is trusted to pass an enum value).
        pipeline.process_folder(
            folder=str(empty_folder),
            analyzer_name="unit_test",
            exposure_quality="not-a-real-mode",
        )
