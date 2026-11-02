"""Opt-in "enhanced GPU performance" mode: vendor execution providers via Windows ML.

DirectML stays the default. When the user runs the setup action, this module checks the
platform, enumerates the hardware, lets Windows download the vendor execution provider (EP)
for it (OpenVINO for Intel, MIGraphX / VitisAI for AMD, TensorRT-RTX for NVIDIA), and
validates each routable model on that EP in a child process (``enhanced_validate``). Only
models whose outputs match DirectML and that run faster are routed to the vendor EP.

Everything here is optional and fails soft: importing this module never imports Windows ML,
and every failure leaves the pipeline on DirectML. The Cloud Compute service vendors this
package, so nothing at import time may require Windows ML or Windows.

Routing: ``vendor_spec_for(kind)`` is consulted by ``ResilientOnnxSession`` before the
normal provider list. The detector, SAM-HQ encoder and BioCLIP tower may go to the vendor
EP; the SAM-HQ decoder and the legacy quality model stay on DirectML, SigLIP stays on CPU,
and NPUs are never used (see the benchmark: crashes and slower than the GPU).
"""

from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

# Models that may run on the vendor EP when they pass validation.
ROUTABLE_KINDS = ("detector", "sam_enc", "bird_species")
# Validation thresholds (ML time only, steady state after warm-up).
MIN_COSINE = 0.9999
MIN_SPEEDUP = 1.5
WARMUP_RUNS = 3
TIMED_RUNS = 5

MIN_WINDOWS_BUILD = 26100  # Windows 11 24H2: needed to download vendor EPs through Windows ML
SETTING_ENABLED = "enhanced_gpu_enabled"
STATE_VERSION = 1
# Unclean exits (e.g. an access violation inside a vendor EP) with the mode active before
# the mode switches itself off.
MAX_UNCLEAN_EXITS = 2
# Repeated runtime failures of one vendor model before it is routed back to DirectML for good.
MAX_RUNTIME_FAILURES = 2

# EPs Windows ML can offer that are never used as the vendor GPU EP: ORT's own, DirectML
# (the default), and the experimental WebGPU EP (not benchmarked).
_NON_VENDOR_EPS = frozenset({"CPUExecutionProvider", "DmlExecutionProvider", "WebGpuExecutionProvider"})
_VENDOR_LABELS = {
    "OpenVINOExecutionProvider": "Intel (OpenVINO)",
    "MIGraphXExecutionProvider": "AMD (MIGraphX)",
    "VitisAIExecutionProvider": "AMD (Vitis AI)",
    "NvTensorRTRTXExecutionProvider": "NVIDIA (TensorRT-RTX)",
}
# Only the Intel OpenVINO EP has been measured; the others are tried on request and fall back
# to DirectML if validation fails. Informational (shown in the UI and logged).
MEASURED_EPS = frozenset({"OpenVINOExecutionProvider"})

ProgressFn = Callable[[str, str], None]  # (stage, message)


def _log(level: str, msg: str) -> None:
    try:
        from ..logging_utils import debug, info, warn
        {"info": info, "warn": warn}.get(level, debug)(f"[enhanced-gpu] {msg}")
    except Exception:
        pass


# --------------------------------------------------------------------------- platform

def _is_windows() -> bool:
    return sys.platform == "win32"


def windows_build() -> int:
    try:
        return int(sys.getwindowsversion().build)  # type: ignore[attr-defined]
    except Exception:
        return 0


def has_package_identity() -> bool:
    """True inside an MSIX / Store-packaged process (Windows only)."""
    if not _is_windows():
        return False
    try:
        import ctypes
        length = ctypes.c_uint32(0)
        # APPMODEL_ERROR_NO_PACKAGE (15700) = unpackaged; ERROR_INSUFFICIENT_BUFFER (122) = packaged.
        rc = ctypes.windll.kernel32.GetCurrentPackageFullName(ctypes.byref(length), None)  # type: ignore[attr-defined]
        return rc != 15700
    except Exception:
        return False


