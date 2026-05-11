# Legacy Database Fixtures — Cloud Agent Task Brief

## Goal

Generate **authentic** `kestrel_database.csv` files for each historical Kestrel release by **actually running the pipeline** at each git tag against a small test image set. These fixtures power `analyzer/tests/compat/test_database_migration.py`, which verifies the current `_perform_db_upgrade()` migration correctly handles every legacy schema we've ever shipped.

**Approach:** Don't hand-synthesize. Checkout each tag → run `cli.py` on a small set of CR3s → capture the resulting `.kestrel/kestrel_database.csv` → label it with the tag name. This produces real, exact, faithful fixtures rather than guesses about what schema versions looked like.

**Important:** The current (main branch) schema does NOT need a fixture — tests exercise it directly against live code. You're only generating fixtures for **past** schemas that differ from today. Additionally, cli.py may not have existed on earlier tags. If you checkout a tag which does not have cli.py entirely, just skip that release with a note.

---

## Background

The current schema (see `analyzer/kestrel_analyzer/database.py`):
- **`BASE_COLUMNS`** — Pipeline-written columns only.
- **`LEGACY_USER_COLUMNS`** = `['rating', 'normalized_rating', 'scene_name', 'rating_origin']` — these were inline CSV columns in older versions; now migrated to `kestrel_scenedata.json` on first load by `_perform_db_upgrade()`.

Older tags had:
- Legacy user columns (`rating`, `scene_name`, etc.) inline in CSV.
- Different `BASE_COLUMNS` lists.
- Some had no scenedata system at all.

---

## Environment Setup

The cloud agent needs:

1. **Python 3.11** matching what the project uses.
2. **Git LFS** enabled — models live in LFS:
   ```bash
   git lfs install
   git -C /repo lfs pull
   ```
3. **A working virtualenv per tag** — older tags may have older `requirements*.txt`. Setting up a fresh venv per tag is safest:
   ```bash
   python -m venv .venv-fixturegen
   .venv-fixturegen/bin/pip install -r requirements-linux.txt  # or platform equiv
   .venv-fixturegen/bin/pip install pyinstaller  # if needed
   ```
4. **A small set of test CR3 images** — copy these from main's `analyzer/tests/fixtures/test_sets/set_a_fresh/` BEFORE checking out older tags:
   ```bash
   # On main:
   cp -r analyzer/tests/fixtures/test_sets/set_a_fresh /tmp/kestrel_fixture_input
   ```

---

## Step-by-Step Procedure

### Step 1: List release tags

```bash
git -C /repo for-each-ref --sort=creatordate \
  --format='%(creatordate:short) %(refname:short)' refs/tags
```

Expected output (18 tags as of this writing):

