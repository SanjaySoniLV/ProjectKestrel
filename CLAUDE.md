# CLAUDE.md

Guidance for Claude Code in the Project Kestrel desktop app repo. **This file is tracked and public** —
keep it free of anything private: no internal infrastructure detail, no references to private repos or
private documentation, no local machine paths, no secrets. Maintainer-only notes belong in the gitignored
`.claude/CLAUDE.md`.

The project overview, repository structure, setup commands, contribution rules and PR conventions
(PRs target `dev`, not `main`) are in `AGENTS.md`, imported here:

@AGENTS.md

The rest of this file adds detail for working on the code.

> `README.md` and `DEVELOPMENT.md` still describe an older layout (a separate PyQt `analyzer/` GUI, a
> standalone `visualizer/` web server, and a single ~10k-line `analyzer/visualizer.js`). **That is out of
> date**: there is no PyQt GUI and no standalone server, and the frontend is ~25 modules under
> `analyzer/js/`. Don't rely on those two files for structure questions.

## Architecture: the bridge is the trust boundary

```
  visualizer.py  ── owns the process: opens a native pywebview window AND a
       │            local-only HTTP server (127.0.0.1) that serves ONLY static
       │            files via GET (visualizer.html, analyzer/js/*.js, assets)
       │            under a strict CSP. The handler implements do_GET only.
       │            The old HTTP control API (/settings, /queue/*, /open) was
       │            removed — do not add control routes back.
       ▼
  pywebview window (WebView2 on Windows / WKWebView on macOS)
       │
   window.pywebview.api   ◄── the single JS→Python trust boundary
       ▼
  api_bridge.py (Api class)  ── every privileged operation is a method here:
       │   folder pick, file read, settings I/O, analysis control, cloud /
       │   Perch / auth calls. Path-accepting methods confine every operation
       │   to a validated root (_validate_root_dir / _is_within_root /
       │   _resolve_path_in_root) so a hostile filename or `..` can't escape
       │   the photo folder.
       ▼
  queue_manager.py  ── sequential folder-analysis worker; snapshots settings at
       │                enqueue time; lazy-imports the pipeline so browse-only
       │                sessions start instantly; reuses loaded models across items.
       ▼
  kestrel_analyzer/pipeline.py (AnalysisPipeline.process_folder)
       │   decode → detect (MegaDetector) → classify (SpeciesNet + custom ONNX
       │   bird model) → segment (SAM-HQ) → quality score → exposure solve →
       │   scene-cluster → crop/export → write .kestrel/
       ▼
  kestrel_analyzer/ml/   ── ONNX Runtime sessions (DirectML on Windows, CoreML on
       │                     macOS, CPU everywhere). Lazy-loaded on first analysis.
       ▼
  <photos>/.kestrel/   ── the durable output:
            kestrel_database.csv   (per-image results)
            kestrel_scenedata.json (scene grouping + user ratings/tags)
            kestrel_metadata.json  (analysis-run audit trail)
            crop/  export/         (JPEGs the UI renders)
```

The optional hosted add-ons are reached through client modules on the bridge. Their server code is not in
this repo; the desktop app only talks to them over HTTPS. All of them read or write the same `.kestrel/`
schema:

- `cloud_compute_client.py` (+ `cloud_jobs_store.py`, `cloud_folder_state.py`) — GPU offload: create job →
  upload images directly to object storage via presigned URLs → poll → download result packs → merge into
  `.kestrel/`. Frontend: `js/cloud-compute.js`.
- `perch_uploader.py` (+ `perch_manifest.py`) — publish an outing: build a local manifest → presign →
  direct upload → commit. Frontend: `js/perch.js`.
- `oauth_client.py` (+ `auth_client.py`) — sign-in with Clerk. The resulting Clerk JWT authorizes both
  Cloud Compute and Perch calls and is stored in the OS keyring. Frontend: `js/auth.js`.
- `kestrel_telemetry.py` — fire-and-forget telemetry, feedback and crash reports. Must never block or
  break the app when offline.

## Auth transports (one token model, per-platform sign-in)

Every hosted service validates the same Clerk JWT (Clerk JWKS, issuer `https://clerk.projectkestrel.org`).
Clerk OAuth access tokens and Clerk session tokens are signed by the same JWKS and are interchangeable at
the services.

