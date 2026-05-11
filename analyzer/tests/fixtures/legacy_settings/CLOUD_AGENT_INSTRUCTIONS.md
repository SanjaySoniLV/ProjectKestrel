# Legacy Settings Fixtures — Cloud Agent Task Brief

## Goal

Generate **authentic** `settings.json` files from each historical Kestrel release by **actually executing the settings code** at each git tag. These fixtures power `analyzer/tests/compat/test_settings_migration.py`, which verifies the current `load_persisted_settings()` / `_sanitize_settings_payload()` correctly handle every historical key-set.

**Approach:** Don't hand-write JSON. Checkout each tag → import that tag's `settings_utils` module → call `save_persisted_settings()` with seeded values → capture the file from disk → label with the tag name. The settings sanitizer at each tag will write out exactly the keys it knew about at that version, with its own defaults applied.

---

## Background

Settings live at:
- **Windows:** `%LOCALAPPDATA%\ProjectKestrel\settings.json`
- **macOS:** `~/Library/Application Support/ProjectKestrel/settings.json`
- **Linux:** `~/.local/share/project-kestrel/settings.json`

Each tag's `analyzer/settings_utils.py` defines:
- Which keys are valid at that version
- Default values
- Sanitization (coercion, clamping, enum allowlists)
- Monotonic counter guard (`kestrel_impact_total_files`, `kestrel_impact_total_seconds`)
- Forward-compat passthrough (in newer versions)

We want the literal output of `save_persisted_settings()` at each tag.

---

## Environment Setup

The cloud agent needs:

1. **Python 3.11**.
2. **A clean filesystem location for the per-tag settings file** — to avoid collisions, override the settings directory per-tag via env var or symlink. See "Tip: redirect settings path" below.
3. **No models or heavy deps needed** — settings code is pure Python; you don't need to install `requirements*.txt` for this task.

### Tip: Redirect settings path to a temp dir

The settings module reads `_get_user_data_dir()` from environment-derived paths. To avoid polluting your real `%LOCALAPPDATA%` or `~/Library`, you can:

- **Option A (Linux/macOS):** Use `HOME=$tempdir python ...` or `XDG_DATA_HOME=$tempdir/.local/share python ...`
- **Option B (cross-platform):** Move/rename the resulting file after each capture.
- **Option C (cleanest):** Monkey-patch `_get_user_data_dir` in the capture script (see Step 3).

---

## Step-by-Step Procedure

### Step 1: List release tags

```bash
git -C /repo for-each-ref --sort=creatordate \
  --format='%(creatordate:short) %(refname:short)' refs/tags
```

(Same 18 tags as for legacy databases — see that doc.)

### Step 2: Write a per-tag capture script

Save this as `/tmp/capture_settings.py`. It loads the tag's `settings_utils`, calls `load_persisted_settings()` to get a clean defaults dict, then seeds it with deliberate values and calls `save_persisted_settings()`, then reads the file off disk:

```python
"""Capture script — run inside each tag's checkout.

Outputs: prints the contents of the settings.json that the tag's own code wrote.
"""
import sys
import os
import json
import tempfile

# Force settings into a temp dir so we don't touch the real user profile
TEMP_HOME = tempfile.mkdtemp(prefix='kestrel_fixture_')

# Override platform user-data locations
os.environ['HOME'] = TEMP_HOME
os.environ['XDG_DATA_HOME'] = os.path.join(TEMP_HOME, '.local', 'share')
os.environ['LOCALAPPDATA'] = os.path.join(TEMP_HOME, 'AppData', 'Local')

# Make analyzer importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'analyzer'))

import settings_utils

# Seed values — these are deliberately maxed out / non-default so we can prove
# the sanitizer preserves them rather than silently resetting to defaults.
SEED = {
    "editor": "/Applications/Adobe Lightroom Classic.app",
    "rating_profile": "balanced",
    "detection_threshold": 0.27,
    "scene_time_threshold": 1500,
    "mask_threshold": 0.55,
    "max_bird_crops": 5,
    "wildlife_model_mode": "balanced",
    "detector_name": "mdv5a",
    "exposure_quality": "balanced",
    "thumbnail_max_width": 1200,
    "thumbnail_jpeg_quality": 75,
    "raw_preview_cache_enabled": True,
    # Monotonic counters — deliberately high so the guard test can verify
    # they're preserved across loads:
    "kestrel_impact_total_files": 15000,
    "kestrel_impact_total_seconds": 9500.5,
    # Some plausible future keys — sanitizers in tags WITH passthrough should
    # preserve these; sanitizers WITHOUT passthrough should drop them silently.
    # The test suite uses this to verify forward-compat behavior per tag.
    "future_feature_xyz": True,
    "future_unknown_key": "hello",
}

# Save (calls the tag's own _sanitize_settings_payload first)
settings_utils.save_persisted_settings(SEED)

# Read the file back from disk and emit to stdout
path = settings_utils._get_settings_path()
with open(path, 'r', encoding='utf-8') as f:
    print(f.read())
```

