import argparse
import json as _json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).parent))
from kestrel_analyzer.pipeline import AnalysisPipeline
from kestrel_analyzer.logging_utils import get_log_path, log_event, log_exception
from kestrel_analyzer.config import (
    DEFAULT_DETECTOR_NAME,
    DETECTOR_ONNX_PATHS,
    select_camera_images,
)


# Maps the GUI's "Wildlife Detection Model" select to the corresponding
# DETECTOR_ONNX_PATHS key. Matches the JS mapping in visualizer.js.
WILDLIFE_MODEL_MODE_TO_DETECTOR = {
    "fast": "mdv1000-cedar",
    "accurate": "mdv5a",
}


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
        default=None,
        help="Select detector model variant by ONNX key (advanced). Overrides --wildlife-model-mode.",
    )
    parser.add_argument(
        "--wildlife-model-mode",
        choices=("fast", "accurate"),
        default=None,
        help=(
            "GUI-aligned detector selector: 'fast' uses mdv1000-cedar (smaller); "
            "'accurate' uses mdv5a (larger, stronger recall). Default: 'accurate'."
        ),
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
        "--json-output",
        action="store_true",
        help="Emit machine-readable JSON events on stdout for a parent process.",
    )
    parser.add_argument(
        "--stop-file",
        type=str,
        default=None,
        help="Path to a sentinel file; when it appears the CLI drains and exits cleanly.",
    )
    parser.add_argument(
        "--max-bird-crops",
        type=int,
        default=10,
        help="Max bird/wildlife subjects saved per image (1-20); matches desktop default.",
    )
    parser.add_argument(
        "--exposure-quality",
        choices=("lenient", "balanced", "aggressive"),
        default=None,
        help=(
            "Exposure correction aggressiveness. 'lenient' (50%%, ±3 stops), "
            "'balanced' (75%%, ±5 stops), 'aggressive' (100%%, ±8 stops). "
            "Default: read settings.json or 'balanced'."
        ),
    )
    parser.add_argument(
        "--scene-time-threshold",
        type=float,
        default=1.0,
        help="Seconds between images to group as one burst/scene (0-60). Default: 1.0.",
    )
    parser.add_argument(
        "--thumbnail-max-width",
        type=int,
        default=None,
        help=(
            "Long-edge pixel width for cached analysis thumbnails (400-2400). "
            "Default: read settings.json or 1200."
        ),
    )
    parser.add_argument(
        "--thumbnail-jpeg-compression",
        type=float,
        default=None,
        help=(
            "Thumbnail JPEG quality factor (0.50-1.00). 0.75 means 75%% quality. "
            "Default: read settings.json or 0.75."
        ),
    )
    parser.add_argument(
        "--wildlife",
        dest="wildlife_enabled",
        action="store_true",
        help="Enable non-bird wildlife detection (SpeciesNet, experimental).",
    )
    parser.add_argument(
        "--no-wildlife",
        dest="wildlife_enabled",
        action="store_false",
        help="Disable non-bird wildlife detection (default).",
    )
    parser.add_argument(
        "--species-detection",
        dest="species_detection_enabled",
        action="store_true",
        help="Run bird species classifier on each detection (default).",
    )
    parser.add_argument(
        "--no-species-detection",
        dest="species_detection_enabled",
        action="store_false",
        help=(
            "Skip bird species classification. Detection, quality scoring, and "
            "culling are unaffected."
        ),
    )
    parser.add_argument(
        "--retry-errored",
        dest="retry_errored",
        action="store_true",
        help="Drop previously-errored rows and re-run analysis on them.",
    )
    parser.add_argument(
        "--no-retry-errored",
        dest="retry_errored",
        action="store_false",
        help="Leave previously-errored rows alone (default).",
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
    parser.set_defaults(
        use_gpu=True,
        wildlife_enabled=False,
        species_detection_enabled=True,
        retry_errored=False,
    )
    return parser.parse_args(argv)


def _resolve_detector_name(args) -> str:
    """--detector-name takes priority over --wildlife-model-mode. If neither
    is set, fall back to DEFAULT_DETECTOR_NAME (mdv5a / "accurate")."""
    if args.detector_name:
        return args.detector_name
    if args.wildlife_model_mode:
        return WILDLIFE_MODEL_MODE_TO_DETECTOR[args.wildlife_model_mode]
    return DEFAULT_DETECTOR_NAME


def _find_first_image(folder: str) -> str | None:
    entries = [
        name
        for name in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, name))
    ]
    files = select_camera_images(entries)
    files.sort()
    if not files:
        return None
    return os.path.join(folder, files[0])


