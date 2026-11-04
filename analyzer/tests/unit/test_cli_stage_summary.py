"""The CLI's end-of-run per-stage timing summary."""

import pytest

pytestmark = pytest.mark.unit

cli = pytest.importorskip("cli")  # imports the pipeline (cv2 / onnxruntime)


def test_stage_summary_per_image_and_other():
    lines = cli.format_stage_summary({"detector_sam": 8.0, "bioclip": 2.0}, {"detector_sam": 4, "bioclip": 6},
                                     n_images=4, wall_s=12.0)
    assert lines[0] == "[stage-times] 4 images, wall 12.0s (3.000 s/image)"
    assert "detector_sam" in lines[1] and "calls      4" in lines[1] and lines[1].endswith("2.000 s/image")
    assert "bioclip" in lines[2] and lines[2].endswith("0.500 s/image")
    assert "other" in lines[3] and lines[3].endswith("0.500 s/image")


def test_stage_summary_no_images_does_not_divide_by_zero():
    lines = cli.format_stage_summary({}, {}, n_images=0, wall_s=1.0)
    assert lines[0].startswith("[stage-times] 0 images")
    assert lines[-1].endswith("1.000 s/image")
