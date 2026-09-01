# Project Kestrel - Development & Packaging Guide

## Architecture

Kestrel is a single desktop application: a pywebview shell wrapping a vanilla
JS/HTML/CSS UI, with a Python backend that exposes an `Api` class to JS via the
pywebview bridge. There is no PyQt GUI, no separate visualizer server, no SPA
framework.

```
ProjectKestrel/
├── analyzer/                        # Single unified app
│   ├── visualizer.py                # App entry: pywebview window + local-only HTTP server
│   ├── api_bridge.py                # `Api` class — every JS↔Python call lands here
│   ├── queue_manager.py             # Sequential folder-analysis queue worker
│   ├── settings_utils.py            # Atomic settings.json I/O + schema validation
│   ├── cli.py                       # Headless CLI entry
│   ├── visualizer.html              # Main window markup (all dialogs inline)
│   ├── visualizer.js                # Main window logic (~10k lines, vanilla JS)
│   ├── visualizer.css               # Main window styles
│   ├── culling.html                 # Culling Assistant window (separate webview)
│   ├── metadata_writer.py           # XMP sidecar writer
│   ├── editor_launch.py             # Open photos in external editors
│   ├── folder_inspector.py          # Lightweight folder probing (no ML)
│   ├── shutdown_watch.py            # OS-shutdown / logoff detection
│   ├── kestrel_telemetry.py         # Cloudflare Worker API client (failsafe)
│   ├── runtime_hook.py              # PyInstaller runtime hook (Windows DLL search)
│   ├── ProjectKestrel.spec          # PyInstaller spec (Windows)
│   ├── ProjectKestrel-macos.spec    # PyInstaller spec (macOS)
│   ├── models/                      # Bundled AI model files
│   │   ├── model.onnx               # Bird species classifier
│   │   ├── labels.txt
│   │   ├── labels_scispecies.csv
│   │   ├── scispecies_dispname.csv
│   │   ├── quality.onnx             # Quality assessment model
│   │   ├── quality_normalization_data.csv
│   │   └── speciesnet/              # MegaDetector ONNX + SAM-HQ encoder/decoder + SpeciesNet weights
│   ├── kestrel_analyzer/            # Core analysis pipeline (no UI dependencies)
│   │   ├── pipeline.py              # Main orchestration (`AnalysisPipeline`)
│   │   ├── config.py                # Paths and constants
│   │   ├── database.py              # kestrel_database.csv I/O + scenedata migration
│   │   ├── image_utils.py, raw_exif.py, similarity.py, ratings.py, ...
│   │   └── ml/                      # SpeciesNet+SAM-HQ wrapper, species classifier, quality classifier
│   └── tests/                       # pytest + unittest
├── packaging/                       # MSIX manifest + installer build scripts
│   ├── ProjectKestrel.appxmanifest
│   ├── build_installer_headless.bat       # Windows: PyInstaller build
│   ├── build_installer_headless_part2.bat # Windows: Inno Setup installer
│   ├── build_app_headless.sh              # macOS PyInstaller build
│   └── kestrel_installer.iss              # Inno Setup script
├── utils/
│   └── resave_quality_model.py      # Dev-only: re-save Keras model (historical)
├── test_imgs/                       # Tiny CI smoke-test images
├── EXPCOMP_tests/                   # Exposure-compensation reference data
├── sample_sets/                     # Bundled sample bird-photo sets shown in onboarding
├── requirements.txt                 # Generic / Linux-from-source dependencies
├── requirements-windows.txt         # DirectML build (Windows)
└── requirements-macos.txt           # CoreML / Apple Silicon
```

## Development Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/SanjaySoniLV/ProjectKestrel.git
cd ProjectKestrel
pip install -r requirements-windows.txt   # or requirements-macos.txt / requirements.txt
```

If `pip` installs both `opencv-python` and `opencv-python-headless` (e.g. via
`speciesnet`), `cv2.imwrite` may reject the JPEG-quality parameter. Fix:

```bash
pip uninstall -y opencv-python-headless
pip install --force-reinstall opencv-python==4.11.0.86
```

Re-pin `numpy==2.1.3` afterward if pip upgrades it — TensorFlow expects
`numpy<2.2`.

### 2. Required model files

All under `analyzer/models/`:
- `model.onnx`, `labels.txt`, `labels_scispecies.csv`, `scispecies_dispname.csv`
- `quality.onnx`, `quality_normalization_data.csv`
- `speciesnet/` (MegaDetector ONNX variants, SAM-HQ ViT-Tiny encoder/decoder ONNX, SpeciesNet weights)

SpeciesNet weights are bundled in the repo via Git LFS. SAM-HQ ViT-Tiny ONNX
files are also bundled. No first-run downloads are needed.

### 3. Running the app

Desktop (pywebview) — the normal launch:
```bash
python analyzer/visualizer.py
```

Headless CLI:
```bash
python analyzer/cli.py "C:\path\to\photos" --no-gpu
python analyzer/cli.py "C:\path\to\photos" --gpu --parallel-prefetch 3

