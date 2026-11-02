import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence

sys.path.insert(0, str(Path(__file__).parent))
from kestrel_analyzer.pipeline import AnalysisPipeline
from kestrel_analyzer.logging_utils import get_log_path, log_event, log_exception
from kestrel_analyzer.config import (
    DEFAULT_DETECTOR_NAME,
    DEFAULT_MAX_CONSECUTIVE_ERRORS,
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
        default=0.15,
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
        "--scene-break-gap",
        type=float,
        default=0.0,
        help=(
            "Start a new scene whenever more than this many seconds pass between "
            "consecutive images, even if they look alike (0-86400). "
            "0 disables the rule. Default: 0."
        ),
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
        help="Enable non-bird wildlife detection (BioCLIP routing; default).",
    )
    parser.add_argument(
        "--no-wildlife",
        dest="wildlife_enabled",
        action="store_false",
        help="Disable non-bird wildlife detection.",
    )
    parser.add_argument(
        "--species-detection",
        dest="species_detection_enabled",
        action="store_true",
        help="Run the BioCLIP bird species classifier on each detection (default).",
    )
    parser.add_argument(
        "--no-smart-search",
        dest="smart_search_enabled",
        action="store_false",
        default=None,
        help=(
            "Skip the SigLIP whole-frame embedding used by smart search (crops are "
            "always embedded: that embedding is also the default quality score). "
            "Default: the smart_search_enabled setting (on)."
        ),
    )
    parser.add_argument(
        "--raw-embedded-preview",
        dest="raw_use_embedded_preview",
        action="store_true",
        default=False,
        help=(
            "Analyse each RAW's embedded JPEG preview instead of decoding the "
            "sensor data: much faster, no highlight recovery. RAWs without a "
            "usable preview are still decoded. Off by default (the desktop "
            "app's setting does not apply to the CLI)."
        ),
    )
    parser.add_argument(
        "--quality-model",
        choices=("siglip", "legacy"),
        default=None,
        help=(
            "Which stored quality score becomes the effective `quality` column: "
            "'siglip' (SigLIP 2 sharpness, default) or 'legacy' (pre-3.0 quality CNN). "
            "Both are always computed. Default: the quality_model setting."
        ),
    )
    parser.add_argument(
        "--upgrade-v3",
        action="store_true",
        help=(
            "Upgrade an already-analysed folder to pipeline 3.0 from its stored crops and "
            "export JPEGs (no RAW decode): species re-ID, SigLIP quality, smart-search "
            "index. Never changes culling decisions, ratings or edited tags."
        ),
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
        "--species-regions",
        type=str,
        default=None,
        help=(
            "Comma-separated region codes the species classifier chooses from "
            "(e.g. 'NA' or 'PAL,AF'), or 'global' for every IOC world species. "
            "Default: the bird_regions setting in settings.json, else 'NA'."
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
    # Internal: validation child of the "enhanced GPU performance" setup (ml/enhanced_gpu.py). It runs
    # in its own process because a vendor execution provider can end its process with an access violation.
    parser.add_argument("--enhanced-gpu-validate", dest="enhanced_gpu_validate", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument("--enhanced-gpu-ep", dest="enhanced_gpu_ep", type=str, default="", help=argparse.SUPPRESS)
    parser.add_argument("--enhanced-gpu-kinds", dest="enhanced_gpu_kinds", type=str, default="",
                        help=argparse.SUPPRESS)
    parser.add_argument("--enhanced-gpu-output", dest="enhanced_gpu_output", type=str, default="",
                        help=argparse.SUPPRESS)
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
    parser.add_argument(
        "--max-gpu-restarts", dest="max_gpu_restarts", type=int, default=3,
        help="If the GPU is lost mid-run (Windows GPU reset), continue the same folder in a fresh "
             "process, up to this many times, then finish on CPU. 0 disables restarting.",
    )
    parser.add_argument(
        "--max-consecutive-errors", dest="max_consecutive_errors", type=int,
        default=DEFAULT_MAX_CONSECUTIVE_ERRORS,
        help="Stop the run (exit code 5) after this many frames in a row end as Error rows, instead of "
             "writing an Error row for every remaining photo. A successful frame resets the count. "
             "0 disables.",
    )
    parser.add_argument(
        "--fail-on-errors", dest="fail_on_errors", action="store_true",
        help="Exit with code 4 if any frame ended as an Error row (default: exit 0, as before).",
    )
    # Internal: set by the restart loop on the processes it starts.
    parser.add_argument("--gpu-child", dest="gpu_child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--gpu-restart-attempt", dest="gpu_restart_attempt", type=int, default=0,
                        help=argparse.SUPPRESS)
    parser.set_defaults(
        use_gpu=True,
        wildlife_enabled=True,
        species_detection_enabled=True,
        retry_errored=False,
    )
    return parser.parse_args(argv)


# Exit code a restart child uses to say "the GPU was lost; run me again".
EXIT_GPU_LOST = 75
# Some frames ended as Error rows and --fail-on-errors was given.
EXIT_FRAMES_ERRORED = 4
# The run stopped because --max-consecutive-errors frames in a row failed.
EXIT_ERROR_STOP = 5


def _count_error_rows(folder: str):
    """Rows of ``folder/.kestrel/kestrel_database.csv`` whose species is
    ``Error`` (None when the database is missing or unreadable)."""
    try:
        import csv
        path = os.path.join(folder, ".kestrel", "kestrel_database.csv")
        with open(path, "r", encoding="utf-8", newline="") as f:
            return sum(1 for row in csv.DictReader(f) if (row.get("species") or "") == "Error")
    except Exception:
        return None


def _child_command(argv: Sequence[str]) -> list[str]:
    """Command line that re-runs this CLI with ``argv`` in a new process."""
    if getattr(sys, "frozen", False):
        # Frozen app: the executable's own ``--cli`` switch reaches cli.main.
        return [sys.executable, "--cli", *argv]
    return [sys.executable, os.path.abspath(__file__), *argv]


def _strip_internal_flags(argv: Sequence[str]) -> list[str]:
    out: list[str] = []
    skip = False
    for a in argv:
        if skip:
            skip = False
            continue
        if a == "--gpu-child":
            continue
        if a == "--gpu-restart-attempt":
            skip = True
            continue
        if a.startswith("--gpu-restart-attempt="):
            continue
        out.append(a)
    return out


def run_gpu_restart_loop(argv: Sequence[str], first_attempt: int, max_restarts: int, log_path=None,
                         runner=None) -> int:
    """Re-run the CLI in fresh processes until it stops reporting a GPU loss.

    A lost GPU device cannot be recovered inside the process that lost it, but
    a new process gets the GPU back; ``process_folder`` resumes from the
    database. Attempts ``first_attempt`` .. ``max_restarts`` are started; the
    last one runs without stop-on-loss, so it carries on at CPU speed exactly
    as a run without this feature would. Returns the last child's exit code.
    """
    run = runner or (lambda cmd: subprocess.call(cmd))
    base = _strip_internal_flags(argv)
    attempt = first_attempt
    while True:
        print(f"\nGPU was reset; continuing in a fresh process (restart {attempt} of {max_restarts}).",
              flush=True)
        if log_path:
            log_event(log_path, {"level": "warning", "event": "gpu_restart", "attempt": attempt,
                                 "max_restarts": max_restarts})
        try:
            rc = run(_child_command([*base, "--gpu-child", "--gpu-restart-attempt", str(attempt)]))
        except KeyboardInterrupt:
            return 130
        if rc != EXIT_GPU_LOST:
            return rc
        attempt += 1
        if attempt > max_restarts:  # defensive: the last child never exits with EXIT_GPU_LOST
            return rc


def _maybe_inject_test_gpu_loss(pipeline, state: dict, processed: int, attempt: int) -> None:
    """Test-only: ``KESTREL_TEST_GPU_LOSS_AFTER=N`` simulates a device-lost
    error N images into the first attempt, to exercise the restart loop."""
    raw = os.environ.get("KESTREL_TEST_GPU_LOSS_AFTER")
    if not raw or attempt != 0 or state.get("done"):
        return
    state.setdefault("base", processed)
    try:
        if processed - state["base"] >= int(raw):
            state["done"] = True
            pipeline.sn_sam.coord.on_run_failure(
                RuntimeError("887A0005 The GPU device instance has been suspended (injected test fault)"))
    except Exception:
        pass


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


def format_stage_summary(stage_times, stage_counts, n_images: int, wall_s: float) -> list[str]:
    """Per-stage wall time for the run: total seconds, calls, and seconds per
    processed image (calls can exceed images: bioclip / siglip_crop run per crop).
    ``other`` is wall time no stage covers (decode waits, scene grouping, exports)."""
    n = max(1, int(n_images))
    lines = [f"[stage-times] {n_images} images, wall {wall_s:.1f}s ({wall_s / n:.3f} s/image)"]
    for stage, total in sorted(stage_times.items(), key=lambda kv: -kv[1]):
        lines.append(f"[stage-times]   {stage:<15} total {total:8.1f}s  calls {stage_counts.get(stage, 0):6d}"
                     f"  {total / n:.3f} s/image")
    other = max(0.0, wall_s - sum(stage_times.values()))
    lines.append(f"[stage-times]   {'other':<15} total {other:8.1f}s  {'':12}  {other / n:.3f} s/image")
    return lines


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
        if args.enhanced_gpu_validate:
            from kestrel_analyzer.ml import enhanced_validate
            from kestrel_analyzer.ml.enhanced_gpu import ROUTABLE_KINDS
            kinds = [k for k in args.enhanced_gpu_kinds.split(",") if k] or list(ROUTABLE_KINDS)
            sys.exit(enhanced_validate.main(args.enhanced_gpu_ep, kinds, args.enhanced_gpu_output))
        if args.validate:
            from kestrel_analyzer.validation import run_validation
            sys.exit(run_validation(
                images_dir=args.validate_images,
                output_path=args.validate_output,
            ))
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
        scene_break_gap_seconds = max(0.0, min(86400.0, float(args.scene_break_gap)))
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

        if args.upgrade_v3:
            from kestrel_analyzer import upgrade_v3
            kestrel_dir = os.path.join(args.folder, ".kestrel")
            elig = upgrade_v3.eligibility(kestrel_dir)
            print(f"Pipeline-3 upgrade: {elig['status']} -- {elig['reason']}", flush=True)
            if elig["status"] in ("current", "not_analyzed", "reanalyze"):
                sys.exit(0 if elig["status"] == "current" else 3)
            pipeline.load_models(max_bird_crops=max_bird_crops)
            stats = upgrade_v3.upgrade_folder(
                kestrel_dir,
                pipeline,
                progress_cb=lambda done, total, fn: print(f"\rUpgraded {done}/{total}", end="", flush=True),
                log_path=log_path,
                quality_model=args.quality_model,
            )
            print()
            print(f"Upgrade result: {stats}", flush=True)
            sys.exit(0 if stats.get("failed", 0) == 0 else 1)

        def on_status(msg):
            print(msg)

        processed_count = [0]
        test_fault_state: dict = {}
        stop_on_gpu_loss = bool(
            args.use_gpu and int(args.gpu_restart_attempt) < max(0, int(args.max_gpu_restarts))
        )

        def on_progress(processed, total):
            processed_count[0] = processed
            _maybe_inject_test_gpu_loss(pipeline, test_fault_state, processed, int(args.gpu_restart_attempt))
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
                "scene_break_gap_seconds": scene_break_gap_seconds,
                "exposure_quality": args.exposure_quality,
                "thumbnail_max_width": thumbnail_max_width,
                "thumbnail_jpeg_compression": thumbnail_jpeg_compression,
                "wildlife_enabled": args.wildlife_enabled,
                "species_detection_enabled": args.species_detection_enabled,
                "retry_errored": args.retry_errored,
            },
        )

        t_run = time.perf_counter()
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
            scene_break_gap_seconds=scene_break_gap_seconds,
            max_bird_crops=max_bird_crops,
            parallel_prefetch=parallel_pf,
            retry_errored=args.retry_errored,
            exposure_quality=args.exposure_quality,
            thumbnail_max_width=thumbnail_max_width,
            thumbnail_jpeg_compression=thumbnail_jpeg_compression,
            species_regions=(
                [t.strip() for t in args.species_regions.split(",") if t.strip()]
                if args.species_regions else None
            ),
            smart_search_enabled=args.smart_search_enabled,
            quality_model=args.quality_model,
            raw_use_embedded_preview=args.raw_use_embedded_preview,
            stop_on_gpu_loss=stop_on_gpu_loss,
            restart_on_cpu_fallback=stop_on_gpu_loss,
            max_consecutive_errors=max(0, int(args.max_consecutive_errors)),
        )
        print()
        lost = getattr(pipeline, "gpu_lost", None)
        if isinstance(lost, dict) and lost:
            print(f"GPU lost while processing {lost.get('file') or '(unknown file)'}: "
                  f"{(lost.get('reason') or '')[:200]}", flush=True)
            if args.gpu_child:
                sys.exit(EXIT_GPU_LOST)
            # Top-level process: free the models, then continue in fresh processes.
            del pipeline
            import gc
            gc.collect()
            sys.exit(run_gpu_restart_loop(
                list(argv) if argv is not None else sys.argv[1:],
                first_attempt=int(args.gpu_restart_attempt) + 1,
                max_restarts=int(args.max_gpu_restarts),
                log_path=log_path,
            ))
        for line in format_stage_summary(pipeline.stage_times, pipeline.stage_counts,
                                         processed_count[0], time.perf_counter() - t_run):
            print(line, flush=True)
        retry_cmd = f'python analyzer/cli.py "{args.folder}" --retry-errored'
        error_stop = getattr(pipeline, "error_stop", None)
        run_errors = getattr(pipeline, "error_count", 0)
        run_errors = run_errors if isinstance(run_errors, int) else 0
        folder_errors = _count_error_rows(args.folder) if run_errors or isinstance(error_stop, dict) else None
        if run_errors:
            where = f" ({folder_errors} Error row(s) in the folder in total)" if folder_errors is not None else ""
            print(f"WARNING: {run_errors} frame(s) ended as Error rows in this run{where}.", flush=True)
            print(f"To retry them: {retry_cmd}", flush=True)
        if isinstance(error_stop, dict) and error_stop:
            print(
                f"STOPPED: {error_stop.get('consecutive')} frames in a row failed "
                f"(last: {error_stop.get('last_file')}: {str(error_stop.get('last_error'))[:200]}). "
                "The remaining photos were not processed; run the same command again to continue "
                "(add --retry-errored to also retry the failed frames).",
                flush=True,
            )
            sys.exit(EXIT_ERROR_STOP)
        if run_errors and args.fail_on_errors:
            sys.exit(EXIT_FRAMES_ERRORED)
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
