"""Placement diagnostics must not leave sessions logging at VERBOSE.

ORT only reports node placement at VERBOSE, and a session's log level is fixed
when it is built, so a session built at VERBOSE logs on every ``run()`` (one
``execution_frame.cc`` "fall back to default allocation" line per
dynamically-sized tensor). ``build_session`` therefore probes placement with a
throwaway VERBOSE session and returns one built at the default level.
"""

import base64
import sys

import numpy as np
import pytest

ort = pytest.importorskip("onnxruntime")

from kestrel_analyzer.ml import providers as P

pytestmark = pytest.mark.unit

CPU = ["CPUExecutionProvider"]

# shape (int64[2]) -> ConstantOfShape(1.0) -> Relu -> ReduceSum. The intermediate
# tensors are sized by the input's *values*, so every run with a new shape
# misses ORT's cached memory pattern and logs the fallback line at VERBOSE.
# Built with onnx.helper (opset 13, IR 7); kept as bytes so the test needs no
# onnx package and no model weights.
_MODEL_B64 = (
    "CAcSADqVAQo5CgVzaGFwZRIEb25lcyIPQ29uc3RhbnRPZlNoYXBlKhkKBXZhbHVlKg0IARABIgQAAIA/QgF2oAEECg8KBG9uZXMS"
    "AXIiBFJlbHUKIgoBchIBeSIJUmVkdWNlU3VtKg8KCGtlZXBkaW1zGACgAQISAWdaEwoFc2hhcGUSCgoICAcSBAoCCAJiCwoBeRIG"
    "CgQIARIAQgQKABAN"
)


def _ort_emits_logs() -> bool:
    """False for the onnxruntime-windowsml build (ORT 1.25.2), which writes no log text to fd 2, so
    placement counts (and per-run VERBOSE lines) cannot be captured there."""
    import os
    import tempfile
    path = os.path.join(tempfile.mkdtemp(), "dyn.onnx")
    with open(path, "wb") as f:
        f.write(base64.b64decode(_MODEL_B64))
    sys.stderr.flush()
    tmp, saved = tempfile.TemporaryFile(mode="w+b"), os.dup(2)
    os.dup2(tmp.fileno(), 2)
    try:
        so = ort.SessionOptions()
        so.log_severity_level = 0
        ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])
    finally:
        os.dup2(saved, 2)
        os.close(saved)
    tmp.seek(0)
    return b"onnxruntime" in tmp.read().replace(b"\x00", b"")


requires_ort_log = pytest.mark.skipif(
    not _ort_emits_logs(), reason="this ONNX Runtime build (onnxruntime-windowsml) writes no log text")


@pytest.fixture
def model(tmp_path, monkeypatch):
    monkeypatch.setenv("KESTREL_ORT_PLACEMENT_DIAG", "1")
    P.clear()
    path = tmp_path / "dyn.onnx"
    path.write_bytes(base64.b64decode(_MODEL_B64))
    yield str(path)
    P.clear()


def _run_varied(sess):
    for n in (2, 8, 3, 16):
        sess.run(None, {"shape": np.array([n, n], np.int64)})


def _verbose_lines(capfd):
    # ORT writes UTF-16LE to fd 2 on Windows; every character checked is ASCII.
    err = capfd.readouterr().err.replace("\x00", "")
    return [ln for ln in err.splitlines() if "[V:onnxruntime" in ln]


@requires_ort_log
def test_model_logs_per_run_at_verbose(model, capfd):
    """Guard against a vacuous test: a VERBOSE session does log during run()."""
    so = ort.SessionOptions()
    so.log_severity_level = 0
    sess = ort.InferenceSession(model, sess_options=so, providers=CPU)
    capfd.readouterr()
    _run_varied(sess)
    assert _verbose_lines(capfd)


@requires_ort_log
def test_probed_session_keeps_placement_and_runs_quiet(model, capfd, monkeypatch):
    monkeypatch.setattr(P, "_needs_probe", lambda providers: True)
    sess = P.build_session(model, CPU, kind="dyn")
    assert P.placement_summary()[-1]["nodes"] == {"CPUExecutionProvider": 3}
    capfd.readouterr()
    _run_varied(sess)
    assert _verbose_lines(capfd) == []


@requires_ort_log
def test_placement_is_probed_once_per_model(model, monkeypatch):
    calls = []
    real = P._probe_placement
    monkeypatch.setattr(P, "_needs_probe", lambda providers: True)
    monkeypatch.setattr(P, "_probe_placement", lambda *a: calls.append(1) or real(*a))
    P.build_session(model, CPU, kind="dyn")
    P.build_session(model, CPU, kind="dyn")  # e.g. a coordinator recreate
    assert len(calls) == 1
    assert [r["nodes"] for r in P.placement_summary()] == [{"CPUExecutionProvider": 3}] * 2


def test_cpu_only_session_is_not_probed_and_runs_quiet(model, capfd, monkeypatch):
    monkeypatch.setattr(P, "_probe_placement", lambda *a: pytest.fail("CPU-only list was probed"))
    sess = P.build_session(model, CPU, kind="siglip_image")
    rec = P.placement_summary()[-1]
    assert rec["actual"] == CPU and rec["nodes"] == {}
    capfd.readouterr()
    _run_varied(sess)
    assert _verbose_lines(capfd) == []
