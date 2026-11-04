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
3. **Assignment**: Hungarian over the cost matrix padded with "no match" at `tau = 2.5` (was 4.5; see below), so
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

## Re-evaluation on full, uncull'd bursts (October 2026)

The first data set came from culled uploads, so its bursts were short (3–5 frames) and gaps were
large. A second upload was not culled: 55 multi-bird bursts, 432 frames, up to 31 frames long,
including dense skimmer flocks of up to 10 crops per frame. 70 frame pairs were labelled (60 where
methods disagreed, 25 of them skimmer pairs, plus 10 where they agreed). That gives 512 bird links,
260 of them with high-confidence labels.

**New ideas tried, each following a bird seeded in the top frame with the whole burst available:**

| Method | All | High-confidence | Skimmers |
|---|---|---|---|
| Previous configuration (tau 4.5) | 0.738 | 0.912 | 0.672 |
| BioCLIP weight 1.0 | 0.752 | 0.919 | 0.695 |
| Adaptive weights (an embedding counts more when it separates the frame's birds) | 0.732 | 0.915 | 0.667 |
| Adaptive + BioCLIP 1.0 | 0.746 | 0.919 | 0.695 |
| Constant-velocity box prediction | 0.738 | 0.912 | 0.672 |
| Viterbi best path over the whole burst | 0.732 | 0.912 | 0.684 |
| Viterbi + velocity | 0.725 | 0.904 | 0.701 |
| **Previous configuration, tau 2.5** | **0.795** | **0.965** | 0.718 |
| Previous configuration, tau 2.0 | 0.797 | 0.969 | 0.741 |
| BioCLIP 1.0, tau 3.0 | 0.795 | 0.958 | 0.736 |

**Why tau.** The error breakdown showed what was going wrong. In 98 labelled links the bird has no
box in the next frame: the pipeline crops at most N birds, so in a dense flock a different subset gets
boxed each frame. The old tracker linked 81 of those to a lookalike. A stricter "no match" (tau 2.5)
fixes most of them.

**What didn't help.** Velocity, Viterbi and adaptive weighting changed little. Capture times are only
second-resolution, and appearance does not separate identical flock birds whichever way it is
weighted. BioCLIP 1.0 helps on skimmers but costs 1.5 points on the first data set when the bird is
picked in the last frame.

**Check on the first data set.** tau 2.5 scores 96.8 % from the first frame and 95.6 % from the last,
against 97.0 % and 95.8 % before, so it is essentially unchanged.

**Confidence.** At tau 2.5 on the new data, links with margin ≥ 1 are 92 % right and "unsure" links
63 %. In dense flocks only about half the links are "sure", which is the honest answer there.

## In-app prototype

`analyzer/kestrel_analyzer/primary_subject.py` is `tracker.py` made importable by the app. It numbers
every bird in each scene, so the same number is the same bird in every frame, and stores the result in
`.kestrel/bird_links.json`:

```
{version, params, scenes: {scene_count: {files, ids: {file: {crop_index: bird}}, margins, crops}}}
```

- **When it is computed.** At the end of analysis, at the end of the pipeline-3 upgrade, and in the
  background on folder load (`read_kestrel_csv`) when the file is missing or stale. No pipeline
  version bump is needed: a change to the matcher's parameters changes `params` and invalidates the
  file.
- **Surviving culls.** A scene whose remaining frames are all already stored keeps its links, so
  deleting frames later doesn't change the numbers.
- **Bridge.** `Api.primary_subject_links(folder)` returns the stored links, computing them first if
  needed.
- **UI** (`analyzer/js/primary-subject.js`, hooked into the scene dialog). There are no new panels:
  - Crop overlays label boxes "Bird N", or "Bird N?" when the link into that frame is unsure
    (margin < 1).
  - The active box adds a hint: "Shift+Click or press F to focus this scene on this bird".
  - Focusing makes that bird the primary crop in every photo of the scene where it was found,
    through the normal promote/save path. The filmstrip re-sorts by its quality.
  - F on the same bird undoes the focus and restores the previous primary crops.
  - Filmstrip badges mark unsure photos and photos where the bird wasn't found.
  - **B** keeps the bird numbers on screen.

Tests: `analyzer/tests/unit/test_primary_subject.py`.
