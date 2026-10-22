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
   are dynamically quantised int8 graphs (DynamicQuantizeLinear +
   MatMulInteger). CoreML has no kernels for those ops, so on macOS a GPU
   session splits the graph into many CoreML/CPU partitions and copies
   activations back and forth on every layer -- usually slower than plain
   CPU. DirectML implements both ops. Default ``auto`` therefore pins the
   towers to CPU on macOS/CoreML and lets them use the GPU on Windows/DirectML
   (the detector and SAM-HQ are unaffected and always follow the GPU
   setting). Override with the ``KESTREL_TOWER_DEVICE`` environment variable
   or the ``tower_device`` setting: ``auto`` | ``cpu`` | ``gpu``.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import threading
from typing import Dict, List, Optional

TOWER_KINDS = frozenset({"bird_species", "siglip_image"})
TOWER_DEVICES = ("auto", "cpu", "gpu")

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


def parse_placement(log_text: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for ep, n in _PLACED_RE.findall(log_text or ""):
        out[ep] = out.get(ep, 0) + int(n)
    return out


def _is_ort_log_line(line: str) -> bool:
    return ":onnxruntime:" in line and ("[V:" in line or "[I:" in line)


def build_session(model_path: str, providers: List[str], kind: str = ""):
    """``ort.InferenceSession`` + placement record. Falls back to a plain build
    whenever the stderr capture cannot be set up (no fd 2, frozen GUI app...)."""
    import onnxruntime as ort

    if not _diag_enabled():
        return ort.InferenceSession(model_path, providers=providers)
    so = ort.SessionOptions()
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
                    captured = tmp.read().decode("utf-8", errors="replace")
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
    with _lock:
        _records.append(record)
        del _records[:-64]
    try:
        from ..logging_utils import debug
        nodes = ", ".join(f"{k.replace('ExecutionProvider', '')}={v}" for k, v in record["nodes"].items()) or "n/a"
        debug(
            f"[providers] {record['kind'] or record['model']}: requested={record['requested']} "
            f"actual={record['actual']} nodes: {nodes}"
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
