# Bundled SigLIP 2 smart-search assets

The files in this folder are shipped with the app (the PyInstaller specs bundle `models/`
wholesale, so this notice ships alongside them). They are for the SigLIP 2 ViT-B/16
smart search that replaces OpenAI CLIP (`exp/pipeline-v3`).

| File | What it is |
|---|---|
| `siglip2_tokenizer.json.gz` | The SigLIP 2 text tokenizer (Gemma BPE: 256000 pieces, ~580k merges) re-encoded for `kestrel_analyzer/ml/siglip_tokenizer.py`, plus the list of Gemma ids kept in the app's pruned text-embedding table. Built from `tokenizer.json` by `experiments/bioclip/export_siglip_tokenizer.py`. |

## SigLIP 2 tokenizer

From the `timm/ViT-B-16-SigLIP2` Hugging Face repository (as used by open_clip,
`ViT-B-16-SigLIP2` / `pretrained="webli"`), originally Google's big_vision SigLIP 2
release (https://github.com/google-research/big_vision), whose text tokenizer is the Gemma
SentencePiece vocabulary. Licence: Apache-2.0 per the model card.
TODO before release: confirm whether the Gemma vocabulary carries additional terms.