### Step 3: For each tag, run the capture

```bash
TAG="<tag-name>"
SAFE_TAG=$(echo "$TAG" | tr '.' '_' | tr '/' '_')
OUT_FILE="/repo/analyzer/tests/fixtures/legacy_settings/settings_v_${SAFE_TAG}.json"

git -C /repo checkout $TAG

# Verify settings_utils exists at this tag
if [ ! -f /repo/analyzer/settings_utils.py ]; then
    echo "✗ $TAG has no settings_utils.py — skipping"
    continue
fi

# Run the capture, redirecting stdout to the fixture file
python /tmp/capture_settings.py > $OUT_FILE 2>/tmp/capture_err_${SAFE_TAG}.log

if [ -s "$OUT_FILE" ]; then
    # Validate it parses as JSON
    if python -c "import json; json.load(open('$OUT_FILE'))" 2>/dev/null; then
        echo "✓ Captured $TAG → settings_v_${SAFE_TAG}.json"
    else
        echo "✗ $TAG produced invalid JSON — see capture_err_${SAFE_TAG}.log"
        rm $OUT_FILE
    fi
else
    echo "✗ $TAG produced empty output — see capture_err_${SAFE_TAG}.log"
    rm $OUT_FILE
fi
```

**Time budget:** ~1-2 seconds per tag. Total: <1 minute for 18 tags.

### Step 4: Generate edge-case variants for the most recent legacy tag

