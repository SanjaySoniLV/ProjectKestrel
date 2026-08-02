# v(Dusky Grouse) — 2026-08-01

Mac Users can support Project Kestrel's development while receiving automatic updates by purchasing Kestrel from the **Mac App Store**!^1 v(Dusky Grouse) comes with a more intuitive user interface, new customizability for star ratings and the Culling Assistant, metadata writing for JPEGs, and fixes several bugs for better overall stability. In addition, Cloud Compute analysis time estimates are far more accurate, and Perch users can now follow their friends!

## Major Changes

- **Project Kestrel is on the Mac App Store — as a way to support development, not a better version.**
  - The listing costs $10. That buys automatic updates through the App Store and a direct way to support a solo developer. It does **not** buy any extra functionality.
  - Project Kestrel stays free and open source (AGPLv3). The direct download and Windows builds are unchanged, still free, and feature-identical. Nothing has moved behind a paywall, and nothing will.
  - If the free download works for you, keep using it.
  - Under the hood the App Store build is sandboxed, signs in with native **Sign in with Apple** or an in-app sign-in sheet instead of your browser, and includes in-app account deletion.
  - Learn more about why the App Store listing is paid at projectkestrel.org/notes/app-store-release

- **Choose Analysis Settings in a more friendly way.**
  - *What subjects does this shoot contain?* — birds only, or birds and other wildlife (1,200+ non-bird species).
  - *Does this shoot contain North American birds?* — answering no skips the species classifier and finishes slightly faster. Detection and quality scoring are unaffected.
  - *How many subjects should Kestrel analyze in each image?* — useful if you're shooting huge flocks.
  - Fast/Accurate moved into **More options** and now defaults to **Accurate**, the right answer for nearly every shoot.

- **Set your own star thresholds, and cull by quality score.**
  - New **Custom** rating profile: a 0.00–1.00 number line with the five star bands drawn on it. Drag a star to change the score it requires; arrow keys nudge by 0.01 (Shift for 0.05).
  - Dragging one cutoff past another pushes its neighbour instead of collapsing the band, so every star level stays reachable. "Reset to Balanced" is one click, and switching to Custom seeds from your current profile.
  - The Culling Assistant's accept cutoff is now a **quality-score slider** instead of a 0–5 star picker, with tick marks showing where each star band starts. Finer control than whole stars.
  - Photos you rated by hand sit at the floor of the star band you gave them, so your judgement still beats the computed score.
  - Changing cutoffs re-rates your loaded folders immediately.

- **Embed metadata directly into JPEG originals (opt-in).** If you shoot JPEG-only and could never get your photo editor to read Kestrel's exported data, it's because Lightroom and other editors ignore `.xmp` sidecars for JPEGs. With this update, Write Photo Metadata now offers to embed ratings, labels, species, family, and quality score into each JPEG's own metadata. Only the metadata is rewritten; pixel data is untouched and RAW files are never modified. The option is unchecked every time you open the dialog, because it edits your originals.

- **Follow other birders on Perch.** Follow someone from their profile page and every perch they publish publicly lands in your **Following** feed, newest first. Profiles now show follower and following counts. Only perches you explicitly set to public ever enter anyone's feed, and following needs no approval from the person you follow.

- **Much more accurate Cloud Compute time estimates.** Cloud compute now simulates the real worker pipeline — dispatch threshold, container cold start, scale-out as the backlog grows, per-container throughput — with those numbers served by Cloud Compute itself during the speed test, so they can't drift out of sync.
  - A new chip tells you what the constraint is: **Limited by your upload speed**, **Balanced**, or **Limited by Cloud Compute speed**.
  - While a job runs, a live pill shows whether it's playing out the way you were quoted.

- **A new warning for unsupported RAW files.** When Kestrel can open a RAW file but can't decompress its sensor data — most often Nikon High Efficiency (HE / HE\*) NEFs — it falls back to the embedded JPEG preview, which gives worse results. This now raises a dismissible banner that explains what happened and how to fix it.

