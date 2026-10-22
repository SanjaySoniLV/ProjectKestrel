"""GPU execution-provider probe: where does each Kestrel model actually run?

    python tools/ep_probe.py [--out probe.json] [--variants dyn,wdq] [--runs 5]

For every model the analyzer loads, under every provider configuration this
platform offers (CPU; CoreML NeuralNetwork / MLProgram on macOS; DirectML on
Windows), it builds a session, records how many graph nodes landed on each
execution provider (ORT's verbose "Node placements" log, the same parser the app
uses), runs a few inferences on fixed random input, and compares the output to
the CPU result (cosine). A split graph (many CPU nodes next to the GPU EP) or a
cosine < 0.99 means that configuration is not usable.

The int8 BioCLIP / SigLIP image towers use *dynamic* quantisation
(DynamicQuantizeLinear + MatMulInteger), which CoreML has no ops for, and were exported
with a dynamic batch axis, so the attention scale and every Reshape target are computed
at run time from Shape/Gather/Unsqueeze/Concat (tiny int64 nodes no GPU EP can run: on
DirectML they put 800+ nodes on the CPU and force a host copy per block). ``--variants``
adds rewritten copies of those two files, built here from the shipped weights:

  dyn   the shipped file
  sdyn  dyn with the batch fixed to 1 and the shape plumbing constant-folded away
        (ORT basic-level optimisation; still int8 + dynamic quantisation). The DirectML fix.
  wdq   weight-only dequant: each MatMulInteger pattern becomes
        DequantizeLinear(int8 W, scale, zp) -> MatMul in float. Same int8 bytes on
        disk; activations stay float. (CoreML has no DequantizeLinear, so this is a
        DirectML / CPU experiment, not a CoreML one.)
  f32   the same weights fully dequantised to float32 initializers (4x larger)
  f16   f32 converted to half precision: fp16 weights and compute, float32 graph
        inputs/outputs, LayerNorm / Softmax / Div / Sqrt / Erf kept in float32
  wdq16 wdq with float16 scales / dequantised weights / matmuls (needs opset 19); same
        size as the shipped int8 file.
  s*    swdq / swdq16 / sf32 / sf16: the same with the static-batch transform first. Static graphs
        also get MatMul+Add fused into Reshape -> Gemm -> Reshape (ORT's own load-time
        fusion crashes on these graphs) and the scalar-index Gathers of the q/k/v unpack
        turned into Slice + Squeeze (MLProgram has no Gather). Static graphs take N=1 only,
        which is what the app feeds (ml/bioclip_species.py, ml/siglip.py).

Needs the `onnx` package only for the variants (CI / dev; the app never does this
at runtime). Intended for the GitHub Actions workflow `.github/workflows/ep-probe.yml`,
where macOS runners have CoreML and Windows runners have DirectML (WARP or
paravirtualised GPU, so timings there are not representative; placement and
correctness are).
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "analyzer"))
os.environ.setdefault("KESTREL_ORT_PLACEMENT_DIAG", "1")

VARIANT_NAMES = ("wdq", "f32", "f16", "wdq16", "sdyn", "swdq", "sf32", "sf16", "swdq16")

MODELS = {
    # kind: (relative path, int8-dynamic tower?)
    "detector_mdv5a": ("analyzer/models/speciesnet/mdv5a.onnx", False),
    "sam_encoder": ("analyzer/models/speciesnet/sam_hq_vit_tiny_encoder.onnx", False),
    "quality_legacy": ("analyzer/models/quality.onnx", False),
    "siglip_image": ("analyzer/models/siglip/siglip2_b16_image_int8.onnx", True),
    "bioclip_image": ("analyzer/models/bioclip/bioclip2_image_int8.onnx", True),
    "siglip_text": ("analyzer/models/siglip/siglip2_b16_text_fp16w.onnx", False),
}


def provider_configs():
    import onnxruntime as ort
    avail = ort.get_available_providers()
    cfgs = [("cpu", ["CPUExecutionProvider"])]
    if "CoreMLExecutionProvider" in avail:
        cfgs.append(("coreml_nn", [("CoreMLExecutionProvider", {}), "CPUExecutionProvider"]))
        cfgs.append(("coreml_mlprogram", [("CoreMLExecutionProvider", {"ModelFormat": "MLProgram"}), "CPUExecutionProvider"]))
        cfgs.append(("coreml_mlprogram_gpu", [("CoreMLExecutionProvider", {"ModelFormat": "MLProgram", "MLComputeUnits": "CPUAndGPU"}), "CPUExecutionProvider"]))
    if "DmlExecutionProvider" in avail:
        cfgs.append(("dml", ["DmlExecutionProvider", "CPUExecutionProvider"]))
    return cfgs, avail


# ── variants ─────────────────────────────────────────────────────────────────

# ops kept in float32 by the "f16" variant (reductions / transcendental ops that lose
# accuracy or overflow in half precision); everything else computes in float16.
F16_KEEP32 = {"LayerNormalization", "Softmax", "ReduceMean", "Pow", "Div", "Sqrt", "Erf",
              "ConstantOfShape"}
# shape plumbing: never gets a float cast inserted in front of it
_F16_PASSTHROUGH = {"Shape", "Size"}


def _rewrite(src: str, dst: str, mode: str) -> dict:
    """Replace every dynamic-quant MatMul pattern produced by onnxruntime's
    ``quantize_dynamic`` with a float MatMul on dequantised weights.

    Pattern: x -> DynamicQuantizeLinear -> (xq, xs, xz);
             MatMulInteger(xq, Wq, xz, Wz) -> Cast(float) -> Mul(., Mul(xs, Ws)) -> y
    where Ws may be a float initializer or Cast(fp16 initializer) (SigLIP stores its
    float constants as fp16 + Cast).

    mode:
      wdq  DequantizeLinear(int8 Wq, Ws, Wz[, axis=1]) -> MatMul(x, W). Same int8 bytes
           on disk; the SigLIP Cast(fp16)->float scale nodes stay intact.
      f32  W = (Wq - Wz) * Ws precomputed as float32 initializers; every Cast of an
           fp16 initializer and every float Constant is folded into a float32 initializer.
      wdq16  wdq with the scales, the dequantised weight and the matmuls in float16 (int8
           bytes on disk, same file size as the shipped graph); opset raised to 19.
      f16  the f32 graph converted to half precision (see ``_to_f16``): fp16 weights and
           compute, float32 graph inputs/outputs (boundary Casts), LayerNorm / Softmax /
           ReduceMean / Div / Sqrt / Erf kept in float32.
    """
    import onnx
    from onnx import helper, numpy_helper

    if mode not in ("wdq", "f32", "f16", "wdq16"):
        raise ValueError(mode)
    m = onnx.load(src)
    g = m.graph
    inits = {i.name: i for i in g.initializer}
    prod = {o: n for n in g.node for o in n.output}
    cons: dict[str, list] = {}
    for n in g.node:
        for i in n.input:
            cons.setdefault(i, []).append(n)

    def const_f32(name):
        """float32 value of an initializer or of Cast(initializer); None if not constant."""
        if name in inits:
            return numpy_helper.to_array(inits[name]).astype(np.float32)
        p = prod.get(name)
        if p is not None and p.op_type == "Cast" and p.input[0] in inits:
            return numpy_helper.to_array(inits[p.input[0]]).astype(np.float32)
        return None

    replace: dict[int, list] = {}   # id(mul node) -> [new nodes]
    remove: set[int] = set()
    new_inits = []
    n_rewritten = 0
    for mm in [n for n in g.node if n.op_type == "MatMulInteger"]:
        xq, wq = mm.input[0], mm.input[1]
        wz = mm.input[3] if len(mm.input) > 3 else ""
        dql = prod.get(xq)
        if dql is None or dql.op_type != "DynamicQuantizeLinear" or wq not in inits:
            continue
        if wz and wz not in inits:
            continue
        x, xs = dql.input[0], dql.output[1]
        if len(cons.get(mm.output[0], [])) != 1:
            continue
        cast = cons[mm.output[0]][0]
        if cast.op_type != "Cast" or len(cons.get(cast.output[0], [])) != 1:
            continue
        mul = cons[cast.output[0]][0]
        if mul.op_type != "Mul":
            continue
        other = [i for i in mul.input if i != cast.output[0]][0]
        smul = prod.get(other)
        if smul is None or smul.op_type != "Mul" or xs not in smul.input:
            continue
        ws = [i for i in smul.input if i != xs][0]
        Ws = const_f32(ws)
        if Ws is None:
            continue
        Wq = numpy_helper.to_array(inits[wq])
        per_channel = Ws.ndim == 1
        if per_channel and Ws.shape[0] != Wq.shape[1]:
            continue        # not a [K,N] weight with per-output-channel scales
        y = mul.output[0]
        wname = wq + "__deq"
        if mode in ("wdq", "wdq16"):
            # zero-point dtype is the weight's own (int8 or uint8) because it is the original
            # initializer; per-channel scales/zero-points are [N] -> axis 1 of the [K,N] weight.
            dq_in = [wq, ws] + ([wz] if wz else [])
            attrs = {"axis": 1} if per_channel else {}
            nodes = [helper.make_node("DequantizeLinear", dq_in, [wname], name=wname + "_dq", **attrs)]
        else:
            Wz = numpy_helper.to_array(inits[wz]).astype(np.float32) if wz else np.zeros((), np.float32)
            if per_channel:
                Ws, Wz = Ws.reshape(1, -1), (Wz.reshape(1, -1) if Wz.ndim else Wz)
            W = ((Wq.astype(np.float32) - Wz) * Ws).astype(np.float32)
            new_inits.append(numpy_helper.from_array(W, wname))
            nodes = []
        nodes.append(helper.make_node("MatMul", [x, wname], [y], name=mm.name + "_f"))
        replace[id(mul)] = nodes
        remove.update(id(n) for n in (mm, cast, smul))
        n_rewritten += 1

    # rebuild the node list in place order: the new DQ/MatMul sit where the old final Mul was
    # (all their inputs are produced earlier), so topological order is preserved.
    nodes_out = []
    for n in g.node:
        if id(n) in replace:
            nodes_out.extend(replace[id(n)])
        elif id(n) not in remove:
            nodes_out.append(n)
    g.initializer.extend(new_inits)
    del g.node[:]
    g.node.extend(nodes_out)

    if mode in ("f32", "f16", "wdq16"):
        _fold_constants(m)
    fused = _fuse_matmul_add(m)
    # (only for static-batch graphs: in a dynamic one the Gathers are Shape plumbing)
    static_in = all(d.HasField("dim_value") for i in g.input for d in i.type.tensor_type.shape.dim)
    sliced = _gather_to_slice(m) if static_in else 0
    if mode in ("f16", "wdq16"):
        _to_f16(m)
    _dce(m)
    for op in m.opset_import:
        if op.domain in ("", "ai.onnx") and op.version < 13:
            op.version = 13     # DequantizeLinear with axis needs opset >= 13
        if mode == "wdq16" and op.domain in ("", "ai.onnx") and op.version < 19:
            op.version = 19     # DequantizeLinear with a float16 scale needs opset >= 19
            for n in g.node:    # opset 18: an equal Split must say how many outputs it has
                if n.op_type == "Split" and len(n.input) < 2 and not any(a.name == "num_outputs" for a in n.attribute):
                    n.attribute.append(helper.make_attribute("num_outputs", len(n.output)))
    onnx.checker.check_model(m, full_check=False)
    onnx.save(m, dst)
    return {"rewritten_matmuls": n_rewritten, "gemm_fused": fused, "gather_to_slice": sliced, "bytes": os.path.getsize(dst)}


def _light_infer(m):
    """Shape-inferred copy of ``m`` without the big weights (they become graph inputs), so
    inference stays cheap on a 1 GB float32 graph. Only the copy's value_info is useful."""
    import onnx
    from onnx import helper
    light = onnx.ModelProto()
    light.CopyFrom(m)
    lg = light.graph
    big = [i for i in lg.initializer if np.prod(i.dims or [1]) > 64]
    for i in big:
        lg.input.append(helper.make_tensor_value_info(i.name, i.data_type, list(i.dims)))
    small = [i for i in lg.initializer if np.prod(i.dims or [1]) <= 64]
    del lg.initializer[:]
    lg.initializer.extend(small)
    return onnx.shape_inference.infer_shapes(light)