```
2026-02-04 alpha-2026.02.04
2026-02-04 Public-Release-1
2026-02-04 public-release-alpha-1-(R2024.02.04)
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

### Step 2: Save the test image input outside the repo

Before checking out old tags (which may not have set_a_fresh/), stage the input images outside the repo:

```bash
mkdir -p /tmp/kestrel_fixture_input
cp analyzer/tests/fixtures/test_sets/set_a_fresh/*.CR3 /tmp/kestrel_fixture_input/
ls /tmp/kestrel_fixture_input/  # should show 4 CR3 files
```

### Step 3: For each tag, run the pipeline and capture the database

For each tag in chronological order, run this script:

```bash
TAG="<tag-name>"
WORKDIR=$(mktemp -d)
cp /tmp/kestrel_fixture_input/*.CR3 $WORKDIR/

git -C /repo checkout $TAG
git -C /repo lfs pull  # ensure models are present at this tag

# Inspect cli.py to understand the invocation form for this tag
git show $TAG:analyzer/cli.py | head -50

# Run the pipeline. The flags may have changed across tags:
# - Current form:  python analyzer/cli.py <folder> --no-gpu
# - Some older tags may need different flags
# - If --no-gpu doesn't exist, try without it (the agent's machine may not have a GPU anyway)
# - If --parallel-prefetch exists, set it to 1 for determinism

cd /repo
.venv-fixturegen/bin/python analyzer/cli.py $WORKDIR --no-gpu --parallel-prefetch 1 2>&1 | tail -20

# Capture the resulting database (if pipeline succeeded)
if [ -f "$WORKDIR/.kestrel/kestrel_database.csv" ]; then
    # Use a sanitized tag name (dots → underscores)
    SAFE_TAG=$(echo "$TAG" | tr '.' '_' | tr '/' '_')
    cp "$WORKDIR/.kestrel/kestrel_database.csv" \
       /repo/analyzer/tests/fixtures/legacy_databases/v_${SAFE_TAG}_kestrel_database.csv
    
    # Also capture scenedata and metadata if present (for richer testing)
    if [ -f "$WORKDIR/.kestrel/kestrel_scenedata.json" ]; then
        cp "$WORKDIR/.kestrel/kestrel_scenedata.json" \
           /repo/analyzer/tests/fixtures/legacy_databases/v_${SAFE_TAG}_kestrel_scenedata.json
    fi
    if [ -f "$WORKDIR/.kestrel/kestrel_metadata.json" ]; then
        cp "$WORKDIR/.kestrel/kestrel_metadata.json" \
           /repo/analyzer/tests/fixtures/legacy_databases/v_${SAFE_TAG}_kestrel_metadata.json
    fi
    echo "✓ Captured fixture for $TAG"
else
    echo "✗ Pipeline did not produce database for $TAG — check logs"
fi

rm -rf $WORKDIR
```

**Per-tag time budget:** ~20-60 seconds (pipeline runs 4 images). Total time: ~20 minutes for 18 tags.

### Step 4: Handle tags where the pipeline can't run

Some tags may fail because:
- CLI flags changed (try without `--no-gpu` / `--parallel-prefetch`)
- Dependencies don't install on current Python
- Models are incompatible with that tag's expected paths

For each failing tag, document the failure reason in `SCHEMA_NOTES.md` and move on. **Do NOT hand-synthesize fixtures for failed tags** — better to have an incomplete-but-accurate fixture set than a complete-but-fabricated one.

### Step 5: Deduplicate fixtures by unique schema

After running all tags, multiple tags may have produced identical CSV headers. **Keep only the OLDEST fixture per unique header set**, delete the rest:

```bash
# Compute header hash for each CSV and group
cd /repo/analyzer/tests/fixtures/legacy_databases
for f in v_*_kestrel_database.csv; do
    head -1 "$f" | sha256sum | cut -c1-8
    echo "  $f"
done | paste - - | sort | uniq -c | sort -rn
```

Manually delete duplicate fixtures (keeping the oldest tag in each group), and note groupings in `SCHEMA_NOTES.md`.

### Step 6: Create `SCHEMA_NOTES.md`

Write a `SCHEMA_NOTES.md` in `fixtures/legacy_databases/` documenting findings:

```markdown
# Database Schema Evolution Notes

## Procedure

Generated by running `analyzer/cli.py` at each release tag against 4 CR3 images
from `set_a_fresh/`. CSV fixtures are the literal output of those runs.

## Tag → Schema Mapping

| Tag | Date | Fixture File | Unique Schema? | Notes |
|-----|------|--------------|----------------|-------|
| alpha-2026.02.04 | 2026-02-04 | v_alpha-2026_02_04_kestrel_database.csv | yes | Earliest, pre-migration |
| Public-Release-1 | 2026-02-04 | (dedup → alpha-2026_02_04) | no | Identical schema |
| ... | ... | ... | ... | ... |

## Schema Differences From Current

For each unique schema kept, document what differs from main:

### v_alpha-2026_02_04
- Has columns: `rating`, `normalized_rating`, `scene_name`, `rating_origin` (legacy)
- Missing columns from main: `exposure_correction`, `exposure_pipeline`, ...
- Column order differences: ...

## Failed Tags

| Tag | Failure Reason |
|-----|----------------|
| test | Pipeline crashed on dependency mismatch |
| ... | ... |

## How tests use these fixtures

`analyzer/tests/compat/test_database_migration.py` parametrizes across every
fixture and verifies the current `load_database()` / `_perform_db_upgrade()`
correctly handles each historical schema.
```

---

## Output Directory Structure (Expected)

```
analyzer/tests/fixtures/legacy_databases/
├── CLOUD_AGENT_INSTRUCTIONS.md          # this file
├── SCHEMA_NOTES.md                      # NEW — generate this
├── v_alpha-2026_02_04_kestrel_database.csv
├── v_alpha-2026_02_04_kestrel_scenedata.json    # if scenedata system existed
├── v_alpha-2026_02_04_kestrel_metadata.json
├── v_Sparrow_kestrel_database.csv
├── ... (one set per unique schema)
```

---

## Verification

Before finalizing the PR, verify with this snippet from repo root:

```bash
cd /repo
python <<EOF
import pandas as pd
import os

fixture_dir = "analyzer/tests/fixtures/legacy_databases"
csvs = sorted(f for f in os.listdir(fixture_dir) if f.endswith("kestrel_database.csv"))
print(f"Found {len(csvs)} database fixtures:")
for f in csvs:
    df = pd.read_csv(os.path.join(fixture_dir, f))
    print(f"  {f}: {len(df)} rows, {len(df.columns)} cols")
    assert len(df) > 0, f"{f} is empty"
    assert "filename" in df.columns, f"{f} missing 'filename'"
EOF
```

Then run the compat test suite to confirm fixtures activate cleanly:

```bash
cd analyzer
python -m pytest tests/compat/test_database_migration.py -v
```

(Should report many parametrized tests passing, with each fixture exercised.)

---

## What NOT To Do

- ❌ Don't modify any source code outside `fixtures/legacy_databases/`.
- ❌ Don't hand-craft CSVs — only commit literal outputs from `cli.py` runs.
- ❌ Don't keep all 18 fixtures if many are duplicates — dedupe to unique schemas only.
- ❌ Don't generate a fixture for the CURRENT (main) schema — tests exercise live code.
- ❌ Don't commit very large files (crops, exports) — only the CSV/JSON.
- ❌ Don't strip rows from the CSV — preserve whatever the pipeline produced.

---

## Edge Cases

1. **Tag fails to run** — document in SCHEMA_NOTES.md, move on. Better to skip than fabricate.
2. **Pipeline produces error logs but still writes CSV** — keep the CSV; that IS the schema at that tag.
3. **Multiple consecutive tags produce identical schema** — dedupe to oldest only.
4. **Tag predates the cli.py entry point** — try `python -m analyzer.cli` or similar; if no CLI exists at that tag, skip.

---

## When Complete

Open a PR with:
- Subject: `test fixtures: legacy database schemas captured from release tags`
- Description: link to this doc, list fixtures generated, note dedup groupings, mention any failed tags.
- All fixture CSVs/JSONs + `SCHEMA_NOTES.md`.
- No other code changes.
