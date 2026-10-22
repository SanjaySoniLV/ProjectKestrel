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
