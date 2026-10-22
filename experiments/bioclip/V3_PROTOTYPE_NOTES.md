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
   3.0…* (or turn on ✨ Smart search and click *Upgrade folder*). CLI:
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

## GPU-friendly tower variants

`tools/ep_probe.py --variants ...` builds rewritten copies of the two int8 image towers
(`make_variant`, needs `onnx`; nothing ships). Findings from the CPU tests (20 real crops,
app preprocessing, one image per run, ORT 1.23.2, 4 cores; variants were built with ORT 1.31
and also load and run on 1.23.2):

| model | variant | MB | cos vs dyn (mean/min) | cos vs fp32 torch (mean/min) | CPU ms |
|---|---|---|---|---|---|
| BioCLIP | dyn (shipped) | 309 | 1 | 0.9933 / 0.974 | 169 |
| BioCLIP | sdyn (static, int8) | 308 | 0.998 / 0.990 | 0.9932 / 0.972 | 166 |
| BioCLIP | wdq / swdq | 309 / 308 | 0.9946 / 0.974 | 0.9985 / 0.994 | 719 / 786 |
| BioCLIP | f32 / sf32 | 1217 / 1216 | 0.9946 / 0.974 | 0.9985 / 0.994 | 497 / 661 |
| BioCLIP | sf16 | 608 | 0.9946 / 0.974 | 0.9985 / 0.994 | 587 |
| BioCLIP | f16 (dynamic batch) | 609 | segfaults in ORT at ENABLE_ALL (fine at BASIC) | | |
| SigLIP | dyn (shipped) | 97 | 1 | 0.9927 / 0.983 | 50 |
| SigLIP | sdyn | 103 | 0.9957 / 0.988 | 0.9933 / 0.986 | 44 |
| SigLIP | wdq / swdq | 97 / 103 | 0.9929 / 0.983 | 0.9996 / 0.999 | 412 / 406 |
| SigLIP | f32 / sf32 | 372 / 369 | 0.9929 / 0.983 | 0.9996 / 0.999 | 139 / 121 |
| SigLIP | f16 / sf16 | 186 / 185 | 0.9929 / 0.983 | 0.9996 / 0.999 | 131 / 169 |

All variants pass `onnx.checker` and load under ORT 1.23.2. CPU latency is irrelevant to GPU
speed (CPU has no fast fp16/DQ path); it is only a sanity check.

- **The shipped dyn graphs are the noisy ones.** Dropping activation quantisation (wdq/f32/f16)
  moves the embedding *closer* to the fp32 torch model (0.9985 / 0.9996 vs 0.993), so cos vs
  dyn of 0.993-0.995 is dyn's own error, not the rewrite's (wdq vs f32 on ORT 1.23.2: 0.9999999).
  fp16 compute costs nothing measurable (sf16 = sf32 to 4 digits; CPU emulation, so confirm on
  a real GPU).
- **Dynamic batch is the DirectML problem, not the int8 ops.** The 847 / 164 CPU nodes are the
  Shape -> Gather -> Unsqueeze -> Concat (+ Sqrt/Div/Where/Mod) plumbing for the attention scale
  and Reshape targets. Fixing batch=1 and constant-folding with ORT *basic* optimisation
  removes it (BioCLIP: Shape 196 -> 0, Unsqueeze 555 -> 0, Concat 243 -> 1, Mod 24 -> 0;
  SigLIP: Shape 42 -> 0, Unsqueeze 54 -> 0, Concat 29 -> 0). The app always runs N=1
  (`bioclip_species.py`, `siglip.py`), so a static graph is safe; N>1 would be rejected.
- **ORT bug found:** on a static graph ORT's load-time MatMulAddFusion names every shape
  initializer `gemm_input_shape`, so two different K make session creation fail ("Initializer
  with same name exists", ORT 1.23.2 and 1.31). `_fuse_matmul_add` does the same Reshape ->
  Gemm -> Reshape fusion up front with unique names. (A static *wdq/f32/f16* graph without
  this fails to load; static int8 is unaffected.)
- **Op support (ORT 1.23-1.24 docs / OperatorKernels).** CoreML MLProgram has no Gather,
  DequantizeLinear, DynamicQuantizeLinear, MatMulInteger, Where, Equal, Expand, ConstantOfShape
  or Mod; the NeuralNetwork format additionally lacks Erf, LayerNormalization, ReduceMean and
  Unsqueeze and needs a constant B for every MatMul (attention QK^T and AV are not), so use
  MLProgram. The static f32/f16 graphs use only Conv, Reshape, Transpose, Add, Mul, Div,
  LayerNormalization, Gemm (const B and C), MatMul, Softmax, Erf, Split, Squeeze, Slice (+ Cast
  in f16), all in the MLProgram list; the q/k/v and attention-pool Gathers (scalar index) are
  rewritten as Slice + Squeeze (`_gather_to_slice`). Not verifiable without a Mac:
  LayerNormalization/Softmax constraints and fp16 tensors in the CoreML EP -- that is what the
  ep-probe CI run is for. DirectML (OperatorKernels, DmlExecutionProvider) lists every op in
  all variants incl. DynamicQuantizeLinear, MatMulInteger, DequantizeLinear (int8/uint8,
  float16 scale), LayerNormalization, Erf, Gather, Where, Mod, in fp32 and fp16.
- **wdq is not a CoreML candidate:** DequantizeLinear survives ORT's optimiser (52/97 DQ nodes
  stay) and CoreML has no op for it, so each weight would be dequantised on the CPU and
  copied to the GPU per call.
