"""Opt-in enhanced GPU mode (vendor EPs through Windows ML): everything that can be checked
without Windows ML, a vendor GPU or ONNX model weights.

Covers: importing with no Windows ML present, the platform gate (Windows 10 / pre-24H2), the saved
state and when it counts as stale, routing (which models, which model files, when the user or a
failure switches it off), the session fallback to DirectML at build and run time, the validation
thresholds, the setup flow with a fake catalog and fake validation child, and the settings key.
"""

import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.ml import enhanced_gpu as eg  # noqa: E402
from kestrel_analyzer.ml import enhanced_validate as ev  # noqa: E402
from kestrel_analyzer.ml import is_gpu_active  # noqa: E402
from kestrel_analyzer.ml.provider_coordinator import ProviderCoordinator, ProviderState  # noqa: E402
from kestrel_analyzer.ml.resilient_session import ResilientOnnxSession  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Own user-data dir, no cached routing, mode switched on by the user, fixed versions."""
    monkeypatch.setattr(eg, "_user_dir", lambda: tmp_path)
    monkeypatch.setattr(eg, "user_enabled", lambda: True)
    monkeypatch.setattr(eg, "_ort_version", lambda: "1.25.2")
    monkeypatch.setattr(eg, "_pipeline_version", lambda: "3.0.0")
    monkeypatch.setattr(eg, "platform_status", lambda: {"available": True, "reason": "", "build": 26100,
                                                         "packaged": False})
    monkeypatch.setattr(eg, "ensure_runtime", lambda download=False, progress=None: {"OpenVINOExecutionProvider": "x.dll"})
    monkeypatch.setattr(eg, "_is_windows", lambda: True)
    eg.reset_cache()
    eg._marker_active = False
    yield
    eg.reset_cache()


def _good_state(**over):
    st = {"ok": True, "ep_name": "OpenVINOExecutionProvider", "ort_version": "1.25.2", "pipeline_version": "3.0.0",
          "models": {
              "detector": {"file": "mdv5a.onnx", "enabled": True, "speedup": 4.0},
              "sam_enc": {"file": "sam_hq_vit_tiny_encoder.onnx", "enabled": True, "speedup": 2.0},
              "bird_species": {"file": "bioclip2_image_wdq16.onnx", "enabled": False, "reason": "slow"},
          }}
    st.update(over)
    eg.save_state(st)
    return st


# ---- import safety and platform gate -------------------------------------------------------

def test_import_does_not_need_windows_ml(monkeypatch):
    monkeypatch.setitem(sys.modules, "winui3", None)  # makes `import winui3` raise ImportError
    import importlib
    importlib.reload(eg)  # must not import Windows ML at import time
    assert eg.ROUTABLE_KINDS


def test_ensure_runtime_raises_without_windows_ml_and_routing_falls_back(monkeypatch):
    _good_state()

    def boom(download=False, progress=None):
        raise ImportError("No module named 'winui3'")
    monkeypatch.setattr(eg, "ensure_runtime", boom)
    eg.reset_cache()
    assert eg.vendor_spec_for("detector", "mdv5a.onnx") is None  # silently DirectML


@pytest.mark.parametrize("build,expected", [(19045, False), (22631, False), (26100, True), (26200, True)])
def test_platform_gate_by_windows_build(monkeypatch, build, expected):
    import platform
    monkeypatch.undo()  # use the real platform_status
    monkeypatch.setattr(eg, "_is_windows", lambda: True)
    monkeypatch.setattr(eg, "windows_build", lambda: build)
    monkeypatch.setattr(eg, "has_package_identity", lambda: False)
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    import importlib.util
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: object())
    ps = eg.platform_status()
    assert ps["available"] is expected
    if not expected:
        assert "24H2" in ps["reason"]


def test_platform_gate_not_windows(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(eg, "_is_windows", lambda: False)
    ps = eg.platform_status()
    assert not ps["available"] and "only available on windows" in ps["reason"].lower()


# ---- routing -----------------------------------------------------------------------------

def test_routes_only_validated_models_and_files():
    _good_state()
    spec = eg.vendor_spec_for("detector", "mdv5a.onnx")
    assert spec is not None and spec.ep_name == "OpenVINOExecutionProvider"
    assert eg.vendor_spec_for("sam_enc", "sam_hq_vit_tiny_encoder.onnx") is not None
    assert eg.vendor_spec_for("bird_species", "bioclip2_image_wdq16.onnx") is None   # did not pass validation
    assert eg.vendor_spec_for("detector", "mdv1000-cedar.onnx") is None             # other detector: never validated
    for kind in ("sam_dec", "quality", "siglip_image"):                               # never vendor-routed
        assert eg.vendor_spec_for(kind, "x.onnx") is None


@pytest.mark.parametrize("override", [
    {"ort_version": "1.30.0"},          # stale: ORT changed
    {"pipeline_version": "4.0.0"},      # stale: pipeline changed
    {"disabled": True},                 # switched off after crashes
    {"ok": False},                      # setup failed
])
def test_stale_failed_or_disabled_state_uses_directml(override):
    _good_state(**override)
    assert eg.vendor_spec_for("detector", "mdv5a.onnx") is None


def test_user_toggle_off_and_platform_unavailable(monkeypatch):
    _good_state()
    monkeypatch.setattr(eg, "user_enabled", lambda: False)
    eg.reset_cache()
    assert eg.vendor_spec_for("detector", "mdv5a.onnx") is None
    monkeypatch.setattr(eg, "user_enabled", lambda: True)
    monkeypatch.setattr(eg, "platform_status", lambda: {"available": False, "reason": "old"})
    eg.reset_cache()
    assert eg.vendor_spec_for("detector", "mdv5a.onnx") is None


def test_ep_no_longer_installed_uses_directml(monkeypatch):
    _good_state()
    monkeypatch.setattr(eg, "ensure_runtime", lambda download=False, progress=None: {})
    eg.reset_cache()
    assert eg.vendor_spec_for("detector", "mdv5a.onnx") is None


def test_repeated_runtime_failures_route_that_model_back(monkeypatch):
    _good_state()
    assert eg.vendor_spec_for("detector", "mdv5a.onnx")
    for _ in range(eg.MAX_RUNTIME_FAILURES):
        eg.record_runtime_failure("detector", RuntimeError("OpenVINOExecutionProvider node failed"))
    assert eg.vendor_spec_for("detector", "mdv5a.onnx") is None
    assert eg.vendor_spec_for("sam_enc", "sam_hq_vit_tiny_encoder.onnx") is not None


def test_unclean_exits_switch_the_mode_off(monkeypatch):
    _good_state()
    monkeypatch.setattr(eg, "_pid_alive", lambda pid: False)
    for _ in range(eg.MAX_UNCLEAN_EXITS):
        eg._write_json(eg._marker_path(), {"pid": 99999999, "at": 1})  # a previous process that never cleaned up
        eg.reset_cache()
        eg.current_spec()
    assert eg.load_state().get("disabled") is True
    eg.reset_cache()
    assert eg.vendor_spec_for("detector", "mdv5a.onnx") is None


def test_live_marker_owner_is_not_counted(monkeypatch):
    _good_state()
    monkeypatch.setattr(eg, "_pid_alive", lambda pid: True)
    eg._write_json(eg._marker_path(), {"pid": 4242, "at": 1})
    eg.reset_cache()
    assert eg.current_spec() is not None
    assert not eg.load_state().get("unclean_exits")


def test_mode_summary_for_metadata():
    _good_state(device="GPU", ep_version="1.8.95.0")
    eg.vendor_spec_for("detector", "mdv5a.onnx")
    s = eg.mode_summary()
    assert s["ep"] == "OpenVINOExecutionProvider" and s["models"] == ["detector", "sam_enc"]
    assert s["ep_version"] == "1.8.95.0"


def test_mode_summary_none_when_off(monkeypatch):
    monkeypatch.setattr(eg, "user_enabled", lambda: False)
    assert eg.mode_summary() is None


def test_is_gpu_active_counts_vendor_ep():
    assert is_gpu_active(["OpenVINOExecutionProvider"])
    from kestrel_analyzer.ml import GPU_EP
    assert is_gpu_active([GPU_EP, "CPUExecutionProvider"])
    assert not is_gpu_active(["CPUExecutionProvider"])


# ---- coordinator gating -------------------------------------------------------------------

def test_coordinator_only_offers_vendor_ep_while_on_gpu():
    _good_state()
    gpu = ProviderCoordinator(user_gpu_enabled=True)
    assert gpu.vendor_for("detector", "mdv5a.onnx") is not None
    assert gpu.vendor_for("siglip_image", "siglip.onnx") is None
    gpu._state = ProviderState.CPU
    assert gpu.vendor_for("detector", "mdv5a.onnx") is None
    gpu._state = ProviderState.GPU
    gpu._gpu_lost_reason = "device removed"
    assert gpu.vendor_for("detector", "mdv5a.onnx") is None
    assert ProviderCoordinator(user_gpu_enabled=False).vendor_for("detector", "mdv5a.onnx") is None


# ---- session fallback -----------------------------------------------------------------------

class _FakeSession:
    def __init__(self, name, fail_run=None):
        self.name, self.fail_run, self.calls = name, fail_run, 0

    def run(self, *_a, **_k):
        self.calls += 1
        if self.fail_run is not None:
            raise self.fail_run
        return [self.name]

    def get_providers(self):
        return [self.name]

    def get_inputs(self):
        return []

    get_outputs = get_inputs


class _FakeCoord:
    def __init__(self, spec):
        self.spec = spec
        self.registered = []

    def providers_for(self, kind):
        return ["DmlExecutionProvider", "CPUExecutionProvider"]

    def vendor_for(self, kind, name=""):
        return self.spec

    def _register_session(self, s):
        self.registered.append(s)


def _patch_build(monkeypatch, vendor_session=None, vendor_build_error=None):
    import kestrel_analyzer.ml.providers as prov
    built = []

    def fake_build(path, providers, kind="", config=None, vendor=None):
        built.append("vendor" if vendor else "dml")
        if vendor:
            if vendor_build_error:
                raise vendor_build_error
            return vendor_session
        return _FakeSession("dml")
    monkeypatch.setattr(prov, "build_session", fake_build)
    return built


def test_session_uses_vendor_ep_when_routed(monkeypatch):
    spec = eg.VendorSpec("OpenVINOExecutionProvider", frozenset({"detector"}), (("detector", "mdv5a.onnx"),))
    built = _patch_build(monkeypatch, vendor_session=_FakeSession("ov"))
    s = ResilientOnnxSession("detector", "mdv5a.onnx", _FakeCoord(spec))
    assert built == ["vendor"] and s.run(None, {}) == ["ov"]


def test_vendor_build_failure_falls_back_to_directml_quietly(monkeypatch):
    _good_state()
    spec = eg.VendorSpec("OpenVINOExecutionProvider", frozenset({"detector"}), (("detector", "mdv5a.onnx"),))
    built = _patch_build(monkeypatch, vendor_build_error=RuntimeError("EP plugin failed to create session"))
    s = ResilientOnnxSession("detector", "mdv5a.onnx", _FakeCoord(spec))
    assert built == ["vendor", "dml"] and s.run(None, {}) == ["dml"]
    assert eg.load_state()["runtime_failures"] == 1


def test_vendor_run_failure_rebuilds_on_directml_and_retries(monkeypatch):
    _good_state()
    spec = eg.VendorSpec("OpenVINOExecutionProvider", frozenset({"detector"}), (("detector", "mdv5a.onnx"),))
    bad = _FakeSession("ov", fail_run=RuntimeError("RuntimeException ... OpenVINOExecutionProvider node"))
    built = _patch_build(monkeypatch, vendor_session=bad)
    s = ResilientOnnxSession("detector", "mdv5a.onnx", _FakeCoord(spec))
    assert s.run(None, {}) == ["dml"]
    assert built == ["vendor", "dml"] and bad.calls == 1
    assert s.run(None, {}) == ["dml"] and built == ["vendor", "dml"]  # stays on DirectML


def test_device_lost_error_is_left_to_the_coordinator(monkeypatch):
    spec = eg.VendorSpec("OpenVINOExecutionProvider", frozenset({"detector"}), (("detector", "mdv5a.onnx"),))
    lost = RuntimeError("ReadbackHeap.cpp(22) 887A0005 The GPU device instance has been suspended.")
    _patch_build(monkeypatch, vendor_session=_FakeSession("ov", fail_run=lost))
    s = ResilientOnnxSession("detector", "mdv5a.onnx", _FakeCoord(spec))
    with pytest.raises(RuntimeError, match="887A0005"):
        s.run(None, {})


def test_vendor_error_text_is_not_misread_as_directx_device_lost():
    from kestrel_analyzer.ml.provider_coordinator import is_device_lost, is_session_dead
    e = RuntimeError("[ONNXRuntimeError] : 6 : RUNTIME_EXCEPTION : OpenVINOExecutionProvider node failed")
    assert not is_device_lost(e) and not is_session_dead(e)  # handled by the vendor fallback, not GPU-lost


# ---- validation thresholds ------------------------------------------------------------------

def test_decide_thresholds():
    assert ev.decide(0.99999, True, 2.5) is None
    assert "non-finite" in ev.decide(1.0, False, 3.0)
    assert "differs" in ev.decide(0.9990, True, 3.0)
    assert "not fast enough" in ev.decide(1.0, True, 1.2)
    assert "not fast enough" in ev.decide(1.0, True, 1.49)
    assert ev.decide(1.0, True, 1.5) is None


def test_compare_outputs_detects_mismatch_and_nan():
    import numpy as np
    a = [[np.arange(1, 9, dtype=np.float32)]]
    same = ev.compare_outputs(a, [[np.arange(1, 9, dtype=np.float32) * 1.0]])
    assert same["cosine"] > 0.99999 and same["finite"]
    flipped = ev.compare_outputs(a, [[np.arange(8, 0, -1, dtype=np.float32)]])
    assert flipped["cosine"] < eg.MIN_COSINE
    nan = ev.compare_outputs(a, [[np.full(8, np.nan, dtype=np.float32)]])
    assert not nan["finite"]


def test_only_the_three_routable_models_are_validated():
    assert set(ev._MODELS) == set(eg.ROUTABLE_KINDS) == {"detector", "sam_enc", "bird_species"}
    assert ev.model_file("bird_species") == "bioclip2_image_wdq16.onnx"


# ---- setup flow with a fake catalog and a fake validation child -------------------------------

class _FakeOrt:
    def __init__(self, devices):
        self._d = devices

    def get_ep_devices(self):
        return self._d


def _dev(ep, typ="OrtHardwareDeviceType.GPU"):
    return types.SimpleNamespace(ep_name=ep, device=types.SimpleNamespace(type=typ, vendor="v", metadata={"driver": "1"}))


def _setup_env(monkeypatch, devices, validator):
    monkeypatch.setitem(sys.modules, "onnxruntime", _FakeOrt(devices))
    monkeypatch.setattr(eg, "ensure_runtime", lambda download=False, progress=None: {d.ep_name: "x.dll" for d in devices})
    monkeypatch.setattr(eg, "enumerate_hardware", lambda download=False: {"catalog": [], "devices": []})
    monkeypatch.setattr(eg, "_run_validator_child", validator)


def _res(kind, ok, speedup=2.0):
    return {"kind": kind, "file": f"{kind}.onnx", "enabled": ok, "speedup": speedup, "reason": "" if ok else "slow"}


def test_setup_success_saves_per_model_results(monkeypatch):
    _setup_env(monkeypatch, [_dev("OpenVINOExecutionProvider"), _dev("OpenVINOExecutionProvider", "OrtHardwareDeviceType.NPU")],
               lambda ep, progress, cancel, timeout=0: {"models": [_res("detector", True), _res("sam_enc", True),
                                                                   _res("bird_species", False)], "exit_code": 0})
    seen = []
    st = eg.run_setup(lambda stage, msg: seen.append(stage))
    assert st["ok"] and st["ep_name"] == "OpenVINOExecutionProvider" and st["measured"]
    assert st["models"]["bird_species"]["enabled"] is False
    saved = eg.load_state()
    assert saved["ok"] and saved["models"]["detector"]["enabled"]
    assert {"platform", "hardware", "validate"} <= set(seen)
    assert eg.status()["set_up"] and eg.status()["models"]["detector"]["enabled"]


def test_setup_no_model_passes_is_not_ok(monkeypatch):
    _setup_env(monkeypatch, [_dev("OpenVINOExecutionProvider")],
               lambda ep, progress, cancel, timeout=0: {"models": [_res("detector", False)], "exit_code": 0})
    st = eg.run_setup()
    assert not st["ok"] and "faster" in st["reason"]
    assert eg.vendor_spec_for("detector", "mdv5a.onnx") is None


def test_setup_unmeasured_vendor_is_tried_and_flagged(monkeypatch):
    _setup_env(monkeypatch, [_dev("NvTensorRTRTXExecutionProvider")],
               lambda ep, progress, cancel, timeout=0: {"models": [_res("detector", True)], "exit_code": 0})
    st = eg.run_setup()
    assert st["ok"] and st["measured"] is False


def test_setup_skips_directml_cpu_webgpu_and_npu_only_eps(monkeypatch):
    called = []
    _setup_env(monkeypatch, [_dev("DmlExecutionProvider"), _dev("CPUExecutionProvider", "OrtHardwareDeviceType.CPU"),
                             _dev("WebGpuExecutionProvider"), _dev("OpenVINOExecutionProvider", "OrtHardwareDeviceType.NPU")],
               lambda ep, progress, cancel, timeout=0: called.append(ep))
    st = eg.run_setup()
    assert not st["ok"] and called == [] and "did not offer" in st["reason"]


def test_setup_windows_ml_failure_is_reported_not_raised(monkeypatch):
    def boom(download=False, progress=None):
        raise OSError("0x80070490 the Windows App SDK runtime is missing")
    monkeypatch.setattr(eg, "ensure_runtime", boom)
    monkeypatch.setattr(eg, "enumerate_hardware", lambda download=False: {"catalog": [], "devices": []})
    st = eg.run_setup()
    assert not st["ok"] and "Windows ML could not start" in st["reason"]
    assert eg.vendor_spec_for("detector", "mdv5a.onnx") is None


def test_setup_unavailable_platform_message(monkeypatch):
    monkeypatch.setattr(eg, "platform_status", lambda: {"available": False, "reason": "Requires Windows 11 version 24H2 or later (this is build 19045)."})
    st = eg.run_setup()
    assert not st["ok"] and st["unavailable"] and "24H2" in st["reason"]


def test_setup_falls_back_to_in_process_validation_when_no_child_can_start(monkeypatch):
    _setup_env(monkeypatch, [_dev("OpenVINOExecutionProvider")], lambda ep, progress, cancel, timeout=0: None)
    monkeypatch.setattr(eg, "_run_validator_inprocess",
                        lambda ep, progress: {"models": [_res("detector", True)], "exit_code": 0, "inprocess": True})
    assert eg.run_setup()["ok"]


def test_child_crash_marks_that_model_and_continues_with_the_rest(monkeypatch):
    calls = []

    def once(ep, kinds, done, progress, cancel, timeout):
        calls.append(list(kinds))
        if len(calls) == 1:  # detector finishes, sam_enc kills the child
            return {"models": [_res("detector", True)], "exit_code": 0xC0000005}
        return {"models": [_res(k, True) for k in kinds], "exit_code": 0}
    monkeypatch.setattr(eg, "_run_child_once", once)
    res = eg._run_validator_child("OpenVINOExecutionProvider", lambda s, m: None, lambda: False)
    by = {r["kind"]: r for r in res["models"]}
    assert by["sam_enc"]["crashed"] and not by["sam_enc"]["enabled"]
    assert by["detector"]["enabled"] and by["bird_species"]["enabled"]
    assert calls == [["detector", "sam_enc", "bird_species"], ["bird_species"]]


def test_child_command_uses_the_frozen_exe_with_cli_flag(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    cmd = eg._validator_command("OpenVINOExecutionProvider", ["detector"], tmp_path / "o.json")
    assert cmd[0] == sys.executable and cmd[1] == "--cli" and "--enhanced-gpu-validate" in cmd
    assert cmd[cmd.index("--enhanced-gpu-kinds") + 1] == "detector"


# ---- settings + cli plumbing ----------------------------------------------------------------

def test_settings_sanitizer_keeps_enhanced_gpu_toggle_as_bool():
    from settings_utils import _sanitize_settings_payload
    assert _sanitize_settings_payload({"enhanced_gpu_enabled": True})["enhanced_gpu_enabled"] is True
    assert _sanitize_settings_payload({"enhanced_gpu_enabled": False})["enhanced_gpu_enabled"] is False


def test_cli_accepts_the_internal_validation_flags():
    import cli
    a = cli.parse_args(["--enhanced-gpu-validate", "--enhanced-gpu-ep", "OpenVINOExecutionProvider",
                        "--enhanced-gpu-kinds", "detector,sam_enc", "--enhanced-gpu-output", "o.json"])
    assert a.enhanced_gpu_validate and a.enhanced_gpu_kinds == "detector,sam_enc"


def test_status_reports_progress_free_summary_when_never_set_up():
    s = eg.status()
    assert s["set_up"] is False and s["available"] is True and json.dumps(s)


def test_ep_version_is_parsed_from_the_install_path_and_a_change_means_stale(monkeypatch):
    p = r"C:\Program Files\WindowsApps\Microsoft.WinML.Intel.OpenVINO.EP.Framework.2_1.8.95.0_x64__8wekyb3d8bbwe\ov.dll"
    assert eg.ep_version_from_path(p) == "1.8.95.0"
    assert eg.ep_version_from_path("") == ""
    _good_state(ep_version="1.8.95.0")
    monkeypatch.setattr(eg, "ensure_runtime", lambda download=False, progress=None: {"OpenVINOExecutionProvider": p})
    eg.reset_cache()
    assert eg.vendor_spec_for("detector", "mdv5a.onnx") is not None
    _good_state(ep_version="1.7.0.0")  # validated against an older EP: re-validate before using
    eg.reset_cache()
    assert eg.vendor_spec_for("detector", "mdv5a.onnx") is None


# ---- live status ("what is analysis running on?") --------------------------------------------

class _ModeCoord:
    def __init__(self, state="gpu", gpu_lost=False):
        self.state = types.SimpleNamespace(value=state)
        self.gpu_lost = gpu_lost


@pytest.fixture
def clean_runtime():
    eg.reset_runtime()
    yield
    eg.reset_runtime()


def test_gpu_mode_none_until_models_load(clean_runtime):
    assert eg.gpu_mode(True, None) is None


def test_gpu_mode_cpu_selected(clean_runtime):
    m = eg.gpu_mode(False, _ModeCoord())
    assert m["mode"] == "cpu_selected" and not m["report"] and not m["needs_restart"]


def test_gpu_mode_standard_when_not_set_up(clean_runtime, monkeypatch):
    monkeypatch.setattr(eg, "user_enabled", lambda: False)
    m = eg.gpu_mode(True, _ModeCoord())
    assert m["mode"] == "standard" and not m["report"]


def test_gpu_mode_enhanced_and_partial(clean_runtime, monkeypatch):
    _good_state()
    monkeypatch.setattr(eg, "current_spec", lambda: types.SimpleNamespace(label=lambda: "Intel OpenVINO"))
    eg.note_vendor_active("detector", "mdv5a.onnx")
    eg.note_vendor_active("sam_enc", "enc.onnx")
    m = eg.gpu_mode(True, _ModeCoord())
    assert m["mode"] == "enhanced" and "Intel OpenVINO" in m["label"] and not m["report"]
    eg.note_vendor_left("sam_enc", "boom")
    m = eg.gpu_mode(True, _ModeCoord())
    assert m["mode"] == "enhanced_partial" and m["report"]
    assert eg.runtime_summary()["on_vendor"] == ["detector"]
    assert eg.runtime_summary()["fallbacks"][0]["to"] == "DirectML"


def test_gpu_mode_vendor_failure_everywhere_is_standard_with_report(clean_runtime):
    eg.note_vendor_active("detector", "mdv5a.onnx")
    eg.note_vendor_left("detector", "bad node")
    m = eg.gpu_mode(True, _ModeCoord())
    assert m["mode"] == "standard" and m["report"] and "bad node" in m["detail"]


@pytest.mark.parametrize("state", ["cpu", "gpu_locked_out"])
def test_gpu_mode_cpu_fallback_needs_restart_and_report(clean_runtime, state):
    m = eg.gpu_mode(True, _ModeCoord(state=state))
    assert m["mode"] == "cpu_fallback" and m["needs_restart"] and m["report"]
    assert "Restart Kestrel" in m["detail"]


def test_gpu_mode_gpu_lost(clean_runtime):
    m = eg.gpu_mode(True, _ModeCoord(), gpu_lost=True)
    assert m["mode"] == "gpu_lost" and m["needs_restart"] and m["report"]


def test_note_vendor_left_ignores_models_never_on_vendor(clean_runtime):
    eg.note_vendor_left("detector", "x")
    assert eg.runtime_summary() == {"on_vendor": [], "fallbacks": []}


def test_report_text_mentions_fallbacks(clean_runtime):
    _good_state()
    eg.note_vendor_active("detector", "mdv5a.onnx")
    eg.note_vendor_left("detector", "bad node", to="CPU")
    t = eg.report_text()
    assert "bad node" in t and "OpenVINOExecutionProvider" in t


def test_status_includes_setup_time_and_failures():
    _good_state(finished=123, runtime_failures=2, last_failure={"kind": "detector"})
    st = eg.status()
    assert st["finished"] == 123 and st["runtime_failures"] == 2 and st["last_failure"]["kind"] == "detector"
