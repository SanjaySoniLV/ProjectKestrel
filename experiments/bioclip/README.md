# BioCLIP species-classifier experiment

Exploratory code for evaluating [BioCLIP 2](https://huggingface.co/imageomics/bioclip-2) and
[BioCLIP 2.5 Huge](https://huggingface.co/imageomics/bioclip-2.5-vith14) (both MIT) as a zero-shot
replacement for Kestrel's bird species CNN (`analyzer/models/model.onnx`). The detector and SAM stages
are unchanged; only the species head is swapped.

Nothing here is imported by the app, and none of it is a dependency of the main branch. Results are not
kept in this repo.

## Setup

Use a separate virtualenv; these packages are **not** added to the app's requirements.

```bash
python -m venv venv
venv/bin/pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
venv/bin/pip install open_clip_torch onnx onnxruntime opencv-python-headless pandas pillow
```

Working files go to `$BIOCLIP_WORK` (default `/home/user/bioclip-work`): `data/` (downloaded crops),
`cache/` (embeddings, ONNX exports), `results/` (JSON). Never commit any of these.

The perches to evaluate are listed in a private `$BIOCLIP_WORK/eval_config.json` (schema:
`eval_config.example.json`). Each perch has a `region` (`us` or `india`, which picks the
continent-level geofence) and a `checklist` (a file in `geofence/`).

## Pipeline

| Step | Script | Notes |
|---|---|---|
| 1. Data | `fetch_perches.py --out $BIOCLIP_WORK/data <slug>...` | Public Perch API. Downloads each scene's Kestrel square crops (1024 px) and writes `crops_index.csv` with the scene's species tags. |
| 2. Image embeddings | `embed_images.py --model bioclip-2 [--frac 0.6] [--bf16] [--onnx int8.onnx --tag _int8]` | One L2-normalised embedding per crop. `--frac` centre-crops the square crop first. |
| 3. Text embeddings | `embed_text.py --model bioclip-2 --text-type taxon_com [--templates single\|bird7\|openai80] [--subset names.txt]` | One embedding per IOC species (11,251 by default). |
| 4. Baseline | `kestrel_cnn.py --frac 1.0` | Runs the current CNN on the same crops with the app's exact preprocessing. |
| 5. Cost | `bench.py` | CPU latency of every pipeline ONNX model vs the BioCLIP image towers (torch fp32, bf16, ONNX fp32, ONNX int8). |
| 6. Scoring | `run_experiments.py [name...]` | All experiments; writes `$BIOCLIP_WORK/results/<name>.json`. Prompt default is `com`; trick experiments use `$BIOCLIP_MODEL` (default `bioclip-2`). |
| 7. Summary | `summarize.py` | Headline per-perch table, int8/bf16 fidelity, and a list of scenes where the model confidently disagrees with the tag. |

Shared code: `common.py` (eval set, IOC taxonomy, prompt strings), `labelsets.py` (geofence tiers),
`evalcore.py` (metrics, scene aggregation).

## Ground truth and label space

Everything is keyed on the **IOC v15.1 scientific name** from `analyzer/models/birds/birds_global.csv`.
Scene tags resolve by common name or alias, plus two AOS→IOC fixes for the Americas: "Sandwich Tern" →
Cabot's Tern and "Barn Owl" → American Barn Owl. These two fixes are also applied to the CNN's outputs.
Only scenes with exactly one bird species tag are scored, and every crop in such a scene takes that
species as its truth.

## Prompt templates (documented per the experiment brief)

BioCLIP 2 was trained on five caption types per image (`src/training/data.py` in Imageomics/bioclip-2).
`common.text_for()` reproduces each one:

| `--text-type` | String for Snowy Egret |
|---|---|
| `com` | `Snowy Egret` |
| `sci` | `Egretta thula` |
| `taxon` | `Animalia Chordata Aves Pelecaniformes Ardeidae Egretta thula` |
| `sci_com` | `Egretta thula with common name Snowy Egret` |
| `taxon_com` | `Animalia Chordata Aves Pelecaniformes Ardeidae Egretta thula with common name Snowy Egret` |

The strings come straight from the catalog: IOC order and family, IOC binomial and the catalog's
canonical common name. Kingdom, phylum and class are fixed, because every class is a bird.

Each string is wrapped in one or more templates, and a class embedding is the normalised mean of its
per-template embeddings:

- `single`: `a photo of {c}.`. This is the default, used for every 11k-species run. With `com` it
  tied or beat every other text type and template set in our runs.
- `bird7`: seven photography-flavoured templates of our own (see `common.templates`).
- `openai80`: the 80 OpenAI ImageNet templates, imported from `open_clip`. This is the set BioCLIP's
  own NABirds evaluation uses.

`embed_text.encode_text_trimmed` drops trailing pad tokens before the text transformer runs. The text
tower is causal, so the result matches `CLIP.encode_text` (max diff about 1e-7) and runs 2–4× faster
on CPU.

## Geofence tiers (`labelsets.py`)

| Tier | Classes |
|---|---|
| `global` | All 11,251 IOC species |
| `continent` | IOC breeding-range code `NA` (US perches) or `OR` (India), plus `Worldwide`. US perches also get the CNN's 500 species, which add introduced species and pelagics that breeding ranges miss. |
| `continent+` | Adds `MA` (US) or `PAL` (India) |
| `checklist` | `geofence/<name>.txt`: a country or state list filtered from the IOC catalog by an LLM sub-agent that could not see the eval labels. Treat these lists as approximate. |
| `cnn500` | Exactly the CNN's vocabulary |
| `oracle` | Only the species present in that perch (an upper bound, not deployable) |
