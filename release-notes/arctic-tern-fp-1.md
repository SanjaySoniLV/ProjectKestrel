# v(Arctic Tern (FP 1)) — 2026-11-08

v(Arctic Tern (FP 1)) is a feature patch for v(Arctic Tern), focused on stability, polish and speed. It adds an opt-in Enhanced GPU Performance mode for Windows, makes analysis recover from a lost GPU instead of saving empty results, and shows Cloud Compute estimates and prices inside the Analyze dialog. It also brings a cleaner scene viewer and analysis queue, queued analysis version 3.0 upgrades, bulk culling actions, an updated tutorial and demo set, plainer wording across settings and analysis screens, and a round of bug fixes.

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

1. Opt-in Enhanced GPU Performance speeds up analysis on supported Windows PCs, and analysis now recovers from a lost GPU instead of saving empty results.
2. Cloud Compute estimates and prices now appear in the Analyze dialog, with queued analysis 3.0 upgrades, bulk culling actions and a redesigned scene viewer and queue.
3. An updated tutorial and demo set, plainer wording throughout, and fixes for NAS folders, sign-out, crash-recovery prompts and signed-in bug reports.

## In-App WHATS_NEW Items (for analyzer/js/welcome.js)

Applied on the integration branch. These three go in front of the existing Arctic Tern bullets in the Project Kestrel card. Prices are left out because the same note ships in the Mac App Store build, which hides them.

- '<b>Enhanced GPU Performance</b> (Windows 11 24H2 or later, experimental): set it up in Settings to use extra GPU software made for your graphics hardware. If it fails, Kestrel falls back to standard GPU acceleration or the CPU, and a lost GPU no longer saves empty results.'
- 'Folders from analysis version 2.x now default to a fast <b>upgrade to 3.0</b> that keeps your culls, and upgrades can be queued from each folder’s header. New <b>Reject Undecided</b> speeds up bulk culling.'
- 'Polish and fixes: a cleaner scene viewer and analysis queue, capture time in the scene viewer, an updated tutorial and demo set, plainer wording in settings, and a fix for network (NAS) folders showing 0 images.'

Perch card: 'Changes to Perch are coming soon. There’s a new <b>feedback button</b> in Perch, and I’d love to hear your feedback on how we can make Perch better!'
Cloud Compute card: adds 'Cloud Compute is upgraded to <b>analysis 3.0</b>.' in front of the existing bullets.

## Field Notes changelog draft

