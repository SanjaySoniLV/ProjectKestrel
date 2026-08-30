# FINDING-01 end-to-end fixture

This fixture lets you confirm the stored-XSS fix end-to-end inside the real
pywebview build, without DevTools and without running anything that could
actually execute code on the host.

## What it contains

`.kestrel/kestrel_scenedata.json` — four scenes. The first three are attack
payloads; the fourth is a plain-text control.

| Scene | Technique           | What it does when rendered unsafely                                      |
| ----- | ------------------- | ------------------------------------------------------------------------ |
| #1    | `<img onerror>`     | Native `alert()` pops, then paints a giant red "XSS FIRED" banner.       |
| #2    | `<svg onload>`      | Native `alert()` pops.                                                   |
| #3    | `<iframe srcdoc>`   | Native `alert()` pops from inside the iframe.                            |
| #4    | plain text          | Control — should render verbatim with `<` and `>` visible.               |

No payload calls `open_url`, writes to disk, or navigates. The worst that
happens if the vulnerability is present is a red page and three dialogs.

## Repro against a running build

The visualizer only renders scenes that correspond to rows in
`kestrel_database.csv`, so the quickest path is to graft this fixture onto
an already-analyzed folder:

1. Point Kestrel at a real photo folder and run an analysis so that
   `<folder>/.kestrel/kestrel_database.csv`,
   `<folder>/.kestrel/kestrel_metadata.json`, and
   `<folder>/.kestrel/kestrel_scenedata.json` exist.
2. Note the **scene IDs** that already exist (open the real
   `kestrel_scenedata.json` — each entry has an `id` like `"0:1"`).
3. Close the app.
4. Edit the real `kestrel_scenedata.json`, replacing each `name` field with
   the corresponding payload from **this** fixture. Keep the original `id`
   values from the real file — the scene IDs must match what's in the CSV,
   or the sceneName won't be applied. Example:

   ```jsonc
   // Your real .kestrel/kestrel_scenedata.json, edited in-place:
   {
     "version": 2,
     "scenes": [
       { "id": "0:1", "name": "<img src=x onerror=\"alert('FINDING-01 XSS FIRED — scene #1')\">" },
       { "id": "0:2", "name": "<svg onload=\"alert('FINDING-01 XSS FIRED — scene #2')\"></svg>" },
       { "id": "0:3", "name": "Normal scene name — should show angle brackets literally" }
     ]
   }
   ```

5. Re-open the folder in Kestrel.

## Pass / fail criteria

| Signal                                              | Vulnerable                   | Fixed                      |
| --------------------------------------------------- | ---------------------------- | -------------------------- |
| Native alert dialog appears on scene render         | **YES** (up to 3 dialogs)    | NO                         |
| Page background turns red / "XSS FIRED" banner      | **YES**                      | NO                         |
| Scene #4 title shows `<` and `>` verbatim as text   | NO (they're gone, tag parsed)| **YES** (visible brackets) |
| `document.title` becomes altered                    | possibly                     | NO                         |

If a dialog pops, stop — the patch isn't in place (or there's a second sink
somewhere else in the render path). The Python unit suite under
`analyzer/tests/` will point at the exact line.

## Clean-up

After testing, either:

- Restore the real scene names: Kestrel's "Reset Culling Decisions" /
  rename flow will persist a new `kestrel_scenedata.json`, OR
- Delete `<folder>/.kestrel/kestrel_scenedata.json` entirely; the app will
  regenerate an empty one next time scenes are named.

## Safety

Every payload here is inert beyond visible UI effects: no `open_url`, no
`fetch`, no file system access, no navigation. Safe to run on any
workstation. The red banner is cosmetic — reload the folder to clear it.
