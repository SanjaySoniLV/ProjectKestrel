import argparse
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kestrel_analyzer.pipeline import AnalysisPipeline
from kestrel_analyzer.logging_utils import get_log_path, log_event, log_exception
from kestrel_analyzer.config import RAW_EXTENSIONS, JPEG_EXTENSIONS


def parse_args():
    parser = argparse.ArgumentParser(description="Kestrel Analyzer CLI")
    parser.add_argument("folder", help="Folder with RAW/JPEG images")
    parser.add_argument("--gpu", dest="use_gpu", action="store_true", help="Use GPU (CUDA/ROCm/DirectML) for inference")
    parser.add_argument("--no-gpu", dest="use_gpu", action="store_false", help="Force CPU for all inference")
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Load a single image via Wand and exit (skips model loading)",
    )
    parser.set_defaults(use_gpu=True)
    return parser.parse_args()


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


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s")
    log_path = get_log_path(None)
    try:
        args = parse_args()
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
        from kestrel_analyzer.gpu_utils import get_gpu_summary
        gpu_info = get_gpu_summary(args.use_gpu)
        print(f"GPU: {gpu_info}", flush=True)

        pipeline = AnalysisPipeline(use_gpu=args.use_gpu)

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
                "gpu": gpu_info,
            },
        )

        pipeline.process_folder(
            args.folder,
            callbacks={
                "on_status": on_status,
                "on_progress": on_progress,
            },
            analyzer_name="cli",
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