def platform_status() -> Dict[str, Any]:
    """Whether the mode can be offered here: ``{available, reason, build, packaged}``."""
    if not _is_windows():
        return {"available": False, "reason": "Only available on Windows.", "build": 0, "packaged": False}
    import platform as _platform
    build = windows_build()
    packaged = has_package_identity()
    if _platform.machine().upper() not in {"AMD64", "X86_64", "ARM64"}:
        return {"available": False, "reason": "This processor type is not supported.", "build": build,
                "packaged": packaged}
    if build < MIN_WINDOWS_BUILD:
        return {"available": False, "build": build, "packaged": packaged,
                "reason": f"Requires Windows 11 version 24H2 or later (this is build {build})."}
    try:
        import importlib.util
        ok = (importlib.util.find_spec("winui3") is not None)
    except Exception:
        ok = False
    if not ok:
        return {"available": False, "build": build, "packaged": packaged,
                "reason": "This build of Kestrel does not include Windows ML support."}
    return {"available": True, "reason": "", "build": build, "packaged": packaged}


# --------------------------------------------------------------------------- state files

def _user_dir() -> Path:
    try:
        try:
            from ...settings_utils import _get_user_data_dir  # type: ignore
        except (ImportError, ValueError):
            from settings_utils import _get_user_data_dir  # type: ignore
        return Path(_get_user_data_dir())
    except Exception:
        return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "ProjectKestrel"


def state_path() -> Path:
    return _user_dir() / "enhanced_gpu.json"


def _marker_path() -> Path:
    return _user_dir() / "enhanced_gpu.running"


def _read_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        _log("warn", f"could not write {path.name}: {e}")


def load_state() -> Optional[dict]:
    st = _read_json(state_path())
    return st if st and st.get("version") == STATE_VERSION else None


def save_state(state: dict) -> None:
    state = dict(state, version=STATE_VERSION)
    _write_json(state_path(), state)


def _pipeline_version() -> str:
    try:
        from ..config import PIPELINE_VERSION
        return str(PIPELINE_VERSION)
    except Exception:
        return ""


def _ort_version() -> str:
    try:
        import onnxruntime as ort
        return str(ort.__version__)
    except Exception:
        return ""


def user_enabled() -> bool:
    """The user's toggle (a persisted setting; default off)."""
    env = os.environ.get("KESTREL_ENHANCED_GPU", "").strip().lower()
    if env in {"0", "false", "off", "no"}:
        return False
    try:
        try:
            from ...settings_utils import load_persisted_settings  # type: ignore
        except (ImportError, ValueError):
            from settings_utils import load_persisted_settings  # type: ignore
        return bool((load_persisted_settings() or {}).get(SETTING_ENABLED, False))
    except Exception:
        return False


# --------------------------------------------------------------------------- runtime registration

_rt_lock = threading.RLock()
_boot_ctx: Any = None
_registered: Optional[Dict[str, str]] = None  # EP name -> library path (None = not attempted)
_rt_error: Optional[str] = None


def _import_winml():
    import winui3.microsoft.windows.applicationmodel.dynamicdependency.bootstrap as bootstrap
    import winui3.microsoft.windows.ai.machinelearning as winml
    return bootstrap, winml


def _bootstrap(bootstrap) -> None:
    """Make the Windows App SDK runtime usable in this process.

    Unpackaged: ``initialize`` finds the installed runtime (and shows Microsoft's installer UI
    if it is missing). Packaged (MSIX): the runtime must already be a dependency of the package;
    ``ON_PACKAGE_IDENTITY_NOOP`` makes the call a no-op there instead of an error."""
    global _boot_ctx
    if _boot_ctx is not None:
        return
    opts = bootstrap.InitializeOptions
    if has_package_identity():
        flag = opts.ON_PACKAGE_IDENTITY_NOOP
    else:
        flag = opts.ON_NO_MATCH_SHOW_UI
    ctx = bootstrap.initialize(options=flag)
    ctx.__enter__()
    _boot_ctx = ctx


