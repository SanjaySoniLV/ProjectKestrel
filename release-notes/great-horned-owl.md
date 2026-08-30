# v(Great-Horned Owl) — 2026-05-24

This release dramatically simplifies the user interface and introduces new tools to help you tag your scenes by bird species and family. Kestrel's main GUI and Analyze Folders dialog have been completely redesigned with a focus on intuitiveness and simplification. Bird species and family tags are now supported by a searchable dropdown with comprehensive coverage of all global bird species. 

Project Kestrel is now operated and offered under Project Kestrel LLC!

## Major Changes

- **Regional bird catalog — ~11,250 species, fuzzy search, 4-letter codes, sci-name toggle**
  - Species and family comboboxes now search a bundled global catalog built from the IOC World Bird List (v15.1, CC-BY 3.0) and IBP-AOS Alpha Codes (66th AOS Supplement, 2025) — expanded from the 500 labels the ML model produces.
  - Intelligently search through all your regions' bird species and bird families while typing the name of the tag.
  - New **Species & Region** settings section with a multi-region picker (defaults to North America) and a **Show scientific names** toggle that renders the Latin binomial in italics under each species pill and shows the scientific family name on family pills.
  - New keyboard shortcuts: CTRL+R and CTRL+SHIFT+R + TAB help you label your scenes without even leaving the keyboard.

- **Redesigned Analyze Folders dialog (3-column layout)**
  - New layout: **Queue Builder** (add folders + recents chips + dialog-local tree), **Settings** (critical knobs always visible, advanced collapsed), **Queue Summary + Start** (live counts, warnings, per-folder list).
  - Dialog state is fully independent from the main sidebar tree, so picking folders to analyze no longer disturbs what you're browsing.
  - New **recents row** with a chip strip of recently-queued folders, persisted across sessions.
  - Live local-rate time estimates use a new perf-samples history (GPU vs CPU tracked separately).

- **Redesigned home page — less cluttered, segmented controls**
  - Old three-button header replaced by a single **+ Load Folders…** button, recents chips, empty-state hint, and a clean **Clear / Select All ↔ Select None** segmented strip at the bottom.
  - Sidebar footer is now a segmented strip (**Settings | Feedback | Tutorial | Donate**); the zoom widget (slider + buttons) has moved to the bottom status bar where it belongs.
  - In-progress folders are now marked with a lightweight ⏳ hourglass emoji instead of the old purple-background-and-italic-text styling.

- **Homepage simplified — search/filter/sort moved to a sticky top bar**
  - The two large "First time" / "Returning" welcome cards are gone — the welcome panel now focuses on the What's New banner and the rotating tip carousel.
  - Search, confidence, sort, and grouping/multi-subject toggles moved out of the sidebar into a new sticky **timeline filter bar** above the timeline, with an **⚙ More ▾** popover for less-used options.

- **New Folder Actions dropdown and Clear Kestrel Data confirmation**
  - Per-folder actions menu in the scene grid for re-analysis, removal, and clearing analyzed data.
  - **Clear Kestrel Data** is now gated by a typed-confirmation dialog before the `.kestrel/` folder is removed.

## Minor Changes

- **macOS USB-drive crash fix** — Sony A1 users on macOS-mounted exFAT/NTFS drives no longer see a wall of "Unsupported file format" errors. The pipeline now skips macOS AppleDouble (`._*`) companion files and all other dot-prefixed files (`.DS_Store`, etc.) at every enumeration site (folder UI, analysis, `--validate`, telemetry).
- **macOS ONNX null-cascade fix** — when a CoreML GPU promotion failed mid-analysis, every subsequent image used to crash with `AttributeError: 'NoneType' object has no attribute 'run'`. The resilient session now restores the previous session on rebuild failure, and the provider coordinator forces a clean CPU rebuild after a failed GPU promotion.
- **Windows 64-bit shutdown-watch fix** — switched `WNDPROC` callbacks to pointer-sized `c_size_t` / `c_ssize_t` types so 64-bit `LPARAM` values stop throwing `OverflowError: int too long to convert` on every affected Windows session.
- **Removed clunky "Loading Kestrel Analyzer…" overlay** that briefly appeared on analysis start.
- **Tightened scene-card layout** to reduce visual noise and increase information density.
- **API cleanup** — removed redundant logging on every settings get/save; new `get_bird_catalog_meta`, `search_birds`, and `lookup_birds` bridge methods (catalog cached on the bridge instance).
- **Tests** — 454-line `test_bird_catalog.py` (48 new unit tests covering region filtering, alpha-code lookup, fuzzy-search tier ordering, and full coverage that every ML-model label resolves to a catalog row), 227-line `test_settings.py` additions for the new `bird_regions` / `show_scientific_names` / `analyze_recents` fields, plus new `test_folder_inspector.py` and `test_config_helpers.py`.
- **Project Kestrel LLC** — the Microsoft Store `appxmanifest` `PublisherDisplayName` is updated from "Project Kestrel" to "Project Kestrel LLC" this release reflecting the new corporate entity.

## Known Issues

- **Re-queueing a pending folder ignores new analysis options** — If a folder is already in the queue (pending or running) and you change its per-folder options (e.g. "Delete existing analysis data first" or "Skip if already analyzed") and click Start again, the new options will be silently dropped. **Workaround:** remove the folder from the queue first, then re-add it with the desired settings.

---

## 3-Bullet Summary (for version.json)
1. Redesigned user interface offers a much simpler and more intuitive experience as you browse and analyze your photos. 
2. Species and family tagging now searches a bundled global catalog of ~11,250 birds with fuzzy search, 4-letter alpha codes, and a scientific-name toggle.
3. Several bug fixes for both MacOS and Windows versions

## In-App WHATS_NEW Items (for analyzer/js/welcome.js)
- New <b>regional bird catalog</b> covers ~11,250 species (up from 500). Type a name, partial, or <b>4-letter alpha code</b> (AMGO, NOCA) into the species or family box — region-filterable and fuzzy-matched. Toggle <b>Show scientific names</b> in Settings to see the Latin binomial under each pill.
- Completely <b>redesigned Analyze Folders dialog</b> with a 3-column layout — queue builder, settings, and live queue summary + analysis time estimates.
- Redesigned homepage with a focus on simplicity and intuitiveness.
- Bug Fixes for MacOS and Windows, particularly for users analyzing on external drives, and for MacOS users using GPU support.
- 📣 <b>A NOTE FROM THE DEVELOPER:</b> Hello! Project Kestrel is moving towards the launch of <b>Perch</b>, a new sharing platform that lets others view your entire birding outing on the web, and <b>Cloud Compute</b>, a Kestrel add-on for faster analysis powered by cloud GPUs. (I’m especially excited about Perch! Take a sneak peek by visiting [this link](https://perch.projectkestrel.org/).) <b>I need beta testers!</b> If you love Project Kestrel and would be willing to test and provide feedback on these new platforms, please contact me via the in-app "Feedback" form!

### Headline for in-app banner
`New in v(Great-Horned Owl) — Auto-Complete Bird Tags, cleaner UI, crash fixes!`
