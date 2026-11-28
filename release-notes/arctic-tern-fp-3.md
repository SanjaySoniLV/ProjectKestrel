# v(Arctic Tern (FP 3)) — 2026-11-28

v(Arctic Tern (FP 3)) introduces **Cull mode**, a full-screen way to accept or reject every photo in a folder scene by scene, which replaces the separate Culling Assistant window. It also brings a redesigned main window, Settings, Analyze, Folder settings and Feedback dialogs with a new icon set, **custom keyboard shortcuts** with one-handed layouts, and **favorites**.

## Major Changes

- **Cull mode replaces the Culling Assistant.** Cull mode is a full-screen mode of the main window. Open it from **Enter Cull mode** in a folder's actions; **Exit** (top right) returns to the library.
  - Every pass opens on its **starting picks**: Kestrel accepts photos at or above a Quality you choose on a histogram (hold Shift to snap to a star edge), with **Accept at least** and **Keep at most** per-scene limits and a choice for photos with no bird found. Favorites always stay accepted.
  - Kestrel's suggestions show with a dotted border and turn solid once you confirm them. Press **Enter** to confirm a scene and go to the next one, or **Tab** to skip it. **Space** flips a photo between accept and reject, and **Accept all** / **Reject all** (Shift+Z / Shift+C) decide a whole scene at once. The arrow keys stop at the ends of a scene, so reaching the last photo is your cue to confirm.
  - Scenes can be grouped **By species** or **By time**, with "No bird found" after every species group. Each photo shows a large full image (click and hold to zoom into the RAW) and a bird crop, with RAW Zoom, Exposure Adjustment and a "% clipped" highlight check.
  - **Finish** first takes you through any scenes you have not confirmed (or lets you **Confirm all as suggested**), then can move rejects to `_KESTREL_Rejects` and write photo metadata (accept/reject labels and star ratings). It then offers **another pass**, which looks again at the photos you kept with stricter starting picks.
  - The pass badge lets you view earlier passes read-only. **Culling history** lists every finished pass with **Undo move**, **Revert to this pass** and **Reset folder**. Favorites are kept in every case, and nothing is deleted.
  - A folder can now be culled more than once. Each reject move is recorded as its own pass with its own undo, and folders culled by older versions show their earlier rejects as one pass.
  - A short **tour** runs the first time a folder enters Cull mode and can be replayed from **How it works**. It shows your own keys if you have changed them.
- **Photos with several birds.** In Cull mode, the full image outlines every bird when a photo has more than one, and strip cards carry an "N birds" tag. Click a bird, or use the Up and Down arrows, to see its close-up in the bird crop ("bird 2 of 3"). Press **S** to focus the whole scene on that bird, which updates each photo's Quality and species and moves the scene to that bird's species group. Shift+Enter makes the bird the main bird for that photo. **S** now focuses the bird in the scene viewer too (it was F).
- **Favorites.** Press **F** in the scene viewer or Cull mode to favorite a photo. A pink heart shows on filmstrip cards and in the info bar, Cull mode has a **Favorites** list across the folder, and favorites are kept when a folder is analyzed again.
- **Custom keyboard shortcuts.** A new **Settings > Keyboard** section lets you reassign accept, reject, favorite, flip, star ratings and the other action keys for Cull mode and the scene viewer, so culling can be done with one hand.
  - Three layouts to start from: **Default**, **Right hand on the keyboard** (decisions on M , . and the numpad) and **Left hand on the keyboard** (browse with A D Q E, confirm with G).
  - Navigation keys, Esc and Ctrl shortcuts keep their keys and can take extra ones. A key that is already in use offers **Swap**, and you can reset one row or the whole set.
  - The scene viewer's key legend, Cull mode's key hints and tooltips all show your keys.
- **A redesigned main window.**
  - **Analyze folders…** is the one main button in the sidebar. A new **Library** header holds **Add folders…**, and recent folders show as chips with the folder's name (full path in the tooltip).
  - The filter bar has an **All / To review / Reviewed** toggle and one **View** menu for sort, grouping, bird thumbnails, Multi-subject mode and the species match threshold.
  - Scene cards show a reviewed check, the best photo's star rating and the photo count. The quality score moves to the tooltip.
  - A scene now counts as reviewed once you accept or reject a photo or set a star rating. Confirming the species is a separate step: "Mark as Reviewed" is now **Confirm species** in the scene viewer, the selection bar and the Perch panel.
  - A new set of line icons replaces emoji across the window, menus, folder tree, queue, Perch and Cloud Compute. The app's own confirm and notice dialogs replace the system pop-ups, and destructive questions put the focus on Cancel so Enter never confirms them by accident.