def ensure_runtime(download: bool = False, progress: Optional[ProgressFn] = None) -> Dict[str, str]:
    """Bootstrap Windows ML and register the catalog's EPs with ORT. Returns {ep name: library path}.

    ``download=False`` only registers EPs that are already installed (the normal path at analysis
    time: it never triggers a download). Raises on failure; callers fall back to DirectML."""
    global _registered, _rt_error
    with _rt_lock:
        if _registered is not None and not download:
            return dict(_registered)
        import onnxruntime as ort
        bootstrap, winml = _import_winml()
        _bootstrap(bootstrap)
        catalog = winml.ExecutionProviderCatalog.get_default()
        out: Dict[str, str] = dict(_registered or {})
        for p in catalog.find_all_providers():
            name = str(p.name)
            if name in out:
                continue
            ready = p.ready_state
            if ready == winml.ExecutionProviderReadyState.NOT_PRESENT and not download:
                continue
            if ready != winml.ExecutionProviderReadyState.READY:
                if progress:
                    progress("download", f"Downloading {_VENDOR_LABELS.get(name, name)}...")
                r = p.ensure_ready_async().get()
                if r.status != winml.ExecutionProviderReadyResultState.SUCCESS:
                    _log("warn", f"catalog EP {name}: ensure_ready failed: {r.status} {getattr(r, 'diagnostic_text', '')}")
                    continue
            try:
                ort.register_execution_provider_library(name, str(p.library_path))
                out[name] = str(p.library_path)
            except Exception as e:  # already registered in this process is fine
                if "already" in str(e).lower():
                    out[name] = str(p.library_path)
                else:
                    _log("warn", f"register {name} failed: {e}")
        _registered = out
        _rt_error = None
        return dict(out)


def ep_version_from_path(path: str) -> str:
    """Version of a Windows ML EP package from its install path
    (``...\\Microsoft.WinML.Intel.OpenVINO.EP.Framework.2_1.8.95.0_x64__8wekyb3d8bbwe\\...``)."""
    m = re.search(r"_(\d+(?:\.\d+){2,3})_[^\\/]*__", str(path or ""))
    return m.group(1) if m else ""


def _driver_text(dev) -> str:
    try:
        md = dict(dev.device.metadata or {})
    except Exception:
        md = {}
    return ",".join(f"{k}={v}" for k, v in sorted(md.items()))


def enumerate_hardware(download: bool = False) -> Dict[str, Any]:
    """Describe the catalog and the ORT devices for the log (never raises).

    Logged on every setup attempt (and on failures) so bug reports show which vendor EPs
    Windows offered on a machine, even when none of them could be used."""
    info: Dict[str, Any] = {"catalog": [], "devices": [], "ort": _ort_version()}
    try:
        bootstrap, winml = _import_winml()
        _bootstrap(bootstrap)
        cat = winml.ExecutionProviderCatalog.get_default()
        for p in cat.find_all_providers():
            rs = p.ready_state
            info["catalog"].append({"name": str(p.name), "ready_state": getattr(rs, "name", str(rs))})
    except Exception as e:
        info["catalog_error"] = f"{type(e).__name__}: {e}"
    try:
        import onnxruntime as ort
        for d in ort.get_ep_devices():
            info["devices"].append({
                "ep": str(d.ep_name), "type": str(d.device.type), "vendor": str(getattr(d.device, "vendor", "")),
                "metadata": _driver_text(d),
            })
    except Exception as e:
        info["devices_error"] = f"{type(e).__name__}: {e}"
    return info


def gpu_devices_for(ort, ep_name: str) -> list:
    return [d for d in ort.get_ep_devices()
            if str(d.ep_name) == ep_name and str(d.device.type).upper().endswith("GPU")]


def vendor_candidates(ort, registered: Dict[str, str]) -> List[str]:
    """EP names that registered from the catalog and expose at least one GPU device."""
    out = []
    for name in registered:
        if name in _NON_VENDOR_EPS:
            continue
        try:
            if gpu_devices_for(ort, name):
                out.append(name)
        except Exception:
            continue
    return out


# --------------------------------------------------------------------------- routing

@dataclass(frozen=True)
class VendorSpec:
    ep_name: str
    kinds: frozenset
    files: tuple = ()  # ((kind, validated model file name), ...)

    def label(self) -> str:
        return _VENDOR_LABELS.get(self.ep_name, self.ep_name)


_route_lock = threading.Lock()
_spec_cache: Dict[str, Any] = {"loaded": False, "spec": None}
_runtime_failures: Dict[str, int] = {}
_disabled_kinds: set = set()


def reset_cache() -> None:
    """Forget the cached routing decision (after setup, toggle, or in tests)."""
    with _route_lock:
        _spec_cache.update(loaded=False, spec=None)
        _runtime_failures.clear()
        _disabled_kinds.clear()


