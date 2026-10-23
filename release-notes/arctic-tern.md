# v(Arctic Tern) - 2026-10-23


One of the most significant updates to date, Project Kestrel v(Arctic Tern) introduces accurate **global bird species recognition**, more accurate quality scoring/sorting, and a new **Smart Search** that lets you find your bird photos by simply describing them. 
## Major Changes
* Introducing **Analysis Pipeline v3.0**.
	* **Accurate, worldwide bird species identification.** Project Kestrel now integrates BioCLIP 2, an advanced machine learning model that supports all bird species around the world. 
		* This replaces Kestrel's North American bird species classifier and SpeciesNet.
		* North American users will notice a significant improvement in species classification accuracy.
		* Global users will see their bird photos accurately tagged by species.
		* This is the most requested feature since Kestrel's beginning.
	* **Smart Search.** Kestrel now integrates SigLIP 2, an advanced machine learning model that allows you to search your photo library by describing the photo (i.e. "roseate spoonbill in flight", "pair of mourning doves nesting", "herons in a marsh", etc.)
		* One search box for everything: searching only for a species (ex. "mourning dove") filters your timeline as before, and any other description runs Smart Search automatically.
		* Smart Search results show a "% match" score, and you can right-click a result (or use "Show in timeline") to **Go to scene in timeline**.
	* **Improved Quality Scoring.** Kestrel now utilizes a new quality scoring model based on SigLIP 2 providing the first major improvement to quality scoring/sorting since Kestrel's first release.
		* Switch back to the legacy model in Settings without needing to reanalyze photos.
	* **Upgrade Existing Folders to Pipeline 3.0 locally.** If you have already analyzed a folder with pipeline v2.0 or higher, you can upgrade the analysis to pipeline v3.0 without resetting your culling decisions. This process is significantly faster than full reanalysis.
	* Pipeline 3.0 analyzes images approximately 10% slower than Pipeline 2.x.
* **Help Kestrel Improve Quality Scoring.** You can optionally send Project Kestrel a few sorted thumbnails of photos to help improve our quality ranking model even further. No data is sent without your explicit consent.
	* The sorting dialog now shows much larger crop thumbnails, with a magnifier button to inspect a single crop at full size, making it easier to judge sharpness.
- **Cloud Compute is on pipeline 3**: Cloud Compute result packs now carry pipeline-3 data (species, quality scores, embeddings) into the local folder.

## Minor Changes

- Several Bug Fixes and UI improvements
	- Cloud pack merge no longer drops local rows after a UI save; UI saves preserve images they never loaded and do not resurrect rows moved to rejects.
	- A missing folder no longer blocks other folders from loading.
	- Image decode failures report the real cause.
	- Warning in the queue panel when results repeatedly fail to save.
	- Crash diagnostics: each instance has its own log, the Windows event log is read to explain the previous session's death, no prompt for unclean sessions that ended weeks ago, clean exit recorded on macOS Cmd+Q.
	- Culling: one assistant window, Ctrl+Z/X/C no longer trigger cull keys, X is manual Undecided, companion .xmp/.XMP handling, sidecar kept in the root for symlinked images.
	- Memory: LRU caps and URL revocation for scene RAW previews, queue thumbnails and culling caches.
	- Data safety: fsync and retried atomic replace for settings, cloud job and folder-state JSON; auth.json written at 0600; missing quality percentile CSV no longer swallowed.
	- pandas FutureWarning on the first appended row removed.
- Improvements from community suggestions
	- Scene grouping: A new regroup scenes folder action allows you to regroup scenes based on certain thresholds, such as specifying a mandatory scene break after a long gap between photos, or a similarity threshold.
	- Culling assistant: New typable thresholds for culling boundaries.

---

## 3-Bullet Summary (for version.json)
1. Major update featuring pipeline v3.0, which introduces global bird species recognition, smart search, improved quality scoring, and more.
2. Kestrel can now tag your photos by bird species, regardless of where you took them! And new Smart Search finds photos from a plain-language description, ex. "Herons in a marsh".
3. Significant improvement to Kestrel's quality scoring model, several bug fixes and UI improvements.

## In-App WHATS_NEW Items (Project Kestrel card only)
- One of the most significant updates to date, v(Arctic Tern) introduces analysis v3.0, with several major improvements.
- Kestrel can now accurately identify bird species worldwide, not just in North America. Accuracy for bird species recognition is significantly improved.
- New **Smart Search**: find your photos by simply describing what you remember, ex. "herons in a marsh" or "flock of doves in flight". Works on all folders analyzed with pipeline v3.0. Results show a % match and can jump to the scene in your timeline.
- Improved sharpness scoring: Kestrel should more accurately sort your photos from sharpest to blurriest within a burst. 
- <b>Help improve sharpness scoring</b> (optional): sort a burst from sharpest to blurriest and choose whether to share the crops. The sort dialog now has larger thumbnails and a zoom button.
- <b>Regroup Scenes</b>, <b>Cloud Compute on the new pipeline</b>, and many stability and data-safety fixes.
- Important: Folders analyzed on previous versions of Kestrel can be upgraded to v3.0 without requiring full re-analysis.

### Headline for in-app banner
`New in v(Arctic Tern) - Introducing Global Species Recognition and Smart Search!`
