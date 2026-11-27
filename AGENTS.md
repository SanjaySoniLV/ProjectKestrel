# AGENTS.md

This file orients AI coding agents and human contributors working in the **Project Kestrel**
desktop app repo. It is tracked and public. Keep it, and everything else in this repo, free of
anything private: no internal infrastructure detail, no names of private repositories or
documents, no secrets, no local machine paths.

## What this project is

Project Kestrel is a free, open-source (AGPLv3), local-first wildlife-photography culling app for
Windows and macOS. It detects birds in your photos, identifies species, scores sharpness, groups
bursts into "scenes", and lets you browse and cull a library. All ML runs on-device. Two optional
paid hosted add-ons, **Perch** (share curated outings) and **Cloud Compute** (GPU offload), are
reached over HTTPS through client modules in this repo. The app never depends on them and runs
fully offline.

It is a single-process [pywebview](https://pywebview.flowrl.com/) app: a Python backend that owns
a native window, plus a vanilla JS/HTML/CSS frontend. No SPA framework, no bundler, no Electron,
no PyQt. It ships as one PyInstaller binary.

## Repository structure

```
analyzer/
├── visualizer.py        # entry: pywebview window + local static-file server (GET only)
├── api_bridge.py        # Api class: the only JS → Python path (the trust boundary)
├── queue_manager.py     # sequential folder-analysis queue
├── settings_utils.py    # atomic settings.json I/O + sanitizer allowlists
├── cli.py               # headless entry point
├── visualizer.html      # main window markup (Cull mode is js/cull-*.js inside it)
├── js/*.js              # frontend modules: classic scripts sharing one global scope, load order in visualizer.html
├── css/*.css            # stylesheets; design tokens live in css/base.css, dialogs in css/dialogs/kdlg.css
├── *_client.py, perch_uploader.py, oauth_client.py   # optional hosted add-ons
├── metadata_writer.py, editor_launch.py              # XMP sidecars, external editors (security-sensitive)
├── kestrel_analyzer/    # pure analysis pipeline, no UI imports
│   └── ml/              # ONNX Runtime sessions (DirectML / CoreML / CPU)
├── models/              # bundled weights and data (shipped to every user)
├── ProjectKestrel*.spec # PyInstaller specs (Windows, macOS direct, macOS App Store)
└── tests/               # pytest: unit / integration / compat / ui markers + security tests
packaging/               # installer build scripts (Inno Setup, MSIX, macOS)
tools/                   # build-time asset tooling (not shipped)
experiments/             # research scripts (not shipped, not imported by the app)
```

An analysis run writes `.kestrel/` beside the user's photos (`kestrel_database.csv`,
`kestrel_scenedata.json`, `kestrel_metadata.json`, `crop/`, `export/`, `embeddings/`). This schema
is shared by the browse UI and both hosted add-ons, so changing it is a breaking change. Say so in
the PR.

## Setting up and running

```bash
pip install -r requirements-windows.txt    # Windows (DirectML)
pip install -r requirements-macos.txt      # macOS (CoreML)
pip install -r requirements.txt            # Linux / generic

python analyzer/visualizer.py              # desktop app
python analyzer/cli.py "/path/to/photos" --no-gpu

pytest analyzer/tests -m unit              # fast lane, no model weights (~1 min)
git lfs pull && pytest analyzer/tests -m "not ui"   # full lane: needs real weights, not LFS stubs
```

## Packaging (PyInstaller)

The app ships as a frozen binary, so code that works from source can still break in a release.

- There are three specs: `analyzer/ProjectKestrel.spec` (Windows),
  `analyzer/ProjectKestrel-macos.spec` (macOS direct download) and
  `analyzer/ProjectKestrel-macos-appstore.spec` (sandboxed Mac App Store). Their `datas` and
  `hiddenimports` lists are maintained by hand. **When you add a top-level module, a data file, or
  an import that happens inside a function or `try` block, add it to every spec that needs it.**
- `models/` is bundled wholesale. Don't put build inputs, backups or experiments there. They ship
  to every user.
- Don't add yet another `sys.frozen` / `_MEIPASS` check where an existing helper already finds the
  bundled file.
- The App Store build is sandboxed: file access goes through `mac_sandbox.py`, and payment links are
  gated by storefront. Don't add a code path that opens files or URLs around those helpers.
- If you touch a spec, packaging script or model file, say in the PR that a frozen build needs a
  smoke test.

## Licensing

- Source code is **AGPLv3** (`LICENSE`). By contributing, you license your contribution under it.
- The names "Project Kestrel", "Perch" and "Cloud Compute", and the logos and files in `assets/`,
  are **not** AGPL. They are proprietary (`assets/LICENSE`). Don't add them to new places that a
  fork would need to rebrand, and don't copy them into third-party projects.
- **Models and data.** Every bundled model or dataset needs a licence that allows redistribution
  inside an AGPLv3 app, and a `NOTICES.md` in its `models/` folder giving the source, licence text
  and required attribution. Non-commercial (e.g. CC BY-NC) and "research only" licences are not
  acceptable. Converted weights (ONNX, quantised) are still covered by the original licence.
- **Dependencies.** New Python or JS dependencies must be AGPL-compatible (MIT, BSD, Apache-2.0,
  LGPL, GPL-3.0 are fine; GPL-2.0-only and proprietary are not). Prefer the standard library; ask
  before adding a dependency.

## How to write code here

Reviewers check these rules; automatic checks cover some of them.