def _fuse_matmul_add(m) -> int:
    """MatMul(x[..,K], W[K,N]) -> Add(bias[N]) becomes Reshape -> Gemm -> Reshape (or a bare
    Gemm for 2-D x), but only when x's shape is fully known (a static-batch graph).

    ORT's own MatMulAddFusion does the same rewrite at load time once shapes are static,
    but names the shape initializer "gemm_input_shape" for every instance; with two
    different K it dies at session creation ("Initializer with same name exists", seen on
    ORT 1.23.2 and 1.31). Doing it here with unique names sidesteps the bug, and Gemm with a
    constant B/C is what the CoreML EP wants anyway.
    """
    import onnx
    from onnx import TensorProto as TP, helper, numpy_helper
    g = m.graph
    light = _light_infer(m)
    shp = {}
    for v in list(light.graph.value_info) + list(light.graph.input):
        t = v.type.tensor_type
        if t.HasField("shape") and all(d.HasField("dim_value") for d in t.shape.dim):
            shp[v.name] = [d.dim_value for d in t.shape.dim]
    inits = {i.name: i for i in g.initializer}
    prod = {o: n for n in g.node for o in n.output}
    cons: dict[str, list] = {}
    for n in g.node:
        for i in n.input:
            cons.setdefault(i, []).append(n)

    def const_w(name):
        if name in inits:
            return len(inits[name].dims) == 2
        p = prod.get(name)
        return p is not None and p.op_type == "DequantizeLinear" and p.input[0] in inits \
            and len(inits[p.input[0]].dims) == 2

    repl: dict[int, list] = {}
    drop: set[int] = set()
    new_inits = []
    for mm in g.node:
        if mm.op_type != "MatMul" or mm.input[0] not in shp or not const_w(mm.input[1]):
            continue
        c = cons.get(mm.output[0], [])
        if len(c) != 1 or c[0].op_type != "Add" or mm.output[0] in {o.name for o in g.output}:
            continue
        add = c[0]
        bias = [i for i in add.input if i != mm.output[0]]
        if len(bias) != 1 or bias[0] not in inits or len(inits[bias[0]].dims) != 1:
            continue
        xs = shp[mm.input[0]]
        wd = inits[mm.input[1]].dims if mm.input[1] in inits else inits[prod[mm.input[1]].input[0]].dims
        K, N = int(wd[0]), int(wd[1])
        if xs[-1] != K or inits[bias[0]].dims[0] != N:
            continue
        tag = mm.name or mm.output[0]
        if len(xs) == 2:
            nodes = [helper.make_node("Gemm", [mm.input[0], mm.input[1], bias[0]], [add.output[0]], name=tag + "_gemm")]
        else:
            s_in, s_out = tag + "_flat_shape", tag + "_unflat_shape"
            new_inits.append(numpy_helper.from_array(np.array([-1, K], np.int64), s_in))
            new_inits.append(numpy_helper.from_array(np.array(xs[:-1] + [N], np.int64), s_out))
            nodes = [helper.make_node("Reshape", [mm.input[0], s_in], [tag + "_flat"], name=tag + "_reshape_in"),
                     helper.make_node("Gemm", [tag + "_flat", mm.input[1], bias[0]], [tag + "_gemm_out"], name=tag + "_gemm"),
                     helper.make_node("Reshape", [tag + "_gemm_out", s_out], [add.output[0]], name=tag + "_reshape_out")]
        repl[id(add)] = nodes
        drop.add(id(mm))
    if not repl:
        return 0
    out = []
    for n in g.node:
        if id(n) in repl:
            out.extend(repl[id(n)])
        elif id(n) not in drop:
            out.append(n)
    del g.node[:]
    g.node.extend(out)
    g.initializer.extend(new_inits)
    return len(repl)



