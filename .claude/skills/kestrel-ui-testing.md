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
**Important**: the server emits log lines to stdout that aren't JSON-RPC.
Skip lines that don't parse as JSON or lack a matching `"id"` field.

**Initialization handshake** (required before tool calls):
```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"agent","version":"1.0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
```

**Calling a tool**:
```json
{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"ui_start","arguments":{}}}
```

**Response** (after skipping log lines):
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

## Key UI Selectors

| Element | Selector |
|---------|----------|
| Analyze Folders button | `#analyzeQueueBtn` |
| Add Folders (in dialog) | `#analyzeDlgLoadFolders` |
| Start Analysis | `#analyzeDlgAdd` |
| Cancel dialog | `#analyzeDlgCancel` |
| Settings | `#settingsBtn` |
| Status bar | `#status` |
| Scene grid | `#sceneGrid` |
| Legal agree | `#legalAgreeBtn` |
| Feedback decline | `#analyticsDecline` |
| Account button | `#accountBtn` |

## Typical Flow

1. **Start**: `ui_start()` — launches app, dismisses overlays, returns screenshot
2. **Observe**: `ui_screenshot()` — see current state
3. **Discover**: `ui_get_elements('button')` — find clickable elements
4. **Act**: `ui_click(selector='#analyzeQueueBtn')` — click something
5. **Adapt**: If click fails (overlay blocking), `ui_get_visible_dialogs()` → dismiss
6. **Monitor**: `ui_wait_for_analysis(target_processed=5)` — wait for milestone
7. **Repeat**: The app stays running. Keep interacting as needed.

## Error Handling

Every tool returns `{success: true/false, ...}`. On failure:
- `error` — what went wrong
- `hint` — suggested recovery action (timeout → take screenshot; overlay → dismiss)
- `screenshot` — viewport at time of failure (for click failures)

## Folder Dialog Intercept

Native OS file dialogs can't work in headless mode. Instead, `ui_set_folder(path)`
controls what path is returned when JS calls `choose_directory()` /
`choose_directories()`. Set it before triggering any folder picker action.

## Auth / Signed-In Testing

Set `KESTREL_AUTH_TOKEN` env var before running `setup.sh`. The setup script
writes the JWT to `~/.local/share/project-kestrel/auth.json`. On app start,
wait 2–3 seconds — the account button should show the signed-in state.
This enables cloud compute submission, Perch uploads, and account features.