# Equivalent: forward through the same entry point
python analyzer/visualizer.py --cli "C:\path\to\photos" --no-gpu
```

Optional environment variables:
- `KESTREL_ALLOWED_ROOT` — jail bridge calls to this root directory.
- `KESTREL_ALLOWED_EXTENSIONS` — override the editor-launch extension allowlist
  (comma-separated list, e.g. `.cr3,.jpg`).

### 4. Output layout

For each analyzed folder, Kestrel creates `.kestrel/` containing:
- `kestrel_database.csv` — per-image analysis results
- `kestrel_scenedata.json` — scene grouping + user-edited ratings/tags
- `kestrel_metadata.json` — analysis-run audit trail (settings used, render mode)
- `export/` — resized JPEG thumbnails for the UI
- `crop/` — square bird crops for the UI
- `culling_TMP/` — RAW preview cache (deleted on app close)

## Testing

Configured in `analyzer/tests/pytest.ini`. Markers: `unit`, `integration`, `e2e`,
`compat`, `ui`.

There are two lanes, and the difference matters:

```bash
# Fast lane (~1 min). No ML weights needed, no LFS objects required.
# Use this while iterating.
pytest analyzer/tests -m unit

# FULL PRE-DEPLOY LANE (~4 min on CPU). Run this before cutting a build.
# Adds integration/e2e/compat, which load the real ONNX models -- so it needs
# the Git LFS objects under analyzer/models/ actually materialised, not
# pointer stubs.
pytest analyzer/tests -m "not ui"

pytest analyzer/tests/unit/test_database.py -v
pytest analyzer/tests/unit/test_database.py::TestName::test_case
```

**`-m unit` is not "the test suite".** It deliberately excludes every test that
touches a model, so a broken model path stays green there indefinitely. Five
quality-classifier integration tests were broken for roughly four months
without being noticed, because the fast lane was the only command documented
here and the dev build workflows mark their pytest step `continue-on-error`.
The four release workflows *do* gate on `-m "not ui"`, so anything the fast
lane misses surfaces as a failed release build rather than a failed commit.
Run the full lane before you deploy.

`conftest.py` puts `analyzer/` on `sys.path`, so tests import as
`from kestrel_analyzer.database import ...` (not `from analyzer.kestrel_analyzer...`).

Taxonomy-routing test (no ML weights needed, no pytest required):
```bash
# Windows
$env:PYTHONPATH = "analyzer"
python -m unittest analyzer.tests.test_speciesnet_taxonomy -v