Pick the most recent legacy tag (not main — e.g., `Gambels-Quail` if it's still on the previous schema). Generate two extra fixtures:

```python
# --- minimal variant ---
# Use only required keys + monotonic counters
SEED_MINIMAL = {
    "kestrel_impact_total_files": 15000,
    "kestrel_impact_total_seconds": 9500.5,
}
# Save and capture as settings_v_<tag>_minimal.json

# --- maximal variant (with future keys) ---
# Already covered by main capture, but force a separate file
# Save and capture as settings_v_<tag>_with_future_keys.json
# (Same seed as main script — this just labels the file with a hint)
```

### Step 5: Deduplicate fixtures by unique key-set

Multiple tags may produce JSON with identical key sets. Keep the OLDEST tag per unique key-set:

```bash
cd /repo/analyzer/tests/fixtures/legacy_settings
for f in settings_v_*.json; do
    # Skip variants
    case "$f" in *_minimal.json|*_with_future_keys.json) continue;; esac
    
    # Hash the sorted key list
    python -c "
import json, sys, hashlib
d = json.load(open('$f'))
key_sig = ','.join(sorted(d.keys()))
print(hashlib.sha256(key_sig.encode()).hexdigest()[:8])
"
    echo "  $f"
done | paste - - | sort | uniq -c | sort -rn
```

Manually delete duplicates, keeping only the oldest tag in each group. Document groupings in `KEY_EVOLUTION.md`.

### Step 6: Create `KEY_EVOLUTION.md`

```markdown
# Settings Key Evolution Notes

## Procedure

Generated by running each tag's own `settings_utils.save_persisted_settings()`
with a seeded payload, then capturing the resulting `settings.json` from disk.
This produces *literal* sanitizer output for each version.

## Tag → Key-Set Mapping

| Tag | Date | Fixture File | Unique Set? | Has Future-Key Passthrough? | Notes |
|-----|------|--------------|-------------|------------------------------|-------|
| alpha-2026.02.04 | 2026-02-04 | settings_v_alpha-2026_02_04.json | yes | no | Earliest |
| Sparrow | 2026-02-07 | (dedup → alpha-2026_02_04) | no | no | Identical |
| ... | ... | ... | ... | ... | ... |

## Key Lifecycle

For each key, note which tag first added it (the earliest tag where it appears
in the fixture output):

| Key | First Seen Tag | Notes |
|-----|----------------|-------|
| `editor` | alpha-2026.02.04 | External editor path |
| `detection_threshold` | alpha-2026.02.04 | |
| `rating_profile` | Junco | Added when 5 profiles introduced |
| `wildlife_model_mode` | Tufted-Titmouse | |
| ... | ... | ... |

## Monotonic Counters

| Counter | First Seen | Type | Guard Tested? |
|---------|------------|------|---------------|
| `kestrel_impact_total_files` | <tag> | int | yes |
| `kestrel_impact_total_seconds` | <tag> | float | yes |

## Forward-Compat Passthrough

| Tag | Future keys preserved? |
|-----|------------------------|
| alpha-2026.02.04 | no — passthrough not yet introduced |
| ... | ... |
| Gambels-Quail | yes |

## Failed Tags

| Tag | Failure Reason |
|-----|----------------|
| test | settings_utils.py crash on import |
| ... | ... |
```

---

## Output Directory Structure (Expected)

```
analyzer/tests/fixtures/legacy_settings/
├── CLOUD_AGENT_INSTRUCTIONS.md            # this file
├── KEY_EVOLUTION.md                       # NEW — generate this
├── settings_v_alpha-2026_02_04.json
├── settings_v_Junco.json
├── settings_v_<next-unique-set>.json
├── settings_v_Gambels-Quail.json
├── settings_v_Gambels-Quail_minimal.json
├── settings_v_Gambels-Quail_with_future_keys.json
└── ... (one JSON per unique key-set + edge variants for newest tag)
```

---

## Verification

Before finalizing, run:

```bash
cd /repo
python <<EOF
import json
import os

fixture_dir = "analyzer/tests/fixtures/legacy_settings"
jsons = sorted(f for f in os.listdir(fixture_dir) if f.endswith(".json"))
print(f"Found {len(jsons)} settings fixtures:")
for f in jsons:
    path = os.path.join(fixture_dir, f)
    with open(path) as fp:
        data = json.load(fp)
    print(f"  {f}: {len(data)} keys")
    assert isinstance(data, dict)
    assert len(data) > 0
EOF
```

Then run the compat test suite — fixtures should activate the previously-skipped parametrized tests:

```bash
cd analyzer
python -m pytest tests/compat/test_settings_migration.py -v
```

Also run the existing settings unit tests to confirm no regression:

```bash
python -m pytest tests/unit/test_settings.py -v
```

(Should still report all 44 tests passing.)

---

## What NOT To Do

- ❌ Don't modify any source code outside `fixtures/legacy_settings/`.
- ❌ Don't hand-write JSON — only commit output from `save_persisted_settings()` runs.
- ❌ Don't pollute your real settings.json — always redirect via env vars or monkeypatch (see Step 2).
- ❌ Don't commit fixtures with real user data, credentials, or paths from your filesystem.
- ❌ Don't generate a fixture for main — tests exercise live code.
- ❌ Don't combine multiple unique key-sets into one fixture.

---

## Edge Cases

1. **Tag has no `settings_utils.py`** — skip, document in `KEY_EVOLUTION.md`.
2. **Tag's settings_utils crashes on import** — try installing minimal deps (`pip install` whatever it requires), or document failure and skip.
3. **`_get_settings_path` differs at that tag** — read the file from wherever that tag writes to.
4. **Tag has a different sanitizer behavior** (e.g., a key has different type / default than today) — the captured fixture should reflect that; the test will exercise our current loader's handling.
5. **Tag rejects unknown keys (no passthrough)** — the captured fixture will not contain `future_feature_xyz` / `future_unknown_key`. Note in `KEY_EVOLUTION.md`. Generate the `_with_future_keys.json` variant ONLY for tags that have passthrough.

---

## When Complete

Open a PR with:
- Subject: `test fixtures: legacy settings.json captured from release tags`
- Description: link to this doc, list fixtures generated, note dedup groupings, mention any failed tags.
- All fixture JSONs + `KEY_EVOLUTION.md`.
- No other code changes.