def _gather_to_slice(m) -> int:
    """Gather(activation, scalar constant index, axis) -> Slice + Squeeze.

    The tower's q/k/v unpacking (and the attention-pool token pick) are Gathers with a
    scalar index, which CoreML's MLProgram format has no op for; Slice and Squeeze it does.
    Same numbers, since both just select one entry along ``axis``.
    """
    from onnx import helper, numpy_helper
    g = m.graph
    inits = {i.name: i for i in g.initializer}
    out, new_inits, n_done = [], [], 0
    for n in g.node:
        if (n.op_type == "Gather" and n.input[0] not in inits and n.input[1] in inits
                and not inits[n.input[1]].dims):
            idx = int(numpy_helper.to_array(inits[n.input[1]]))
            axis = next((a.i for a in n.attribute if a.name == "axis"), 0)
            tag = n.name or n.output[0]
            end = idx + 1 if idx != -1 else np.iinfo(np.int64).max
            for suffix, val in (("starts", [idx]), ("ends", [end]), ("axes", [axis])):
                new_inits.append(numpy_helper.from_array(np.array(val, np.int64), f"{tag}_{suffix}"))
            new_inits.append(numpy_helper.from_array(np.array([axis], np.int64), f"{tag}_sq_axes"))
            out.append(helper.make_node("Slice", [n.input[0], f"{tag}_starts", f"{tag}_ends", f"{tag}_axes"],
                                        [tag + "_slice"], name=tag + "_slice"))
            out.append(helper.make_node("Squeeze", [tag + "_slice", f"{tag}_sq_axes"], list(n.output), name=tag + "_squeeze"))
            n_done += 1
        else:
            out.append(n)
    del g.node[:]
    g.node.extend(out)
    g.initializer.extend(new_inits)
    return n_done