# POSIX
PYTHONPATH=analyzer python -m unittest analyzer.tests.test_speciesnet_taxonomy -v
```

Pipeline smoke test on the bundled images:
```bash
python analyzer/cli.py test_imgs --no-gpu --parallel-prefetch 1
```

## Building Executables

### Windows

```cmd
packaging\build_installer_headless.bat
packaging\build_installer_headless_part2.bat
```

- `build_installer_headless.bat` runs PyInstaller against
  `analyzer/ProjectKestrel.spec` and produces `analyzer/dist/ProjectKestrel/`.
- `build_installer_headless_part2.bat` packages with Inno Setup into
  `dist/installer/*.exe`.

CI (`.github/workflows/main.yml`) also builds an MSIX/MSIXBUNDLE via `makeappx`
using `packaging/ProjectKestrel.appxmanifest`.

### macOS

```bash
packaging/build_app_headless.sh
```

Targets `analyzer/ProjectKestrel-macos.spec`.

#### Building on Intel (x86_64) Macs

The spec builds for the host architecture (`target_arch=None`), so a native
Intel build needs no code changes — just an x86_64 environment. Verified on
macOS 26 / Intel with Python 3.12:

1. Make sure Git LFS is installed and the model weights are pulled **before**
   building:

   ```bash
   brew install git-lfs
   git lfs install
   git lfs pull
   ```

   Without this, the ONNX files under `analyzer/models/` are tiny pointer
   stubs (e.g. `quality.onnx` at ~130 bytes instead of ~1.1 MB) and the
   build "succeeds" but the app fails at model load.

2. Create the venv the build script expects, using an **x86_64** Python 3.12:

   ```bash
   python3.12 -m venv .venv2
   .venv2/bin/pip install -r requirements-macos.txt
   ```

   All pinned packages ship x86_64 macOS wheels except `rawpy`, which pip
   builds from source automatically (takes a few minutes).

3. Run `packaging/build_app_headless.sh`. Output lands in
   `analyzer/dist/Project Kestrel.app`. Confirm the architecture with:

   ```bash
   lipo -archs "analyzer/dist/Project Kestrel.app/Contents/MacOS/ProjectKestrel"
   ```

To produce a DMG locally, mirror the CI packaging step
(`.github/workflows/build-macos.yml`):

```bash
brew install create-dmg
create-dmg \
  --volname "Project Kestrel" \
  --window-size 540 380 \
  --icon-size 128 \
  --icon "Project Kestrel.app" 140 190 \
  --app-drop-link 400 190 \
  --hide-extension "Project Kestrel.app" \
  "artifact/ProjectKestrel.dmg" \
  "analyzer/dist/Project Kestrel.app"
```

Note that local builds are neither Developer ID-signed nor notarized
(`codesign_identity=None` in the spec), so Gatekeeper will warn on other
machines. Signing and notarization happen in CI.

Both spec files use `analyzer/visualizer.py` as the entry point.

## Code Organization

### `analyzer/kestrel_analyzer/` — pure pipeline (no UI dependencies)

The pipeline is reusable from the CLI, the queue manager, or any future
caller. All ML model loading is lazy and triggered by
`AnalysisPipeline.load_models()`. Key modules:

- `pipeline.py` — `AnalysisPipeline.process_folder()` is the main entry. Drives
  decode → detection (SpeciesNet + SAM-HQ) → species classification → quality
  scoring → scene grouping → CSV write.
- `database.py` — `kestrel_database.csv` I/O. Includes a one-time migration that
  moves user-editable columns (`rating`, `scene_name`, etc.) out of the CSV
  and into `kestrel_scenedata.json`.
- `ml/speciesnet_sam_hq.py` — SpeciesNet (MegaDetector + classifier) + SAM-HQ
  ViT-Tiny wrapper, all on ONNX Runtime with DirectML/CoreML/CPU provider
  coordination.
- `ml/bird_species.py` — Custom ONNX bird species classifier.
- `ml/quality.py` — Custom ONNX quality model with percentile normalization.
- `ratings.py` — Quality score → 1–5 star mapping using hardcoded
  per-profile thresholds (`very_strict`, `strict`, `balanced`, `lenient`,
  `very_lenient`).

### `analyzer/` — application shell (pywebview + bridge + queue)

- `visualizer.py` — Process entry. Owns the pywebview window, the local-only
  HTTP server that serves `visualizer.html`, session lifecycle (clean-exit /
  OS-shutdown / crash detection), and the crash-report handler.
- `api_bridge.py` — `Api` class. Every JS call comes through here. Adding a
  bridge call means: (1) new method on `Api`, (2) JS call site in
  `visualizer.js`, (3) settings-schema update in `settings_utils.py` if it
  persists.
- `queue_manager.py` — Sequential folder analysis queue. Lazy-imports
  `kestrel_analyzer.pipeline` so the app starts fast for browse-only sessions.
  Snapshots settings at enqueue time — changing settings mid-run does not
  affect the running job.
- `settings_utils.py` — Atomic `settings.json` I/O with schema validation,
  monotonic counter guards, and `.bak` recovery. Protected by `_SAVE_LOCK`
  because the JS bridge, queue worker, and startup path all write concurrently.

### Settings flow

1. Backend persists `settings.json` at a platform-specific path
   (`%LOCALAPPDATA%\ProjectKestrel\` on Windows,
   `~/Library/Application Support/ProjectKestrel/` on macOS).
2. Frontend mirrors into `localStorage` under key `kestrel-webviz-settings-v1`.
3. JS reads/writes via `window.pywebview.api.get_settings()` /
   `save_settings_data()`.
4. Unknown keys are dropped with size limits on save; new persisted settings
   require an entry in `_sanitize_settings_payload()` in
   `analyzer/settings_utils.py`.

## Security Posture

- All control flows through the pywebview JS bridge. The local HTTP server
  only serves static files (GET) on 127.0.0.1.
- Bridge methods that accept paths jail every operation under a validated root
  via `_validate_root_dir` + `_is_within_root` + `_resolve_path_in_root`.
- `metadata_writer._safe_sidecar_path` rejects sidecar filenames containing
  path separators, drive letters, UNC prefixes, or traversal segments.
- `editor_launch._validate_custom_editor_path` rejects UNC, relative paths,
  control chars, and non-existent targets before `Popen`.
- `api_bridge.open_url` allowlists `http`, `https`, `mailto` schemes — no
  `file://`, `javascript:`, `data:`, custom URI handlers, or UNC paths.
- Static file serving sends a strict CSP plus `X-Content-Type-Options`,
  `X-Frame-Options: DENY`, and `Referrer-Policy: no-referrer`.

## GPU Acceleration

GPU support is opt-in and currently considered Beta. On Windows the analyzer
uses DirectML via `onnxruntime-directml`; on macOS, CoreML via
`onnxruntime-coreml`. CPU mode works everywhere. The
`provider_coordinator.py` module handles automatic GPU→CPU fallback on
session failure.

Released installers ship CPU-only. Running from source on Windows with
`requirements-windows.txt` enables DirectML; the `--gpu` flag on `cli.py`
opts in.
