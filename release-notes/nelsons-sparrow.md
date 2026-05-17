# v(Nelson's Sparrow) — 2026-05-16

Nelson's Sparrow is a stability, resilience, and developer-experience release built on top of the Gambel's Quail pipeline. It adds a new fast wildlife detector (MegaDetector v1000-cedar), resilient ONNX execution that recovers from GPU/driver failures, OS-shutdown detection that finally silences phantom crash dialogs, and the largest test-suite expansion in the project's history (350+ tests across unit, integration, compat, security, and a real-binary UI probe). Several latent performance and accuracy bugs are also fixed, including a duplicated SpeciesNet batch pass that was silently reducing Gambel's Quail speedup.

## Major Changes

- **Resilient ONNX execution** — new `ProviderCoordinator` + `ResilientOnnxSession` wrappers manage GPU/CPU state across all six ONNX models.
  - Detects DirectML / CoreML inference failures and transparently recreates sessions on CPU, preserving partial results.
  - New per-image error state surfaces in the UI as "errored" thumbnails; a new **"Re-attempt analysis on errored images"** action (also available as `--retry-errored` from the CLI) re-queues just those files without touching successful work.
  - New GPU-resilience settings in the Analyze Folders dialog let you tune fallback behavior.
- **New "Fast" wildlife detector — MegaDetector v1000-cedar** — replaces the older `mdv6-mit-yolov9` family as the Fast option, with `mdv5a` retained as Accurate after edge-case testing in Gambel's Quail confirmed it was the better default. The new model is shipped as ONNX and inherits the Resilient-session machinery automatically. It is far more accurate than the previous version.
- **Real-crash vs. OS-shutdown detection** — new `shutdown_watch` module with platform-specific listeners (Windows `WM_QUERYENDSESSION/WM_ENDSESSION` window pump + console-ctrl fallback, macOS `NSWorkspaceWillPowerOffNotification`, Linux `SIGTERM/SIGHUP`).
  - PC reboots, logoffs, and power loss no longer trigger the "did not shut down cleanly" dialog.
  - Crash reports now carry an `exit_reason` field (`clean` / `os_shutdown` / `crash` / `unknown`) so genuine crashes are no longer drowned out by shutdown noise.