- Try first: **CoreML** `sf16` (MLProgram; sf32 as reference; BioCLIP 608 MB vs 309 MB
  shipped), **DirectML** `sdyn` (same int8 bytes, only the plumbing removed), then `sf16`
  if the int8 MatMulInteger kernels are slow on the GPU.

### Shipped change (2026-10-22): static batch-1 towers

The shipped `bioclip2_image_int8.onnx` and `siglip2_b16_image_int8.onnx` are now the `sdyn`
variant: the same int8 weights with a fixed `[1,3,224,224]` input and the shape subgraph
constant-folded away (BioCLIP: Shape 196→0, Unsqueeze 555→0, Concat 243→1; SigLIP: all gone).
Sanjay's DirectML log showed those shape nodes, not the int8 ops, were what put 847 (BioCLIP) /
164 (SigLIP) nodes on the CPU with a MemcpyFromHost per transformer block. Embedding quality vs
fp32 is unchanged (0.993 BioCLIP, 0.993 SigLIP) and CPU speed is the same; the app always runs
one image per call, so the fixed batch is safe. Quality calibration knots were refitted on the new
SigLIP file (99.6% of reference crops keep their star). CoreML still needs a float variant
(`sf16` is the first candidate; see the CI probe `.github/workflows/ep-probe.yml`).

### Shipped change (2026-10-22): one set of int8 weights, a CPU graph and a DirectML graph

