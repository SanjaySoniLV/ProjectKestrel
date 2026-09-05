# AGENTS.md

This file orients AI coding agents (and human contributors) working in the **Project Kestrel**
desktop app repo. It is tracked and public — keep it free of anything private (no internal
infrastructure, no sibling-repo details, no secrets).

## What this project is

Project Kestrel is a free, open-source (AGPLv3), local-first wildlife-photography culling app for
Windows and macOS. It detects birds in your photos, scores image quality, groups bursts into
"scenes," and lets you browse and cull a library — all ML processing runs 100% on-device. There are
two optional paid hosted add-ons it can talk to but never depends on: **Perch** (publish curated
outings as shareable timelines) and **Cloud Compute** (offload analysis to a GPU). The app runs fully
offline once installed.

This is a single-process [pywebview](https://pywebview.flowrl.com/) desktop app: a Python backend
that owns a native window, plus a vanilla JS/HTML/CSS frontend. No SPA framework, no bundler, no
Electron, no PyQt. It ships as one PyInstaller binary.

## Repository structure

```
analyzer/
├── visualizer.py          # app entry: pywebview window + local-only HTTP server; also `--cli`
├── api_bridge.py          # Api class — the JS↔Python bridge (the trust boundary between
│                          #   untrusted frontend JS and privileged filesystem/network access)
├── queue_manager.py       # sequential folder-analysis queue
├── settings_utils.py      # atomic settings.json I/O + schema validation
├── cli.py                 # headless entry point (main.py --cli forwards here)
├── visualizer.html        # main window markup
├── js/*.js                # the frontend — vanilla-JS modules (state, event-wiring, scenes,
│                          #   culling, queue, perch, cloud-compute, auth, welcome, …)
├── css/*.css              # split stylesheets
├── oauth_client.py, auth_client.py, cloud_compute_client.py, perch_uploader.py
│                          # clients for the optional hosted add-ons
├── metadata_writer.py     # XMP sidecar writer
├── editor_launch.py       # open photos in external editors
├── kestrel_analyzer/      # pure analysis pipeline — no UI dependencies
│   ├── pipeline.py        #   AnalysisPipeline.process_folder()
│   ├── database.py        #   kestrel_database.csv I/O + scenedata migration
│   ├── validation.py      #   build validator (--validate)
│   └── ml/                #   species/bird detection, segmentation, quality-scoring models
├── ProjectKestrel*.spec   # PyInstaller build specs (Windows/macOS)
└── tests/                 # pytest suite (unit/integration/e2e/compat/ui) + security tests
packaging/                 # PyInstaller specs + Windows/macOS installer build scripts
```

The durable output of an analysis run lives alongside the user's photos in a `.kestrel/` folder
(`kestrel_database.csv`, `kestrel_scenedata.json`, `kestrel_metadata.json`, plus `crop/` and
`export/` image folders). This is a shared, versioned schema — changing it has ripple effects across
the browse UI and both optional hosted add-ons, so treat schema changes carefully.

## Setting up and running

```bash
# Install (pick the platform requirements file)
pip install -r requirements-windows.txt    # DirectML build (Windows)
pip install -r requirements-macos.txt      # CoreML / Apple Silicon
pip install -r requirements.txt            # generic / Linux-from-source

# Run the desktop app / headless CLI
python analyzer/visualizer.py
python analyzer/cli.py "/path/to/photos" --no-gpu

# Tests
pytest analyzer/tests -m unit              # fast lane, no model weights needed (~1 min)
pytest analyzer/tests -m "not ui"          # full pre-deploy lane (~4 min) — run before opening a PR
```

## Contribution guidelines

- **No secrets in the repo.** This project has no API keys or `.env` files checked in. Don't add
  any, and don't hardcode credentials or private endpoints.
- **Respect the trust boundary.** `api_bridge.py` is the only path from frontend JS into privileged
  Python code. Any bridge method that accepts a filesystem path must stay inside the existing
  root-jail validation pattern — don't add a path-accepting method that skips it.
- **The local HTTP server is static-file-only.** Never add a control/API route to it; add a method
  to the `Api` bridge class instead.
- **Run the full (non-UI) test lane before opening a PR**: `pytest analyzer/tests -m "not ui"`. The
  `-m unit` fast lane alone does not exercise the ML model paths.
- **No new GUI framework.** UI changes go in `visualizer.html` plus a module under `analyzer/js/`
  and a stylesheet under `analyzer/css/` — don't introduce React/Vue/etc.
- **Keep changes scoped.** Don't bundle unrelated refactors or formatting-only diffs into a
  functional PR.

### Pull request target branch

**Target all pull requests at `dev`, not `main`.** `main` only receives direct changes for urgent
fixes that must be deployed immediately; everything else — features, refactors, routine bug fixes —
goes through `dev` first.

### Pull request description

Every PR description should include three short paragraphs:

1. **The problem** — what's broken, missing, or awkward today, in plain terms. Assume the reader
   hasn't seen the issue before.
2. **How to reproduce** — the concrete steps (or scenario) that show the problem, so a reviewer can
   confirm it existed before the fix.
3. **What this PR does** — the actual change, and why this approach was chosen over alternatives if
   that isn't obvious from the diff.

Keep each paragraph short — a few sentences is enough. Link any relevant issue.
