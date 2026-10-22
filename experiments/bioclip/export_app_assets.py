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
  clip_b16_image_int8.onnx     OpenAI CLIP ViT-B/16 image tower (MIT), same I/O as above, [N,512];
                               per-channel int8 MatMuls (keeps the sharpness signal: rho 0.69 vs 0.74 fp32)
  clip_b16_text_fp16w.onnx     its text tower, input "tokens" [N,77] int64, output "emb" [N,512];
                               fp16 *weights*, fp32 compute (int8 text reordered top-50 results by up to
                               96%; weight-only fp16 is exact and half size)
  bpe_simple_vocab_16e6.txt.gz CLIP BPE vocab
With --siglip, SigLIP 2 ViT-B/16 (Apache-2.0), the v3 search + zero-shot quality model, into models/siglip/:
  siglip2_b16_image_int8.onnx  image tower, input "image" [N,3,224,224] (bicubic squash to 224, (x/255-0.5)/0.5),
                               output "emb" [N,768]; int8 MatMuls except one outlier layer, fp16 weights
  siglip2_b16_text_fp16w.onnx  text tower WITHOUT its token table: input "x" [N,64,768] gathered token
                               embeddings, output "emb" [N,768]
  siglip2_token_emb_int8.npz   pruned token table (rows = siglip_tokenizer subset), int8 + per-row scale
  siglip_index.json            preprocessing + logit scale/bias
  (siglip2_tokenizer.json.gz comes from export_siglip_tokenizer.py)
With --gate, the v3 false-positive gate (BioCLIP text embeddings, ml/animal_gate.py), into models/bioclip/:
  gate_text_emb.npy + gate_index.json   animal prompts followed by background prompts
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


def quantize(src, dst, ops=("MatMul",), per_channel=False, exclude=()):
    from onnxruntime.quantization import QuantType, quantize_dynamic
    quantize_dynamic(src, dst, weight_type=QuantType.QInt8, op_types_to_quantize=list(ops), per_channel=per_channel,
                     nodes_to_exclude=list(exclude))


def weights_fp16(src, dst, min_size=1024):
    """Store large float initializers as fp16 + a Cast back to fp32 (exact-enough, half size)."""
    import onnx
    from onnx import TensorProto, helper, numpy_helper
    m = onnx.load(src)
    g = m.graph
    inits, casts = [], []
    for init in g.initializer:
        if init.data_type == TensorProto.FLOAT and int(np.prod(init.dims)) >= min_size:
            inits.append(numpy_helper.from_array(numpy_helper.to_array(init).astype(np.float16), init.name + "__fp16"))
            casts.append(helper.make_node("Cast", [init.name + "__fp16"], [init.name], to=TensorProto.FLOAT))
        else:
            inits.append(init)
    del g.initializer[:]
    g.initializer.extend(inits)
    nodes = list(g.node)
    del g.node[:]
    g.node.extend(casts + nodes)
    onnx.save(m, dst)


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
    """General CLIP for free-text search. Export with grad enabled (see image tower note)."""
    model, _, _ = open_clip.create_model_and_transforms("ViT-B-16", pretrained="openai")
    model.eval()
    tok = open_clip.get_tokenizer("ViT-B-16")
    tmp = tempfile.mkdtemp()
    x = torch.randn(1, 3, 224, 224)
    p = os.path.join(tmp, "img.onnx")
    torch.onnx.export(model.visual, x, p, input_names=["image"], output_names=["emb"],
                      dynamic_axes={"image": {0: "n"}, "emb": {0: "n"}}, opset_version=17, dynamo=False)
    quantize(p, os.path.join(out, "clip_b16_image_int8.onnx"), per_channel=True)
    p = os.path.join(tmp, "txt.onnx")
    torch.onnx.export(TextTower(model), tok(["a photo of a bird."]), p, input_names=["tokens"], output_names=["emb"],
                      dynamic_axes={"tokens": {0: "n"}, "emb": {0: "n"}}, opset_version=17, dynamo=False)
    weights_fp16(p, os.path.join(out, "clip_b16_text_fp16w.onnx"))
    shutil.copy(os.path.join(os.path.dirname(open_clip.__file__), "bpe_simple_vocab_16e6.txt.gz"), out)
    for f in ("clip_b16_image_int8.onnx", "clip_b16_text_fp16w.onnx"):
        print(f, os.path.getsize(os.path.join(out, f)) / 1e6, "MB")
    shutil.rmtree(tmp, ignore_errors=True)


