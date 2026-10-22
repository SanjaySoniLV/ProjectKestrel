# Analysis pipeline 3.0.0 — prototype notes (`exp/pipeline-v3`)

Branch `exp/pipeline-v3` (on top of `exp/semantic-search` + the SigLIP tokenizer/assets
commits `a2e349a`, `841f6e8`). Written 2026-10-22 for local testing on Windows and macOS.

## What changed

**Pipeline (`analyzer/kestrel_analyzer/pipeline.py`)**

```
decode ─► MegaDetector (animal boxes ≥ detection threshold, box dedupe, ≤ 2×max_bird_crops)
       ─► SAM-HQ masks ─► square exposure-corrected crop (unchanged)
       ─► BioCLIP 2 image embedding of the crop (ONE run per box)
            ├─ false-positive gate  ml/animal_gate.py      linear probe on the embedding, keep iff P(animal) ≥ 0.12
            ├─ bird vs wildlife     BioClipSpeciesClassifier.route   (softmax over geofenced birds ∪ 1,153 wildlife labels)
            └─ bird species/family  BioClipSpeciesClassifier.classify_embedding (unchanged head)
       ─► per-class cap (max_bird_crops birds + max_bird_crops wildlife), birds first
       ─► SigLIP 2 crop embedding ─► quality_siglip (calibrated zero-shot sharpness) + stored for search
       ─► legacy quality CNN       ─► quality_legacy
       ─► SigLIP 2 embedding of the downscaled whole frame (export JPEG pixels) ─► stored for search
```

* SpeciesNet classifier, ensemble, taxonomy routing and assets removed (`speciesNet_v4.0.1a.onnx`,
  labels/geofence/taxonomy/info.json). `speciesnet_sam_hq.py` keeps its name; the class is now
  `DetectorSAMHQWrapper` (alias `SpeciesNetSAMHQWrapper`), detector + SAM only.
* OpenAI CLIP prototype removed (`models/clip/`, `clip_search.py`, `clip_tokenizer.py`, fixture,
  tests). A folder indexed by the CLIP prototype just shows as "needs upgrade"; its dead
  `embeddings/clip-b16` slot is deleted by the next analysis or upgrade.
* Nothing references the old North-American CNN (`model.onnx` is not on this branch).
* Wildlife rows keep the SpeciesNet-era CSV/UI semantics: species = title-cased common name
  (e.g. "White-Tailed Deer"), family = "N/A". The wildlife taxonomy goes into the crop record
  (`wildlife_scientific`, `wildlife_family`, `wildlife_class`) instead.
* New per-crop fields in `crops_json`: `quality_siglip`, `quality_legacy`, `animal_score`,
  `subject` (bird/wildlife), `bird_prob` (+ `gate_flagged` after an upgrade).
* Stage timers on the pipeline (`stage_times`) and `tools/bench_pipeline.py`.

**Schema / settings / versioning**

* `config.VERSION = "3.0.0"`, `PIPELINE_MAJOR = 3`. Metadata: `pipeline_major` (only set when every
  row is pipeline-3: a fresh analysis or a completed upgrade; a 2.x folder topped up by a 3.0 build
  keeps it absent so the upgrade stays on offer), `models{detector,species,siglip,legacy_quality,
  animal_gate}`, `quality_model`, `schema_version: 3`.
* CSV schema 3: `quality` stays the effective score (all readers unchanged) + `quality_siglip`,
  `quality_legacy`. Loading an older CSV sets `quality_legacy = quality`, `quality_siglip = -1`.
  New fixture `v_pipeline-v3_*` (literal `cli.py --no-gpu` on `set_a_fresh`), SCHEMA_NOTES updated.
* Setting `quality_model` (`siglip` default | `legacy`), Settings → *Quality scorer*. Switching is
  free: `quality_model.apply_quality_model` rewrites `quality` + each crop's `quality`/`rating` from
  the stored scores. **When:** lazily in `Api.read_kestrel_csv` when the folder's metadata records a
  different scorer (skipped while that folder is analysed/upgraded); the Settings dialog saves pending
  edits and reloads the open folders after a switch. Pre-3.0 folders are never rewritten (no SigLIP
  score yet). The primary crop is never re-pointed (it may be a manual pick).
* Setting `tower_device` (`auto` | `cpu` | `gpu`), Settings → *BioCLIP / SigLIP device*; env
  `KESTREL_TOWER_DEVICE` overrides.

**Smart search (SigLIP 2)** — `ml/siglip.py`, `query_parser.py`, `smart_search.py`, `js/smart-search.js`

