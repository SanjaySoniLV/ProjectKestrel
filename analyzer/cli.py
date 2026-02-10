import argparse
import os
import shutil
import sys
import tempfile
import traceback
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kestrel_analyzer.pipeline import AnalysisPipeline
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
                print("No supported image files found.")
                return
            print(f"Smoke test: importing wand for {os.path.basename(image_path)}")
            print(f"MAGICK_HOME={os.environ.get('MAGICK_HOME')}")
            print(f"MAGICK_CONFIGURE_PATH={os.environ.get('MAGICK_CONFIGURE_PATH')}")
            print(f"MAGICK_CODER_MODULE_PATH={os.environ.get('MAGICK_CODER_MODULE_PATH')}")
            print(f"Smoke test image path: {image_path}")
            print(f"Smoke test image exists: {os.path.exists(image_path)}")
            if os.path.exists(image_path):
                try:
                    print(f"Smoke test image size: {os.path.getsize(image_path)} bytes")
                except Exception as size_exc:
                    print(f"Smoke test image size error: {size_exc}")

            magick_home = os.environ.get("MAGICK_HOME")
            magick_bin = os.path.join(magick_home, "bin", "magick") if magick_home else None
            magick_cmd = magick_bin if magick_bin and os.path.exists(magick_bin) else "magick"
            print(f"Smoke test magick command: {magick_cmd}")
            try:
                result = subprocess.run(
                    [magick_cmd, "-list", "format"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                print(f"Smoke test magick -list format exit={result.returncode}")
                if result.stdout:
                    print("Smoke test magick -list format (first 40 lines):")
                    for line in result.stdout.splitlines()[:40]:
                        print(line)
                if result.stderr:
                    print("Smoke test magick -list format (stderr):")
                    print(result.stderr.strip())
            except Exception as exc:
                print(f"Smoke test magick -list format failed: {exc}")
            try:
                from wand.image import Image as WandImage
            except Exception as exc:
                print("Smoke test failed to import wand.")
                traceback.print_exc()
                print(f"Smoke test wand import error: {exc}")
                raise

            try:
                with WandImage(filename=image_path) as img:
                    _ = img.width
            except Exception as exc:
                print("Smoke test Wand read failed.")
                traceback.print_exc()
                print(f"Smoke test wand read error: {exc}")
                raise

            print("Smoke test: wand read ok")
            pipeline = AnalysisPipeline(use_gpu=args.use_gpu)

            def on_status(msg):
                print(msg)

            def on_progress(processed, total):
                print(f"\rProcessed {processed}/{total}", end="", flush=True)

            with tempfile.TemporaryDirectory(prefix="kestrel_smoke_") as temp_dir:
                shutil.copy(image_path, os.path.join(temp_dir, os.path.basename(image_path)))
                pipeline.process_folder(
                    temp_dir,
                    callbacks={
                        "on_status": on_status,
                        "on_progress": on_progress,
                    },
                    analyzer_name="cli_smoke",
                )
            print()
            print(f"Smoke test ok: {os.path.basename(image_path)}")
            return
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
