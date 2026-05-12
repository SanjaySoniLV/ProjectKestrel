# Colab handoff — Project Kestrel quality-model retrain

You are the agent running inside a Google Colab notebook. This document is your briefing. The other half of the work — modifying the desktop analyzer to produce training data — has already shipped. Your job starts where that hands off.

---

## 1. What we're doing and why

**Project Kestrel** is a desktop wildlife-photography app (Windows + macOS). One of its ML components is a *quality classifier*: it scores how sharp/usable each detected bird is. The current production model is `analyzer/models/quality.onnx`.

That model was trained ~mid-2025 against a pipeline that used **Mask R-CNN** for bird segmentation. The pipeline has since been replaced with **SpeciesNet + SAM-HQ ViT-Tiny**. Different segmenter → different masks → different square crops → different Sobel inputs. The existing model is being fed data it wasn't trained on, and quality scores have drifted accordingly.

**The retrain.** We've added a `--export-training-data` flag to the analyzer that produces a Colab-ready dataset by running the *current production pipeline* over every sample image and dumping the exact tensors the quality model would see at inference. You will train a new quality model on those tensors.

The plan has five phases. **Phase 1 (analyzer modifications) is done and committed.** You are involved from Phase 2 onward:

| Phase | What | Where |
|---|---|---|
| 1 | Add export hook to the analyzer | ✅ Shipped in this repo |
| 2 | Run export on `C:\Data\Resnet Testing\birds\` (2834 RAWs) + 50 new scene-test-sets, upload to shared drive | User runs locally, then uploads |
| 3 | Binary classifier training in Colab (replaces current `quality.onnx`) | **You** |
| 4 | Ranked-loss fine-tune on scene sets | **You**, after Phase 3 |
| 5 | Export to ONNX, drop into `analyzer/models/quality.onnx`, validate | User |

---

## 2. What you'll receive on the shared drive

The user is running this command locally on each source folder (GPU + max parallel RAW prefetch for throughput, debug logging on so any per-image failure surfaces in the log):

```powershell
$env:KESTREL_LOG_LEVEL = "DEBUG"
python analyzer/cli.py "<source_folder>" `
  --gpu `
  --parallel-prefetch 5 `
  --export-training-data "<shared_drive_path>/v3_export"
```

After all runs complete, you'll see a single flat directory on the shared drive containing:

```
v3_export/
├── manifest.csv                              ← one row per crop, all metadata
├── birds__IMG_8411_crop_0_input.npy          ← (1024,1024,1) float32, ready to feed the model
├── birds__IMG_8411_crop_0_rgb.jpg            ← 1024×1024 RGB crop, jpg quality 95
├── birds__IMG_8411_crop_0_mask.png           ← 1024×1024 binary mask (0/255)
├── birds__IMG_8412_crop_0_input.npy
├── birds__IMG_8412_crop_0_rgb.jpg
├── birds__IMG_8412_crop_0_mask.png
├── scene_001__DSC_0042_crop_0_input.npy      ← from scene-test-sets, same naming pattern
...
```

**`crop_id` naming**: `{source_folder_basename}__{file_stem}_crop_{detection_idx}`. This is flat across all source folders so a single Drive directory holds everything.

### `manifest.csv` columns

```
crop_id, source_folder, source_filename, crop_index, detection_index,
detection_confidence, species, species_confidence, family, family_confidence,
quality_legacy_model, rating_legacy_model, exposure_correction, exposure_pipeline,
exposure_subject_stops, exposure_meter_scale, bbox_x_min, bbox_x_max,
bbox_y_min, bbox_y_max, capture_time, orientation,
input_npy_path, rgb_jpg_path, mask_png_path
```

Notes:
- `quality_legacy_model` is the **current production model's prediction on the new pipeline's crops**. Useful for distribution analysis and percentile-curve regeneration (Phase 5), but **not a training target** — it's what we're replacing.
- `rating_legacy_model` is derived from `quality_legacy_model` via threshold buckets. Same caveat.
- `capture_time` is ISO 8601 from EXIF (RAW files always have it; some Fujifilm / Sigma RAWs may not).
- `bbox_*` is in *original full-image pixel coordinates*, not crop coordinates.
- `exposure_pipeline` is either `"numpy_linear_v2"` (RAW path, full per-bird exposure compensation) or `"legacy_auto_bright_v1"` (JPEG input — for retraining you can probably ignore JPEG-sourced rows since the production pipeline applies less exposure correction to them).