def main(argv: Sequence[str] | None = None):
    # Dump a Python traceback on SIGSEGV / SIGABRT / native crashes from ONNX
    # Runtime, OpenCV, etc. The full-app entrypoint (visualizer.py:main) enables
    # this too, but the --cli branch hands off before that — without this, a
    # native abort inside the pipeline leaves zero diagnostic output in CI.
    try:
        import faulthandler
        faulthandler.enable()
    except Exception:
        pass

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
        max_bird_crops = max(1, min(20, int(float(args.max_bird_crops))))
        scene_time_threshold = max(0.0, min(60.0, float(args.scene_time_threshold)))
        thumbnail_max_width = None
        if args.thumbnail_max_width is not None:
            thumbnail_max_width = max(400, min(2400, int(float(args.thumbnail_max_width))))
        thumbnail_jpeg_compression = None
        if args.thumbnail_jpeg_compression is not None:
            thumbnail_jpeg_compression = max(0.5, min(1.0, float(args.thumbnail_jpeg_compression)))
        detector_name = _resolve_detector_name(args)
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
        cancel_event = threading.Event()
        if args.stop_file:
            def _watch_stop():
                while not cancel_event.is_set():
                    if os.path.exists(args.stop_file):
                        cancel_event.set()
                        break
                    time.sleep(1.0)
            threading.Thread(target=_watch_stop, daemon=True).start()

        pipeline = AnalysisPipeline(
            use_gpu=args.use_gpu,
            detector_name=detector_name,
        )

        if args.json_output:
            def on_status(msg):
                print(_json.dumps({"event": "status", "msg": msg}), flush=True)
            def on_progress(processed, total):
                print(_json.dumps({"event": "progress", "count": processed, "total": total}), flush=True)
            def on_pointer_stall(filename):
                print(_json.dumps({"event": "pointer_stall", "filename": filename}), flush=True)
            def on_error(raw_file, exc):
                # Emit a machine-readable per-image error event for the parent
                # process. The existing status string emit (driven by the
                # pipeline's on_status callback) still happens separately; this
                # is additive.
                try:
                    print(_json.dumps({
                        "event": "image_error",
                        "filename": Path(raw_file).name,
                        "exception_type": type(exc).__name__,
                        "exception_message": str(exc)[:500],
                    }), flush=True)
                except Exception:
                    # Never let the error emitter itself break the pipeline.
                    pass
        else:
            def on_status(msg):
                print(msg)
            def on_progress(processed, total):
                print(f"\rProcessed {processed}/{total}", end="", flush=True)
            def on_pointer_stall(filename):
                print(f"[pointer-stall] {filename}", flush=True)
            def on_error(raw_file, exc):
                print(f"[image-error] {Path(raw_file).name}: "
                      f"{type(exc).__name__}: {str(exc)[:500]}", flush=True)

        log_event(
            log_path,
            {
                "level": "info",
                "event": "cli_start",
                "folder": args.folder,
                "use_gpu": args.use_gpu,
                "detector_name": detector_name,
                "detection_threshold": detection_threshold,
                "parallel_prefetch": parallel_pf,
                "max_bird_crops": max_bird_crops,
                "scene_time_threshold": scene_time_threshold,
                "exposure_quality": args.exposure_quality,
                "thumbnail_max_width": thumbnail_max_width,
                "thumbnail_jpeg_compression": thumbnail_jpeg_compression,
                "wildlife_enabled": args.wildlife_enabled,
                "species_detection_enabled": args.species_detection_enabled,
                "retry_errored": args.retry_errored,
            },
        )

        pipeline.process_folder(
            args.folder,
            cancel_event=cancel_event,
            callbacks={
                "on_status": on_status,
                "on_progress": on_progress,
                "on_pointer_stall": on_pointer_stall,
                "on_error": on_error,
            },
            analyzer_name="cli",
            wildlife_enabled=args.wildlife_enabled,
            species_detection_enabled=args.species_detection_enabled,
            detection_threshold=detection_threshold,
            scene_time_threshold=scene_time_threshold,
            max_bird_crops=max_bird_crops,
            parallel_prefetch=parallel_pf,
            retry_errored=args.retry_errored,
            exposure_quality=args.exposure_quality,
            thumbnail_max_width=thumbnail_max_width,
            thumbnail_jpeg_compression=thumbnail_jpeg_compression,
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
