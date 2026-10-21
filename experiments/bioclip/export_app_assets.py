"""Build the runtime assets the desktop app needs for the BioCLIP species head.

    python export_app_assets.py [--out ../../analyzer/models/bioclip] [--search]

Writes (all consumed by analyzer/kestrel_analyzer/ml/bioclip_species.py):
  bioclip2_image_int8.onnx     BioCLIP 2 (ViT-L/14) image tower, input "image"
                               [N,3,224,224] float32 (CLIP-normalised), output
                               "emb" [N,768] (not normalised). Dynamic int8,
                               MatMul only: quantising the patch-embed Conv emits
                               ConvInteger, which onnxruntime 1.23 (the app's pin)
                               has no CPU kernel for.
  species_text_emb.npy         float16 [11251,768], L2-normalised text embedding
                               per IOC species, prompt "a photo of {common name}."
  species_index.json           row order (IOC scientific names) + provenance
With --clip, the general-purpose CLIP used for free-text search (exp/semantic-search):
  clip_b16_image_int8.onnx     OpenAI CLIP ViT-B/16 image tower (MIT), same I/O as above, [N,512]
  clip_b16_text_int8.onnx      its text tower, input "tokens" [N,77] int64, output "emb" [N,512]
  bpe_simple_vocab_16e6.txt.gz CLIP BPE vocab
With --search, also (BioCLIP text tower; superseded by --clip for search):
  bioclip2_text_int8.onnx      text tower, input "tokens" [N,77] int64, output
                               "emb" [N,768] (not normalised); free-text search
  bpe_simple_vocab_16e6.txt.gz CLIP BPE vocab (from open_clip, MIT)

Dev-only: needs torch + open_clip + onnx + onnxruntime (see README.md). The
app itself only needs onnxruntime + numpy + Pillow at runtime.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile

import numpy as np
import open_clip
import torch

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import load_catalog  # noqa: E402
from embed_text import encode_text_trimmed  # noqa: E402

HUB = "hf-hub:imageomics/bioclip-2"
PROMPT = "a photo of {}."


def quantize(src, dst, ops=("MatMul",)):
    from onnxruntime.quantization import QuantType, quantize_dynamic
    quantize_dynamic(src, dst, weight_type=QuantType.QInt8, op_types_to_quantize=list(ops))


class TextTower(torch.nn.Module):
    """Full-length (77-token) text tower so the ONNX graph has a static mask."""

    def __init__(self, m):
        super().__init__()
        self.m = m

    def forward(self, tokens):
        m = self.m
        x = m.token_embedding(tokens) + m.positional_embedding
        x = m.transformer(x, attn_mask=m.attn_mask)
        x = m.ln_final(x)
        x = x[torch.arange(x.shape[0]), tokens.argmax(dim=-1)]
        return x @ m.text_projection


def export_clip(out):
    """General CLIP for free-text search. Export with grad enabled (see image tower note);
    the text tower's Gather (token table) is quantised too, which drops ~75 MB."""
    model, _, _ = open_clip.create_model_and_transforms("ViT-B-16", pretrained="openai")
    model.eval()
    tok = open_clip.get_tokenizer("ViT-B-16")
    tmp = tempfile.mkdtemp()
    x = torch.randn(1, 3, 224, 224)
    p = os.path.join(tmp, "img.onnx")
    torch.onnx.export(model.visual, x, p, input_names=["image"], output_names=["emb"],
                      dynamic_axes={"image": {0: "n"}, "emb": {0: "n"}}, opset_version=17, dynamo=False)
    quantize(p, os.path.join(out, "clip_b16_image_int8.onnx"))
    p = os.path.join(tmp, "txt.onnx")
    torch.onnx.export(TextTower(model), tok(["a photo of a bird."]), p, input_names=["tokens"], output_names=["emb"],
                      dynamic_axes={"tokens": {0: "n"}, "emb": {0: "n"}}, opset_version=17, dynamo=False)
    quantize(p, os.path.join(out, "clip_b16_text_int8.onnx"), ops=("MatMul", "Gather"))
    shutil.copy(os.path.join(os.path.dirname(open_clip.__file__), "bpe_simple_vocab_16e6.txt.gz"), out)
    for f in ("clip_b16_image_int8.onnx", "clip_b16_text_int8.onnx"):
        print(f, os.path.getsize(os.path.join(out, f)) / 1e6, "MB")
    shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "..", "..", "analyzer", "models", "bioclip"))
    ap.add_argument("--search", action="store_true", help="also export the BioCLIP text tower + tokenizer vocab")
    ap.add_argument("--clip", action="store_true", help="ONLY export the general CLIP ViT-B/16 towers for search")
    a = ap.parse_args()
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    torch.set_num_threads(os.cpu_count())
    if a.clip:
        export_clip(out)
        return
    model, _, _ = open_clip.create_model_and_transforms(HUB)
    model.eval()
    tok = open_clip.get_tokenizer(HUB)
    tmp = tempfile.mkdtemp()

    # 1. image tower. Export outside inference_mode/no_grad: under them PyTorch
    # takes the fused _native_multi_head_attention path, which ONNX can't export.
    x = torch.randn(1, 3, 224, 224)
    fp32 = os.path.join(tmp, "image_fp32.onnx")
    torch.onnx.export(model.visual, x, fp32, input_names=["image"], output_names=["emb"],
                      dynamic_axes={"image": {0: "n"}, "emb": {0: "n"}}, opset_version=17, dynamo=False)
    quantize(fp32, os.path.join(out, "bioclip2_image_int8.onnx"))
    print("image tower ->", os.path.getsize(os.path.join(out, "bioclip2_image_int8.onnx")) / 1e6, "MB")

    # 2. species text embeddings (all IOC species, common-name prompt)
    cat = load_catalog()
    names = [PROMPT.format(r["canonical_common_name"]) for r in cat]
    order = np.argsort([len(n) for n in names])
    emb = np.zeros((len(names), model.text_projection.shape[1]), np.float32)
    with torch.inference_mode():
        for i in range(0, len(names), 256):
            ix = order[i:i + 256]
            e = encode_text_trimmed(model, tok([names[j] for j in ix]))
            emb[ix] = torch.nn.functional.normalize(e, dim=-1).numpy()
    np.save(os.path.join(out, "species_text_emb.npy"), emb.astype(np.float16))
    json.dump({
        "model": HUB,
        "prompt": PROMPT,
        "logit_scale": float(model.logit_scale.exp()),
        "catalog": "analyzer/models/birds/birds_global.csv (IOC v15.1)",
        "scientific_names": [r["scientific_name"] for r in cat],
    }, open(os.path.join(out, "species_index.json"), "w"))
    print("text embeddings ->", emb.shape)

    # 3. optional: text tower + vocab for free-text search
    if a.search:
        t = tok(["a photo of a bird."])
        fp32 = os.path.join(tmp, "text_fp32.onnx")
        torch.onnx.export(TextTower(model), t, fp32, input_names=["tokens"], output_names=["emb"],
                          dynamic_axes={"tokens": {0: "n"}, "emb": {0: "n"}}, opset_version=17, dynamo=False)
        quantize(fp32, os.path.join(out, "bioclip2_text_int8.onnx"))
        vocab = os.path.join(os.path.dirname(open_clip.__file__), "bpe_simple_vocab_16e6.txt.gz")
        shutil.copy(vocab, out)
        print("text tower ->", os.path.getsize(os.path.join(out, "bioclip2_text_int8.onnx")) / 1e6, "MB")
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
