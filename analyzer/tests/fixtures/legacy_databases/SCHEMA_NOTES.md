# Database Schema Evolution Notes

## Status: BLOCKED — Git LFS unavailable in cloud agent environment

This task was attempted but could not be completed because every ML model
file in this repository is a Git LFS pointer (~132-byte text file), and the
LFS server in this sandbox returned HTTP 502 on every fetch attempt. Without
real model weights the pipeline cannot run at any tag, so no authentic
fixtures could be captured.

## What was attempted

1. Installed Python deps for the modern pipeline path: `onnxruntime`,
   `opencv-python`, `pillow`, `rawpy`, `PyExifTool`, `pandas`, `Wand`, and
   the `libimage-exiftool-perl` / `libmagickwand-dev` system packages.
2. Created a git worktree at the `Gambels-Quail` tag (most modern, ONNX-only
   pipeline — no PyTorch/TensorFlow needed).
3. Staged 4 CR3s from `analyzer/tests/fixtures/test_sets/set_a_fresh/` into
   a scratch dir at `/tmp/dbrun_gq`.
4. Ran `python analyzer/cli.py /tmp/dbrun_gq --no-gpu --parallel-prefetch 1`.
   The pipeline loaded the detector successfully but failed on
   `quality.onnx`:

   ```
   [QualityClassifier] Failed with preferred providers
   ([ONNXRuntimeError] : 7 : INVALID_PROTOBUF : Load model from
   /tmp/kestrel_tag_wt/analyzer/models/quality.onnx failed:Protobuf
   parsing failed.), falling back to CPU
   Fatal error: [ONNXRuntimeError] : 7 : INVALID_PROTOBUF : ...
   ```

5. Inspection confirmed `quality.onnx` was an LFS pointer:

   ```
   $ file analyzer/models/quality.onnx
   analyzer/models/quality.onnx: ASCII text
   $ head -c 200 analyzer/models/quality.onnx
   version https://git-lfs.github.com/spec/v1
   oid sha256:6c6ce14db9ab6ee0fbd4de5e038a240e60547f04d9618a8d973e752c106731c1
   size 1181691
   ```

6. `git lfs install && git lfs pull` failed against the sandbox's LFS proxy:

   ```
   batch response: Fatal error: Server error ... from HTTP 502
   Failed to fetch some objects from
   'http://local_proxy@127.0.0.1:37455/git/SanjaySoniLV/ProjectKestrel.git/info/lfs'
   ```

   Retried twice with the same result. Every `.onnx` and `.pth` under
   `analyzer/models/` is a 132-133 byte LFS pointer.

7. The older-tag pipelines (`alpha-2026.02.04` etc.) additionally need
   `torch==2.6.0`, `torchvision==0.21.0`, and `tensorflow==2.19.0` (per that
   tag's `requirements.txt`) for Mask R-CNN. Those weren't installed in this
   environment because the LFS blocker upstream made it pointless.

## To pick this up in a working environment

1. Ensure `git lfs install && git lfs pull` succeeds and that
   `file analyzer/models/quality.onnx` reports a binary file (not ASCII text).
2. Set up Python 3.11 with the deps already installed in this sandbox
   (`onnxruntime`, `opencv-python`, `pillow`, `rawpy`, `PyExifTool`, `pandas`,
   `Wand`, `numpy`, plus `libimage-exiftool-perl` and `libmagickwand-dev`).
   For pre-Gambels-Quail tags, also install `torch==2.6.0`, `torchvision==0.21.0`,
   and `tensorflow==2.19.0` per each tag's own `requirements.txt`.
3. Follow `CLOUD_AGENT_INSTRUCTIONS.md` from Step 2 onward. The 17 tags
   present in this repo (in chronological order) are:

   ```
   2026-02-04 alpha-2026.02.04
   2026-02-04 Public-Release-1
   2026-02-07 Sparrow
   2026-02-11 test
   2026-02-12 sparrow_2026.02.12
   2026-02-20 Finch
   2026-02-24 Junco
   2026-03-01 Goldfinch
   2026-03-04 Chickadee
   2026-03-08 Tufted-Titmouse
   2026-03-10 Swamp-Sparrow
   2026-03-10 Yellow-Warbler
   2026-03-15 Lincolns-Sparrow
   2026-03-17 Willow-Ptarmigan
   2026-03-19 Willow-Ptarmigan-F1
   2026-04-02 Kentucky-Warbler
   2026-04-23 Gambels-Quail
   ```

   The `public-release-alpha-1-(R2024.02.04)` tag listed in
   `CLOUD_AGENT_INSTRUCTIONS.md` is not present in this clone.

4. Once fixtures are generated, fill in the tables below.

## Tag → Schema Mapping (TO BE COMPLETED)

| Tag | Date | Fixture File | Unique Schema? | Notes |
|-----|------|--------------|----------------|-------|
| _Pending — see status above_ | | | | |

## Schema Differences From Current (TO BE COMPLETED)

_Pending fixture generation in a working LFS environment._

## Failed Tags

| Tag | Failure Reason |
|-----|----------------|
| All 17 tags | LFS server returned HTTP 502 — could not fetch ONNX/PyTorch model weights. Pipeline aborts on `quality.onnx` load. |

## How tests use these fixtures

`analyzer/tests/compat/test_database_migration.py` parametrizes across every
fixture matching `v_*_kestrel_database.csv` in this directory. With no
fixtures present, the parametrized class is skipped via
`pytest.mark.skipif`, exactly as it was before this branch.
