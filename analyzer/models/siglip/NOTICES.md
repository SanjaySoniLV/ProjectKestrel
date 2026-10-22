# Bundled SigLIP 2 assets (smart search + default quality score)

The files in this folder are shipped with the app (the PyInstaller specs bundle `models/`
wholesale, so this notice ships alongside them). They implement analysis pipeline 3.0's
smart search and its default zero-shot sharpness score (`kestrel_analyzer/ml/siglip.py`).

| File | What it is |
|---|---|
| `siglip2_b16_image_int8.onnx` | SigLIP 2 ViT-B/16 image tower, ONNX, dynamic int8 MatMuls (one outlier layer left float), fp16-stored weights. Input `image` float32 [N,3,224,224] (RGB, bicubic squash to 224, `(x/255-0.5)/0.5`); output `emb` [N,768]. Run one image per call. |
| `siglip2_b16_text_fp16w.onnx` | The text tower without its token-embedding table: input `x` float32 [N,64,768] (gathered token embeddings), output `emb` [N,768]. fp16-stored weights, fp32 compute. |
| `siglip2_token_emb_int8.npz` | The token-embedding table pruned to the ids in the tokenizer's `subset` (110,809 rows), int8 + per-row scale. |
| `siglip2_tokenizer.json.gz` | The SigLIP 2 text tokenizer (Gemma BPE: 256000 pieces, ~580k merges) re-encoded for `kestrel_analyzer/ml/siglip_tokenizer.py`, plus the subset ids. |
| `siglip_index.json` | Preprocessing constants, logit scale / bias. |
| `quality_calibration.json` | The focus-only prompts and the fixed monotone calibration that maps the raw SigLIP sharpness score onto Kestrel's 0-1 quality scale (built by `experiments/bioclip/quality_calibrate.py`). |
| `quality_prompt_emb.npy` + `.json` | The two calibration prompts' text embeddings, precomputed by `tools/build_v3_text_assets.py` so analysis never loads the text tower. |

The ONNX / int8 / pruned files are a conversion of the original weights made by
`experiments/bioclip/export_app_assets.py --siglip` (open_clip → `torch.onnx.export` →
onnxruntime dynamic quantisation) and `export_siglip_tokenizer.py`. They are not the
upstream release files.

## SigLIP 2 model weights and tokenizer

From the `timm/ViT-B-16-SigLIP2` Hugging Face repository (as used by open_clip,
`ViT-B-16-SigLIP2` / `pretrained="webli"`), originally Google's big_vision SigLIP 2
release (https://github.com/google-research/big_vision), whose text tokenizer is the Gemma
SentencePiece vocabulary. Licence: Apache-2.0 per the model card
(https://www.apache.org/licenses/LICENSE-2.0).
TODO before release: confirm whether the Gemma vocabulary carries additional terms.

### Citation

```bibtex
@article{tschannen2025siglip2,
  title   = {SigLIP 2: Multilingual Vision-Language Encoders with Improved Semantic Understanding, Localization, and Dense Features},
  author  = {Tschannen, Michael and Gritsenko, Alexey and Wang, Xiao and Naeem, Muhammad Ferjad and Alabdulmohsin, Ibrahim and Parthasarathy, Nikhil and Evans, Talfan and Beyer, Lucas and Xia, Ye and Mustafa, Basil and H{\'e}naff, Olivier and Harmsen, Jeremiah and Steiner, Andreas and Zhai, Xiaohua},
  journal = {arXiv preprint arXiv:2502.14786},
  year    = {2025}
}
```