def _state_is_current(state: dict) -> bool:
    return (state.get("ort_version") == _ort_version()
            and state.get("pipeline_version") == _pipeline_version()
            and state.get("platform_build_ok", True))


def _load_spec() -> Optional[VendorSpec]:
    if not _is_windows() or not user_enabled():
        return None
    state = load_state()
    if not state or not state.get("ok") or state.get("disabled"):
        return None
    if not _state_is_current(state):
        _log("info", "validation is stale (ORT or pipeline version changed); using DirectML until setup is re-run")
        return None
    if not platform_status().get("available"):
        return None
    kinds = frozenset(k for k, v in (state.get("models") or {}).items() if v.get("enabled") and k in ROUTABLE_KINDS)
    if not kinds or not state.get("ep_name"):
        return None
    try:
        reg = ensure_runtime(download=False)
    except Exception as e:
        _log("warn", f"Windows ML unavailable ({type(e).__name__}: {e}); using DirectML")
        return None
    if state["ep_name"] not in reg:
        _log("warn", f"{state['ep_name']} is not installed any more; using DirectML")
        return None
    now_ver = ep_version_from_path(reg[state["ep_name"]])
    if state.get("ep_version") and now_ver and now_ver != state["ep_version"]:
        _log("info", f"{state['ep_name']} changed ({state['ep_version']} -> {now_ver}); using DirectML until setup is re-run")
        return None
    files = tuple((k, v.get("file", "")) for k, v in (state.get("models") or {}).items() if k in kinds)
    spec = VendorSpec(state["ep_name"], kinds, files)
    _check_unclean_exits()
    if load_state() and load_state().get("disabled"):
        return None
    _log("info", f"enhanced GPU mode active: {spec.label()} for {sorted(kinds)}")
    return spec


def current_spec() -> Optional[VendorSpec]:
    with _route_lock:
        if not _spec_cache["loaded"]:
            try:
                _spec_cache["spec"] = _load_spec()
            except Exception as e:
                _log("warn", f"enhanced GPU mode failed to load: {e}")
                _spec_cache["spec"] = None
            _spec_cache["loaded"] = True
        return _spec_cache["spec"]


def vendor_spec_for(kind: str, model_name: str = "") -> Optional[VendorSpec]:
    """The vendor spec when ``kind`` should currently run on the vendor EP, else None.
    ``model_name`` (file name) must be the one that was validated (e.g. only ``mdv5a.onnx``,
    not the other detector)."""
    if kind not in ROUTABLE_KINDS or kind in _disabled_kinds:
        return None
    spec = current_spec()
    if spec is None or kind not in spec.kinds:
        return None
    want = dict(spec.files).get(kind)
    if model_name and want and Path(model_name).name != want:
        return None
    return spec


def record_runtime_failure(kind: str, exc: BaseException) -> None:
    """A vendor session failed at build or run time; the caller falls back to DirectML."""
    _log("warn", f"{kind} failed on the vendor EP ({type(exc).__name__}: {str(exc)[:300]}); using DirectML")
    note_vendor_left(kind, f"{type(exc).__name__}: {str(exc)[:120]}")
    with _route_lock:
        _runtime_failures[kind] = _runtime_failures.get(kind, 0) + 1
        n = _runtime_failures[kind]
        if n >= MAX_RUNTIME_FAILURES:
            _disabled_kinds.add(kind)
    _bump_failure_counter(kind, exc)


def _bump_failure_counter(kind: str, exc: BaseException) -> None:
    st = load_state()
    if not st:
        return
    st["runtime_failures"] = int(st.get("runtime_failures", 0)) + 1
    st["last_failure"] = {"kind": kind, "error": f"{type(exc).__name__}: {str(exc)[:300]}", "at": int(time.time())}
    if st["runtime_failures"] >= 3 * MAX_RUNTIME_FAILURES:
        st["disabled"] = True
        st["disabled_reason"] = "repeated runtime failures"
        _log("warn", "enhanced GPU mode switched off after repeated failures")
    save_state(st)


