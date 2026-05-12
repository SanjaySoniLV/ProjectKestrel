import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).parent))
from kestrel_analyzer.pipeline import AnalysisPipeline
from kestrel_analyzer.logging_utils import get_log_path, log_event, log_exception
from kestrel_analyzer.config import (
    DEFAULT_DETECTOR_NAME,
    DETECTOR_ONNX_PATHS,
    JPEG_EXTENSIONS,
    RAW_EXTENSIONS,
)


def parse_args(argv: Sequence[str] | None = None):
    detector_choices = sorted(DETECTOR_ONNX_PATHS.keys())
    parser = argparse.ArgumentParser(description="Kestrel Analyzer CLI")
    parser.add_argument(
        "folder",
        nargs="?",
        default=None,
        help="Folder with RAW/JPEG images (optional when --validate is set)",
    )
    parser.add_argument("--gpu", dest="use_gpu", action="store_true", help="Use GPU (DirectML on Windows, CoreML on macOS) for ONNX")
    parser.add_argument("--no-gpu", dest="use_gpu", action="store_false", help="Force CPU for ONNX")
    parser.add_argument(
        "--detector-name",
        choices=detector_choices,
        default=DEFAULT_DETECTOR_NAME,
        help="Select detector model variant.",
    )
    parser.add_argument(
        "--detection-threshold",
        type=float,
        default=0.25,
        help="Minimum detection confidence threshold (0.10-0.99); matches desktop default.",
    )
    parser.add_argument(
        "--parallel-prefetch",
        type=int,
        default=3,
        help="Parallel RAW decode workers (1-5); matches desktop 'parallel prefetch' setting.",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Load a single image via Wand and exit (skips model loading)",
    )
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Run the 9-check end-to-end validation harness and exit.",
    )
    parser.add_argument(
        "--validate-images",
        type=str,
        default=None,
        help="Folder with at least 2 sample images for --validate (e.g. test_imgs/).",
    )
    parser.add_argument(
        "--validate-output",
        type=str,
        default=None,
        help="Path for the --validate result JSON. Recommended on Windows where console=False hides stdout.",
    )
    parser.add_argument(
        "--export-training-data",
        type=str,
        default=None,
        metavar="DIR",
        help="Additionally dump per-detection (preprocessed .npy + RGB crop + mask + manifest) to DIR for quality-model retraining.",
    )
    parser.set_defaults(use_gpu=True)
    return parser.parse_args(argv)


def _find_first_image(folder: str) -> str | None:
    files = [
        f
        for f in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, f))
        and os.path.splitext(f)[1].lower() in RAW_EXTENSIONS
    ]
    if not files:
        files = [
            f
            for f in os.listdir(folder)
            if os.path.isfile(os.path.join(folder, f))
            and os.path.splitext(f)[1].lower() in JPEG_EXTENSIONS
        ]
    files.sort()
    if not files:
        return None
    return os.path.join(folder, files[0])


def main(argv: Sequence[str] | None = None):
    log_path = get_log_path(None)
    try:
        args = parse_args(argv)
        if args.validate:
            from kestrel_analyzer.validation import run_validation
            sys.exit(run_validation(
                images_dir=args.validate_images,
                output_path=args.validate_output,
            ))
        if not args.folder:
            print("error: 'folder' is required unless --validate is set", flush=True, file=sys.stderr)
            sys.exit(2)
        detection_threshold = max(0.10, min(0.99, float(args.detection_threshold)))
        parallel_pf = max(1, min(5, int(float(args.parallel_prefetch))))
        log_path = get_log_path(args.folder)
        if args.smoke:
            log_event(
                log_path,
                {
                    "level": "info",
                    "event": "cli_smoke_start",
                    "folder": args.folder,
                },
            )
            image_path = _find_first_image(args.folder)
            if not image_path:
                print("No supported image files found.", flush=True)
                return
            
            print(f"Smoke test: reading image {os.path.basename(image_path)}", flush=True)
            print(f"Smoke test image path: {image_path}", flush=True)
            print(f"Smoke test image exists: {os.path.exists(image_path)}", flush=True)
            
            from kestrel_analyzer.image_utils import read_image
            
            img_array = read_image(image_path)
            if img_array is not None:
                print(f"Smoke test SUCCESS: Read image with shape {img_array.shape}", flush=True)
            else:
                print("Smoke test FAILED: read_image returned None", flush=True)
            return
        pipeline = AnalysisPipeline(
            use_gpu=args.use_gpu,
            detector_name=args.detector_name,
        )

        def on_status(msg):
            print(msg)

        def on_progress(processed, total):
            print(f"\rProcessed {processed}/{total}", end="", flush=True)

        log_event(
            log_path,
            {
                "level": "info",
                "event": "cli_start",
                "folder": args.folder,
                "use_gpu": args.use_gpu,
                "detector_name": args.detector_name,
                "detection_threshold": detection_threshold,
                "parallel_prefetch": parallel_pf,
            },
        )

        pipeline.process_folder(
            args.folder,
            callbacks={
                "on_status": on_status,
                "on_progress": on_progress,
            },
            analyzer_name="cli",
            detection_threshold=detection_threshold,
            parallel_prefetch=parallel_pf,
            training_export_dir=args.export_training_data,
        )
        print()
    except Exception as e:
        log_exception(
            log_path,
            e,
            stage="startup",
            context={"analyzer": "cli"},
        )
        raise


if __name__ == "__main__":
    main()
