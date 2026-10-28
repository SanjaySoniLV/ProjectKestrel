# Primary-subject tracking experiment

Link the same bird across the frames of a scene, so a user can pick one bird ("my subject")
and have Kestrel rank the scene's frames by **that bird's** quality instead of the best crop
in each frame. Only data the pipeline already stores is used: the crop boxes and qualities in
`crops_json` and the per-crop BioCLIP 2 / SigLIP 2 embeddings in `.kestrel/embeddings/`. No
model runs and nothing is added to the pipeline.

Nothing here is imported by the app. Evaluation data (photos, labels) is not kept in this repo.

## Files

| File | What it is |
|---|---|
| `tracker.py` | The algorithm: motion estimate, link cost, assignment with "no match", multi-target tracking, `follow_subject`, `rank_by_subject`. Plain numpy (own Hungarian solver, checked against scipy on 300 random matrices). |
| `run_local.py` | Runs it on an analysed folder: prints tracks per multi-bird scene, follows a chosen bird (`--seed FILE:CROP`), and draws per-scene sheets where the same number marks the same bird (`--sheets DIR`). |

```bash
python experiments/primary_subject/run_local.py "/path/to/photos" --sheets /tmp/tracks
python experiments/primary_subject/run_local.py "/path/to/photos" --seed IMG_1234.CR3:2
```

The folder must have been analysed with pipeline 3.0 (or upgraded), so the embeddings exist.
`run_local.py` assumes embedding row `k` of an image is crop `k` of its `crops_json` (that is
how the pipeline writes them today).

## Algorithm

1. **Camera motion** between consecutive frames, modelled as zoom + shift with no rotation. Every
   candidate crop pair proposes a motion; the proposal that lines up the most other crops wins,
   with each aligned crop weighted by its SigLIP similarity. Geometry alone is ambiguous with
   one or two birds: many proposals tie, and picking arbitrarily made results *worse* than no
   motion model at all.
2. **Link cost** `= min(pos, 5) + 10 * (0.5 * (1 - cos_BioCLIP) + 1 * (1 - cos_SigLIP))`, where
   `pos` is the motion-compensated centre distance, measured in target-box sizes, plus half the
   log size ratio.
3. **Assignment**: Hungarian over the cost matrix padded with "no match" at `tau = 4.5`, so
   birds can leave or enter the frame.
4. **Tracking**: every bird in the first frame is a track with an averaged appearance template
   and its last box. All tracks compete for each new frame's crops together, and a missed
   frame can be re-acquired later. `follow_subject` runs this forward and backward from
   whichever frame the user picks the bird in.
5. **Confidence**: the margin between the chosen cost and the next-best option. Ranking puts
   confident matches first, then uncertain ones, then "not found".

## Evaluation (October 2026)

**Data.** Multi-bird scenes (3–10 frames) from the maintainer's own public and unlisted Perch
uploads. Each crop was re-embedded with the shipped int8 BioCLIP 2 / SigLIP 2 towers.

**Labels.**
- 232 consecutive frame pairs were hand-labelled: which box is the same individual in the
  next frame.
- 172 of those were an ordinary sample. The other 60 were chosen from new scenes because the
  candidate methods disagreed on them.
- A review pass removed frames that weren't one burst.
- **On hard pairs the labels themselves are noisy.** A second, independent labelling of 16
  low-confidence pairs agreed on only 75% of links. Treat accuracies on hard pairs below about
  0.75 as unmeasurable, not as failures.

**Baseline.** Matching boxes by detection index, i.e. assuming the detector lists the birds in
the same order every frame, is right 71% of the time on ordinary pairs and 33% on hard ones.