---

## 3. The critical invariant: training/inference parity

The `_input.npy` files are produced by calling **the exact same function** the production model sees at inference time: `kestrel_analyzer.ml.quality.QualityClassifier._preprocess(rgb_crop, mask)`. That function does:

```python
gray   = cv2.cvtColor(rgb_crop, cv2.COLOR_RGB2GRAY)        # uint8 → uint8
sx     = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=5)        # → float32
sy     = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=5)        # → float32
mag    = np.sqrt(sx**2 + sy**2)                            # edge magnitude
masked = cv2.bitwise_and(mag, mag, mask=mask.astype(uint8))
out    = masked[..., None]                                 # → (1024,1024,1) float32
```

A unit test ([`analyzer/tests/unit/test_training_export.py`](analyzer/tests/unit/test_training_export.py)) asserts the dumped `.npy` is **bit-identical** to that function's output. So:

- Feed `.npy` files straight into `model.fit(...)` — no preprocessing needed.
- Do **not** re-normalize, re-scale, or convert dtype. The values are unbounded floats (Sobel magnitudes can be hundreds), and that's what production sees.
- If you want to add augmentation (exposure jitter, rotation, etc.), do it on the **RGB + mask pair** in `_rgb.jpg` + `_mask.png` and re-run `QualityClassifier._preprocess` — don't augment the `.npy` directly, because Sobel + mask doesn't commute cleanly with rotations.

---

## 4. Labels

### Binary labels (Phase 3 — train from scratch)