Measured on DirectML (Arc 140V, ORT-DML 1.24.4, maintainer): the int8 BioCLIP graph is fully
placed on DML but takes 13.3 s per crop (DML's MatMulInteger kernels); `swdq16` -- the same int8
weights behind DequantizeLinear with fp16 scales, the rest fp16 with fp32 I/O -- loaded with
`session.disable_quant_qdq=1` lets ORT constant-fold every DequantizeLinear into an fp16 weight at
load: **BioCLIP 52 ms, SigLIP 10 ms** (sf16: 36 / 9 ms; without the config entry 242 / 33 ms),
fully on DML, +0.5-0.9 s load. On CPU swdq16 is slow (BioCLIP 750-900 ms), so the CPU keeps
the int8 graph. `tools/build_gpu_towers.py` builds the layout from the towers in place
(idempotent; `--verify` checks it):

| file | MB | what |
|---|---|---|
| `bioclip/bioclip2_image_int8.onnx` | 5.1 | CPU graph, node-for-node the previous file (DynamicQuantizeLinear + MatMulInteger); fp32 biases/LayerNorm inline |
| `bioclip/bioclip2_image_wdq16.onnx` | 3.0 | DirectML graph (opset 19); fp16 scales/biases inline |
| `bioclip/bioclip2_image.data` | 303.2 | int8 weights + zero points + fp32 scales, referenced by **both** graphs (same offsets) |
| `siglip/siglip2_b16_image_int8.onnx` | 13.1 | CPU graph |
| `siglip/siglip2_b16_image_wdq16.onnx` | 6.9 | DirectML graph (Split nodes use `num_outputs`) |
| `siglip/siglip2_b16_image.data` | 89.5 | shared int8 weights |

Total 311.3 MB vs 307.9 MB (BioCLIP) and 109.5 vs 102.6 MB (SigLIP) before: +10 MB for both GPU
graphs, instead of +~400 MB for separate files. The 10 MB are the fp16 copies of the float
(non-int8) parameters; pointing the GPU graph at the fp32 ones in the `.data` through Cast
nodes would drop that to ~0 if it ever matters. ORT resolves the same `.data` from both `.onnx`
files (checked on ORT 1.23.2 and 1.31, CPU EP): the new CPU graph's output is **bit-identical**
to the previous single file, both pass `onnx.checker`, and the folded GPU session (optimised
model at BASIC level) has 0 DequantizeLinear left (97 / 51 before).

Loader (`ml/providers.py` `tower_graph`, `ml/resilient_session.py`): a tower session whose
provider list contains `DmlExecutionProvider` gets `*_wdq16.onnx` + the config entry; every other
list (CPU, CoreML) gets `*_int8.onnx`. A GPU build that throws, or a missing/LFS-stub GPU graph,
falls back to the int8 graph **on CPU** (never the int8 graph on DML). A coordinator demotion
rebuilds on CPU, i.e. with the int8 graph. The placement record carries `config` when the entry
is set. Tower device policy is unchanged: `auto` = GPU on DirectML (now the fast graph), CPU on
CoreML.

**CoreML stays on the int8 CPU graph.** The macOS CI probe (ORT 1.23.2, virtualised runner, so
placement is meaningful and timings are not): fp16 graphs are mostly *not* placed on CoreML
(SigLIP sf16 MLProgram 54 CoreML / 579 CPU nodes, BioCLIP sf16 122 / 1240); only the fp32 graph
went fully onto CoreML (SigLIP sf32 MLProgram: one partition) but compiling took ~230 s for
SigLIP and >15 min (timeout) for BioCLIP; int8 dyn splits (52/103, 122/291). So the wdq16 graph is
DirectML-only and `KESTREL_TOWER_DEVICE=gpu` on macOS still means the int8 graph on CoreML.
Follow-up for real Apple hardware: an fp32-folded graph (swdq + `disable_quant_qdq`, i.e. float32
DequantizeLinear output) with the CoreML EP option `ModelCacheDirectory` so the compile is paid
once.

Real-crop accuracy (CPU EP, ORT 1.23.2; the wdq16 graph run on CPU with the config entry, which
approximates DML's fp16 numerics; app preprocessing; fp32 = open_clip torch on the same
preprocessed tensors; crops from `ysh3fvdfme` + `s2jnsi0xj8`):

| | n | int8 vs fp32 cos (mean/min) | wdq16 vs fp32 | wdq16 vs int8 |
|---|---|---|---|---|
| BioCLIP | 40 | 0.9919 / 0.970 | **0.9988 / 0.996** | 0.9938 / 0.974 |
| SigLIP | 300 | 0.9927 / 0.932 | **0.9996 / 0.998** | 0.9931 / 0.933 |

- BioCLIP species top-1 (`classify_embedding`, global label space): int8 == wdq16 on 39/40;
  int8 == fp32 37/40, wdq16 == fp32 38/40; mean |Δ top-1 prob| int8 vs wdq16 0.047.
- SigLIP quality (`SiglipQualityScorer`, balanced profile): int8 vs wdq16 mean |Δq| **0.028**
  (max 0.21), **36/300 = 12 %** star changes, all by one star; int8 vs fp32 is the same size
  (0.026, 37/300). The raw score has no systematic shift (wdq16 − int8: mean +0.0009, sd 0.0024,
  against a population sd of 0.023; star histograms 58/125/51/40/26 vs 56/124/52/43/25), so this
  is per-crop noise of the int8 graph's dynamic activation quantisation, not a calibration
  offset: refitting the knots on wdq16 would not make the two devices agree. Above the 0.01
  bar, so it is a product decision rather than a refit: a folder analysed on a DirectML
  machine and on a CPU-only one will differ by a star on ~1 crop in 8 (the DML one is closer to
  fp32). Options: accept it; run CPU SigLIP through a float graph too (sf32 121 ms / swdq 406 ms
  per crop on CPU vs 44 ms for int8) so both devices compute ~fp32 and refit the
  knots once on that; or keep SigLIP on CPU int8 even on DML (10 ms vs 44 ms is small next to
  BioCLIP's 13 s → 52 ms). Not changed here.

How to verify on DirectML (Windows, real GPU):

```powershell
python tools/build_gpu_towers.py --out-dir $env:TEMP\towers --verify   # should be byte-identical to the shipped files
# Analyse a folder with GPU on. The models_loaded placement records (and the [providers]
# debug lines) must show bird_species / siglip_image with model bioclip2_image_wdq16.onnx /
# siglip2_b16_image_wdq16.onnx, actual=[Dml..., CPU...], config session.disable_quant_qdq=1,
# and (nearly) all nodes on Dml.
python tools/ep_probe.py --only bioclip_image,siglip_image --variants sf16   # reference numbers
```

Still to confirm on DML: the shared-data GPU graph is still ~52 ms / 10 ms per crop and loads in
<1 s extra; GPU memory (the folded fp16 weights are ~600 MB for BioCLIP + ~180 MB for SigLIP on
the device, plus ORT keeps the mmapped int8 `.data`); and that a DML device-lost demotion
rebuilds the towers on CPU with the int8 graph.

### Decision (Sanjay, 2026-10-22): SigLIP stays on the CPU int8 graph everywhere

The SigLIP crop embedding is the default quality score. On 300 real crops its DirectML (wdq16)
graph is closer to fp32 (cos 0.9996 vs 0.993) but moves 12% of crops by one star against the CPU
int8 graph — per-crop activation-quantisation noise, which a calibration refit cannot remove.
To keep star ratings identical across machines (and between analysis and upgrade), `siglip_image`
is in `providers.CPU_PINNED_KINDS`: it always runs the int8 graph on CPU (~44 ms/crop), whatever
the GPU or `tower_device` setting. BioCLIP keeps the DirectML graph (13 s -> 52 ms; species top-1
agrees on 39/40 crops). The SigLIP wdq16 graph stays in the repo for a future change of mind.