def mode_summary() -> Optional[dict]:
    """What to record in the analysis log / ``kestrel_metadata.json`` (None when the mode is off)."""
    spec = current_spec()
    if spec is None:
        return None
    st = load_state() or {}
    return {"ep": spec.ep_name, "device": st.get("device"), "ep_version": st.get("ep_version"),
            "driver": st.get("driver"), "ort": st.get("ort_version"),
            "models": sorted(k for k in spec.kinds if k not in _disabled_kinds)}


# --------------------------------------------------------------------------- crash marker

_marker_active = False


def mark_run_started() -> None:
    """Called when the first vendor session is built in a process. An access violation inside a
    vendor EP cannot be caught, so a marker that survives an unclean exit is how the next start
    learns the mode crashed."""
    global _marker_active
    if _marker_active:
        return
    _marker_active = True
    _write_json(_marker_path(), {"pid": os.getpid(), "at": int(time.time())})
    atexit.register(mark_run_finished)


def mark_run_finished() -> None:
    global _marker_active
    if _marker_active:
        try:
            _marker_path().unlink()
        except OSError:
            pass
        _marker_active = False


def _pid_alive(pid: int) -> bool:
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # type: ignore[attr-defined]
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def _check_unclean_exits() -> None:
    m = _read_json(_marker_path())
    if not m:
        return
    if int(m.get("pid", 0)) == os.getpid() or _pid_alive(int(m.get("pid", 0))):
        return  # a live process owns the marker (this one, or another Kestrel instance)
    try:
        _marker_path().unlink()
    except OSError:
        pass
    st = load_state()
    if not st:
        return
    st["unclean_exits"] = int(st.get("unclean_exits", 0)) + 1
    _log("warn", f"previous run ended without a clean exit with enhanced GPU mode active "
                 f"({st['unclean_exits']}/{MAX_UNCLEAN_EXITS})")
    if st["unclean_exits"] >= MAX_UNCLEAN_EXITS:
        st["disabled"] = True
        st["disabled_reason"] = "repeated unclean exits"
        _log("warn", "enhanced GPU mode switched off; using DirectML")
    save_state(st)


def clear_unclean_exits() -> None:
    st = load_state()
    if st and st.get("unclean_exits"):
        st["unclean_exits"] = 0
        save_state(st)


# --------------------------------------------------------------------------- session creation

def build_vendor_session(ort, model_path: str, so, spec: VendorSpec):
    """Create an ``InferenceSession`` on the vendor GPU device(s) (not a provider-name list)."""
    ensure_runtime(download=False)
    devs = gpu_devices_for(ort, spec.ep_name)
    if not devs:
        raise RuntimeError(f"no GPU device for {spec.ep_name}")
    so.add_provider_for_devices(devs, {})
    mark_run_started()
    return ort.InferenceSession(model_path, sess_options=so)


# --------------------------------------------------------------------------- setup flow

def _validator_command(ep_name: str, kinds: List[str], out_path: Path) -> List[str]:
    args = ["--cli", "--enhanced-gpu-validate", "--enhanced-gpu-ep", ep_name,
            "--enhanced-gpu-kinds", ",".join(kinds), "--enhanced-gpu-output", str(out_path)]
    if getattr(sys, "frozen", False):
        # Same pattern the CI smoke tests use on the frozen exe: the GUI exe has no console, so
        # results go through a file.
        return [sys.executable, *args]
    cli = Path(__file__).resolve().parents[2] / "cli.py"
    return [sys.executable, str(cli), *args[1:]]


def _read_lines(path: Path) -> List[dict]:
    out = []
    try:
        for ln in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(ln))
            except ValueError:
                pass
    except OSError:
        pass
    return out