def _dce(m):
    """Drop nodes whose outputs are unused (e.g. the DynamicQuantizeLinear / scale Mul left
    behind by the rewrite) and initializers nobody reads."""
    g = m.graph
    live = {o.name for o in g.output}
    keep = []
    for n in reversed(g.node):
        if any(o in live for o in n.output):
            keep.append(n)
            live.update(i for i in n.input if i)
    keep.reverse()
    del g.node[:]
    g.node.extend(keep)
    inits = [i for i in g.initializer if i.name in live]
    del g.initializer[:]
    g.initializer.extend(inits)


def _fold_constants(m):
    """Fold Cast(initializer) and float Constant nodes into float32 initializers."""
    import onnx
    from onnx import numpy_helper
    g = m.graph
    inits = {i.name: i for i in g.initializer}
    out = []
    for n in g.node:
        if n.op_type == "Cast" and n.input[0] in inits and n.attribute[0].i == onnx.TensorProto.FLOAT:
            a = numpy_helper.to_array(inits[n.input[0]]).astype(np.float32)
            t = numpy_helper.from_array(a, n.output[0])
            g.initializer.append(t)
            inits[n.output[0]] = t
        elif n.op_type == "Constant" and n.attribute[0].name == "value" \
                and n.attribute[0].t.data_type in (onnx.TensorProto.FLOAT, onnx.TensorProto.FLOAT16):
            a = numpy_helper.to_array(n.attribute[0].t).astype(np.float32)
            t = numpy_helper.from_array(a, n.output[0])
            g.initializer.append(t)
            inits[n.output[0]] = t
        else:
            out.append(n)
    del g.node[:]
    g.node.extend(out)


