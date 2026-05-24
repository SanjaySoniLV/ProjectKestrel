# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Project Kestrel?

Desktop app for organizing bird photography. A pywebview shell wraps a vanilla JS/HTML/CSS UI, with a Python backend (`Api` class in `api_bridge.py`) exposed to JS via the pywebview bridge. There is no SPA framework — `visualizer.js` is ~10k lines of vanilla JS.

## Commands

```bash
# Run desktop app
python analyzer/visualizer.py

# Headless CLI
python analyzer/cli.py "path/to/photos" --no-gpu

# Tests (pytest)
pytest analyzer/tests -m unit
pytest analyzer/tests/unit/test_database.py -v
pytest analyzer/tests/unit/test_database.py::TestName::test_case

# Taxonomy tests (no ML weights needed)
PYTHONPATH=analyzer python -m unittest analyzer.tests.test_speciesnet_taxonomy -v

# Pipeline smoke test
python analyzer/cli.py test_imgs --no-gpu --parallel-prefetch 1
```

Test config lives in `analyzer/tests/pytest.ini`. Markers: `unit`, `integration`, `e2e`, `compat`, `ui`. `conftest.py` puts `analyzer/` on `sys.path`, so tests import as `from kestrel_analyzer.database import ...`.

## Architecture

### Python layers

- **`analyzer/kestrel_analyzer/`** — pure pipeline, no UI dependencies. `pipeline.py` (`AnalysisPipeline`) orchestrates: decode → detection (SpeciesNet + SAM-HQ) → species classification → quality scoring → scene grouping → CSV write. All ML model loading is lazy via `load_models()`.
- **`analyzer/api_bridge.py`** — `Api` class; every JS↔Python call lands here. Adding a bridge call means: new method on `Api`, JS call site in `visualizer.js`, and settings-schema update in `settings_utils.py` if persisted.
- **`analyzer/queue_manager.py`** — sequential folder analysis queue. Lazy-imports the pipeline so the app starts fast for browse-only sessions. Snapshots settings at enqueue time.
- **`analyzer/settings_utils.py`** — atomic `settings.json` I/O with schema validation. Protected by `_SAVE_LOCK` (JS bridge, queue worker, and startup all write concurrently). Unknown keys are dropped on save; new persisted settings require an entry in `_sanitize_settings_payload()`.

### Bird taxonomy data

- **`analyzer/models/birds/birds_global.csv`** — 11,000+ species from IOC World Bird List. Columns: `canonical_common_name`, `scientific_name`, `family_sci`, `family_common`, `order`, `regions`, `alpha_4`, `aliases`, `is_model_species`. Built by `tools/build_bird_catalog.py`.
- **`analyzer/kestrel_analyzer/bird_catalog.py`** — `BirdCatalog` singleton loads the CSV. `BirdRecord` dataclass holds per-species data. Fuzzy search with 8-tier scoring (alpha code → exact → prefix → token-prefix → subsequence → substring). `family_sci_map` property gives family_common → family_sci for all families.
- **`analyzer/models/labels_scispecies.csv`** / **`scispecies_dispname.csv`** — legacy ML model species → scientific family mapping (500 species).

### JS UI architecture

- **`analyzer/js/scene-grid.js`** — card rendering. "Most specific tag" rule: `const cardPills = (s.species && s.species.length) ? s.species : (s.families || []);` — species pills hide family pills on cards; dialog shows both.
- **`analyzer/js/scene-dialog.js`** — `_birdRecordCache` (Map), `_birdRecordMisses` (Set), `_familyCommonToSci` (Map). Scientific names resolved via `_resolvePillSci()` with lazy batch hydration via `_hydrateBirdRecords()` → backend `lookup_birds()`.
- **`analyzer/js/scenes.js`** — scene aggregation, `_sceneMatchesQuery` with optional sci-name matching.

### Settings flow

Backend persists `settings.json` at a platform-specific path. Frontend mirrors into `localStorage` key `kestrel-webviz-settings-v1`. JS reads/writes via `window.pywebview.api.get_settings()` / `save_settings_data()`.

## Security

All control flows through the pywebview JS bridge. Bridge methods that accept paths jail operations under a validated root (`_validate_root_dir` + `_is_within_root` + `_resolve_path_in_root`). `open_url` allowlists `http`, `https`, `mailto` only. Static file serving sends strict CSP.

## Output layout

Per analyzed folder, `.kestrel/` contains: `kestrel_database.csv` (per-image results), `kestrel_scenedata.json` (scene grouping + user edits), `kestrel_metadata.json` (audit trail), `export/` (thumbnails), `crop/` (bird crops).
