# v(Arctic Tern (FP 2)) — 2026-11-15

v(Arctic Tern (FP 2)) is a hotfix release for v(Arctic Tern (FP 1)). It fixes a GPU error on Apple Silicon Macs that stopped the detector from starting, keeps macOS analysis running on the CPU when the GPU keeps failing instead of stopping the folder, and fixes GPU errors on non-English Windows systems going unrecognised. Everything else is unchanged from FP 1, whose notes are repeated below.

## Fixed in FP 2

- **Apple Silicon Macs: GPU analysis works again.** With GPU acceleration on, the bird detector failed to start on the first photo of every run (the log showed "model_path must not be empty"), and in FP 1 this stopped the folder. Kestrel now ships ONNX Runtime 1.28.0 on Apple Silicon, which fixes the error. Object masks (SAM) run on the CPU on Macs, because on the GPU they were several times slower.
- **macOS: repeated GPU failures fall back to the CPU.** In FP 1, a GPU that failed three times in a row on macOS stopped the folder and the rest of the queue, and showed a message about Windows resetting the GPU. On macOS the analysis now carries on using the CPU, as it did before FP 1. Windows behaviour is unchanged.
- **Windows (non-English systems): GPU errors are recognised again.** On Windows installs set to a language such as Spanish or French, a GPU failure message containing accented letters could not be read, so a lost GPU was not detected and every later photo in the folder failed. Kestrel now reads these messages, moves the work to the CPU and records the real error in the log.
- **Apple Silicon Macs now require macOS 14 (Sonoma) or later**, because the updated ONNX Runtime does not support older macOS versions.
- **No Intel Mac download for this release.** The macOS download is for Apple Silicon only. Intel Macs can keep using v(Arctic Tern (FP 1)).

# Included from v(Arctic Tern (FP 1))

## Major Changes

- **Enhanced GPU Performance (Windows, experimental, opt-in).**
  - A new setup action in Settings lets Windows download extra GPU software made for your graphics hardware, then tests Kestrel's models on your computer. Only the models that give the same results and run faster are switched over.
  - Requires Windows 11 version 24H2 or later. The option is not shown on macOS. Standard GPU acceleration (DirectML) remains the default for anyone who does not run setup.
  - While it is in use, the analysis queue, Live Analysis and Settings show "Enhanced GPU Performance Active (Intel OpenVINO)", or the name of the hardware software in use. Intel OpenVINO is the only one that has been measured so far; other vendors are tried on request and are skipped automatically if their tests fail.
  - If enhanced mode fails, Kestrel falls back to standard GPU acceleration (DirectML) or the CPU, and Settings shows which steps fell back, with a "Report a problem" button. A bug report is only offered for real setup failures, not when Windows simply has no package for an older GPU.
- **Analysis recovers when the GPU is lost.**
  - Previously, a Windows GPU reset could leave remaining images with empty results while the run still looked successful. Kestrel now detects a lost GPU, moves the affected steps to the CPU, retries the same photo and keeps going.
  - If every image in a run fails, the run stops with an error instead of reporting success.
  - Object masks (SAM) are now decoded one box at a time, which avoids a GPU crash seen on some Intel hardware at a cost of roughly 0.2 seconds more on a photo with a few birds.
- **Cloud Compute estimates and prices in the Analyze dialog.**
  - The Cloud card shows an estimated time ("As fast as ~X, depending on your upload speed") once 50 or more images are queued, with an upload speed test for a measured time, and shows your remaining images when signed in.
  - When you are signed in and your queue is larger than your remaining Cloud Compute images, the card says so and shows the current plans and prices with a "See plans" link. When you are signed out, the card has a clear "Sign in to use Cloud Compute" prompt.
  - In-app Cloud Compute pricing is not shown in the Mac App Store build.
- **Upgrade folders to analysis version 3.0 in a queue.**
  - Upgrades run one at a time in the analysis queue, each with its own Cancel button. The folder header shows each folder's analysis version and a one-click "Upgrade to 3.0" button, and Folder Actions can upgrade all loaded folders at once.
  - In the Analyze dialog, folders from analysis version 2.x now default to the fast upgrade that keeps your culling decisions, with "Re-analyze from scratch" as the alternative.
- **A cleaner scene viewer, folder settings and analysis queue.**
  - Scene viewer controls are grouped by what they act on, with a new filmstrip order toggle (best quality first or capture order).
  - A new Change Settings dialog holds per-folder options: ignore wildlife, species re-scoring, capture time, regroup scenes and reset culling. Regroup Scenes now previews the current and proposed grouping side by side, with the reason for each split.
  - The sharpness-feedback sorting dialog is redesigned as a ranked list with a large preview. The analysis queue panel has fewer borders and updates rows in place, and Live Analysis shows a loading state and updates faster.
