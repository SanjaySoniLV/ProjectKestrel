import argparse
import os
import shutil
import sys
import tempfile
import traceback
import ctypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kestrel_analyzer.logging_utils import get_log_path, log_event, log_exception
from kestrel_analyzer.config import RAW_EXTENSIONS, JPEG_EXTENSIONS


def parse_args():
    parser = argparse.ArgumentParser(description="Kestrel Analyzer CLI")
    parser.add_argument("folder", help="Folder with RAW/JPEG images")
    parser.add_argument("--gpu", dest="use_gpu", action="store_true", help="Use GPU (DirectML) for ONNX")
    parser.add_argument("--no-gpu", dest="use_gpu", action="store_false", help="Force CPU for ONNX")
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
            print(f"MAGICK_HOME={os.environ.get('MAGICK_HOME')}", flush=True)
            print(f"MAGICK_CONFIGURE_PATH={os.environ.get('MAGICK_CONFIGURE_PATH')}", flush=True)
            print(f"MAGICK_CODER_MODULE_PATH={os.environ.get('MAGICK_CODER_MODULE_PATH')}", flush=True)
            print(f"Smoke test image path: {image_path}", flush=True)
            print(f"Smoke test image exists: {os.path.exists(image_path)}", flush=True)
            
            if os.path.exists(image_path):
                try:
                    print(f"Smoke test image size: {os.path.getsize(image_path)} bytes", flush=True)
                except Exception as size_exc:
                    print(f"Smoke test image size error: {size_exc}", flush=True)
            
            # Test direct MagickWand library access via read_image
            try:
                from kestrel_analyzer.image_utils import read_image
                print("Attempting to read image via direct MagickWand calls...", flush=True)
                img_array = read_image(image_path)
                
                if img_array is None:
                    print("Smoke test: read_image returned None", flush=True)
                    raise RuntimeError("Failed to read image via MagickWand")
                
                print(f"Smoke test: read_image ok - shape={img_array.shape}, dtype={img_array.dtype}", flush=True)
            except Exception as exc:
                print("Smoke test: read_image failed.", flush=True)
                traceback.print_exc()
                print(f"Smoke test read_image error: {exc}", flush=True)
                raise
            
            print(f"Smoke test ok: {os.path.basename(image_path)}", flush=True)
            return
        from kestrel_analyzer.pipeline import AnalysisPipeline
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