def _to_f16(m, keep32=F16_KEEP32):
    """Convert a float32 graph (constants already folded) to half precision in place.

    Each float tensor is tracked as fp32 or fp16; Casts are inserted only where a producer
    and consumer disagree (around keep32 ops and at the graph boundary, where the declared
    float32 input/output types are preserved). Float initializers are converted per consumer.
    """
    import onnx
    from onnx import TensorProto as TP, helper, numpy_helper
    g = m.graph
    # element types of every tensor, from shape inference on a light copy (big weights → inputs)
    light = _light_infer(m)
    is_float = {v.name for v in list(light.graph.value_info) + list(light.graph.input) + list(light.graph.output)
                if v.type.tensor_type.elem_type == TP.FLOAT}
    inits = {i.name: i for i in g.initializer}
    is_float |= {n for n, i in inits.items() if i.data_type == TP.FLOAT}

    cur = {n: 32 for n in is_float}          # dtype currently carried by each float tensor
    cache: dict[tuple[str, int], str] = {}
    out_nodes, new_inits = [], []

    def get(name, want):
        if name not in is_float or cur[name] == want:
            return name
        key = (name, want)
        if key in cache:
            return cache[key]
        new = f"{name}__h" if want == 16 else f"{name}__f"
        if name in inits:       # initializer: convert the constant, no runtime Cast
            a = numpy_helper.to_array(inits[name]).astype(np.float16 if want == 16 else np.float32)
            new_inits.append(numpy_helper.from_array(a, new))
        else:
            out_nodes.append(helper.make_node("Cast", [name], [new], name=new + "_cast",
                                              to=TP.FLOAT16 if want == 16 else TP.FLOAT))
        cache[key] = new
        return new

    outputs = {o.name for o in g.output}
    for n in g.node:
        float_op32 = n.op_type in keep32 or (n.op_type == "Cast" and n.attribute[0].i == TP.FLOAT)
        passthrough = n.op_type in _F16_PASSTHROUGH or (n.op_type == "Cast" and n.attribute[0].i != TP.FLOAT)
        want = 32 if float_op32 else 16
        ins = [i if (passthrough or not i) else get(i, want) for i in n.input]
        node = onnx.NodeProto()
        node.CopyFrom(n)
        del node.input[:]
        node.input.extend(ins)
        post = []
        for k, o in enumerate(n.output):
            if o in is_float:
                cur[o] = want
                if want == 16 and o in outputs:
                    node.output[k] = o + "__h16"
                    cur[o + "__h16"] = 16
                    post.append(helper.make_node("Cast", [o + "__h16"], [o], name=o + "_out_cast", to=TP.FLOAT))
                    cur[o] = 32
        out_nodes.append(node)
        out_nodes.extend(post)
    # fp32 output from a keep32 producer needs nothing; fp16 ones were cast above.
    del g.node[:]
    g.node.extend(out_nodes)
    g.initializer.extend(new_inits)
    del g.value_info[:]


