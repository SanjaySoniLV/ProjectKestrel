# Bundled detector and segmentation models

The files in this folder are shipped with the app (the PyInstaller specs bundle `models/`
wholesale, so this notice ships alongside them). They implement the detection and
segmentation stage of analysis pipeline 3.0 (`kestrel_analyzer/ml/speciesnet_sam_hq.py`).
The folder name is historical: it no longer holds any SpeciesNet files.

| File | What it is |
|---|---|
| `mdv5a.onnx` + `mdv5a.onnx.data` | MegaDetector v5a (YOLOv5x6, 1280 px input), exported to ONNX with its weights in the `.data` sidecar. The accurate detector. |
| `mdv1000-cedar.onnx` | MegaDetector MDv1000-cedar (YOLOv9 gelan-c, 640 px input), exported to ONNX as a single file. The fast detector. |
| `sam_hq_vit_tiny_encoder.onnx` | SAM-HQ "Light HQ-SAM" ViT-Tiny image encoder, exported to ONNX. |
| `sam_hq_vit_tiny_decoder.onnx` | The matching SAM-HQ mask decoder, exported to ONNX. |

These are ONNX conversions of the upstream checkpoints, not the upstream release files.
Converted weights are still covered by the original licence.

## MegaDetector (mdv5a, mdv1000-cedar)

Source: https://github.com/agentmorris/MegaDetector (MDv5a release, and the v1000.0
release for MDv1000-cedar). The repository is released under the MIT licence; its
README notes that code written on or before April 28, 2023 is copyright Microsoft.

TODO before release: confirm the licence of the two weight files themselves. The
MegaDetector documentation does not state a separate weights licence, and the detectors
are built on YOLOv5 (Ultralytics, AGPL-3.0) and YOLOv9 (GPL-3.0) architectures. Both are
compatible with redistribution inside this AGPLv3 app, but the notice should name the
licence that actually applies.

```
MIT License

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
@article{beery2019efficient,
  title   = {Efficient Pipeline for Camera Trap Image Review},
  author  = {Beery, Sara and Morris, Dan and Yang, Siyu},
  journal = {arXiv preprint arXiv:1907.06772},
  year    = {2019}
}
```

## SAM-HQ ViT-Tiny (Light HQ-SAM)

Source: https://github.com/SysCV/sam-hq, checkpoint `sam_hq_vit_tiny.pth`. Licence:
Apache-2.0 (https://www.apache.org/licenses/LICENSE-2.0). Light HQ-SAM uses the TinyViT
image encoder provided by MobileSAM (https://github.com/ChaoningZhang/MobileSAM,
Apache-2.0).

TODO before release: the SAM-HQ README does not state a separate licence for the
checkpoints; confirm it on the checkpoint's Hugging Face page.

### Citation

```bibtex
@inproceedings{sam_hq,
  title     = {Segment Anything in High Quality},
  author    = {Ke, Lei and Ye, Mingqiao and Danelljan, Martin and Liu, Yifan and Tai, Yu-Wing and Tang, Chi-Keung and Yu, Fisher},
  booktitle = {NeurIPS},
  year      = {2023}
}
```