- **Windows / Linux / macOS Google+email:** OAuth 2.0 Authorization Code + **PKCE** against a Clerk OAuth
  application (`oauth_client.py:run_authorization_flow`). Windows/Linux open the system browser and listen on
  a loopback callback: the first free port from `LOOPBACK_PORTS` (17893, 27184, 37265, 47632, 53682 — all
  registered with Clerk; see the comment in `oauth_client.py` for why it's a list). macOS uses
  `ASWebAuthenticationSession` with the `kestrel://callback` scheme (`mac_oauth.py`), because App Store
  Guideline 4 forbids bouncing to the default browser.
- **macOS App Store "Sign in with Apple":** native `ASAuthorizationController` (`mac_apple_signin.py`)
  gets an Apple id_token → Clerk Frontend API sign-in/up → a **Clerk session JWT** used directly (no
  `/oauth/authorize`). The Frontend API calls run in Clerk's **native mode** (`_is_native=1` + a Bearer
  client token, the same surface Clerk's iOS SDK uses), which is exempt from Clerk's CAPTCHA, so bot
  protection can stay on. The `_NativeSession` carries the client token (returned in each response's
  `Authorization` header); `remint_session_token` uses it to mint fresh session tokens (both are in
  `oauth_client.py`). The preferred
  session token is the `kestrel_api` JWT template, falling back to the default session token if the
  template is unavailable.

## Commands beyond AGENTS.md

```bash
python analyzer/cli.py "/path/to/photos" --gpu --parallel-prefetch 3
pytest analyzer/tests/unit/test_database.py -v

# Validate a build (source tree or frozen binary)
python analyzer/cli.py --validate --validate-images test_imgs --validate-output validate.json

# Build the Windows installer (PyInstaller → Inno Setup)
packaging\build_installer_headless.bat
```

- The full lane (`pytest analyzer/tests -m "not ui"`) needs the Git LFS model files downloaded, not LFS
  pointer stubs. Running only `-m unit` hides model-path regressions; quality integration tests have sat
  broken for months before because only the fast lane was being run.
- `analyzer/tests/conftest.py` inserts `analyzer/` on `sys.path`, so test imports are
  `from kestrel_analyzer.database import ...` (not `analyzer.kestrel_analyzer...`). Match that in new tests.
- CI workflows are in `.github/workflows/` (`tests.yml`, platform build workflows, `cloud-e2e.yml`).

## Things to know

- **Adding a bridge call takes three edits:** (1) a method on `Api` in `analyzer/api_bridge.py`, (2) a call
  site in the relevant `analyzer/js/*.js` module, (3) an entry in `_sanitize_settings_payload()` in
  `analyzer/settings_utils.py` if the value is persisted.
- **The settings sanitizer is strict.** Unknown keys pass through with size caps, but anything that needs
  validation (editor names, rating profiles, detector names) must be added to the allowlists at the top of
  `settings_utils.py`. Settings are snapshotted at enqueue time, so changing them mid-run doesn't affect
  the running job. Writes are atomic, guarded against going backwards, recover from `.bak`, and are
  serialized by `_SAVE_LOCK`; don't write settings any other way.
- **Path-taking bridge calls enforce root-boundary checks** and log rejections. Don't bypass them; follow
  the existing pattern. XMP/sidecar reads, `metadata_writer.py` and `editor_launch.py` are
  security-sensitive — the `analyzer/tests/test_security_*.py` regression tests cover XSS, path traversal
  and URL-scheme abuse; keep them passing.
- **opencv duplicate install:** if pip ends up with both `opencv-python` and `opencv-python-headless`
  (speciesnet pulls in the headless one), `cv2.imwrite` rejects the JPEG-quality argument. Fix:
  `pip uninstall -y opencv-python-headless` then `pip install --force-reinstall opencv-python==4.11.0.86`.
- **The `.kestrel/` schema is shared** by the browse UI, the Cloud Compute result-pack merge and the Perch
  upload. Changing columns in `kestrel_analyzer/database.py` affects all three. The Cloud Compute GPU
  service runs a copy of this repo's `analyzer/`, so pipeline-output or model-loading changes also need to
  be mirrored there (maintainers handle that sync).

## macOS: two separate builds

- `analyzer/ProjectKestrel-macos.spec` — the Developer ID-signed direct-download DMG.
- `analyzer/ProjectKestrel-macos-appstore.spec` — the **sandboxed Mac App Store** build. It uses native
  Sign in with Apple (above) and **gates external purchase links by storefront** (`mac_storefront.py` +
  `dist_channel`): Apple's anti-steering rule exempts only the US storefront, so the donate/"Support" link
  is shown only when `SKStorefront` reports `USA`; otherwise the no-payment page is shown (fails closed).
  Signing and upload run in CI (`.github/workflows/build-macos-appstore.yml`). Signing material is kept
  outside the repo and must never be committed.