def _static(src: str, dst: str, batch: int = 1) -> dict:
    """Fix the graph's batch dimension and constant-fold the shape plumbing away.

    The towers were exported with a dynamic batch axis, so the head-dim attention scale and
    every Reshape target are computed at run time from Shape -> Gather -> Unsqueeze -> Concat
    (Sqrt/Div/Cast/Where/Equal on top). Those tiny int64 nodes cannot be placed on a GPU EP,
    so each transformer block bounces to the CPU and back. With a static input the whole
    subgraph is constant, and ORT's *basic*-level optimisation (constant folding, identity /
    redundant-node removal; EP-independent, unlike the extended level that fuses CPU-only
    contrib ops) removes it. The optimised model is saved with the CPU EP and re-saved as is.
    """
    import onnx
    import onnxruntime as ort

    m = onnx.load(src)
    before = _op_counts(m)
    for vi in list(m.graph.input) + list(m.graph.output):
        d0 = vi.type.tensor_type.shape.dim[0]
        d0.Clear()
        d0.dim_value = int(batch)
    del m.graph.value_info[:]
    tmp = dst + ".pre"
    onnx.save(m, tmp)
    del m
    so = ort.SessionOptions()
    so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    so.optimized_model_filepath = dst
    ort.InferenceSession(tmp, so, providers=["CPUExecutionProvider"])
    os.remove(tmp)
    after = _op_counts(onnx.load(dst))
    plumbing = ("Shape", "Gather", "Unsqueeze", "Concat", "Slice", "Where", "Equal", "Mod", "Expand", "ConstantOfShape")
    return {"plumbing_before": {k: before.get(k, 0) for k in plumbing},
            "plumbing_after": {k: after.get(k, 0) for k in plumbing},
            "bytes": os.path.getsize(dst)}


