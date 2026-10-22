"""Tower graph selection: DirectML -> the fp16 wdq16 graph + QDQ folding, everything
else -> the dynamic-int8 graph, and a failed GPU build -> the int8 graph on CPU."""

import pytest

from kestrel_analyzer.ml import providers as P
from kestrel_analyzer.ml.resilient_session import ResilientOnnxSession

DML = ["DmlExecutionProvider", "CPUExecutionProvider"]
COREML = ["CoreMLExecutionProvider", "CPUExecutionProvider"]
CPU = ["CPUExecutionProvider"]


@pytest.fixture
def graphs(tmp_path):
    cpu = tmp_path / "tower_int8.onnx"
    gpu = tmp_path / "tower_wdq16.onnx"
    for p in (cpu, gpu):
        p.write_bytes(b"\0" * (P._MIN_GRAPH_BYTES + 1))
    return cpu, gpu


def test_dml_gets_gpu_graph_and_fold_config(graphs):
    cpu, gpu = graphs
    path, provs, cfg = P.tower_graph(DML, cpu, gpu)
    assert path == str(gpu)
    assert provs == DML
    assert cfg == {"session.disable_quant_qdq": "1"}


def test_dml_provider_with_options_tuple(graphs):
    cpu, gpu = graphs
    provs_in = [("DmlExecutionProvider", {"device_id": 0}), "CPUExecutionProvider"]
    path, provs, cfg = P.tower_graph(provs_in, cpu, gpu)
    assert path == str(gpu) and provs == provs_in and cfg


@pytest.mark.parametrize("provs_in", [CPU, COREML])
def test_cpu_and_coreml_get_int8_graph(graphs, provs_in):
    cpu, gpu = graphs
    path, provs, cfg = P.tower_graph(provs_in, cpu, gpu)
    assert path == str(cpu)
    assert provs == provs_in
    assert cfg == {}


def test_dml_without_gpu_graph_runs_int8_on_cpu(graphs, tmp_path):
    cpu, _ = graphs
    stub = tmp_path / "lfs_stub.onnx"
    stub.write_bytes(b"version https://git-lfs.github.com/spec/v1\n")
    for missing in (None, tmp_path / "absent.onnx", stub):
        path, provs, cfg = P.tower_graph(DML, cpu, missing)
        assert (path, provs, cfg) == (str(cpu), CPU, {})


def test_tower_device_policy_defaults():
    assert P.tower_use_gpu("DmlExecutionProvider", "auto") is True
    assert P.tower_use_gpu("CoreMLExecutionProvider", "auto") is False
    assert P.tower_use_gpu("CoreMLExecutionProvider", "gpu") is True
    assert P.tower_use_gpu("DmlExecutionProvider", "cpu") is False


class _Coord:
    def __init__(self, providers):
        self.providers = providers
        self.sessions = []

    def providers_for(self, kind):
        return list(self.providers)

    def _register_session(self, s):
        self.sessions.append(s)


class _FakeSession:
    def __init__(self, path, providers):
        self.path, self.providers = path, providers

    def get_providers(self):
        return list(self.providers)


def _fake_build(calls, fail_on=None):
    def build(model_path, providers, kind="", config=None):
        calls.append((model_path, list(providers), dict(config or {})))
        if fail_on and model_path.endswith(fail_on):
            raise RuntimeError("DML device lost while building")
        return _FakeSession(model_path, providers)
    return build


def test_resilient_session_uses_gpu_graph_on_dml(graphs, monkeypatch):
    cpu, gpu = graphs
    calls = []
    monkeypatch.setattr(P, "build_session", _fake_build(calls))
    s = ResilientOnnxSession("bird_species", cpu, _Coord(DML), gpu_model_path=gpu)
    assert calls == [(str(gpu.resolve()), DML, {"session.disable_quant_qdq": "1"})]
    assert s.model_path_used == str(gpu.resolve())


def test_resilient_session_cpu_uses_int8_graph(graphs, monkeypatch):
    cpu, gpu = graphs
    calls = []
    monkeypatch.setattr(P, "build_session", _fake_build(calls))
    ResilientOnnxSession("siglip_image", cpu, _Coord(CPU), gpu_model_path=gpu)
    assert calls == [(str(cpu.resolve()), CPU, {})]


def test_resilient_session_gpu_failure_falls_back_to_int8_cpu(graphs, monkeypatch):
    cpu, gpu = graphs
    calls = []
    monkeypatch.setattr(P, "build_session", _fake_build(calls, fail_on=gpu.name))
    s = ResilientOnnxSession("bird_species", cpu, _Coord(DML), gpu_model_path=gpu)
    assert [c[0] for c in calls] == [str(gpu.resolve()), str(cpu.resolve())]
    assert calls[1] == (str(cpu.resolve()), CPU, {})
    assert s.get_providers() == CPU
    assert s.model_path_used == str(cpu.resolve())


def test_resilient_session_cpu_failure_propagates(graphs, monkeypatch):
    cpu, gpu = graphs
    calls = []
    monkeypatch.setattr(P, "build_session", _fake_build(calls, fail_on=cpu.name))
    with pytest.raises(RuntimeError):
        ResilientOnnxSession("bird_species", cpu, _Coord(CPU), gpu_model_path=gpu)
    assert len(calls) == 1


def test_non_tower_session_unchanged(graphs, monkeypatch):
    cpu, _ = graphs
    calls = []
    monkeypatch.setattr(P, "build_session", _fake_build(calls))
    ResilientOnnxSession("detector", cpu, _Coord(DML))
    assert calls == [(str(cpu.resolve()), DML, {})]


def test_siglip_tower_is_pinned_to_cpu_even_on_gpu(monkeypatch):
    """The SigLIP crop embedding is the default quality score; its stars must not
    depend on the machine, so it never gets a GPU EP (BioCLIP still does)."""
    from kestrel_analyzer.ml import provider_coordinator as pc
    from kestrel_analyzer.ml import providers as prov
    monkeypatch.setattr(prov, "tower_device_setting", lambda: "auto")
    coord = pc.ProviderCoordinator(user_gpu_enabled=True)
    coord._state = pc.ProviderState.GPU
    monkeypatch.setattr("kestrel_analyzer.ml.GPU_EP", "DmlExecutionProvider")
    monkeypatch.setattr(pc, "gpu_providers", lambda: ["DmlExecutionProvider", "CPUExecutionProvider"])
    assert coord.providers_for("siglip_image") == ["CPUExecutionProvider"]
    assert coord.providers_for("bird_species") == ["DmlExecutionProvider", "CPUExecutionProvider"]
    assert "siglip_image" in prov.CPU_PINNED_KINDS and "bird_species" not in prov.CPU_PINNED_KINDS


def test_placement_parses_utf16_ort_log():
    # ORT logs to fd 2 as UTF-16LE on Windows; the captured bytes must still parse.
    line = "[V:onnxruntime:, session_state.cc:1310] All nodes placed on [DmlExecutionProvider]. Number of nodes: 1"
    assert P.parse_placement(P._decode_ort_log(line.encode("utf-16-le"))) == {"DmlExecutionProvider": 1}
    assert P.parse_placement(P._decode_ort_log(line.encode("utf-8"))) == {"DmlExecutionProvider": 1}