- **Full CLI parity with Advanced Analysis Settings** — every knob in the in-app Analyze Folders dialog is now exposed on `analyzer/cli.py`, including `--wildlife-model-mode {fast,accurate}`, `--max-bird-crops`, `--exposure-quality`, `--scene-time-threshold`, `--thumbnail-max-width`, `--thumbnail-jpeg-compression`, and `--wildlife / --no-wildlife`, `--species-detection / --no-species-detection`, `--retry-errored / --no-retry-errored` toggles.
- **Build attestation for official builds** — new `build_attestation.py` produces a signed build bundle that the in-app updater uses for authentication headers, replacing the legacy `X-Kestrel-Key` fallback for official releases (legacy auth still works as a fallback for self-builds).
- **New quality normalization curve (v3)** and pipeline version bump to **2.0.4** — the star-rating percentile table is rebalanced: the lower tail moves up (p0 0.0141 → 0.0277) and the upper tail compresses slightly (p99 0.9999 → 0.9998). Older `.kestrel/` folders are flagged as outdated and re-rated against the new curve on next visit.
- **Massive test-suite expansion (~350 tests)** — fully reorganized under `analyzer/tests/{unit,integration,compat,ui,security}/`.
  - Real CR3/CR2/JPEG fixture sets (`set_a_fresh`, `set_b_formats`, `set_c_preanalyzed`, `set_d_jpeg_only`, `set_e_raw_jpg_mix`) plus pre-analyzed `.kestrel/` outputs.
  - Backward-compat tests for legacy `kestrel_database.csv` schemas (Willow-Ptarmigan, Lincoln's-Sparrow, Kentucky-Warbler, alpha-2026_02_04) and legacy `settings.json` shapes.
  - Integration tests for every ML model (MegaDetector, SpeciesNet, SAM-HQ, BirdSpecies, Quality, EXIF, RAW decode, full pipeline e2e).
  - Dev CI workflows now run the real `visualizer.html` against the frozen PyInstaller binary via `--api-probe --probe-target visualizer` to catch JS-bridge regressions on the actual shipped bundle.

## Minor Changes

- **Fix:** removed a duplicate SpeciesNet batch-classifier block that was running the ONNX classifier twice per image and erasing roughly half of the Gambel's Quail batch-processing speedup.
- **Fix:** culling-preview RAW decode no longer runs `raw.postprocess()` twice when exposure correction is non-zero (saves ~1–2 seconds per 45MP RAW per slider movement).
- **Fix:** build attestation now keeps trying candidate paths instead of giving up on the first malformed bundle, so installs with a corrupt primary attestation no longer silently fall back to legacy auth.
- **Fix:** clean-exit handler no longer clobbers an already-set `os_shutdown` / `crash` exit reason on the way out.
- **Perf:** the max-bird-crops cap is now applied **before** SAM-HQ runs (not after), so segmentation work is skipped for the discarded detections.
- **Perf:** SAM-HQ now runs in batched mode with updated weights; SpeciesNet classifier also batched.
- **Refactor:** the 4,388-line `visualizer.css` is split into 18 feature-scoped files under `analyzer/css/` (base, layout, toolbar, grid, folder-tree, welcome, timeline, legal-banner, tutorial, plus a `dialogs/` subfolder). PyInstaller now auto-bundles the whole folder.
- **Refactor:** leveled logging API (`DEBUG/INFO/WARN/ERROR`) gated by `KESTREL_LOG_LEVEL`; ~140 per-image trace `print`s demoted to DEBUG so crash reports surface real warnings.
- **Refactor:** consolidated RAW + JPEG extension lists and capture-time handling across modules; dead code removed across 10 files; DEVELOPMENT.md and README.md rewritten to match the current pywebview-only architecture.
- **Deps:** dropped unused PyQt6 dependency (~100 MB off the dev install, smaller PyInstaller surface). Bumped `rawpy 0.26.1 → 0.27.0` (newer CR3/CR2 demosaic), `pyinstaller 6.18 → 6.20`, `pywebview 6.1 → 6.2.1` (JS bridge stability), `requests 2.33 → 2.34.2`, `pillow 12.1.1 → 12.2.0`, and `numpy 2.1.3 → 2.4.4`. Removed `msvc-runtime` from cross-platform `requirements.txt`.
- **Feature:** species-detection UI improvements with taxonomy mapping (cleaner labels, better tag suggestions).

---

## 3-Bullet Summary (for version.json)
1. Analysis now recovers from GPU and driver crashes automatically, and you can re-run just the failed images instead of starting over.
2. A new Fast wildlife detector (MegaDetector v1000-cedar) and a fix that nearly doubles species-classification speed on batches.
3. Restarting your PC no longer triggers a false "crash" warning, and a rebalanced quality scale gives fairer star ratings across your library.

## In-App WHATS_NEW Items (for analyzer/js/welcome.js)
- New <b>Resilient GPU pipeline</b> automatically falls back to CPU when your graphics driver hiccups, and a new <b>"Re-attempt analysis on errored images"</b> button re-runs just the failures without touching successful results.
- Faster and more accurate wildlife detection with the new <b>MegaDetector v1000-cedar</b> "Fast" model, plus a fix that <b>doubles SpeciesNet batch speed</b> by removing a duplicate inference pass.
- Kestrel now <b>tells the difference between a real crash and a normal PC shutdown</b>, so reboots and logoffs no longer trigger the alarming "did not shut down cleanly" dialog.
- New <b>quality normalization curve (v3)</b> rebalances star ratings across your library, and the <b>command-line interface</b> now exposes every Advanced Analysis Setting for power users and scripted workflows.

### Headline for in-app banner
`New in v(Nelson's Sparrow) — resilient analysis, smarter shutdowns!`