def _op_counts(m) -> dict:
    c: dict[str, int] = {}
    for n in m.graph.node:
        c[n.op_type] = c.get(n.op_type, 0) + 1
    return c


def make_variant(src: str, dst: str, name: str) -> dict:
    """name = [s]{wdq|wdq16|f32|f16|dyn}; the 's' prefix applies the static-batch transform first
    (so 'sdyn' is the shipped int8 graph made static, 'swdq' / 'sf16' add the GPU rewrites)."""
    static = name.startswith("s")
    mode = name[1:] if static else name
    info: dict = {}
    cur = src
    if static:
        pre = dst + ".static"
        info["static"] = _static(src, pre)
        cur = pre
    if mode == "dyn":
        os.replace(cur, dst) if static else __import__("shutil").copy(cur, dst)
    else:
        info.update(_rewrite(cur, dst, mode))
        if static:
            os.remove(cur)
    import onnx
    onnx.checker.check_model(dst)       # path form: works past the 2 GB protobuf limit
    info["checker"] = "ok"
    info["bytes"] = os.path.getsize(dst)
    return info


# ── probing ──────────────────────────────────────────────────────────────────

def _inputs(sess, rng):
    feeds = {}
    for i in sess.get_inputs():
        shape = [d if isinstance(d, int) and d > 0 else 1 for d in i.shape]
        t = i.type
        if "float16" in t:
            feeds[i.name] = rng.standard_normal(shape).astype(np.float16)
        elif "float" in t:
            feeds[i.name] = (rng.standard_normal(shape) * 0.5).astype(np.float32)
        elif "int64" in t:
            feeds[i.name] = rng.integers(0, 100, size=shape).astype(np.int64)
        else:
            feeds[i.name] = np.zeros(shape, dtype=np.int32)
    return feeds


def _cos(a, b):
    a, b = np.asarray(a, np.float64).ravel(), np.asarray(b, np.float64).ravel()
    return float(a @ b / max(np.linalg.norm(a) * np.linalg.norm(b), 1e-12))


def probe_one(path, providers, runs, ref=None):
    from kestrel_analyzer.ml import providers as P
    P.clear()
    t0 = time.perf_counter()
    try:
        sess = P.build_session(path, providers, kind=os.path.basename(path))
    except Exception as e:  # noqa: BLE001 - we want the message in the report
        return {"error": f"{type(e).__name__}: {e}"[:600]}
    load_s = time.perf_counter() - t0
    rec = (P.placement_summary() or [{}])[-1]
    rng = np.random.default_rng(0)
    feeds = _inputs(sess, rng)
    out, times = None, []
    try:
        for _ in range(runs):
            t = time.perf_counter()
            out = sess.run(None, feeds)[0]
            times.append(time.perf_counter() - t)
    except Exception as e:  # noqa: BLE001
        return {"error": f"run: {type(e).__name__}: {e}"[:600], "nodes": rec.get("nodes"), "actual": rec.get("actual")}
    res = {"load_s": round(load_s, 2), "first_run_ms": round(times[0] * 1000, 1),
           "median_ms": round(float(np.median(times[1:] if len(times) > 1 else times)) * 1000, 1),
           "actual": rec.get("actual"), "nodes": rec.get("nodes")}
    if ref is not None:
        res["cos_vs_cpu"] = round(_cos(out, ref), 5)
    return res, out


def _worker(a):
    """One probe in a child process, so a hung EP compile can be timed out by the parent."""
    cfgs = dict(provider_configs()[0])
    ref = np.load(a.ref) if a.ref and os.path.exists(a.ref) else None
    r = probe_one(a.worker, cfgs[a.cfg], a.runs, ref)
    if isinstance(r, tuple):
        r, out = r
        if a.save_ref:
            np.save(a.save_ref, out)
    json.dump(r, open(a.result, "w"))


