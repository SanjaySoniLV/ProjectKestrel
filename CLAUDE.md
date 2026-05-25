# Project Kestrel

Desktop app for analyzing bird photos using ML. Built with pywebview (Python) + vanilla JS frontend.

## Architecture

- `analyzer/visualizer.py` — App entry point. Starts HTTP server + pywebview window.
- `analyzer/api_bridge.py` — Python API class exposed to JS via `window.pywebview.api.*`
- `analyzer/js/` — Frontend JavaScript modules
- `analyzer/visualizer.html` — Main HTML page
- `analyzer/kestrel_analyzer/` — ML pipeline (ONNX models, image processing)
- `analyzer/models/` — ONNX model files (some are Git LFS)

## UI Automation (Kestrel UI Testing)

See `.claude/skills/kestrel-ui-testing.md` for instructions on interacting with
the real app UI via the Playwright-based MCP server in `perch_ui_harness/`.

Quick start:
```bash
bash perch_ui_harness/setup.sh          # one-time: install deps, download models
xvfb-run -a python3 perch_ui_harness/mcp_server.py   # start MCP server
```

## Development

- Python dependencies: `pip install -r requirements.txt`
- Run in dev mode: `python analyzer/visualizer.py --port 8765`
- CLI mode: `python analyzer/visualizer.py --cli /path/to/folder`

## Test Images

`test_imgs/` contains CR3 raw files for testing. Note: Git LFS models in
`analyzer/models/speciesnet/` must be real files (not pointers) for analysis to work.
