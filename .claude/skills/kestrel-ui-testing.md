# Kestrel UI Testing — Drive the Project Kestrel Desktop App

An MCP server + Playwright bridge that gives you full interactive control of
the real Project Kestrel UI in a headless environment. Click buttons, take
screenshots, fill inputs, run JS, monitor analysis — all through the actual
app running in headless Chromium.

## Setup

Run the setup script once per container. It installs all dependencies,
downloads Git LFS models via curl, installs Playwright + Chromium, and
optionally injects an auth token from `KESTREL_AUTH_TOKEN`:

```bash
bash perch_ui_harness/setup.sh
```

If `KESTREL_AUTH_TOKEN` is set as an env var, the script writes it to the
app's auth fallback file so the app starts already signed in (enables cloud
compute and Perch upload testing).

## Starting the Server

The MCP server communicates over JSON-RPC/stdio. Launch it as a subprocess:

```bash
xvfb-run -a python3 perch_ui_harness/mcp_server.py
```

### JSON-RPC Protocol

Send messages as newline-delimited JSON on stdin, read responses from stdout.
The server redirects its own logs to stderr, but the MCP SDK may occasionally
emit non-JSON lines. Filter by parsing each line as JSON and checking for a
matching `"id"` field.

**Initialization handshake** (required before tool calls):
```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"agent","version":"1.0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
```

**Calling a tool**:
```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"ui_start","arguments":{}}}
```

**Response**:
```json
{"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"{\"success\": true, ...}"}]}}
```

The tool result is in `result.content[0].text` as a JSON string. Parse it to
get the actual return value.

## Available Tools

### App lifecycle
| Tool | Purpose |
|------|---------|
| `ui_start(folder_path?)` | Start the app (lazy — auto-starts on first call if not running) |
| `ui_stop()` | Shut down browser and HTTP server |
| `ui_dismiss_overlays()` | Clear legal banner, tutorial, feedback consent |

### Interaction
| Tool | Purpose |
|------|---------|
| `ui_click(selector?, text?, timeout?)` | Click by CSS selector or visible text |
| `ui_type(selector, value)` | Type into an input field |
| `ui_hover(selector)` | Hover over an element |
| `ui_select(selector, value)` | Select from a `<select>` dropdown |
| `ui_checkbox(selector, checked?)` | Check/uncheck a checkbox |
| `ui_scroll(selector?, direction, amount)` | Scroll page or element |
| `ui_set_folder(folder_path)` | Set the folder returned by file picker dialogs |

### Observation
| Tool | Purpose |
|------|---------|
| `ui_screenshot(label?)` | Capture viewport as PNG, returns file path |
| `ui_get_text(selector)` | Get text content of an element |
| `ui_get_elements(selector)` | List elements with tag, id, text, classes, visibility, disabled |
| `ui_evaluate(js_code)` | Run arbitrary JavaScript, return result |
| `ui_wait_for(selector?, text?, state?, timeout?)` | Wait for element/condition |
| `ui_get_visible_dialogs()` | List all open dialogs and overlays |

### Analysis monitoring
| Tool | Purpose |
|------|---------|
| `ui_get_queue_status()` | Get running/items/processed/total from Python backend |
| `ui_wait_for_analysis(target_processed?, timeout?, poll_interval?)` | Wait for N images processed or completion |

### Logs & debugging
| Tool | Purpose |
|------|---------|
| `ui_get_browser_logs(level?, since?, last?)` | Browser console logs. Filter by level: `error`, `warning`, `log`, `info`, `pageerror`. |
| `ui_get_python_logs(pattern?, since?, last?)` | Python-side logs (pipeline, queue, ONNX, API bridge). Filter by substring. |
| `ui_get_errors(last?)` | Combined error view — JS exceptions + Python tracebacks in one call. **Use this first when debugging.** |
| `ui_clear_logs()` | Clear all log buffers |

Logs accumulate from app start in ring buffers (5000 max each). Large results
(>200 entries) are written to a file and the path is returned alongside the
most recent entries inline. Use `since=<count>` for incremental polling.