`C:\Data\Resnet Testing\retrain\trainingdata_v2.csv` (on the user's local machine — user will upload to the shared drive).

Schema:
```
filename, label, raw_file, exported_file, square_crop, square_mask
IMG_8411, 1, birds\IMG_8411.CR2, ...
```

`label ∈ {0, 1}` — bad / good. Covers ~2,051 of the ~2,834 birds; the rest are unlabeled and should be excluded from Phase 3 training (or used only for unsupervised analysis).

**Join key**: `manifest.csv.source_filename` → strip extension → match against `trainingdata_v2.csv.filename`. Example: a manifest row with `source_filename = "IMG_8411.CR3"` and `source_folder = "birds"` joins to the labels row with `filename = "IMG_8411"`. The crop with the highest `detection_confidence` per source image is the canonical training sample; secondary crops on the same image are optional extras.

### Scene-set labels (Phase 4 — fine-tune with ranked loss)

For the 50 scene-test-sets, the user will provide per-image ratings produced by:
- **Stage 1 (initial)**: Kestrel UI star ratings, exported from each scene folder's `.kestrel/kestrel_scenedata.json` under `image_ratings: {filename: 1..5}`. Coarse but free.
- **Stage 2 (later)**: A new ProjectKestrel UI (planned, not yet built) for explicit within-scene reordering and cross-scene pairwise comparisons. Richer ranking labels than the 5-bucket UI allows.

For Phase 4 you'll consume whichever is available at the time. Both will be uploaded to the shared drive as a `scene_labels.csv` or `scene_labels.jsonl` file — schema TBD when we get there.

---

## 5. Model architecture (legacy reference)

This is what produced the current `quality.onnx`. The user will upload `C:\Data\Resnet Testing\retrain\train_quality_model.py` to the shared drive for you to read; the relevant arch lives in lines 192–219. Summary:

```
Input (1024, 1024, 1)
  → Conv2D(32, 5×5, relu, same) → AveragePool(2×2)
  → Conv2D(64, 3×3, relu, same) → AveragePool(4×4)
  → Conv2D(128, 3×3, relu, same) → AveragePool(4×4)
  → Conv2D(64, 3×3, relu, same) → MaxPool(4×4)
  → Conv2D(32, 3×3, relu, same) → MaxPool(2×2)
  → Flatten
  → Dense(256, relu) → Dense(128, relu) → Dense(128, relu)
                    → Dense(128, relu) → Dense(64, relu)
  → Dense(1, sigmoid)

loss      : binary_crossentropy
optimizer : Adam(lr=0.001)
batch     : 4
epochs    : 28 per stage, 3 stages
```

**For Phase 3 baseline**, start by replicating this architecture verbatim. Once you have a working baseline that roughly matches the legacy model's val accuracy on `trainingdata_v2.csv` labels, propose improvements (regularization, modern blocks, etc.) — but the goal of Phase 3 is *parity on the new pipeline's data*, not architecture innovation.

**Legacy training tricks worth keeping**:
- The 3-stage curriculum (base → mixed → augmented).
- Spatial augmentation in stage 3: `tf.image.rot90` with random k, `tf.image.translate` ±128 px.

**Legacy tricks that are now obsolete**:
- The base/pos/neg ±0.25 EV exposure variants on disk. The new pipeline applies per-bird exposure compensation upstream, so each crop is already metered correctly. If you want EV invariance, apply random ±0.25 EV at train time on the RGB crop before re-running `QualityClassifier._preprocess`.

---

## 6. Where to write your outputs

The user wants **everything from your side** under the same shared-drive root as the data. Concretely, please use this layout (create whatever subfolders you need):

```
<shared_drive_root>/
├── v3_export/                  ← input data (already there, from Phase 2)
│   ├── manifest.csv
│   └── *.npy / *.jpg / *.png
├── v3_train/                   ← your outputs (you create this)
│   ├── notebooks/              ← .ipynb files
│   ├── checkpoints/            ← per-epoch Keras checkpoints
│   ├── final/
│   │   ├── quality_v3_binary.keras       ← end of Phase 3
│   │   ├── quality_v3_ranked.keras       ← end of Phase 4
│   │   └── quality_v3_ranked.onnx        ← Phase 5 input
│   ├── logs/                   ← training_log.csv + TensorBoard / W&B exports
│   └── analysis/               ← distribution plots, percentile curves, eval reports
```

Confirm the exact `<shared_drive_root>` path with the user when you start — it's mounted via `drive.mount("/content/drive")`. Don't hardcode paths until the user has confirmed.

Save checkpoints **every epoch**, not just at the end — Colab sessions die at ~12 hours and the user may pause/resume. The legacy training did `quality_classifier_stage{1,2,3}_epoch_{NN}.keras` which is fine.

---

## 7. Repo orientation (for context, not for you to modify)

You don't need to clone or modify the ProjectKestrel repo from inside Colab — your job is data + training. But for reference:

- The Phase 1 implementation lives in: [`analyzer/kestrel_analyzer/training_export.py`](analyzer/kestrel_analyzer/training_export.py) and the corresponding hook in [`analyzer/kestrel_analyzer/pipeline.py`](analyzer/kestrel_analyzer/pipeline.py) inside `process_folder`'s `write_crop` stage.
- The preprocessing function you must match is [`analyzer/kestrel_analyzer/ml/quality.py`](analyzer/kestrel_analyzer/ml/quality.py) `QualityClassifier._preprocess` (lines 82–90). If you ever need to re-derive a `.npy` from an `_rgb.jpg` + `_mask.png` pair, this is the canonical source.
- The original Phase-1 plan: [`C:\Users\Sanja\.claude\plans\we-re-going-to-prepare-humble-fog.md`](../../Users/Sanja/.claude/plans/we-re-going-to-prepare-humble-fog.md) (user-local, ignore if not provided).
- The legacy training repo at `C:\Data\Resnet Testing\retrain\` (`run_all.py`, `train_quality_model.py`, `preprocess_training_data.py`) is user-local — the user will upload the relevant files to the shared drive.

---

## 8. What to do first when you boot

1. Mount Drive: `from google.colab import drive; drive.mount("/content/drive")`.
2. Ask the user for the **exact shared-drive root path** and verify it exists.
3. Read `<root>/v3_export/manifest.csv`. Confirm row count (~2834 + scene-set crops) and that the referenced `.npy` files exist.
4. Spot-check **one** sample: load the `.npy`, assert shape `(1024,1024,1)` and dtype `float32`, then load the sibling `_rgb.jpg` + `_mask.png`, run `QualityClassifier._preprocess`-equivalent code (or paste the function from `quality.py`), and assert the result is identical to the `.npy`. This guards against upload corruption.
5. Read `trainingdata_v2.csv` (once the user uploads it), join to the manifest by source filename stem, and report: how many manifest rows have labels, how many don't, label balance (0 vs 1 count).
6. **Stop and check in with the user** before writing any training code. The plan from there is Phase 3 → Phase 4, but the user will decide the exact starting point based on what data has actually been uploaded.

Be terse, confirm shared-drive paths before reading or writing, and don't assume artifacts exist — the export runs locally on a Windows machine and any subset of folders may be uploaded at any time.
