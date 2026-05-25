# Perch UI — Interact with the Project Kestrel Desktop App

## What This Is

A Playwright-based bridge that lets you interact with the real Project Kestrel
UI in a headless environment. Available as an **MCP server** (preferred —
interactive, persistent) and a standalone script.

The app starts lazily on the first tool call and stays running for the session.

## Response Format

**Every interaction tool** returns a consistent response with three fields:

```json
{
  "success": true,
  "action": "Clicked button '#analyzeQueueBtn' with text \"Analyze Folders...\"",
  "screenshot": "/tmp/kestrel_screenshots/005_click_Analyze.png"
}
```

- **`action`** — Human-readable description of exactly what happened: what
  element was interacted with, its tag, its `#id`, and its visible text content.
- **`screenshot`** — Absolute path to a PNG taken **1 second after** the
  interaction, giving the UI time to settle.

**After every interaction, read the returned screenshot** to confirm the
action had the expected effect before deciding what to do next.

### Error responses

```json
{
  "success": false,
  "error": "Timeout 10000ms exceeded",
  "type": "TimeoutError",
  "hint": "Element not found within timeout. Try: 1) Take a screenshot..."
}
```

Errors include a `hint` field with suggested recovery steps.

## Available Tools

### Starting & Stopping
- `ui_start(folder_path?)` — Start the app (auto-dismisses legal/tutorial overlays)
- `ui_stop()` — Stop the app and close browser

### Interacting (all return `action` + `screenshot`)
- `ui_click(selector?, text?, timeout?)` — Click by CSS selector or visible text
- `ui_type(selector, value)` — Type into an input field
- `ui_hover(selector)` — Hover for tooltips/dropdowns
- `ui_select(selector, value)` — Select a dropdown option
- `ui_checkbox(selector, checked?)` — Toggle a checkbox
- `ui_scroll(selector?, direction?, amount?)` — Scroll page or element
- `ui_dismiss_overlays()` — Clear legal/tutorial/consent dialogs

### Observing
- `ui_screenshot(label?)` — Capture current viewport
- `ui_wait_and_screenshot(seconds?, label?)` — Wait N seconds (1-120) then screenshot
- `ui_get_text(selector)` — Read element text content
- `ui_get_elements(selector)` — List matching elements with attributes
- `ui_evaluate(js_code)` — Run arbitrary JavaScript
- `ui_wait_for(selector?, text?, state?, timeout?)` — Wait for a condition
- `ui_get_visible_dialogs()` — List open dialogs/overlays

### Managing State
- `ui_set_folder(folder_path)` — Set folder for dialog intercepts
- `ui_get_queue_status()` — Check analysis queue progress
- `ui_wait_for_analysis(target_processed?, timeout?)` — Wait for analysis milestones

## Clicking by Text — Disambiguation

`ui_click(text="...")` searches for visible interactive elements containing
that text. There are three possible outcomes:

### 1. Single match — clicks immediately

```json
{
  "success": true,
  "action": "Clicked button '#analyzeQueueBtn' with text \"Analyze Folders...\"",
  "screenshot": "/tmp/kestrel_screenshots/005_click_Analyze.png"
}
```

### 2. Multiple matches — returns candidates (no click happens)

```json
{
  "success": false,
  "needs_disambiguation": true,
  "message": "Found 3 elements matching \"Analyze\". Call ui_click(selector=...) with one of these selectors:",
  "candidates": [
    {"index": 0, "tag": "button", "id": "analyzeQueueBtn", "text": "Analyze Folders...", "selector": "#analyzeQueueBtn", "disabled": false},
    {"index": 1, "tag": "button", "id": "analyzeDlgAdd", "text": "Analyze Selected", "selector": "#analyzeDlgAdd", "disabled": true},
    {"index": 2, "tag": "a", "id": null, "text": "Learn about Analysis", "selector": "a.help-link:nth-child(3)", "disabled": false}
  ]
}
```

**What to do:** Read the candidates, pick the correct one by examining its
text/id/disabled state, and re-call `ui_click(selector="...")` with its
`selector` field.

**Exception:** If exactly one candidate is an *exact* text match (ignoring
case), it clicks that one automatically even when there are other partial
matches.

