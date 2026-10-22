# Bundled BioCLIP species-head assets

The files in this folder are shipped with the app (the PyInstaller specs bundle `models/`
wholesale, so this notice ships alongside the weights).

| File | What it is |
|---|---|
| `bioclip2_image_int8.onnx` | BioCLIP 2 (ViT-L/14) image tower, exported to ONNX and dynamically quantised (int8 MatMuls). Input `image` float32 [1,3,224,224], CLIP-normalised; output `emb` [1,768], not normalised. CPU graph; its weights are in `bioclip2_image.data`. Used by `kestrel_analyzer/ml/bioclip_species.py`. |
| `bioclip2_image_wdq16.onnx` | The same tower for DirectML: each int8 MatMul becomes DequantizeLinear (fp16 scales) + an fp16 MatMul/Gemm, the rest of the graph fp16 with float32 I/O. Same input/output. Built by `tools/build_gpu_towers.py`. |
| `bioclip2_image.data` | ONNX external data shared by both graphs: the int8 weights, zero points and float32 scales, stored once. |
| `species_text_emb.npy` | float16 [11251,768], L2-normalised BioCLIP 2 text embedding of `"a photo of {common name}."` for every species in `../birds/birds_global.csv`. |
| `species_index.json` | Row order of the `.npy` (IOC scientific names), prompt, model id and `logit_scale`. |
| `wildlife_text_emb.npy` + `wildlife_index.json` | float16 [1153,768] BioCLIP 2 text embeddings of non-bird animals (`"a photo of {common name}."`; labels from SpeciesNet v4.0.1a's label list (Apache-2.0) plus a few coarse invertebrate/herp groups) and their taxonomy, for bird-vs-wildlife routing in pipeline 3.0. |
| `gate_text_emb.npy` + `gate_index.json` | BioCLIP 2 text embeddings of generic animal prompts followed by background prompts (grass, bark, posts, sky, …): the zero-shot false-positive gate (`kestrel_analyzer/ml/animal_gate.py`). Built by `export_app_assets.py --gate`. |

The ONNX / int8 files and the text embeddings are a conversion of the original BioCLIP 2
weights made by `experiments/bioclip/export_app_assets.py` (open_clip → `torch.onnx.export` →
onnxruntime dynamic quantisation). They are not the upstream release files.

Species names in the text prompts come from the **IOC World Bird List v15.1** (CC BY 3.0);
see `../birds/NOTICES.md`.

## BioCLIP 2 model weights

Source: https://huggingface.co/imageomics/bioclip-2 — licence: MIT. Trained on
TreeOfLife-200M (iNaturalist, GBIF, EOL, BIOSCAN; various CC licences).

```
MIT License

Copyright (c) 2025 Imageomics Institute

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Citation

```bibtex
@software{Gu_BioCLIP_2_model,
  author = {Jianyang Gu and Samuel Stevens and Elizabeth G Campolongo and Matthew J Thompson and Net Zhang and Jiaman Wu and Andrei Kopanev and Zheda Mai and Alexander E. White and James Balhoff and Wasila M Dahdul and Daniel Rubenstein and Hilmar Lapp and Tanya Berger-Wolf and Wei-Lun Chao and Yu Su},
  license = {MIT},
  title = {{BioCLIP 2}},
  url = {https://huggingface.co/imageomics/bioclip-2},
  version = {1.0.0},
  doi = {10.57967/hf/5765},
  publisher = {Hugging Face},
  year = {2025}
}

@inproceedings{NEURIPS2025_94da80cb,
  author = {Gu, Jianyang and Stevens, Sam and Campolongo, Elizabeth and Thompson, Matthew and Zhang, Net and Wu, Jiaman and Kopanev, Andrei and Mai, Zheda and White, Alexander and Balhoff, James and Dahdul, Wasila and Rubenstein, Daniel and Lapp, Hilmar and Berger-Wolf, Tanya and Chao, Wei-Lun (Harry) and Su, Yu},
  booktitle = {Advances in Neural Information Processing Systems},
  pages = {102778--102811},
  publisher = {Curran Associates, Inc.},
  title = {BioCLIP 2: Emergent Properties from Scaling Hierarchical Contrastive Learning},
  volume = {38},
  year = {2025}
}
```
