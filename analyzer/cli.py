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
            print(f"Smoke test: importing wand for {os.path.basename(image_path)}", flush=True)
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

            magick_coders = os.environ.get("MAGICK_CODER_MODULE_PATH")
            if magick_coders and os.path.isdir(magick_coders):
                try:
                    coders_list = sorted(os.listdir(magick_coders))
                    print(f"Smoke test coders dir entries: {len(coders_list)}", flush=True)
                    print("Smoke test coders dir sample:", flush=True)
                    for name in coders_list[:20]:
                        print(name, flush=True)
                except Exception as exc:
                    print(f"Smoke test coders dir error: {exc}", flush=True)
            try:
                from wand.image import Image as WandImage
                from wand import version as wand_version
            except Exception as exc:
                print("Smoke test failed to import wand.", flush=True)
                traceback.print_exc()
                print(f"Smoke test wand import error: {exc}", flush=True)
                raise

            try:
                print(f"Wand version: {wand_version.VERSION}", flush=True)
                print(f"Magick version: {wand_version.MAGICK_VERSION}", flush=True)
                print(f"Magick version number: {wand_version.MAGICK_VERSION_NUMBER}", flush=True)
                print(f"Magick quantum depth: {wand_version.QUANTUM_DEPTH}", flush=True)
            except Exception as exc:
                print(f"Smoke test wand version error: {exc}", flush=True)

            magickwand_path = os.environ.get("MAGICKWAND_LIBRARY")
            if magickwand_path:
                try:
                    ctypes.CDLL(magickwand_path)
                    print(f"Smoke test CDLL ok: {magickwand_path}", flush=True)
                except Exception as exc:
                    print(f"Smoke test CDLL failed: {magickwand_path}", flush=True)
                    print(f"Smoke test CDLL error: {exc}", flush=True)

            try:
                from wand.api import library
                count = ctypes.c_size_t()
                library.MagickQueryFormats.restype = ctypes.POINTER(ctypes.c_char_p)
                library.MagickQueryFormats.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_size_t)]
                library.MagickRelinquishMemory.argtypes = [ctypes.c_void_p]
                formats_ptr = library.MagickQueryFormats(b"*", ctypes.byref(count))
                formats = [
                    formats_ptr[i].decode("utf-8", errors="ignore")
                    for i in range(count.value)
                ]
                library.MagickRelinquishMemory(formats_ptr)
                print(f"Wand supported formats: {len(formats)}", flush=True)
                print("Wand formats sample:", flush=True)
                for name in sorted(formats)[:40]:
                    print(name, flush=True)
            except Exception as exc:
                print(f"Smoke test formats error: {exc}", flush=True)

            try:
                with WandImage(filename=image_path) as img:
                    _ = img.width
            except Exception as exc:
                print("Smoke test Wand read failed.", flush=True)
                traceback.print_exc()
                print(f"Smoke test wand read error: {exc}", flush=True)
                raise

            print("Smoke test: wand read ok", flush=True)
            from kestrel_analyzer.pipeline import AnalysisPipeline
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
            print(flush=True)
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