| Method | Ordinary pairs (all / low-confidence) | Hard pairs (high-confidence labels) | Synthetic pan ±15 %, zoom 0.8–1.25, 20 % dropped boxes |
|---|---|---|---|
| detection index | 0.69 / 0.34 | 0.33 | 0.69 |
| position only | 0.93 / 0.76 | 0.60 | 0.88–0.93 |
| position + motion (geometry only) | 0.94 / 0.79 | — | 0.86–0.89 |
| position + motion (appearance-weighted) | — | 0.89 | 0.92–0.97 |
| BioCLIP only | 0.88 / 0.67 | 0.85 | — |
| SigLIP only | 0.91 / 0.74 | 0.72 | 0.96 |
| position + SigLIP | 0.95 / 0.84 | 0.78 | 0.95–0.97 |
| **position + motion (appearance) + BioCLIP + SigLIP** | — | **0.875** | **0.96–0.98** |

**Following one bird through a whole scene.** 126 labelled tracks, 406 frame placements:

| Tracker | Frames right | Last frame right |
|---|---|---|
| Multi-target tracker, final configuration | **97.0 %** | 96.8 % |
| Seeded from the last frame instead of the first | 95.8 % | |
| Matching every frame directly to the seed frame | 94.8 % | |

All remaining errors were in scenes the labellers also marked low-confidence: tiny birds
against sky, dense overlapping flocks, and identical birds swapping perches.

**Confidence margin** (403 boxes with high-confidence labels):

| Margin | Accuracy |
|---|---|
| < 0.5 | 0.875 |
| 0.5–1 | 0.931 |
| 1–2 | 0.963 |
| 2–4 | 0.994 |
| > 4 | 1.000 |

Answering only when the margin is at least 1 covers 89 % of boxes at 98.6 % accuracy.

**Does it matter?** For 47 % of tracked birds, the best frame *for that bird* is not the frame
ranked best today (today's ranking uses the best crop of each frame).

## What worked, what didn't

- **Position is the strongest single signal** within a burst. Detection order is not reliable.
- **SigLIP beats BioCLIP for identity** when birds are the same species. BioCLIP mostly encodes
  species, so it can't separate two doves. It does help on mixed-species flocks.
- **Motion compensation needs appearance.** A geometric RANSAC-style estimate made things worse
  with one or two birds, because many hypotheses tie. Weighting hypotheses by SigLIP agreement
  fixed that and is what makes re-framed bursts work (0.60 → 0.89 on hard pairs).
- **Tracking all birds jointly** beats chaining pairwise matches or matching each frame to the
  seed. The averaged template absorbs pose change.
- **Not solved:** dense flocks of small, identical birds. Humans can't label those reliably
  either. The margin flags them, so the UI should say "not sure" rather than guess.

## In-app prototype

`analyzer/kestrel_analyzer/primary_subject.py` is `tracker.py` made importable by the app, plus a
per-folder embedding cache. It reproduces the numbers above (97.0 % from the first frame, 95.8 % from
the last). It also adds `label_birds`, which gives every bird in the scene a number, including birds
that first appear in a later frame.

- **Bridge.** `Api.primary_subject_scene(folder, frames, seed)` takes the scene's frames in capture
  order (crop index, bbox and quality per crop) and an optional seed `{frame, crop}`. It returns bird
  numbers per crop and, when a seed is given, the followed bird's crop and margin in every frame. It
  only reads `.kestrel/embeddings/`.
- **UI** (`analyzer/js/primary-subject.js`, hooked into the scene dialog). In a scene where some frame
  has several birds, a bar under the info bar offers these controls:
  - **B** shows bird numbers on the full image.
  - **F** follows the active crop's bird (pick it with ↑/↓). Each frame's crop switches to that bird,
    and the filmstrip sorts by its quality: sure matches first, then unsure (margin < 1), then not
    found. Badges show which is which.
  - **Use Bird N as primary crop** promotes the sure matches through the normal path, so ratings,
    sorting and saving follow. **Stop following** undoes the temporary view.
- Nothing is persisted except the promoted primary crops. No analysis runs, so a folder analysed with
  pipeline 3.0, or upgraded to it, works as soon as it is loaded. Without embeddings, linking falls
  back to position only and the bar says so.

Tests: `analyzer/tests/unit/test_primary_subject.py`.
