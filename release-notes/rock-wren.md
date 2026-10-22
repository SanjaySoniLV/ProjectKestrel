# v(Rock Wren) — 2026-06-28

This release provides new tools to select photos based on exposure characteristics. Kestrel already auto-corrected exposure such that the bird subject was properly exposed. In this update, a **Exposure Compensation Strength** strength slider controls precisely how much automatic exposure correction Kestrel applies to the full-image previews and RAW previews. Alongside this feature are a highlight clipping ("blow-out") mask overlay and a live "% clipped" readout, helping you avoid selecting images with irrecoverably bright RAW highlights. This release also includes multiple bug-fixes and stability improvements.

## Major Changes

- **Exposure-compensation preview overhaul** — A complete rework of how the calculated exposure boost is shown in previews.
  - One unified **Preview Exposure Compensation** strength slider (0–100%, default 70%), replacing the old "Disable RAW Exposure Correction" and "Exposure-corrected thumbnails" checkboxes. 0% shows the uncorrected export; 100% applies the full auto correction so the subject roughly matches the bird-crop thumbnail.
  - See this new slider in the scene viewer and in the settings panel.
  - New **highlight clipping / blow-out mask**: click the readout to overlay clipped (blown-out) highlights in orange on the preview, working in both the static export preview and the live RAW zoom.
  - New **"% clipped" readout** computed from the *uncorrected* source image; it turns orange once ≥1% of the frame is clipping and doubles as the clip-mask toggle.
  - New live **"Exp Comp (+#.##EV)"** label showing the actual EV applied to the current image's preview.
  - Corrected the underlying math: the preview now applies the full strength-scaled total correction (not subject-stops only) with sRGB-gamma-aware brightness, so the strength knob is actually visible and the flat preview agrees with the RAW zoom. A one-time migration lands all users on the 70% default while preserving any later manual choice. (Setting persisted as `exposure_preview_strength`; preview-only — analysis and baked crops are unaffected.)

- **More reliable sign-in** — Sign-in no longer fails intermittently on Windows.
  - Replaced the single fixed loopback port (53682) with a list of candidate ports in the IANA user/registered range, binding the first available one. This fixes `bind_failed` / WSAEACCES (WinError 10013) caused by Hyper-V / WSL2 / Docker / WinNAT re-randomizing reserved dynamic port ranges on every boot.
  - Recovers from an abandoned sign-in: if you close the OAuth browser tab and click Sign In again, Kestrel cancels the stale flow and starts fresh instead of dead-ending until a 5-minute timeout or app restart.
  - Clearer error messages, including a new "could not open a local sign-in port" hint pointing at VPN / firewall / virtualization software.

- **Perch sharing improvements** — "Share with Perch" is now disabled while a folder is still analyzing (partial, incomplete timelines can't be uploaded), and re-enabled automatically when analysis finishes. The 🪶 Perch feather marker now appears in the folder tree immediately after upload instead of only after a restart.

## Bug Fixes

- Fixed issues related to tutorial sample sets causing crashes. (Tutorial sample sets are now mirroed to a per-session temporary folder.)
- Improved performance on some MacOS systems where the segmentation model batch-decoding step repeatedly failed on each image.
- Fixed build attestation signing issue so crash and bug reports are more reliable.
- Fixed valid image reads being rejected on mapped network drives mounted under a UNC root (the root-boundary check now resolves non-existent path tails correctly).
- Benign pywebview "orphan callback" JS exceptions (when the page navigates before a Python call returns) are now filtered out of crash reports rather than logged as crashes.
- Folder-tree status emojis (Perch feather, in-progress hourglass) moved to the right edge so they no longer crowd the folder name.

---

## 3-Bullet Summary (for version.json)
1. New Exposure Compensation slider allows you to control how much automatic exposure correction is applied to full-image thumbnails.
2. New highlight-clipping mask preview and "% clipped" readoung allows you to quickly identify overexposed images, particularly useful for shooting with exposure bracketing. 
3. Several bug fixes and stability improvements across MacOS and Windows.

## In-App WHATS_NEW Items (for analyzer/js/welcome.js)
- <b>New Exposure Triage Tools.</b> A new <b>Exposure Compensation</b> Strength slider now lives in both the scene viewer and Settings, and controls how much automatic exposure compensation is applied to your photos.
- <b>See your blown highlights.</b> A new <b>"% clipped"</b> readout sits next to RAW Zoom — click it to paint a highlight clipping mask over the preview, in both the flat image and the live RAW zoom, so over-exposed areas are obvious at a glance.
- <b>Several Bug Fixes and Stability Improvements</b> across both Windows and MacOS users should reduce crash frequency and improve reliability.

### Headline for in-app banner
`New in v(Rock Wren) — A smarter exposure-compensation preview and several stability improvements!`