## Key UI Selectors

| Element | Selector |
|---------|----------|
| Analyze Folders button | `#analyzeQueueBtn` |
| Add Folders (in dialog) | `#analyzeDlgLoadFolders` |
| Start Analysis | `#analyzeDlgAdd` (starts disabled; enabled when folders are checked) |
| Cancel dialog | `#analyzeDlgCancel` |
| Settings | `#settingsBtn` |
| Status bar | `#status` |
| Scene grid | `#sceneGrid` |
| Legal agree | `#legalAgreeBtn` |
| Feedback decline | `#analyticsDecline` |
| Account button | `#accountBtn` |
| Save CSV | `#saveCsv` |
| Revert changes | `#revertCsv` |

## Typical Flow

1. **Start**: `ui_start()` — launches app, dismisses overlays, returns screenshot
2. **Observe**: `ui_screenshot()` — see current state
3. **Discover**: `ui_get_elements('button')` — find clickable elements
4. **Act**: `ui_click(selector='#analyzeQueueBtn')` — click something
5. **Adapt**: If click fails (overlay blocking), `ui_get_visible_dialogs()` → dismiss
6. **Monitor**: `ui_wait_for_analysis(target_processed=5)` — wait for milestone
7. **Debug**: `ui_get_errors()` — check for problems
8. **Repeat**: The app stays running. Keep interacting as needed.

## Error Handling

Every tool returns `{success: true/false, ...}`. On failure:
- `error` — what went wrong
- `hint` — suggested recovery action (timeout → take screenshot; overlay → dismiss)
- `screenshot` — viewport at time of failure (for click failures)

## Folder Dialog Intercept

Native OS file dialogs can't work in headless mode. Instead, `ui_set_folder(path)`
controls what path is returned when JS calls `choose_directory()` /
`choose_directories()`. Set it before triggering any folder picker action.

The default is `test_imgs/` (from the `PERCH_TEST_FOLDER` env var). To analyze
a different folder, call `ui_set_folder('/absolute/path')` before clicking any
"Add Folders" or "Load Folders" button.

## Auth / Signed-In Testing

Set `KESTREL_AUTH_TOKEN` env var before running `setup.sh`. The setup script
writes the JWT to `~/.local/share/project-kestrel/auth.json`. On app start,
wait 2–3 seconds — the account button should show the signed-in state.
This enables cloud compute submission, Perch uploads, and account features.

## Known Issues & Workarounds

These are real issues discovered during development. Be aware of them:

### Overlays that block clicks
The app has several overlays that appear on first launch and after certain
actions. They intercept pointer events and cause `ui_click` to fail with
`"intercepts pointer events"` errors. Always call `ui_dismiss_overlays()` after
`ui_start()` and after starting analysis. The known overlays are:
- **Legal notice banner** (top of page) — dismissed by clicking `#legalAgreeBtn`
- **Tutorial overlay** (modal walkthrough) — dismissed by clicking "Skip Tour"
- **Feedback consent dialog** (after first analysis) — dismissed by clicking `#analyticsDecline`

If a new/unknown overlay appears, use `ui_get_visible_dialogs()` to identify
it, then `ui_evaluate()` to remove it.

### The analyze dialog has its own state model
The "Analyze Folders" dialog (`#analyzeQueueDlg`) manages its own internal
JavaScript state (`analyzeDlgRootNodes`, `analyzeDlgCheckedPaths`). Manually
checking HTML checkboxes via JS does NOT update this internal state. You must
use the real UI flow: click `#analyzeDlgLoadFolders` (which triggers
`choose_directories()` → your intercepted folder → `addAnalyzeDlgRoot()` →
`inspect_folders()` → auto-check folders with work to do → enable Start button).

If the Start Analysis button (`#analyzeDlgAdd`) stays disabled after adding
folders, wait 2–3 seconds for the folder inspection to complete and re-take
a screenshot. The inspection calls `inspect_folders` which enumerates images;
this can take a few seconds for large folders.