```
---
title: "Arctic Tern (FP 1)"
date: 2026-11-08
section: changelog
draft: true
tags: [Desktop, Cloud Compute]
excerpt: "A stability and polish patch for Arctic Tern: opt-in Enhanced GPU Performance on Windows, analysis that recovers from a lost GPU, Cloud Compute prices in the Analyze dialog, and an updated tutorial and demo set."
---
<p><strong>A stability, polish and performance patch for Arctic Tern.</strong> v(Arctic Tern (FP 1)) keeps everything from Arctic Tern's analysis v3.0 and adds an opt-in Enhanced GPU Performance mode for Windows, analysis that recovers from a lost GPU, Cloud Compute estimates and prices inside the Analyze dialog, an updated tutorial and demo set, and a long list of fixes.</p>

<h4 style="margin-top: 20px; color: var(--ink);">Desktop App &mdash; Major Changes</h4>
<ul>
<li><strong>Enhanced GPU Performance (Windows, experimental, opt-in).</strong>
<ul style="margin-top: 5px; font-size: 0.9rem;">
<li>A new setup action in Settings lets Windows download extra GPU software made for your graphics hardware, then tests Kestrel's models on your computer. Only models that produce the same results and run faster are switched over.</li>
<li>Requires Windows 11 version 24H2 or later and is not shown on macOS. Standard GPU acceleration remains the default.</li>
<li>While it is active, the analysis queue, Live Analysis and Settings show "Enhanced GPU Performance Active (Intel OpenVINO)". Intel OpenVINO is the only option measured so far; other graphics vendors are tried on request and skipped automatically if their tests fail.</li>
<li>If enhanced mode fails, Kestrel falls back to standard GPU acceleration (DirectML) or the CPU.</li>
</ul>
</li>
<li style="margin-top: 10px;"><strong>Analysis recovers when the GPU is lost.</strong> Previously a Windows GPU reset could leave the remaining images with empty results while the run looked successful. Kestrel now moves the affected steps to the CPU, retries the same photo and carries on, and stops with an error if every image fails. Object masks are now decoded one box at a time, which avoids a GPU crash seen on some Intel hardware.</li>
<li style="margin-top: 10px;"><strong>Cloud Compute in the Analyze dialog.</strong> The Cloud card shows an estimated time once 50 or more images are queued, an upload speed test for a measured time, your remaining images when signed in, and, when a queue needs more images than you have left, current plan prices with a "See plans" link. In-app Cloud Compute pricing is not shown in the Mac App Store build.</li>
<li style="margin-top: 10px;"><strong>Queue upgrades to analysis version 3.0.</strong> Upgrades run one at a time with their own Cancel buttons. The folder header shows each folder's analysis version with an "Upgrade to 3.0" button, Folder Actions can upgrade all loaded folders, and the Analyze dialog defaults 2.x folders to the fast upgrade that keeps your culling decisions.</li>
<li style="margin-top: 10px;"><strong>Cleaner scene viewer, folder settings and queue.</strong> Scene viewer controls are grouped by what they act on, with a best-quality or capture-order filmstrip toggle. A new per-folder Change Settings dialog holds ignore wildlife, species re-scoring, capture time, regroup scenes and reset culling, and Regroup Scenes previews current and proposed grouping side by side. The sharpness feedback dialog now shows a ranked list with a large preview, and the analysis queue and Live Analysis are calmer and update faster.</li>
<li style="margin-top: 10px;"><strong>Faster culling and search.</strong> New <em>Reject Undecided</em> and multi-scene <em>Mark as Reviewed</em> actions, hover or click a bird on the full image to choose its crop, species and groups appear as colored pills in the search bar, and typing a species name filters instantly even on very large libraries.</li>
<li style="margin-top: 10px;"><strong>An updated tutorial and demo set,</strong> plus plainer wording across settings and analysis screens: model and library names such as DirectML, Core ML, BioCLIP 2 and SigLIP no longer appear in labels and hints.</li>
<li style="margin-top: 10px;"><strong>Fast RAW analysis (optional).</strong> A new setting analyzes each RAW's embedded JPEG instead of decoding sensor data, which is much faster at some cost in highlight recovery. It is off by default and does not apply to Cloud Compute. Photos from newer cameras that the bundled RAW library cannot open, such as the Sony A7R VI, now analyze using their embedded preview.</li>
</ul>

<h4 style="margin-top: 20px; color: var(--ink);">Desktop App &mdash; Improvements &amp; Fixes</h4>
<ul>
<li>Folders on a NAS or mapped network drive no longer show 0 images in the Analyze dialog.</li>
<li>The scene viewer now shows each photo's capture time.</li>
<li>With "Unreviewed only" selected, Tab and the arrow keys step only through scenes the filter shows.</li>
<li>Signing out now properly revokes your sign-in token.</li>
<li>The crash-recovery prompt no longer reappears for a shutdown you already answered.</li>
<li>Bug reports sent while signed in now include the recent log and your screenshot, with an optional case-reference field.</li>
<li>The detection-confidence default is now 0.15 everywhere, and wildlife detection defaults to on.</li>
<li>ON1 Photo RAW is now found on Windows when chosen as your photo editor.</li>
<li>Pillow updated to 13.0.0 to resolve security advisories.</li>
</ul>
```

## Microsoft Store "What's new" (plain text, under 1500 characters)

Arctic Tern (FP 1): a stability, polish and performance update.

NEW
- Enhanced GPU Performance (Windows 11 24H2 or later, experimental): turn it on in Settings to use extra GPU software made for your graphics hardware. If it fails, Kestrel falls back to standard GPU acceleration or the CPU.
- Analysis now recovers when the GPU is lost, instead of saving empty results.
- Cloud Compute estimates and remaining images now appear in the Analyze dialog, with prices when a queue needs more images than you have left.
- Queue upgrades to analysis version 3.0, with a one-click upgrade button on each folder.
- Reject Undecided and multi-scene Mark as Reviewed for faster culling.
- A cleaner scene viewer, folder settings dialog and analysis queue.
- An updated tutorial and demo set.
- Plainer wording across settings and analysis screens.

FIXED
- NAS and mapped-drive folders showing 0 images in the Analyze dialog.
- Capture time is now shown in the scene viewer.
- Tab now follows the "Unreviewed only" filter.
- Signing out now properly revokes your sign-in token.
- The crash-recovery prompt no longer reappears after you answer it.
- Bug reports sent while signed in now include logs and a screenshot.
- ON1 Photo RAW is now found on Windows.