- **Faster, more flexible culling and search.**
  - New Reject Undecided in the scene viewer and the multi-select bar, plus Mark as Reviewed for several selected scenes at once. Manual accepts and rejects are kept.
  - In the scene viewer, hover or click a bird's box on the full image to choose its crop.
  - The search box now shows recognised species and groups as colored pills inside the bar, and typing a species name filters instantly even on very large libraries.
- **The original sharpness model is the default again.** Arctic Tern made the newer sharpness model the default quality scorer. This patch switches everyone back to the original model once, because it looks at each bird at a much higher resolution. Both scores are already stored, so nothing is re-analysed: each folder updates the first time you open it, and automatic star ratings and "best quality first" order can change where the two models disagree. Your manual ratings, tags and accept/reject decisions are kept. To use the newer model, choose it under Quality scorer in Settings; that choice is kept.
- **An updated tutorial and demo set.** The tutorial now matches the current buttons and menus, and its demo set has four outings, including one in India, with new smart-search examples such as "perched on a tree" and "feeding from a flower".
- **Plain-language wording.** Settings and analysis screens no longer use model and library names such as DirectML, Core ML, int8, BioCLIP 2 or SigLIP. For example, the labels now read "Species model device", "Photos to load ahead" and "estimated time", and "Analysis could not start" replaces technical queue errors.
- **New option: Fast RAW analysis.** Settings has a checkbox to analyze each RAW file's embedded JPEG instead of decoding the sensor data, which is much faster at some cost in highlight recovery. It is off by default and does not apply to Cloud Compute. Photos from cameras newer than the bundled RAW library, such as the Sony A7R VI, now open and analyze using their embedded preview.

## Minor Changes

- Folders on a NAS or mapped network drive (for example `P:\`) no longer show 0 images in the Analyze dialog or lose their progress baseline.
- The scene viewer now shows each photo's capture time under the preview and in filmstrip tooltips.
- With "Unreviewed only" (or "Reviewed only") selected, Tab and the arrow keys now step only to scenes the filter shows, and a message appears when no more unreviewed scenes remain. ArrowLeft on a scene's first image now lands on the previous scene's last image.
- Signing out now properly revokes your sign-in token. Previously the revocation request went to the wrong address and the token stayed valid.
- The crash-recovery prompt no longer reappears for an unclean shutdown you already answered.
- Bug reports sent while signed in now include the recent log (with your user name removed) and your screenshot. The feedback dialog has an optional case-reference field and a 10 MB screenshot size check.
- The detection-confidence default is now 0.15 everywhere, matching what the Analyze dialog suggests.
- Wildlife detection now defaults to on.
- ON1 Photo RAW is now found on Windows when chosen as your photo editor (2024, 2025 and 2026 editions).
- Search in large libraries is much faster: a species name filters instantly, and the search index cache now scales to libraries of tens of thousands of photos.
- The Cloud Compute beta label is removed, and the sign-in links in the Analyze dialog now open the sign-in window.
- The support prompt after a large analysis run now leads with that run's numbers (photos sorted, scenes created).
- Pillow updated to 13.0.0 to resolve security advisories in earlier versions.

---

## 3-Bullet Summary (for version.json)

1. A hotfix for Arctic Tern (FP 1): GPU analysis works again on Apple Silicon Macs (now macOS 14 or later), and GPU errors on non-English Windows systems fall back to the CPU.
2. Opt-in Enhanced GPU Performance speeds up analysis on supported Windows PCs, and analysis now recovers from a lost GPU instead of saving empty results.
3. Cloud Compute estimates and prices now appear in the Analyze dialog, with queued analysis 3.0 upgrades, bulk culling actions and a redesigned scene viewer and queue.

## In-App WHATS_NEW Items (for analyzer/js/welcome.js)

One new bullet goes at the top of the Project Kestrel card; every other bullet and the Perch and Cloud Compute cards stay as in FP 1.

- '<b>Mac GPU fix</b>: on Apple Silicon Macs, analysis with GPU acceleration no longer fails on the first photo or stops the folder. Apple Silicon Macs now need macOS 14 (Sonoma) or later.'

### Headline for in-app banner

`New in v(Arctic Tern (FP 2)) — a hotfix for Mac GPU errors`