* Store slots `siglip2-b16` (crops) and `siglip2-b16-frame` (whole frames).
* Score: crop `cos(crop, T("a photo of {q}."))`, image `max(z(frame), z(best crop))` (z over the
  folder's images for that query; one-source images use the one z), scene = max over its images.
  48-scene cap and root-jail validation kept.
* Query parsing: greedy longest n-gram (≤6 words) over IOC common names + aliases + scientific names,
  family display-name parts ("Hawk/Eagle/Kite sp." → hawk/eagle/kite) + scientific family, wildlife
  common/scientific names, and folder-tagged species' last word ("robin" → American Robin when the
  folder has one). Case/apostrophes/hyphens/plurals folded; 1 typo for ≥5 letters, 2 for ≥10.
  Ties: tagged in folder > in folder's region > exact > bird. Matches are a hard OR filter on the
  stored tags; user-edited scene tags win (finalized scenes are judged by `user_tags` only).
  Remainder → SigLIP; when a filter matched, SigLIP sees the whole query ("dove pair", "heron in a
  marsh"), and nothing left → filter-only, sorted by quality.
* UI: coloured removable chips (species / family / wildlife / species group), "none here" when a chip
  matches no photo, example queries + chip legend in the empty state, cards show their best-matching
  photo with an `n/m` match badge, matching photos outlined in the scene filmstrip.

**Upgrade to pipeline 3 (local only)** — `upgrade_v3.py`, `Api.upgrade_folder_v3`, Folder Actions,
`cli.py --upgrade-v3`

* Eligible: metadata `version` (creation) ≥ 2.0.0 and at least half of the sampled crop JPEGs on disk.
  2.0.0 is the MegaDetector + SAM-HQ square-crop pipeline (evidence: `set_c_preanalyzed` is 2.0.1 /
  mdv5a; the Kentucky-Warbler 1.7.0 fixture already has `crops_json`, but its detector generation is
  unconfirmed, so 1.x is conservatively "re-analyse"). Status values: `current` / `upgradable` /
  `reanalyze` / `not_analyzed` (`smart_search_status().upgrade`).
* Reads only `.kestrel/crop/*` and `.kestrel/export/*` (jailed). Per crop: BioCLIP → species re-ID
  (skipped for images in scenes with edited or finalized user tags), routing (wildlife only if the
  folder was analysed with wildlife on), gate **flags only**; SigLIP → `quality_siglip`; the old
  `quality` → `quality_legacy`. Per image: SigLIP frame embedding.
* Never touches `kestrel_scenedata.json`, culling columns (re-merged by `save_database`), XMP, the
  primary crop, or any detection. Backs up the CSV once (`OLD_kestrel_database_pre-v3_<ts>.csv`),
  saves atomically every 25 images, resumable/idempotent, cancellable, refused while that folder is
  being analysed (and pauses if analysis of it starts). Borrows the queue's pipeline object
  (`QueueManager.shared_pipeline`) so the process has one ORT session per model on the shared
  ProviderCoordinator (GPU when the queue's "Use GPU" is on). Not reachable from Cloud Compute.

**Performance / memory / GPU**

* One session per model per process: the upgrade reuses the queue's pipeline (the CLIP backfill
  used to build a second image tower per job); `load_models` is locked; the text encoder is a
  process singleton with a 256-query LRU and drops its session + token table + tokenizer
  (~450 MB) after 5 min idle. Analysis never loads the text tower (quality prompts precomputed).
* `ml/providers.py`: every session logs requested vs actual providers and per-EP node counts
  (parsed from ORT's verbose "Node placements" at session creation); in the analysis log under
  `models_loaded.placement` and `analysis_complete.placement`.
* Tower device policy: BioCLIP/SigLIP image towers (int8 DynamicQuantizeLinear + MatMulInteger)
  default to CPU on CoreML (no kernels → split graph) and to the GPU on DirectML.
* Int8 towers always run one image per call.

## Decisions I made (conservative where the brief left room)

1. **Gate** (updated by the parent session after exp 4, commit `d70d535`) = `linear_probe`: a
   768-weight logistic head on the BioCLIP embedding (`models/bioclip/gate_probe.json`), keep iff
   `P(animal) ≥ 0.12`, its leave-one-perch-out 99%-recall point. On 7 perches it separates animals
   from 114 judged real MegaDetector false positives at AUC 0.97 (zero-shot prompts 0.62) and
   rejects ~47% of them and ~88% of background clutter. The threshold is fixed (not tied to the
   detection threshold, which still filters MegaDetector confidence). `prompt_softmax` stays
   available via `GATE_METHOD`. Each crop's `animal_score` is logged in `crops_json` — check it on
   real runs before release.
2. **Wildlife family stays "N/A"** in the CSV (keeps families out of scene tags exactly as before);
   the BioCLIP wildlife family is stored per crop. Flip in `pipeline.process_subject_items` if
   wildlife families should become tags.
3. **SigLIP query text** = whole query when a filter matched (better captions than the bare
   remainder); remainder alone otherwise.
4. **Chips are OR**ed ("sparrow robin" = either).
5. **Quality switch never re-points the primary crop**; the UI's crop round trip now preserves
   unknown crop fields (`crop-data.js`), and promoting a crop copies its two raw scores.
6. **Upgrade eligibility ≥ 2.0.0** (see above); folders with crops deleted → re-analyse.
7. `smart_search_enabled` now only gates the whole-frame embedding (crop embedding is the quality
   score, always on).
8. Upgrade and Folder Actions use `window.confirm` for the one confirmation (simple; the rest of the
   app mostly uses styled dialogs).
9. No What's New text: `welcome.js` only has the per-release note, no dev section.

## Benchmark (CPU, this box)

`tools/bench_pipeline.py analyzer/tests/fixtures/test_sets/set_a_fresh` — 4 × 45 MP CR3, 1 bird
each, `--prefetch 1` unless noted, CPU only, 4 cores **shared with the parent session's ML jobs
(load average 4.5–8 during the runs)**, so absolute numbers are noisy; compare within the table.
`main` = 50cb414 (SpeciesNet + NA CNN), `exp-bioclip` = 578d606, `exp-semantic-search` = 8b29343
(+ CLIP), `v3` = 1bb5956 (two runs) and v3 with the app-default `--prefetch 3`.

| stage (s/image) | main | exp-bioclip | exp-semantic-search | v3 | v3-run2 | v3-prefetch3 |
|---|---:|---:|---:|---:|---:|---:|
| decode | 9.498 | 9.666 | 9.710 | 9.407 | 9.331 | 14.716 |
| detector | 4.808 | 4.681 | 4.834 | 4.764 | 4.739 | 5.687 |
| sam | 2.155 | 2.018 | 2.481 | 2.225 | 2.074 | 2.174 |
| speciesnet | 0.622 | 0.633 | 0.717 | — | — | — |
| bird_cnn | 0.047 | — | — | — | — | — |
| bioclip | — | 0.449 | 0.537 | 0.481 | 0.494 | 0.501 |
| gate_route | — | — | — | 0.023 | 0.032 | 0.032 |
| siglip_crop | — | — | — | 0.210 | 0.191 | 0.250 |
| siglip_frame | — | — | — | 0.156 | 0.148 | 0.189 |
| clip | — | — | 0.207 | — | — | — |
| legacy_quality | 0.139 | 0.249 | 0.382 | 0.226 | 0.139 | 0.176 |
| writes | 0.028 | 0.026 | 0.031 | 0.035 | 0.026 | 0.028 |
| other | 0.551 | 0.772 | 2.422 | 0.707 | 0.645 | (overlapped) |
| **total s/image** | 17.848 | 18.495 | 21.32 | 18.234 | 17.82 | 13.386 (wall, decode overlapped) |
| model load (s) | 2.45 | 3.15 | 43.39 | 2.9 | 2.74 | 2.83 |
| RSS after load (MB) | 1401.6 | 1745.6 | 1723.1 | 1687.2 | 1743.2 | 1694.4 |
| peak RSS (MB) | 5485.2 | 5911.6 | 5922.2 | 5618.4 | 5654.4 | 7454.1 |

Reading it:

* v3 is on par with `main`/`exp-bioclip` per image (≈ +0.0–0.4 s, within noise): SpeciesNet
  (0.63 s) is gone, SigLIP crop + frame (0.34–0.36 s) and the gate/routing (0.03 s) came in, BioCLIP
  now runs on every proposal instead of birds only (same here: 1 box per image).
* The model work is small next to RAW decode (~9.4 s) + MegaDetector mdv5a at 1280 (~4.7 s) on this
  CPU. On a GPU the detector/SAM shrink and the towers matter relatively more — measure there.
* Memory: v3 adds nothing measurable over exp-bioclip (RSS after load 1.69–1.74 GB; text tower is
  never loaded during analysis). Peak RSS is dominated by decode: each buffered 45 MP image holds a
  float32 linear copy (~540 MB) + the metered frame, and `parallel_prefetch=3` buffers up to 4 of them
  (7.45 GB peak even on a 4-image folder). That, not BioCLIP/CLIP, is the likely source of the
  ~12 GB Sanjay saw — suggested follow-up: keep `noauto_linear` as float16 (halves it) or lower the
  default prefetch on <16 GB machines.
* exp-semantic-search's 43 s model load is most likely a cold disk cache (that checkout's model
  files had not been read yet; the others had); its +2.4 s "other" was not investigated further
  (the CLIP code is gone in v3).

## Known gaps

* **Gate threshold** set from 114 real false positives (exp 4) on box-square crops with fp32
  embeddings; re-check on real v3 runs (`animal_score` in `crops_json`).
* **Cloud Compute parity** out of scope: the Modal pipeline still produces 2.x packs. Merging them
  works (tested by `integration/test_pack_merge.py`); merged rows have no SigLIP score/embeddings
  and the pack's metadata replaces `pipeline_major`, so the folder shows "Upgrade" again — run it.
* CPU/GPU overlap between the CPU towers and GPU detector was not attempted (the pipeline is
  single-threaded per image apart from decode prefetch).
* Upgrade progress is shown in the smart-search bar (visible during an upgrade even with smart
  search off) — no dedicated progress dialog. The UI could save stale rows if the user edits the
  folder *during* an upgrade; the grid reloads when the upgrade finishes.
* SigLIP quality calibration knots were refitted on the int8 tower (all 10,461 reference crops):
  the fp32 knots would have moved 5% of images by one star.
* **Decode prefetch memory** (the ~12 GB): pre-existing on `main`, not changed here; see Benchmark.
* GPU placement and speed on DirectML/CoreML are untested here (no GPU) — the defaults are educated
  guesses; please check the placement log.
* `window.confirm` for the upgrade confirmation; no styled dialog yet.
* Gemma tokenizer licence TODO carried over in `models/siglip/NOTICES.md`.
* The main folder tree marks every pre-3.0 folder "outdated" (version 3.0.0 > 2.0.x) — intended
  nudge, but noisy.

## How to test locally (Windows / macOS)

```bash
git fetch && git checkout exp/pipeline-v3 && git lfs pull      # SigLIP + BioCLIP assets are LFS
pip install -r requirements-windows.txt   # or requirements-macos.txt (no new dependencies)
pytest analyzer/tests -m "not ui"          # full lane: 1,780+ tests (≈6 min on CPU)
```

1. **Analyse a new folder** — app: *Analyze Folders* as usual (Use GPU on). CLI:
   `python analyzer/cli.py "D:\photos\outing" --gpu`. Check `.kestrel/kestrel_metadata.json` has
   `"pipeline_major": 3`, the CSV has `quality_siglip` / `quality_legacy`, and
   `.kestrel/embeddings/siglip2-b16{,-frame}/` exist.
2. **Upgrade an existing 2.x folder** — open it, *Folder Actions → Upgrade to Analysis Pipeline
   3.0…* (or turn on ✨ Smart search and click *Upgrade folder for smart search*). CLI:
   `python analyzer/cli.py "D:\photos\old" --upgrade-v3 --gpu`. Verify culls / star ratings / scene
   names / edited tags are unchanged, `OLD_kestrel_database_pre-v3_*.csv` exists, species may change
   only in scenes you never edited. A 1.x folder should say "re-analyse".
3. **Toggle the quality scorer** — *Settings → Quality scorer*: SigLIP ↔ Legacy. The open folders
   reload; star ratings/order follow the chosen score; no analysis runs. Switch back and compare.
4. **Smart search** — click ✨ *Smart search*, then try: `bird in flight`, `perched on a wire`,
   `two birds`, `blurry`, `dove pair`, `heron in a marsh`, `great blue herons fishing`,
   `yelow warbler` (typo), `robin eating` (if the folder has a robin), a wildlife name. Check the
   chips (× to un-chip), the best-match card thumbnail + `n/m` badge, the outlined matches in the
   scene filmstrip, and the empty-state examples.
5. **GPU placement log** — run with `KESTREL_LOG_LEVEL=DEBUG` (console) or open
   `.kestrel/kestrel_error_*.jsonl` and find `models_loaded` / `analysis_complete` → `placement`:
   one record per session with `requested`, `actual` and `nodes` (e.g.
   `{"DmlExecutionProvider": 412, "CPUExecutionProvider": 6}`). Many CPU nodes next to a GPU EP for
   `bird_species` / `siglip_image` means a split graph; compare speed with
   `python tools/bench_pipeline.py <folder> --gpu` under `KESTREL_TOWER_DEVICE=cpu` vs `=gpu`
   (and `--compare` the JSONs). Defaults: towers on GPU with DirectML, CPU with CoreML.
6. **Old vs new benchmark on your machine** — check out an old build somewhere (`git worktree add
   ../pk-main main && git -C ../pk-main lfs pull`), then
   `python tools/bench_pipeline.py <folder> --gpu --repo ../pk-main --json old.json`,
   `python tools/bench_pipeline.py <folder> --gpu --json new.json`,
   `python tools/bench_pipeline.py --compare old.json new.json`.
