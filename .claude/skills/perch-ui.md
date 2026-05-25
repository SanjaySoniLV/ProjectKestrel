# Perch UI — Interact with the Project Kestrel Desktop App

## What This Is

A Playwright-based bridge that lets you interact with the real Project Kestrel
UI in a headless environment. Available as both an **MCP server** (preferred —
interactive, persistent) and a standalone script.

### MCP Server (Preferred)

When configured in `.claude/settings.local.json`, the `perch-ui` MCP server
exposes tools like `ui_screenshot`, `ui_click`, `ui_type`, `ui_evaluate`, etc.
The app starts lazily on first tool call and stays running for the session.

**Available MCP tools:**
- `ui_start` / `ui_stop` — start or restart the app
- `ui_screenshot` — capture current viewport
- `ui_click(selector=..., text=...)` — click elements
- `ui_type(selector, value)` — fill input fields
- `ui_hover(selector)` — hover for tooltips/dropdowns
- `ui_select(selector, value)` — select dropdown options
- `ui_checkbox(selector, checked)` — toggle checkboxes
- `ui_get_text(selector)` — read element text
- `ui_get_elements(selector)` — list matching elements with attributes
- `ui_evaluate(js_code)` — run arbitrary JavaScript
- `ui_wait_for(selector, text, state, timeout)` — wait for conditions
- `ui_dismiss_overlays` — clear legal/tutorial/consent dialogs
- `ui_set_folder(path)` — set folder for dialog intercepts
- `ui_get_queue_status` — check analysis queue progress
- `ui_wait_for_analysis(target_processed, timeout)` — wait for analysis milestones
- `ui_get_visible_dialogs` — list open dialogs/overlays
- `ui_scroll(selector, direction, amount)` — scroll page/elements

### How It Works

Both modes work by:
1. Starting the app's HTTP server (serves the same HTML/CSS/JS)
2. Launching headless Chromium via Playwright
3. Injecting a fake `window.pywebview.api` that routes JS calls to the real
   Python `Api` class from `analyzer/api_bridge.py`
4. Intercepting native OS dialogs (folder picker) and returning pre-configured paths

## Prerequisites

These must be available in the environment:
- `xvfb-run` (Xvfb virtual framebuffer)
- `playwright` with Chromium installed (`python3 -m playwright install chromium`)
- All Project Kestrel Python dependencies (opencv, onnxruntime, numpy, etc.)
- Git LFS models must be real files (not pointers). If models are LFS pointers,
  download them:
  ```bash
  curl -L -o analyzer/models/speciesnet/mdv5a.onnx \
    https://github.com/SanjaySoniLV/ProjectKestrel/raw/main/analyzer/models/speciesnet/mdv5a.onnx
  # ... repeat for all .onnx files in analyzer/models/speciesnet/ and analyzer/models/quality.onnx
  ```

## Quick Start — Run Full Analysis

```bash
cd /home/user/ProjectKestrel
xvfb-run -a python3 perch_ui_harness/bridge.py \
    --folder /home/user/ProjectKestrel/test_imgs \
    --screenshots /tmp/kestrel_screenshots \
    --timeout 300
```

This runs the complete flow: load app → dismiss overlays → open Analyze dialog →
add folder → start analysis → poll until complete. Screenshots are saved at every step.

## Programmatic Usage (Async Python)

```python
import asyncio
from perch_ui_harness.bridge import KestrelUIBridge

async def main():
    bridge = KestrelUIBridge(
        folder_override='/path/to/images',
        screenshots_dir='/tmp/screenshots',
    )
    await bridge.start()

    # Dismiss legal banner + tutorial
    await bridge.dismiss_overlays()

    # Take screenshots
    await bridge.screenshot('my_label')

    # Click by CSS selector
    await bridge.click(selector='#analyzeQueueBtn')

    # Click by visible text
    await bridge.click(text='Start Tutorial')

    # Fill inputs
    await bridge.fill('#adlgDetectionThreshold', '0.2')

    # Run JavaScript
    result = await bridge.evaluate('document.title')

    # Change folder for next dialog intercept
    await bridge.set_folder_override('/new/path')

    # Check analysis queue status
    info = bridge.get_queue_info()
    # -> {'running': True, 'items': [{'status': 'running', 'processed': 5, 'total': 14, ...}]}

    # Access the raw Playwright page for advanced interactions
    page = bridge.page
    await page.locator('.some-element').hover()

    await bridge.stop()

asyncio.run(main())
```

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
`choose_directories()`, instead of opening a native OS dialog, the bridge
returns whatever path is set in `bridge.folder_override`. Change it with:

```python
await bridge.set_folder_override('/new/folder/path')
```

## Running Within Xvfb

The bridge uses headless Chromium, but Xvfb is required because some
Chromium operations need an X display. Always wrap with `xvfb-run -a`:

```bash
xvfb-run -a python3 -c "
import asyncio
from perch_ui_harness.bridge import KestrelUIBridge
# ... your code
"
```

## Screenshot Output

Screenshots are saved as PNG files with sequential numbering:
- `001_app_loaded.png`
- `002_ready.png`
- `003_analyze_dialog.png`
- etc.

After running, view screenshots with the Read tool to inspect the UI state.