### SSL errors in logs are harmless
The app tries to reach `api.projectkestrel.org` for version checks and
telemetry. In sandboxed/containerized environments, these fail with
`CERTIFICATE_VERIFY_FAILED`. This is expected and does not affect functionality.
The app falls back to offline mode gracefully.

### JS `checkLegalAgreement is not defined` error
This appears on every page load because `state.js` calls a function defined in
`event-wiring.js` which loads later. It's a benign race condition in the script
loading order. The legal check still works because `event-wiring.js` calls it
again once loaded.

### Analysis on CPU is slow
Without GPU acceleration (DirectML/CoreML), the ONNX models run on CPU.
Expect ~20–30 seconds per CR3 image (14 test images ≈ 5–7 minutes total).
Use `ui_wait_for_analysis(timeout=600)` with generous timeouts.

### `ui_start()` takes 10–15 seconds
First call launches the HTTP server, starts Chromium, loads the page, waits
for the pywebview bridge to initialize, and dismisses overlays. Subsequent
tool calls are fast. Don't set short timeouts on the first interaction.

## Reporting Best Practices

When your testing session is complete, provide a structured report in your
final message. This report is the primary deliverable of a UI testing run.

### Report Structure

**1. Summary** — One paragraph: what was tested, overall result (pass/fail/partial),
and the most important finding.

**2. Steps Taken** — A numbered list of every action taken, including:
- The tool called and its arguments
- What was observed (reference the screenshot)
- Any adaptation or recovery steps (e.g., dismissing an overlay)

Example:
> 1. `ui_start()` — App loaded successfully. Screenshot shows welcome screen with tutorial overlay.
> 2. `ui_dismiss_overlays()` — Legal banner and tutorial dismissed.
> 3. `ui_click(selector='#analyzeQueueBtn')` — Analyze dialog opened. Shows folder picker with "Add Folders" button.
> 4. `ui_click(selector='#analyzeDlgLoadFolders')` — Folder `test_imgs` added. Tree shows 14 images, 0 processed.

**3. Screenshots** — Embed every screenshot from the session using the Read tool
to view them, then send them to the user with SendUserFile. Include at minimum:
- App loaded state
- Each major UI transition (dialog open, analysis started, analysis complete)
- Any error states encountered
- Final state

**4. Issues Found** — For each issue:
- **Description**: What happened vs. what was expected
- **Severity**: Critical (blocks usage) / Major (significant UX problem) / Minor (cosmetic/edge case)
- **Reproduction**: Exact steps and tool calls to reproduce
- **Root Cause**: If identifiable from logs. Use `ui_get_errors()` and
  `ui_get_python_logs(pattern='error')` to gather evidence.
- **Evidence**: Reference specific log entries and screenshots

**5. Logs Summary** — Call `ui_get_errors()` at the end of every session and
include the output. Highlight anything unexpected — ignore the known SSL
and `checkLegalAgreement` errors documented above.

### Automatic Bug Triage

When you find a bug during testing:

1. **Collect evidence**: screenshot + `ui_get_errors()` + `ui_get_python_logs()` + `ui_get_browser_logs(level='error')`
2. **Identify root cause**: trace the error to a specific file and function if possible
3. **Assess confidence**: Is this definitely a bug, or could it be environment-specific?
4. **If high confidence**: Create a fix PR targeting the `perch-mcp` branch. Include:
   - Clear description of the bug and reproduction steps
   - The fix with minimal changes
   - Reference the testing session and screenshots in the PR body
5. **If uncertain**: Report the issue in the testing report but do NOT create a PR. Flag it for human review.

**Do not create fix PRs for:**
- Known issues listed in this document
- SSL/network errors in sandboxed environments
- Issues that only reproduce under headless automation (not real user conditions)

### Report Delivery

The final report should be your last chat message in the session. Use
`SendUserFile` to deliver all screenshots. Structure the message as markdown
with the sections above. Be specific — vague reports ("some things didn't work")
are not useful.