# Non-bird animals SpeciesNet's classifier never sees often enough to have a label for,
# added so BioCLIP can still name common invertebrates/herps at a coarse level.
EXTRA_WILDLIFE = [
    ("insect", "insecta", "", ""), ("butterfly", "insecta", "lepidoptera", ""), ("moth", "insecta", "lepidoptera", ""),
    ("dragonfly", "insecta", "odonata", ""), ("damselfly", "insecta", "odonata", ""), ("bee", "insecta", "hymenoptera", ""),
    ("wasp", "insecta", "hymenoptera", ""), ("beetle", "insecta", "coleoptera", ""), ("grasshopper", "insecta", "orthoptera", ""),
    ("spider", "arachnida", "araneae", ""), ("frog", "amphibia", "anura", ""), ("toad", "amphibia", "anura", ""),
    ("salamander", "amphibia", "caudata", ""), ("turtle", "reptilia", "testudines", ""), ("lizard", "reptilia", "squamata", ""),
    ("snake", "reptilia", "squamata", ""), ("alligator", "reptilia", "crocodylia", ""), ("fish", "actinopterygii", "", ""),
    ("crab", "malacostraca", "decapoda", ""), ("snail", "gastropoda", "", ""),
]


def export_wildlife(out):
    """BioCLIP 2 text embeddings for the non-bird animals the v3 pipeline routes to "wildlife",
    replacing SpeciesNet's classifier. Labels come from SpeciesNet's own label list (non-Aves,
    named, not blank/human/vehicle) plus EXTRA_WILDLIFE; same prompt as the bird head."""
    lab = os.path.join(HERE, "..", "..", "analyzer", "models", "speciesnet",
                       "always_crop_99710272_22x8_v12_epoch_00148.labels.20251208.txt")
    rows, seen = [], set()
    for line in open(lab, encoding="utf-8"):
        parts = line.rstrip("\n").split(";")
        if len(parts) < 7:
            continue
        _, cls, order, fam, genus, sp, common = parts[:7]
        common = common.strip()
        if cls in ("aves", "") or not common or common in ("blank", "human", "vehicle", "animal", "no cv result"):
            continue
        if common.lower() in seen:
            continue
        seen.add(common.lower())
        rows.append({"common_name": common, "scientific_name": f"{genus} {sp}".strip(), "class": cls,
                     "order": order, "family": fam})
    for common, cls, order, fam in EXTRA_WILDLIFE:
        if common not in seen:
            seen.add(common)
            rows.append({"common_name": common, "scientific_name": "", "class": cls, "order": order, "family": fam})
    model, _, _ = open_clip.create_model_and_transforms(HUB)
    model.eval()
    tok = open_clip.get_tokenizer(HUB)
    names = [PROMPT.format(r["common_name"]) for r in rows]
    emb = np.zeros((len(names), model.text_projection.shape[1]), np.float32)
    with torch.inference_mode():
        for i in range(0, len(names), 256):
            emb[i:i + 256] = torch.nn.functional.normalize(encode_text_trimmed(model, tok(names[i:i + 256])), dim=-1).numpy()
    np.save(os.path.join(out, "wildlife_text_emb.npy"), emb.astype(np.float16))
    json.dump({"model": HUB, "prompt": PROMPT, "source": "SpeciesNet v4.0.1a labels (non-Aves) + EXTRA_WILDLIFE",
               "labels": rows}, open(os.path.join(out, "wildlife_index.json"), "w"))
    print("wildlife labels ->", len(rows))


# Zero-shot false-positive gate for the v3 pipeline (replaces SpeciesNet's blank/confidence
# gate): BioCLIP text embeddings of generic "there is an animal" prompts vs the things
# MegaDetector mistakes for animals. Read by analyzer/kestrel_analyzer/ml/animal_gate.py.
GATE_ANIMAL = [
    "a bird", "a small bird perched on a branch", "a bird in flight", "a waterbird", "a bird of prey",
    "a shorebird", "an owl", "a mammal", "a small mammal", "a squirrel", "a deer", "an animal",
    "a reptile", "an amphibian", "an insect", "a butterfly",
]
GATE_BACKGROUND = [
    "grass", "branches", "leaves", "tree bark", "reeds", "water", "mud", "sand", "rocks", "the sky",
    "clouds", "a wooden post", "a fence", "a power line", "a sign", "snow", "a log", "a tree stump",
    "a blurry background", "an empty scene", "a shadow", "a pine cone", "a dead leaf", "a flower",
    "a building", "a bare tree",
]


def export_gate(out):
    model, _, _ = open_clip.create_model_and_transforms(HUB)
    model.eval()
    tok = open_clip.get_tokenizer(HUB)
    prompts = [PROMPT.format(p) for p in GATE_ANIMAL + GATE_BACKGROUND]
    with torch.inference_mode():
        emb = torch.nn.functional.normalize(encode_text_trimmed(model, tok(prompts)), dim=-1).numpy()
    np.save(os.path.join(out, "gate_text_emb.npy"), emb.astype(np.float16))
    json.dump({"model": HUB, "prompt": PROMPT, "logit_scale": float(model.logit_scale.exp()),
               "animal": GATE_ANIMAL, "background": GATE_BACKGROUND},
              open(os.path.join(out, "gate_index.json"), "w"), indent=1)
    print("gate prompts ->", len(prompts))