- **Redesigned Settings.** Settings now has sections down the left: **General**, **Keyboard**, **Species**, **Star ratings**, **Previews**, **Performance** and **Privacy**. Every option has a short plain-language hint and on/off options are switches. The rating style shows its star bands as a strip, and Fast RAW analysis, the species model device and Enhanced GPU performance are under **Performance**.
- **A three-step Analyze dialog.** **1 Folders** (the folder tree, recent folders and Add folders…), **2 Settings** (three questions about the shoot: which subjects, whether to identify species and where, and how many subjects per photo, with every other option under **All settings**) and **3 Run** (**This computer** and **Cloud Compute** side by side with their estimated times). Options you have changed from the recommended value are marked with a dot. The Start button reads "Analyze N folders" and Enter moves to the next step. What gets analyzed is unchanged.
- **Cleaner Folder settings and Feedback dialogs.** Folder settings opens with a short summary of counts, puts the path, analysis version, date and Perch link under "Folder details", and turns the red reset block into two ordinary rows. In **Send feedback** you pick one of four tiles (Something's wrong, An idea, Something I like, Something else), and logs, screenshot and "Send as my account" are switches. What is sent is unchanged.

## Minor Changes

- The first-run tutorial now asks where you usually photograph birds, so species names are right for people outside North America. The region can be changed later in Settings > Species.
- The Analyze dialog's photo total now counts failed photos correctly. It used to show only the failed photos when "Retry failed photos" was on, and subtract them twice when it was off.
- Clicking the empty folder list in the Analyze dialog opens the folder picker.
- The Analyze dialog's close button moved to the top-right corner, like every other dialog.
- On Windows, Kestrel now notices when its local port is already taken (for example by a second copy of Kestrel) and moves to a free one, instead of sharing the port.
- Two copies of Kestrel starting at the same moment no longer lose a settings save.
- Cancelling an Enhanced GPU setup re-run now keeps your working setup, instead of falling back to standard GPU until setup was run again.
- The "GPU stopped" message no longer mentions a Windows GPU reset on Macs.
- Crash reports now remove your user name from Windows paths printed with doubled backslashes and from the active-folder details.
- Welcome tips point at places that exist. The wildlife tip opens the Analyze dialog, and the editor and star-rating tips open the right Settings section.
- Upgrade and re-scoring messages say "analysis version 3.0" and "analyze again" instead of "pipeline 3" and "re-analyse".
- The scene viewer title shows the scene name and photo count, quality has one format ("Quality 0.865"), "Exp Comp" is now "Exposure Adjustment", and the shortcut legend is three short columns (Browse, Decide, Birds and tags).
- The status bar reads "Project Kestrel v7.3.0", with the release name and analysis version in its tooltip.
- Code-quality work: new lint and quality checks in CI, removal of unused code, one shared safe-file writer for settings, sidecars and cloud state, and model build files moved out of the app's models folder, so they no longer ship with the app. None of this changes how the app behaves.

---

## 3-Bullet Summary (for version.json)

1. New Cull mode: a full-screen pass through every scene to accept or reject each photo, with starting picks, multiple passes and full undo.
2. A redesigned main window, Settings, Analyze, Folder settings and Feedback dialogs with new icons, plus favorites and a close-up of each bird.
3. Custom keyboard shortcuts with one-handed layouts, a bird-region step in the tutorial, and fixes for the Analyze photo count and Windows ports.

## In-App WHATS_NEW Items (for analyzer/js/welcome.js)

- '<b>Cull mode</b> takes you through every scene full screen. Kestrel’s starting picks show dotted until you confirm them: press Enter to confirm and move on, or Tab to skip.'
- 'Cull a folder as many times as you like. <b>Finish</b> moves the rejects aside and offers a stricter next pass, and <b>Culling history</b> can undo a move or revert to any earlier pass.'
- 'Press <b>F</b> to favorite a photo. When a photo has several birds, click one to see its close-up, and press <b>S</b> to focus the whole scene on it.'
- '<b>Custom keyboard shortcuts</b>: change your keys in Settings, or pick a right-hand or left-hand layout so you can cull with one hand on the keyboard.'
- 'A <b>fresh new look</b>: a cleaner main window and new icons, plus simpler Settings, a three-step Analyze dialog and clearer Folder settings and Feedback dialogs.'

### Headline for in-app banner
`New in v(Arctic Tern (FP 3)) — Introducing Cull mode, a faster way to cull!`