### 3. Zero matches — error with screenshot

```json
{
  "success": false,
  "error": "No visible interactive elements found matching \"Analyze\"",
  "screenshot": "/tmp/kestrel_screenshots/006_click_no_match.png",
  "hint": "Use ui_get_elements(\"button\") or ui_screenshot to see what's on screen."
}
```

**What to do:** Read the screenshot to see the current UI state. The element
may not be visible yet, or its text may differ from what you expected.

## Cookbook — Full Analysis Flow

Follow these steps to analyze a folder of bird photos. After every step,
read the returned screenshot before proceeding.

```
1. ui_start()
   -> Confirm you see the main Kestrel UI with sidebar.

2. ui_click(text="Analyze Folders")
   -> Confirm the analyze dialog opened.
   -> If disambiguation: pick the button with id "analyzeQueueBtn".

3. ui_click(selector="#analyzeDlgLoadFolders")
   -> This triggers the folder picker (auto-intercepted).
   -> Folders should appear in the dialog tree.

4. ui_wait_and_screenshot(seconds=3)
   -> Confirm folder checkboxes are visible in the dialog.

5. Enable all checkboxes and the Start button via JS:
   ui_evaluate('(() => {
     document.querySelectorAll("#analyzeDlgTree input[type=checkbox]")
       .forEach(cb => {
         cb.checked = true;
         cb.dispatchEvent(new Event("change", {bubbles: true}));
       });
     const btn = document.getElementById("analyzeDlgAdd");
     if (btn) btn.disabled = false;
   })()')

6. ui_click(selector="#analyzeDlgAdd")
   -> Confirm analysis has started (status bar should update).

7. Wait for analysis to complete:
   ui_wait_for_analysis(timeout=300)
   -> Or poll manually: ui_wait_and_screenshot(seconds=30) in a loop,
      checking ui_get_queue_status() each time.

8. ui_screenshot(label="final_results")
   -> Capture the final state with results.

9. ui_stop()
```

**Key principle:** After every interaction, read the returned screenshot
before deciding what to do next. Don't assume the UI state — verify it.

## Prerequisites

These must be available in the environment:
- `xvfb-run` (Xvfb virtual framebuffer)
- `playwright` + Chromium (`pip install playwright && python3 -m playwright install chromium`)
- `mcp` Python library (`pip install mcp`)
- All Project Kestrel Python dependencies (opencv, onnxruntime, numpy, etc.)
- Git LFS models must be real files (not pointers)

## Key UI Element Selectors

| Element | Selector | Notes |
|---------|----------|-------|
| Analyze Folders button | `#analyzeQueueBtn` | Opens the analyze dialog |
| Load Folders (sidebar) | `.folder-load-btn` | Load folders in sidebar |
| Add Folders (dialog) | `#analyzeDlgLoadFolders` | Add folders in analyze dialog |
| Start Analysis | `#analyzeDlgAdd` | Starts disabled; enabled when folders checked |
| Cancel dialog | `#analyzeDlgCancel` | Close analyze dialog |
| Clear folders | `#analyzeDlgClear` | Remove all folders from dialog |
| Settings button | `#settingsBtn` | Open settings |
| Legal agree | `#legalAgreeBtn` | Dismiss legal banner |
| Skip tutorial | text: "Skip Tour" | Skip tutorial overlay |
| Feedback decline | `#analyticsDecline` | Dismiss feedback consent |
| Status bar | `#status` | Current status text |
| Scene grid | `#sceneGrid` | Grid of analyzed scenes |
| Save CSV | `#saveCsv` | Save changes to CSV |
| Revert | `#revertCsv` | Revert changes |

## How the Folder Dialog Intercept Works

When JS calls `window.pywebview.api.choose_directory()` or
`choose_directories()`, the bridge returns the configured folder override
instead of opening a native OS dialog. Change it with
`ui_set_folder("/new/folder/path")`.

## Screenshot Output

Screenshots are saved as PNG files with sequential numbering:
- `001_started.png`
- `002_click_Analyze_Folders.png`
- `003_typed.png`
- `004_wait_5s.png`

Read screenshots with the Read tool to inspect UI state at each step.