class SiglipTextBody(torch.nn.Module):
    """SigLIP 2 text tower minus its 256k-row token table: input "x" = gathered token
    embeddings [N,64,768] (the app looks them up from the pruned int8 table), output "emb"
    [N,768] (not normalised). Pooling is the last position, so ids never enter the graph."""

    def __init__(self, t):
        super().__init__()
        self.t = t

    def forward(self, x):
        t = self.t
        x = x + t.positional_embedding[: x.shape[1]]
        x = t.transformer(x, attn_mask=None)
        x = t.ln_final(x)[:, -1]
        return t.text_projection(x)


def export_siglip(out, tokenizer_asset):
    """SigLIP 2 ViT-B/16 (Apache-2.0) for search + zero-shot quality (exp/pipeline-v3)."""
    import gzip
    sys.path.insert(0, os.path.join(HERE, "..", "..", "analyzer"))
    from kestrel_analyzer.ml.siglip_tokenizer import SiglipTokenizer
    model, _, _ = open_clip.create_model_and_transforms("ViT-B-16-SigLIP2", pretrained="webli")
    model.eval()
    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "img.onnx")
    torch.onnx.export(model.visual, torch.randn(1, 3, 224, 224), p, input_names=["image"], output_names=["emb"],
                      dynamic_axes={"image": {0: "n"}, "emb": {0: "n"}}, opset_version=17, dynamo=False)
    # Dynamic int8 on every MatMul wrecks this tower (cos 0.69 vs fp32): one layer, block 5's
    # MLP fc2, carries activation outliers. Quantised alone it drops cos to 0.72; every other
    # MatMul is harmless (>= 0.995). So leave that one float, and store it (and every other
    # float weight) as fp16: cos 0.994 vs fp32, 97 MB.
    q = os.path.join(tmp, "img_q.onnx")
    quantize(p, q, per_channel=True, exclude=["/trunk/blocks/blocks.5/mlp/fc2/MatMul"])
    weights_fp16(q, os.path.join(out, "siglip2_b16_image_int8.onnx"))
    p = os.path.join(tmp, "txt.onnx")
    torch.onnx.export(SiglipTextBody(model.text), torch.randn(1, 64, 768), p, input_names=["x"], output_names=["emb"],
                      dynamic_axes={"x": {0: "n"}, "emb": {0: "n"}}, opset_version=17, dynamo=False)
    # int8 text reorders top-50 results (84% overlap vs 98% for fp16 weights), as with CLIP.
    weights_fp16(p, os.path.join(out, "siglip2_b16_text_fp16w.onnx"))
    tok = SiglipTokenizer(tokenizer_asset)
    tok.subset_size  # loads the asset (lazy)
    W = model.text.token_embedding.weight.detach().numpy()[np.asarray(tok.subset_ids)]
    scale = np.abs(W).max(1) / 127.0
    q = np.round(W / scale[:, None]).astype(np.int8)
    np.savez(os.path.join(out, "siglip2_token_emb_int8.npz"), q=q, scale=scale.astype(np.float32))
    json.dump({"model": "open_clip ViT-B-16-SigLIP2 / webli", "image_size": 224,
               "image_mean": [0.5, 0.5, 0.5], "image_std": [0.5, 0.5, 0.5],
               "context_length": 64, "logit_scale": float(model.logit_scale.exp()),
               "logit_bias": float(model.logit_bias), "token_table_rows": int(len(q))},
              open(os.path.join(out, "siglip_index.json"), "w"), indent=1)
    for f in sorted(os.listdir(out)):
        print(f, round(os.path.getsize(os.path.join(out, f)) / 1e6, 1), "MB")
    shutil.rmtree(tmp, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "..", "..", "analyzer", "models", "bioclip"))
    ap.add_argument("--search", action="store_true", help="also export the BioCLIP text tower + tokenizer vocab")
    ap.add_argument("--clip", action="store_true", help="ONLY export the general CLIP ViT-B/16 towers for search")
    ap.add_argument("--wildlife", action="store_true", help="ONLY export BioCLIP text embeddings for non-bird wildlife")
    ap.add_argument("--siglip", action="store_true", help="ONLY export the SigLIP 2 ViT-B/16 towers + pruned token table")
    ap.add_argument("--gate", action="store_true", help="ONLY export the BioCLIP animal-vs-background gate prompts")
    a = ap.parse_args()
    out = os.path.abspath(a.out)
    os.makedirs(out, exist_ok=True)
    torch.set_num_threads(os.cpu_count())
    if a.clip:
        export_clip(out)
        return
    if a.wildlife:
        export_wildlife(out)
        return
    if a.gate:
        export_gate(out)
        return
    if a.siglip:
        export_siglip(out, os.path.join(HERE, "..", "..", "analyzer", "models", "siglip", "siglip2_tokenizer.json.gz"))
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