def _run_child_once(ep_name: str, kinds: List[str], done: int, progress: ProgressFn,
                    cancel: Callable[[], bool], timeout: float) -> Optional[dict]:
    """One validation child for ``kinds``. None if it could not be started."""
    out_path = _user_dir() / "enhanced_gpu_validation.json"
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.unlink()
    except OSError:
        pass
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(_validator_command(ep_name, kinds, out_path), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    except Exception as e:
        _log("warn", f"could not start the validation process: {e}")
        return None
    t0 = time.time()
    seen = 0
    while True:
        rc = proc.poll()
        n = len(_read_lines(out_path))
        if n != seen:
            seen = n
            progress("validate", f"Checked {done + n} of {len(ROUTABLE_KINDS)} models...")
        if rc is not None:
            break
        if cancel():
            proc.kill()
            return {"models": [], "cancelled": True}
        if time.time() - t0 > timeout:
            proc.kill()
            _log("warn", "validation process timed out")
            rc = -1
            break
        time.sleep(0.5)
    return {"models": _read_lines(out_path), "exit_code": proc.returncode if rc is None else rc}


def _run_validator_child(ep_name: str, progress: ProgressFn, cancel: Callable[[], bool],
                         timeout: float = 600.0) -> Optional[dict]:
    """Validate every routable model in child processes (a vendor EP can end its process with an
    access violation). A model that leaves no result is recorded as crashed and the remaining models
    run in a fresh child. None if no child could be started at all."""
    remaining = list(ROUTABLE_KINDS)
    results: List[dict] = []
    last_rc = 0
    while remaining:
        res = _run_child_once(ep_name, remaining, len(results), progress, cancel, timeout)
        if res is None:
            return None if not results else {"models": results, "exit_code": last_rc}
        if res.get("cancelled"):
            return res
        got = [r for r in res["models"] if isinstance(r, dict) and r.get("kind") in remaining]
        results.extend(got)
        last_rc = res.get("exit_code")
        remaining = [k for k in remaining if k not in {r["kind"] for r in got}]
        if remaining:  # the child ended early: the next model it would have run is the one that died
            dead = remaining.pop(0)
            code = res.get("exit_code")
            results.append({"kind": dead, "ep": ep_name, "enabled": False,
                            "reason": f"the validation process ended unexpectedly (exit code {code})",
                            "crashed": True})
            _log("warn", f"validation of {dead} on {ep_name} crashed (exit code {code})")
    return {"models": results, "exit_code": last_rc}


def _run_validator_inprocess(ep_name: str, progress: ProgressFn) -> dict:
    """Fallback when no child process can be started: validate in this process. An access
    violation would end the app, so this uses the crash marker (the mode switches itself off
    after repeated unclean exits)."""
    from . import enhanced_validate
    mark_run_started()
    results = []
    for kind in ROUTABLE_KINDS:
        results.append(enhanced_validate.validate_kind(kind, ep_name))
        progress("validate", f"Checked {len(results)} of {len(ROUTABLE_KINDS)} models...")
    return {"models": results, "exit_code": 0, "inprocess": True}


def run_setup(progress: Optional[ProgressFn] = None, cancel: Optional[Callable[[], bool]] = None) -> dict:
    """The user-triggered setup. Returns the saved state dict (``ok`` False with ``reason`` on failure).
    Never raises."""
    progress = progress or (lambda stage, msg: None)
    cancel = cancel or (lambda: False)
    state: Dict[str, Any] = {"ok": False, "started": int(time.time()), "ort_version": _ort_version(),
                             "pipeline_version": _pipeline_version(), "models": {}}
    try:
        progress("platform", "Checking this computer...")
        ps = platform_status()
        _log("info", f"setup: platform {ps}")
        if not ps["available"]:
            state["reason"] = ps["reason"]
            state["unavailable"] = True
            return _finish(state)
        progress("hardware", "Looking for GPU acceleration options...")
        try:
            reg = ensure_runtime(download=True, progress=progress)
        except Exception as e:
            state["reason"] = f"Windows ML could not start: {type(e).__name__}: {e}"
            state["hardware"] = enumerate_hardware()
            _log("warn", f"setup: {state['reason']}")
            _log("info", f"setup: hardware {json.dumps(state['hardware'])}")
            return _finish(state)
        if cancel():
            state["reason"] = "Cancelled."
            return _finish(state)
        import onnxruntime as ort
        hw = enumerate_hardware()
        state["hardware"] = hw
        _log("info", f"setup: hardware {json.dumps(hw)}")
        cands = vendor_candidates(ort, reg)
        if not cands:
            state["reason"] = ("Windows did not offer a GPU acceleration package for this computer "
                               "(or it could not be installed).")
            return _finish(state)
        best: Optional[dict] = None
        for ep in cands:
            progress("validate", f"Testing {_VENDOR_LABELS.get(ep, ep)} on your models (this can take a few minutes)...")
            res = _run_validator_child(ep, progress, cancel)
            if res is None:
                res = _run_validator_inprocess(ep, progress)
            if res.get("cancelled"):
                state["reason"] = "Cancelled."
                return _finish(state)
            models = {r["kind"]: r for r in res["models"] if isinstance(r, dict) and "kind" in r}
            n_ok = sum(1 for r in models.values() if r.get("enabled"))
            _log("info", f"setup: {ep}: {n_ok}/{len(ROUTABLE_KINDS)} models passed; child exit "
                         f"{res.get('exit_code')}; details {json.dumps(models)}")
            if best is None or n_ok > best["n"]:
                devs = gpu_devices_for(ort, ep)
                best = {"n": n_ok, "ep": ep, "models": models, "exit_code": res.get("exit_code"),
                        "device": _driver_text(devs[0]) if devs else ""}
        assert best is not None
        state.update(ep_name=best["ep"], device=best["device"], models=best["models"],
                     ep_version=ep_version_from_path(reg.get(best["ep"], "")),
                     measured=best["ep"] in MEASURED_EPS, child_exit_code=best["exit_code"])
        if best["n"] == 0:
            state["reason"] = "No model ran faster and matched on the GPU acceleration package."
            return _finish(state)
        state["ok"] = True
        return _finish(state)
    except Exception as e:  # never let setup raise into the UI
        state["reason"] = f"{type(e).__name__}: {e}"
        _log("warn", f"setup crashed: {state['reason']}")
        return _finish(state)


def _finish(state: dict) -> dict:
    state["finished"] = int(time.time())
    state.pop("disabled", None)
    state["unclean_exits"] = 0
    save_state(state)
    reset_cache()
    _log("info", f"setup finished: ok={state['ok']} reason={state.get('reason', '')}")
    return state


def status() -> Dict[str, Any]:
    """Everything the settings UI shows."""
    ps = platform_status()
    st = load_state() or {}
    return {
        "available": ps["available"], "unavailable_reason": ps["reason"], "packaged": ps.get("packaged", False),
        "enabled": user_enabled(), "set_up": bool(st.get("ok")), "disabled": bool(st.get("disabled")),
        "disabled_reason": st.get("disabled_reason", ""), "reason": st.get("reason", ""),
        "stale": bool(st) and not _state_is_current(st),
        "ep": st.get("ep_name", ""), "ep_label": _VENDOR_LABELS.get(st.get("ep_name", ""), st.get("ep_name", "")),
        "measured": bool(st.get("measured", False)), "finished": st.get("finished"),
        "runtime_failures": int(st.get("runtime_failures", 0) or 0), "last_failure": st.get("last_failure") or {},
        "models": {k: {"enabled": bool(v.get("enabled")), "speedup": v.get("speedup"), "reason": v.get("reason", "")}
                   for k, v in (st.get("models") or {}).items()},
    }


def is_vendor_ep(name: str) -> bool:
    """True for an EP name that enhanced GPU mode can create (used to count it as a GPU session)."""
    return str(name) in _VENDOR_LABELS


# --------------------------------------------------------------------------- live status

_KIND_LABELS = {"detector": "detector", "sam_enc": "segmentation", "bird_species": "species ID"}
_rt_status_lock = threading.Lock()
_rt_on_vendor: Dict[str, str] = {}   # kind -> model file currently running on the vendor EP
_rt_fallbacks: List[dict] = []       # {"kind", "to", "reason", "at"}: a vendor model that left the vendor EP


def note_vendor_active(kind: str, model_file: str) -> None:
    with _rt_status_lock:
        _rt_on_vendor[kind] = model_file


def note_vendor_left(kind: str, reason: str, to: str = "DirectML") -> None:
    """A model that was on the vendor EP is not any more (error fallback, or the run moved to CPU)."""
    with _rt_status_lock:
        if kind not in _rt_on_vendor:
            return
        del _rt_on_vendor[kind]
        _rt_fallbacks.append({"kind": kind, "to": to, "reason": str(reason)[:200], "at": int(time.time())})
        del _rt_fallbacks[:-20]


def runtime_summary() -> dict:
    """What this process is doing right now: models on the vendor EP and models that left it."""
    with _rt_status_lock:
        return {"on_vendor": sorted(_rt_on_vendor), "fallbacks": [dict(f) for f in _rt_fallbacks]}


def reset_runtime() -> None:
    with _rt_status_lock:
        _rt_on_vendor.clear()
        _rt_fallbacks.clear()


def _names(kinds) -> str:
    return ", ".join(_KIND_LABELS.get(k, k) for k in kinds)


def gpu_mode(use_gpu: bool, coord=None, gpu_lost: bool = False) -> Optional[dict]:
    """One answer for "what is analysis running on?", shown in the queue panel and the Live Analysis
    dialog. ``mode`` is one of ``enhanced``, ``enhanced_partial``, ``standard``, ``cpu_selected``,
    ``cpu_fallback``, ``gpu_lost``. ``needs_restart`` / ``report`` tell the UI to offer a restart
    hint and the bug-report button. None until the models are loaded (``coord`` is None)."""
    if coord is None:
        return None
    rt = runtime_summary()
    base: Dict[str, Any] = {"vendor": rt["on_vendor"], "fallbacks": rt["fallbacks"],
                            "needs_restart": False, "report": False}
    if not use_gpu:
        return dict(base, mode="cpu_selected", label="CPU", detail="GPU analysis is turned off for this run.")
    if gpu_lost or getattr(coord, "gpu_lost", False):
        return dict(base, mode="gpu_lost", needs_restart=True, report=True, label="GPU stopped responding",
                    detail="Windows reset the GPU. Restart Kestrel to use the GPU again. "
                           "If this keeps happening, please report it.")
    state = getattr(getattr(coord, "state", None), "value", "gpu")
    if state != "gpu":
        locked = state == "gpu_locked_out"
        return dict(base, mode="cpu_fallback", needs_restart=True, report=True, label="Fell back to CPU",
                    detail=("The GPU failed repeatedly, so the rest of this session runs on the CPU (slower). "
                            if locked else "The GPU failed, so analysis is running on the CPU (slower) for now. ")
                    + "Restart Kestrel to use the GPU again, and please report the problem so we can fix it.")
    spec = current_spec()
    if rt["on_vendor"]:
        label = f"Enhanced GPU ({spec.label() if spec else 'vendor'})"
        if rt["fallbacks"]:
            gone = sorted({f["kind"] for f in rt["fallbacks"]})
            return dict(base, mode="enhanced_partial", report=True, label=label + ", partly",
                        detail=f"On the enhanced GPU: {_names(rt['on_vendor'])}. "
                               f"Fell back to the standard GPU (DirectML): {_names(gone)}.")
        return dict(base, mode="enhanced", label=label,
                    detail=f"On the enhanced GPU: {_names(rt['on_vendor'])}. "
                           "Other models use the standard GPU (DirectML) or the CPU by design.")
    set_up = bool((load_state() or {}).get("ok")) and user_enabled()
    if rt["fallbacks"]:
        return dict(base, mode="standard", report=True, label="Standard GPU (DirectML)",
                    detail="Enhanced GPU mode failed and was switched off for this session: "
                           + rt["fallbacks"][0]["reason"] + ".")
    if set_up and spec is None:
        return dict(base, mode="standard", label="Standard GPU (DirectML)",
                    detail="Enhanced GPU mode is set up but not in use (switched off or out of date). "
                           "Run setup again in Settings.")
    return dict(base, mode="standard", label="Standard GPU (DirectML)", detail="")


def report_text() -> str:
    """Plain-text description for a bug report about the GPU mode (the log tail is attached separately)."""
    st = load_state() or {}
    rt = runtime_summary()
    lines = [f"GPU mode problem. Enhanced mode set up: {bool(st.get('ok'))}, enabled: {user_enabled()}.",
             f"Vendor EP: {st.get('ep_name') or 'none'}; models on vendor now: {rt['on_vendor']}.",
             f"Fell back: {json.dumps(rt['fallbacks'])}",
             f"Setup result: {st.get('reason') or 'ok'}; failures: {st.get('runtime_failures', 0)}; "
             f"unclean exits: {st.get('unclean_exits', 0)}; disabled: {st.get('disabled_reason', '') or 'no'}.",
             f"Per-model validation: {json.dumps(st.get('models', {}))}",
             f"Hardware: {json.dumps(st.get('hardware', {}))}"]
    return "\n".join(lines)[:6000]
