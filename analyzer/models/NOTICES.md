# Bundled files directly in `models/`

The PyInstaller specs bundle `models/` wholesale, so everything here ships to every user.
Each subfolder has its own `NOTICES.md` (`bioclip/`, `birds/`, `siglip/`, `speciesnet/`).
Build inputs, backups and experiments don't belong here; inputs that are only read when an
asset is rebuilt live in `tools/model_sources/`.

| File | What it is |
|---|---|
| `quality.onnx` | The legacy quality CNN: a focus/sharpness score for a bird crop. Used by `kestrel_analyzer/ml/quality.py` (an optional scorer since analysis pipeline 3.0). |
| `quality_normalization_data.csv` | Percentile table that maps the CNN's raw output onto Kestrel's 0-1 quality scale. |

Both were trained and built for Project Kestrel; they are not third-party models. The
original Keras model they were converted from is `tools/model_sources/quality_old.keras`.

Licence: GNU Affero General Public License v3 (AGPLv3), the same as the source code
(see `LICENSE` at the repository root).
