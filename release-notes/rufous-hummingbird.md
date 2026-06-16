# v(Rufous Hummingbird) — 2026-06-15

This is the headline release: the first-ever go-live of **Perch** — share an entire birding outing on the web as a timeline, not just loose photos — and **Cloud Compute**, which offloads analysis to cloud GPUs so a backlog crunches while you keep working. It ships everything that has accumulated on `dev` since Great-Horned Owl, and marks a milestone of **1.5 million photos analyzed** in roughly three months. New accounts start with free Perch storage (3 GB / ~15,000 photos) and 2,500 images of Cloud Compute credits to try.

## Major Changes

- **Perch — share entire birding outings on the web.**
  - New **Share with Perch** flow uploads your whole analyzed timeline (scenes, crops, scientific family/species names) to `perch.projectkestrel.org`, then lets you personalize it with notes before sharing the link with anyone.
  - Free for everyone: **3 GB (~15,000 photos) per account.**
  - Rebuilt upload engine (`perch_uploader.py`): a clean three-step create → presign → commit flow. The desktop PUTs assets directly to R2 in parallel, then the server verifies actual object sizes before flipping the perch to `complete` — no more on-disk resumable-upload machinery.
  - The folder button flips to **"On Perch"** the moment an upload completes.
  - **Re-link** an already-shared folder to an existing perch without re-uploading, plus **tag-sync** to push tag edits from a linked folder up to its perch.
  - Sign-in via OAuth 2.0 Authorization Code + PKCE against Clerk (`oauth_client.py`); the same identity gates both Perch and Cloud Compute.

- **Cloud Compute — offload analysis to cloud GPUs.**
  - Hand a big backlog to the cloud and keep working locally; it runs the *exact same* Kestrel pipeline, spread across **up to 3 GPUs at once** (`cloud_compute_client.py`).
  - New accounts get **2,500 images of credits** to try. Found in the **Analyze Folders** dialog.
  - Full job-management UI: account panel, per-account job history, in-progress hourglass with auto-refresh, pending queue with auto-drain, resume/load actions, and cancel.
  - Results merge back into the standard `.kestrel/` on-disk layout (database CSV, scenedata, crops, exports) — identical to a local run, so analyzed cloud folders register in recents.
  - Advanced analysis settings and **retry-errored** are piped through to the cloud; correct scene-index merging when stitching scene packs.

- **Account, billing & entitlements surface.**
  - New auth/entitlements client drives Cloud Compute usage and Perch quota from the server (`auth.js` / `AuthClient`); credits and two-bucket usage **bars are rendered verbatim from the server** — all client-side billing math was deleted.
  - Desktop **notification bell** with a deep-linkable "View →" action on notifications.
  - Opt-in **"send as my account"** toggle routes signed-in feedback to the Auth Worker (with analytics fallback).

- **Privacy, security & legal.**
  - Project relicensed to **AGPLv3**; updated Privacy Policy and Terms of Use, with a legal-agreement check before first Perch/Cloud Compute use.
  - **Log redaction** (`log_redactor.py`): the local OS username is stripped from log/traceback paths before any feedback or crash report leaves the device.
  - Crash-report sending is now **user opt-out** in Settings.
  - `id_token` dropped from the keyring payload to shrink and harden stored credentials.

## Minor Changes

- **macOS frozen-app TLS fix** — a shared certifi-backed SSL context (`net_tls.py`) for all stdlib-`urllib` clients, so HTTPS no longer fails with `CERTIFICATE_VERIFY_FAILED` inside a bundled `.app`.
- **App-shutdown upload-hang fix** — the app no longer hangs on exit while a Perch/cloud upload thread is in flight.
- **CC billing correctness** — fixed an under-count from stale packs being re-downloaded, and an image-discovery off-by-one between the cloud and local paths; orphan JPEGs in mixed RAW+JPG folders are no longer dropped.
- **Cloud Compute reliability** — cancel-desync fix, removal of client-side pause, cold-start retries for account/settings hydration, and validation that a job finishes uploading before the next one starts.
- **Billing period** date fixed (epoch seconds, UTC) and usage bars labeled.
- **Windows** — falls back to a nearby port when 8765 is occupied; `DefWindowProcW` argtypes set to forward 64-bit `LPARAM` safely.
- Searchable dropdown for family tags; faster Analyze Folders dialog load; unified dialog system with responsive sizing; assorted UI polish (cloud hourglass, neutral tree, corner-radius tokens, descender clipping, notification-panel clipping).
- Crash-report payloads now carry a build attestation header.
- New telemetry module (`kestrel_telemetry.py`): failsafe, non-blocking, opt-in analytics that never send paths or image data.
- Large test-coverage expansion — Perch uploader, Cloud Compute (cancel-desync, image discovery, relocate, submit errors, read-only listing), pack-merge integration, log redactor, net-TLS, telemetry, and feedback routing.

---

## 3-Bullet Summary (for version.json)
1. Introducing Perch and Cloud Compute — share entire birding outings on the web (3 GB free per account) and offload analysis to cloud GPUs (2,500 free credits to start).
2. Built-in accounts with server-driven credits and usage bars, a notification bell, opt-in feedback-as-your-account, and a full Cloud Compute job manager.
3. Relicensed to AGPLv3 with username log redaction and opt-out crash reports, plus macOS frozen-app TLS and upload-shutdown fixes and CC billing/discovery corrections.

## In-App Note (already authored this session)
Note: the in-app "What's New" is a custom launch letter this release (see `WHATS_NEW` in `welcome.js` / `whats-new.json`), so the old 3-4 bullet `items` format does not apply. The note is a personal letter from the developer celebrating the 1.5M-photos milestone, then introducing the two new platforms via cards — Perch (free, 3 GB/~15,000 photos) and Cloud Compute (up to 3 GPUs, 2,500 starting credits) — with a link to the full launch blog and a call for feedback.

### Headline for in-app banner
`New in v(Rufous Hummingbird) — Perch & Cloud Compute are live!`
