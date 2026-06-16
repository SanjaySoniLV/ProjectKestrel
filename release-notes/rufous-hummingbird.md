# New in v(Rufous Hummingbird): Celebrating 1.5 Million Photos Analyzed by Kestrel

This update marks the launch of **Perch** and **Cloud Compute**, and includes several bug fixes, UI improvements, and stability improvements.

Introducing **Perch** by Project Kestrel: share entire birding outings with anyone. Perch is a new sharing platform built on Project Kestrel that lets you upload and personalize an entire timeline of everything you captured during your outing. Read more below.

Introducing **Cloud Compute**: offload analysis of large backlogs to fast cloud GPUs. Cloud Compute is a paid subscription service that runs the exact same Project Kestrel analysis pipeline in the cloud, parallelizing your workload across up to 3 cloud GPUs.

New accounts start with free Perch storage (3 GB / ~15,000 photos) and 2,500 images of Cloud Compute credits to try.

## Major Changes

- **Introducing Perch: Share Entire Birding Outings with Anyone**
  - Perch is a sharing platform centered around the experience of birding: upload an entire timeline of everything you saw on your outing, personalize it with notes, and share it with your friends. 
  - To get started, open an analyzed folder and click "Share with Perch." Follow the steps to upload your timeline, then open your Perch on perch.projectkestrel.org. Add notes, select your favorites, and share! Users can browse your timeline, search by species, read your notes, leave comments, and like their favorites.
  - All accounts have 3GB of free Perch storage, equivalent to ~15,000 photos.
  - Sync features let you update species/scientific name tags. 
  - Perch is a completely optional extension of Project Kestrel; no account is needed to continue using the free and open-source desktop app.

- **Cloud Compute — offload analysis to cloud GPUs.**
  - Hand a big backlog to the cloud and keep working locally; it runs the *exact same* Kestrel pipeline, spread across **up to 3 GPUs at once**.
  - New accounts get **2,500 images of credits** to try. Simply sign in, then click "Cloud Analysis" in the "Analyze Folders" dialog. 
  - See your job history, track your usage, and manage your account online at myaccount.projectkestrel.org.
  - Cloud Compute respects your privacy: Photos are deleted from the cloud as soon as they are analyzed, and analysis results are deleted as soon as your desktop client receives them.
  - Subscribing to Cloud Compute is a great way to support me, the solo developer/owner of Project Kestrel.
  - Cloud Compute is a completely optional extension of Project Kestrel; no account is needed to continue using the free and open-source desktop app.

- **Account Management Surface**
  - For users of Perch or Cloud Compute, you can sign in with your account within the Project Kestrel desktop app. 
  - See your Cloud Compute job history, download any results that are ready on the server, cancel in-progress jobs, see your usage, and more.
  - Easily access your full account management portal by clicking "Manage my Account".
  - When submitting feedback or bug reports as a signed-in user, you can optionally attach your user information to that report.

- **Privacy, security & legal.**
  - Project Kestrel's license is becoming less restrictive — it has been relicensed to **AGPLv3**.
  - The Privacy Policy and Terms of Use have been updated.


## Minor Changes
  - **Log redaction**: the local OS username is stripped from log/traceback paths before any feedback or crash report leaves the device. This behavior has been strengthened for improved privacy protections of anonymous users.
  - You can now opt-out of automatic Crash Report sending in Settings.
  - Fixed a bug where the user could not open two instances of Project Kestrel at once.
  - Fixed a bug leading to unnecessary crashes for some Windows users.
  - New searchable dropdown for family tags adds parity with the species tag improvements introduced in v(Great-Horned Owl).
  - Polished user interface across the entire app.
  - For contributors: Large test-coverage expansion — Perch uploader, Cloud Compute (cancel-desync, image discovery, relocate, submit errors, read-only listing), pack-merge integration, log redactor, net-TLS, telemetry, and feedback routing.

---

## 3-Bullet Summary (for version.json)
1. Introducing Perch by Project Kestrel: share entire birding outings with anyone in just a few clicks. Perch is free for everyone; each account gets 3GB/~15,000 photos free storage.
2. Introducing Kestrel Cloud Compute: offload analysis to fast, parallelized cloud GPUs. New accounts get 2,500 images of free cloud analysis to start.
3. Several bug fixes, UI improvements, stability improvements, and minor feature additions.

### Headline for in-app banner
`New in v(Rufous Hummingbird) — Perch & Cloud Compute are live!`
