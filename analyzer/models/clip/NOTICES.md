# Bundled CLIP smart-search assets

The files in this folder are shipped with the app (the PyInstaller specs bundle `models/`
wholesale, so this notice ships alongside the weights). They power the experimental
"Smart search" (free-text search over behaviour, pose and composition of the crops in a
folder); species identification does not use them.

| File | What it is |
|---|---|
| `clip_b16_image_int8.onnx` | OpenAI CLIP ViT-B/16 image tower, exported to ONNX with per-channel dynamic int8 quantisation of the MatMuls. Input `image` float32 [N,3,224,224], CLIP-normalised; output `emb` [N,512], not normalised. Used by `kestrel_analyzer/ml/clip_search.py`. |
| `clip_b16_text_fp16w.onnx` | OpenAI CLIP ViT-B/16 text tower, fp16 weights / fp32 compute. Input `tokens` int64 [N,77]; output `emb` [N,512], not normalised. Loaded only when a search runs. |
| `bpe_simple_vocab_16e6.txt.gz` | CLIP BPE vocabulary read by `kestrel_analyzer/ml/clip_tokenizer.py`. |

The ONNX files are a conversion of the original OpenAI CLIP ViT-B/16 weights (as loaded by
open_clip, `pretrained="openai"`) made by `experiments/bioclip/export_app_assets.py --clip`
(open_clip → `torch.onnx.export` → onnxruntime quantisation / fp16 weight cast). They are
not the upstream release files.

## OpenAI CLIP model weights and BPE vocabulary

From OpenAI CLIP (https://github.com/openai/CLIP), as redistributed by open_clip
(https://github.com/mlfoundations/open_clip). Licence: MIT.

```
MIT License

Copyright (c) 2021 OpenAI

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

The tokenizer in `kestrel_analyzer/ml/clip_tokenizer.py` is a dependency-free port of
open_clip's `SimpleTokenizer` (itself derived from OpenAI CLIP's tokenizer, MIT).

### Citation

```bibtex
@inproceedings{radford2021learning,
  title = {Learning Transferable Visual Models From Natural Language Supervision},
  author = {Radford, Alec and Kim, Jong Wook and Hallacy, Chris and Ramesh, Aditya and Goh, Gabriel and Agarwal, Sandhini and Sastry, Girish and Askell, Amanda and Mishkin, Pamela and Clark, Jack and Krueger, Gretchen and Sutskever, Ilya},
  booktitle = {International Conference on Machine Learning},
  year = {2021}
}
```
