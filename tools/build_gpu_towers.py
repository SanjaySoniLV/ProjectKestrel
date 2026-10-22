"""Build the shared-weight CPU + GPU layouts of the BioCLIP 2 / SigLIP 2 image towers.

    python tools/build_gpu_towers.py [--out-dir DIR] [--only bioclip,siglip] [--verify]

Needs ``onnx`` and ``onnxruntime`` (the dev venv). Input is the current tower in
``analyzer/models/<tower>/`` -- either the original single-file int8 graph or a
layout this script wrote earlier (external data is loaded either way), so the
script is idempotent. For each tower it writes, into ``--out-dir`` (default: the
tower's own folder):

  <stem>_int8.onnx    CPU graph: the shipped dynamic-int8 structure (DynamicQuantizeLinear
                      + MatMulInteger), unchanged node for node; its int8 weights,
                      int8 zero points and fp32 weight scales live in <stem>.data.
  <stem>_wdq16.onnx   GPU graph ("swdq16"): every MatMulInteger pattern becomes
                      DequantizeLinear(int8 W, fp16 scale, int8 zp) -> MatMul/Gemm in fp16;
                      the rest of the graph is fp16 with float32 I/O (LayerNorm / Softmax /
                      Div / Erf ... kept in fp32, see tools/ep_probe.py's f16 mode). Opset 19
                      (fp16 DequantizeLinear scales), Split nodes use ``num_outputs``.
                      Its int8 weights and zero points point at the SAME <stem>.data
                      (same offsets); its fp16 scales and fp16 biases / LayerNorm params are
                      small and stay inline.
  <stem>.data         the shared external data (int8 weights, zero points, fp32 scales).

Load the GPU graph with ``so.add_session_config_entry("session.disable_quant_qdq", "1")``:
ORT then constant-folds every DequantizeLinear into an fp16 weight at session creation
(+0.5-0.9 s), which is what makes it fast on DirectML (BioCLIP 52 ms vs 13.3 s for the
int8 graph on an Arc 140V). Without the entry the DequantizeLinear nodes run per call.

``--verify`` checks both graphs with onnx.checker, loads them with onnxruntime (GPU graph
with and without the config entry), confirms the folded GPU session has no
DequantizeLinear left (optimised model at BASIC level) and that the new CPU graph gives
bit-identical output to the input graph.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

TOWERS = {
    # name: (folder, stem)
    "bioclip": ("analyzer/models/bioclip", "bioclip2_image"),
    "siglip": ("analyzer/models/siglip", "siglip2_b16_image"),
}
GPU_OPSET = 19
DATA_ALIGN = 64          # every tensor
DATA_ALIGN_BIG = 4096    # tensors >= 1 MiB: page-aligned (ORT mmaps external data; it rounds offsets down itself)


def _quant_tensor_names(m) -> set[str]:
    """int8 initializers (MatMulInteger weights + zero points) and the float weight scales
    multiplied onto DynamicQuantizeLinear's activation scale."""
    from onnx import TensorProto as TP
    g = m.graph
    inits = {i.name: i for i in g.initializer}
    names = {n for n, i in inits.items() if i.data_type in (TP.INT8, TP.UINT8)}
    dq_scales = {n.output[1] for n in g.node if n.op_type == "DynamicQuantizeLinear"}
    for n in g.node:
        if n.op_type == "Mul" and any(i in dq_scales for i in n.input):
            names.update(i for i in n.input if i in inits)
    return names


def _write_shared_data(m, names: set[str], data_path: str) -> dict[str, tuple[int, int, bytes]]:
    """Write the named initializers of ``m`` into ``data_path``; return name -> (offset, length, bytes)."""
    from onnx import numpy_helper
    out: dict[str, tuple[int, int, bytes]] = {}
    with open(data_path, "wb") as f:
        for t in m.graph.initializer:
            if t.name not in names:
                continue
            raw = numpy_helper.to_array(t).tobytes()
            align = DATA_ALIGN_BIG if len(raw) >= (1 << 20) else DATA_ALIGN
            pos = f.tell()
            pad = (-pos) % align
            if pad:
                f.write(b"\0" * pad)
                pos += pad
            f.write(raw)
            out[t.name] = (pos, len(raw), raw)
    return out


def _externalize(m, table: dict[str, tuple[int, int, bytes]], location: str) -> int:
    """Point every initializer of ``m`` whose name AND bytes match ``table`` at the shared file."""
    from onnx import TensorProto as TP, numpy_helper
    n = 0
    for t in m.graph.initializer:
        ent = table.get(t.name)
        if ent is None:
            continue
        off, length, raw = ent
        if numpy_helper.to_array(t).tobytes() != raw:
            raise ValueError(f"{t.name}: bytes differ from the shared copy")
        dims, dtype, name = list(t.dims), t.data_type, t.name
        t.Clear()
        t.name, t.data_type = name, dtype
        t.dims.extend(dims)
        t.data_location = TP.EXTERNAL
        for k, v in (("location", location), ("offset", str(off)), ("length", str(length))):
            e = t.external_data.add()
            e.key, e.value = k, v
        n += 1
    return n


def _save_proto(m, path: str) -> None:
    # Plain serialisation: onnx.save would try to (re)write external tensors.
    with open(path, "wb") as f:
        f.write(m.SerializeToString())


