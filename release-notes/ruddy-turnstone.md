# v(Ruddy Turnstone) — 2026-09-05

v(Ruddy Turnstone) is a major stability and bug-fix update, and also includes a few user-facing improvements. Kestrel now offers you a path to repair Kestrel analysis data if you move, rename, or delete photos from a Kestrel-analyzed folder. The scene grid got a minor upgrade, with better representation of your culling decisions and more accessible keyboard shortcuts. And this update features a very large round of data integrity, stability, crash recovery, security, and resilience bug fixes.

## Major Changes

- If you moved or deleted photos from a folder, Kestrel will notice and offer you a path to fix the data.
  - Previously, if you analyzed a folder with Kestrel, then moved some of those photos into a second folder, Kestrel would break in two ways: 
	1. Kestrel would continue to show the missing photos in the original folder
	2. You'd need to re-analyze the photos in the second folder to be able to cull them again.
  - v(Ruddy Turnstone) fixes this: Kestrel notices that files were missing (deleted, moved, or renamed), and offers you a path to reconcile the differences.
  - This feature also works for file renames, because Kestrel now records each photo's file size alongside its name, so a renamed file can be matched by its contents.
- The Scene Grid now represents your decisions. (This feature came from user feedback!)
  - Previously, Kestrel showed the 'highest quality' photo as the scene's thumbnail, even if you rejected that photo during culling.
  - Now, a scene's thumbnail is the highest-quality photo that you accepted (or did not reject). 
- Hovering over a scene's image count breaks it down into *accepted · undecided · rejected*. (This feature came from user feedback!)
- **7/8/9** now work as Accept / Undecided / Reject alongside Z/X/C, sitting directly above the 1–5 rating keys on a numpad so a whole review pass fits on one hand. (This feature came from user feedback!)
- Enormous quantity of bug fixes, security improvements, data integrity fixes, and more.

## Minor Changes
Many of these changes were contributed to Kestrel by external contributor @Wolfgangh. Thank you!
- The quality classifier no longer swallows a dead GPU session. Previously a lost DirectML/CoreML device left every remaining image unrated while the run still reported success; the failure now propagates so the pipeline can fall back to CPU.
- Fixed several bugs related to accidental overwrite risk of XMP sidecar files. (Data Integrity)
- Crash logs now provide more information on startup and shutdown reasons. This will help to identify the causes of unclean shutdown reports that contain no error information in the log.
- Fixed a bug related to incorrectly allowing the user to clear the .kestrel folder while the folder is being actively analyzed.
- **Security hardening.**
  - Fixed a bug related to Perch Upload data sources.
  - Fixed a bug related to open links for Perch URLs.
- Culling decisions you made **while analysis was running** could be erased by the next per-image save — a second photo culled during a run was wiped by the save that followed it. Fixed. 
- Manual star ratings were silently dropped when upgrading a folder from a legacy database if the rating carried no explicit origin marker. Those ratings are now kept. 
- Rejecting or restoring a photo never overwrites a file already sitting at the destination. Conflicts, missing companions and invalid names are now reported — the culling summary reads "12 rejects moved, 2 skipped" rather than claiming everything moved, and Undo is only offered when something actually did.
- A second culling pass no longer destroys the first pass's Undo point. Previous backups are rotated under timestamped names, keeping the three most recent per file.
- When a reject move falls back to copying (across filesystems), the moved file keeps its modification time and permissions instead of being stamped with the time of the move.
- Reject and Undo now resolve main files case-insensitively, matching the companion-file handling shipped in v(Dusky Grouse).
- A transient file lock on `kestrel_database.csv` — routine on Windows when the UI reads while analysis writes — no longer ends an entire analysis run.
- Queue fixes: adding a folder just as the worker went idle no longer strands it, double-clicking Start can no longer launch two workers on the same queue, the GPU setting carries between runs, and a fatal pipeline error is surfaced instead of the folder being marked done.
- Cancelling an analysis now releases the image-decoding threads promptly instead of leaving them blocked.
- Long browsing sessions no longer grow in memory without bound: the image cache is capped and releases the images it evicts, and two simultaneous requests for the same photo now share a single load.
- Filmstrip and grid thumbnails no longer appear to "stop loading" after switching between scenes quickly — queued loads for thumbnails that have left the screen are now skipped.
- Splitting an approved scene now drops the species labels that only the moved images accounted for, while leaving labels you typed in by hand alone.
- Corrected the Olympus ORF magic bytes, two of which were wrong — EXIF now reads correctly on the affected Olympus RAW files.
- Crash tracebacks work again in packaged builds: the low-level fault handler was being silently disabled because packaged Windows and macOS builds have no console attached.
- Fewer false "Kestrel didn't close properly" prompts: the session is marked clean before shutdown teardown rather than after, a still-running second instance is no longer read as a crashed first one, and a recursion in the warning logger that could abort logging is fixed.
- Scene data, the UI's CSV, cloud pack-merge results, and database backups are all written and restored atomically now, with Windows lock retries. A corrupt `kestrel_scenedata.json` is reported as an error rather than read as an empty file, which would have overwritten your ratings.
- Arrow-key navigation in the scene grid now moves the Shift+Click anchor with it, so the next range selection starts from where you actually are.
- Updated Pillow to 12.3.0 to resolve security vulnerabilities in prior versions.

---

## 3-Bullet Summary (for version.json)

1. Kestrel now spots when photos are moved, renamed, or deleted outside the app, and repairs your ratings and culling decisions instead of losing them.
2. Scene thumbnails now follow your accept/reject decisions. Scene grid navigation is more accessible with new keyboard shortcuts. 
3. Large round of data-safety and crash fixes, security improvements, and more.

## In-App WHATS_NEW Items (for analyzer/js/welcome.js)

- 'If you moved or deleted photos from a folder, Kestrel will notice and finally offer you a path to fix the data. A new <b>Repair</b> dialog offers to reconcile the differences so you don't need to re-analyze each time.'
- 'The scene grid now <b>shows your decisions</b>: each thumbnail is the best photo you accepted, hovering the image count breaks out accepted, undecided and rejected, and a new filter shows <b>only unreviewed scenes</b>. (this feature came from a user suggestion!)'
- '<b>7/8/9</b> join Z/X/C for Accept/Undecided/Reject (accessibility improvement, from a user suggestion!)'
- 'Very large round of bug fixes and stability improvements: <b>culling decisions made during analysis are no longer erased</b>, sidecars you edited in Lightroom are no longer overwritten silently, and a crash now records <b>which stage it died in</b>.'

### Headline for in-app banner

`New in v(Ruddy Turnstone) — Kestrel now repairs folders that have drifted out of sync!`