def _probe_isolated(path, cname, runs, ref_path, save_ref, timeout):
    import subprocess
    res_path = tempfile.mktemp(suffix=".json")
    cmd = [sys.executable, os.path.abspath(__file__), "--worker", path, "--cfg", cname, "--runs", str(runs),
           "--result", res_path]
    if ref_path:
        cmd += ["--ref", ref_path]
    if save_ref:
        cmd += ["--save-ref", save_ref]
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, timeout=timeout, capture_output=True, text=True, errors="replace")
    except subprocess.TimeoutExpired:
        return {"error": f"timeout after {timeout}s (session build or inference hung)"}
    if not os.path.exists(res_path):
        tail = (p.stderr or "")[-400:].replace("\n", " | ")
        return {"error": f"worker exited {p.returncode} after {time.perf_counter() - t0:.0f}s: {tail}"}
    r = json.load(open(res_path))
    os.remove(res_path)
    return r


def main():
    import onnxruntime as ort
    ap = argparse.ArgumentParser()
    ap.add_argument("--worker", help=argparse.SUPPRESS)
    ap.add_argument("--cfg", help=argparse.SUPPRESS)
    ap.add_argument("--ref", help=argparse.SUPPRESS)
    ap.add_argument("--save-ref", help=argparse.SUPPRESS)
    ap.add_argument("--result", help=argparse.SUPPRESS)
    ap.add_argument("--timeout", type=int, default=600, help="seconds per (model, variant, config) probe")
    ap.add_argument("--out", default="ep_probe.json")
    # The shipped towers are already static batch-1 (== sdyn), so the default probes the
    # float candidates for CoreML next to them. swdq is not a CoreML candidate (no
    # DequantizeLinear in MLProgram); add it explicitly if wanted.
    ap.add_argument("--variants", default="sf16,sf32")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--only", default="", help="comma list of model kinds")
    a = ap.parse_args()
    if a.worker:
        return _worker(a)
    cfgs, avail = provider_configs()
    report = {"platform": platform.platform(), "machine": platform.machine(), "python": sys.version.split()[0],
              "onnxruntime": ort.__version__, "available_providers": avail, "results": {}}
    tmp = tempfile.mkdtemp()
    only = {s for s in a.only.split(",") if s}
    for kind, (rel, is_tower) in MODELS.items():
        if only and kind not in only:
            continue
        src = str(ROOT / rel)
        if not os.path.isfile(src) or os.path.getsize(src) < 10_000:
            report["results"][kind] = {"error": "missing or LFS pointer"}
            continue
        variants = {"dyn" if is_tower else "as_shipped": src}
        if is_tower:
            for v in a.variants.split(","):
                if v in VARIANT_NAMES:
                    dst = os.path.join(tmp, f"{kind}_{v}.onnx")
                    try:
                        info = make_variant(src, dst, v)
                        variants[v] = dst
                        report.setdefault("variant_info", {})[f"{kind}:{v}"] = info
                    except Exception as e:  # noqa: BLE001
                        report.setdefault("variant_info", {})[f"{kind}:{v}"] = {"error": f"{type(e).__name__}: {e}"}
        ref_path = os.path.join(tmp, f"{kind}_ref.npy")
        rk = report["results"].setdefault(kind, {})
        for vname, vpath in variants.items():
            for cname, _provs in cfgs:
                key = f"{vname}/{cname}"
                first = not os.path.exists(ref_path)
                # the shipped file on CPU (probed first) is the reference for every variant
                r = _probe_isolated(vpath, cname, a.runs, None if first else ref_path,
                                    ref_path if first and cname == "cpu" else None, a.timeout)
                if first and cname == "cpu" and "error" not in r:
                    r["cos_vs_cpu"] = 1.0
                rk[key] = r
                print(f"{kind:16s} {key:28s} {json.dumps(r)}", flush=True)
                json.dump(report, open(a.out, "w"), indent=1)  # partial results survive a job timeout
        for v in variants.values():
            if v != src:
                try:
                    os.remove(v)
                except OSError:
                    pass
    json.dump(report, open(a.out, "w"), indent=1)
    print("wrote", a.out)


if __name__ == "__main__":
    main()
