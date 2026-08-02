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
    # ── Headless cloud / Perch commands (CI E2E + local smoke) ──────────────
    # Each short-circuits to cli_cloud (like --validate/--smoke); they drive the
    # real upload/cloud-compute/Perch paths against `folder` with no GUI.
    parser.add_argument(
        "--selftest-reach", action="store_true",
        help="Headless: auth + a real upload-throughput probe to the staging bucket (no GPU).",
    )
    parser.add_argument(
        "--cc-run", dest="cc_run", action="store_true",
        help="Headless: submit a cloud-compute job for `folder`, wait for completion + results.",
    )
    parser.add_argument(
        "--perch-upload", dest="perch_upload", action="store_true",
        help="Headless: upload the analyzed `folder` to Perch, print the share link.",
    )
    parser.add_argument(
        "--e2e", action="store_true",
        help="Headless E2E: (optionally --clear) cloud-compute job -> retrieve -> upload to Perch.",
    )
    parser.add_argument(
        "--clear", action="store_true",
        help="With --cc-run/--e2e: delete the folder's .kestrel analysis state first (fresh run).",
    )
    parser.add_argument(
        "--export-auth", dest="export_auth", action="store_true",
        help="Print the current auth token bundle as JSON (for minting the CI "
             "KESTREL_CI_AUTH_JSON secret). SENSITIVE: contains the refresh token. No `folder` needed.",
    )
    parser.add_argument(
        "--selftest-metadata", dest="selftest_metadata", action="store_true",
        help="Headless: verify JPEG XMP embedding works in THIS build (exercises "
             "pyexiv2 and its bundled native libraries). No folder or network needed.",
    )
    parser.add_argument(
        "--sample-count", dest="sample_count", type=int, default=10,
        help="Images to upload for --selftest-reach (default 10).",
    )
    parser.add_argument(
        "--cloud-timeout", dest="cloud_timeout", type=int, default=1800,
        help="Seconds to wait for a cloud job / Perch upload (default 1800).",
    )
    parser.add_argument(
        "--json-out", dest="json_out", type=str, default=None,
        help="Also write the headless cloud command's JSON result to this path.",
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


def _jpeg_scan_bytes(path: str) -> bytes:
    """Return the compressed scan (SOS marker -> EOF).

    Identical bytes before and after a metadata write proves exiv2 rewrote only
    the metadata segment and did not recompress the user's pixels.
    """
    data = Path(path).read_bytes()
    i = 2
    while i < len(data) - 1:
        if data[i] != 0xFF:
            break
        m = data[i + 1]
        if m == 0xD8 or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        if m == 0xDA:  # Start Of Scan
            return data[i:]
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return b""


def _run_selftest_metadata(json_out: str | None = None) -> int:
    """Verify JPEG XMP embedding actually works in THIS build.

    The point of this probe is the *frozen* binary. pyexiv2 wraps the exiv2 C++
    library, and on macOS its wheel ships a libexiv2.dylib whose load commands
    pointed at a Homebrew prefix that does not exist on a user's machine
    (packaging/vendor_macos_native_deps.sh repairs that before PyInstaller runs).
    A unit test in the dev venv cannot prove the repaired libraries survived
    into the app bundle — only running the bundled app can.

    Failure is otherwise silent: metadata_writer degrades to a per-file
    embed_errors entry, so a broken build writes no metadata and says nothing.

    Generates its own JPEG rather than relying on fixtures, so it runs anywhere
    with no folder argument and no network.

    Returns 0 on success, 1 on failure. Never raises.
    """
    import json as _json
    import tempfile

    result: dict = {"ok": False, "probe": "metadata"}
    try:
        import numpy as np
        import cv2

        try:
            from metadata_writer import write_xmp_metadata
        except ImportError:  # pragma: no cover - package-style path
            from analyzer.metadata_writer import write_xmp_metadata

        # Import pyexiv2 explicitly so an unusable native library is reported as
        # itself, rather than as a mysterious "0 embedded".
        try:
            import pyexiv2
            result["pyexiv2_version"] = getattr(pyexiv2, "__version__", "?")
            result["exiv2_version"] = getattr(pyexiv2, "__exiv2_version__", "?")
        except Exception as e:
            result["error"] = f"pyexiv2 unavailable: {e}"
            raise RuntimeError(result["error"])

        with tempfile.TemporaryDirectory(prefix="kestrel-metaprobe-") as tmp:
            name = "probe.jpg"
            path = os.path.join(tmp, name)
            img = np.zeros((64, 64, 3), dtype=np.uint8)
            img[:, :, 1] = 128  # a non-uniform frame compresses to a real scan
            img[16:48, 16:48] = (30, 200, 90)
            if not cv2.imwrite(path, img):
                raise RuntimeError("could not write the probe JPEG")

            before = _jpeg_scan_bytes(path)

            res = write_xmp_metadata(
                tmp,
                [{
                    "filename": name,
                    "rating": 4,
                    "culled": "accept",
                    "culled_origin": "manual",
                    "species": "Atlantic Puffin",
                    "family": "Alcidae",
                    "quality": 0.8734,
                }],
                embed_jpeg=True,
            )

            result["embedded"] = res.get("embedded", 0)
            result["written"] = res.get("written", 0)
            result["embed_errors"] = res.get("embed_errors", [])
            result["errors"] = res.get("errors", [])

            if result["embedded"] != 1:
                raise RuntimeError(
                    f"expected 1 embed, got {result['embedded']}; "
                    f"embed_errors={result['embed_errors']}"
                )

            # Read the XMP back out of the JPEG — the actual proof.
            image = pyexiv2.Image(path)
            try:
                xmp = image.read_xmp()
            finally:
                image.close()

            result["xmp_rating"] = xmp.get("Xmp.xmp.Rating")
            result["xmp_species"] = xmp.get("Xmp.kestrel.Species")
            result["xmp_label"] = xmp.get("Xmp.xmp.Label")

            if result["xmp_rating"] != "4":
                raise RuntimeError(f"rating did not round-trip: {result['xmp_rating']!r}")
            if result["xmp_species"] != "Atlantic Puffin":
                raise RuntimeError(f"species did not round-trip: {result['xmp_species']!r}")

            result["scan_unchanged"] = (_jpeg_scan_bytes(path) == before)
            if not result["scan_unchanged"]:
                raise RuntimeError("pixel scan changed — the JPEG was recompressed")

            result["ok"] = True
    except Exception as e:
        result.setdefault("error", str(e))

    payload = _json.dumps(result, indent=2)
    print(payload, flush=True)
    if json_out:
        try:
            with open(json_out, "w", encoding="utf-8") as f:
                f.write(payload)
        except Exception as e:
            print(f"warning: could not write {json_out}: {e}", flush=True, file=sys.stderr)

    print(
        "metadata self-test: PASS" if result["ok"]
        else f"metadata self-test: FAIL - {result.get('error')}",
        flush=True,
    )
    return 0 if result["ok"] else 1


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
        if args.selftest_metadata:
            sys.exit(_run_selftest_metadata(args.json_out))
        # Headless cloud / Perch commands — short-circuit before any analysis.
        _cloud_modes = (args.selftest_reach, args.cc_run, args.perch_upload, args.e2e, args.export_auth)
        if any(_cloud_modes):
            if sum(bool(m) for m in _cloud_modes) > 1:
                print("error: choose exactly one headless cloud command",
                      flush=True, file=sys.stderr)
                sys.exit(2)
            if not args.export_auth and not args.folder:
                print("error: 'folder' is required for headless cloud commands",
                      flush=True, file=sys.stderr)
                sys.exit(2)
            try:
                from cli_cloud import run_cloud_command
            except ImportError:  # pragma: no cover - package-style path
                from analyzer.cli_cloud import run_cloud_command
            sys.exit(run_cloud_command(args))
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
        pipeline = AnalysisPipeline(
            use_gpu=args.use_gpu,
            detector_name=detector_name,
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
            callbacks={
                "on_status": on_status,
                "on_progress": on_progress,
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