**Size and placement**
- A file over 1,500 lines may not grow. It may shrink. Put new code in a new module and call it
  from there. This rule never asks you to split an existing large file as part of an unrelated
  change: add your code beside it instead.
- Aim for functions under about 60 lines. This is a guideline, not a hard limit: some logic really
  is one long sequence. If a new or changed function runs well past 150 lines, say why in the PR.
- `Api` methods in `api_bridge.py` are thin: validate input, call a domain module, shape the
  result. New logic goes in that domain module, not in the bridge.
- `kestrel_analyzer/` must not import UI or bridge code.

**Reuse these, don't rewrite them**
- Atomic file writes: `kestrel_analyzer/atomic_io.py` (`write_atomic`, `write_text_atomic`,
  `write_json_atomic`, `write_bytes_atomic`, `copy_file_atomic`; also re-exported by `database.py`).
- Settings: `settings_utils.py` only. Persisted keys need a sanitizer entry.
- Path checks in bridge methods: `_validate_root_dir`, `_is_within_root`, `_resolve_path_in_root`.
- Logging: `settings_utils` `info/warn/error`, and `kestrel_analyzer/logging_utils.py` for the
  analysis log.
- JS: toasts via `showToast` (`js/state.js`). Reuse an existing escaping or formatting helper
  rather than writing another one.
- If you find two copies of a helper, merge them rather than adding a third.

**Comments**
- Explain *why the code is the way it is now*: a constraint, a non-obvious invariant, a platform
  quirk. Keep most comments to a few lines; put longer design notes in `DEVELOPMENT.md`.
- Don't narrate history ("used to", "previously", "removed in phase N", "this fixes the bug where").
  That belongs in the commit message and PR description.
- Don't reference anything a reader of this repo can't open. Link a public GitHub issue or nothing.
- When you remove code, remove its comments, call sites and stubs too. Don't leave no-op functions
  "to keep the diff small".

**Errors**
- Never swallow an exception silently. Log it, or add a one-line comment saying why it's safe to
  ignore. Don't add new empty `catch {}` or `except Exception: pass` blocks.
- Catch broad `Exception` only at boundaries: bridge methods (return `{"success": False,
  "error": ...}`), worker-thread loops, shutdown and telemetry. Elsewhere, catch the specific error.
- Telemetry and network calls must never block or crash the app when offline.

**Frontend**
- JS files are classic scripts sharing one global scope, and load order matters (see the `<script>`
  tags at the end of `visualizer.html`). New files wrap their code in an IIFE and expose at most one
  namespace object. Don't add new top-level names to existing files casually.
- No new GUI framework (React, Vue, …). UI changes go in `visualizer.html` plus a module under
  `analyzer/js/` and a stylesheet under `analyzer/css/`.
- Put untrusted text (file names, tags, scene names) in the DOM with `textContent`. If you must use
  `innerHTML`, escape it first. The XSS security test enforces this.
- Colours, radii and spacing come from the CSS variables in `css/base.css`. No new hex colours
  outside that file, no new `!important`.
- Dialogs use the `.kdlg` system (`css/dialogs/kdlg.css`). Don't invent a new button or dialog
  style for one feature.
- User-visible text is plain and sentence case, uses the words in `WORD_LIST.md`, and lives in the
  HTML or in one constant block per feature, not concatenated inside logic.

**Security boundaries (non-negotiable)**
- `api_bridge.py` is the only path from JS into privileged code. Any bridge method that takes a
  path must use the root-jail helpers.
- The local HTTP server serves static files via GET only. Never add a control route; add a bridge
  method.
- `metadata_writer.py`, `editor_launch.py` and URL opening are security-sensitive. Keep the
  `tests/test_security_*.py` tests passing.
- No secrets in the repo: no API keys, `.env` files, credentials or private endpoints.

## Tests

- Every bug fix comes with a test that fails before the fix and passes after it.
- Test behaviour: call the Python function, or run JS logic through Node. Don't assert that source
  files contain particular strings, except in security lints.
- `unit` tests need no model weights and must stay fast. Anything that loads a model is
  `integration`.
- Add new tests to the existing file for that module where one exists, instead of a new file per PR.
- Run `pytest analyzer/tests -m "not ui"` (with real weights) before opening a PR that touches the
  pipeline, models, `.kestrel/` I/O or packaging.

## Branches and pull requests

- **Target `dev`, not `main`.** `main` only takes urgent fixes that must ship immediately.
- Name branches `<type>/<short-description>`: `feat/` (new features), `ui/` (user interface
  improvements), `fix/` (bug fixes and stability improvements), `docs/`, `exp/` (experiments not
  meant to merge as-is) or `release/`. If your tooling requires a `claude/` prefix, use
  `claude/<type>-<short-description>` (for example `claude/fix-directml-cpu-fallback`) instead of a
  generated name. One topic per branch. If a branch builds on another, name the parent branch in the
  PR description. Delete the branch once its PR is merged or closed.
- One purpose per PR: a feature, a fix or a refactor. Refactors change no behaviour.
- Keep it under ~400 changed lines outside tests where you can; split larger work.
- Fill in the PR template: the problem, how to reproduce it, what the PR does, plus the checklist.
  Keep each paragraph to a few sentences and link any relevant issue.
- Say if the PR was written by an AI agent. That's welcome; reviewers just read it differently.
- Update `README.md` / `DEVELOPMENT.md` in the same PR when you change something they describe.
