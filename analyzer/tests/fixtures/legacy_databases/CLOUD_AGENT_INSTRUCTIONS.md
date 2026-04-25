# Legacy Database Fixtures - Cloud Agent Task

## Goal
Generate CSV database files matching the schema from past Kestrel versions to test database migration logic.

## Instructions

### 1. List all release tags
```bash
git log --tags --simplify-by-decoration --pretty="format:%d %H" | grep "tag:" | head -20
```

### 2. For each relevant tag, checkout and inspect the database schema

For each tag with a different schema:
```bash
git checkout <tag>
cat analyzer/kestrel_analyzer/database.py | grep -A 50 "^BASE_COLUMNS = \|^REQUIRED_COLUMNS = "
```

Look specifically for:
- Whether `rating`, `scene_name`, `normalized_rating`, `rating_origin` are inline columns in BASE_COLUMNS
- Whether `rating` appears in REQUIRED_COLUMNS
- Any other differences from the current schema

### 3. Generate synthetic fixture CSV files

For any tag where the schema differs from today (contains `rating`, `scene_name` columns inline):

Create a file: `v{tag_name}_kestrel_database.csv`

**Example:** If version 1.8.0 had a `rating` column:
- File: `v1.8.0_kestrel_database.csv`
- Headers: Copy from the BASE_COLUMNS of that version (include `rating`, `scene_name`, etc.)
- Rows: 4 synthetic rows with filenames like `IMG_0001.CR3`, `IMG_0002.CR3`, etc.
- Populate other columns with reasonable defaults:
  - `filename`: `IMG_000{1,2,3,4}.CR3`
  - `species`: `"aves,columbidae"` (example)
  - `rating`: `3` or `4` (example)
  - `scene_name`: `"Scene A"` or similar
  - Other numeric columns: `0` or `0.5`
  - Other string columns: empty string or `"unknown"`

### 4. Document schema differences

Create a `SCHEMA_NOTES.md` file documenting:
- Which versions had which columns
- When the migration to scenedata JSON happened
- Any other schema evolution milestones

## Example Output Structure

```
legacy_databases/
├── SCHEMA_NOTES.md
├── v1.8.0_kestrel_database.csv      # Had inline rating/scene_name
├── v1.9.0_kestrel_database.csv      # Had inline rating
├── v2.0.0_kestrel_database.csv      # Current schema (no migration needed)
└── CLOUD_AGENT_INSTRUCTIONS.md      # This file
```

## Notes

- Ensure the current (main branch) version is NOT included - we test against that directly in the test suite
- Focus on versions that had schema changes, not every minor release
- The test suite expects files named exactly `v{version}_kestrel_database.csv`