def _split_num_outputs(m) -> int:
    """Opset 18+: Split with an all-equal constant ``split`` input -> ``num_outputs``."""
    from onnx import helper, numpy_helper
    inits = {i.name: i for i in m.graph.initializer}
    done = 0
    for n in m.graph.node:
        if n.op_type != "Split" or any(a.name == "num_outputs" for a in n.attribute):
            continue
        if len(n.input) > 1 and n.input[1]:
            s = inits.get(n.input[1])
            if s is None:
                continue
            sizes = numpy_helper.to_array(s)
            if len(set(sizes.tolist())) != 1 or len(sizes) != len(n.output):
                continue        # unequal splits keep the explicit split input
            del n.input[1:]
        n.attribute.append(helper.make_attribute("num_outputs", len(n.output)))
        done += 1
    return done


def build_wdq16(src: str, tmp_dir: str):
    """swdq16 ModelProto (all weights inline) from a static batch-1 dynamic-int8 tower."""
    import onnx
    import ep_probe as E

    wdq = os.path.join(tmp_dir, "wdq.onnx")
    info = E._rewrite(src, wdq, "wdq")      # MatMulInteger -> DequantizeLinear + MatMul (+ Gemm fusion)
    m = onnx.load(wdq)
    os.remove(wdq)
    E._fold_constants(m)                     # Cast(fp16 initializer) -> fp32 initializer
    E._to_f16(m)                             # DQ scales become fp16 initializers -> fp16 DQ output
    for op in m.opset_import:
        if op.domain in ("", "ai.onnx"):
            op.version = max(op.version, GPU_OPSET)
    m.ir_version = max(m.ir_version, 9)
    info["split_num_outputs"] = _split_num_outputs(m)
    E._dce(m)                                # also drops the now-unused Split size constants
    n_dq = sum(1 for n in m.graph.node if n.op_type == "DequantizeLinear")
    info["dequantize_nodes"] = n_dq
    return m, info


def build_tower(name: str, out_dir: str | None, tmp_dir: str) -> dict:
    import onnx
    folder, stem = TOWERS[name]
    src_dir = ROOT / folder
    src = str(src_dir / f"{stem}_int8.onnx")
    out = Path(out_dir) if out_dir else src_dir
    out.mkdir(parents=True, exist_ok=True)
    data_name = f"{stem}.data"
    cpu_path, gpu_path, data_path = str(out / f"{stem}_int8.onnx"), str(out / f"{stem}_wdq16.onnx"), str(out / data_name)

    # The input may itself use <stem>.data; read everything before writing anything.
    single = os.path.join(tmp_dir, f"{stem}_src.onnx")
    m_cpu = onnx.load(src)
    onnx.save(m_cpu, single)                 # single-file copy for the rewrite + verification
    m_gpu, info = build_wdq16(single, tmp_dir)

    names = _quant_tensor_names(m_cpu)
    tmp_data = data_path + ".tmp"
    table = _write_shared_data(m_cpu, names, tmp_data)
    info["shared_tensors"] = len(table)
    info["cpu_external"] = _externalize(m_cpu, table, data_name)
    info["gpu_external"] = _externalize(m_gpu, table, data_name)
    os.replace(tmp_data, data_path)
    _save_proto(m_cpu, cpu_path)
    _save_proto(m_gpu, gpu_path)
    for p in (cpu_path, gpu_path):
        onnx.checker.check_model(p)          # path form: also resolves the external data
    info["bytes"] = {os.path.basename(p): os.path.getsize(p) for p in (cpu_path, gpu_path, data_path)}
    info["src_single_file"] = single
    info["paths"] = {"cpu": cpu_path, "gpu": gpu_path, "data": data_path}
    return info


def _session(path, qdq_fold: bool, optimized_out: str | None = None):
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.log_severity_level = 3
    if qdq_fold:
        so.add_session_config_entry("session.disable_quant_qdq", "1")
    if optimized_out:
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
        so.optimized_model_filepath = optimized_out
    return ort.InferenceSession(path, so, providers=["CPUExecutionProvider"])


def verify(info: dict, tmp_dir: str) -> dict:
    import onnx
    res = {}
    x = np.random.default_rng(0).standard_normal((1, 3, 224, 224)).astype(np.float32)
    ref = _session(info["src_single_file"], False).run(None, {"image": x})[0]
    cpu = _session(info["paths"]["cpu"], False).run(None, {"image": x})[0]
    res["cpu_identical_to_source"] = bool(np.array_equal(ref, cpu))

    def cos(a, b):
        a, b = a.ravel().astype(np.float64), b.ravel().astype(np.float64)
        return float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b)))

    for fold in (False, True):
        g = _session(info["paths"]["gpu"], fold).run(None, {"image": x})[0]
        res[f"gpu_cos_vs_cpu{'_folded' if fold else ''}"] = round(cos(g, ref), 6)
    opt = os.path.join(tmp_dir, "gpu_opt.onnx")
    _session(info["paths"]["gpu"], True, opt)
    mo = onnx.load(opt, load_external_data=False)
    res["folded_dequantize_nodes_left"] = sum(1 for n in mo.graph.node if n.op_type == "DequantizeLinear")
    for f in os.listdir(tmp_dir):
        if f.startswith("gpu_opt"):
            os.remove(os.path.join(tmp_dir, f))
    return res


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", default="", help="write here instead of over analyzer/models/<tower>/")
    ap.add_argument("--only", default="bioclip,siglip")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    for name in [s for s in a.only.split(",") if s]:
        tmp = tempfile.mkdtemp(prefix=f"gputower_{name}_")
        try:
            od = os.path.join(a.out_dir, name) if a.out_dir else None
            info = build_tower(name, od, tmp)
            if a.verify:
                info["verify"] = verify(info, tmp)
            info.pop("src_single_file", None)
            print(name, info, flush=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
