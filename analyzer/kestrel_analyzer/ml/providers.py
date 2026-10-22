"""ONNX Runtime execution-provider diagnostics and per-model device policy.

Two jobs:

1. **Placement diagnostics.** For every ``ResilientOnnxSession`` built, record
   the providers that were *requested*, the providers ORT *actually* enabled,
   and how many graph nodes landed on each EP. ORT only reports node
   placement in its VERBOSE session log (``session_state.cc ... Node
   placements`` / ``Node(s) placed on [XExecutionProvider]. Number of nodes:
   N``), which goes to the C stderr stream, so the build is wrapped in an
   fd-level capture of stderr and the summary lines are parsed. For EPs that
   compile subgraphs (CoreML, DirectML partitions) a count is the number of
   *fused partitions* plus any loose nodes ORT assigned to that EP, so "many
   nodes on CPU next to a GPU EP" is the signature of a split graph that
   ping-pongs between devices. Each record is logged once
   (``[providers] ...``) and ``placement_summary()`` returns them all for the
   analysis log (``models_loaded`` event). Disable with
   ``KESTREL_ORT_PLACEMENT_DIAG=0``.

2. **Transformer-tower device policy.** BioCLIP 2 and SigLIP 2 image towers
   ship as two graphs over ONE shared external-data file of int8 weights
   (``tools/build_gpu_towers.py``):

   * ``*_int8.onnx`` -- dynamically quantised int8 (DynamicQuantizeLinear +
     MatMulInteger). Fast on CPU (BioCLIP ~125 ms), terrible on DirectML
     (13 s per crop: DML's integer kernels) and split into many CPU/CoreML
     partitions on CoreML (no kernels for those ops).
   * ``*_wdq16.onnx`` -- the same int8 weights behind DequantizeLinear with
     fp16 scales, the rest of the graph fp16 (fp32 I/O). Loaded with
     ``session.disable_quant_qdq=1`` ORT folds the DequantizeLinear nodes into
     fp16 weights at session creation: BioCLIP 52 ms / SigLIP 10 ms on an Arc
     140V. Slow on CPU (~800 ms), and CoreML places almost none of an fp16
     graph (macOS CI probe), so it is a DirectML-only graph.

   ``tower_graph`` picks the GPU graph when the session's providers include an
   EP in ``GPU_GRAPH_EPS`` (DirectML), else the int8 graph; ResilientOnnxSession
   falls back to the int8 graph on CPU if the GPU session cannot be built.
   Default ``auto`` therefore runs the towers on the GPU on Windows/DirectML and
   pins them to CPU on macOS/CoreML (the detector and SAM-HQ are unaffected and
   always follow the GPU setting). Override with the ``KESTREL_TOWER_DEVICE``
   environment variable or the ``tower_device`` setting: ``auto`` | ``cpu`` |
   ``gpu``. ``gpu`` on CoreML still means the int8 graph on CoreML (split);
   a CoreML-friendly tower (fp32-folded wdq graph + ModelCacheDirectory) is a
   follow-up that needs real Apple hardware.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

TOWER_KINDS = frozenset({"bird_species", "siglip_image"})
# Towers pinned to the CPU int8 graph on every machine. SigLIP's crop embedding IS the
# default quality score: its GPU (wdq16) graph is closer to fp32, but it moves ~12% of
# crops by one star against the CPU int8 graph (per-crop quantisation noise, not a shift
# a calibration refit could remove). Pinning it keeps stars identical across machines
# and between analysis and upgrade; SigLIP is ~44 ms/crop on CPU, small next to the
# detector. BioCLIP keeps its GPU graph (13 s -> 52 ms on DirectML; top-1 agrees 39/40).
CPU_PINNED_KINDS = frozenset({"siglip_image"})
TOWER_DEVICES = ("auto", "cpu", "gpu")

# EPs that get the fp16 "wdq16" tower graph instead of the dynamic-int8 one.
GPU_GRAPH_EPS = frozenset({"DmlExecutionProvider"})
# Lets ORT constant-fold the wdq16 graph's DequantizeLinear nodes at load
# (without it they run on every call: BioCLIP 242 ms instead of 52 ms on DML).
GPU_GRAPH_SESSION_CONFIG = {"session.disable_quant_qdq": "1"}
_MIN_GRAPH_BYTES = 100_000  # LFS pointer stubs are ~130 bytes

_PLACED_RE = re.compile(r"placed on \[(\w+)\]\. Number of nodes: (\d+)")

_lock = threading.Lock()
_records: List[dict] = []


def _diag_enabled() -> bool:
    return os.environ.get("KESTREL_ORT_PLACEMENT_DIAG", "1").strip().lower() not in {"0", "false", "no", "off"}


def tower_device_setting() -> str:
    env = os.environ.get("KESTREL_TOWER_DEVICE", "").strip().lower()
    if env in TOWER_DEVICES:
        return env
    try:
        try:
            from ...settings_utils import load_persisted_settings  # type: ignore
        except (ImportError, ValueError):
            from settings_utils import load_persisted_settings  # type: ignore
        v = str((load_persisted_settings() or {}).get("tower_device", "auto")).strip().lower()
        return v if v in TOWER_DEVICES else "auto"
    except Exception:
        return "auto"


def tower_use_gpu(gpu_ep: str, device: Optional[str] = None) -> bool:
    """Whether a transformer tower should request the GPU EP (when the run is on GPU)."""
    d = device or tower_device_setting()
    if d == "cpu":
        return False
    if d == "gpu":
        return True
    return gpu_ep != "CoreMLExecutionProvider"


def _ep_name(p) -> str:
    return str(p[0]) if isinstance(p, (tuple, list)) else str(p)


def _usable(path) -> bool:
    try:
        return path is not None and Path(path).is_file() and Path(path).stat().st_size > _MIN_GRAPH_BYTES
    except OSError:
        return False


def tower_graph(providers: list, cpu_path, gpu_path) -> Tuple[str, list, Dict[str, str]]:
    """``(model_path, providers, session_config)`` for a transformer-tower session.

    A provider list containing a ``GPU_GRAPH_EPS`` EP gets the GPU graph plus
    ``GPU_GRAPH_SESSION_CONFIG``; if that graph is missing (e.g. an LFS stub)
    the tower runs the int8 graph on CPU rather than the int8 graph on DirectML
    (13 s per crop). Every other list (CPU, CoreML) gets the int8 graph unchanged.
    """
    if any(_ep_name(p) in GPU_GRAPH_EPS for p in providers):
        if _usable(gpu_path):
            return str(gpu_path), list(providers), dict(GPU_GRAPH_SESSION_CONFIG)
        return str(cpu_path), ["CPUExecutionProvider"], {}
    return str(cpu_path), list(providers), {}


def parse_placement(log_text: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for ep, n in _PLACED_RE.findall(log_text or ""):
        out[ep] = out.get(ep, 0) + int(n)
    return out


def _decode_ort_log(raw: bytes) -> str:
    """ORT writes its log to fd 2 as UTF-16LE on Windows; every character we parse is ASCII,
    so decoding as UTF-8 and dropping the NULs handles both encodings (and a mix of them)."""
    return raw.decode("utf-8", errors="replace").replace("\x00", "")


def _is_ort_log_line(line: str) -> bool:
    return ":onnxruntime:" in line and ("[V:" in line or "[I:" in line)


def build_session(model_path: str, providers: List[str], kind: str = "",
                  config: Optional[Dict[str, str]] = None):
    """``ort.InferenceSession`` + placement record. Falls back to a plain build
    whenever the stderr capture cannot be set up (no fd 2, frozen GUI app...).
    ``config`` = session config entries (``SessionOptions.add_session_config_entry``)."""
    import onnxruntime as ort

    so = ort.SessionOptions()
    for k, v in (config or {}).items():
        so.add_session_config_entry(str(k), str(v))
    if not _diag_enabled():
        return ort.InferenceSession(model_path, sess_options=so, providers=providers)
    so.log_severity_level = 0
    so.log_verbosity_level = 0
    captured = ""
    sess = None
    with _lock:
        saved = None
        tmp = None
        try:
            sys.stderr.flush()
        except Exception:
            pass
        try:
            tmp = tempfile.TemporaryFile(mode="w+b")
            saved = os.dup(2)
            os.dup2(tmp.fileno(), 2)
        except Exception:
            if saved is not None:
                try:
                    os.dup2(saved, 2)
                    os.close(saved)
                except Exception:
                    pass
            saved = None
        try:
            sess = ort.InferenceSession(model_path, sess_options=so, providers=providers)
        finally:
            if saved is not None:
                try:
                    os.dup2(saved, 2)
                    os.close(saved)
                except Exception:
                    pass
                try:
                    tmp.seek(0)
                    captured = _decode_ort_log(tmp.read())
                except Exception:
                    captured = ""
            if tmp is not None:
                try:
                    tmp.close()
                except Exception:
                    pass
    # Re-emit anything that is not ORT's verbose/info chatter (warnings, other
    # threads' prints that landed in the capture window).
    if captured:
        rest = [ln for ln in captured.splitlines() if ln.strip() and not _is_ort_log_line(ln)]
        if rest:
            try:
                sys.stderr.write("\n".join(rest) + "\n")
            except Exception:
                pass
    try:
        actual = [str(p) for p in sess.get_providers()]
    except Exception:
        actual = []
    record = {
        "kind": str(kind),
        "model": os.path.basename(str(model_path)),
        "requested": list(providers),
        "actual": actual,
        "nodes": parse_placement(captured),
    }
    if config:
        record["config"] = dict(config)
    with _lock:
        _records.append(record)
        del _records[:-64]
    try:
        from ..logging_utils import debug
        nodes = ", ".join(f"{k.replace('ExecutionProvider', '')}={v}" for k, v in record["nodes"].items()) or "n/a"
        debug(
            f"[providers] {record['kind'] or record['model']} ({record['model']}): "
            f"requested={record['requested']} actual={record['actual']} nodes: {nodes}"
            + (f" config={record['config']}" if config else "")
        )
    except Exception:
        pass
    return sess


def placement_summary() -> List[dict]:
    with _lock:
        return [dict(r) for r in _records]


def clear() -> None:
    with _lock:
        _records.clear()