## Bug Fixes

- `kestrel_database.csv` is now written atomically, so a badly-timed refresh can no longer hit a half-written file. On Windows both the save and the refresh retry through the moment the file is swapped, so a save is never silently dropped.
- Rejecting a photo now moves its JPEG and sidecar companions regardless of filename case — `IMG_2265.JPG` alongside `IMG_2265.NEF` could previously be left behind. Same fix for restoring from the reject folder.
- Rejecting or restoring large batches is noticeably faster (each directory is read once, not once per file).
- Zooming into a RAW that fell back to its embedded preview is now labelled "RAW (embedded preview)" instead of showing an EV value that was never applied — and the label now survives re-zooming.
- Fixed the Folder Actions menu being painted behind later folder rows.
- Fixed an error that could stop a configured custom editor from opening.
- Fixed the culling-cache cleanup reporting a failure on macOS when Finder or Spotlight pruned an AppleDouble `._` file mid-cleanup.
- Fixed the tutorial sample sets not being found when running from source.
- Fixed the Support link occasionally pointing at the wrong destination when clicked seconds after launch.
- The unsupported-RAW banner now stacks correctly beneath the legal-consent banner.

## Minor Changes

- Update notifications are now scheduled instead of immediate. A release goes live on GitHub straight away, but the Microsoft Store and Mac App Store take days to certify, so the in-app prompt now waits until every channel can actually serve it. It also compares version numbers rather than release names, so a newer build than the published one stops asking you to update.
- Perch plan-limit messages now offer "Manage my account" and suggest getting in touch about a higher cap, instead of an "Upgrade plan" button for storage the tiers don't sell.
- The "💛 Donate" button is now "💛 Support", with multiple ways for you to support Project Kestrel's development.
- New CI workflow producing a signed and notarized **Intel (x86_64) Mac DMG**, plus a note pointing Intel Mac users to it.
- Added diagnostic logging around shutdown detection, to track down false "Kestrel didn't close properly" prompts.
- The README has been rewritten around the current product, with fresh screenshots.
- XMP writing switched from PyExifTool to pyexiv2, which is what makes in-place JPEG embedding possible with no external binary to install.

---

## 3-Bullet Summary (for version.json)

1. Project Kestrel is now on the Mac App Store — a $10 way to support development with automatic updates; the free download stays free and identical.
2. More intuitive settings, custom star rating thresholds, and improved Culling Assistant behavior!
3. Follow other birders on Perch, far more accurate Cloud Compute time estimates, and a dozen bug fixes.

## In-App WHATS_NEW Items (for analyzer/js/welcome.js)

- 'Project Kestrel is now on the <b>Mac App Store</b>. It costs $10 and is purely <b>a way to support development</b> — you get automatic updates, but <b>no extra features</b>. The free download is unchanged, still free, and still open source.'
- 'The Analyze dialog now asks <b>plain questions about your shoot</b> instead of model settings — what you photographed, whether the birds are North American, and how many subjects per image to analyze.'
- 'New <b>Custom rating profile</b>: drag each star to the quality score it should require. The Culling Assistant now cuts on the <b>quality score</b> too, with your star thresholds drawn right on the slider. Find this in <b>Settings</b>.'
- 'You can now <b>follow other birders on Perch</b> — their public perches land in your <b>Following</b> feed, newest first.'
- 'Cloud Compute <b>time estimates are much more accurate</b>, and now tell you whether your upload or the cloud is the bottleneck. Plus optional <b>XMP embedded into JPEG originals</b>, so Lightroom finally reads your Kestrel ratings if you shoot JPEGs.'
- 'Nearly a dozen <b>bug fixes and stability improvements</b> across Windows and macOS.'

### Headline for in-app banner

`New in v(Dusky Grouse) — now on the Mac App Store, plus a big round of UI and accuracy work!`
