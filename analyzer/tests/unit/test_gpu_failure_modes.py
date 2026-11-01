"""Second GPU failure mode: a poisoned DirectML session, and runs where every frame fails.

Seen on 2026-11-01 while another process shared the Arc GPU: from one frame on,
every MegaDetector ``session.run`` raised
``DmlCommandRecorder.cpp(342) 80004005 Unspecified error`` (plus a few
``8007000E Not enough memory resources``). Nothing matched, so each frame was
written as an Error row (258 of them) while the progress counter kept rising.
A new process worked fine.

Covers: the new device-lost signatures (and what must NOT match), stopping a
run after N consecutive errored frames, and what the CLI and queue do then.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cli as cli_module  # noqa: E402
from kestrel_analyzer.ml.provider_coordinator import (  # noqa: E402
    FailureAction,
    ProviderCoordinator,
    is_device_lost,
    is_session_dead,
)
from queue_manager import QueueManager  # noqa: E402

pytestmark = pytest.mark.unit

LOST_5 = RuntimeError("ReadbackHeap.cpp(22) 887A0005 The GPU device instance has been suspended.")
DML_E_FAIL = RuntimeError(
    "[ONNXRuntimeError] : 1 : FAIL : Non-zero status code returned while running DmlFusedNode node. "
    "DmlCommandRecorder.cpp(342)\\onnxruntime_pybind11_state.pyd: 80004005 Unspecified error"
)
DML_OOM = RuntimeError(
    "DmlCommandRecorder.cpp(280) 8007000E Not enough memory resources are available "
    "to complete this operation."
)


# ---- signatures ---------------------------------------------------------------

@pytest.mark.parametrize("exc", [DML_E_FAIL, DML_OOM])
def test_poisoned_dml_session_errors_are_device_lost_and_session_dead(exc):
    assert is_device_lost(exc)
    assert is_session_dead(exc)


@pytest.mark.parametrize("text", [
    "80004005 Unspecified error",                     # generic code, no DirectML component
    "DmlCommandRecorder.cpp(342) Invalid argument",   # DirectML component, not E_FAIL
    "[ONNXRuntimeError] : 1 : FAIL : CPUExecutionProvider failed to allocate 4294967296 bytes",
    "Unable to allocate 2.00 GiB for an array",
    "Invalid input shape",
])
def test_unrelated_errors_are_not_device_lost(text):
    assert not is_device_lost(RuntimeError(text))
    assert not is_session_dead(RuntimeError(text))


def test_python_memoryerror_is_not_device_lost():
    assert not is_device_lost(MemoryError())


def test_poisoned_session_demotes_and_flags_gpu_lost():
    coord = ProviderCoordinator(user_gpu_enabled=True)
    assert coord.on_run_failure(DML_E_FAIL) is FailureAction.RECREATE_AND_RETRY
    assert coord.gpu_lost
    # Already on CPU: the same text from the CPU side must not demote again.
    assert coord.on_run_failure(DML_E_FAIL) is FailureAction.PROPAGATE


def test_cpu_run_with_dml_looking_text_is_not_flagged():
    coord = ProviderCoordinator(user_gpu_enabled=False)
    assert coord.on_run_failure(DML_E_FAIL) is FailureAction.PROPAGATE
    assert not coord.gpu_lost


def test_pipeline_gpu_in_use_follows_coordinator():
    from kestrel_analyzer.pipeline import AnalysisPipeline

    pipe = AnalysisPipeline.__new__(AnalysisPipeline)
    coord = ProviderCoordinator(user_gpu_enabled=True)
    pipe.sn_sam = MagicMock(coord=coord)
    assert pipe._gpu_in_use()
    coord.on_run_failure(LOST_5)          # demoted to CPU, but flagged lost
    assert pipe._gpu_in_use()
    cpu = AnalysisPipeline.__new__(AnalysisPipeline)
    cpu.sn_sam = MagicMock(coord=ProviderCoordinator(user_gpu_enabled=False))
    assert not cpu._gpu_in_use()


# ---- consecutive errored frames --------------------------------------------------

def test_tracker_stops_after_limit_and_resets_on_success():
    from kestrel_analyzer.pipeline import ConsecutiveErrorTracker

    t = ConsecutiveErrorTracker(3)
    assert not t.record_error()
    assert not t.record_error()
    t.record_success()                     # a good frame resets the streak
    assert not t.record_error()
    assert not t.record_error()
    assert t.record_error()                # third in a row after the reset
    assert t.total == 5


def test_tracker_single_bad_photo_never_stops_and_zero_disables():
    from kestrel_analyzer.pipeline import ConsecutiveErrorTracker

    t = ConsecutiveErrorTracker(10)
    assert not t.record_error()
    t.record_success()
    assert t.streak == 0
    off = ConsecutiveErrorTracker(0)
    assert not any(off.record_error() for _ in range(50))


def test_tracker_decode_errors_neither_count_nor_reset():
    from kestrel_analyzer.pipeline import ConsecutiveErrorTracker

    t = ConsecutiveErrorTracker(2)
    assert not t.record_error()
    assert not t.record_error(decode_error=True)   # a corrupt file in between
    assert t.streak == 1
    assert t.record_error()


def _failing_pipeline(tmp_path, n_files, decode_error=False):
    """Real AnalysisPipeline whose every frame fails in analysis (or in decode)."""
    import os

    from kestrel_analyzer.pipeline import AnalysisPipeline

    for i in range(n_files):
        (tmp_path / f"IMG_{i:03d}.jpg").write_bytes(b"x" * 10)
    pipe = AnalysisPipeline(use_gpu=False)
    pipe.load_models = lambda **k: None

    def decoded(files, folder, max_workers=3):
        for f in files:
            yield {"file_name": f, "image_path": os.path.join(folder, f), "raw_meter_scale": 1.0,
                   "error": RuntimeError("bad file") if decode_error else None, "img": None}

    pipe._iter_decoded = decoded
    return pipe


def test_process_folder_stops_after_consecutive_errors(tmp_path, monkeypatch):
    import csv

    monkeypatch.setattr("kestrel_analyzer.pipeline.time.sleep", lambda *_: None)
    pipe = _failing_pipeline(tmp_path, 8)
    msgs = []
    pipe.process_folder(str(tmp_path), callbacks={"on_status": msgs.append}, max_consecutive_errors=3)
    assert pipe.error_stop["consecutive"] == 3
    assert pipe.error_count == 3
    assert pipe.error_stop["last_file"] == "IMG_002.jpg"
    assert any("Stopped after 3 frames" in m for m in msgs)
    with open(tmp_path / ".kestrel" / "kestrel_database.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 3          # the other 5 frames were not processed, nor written as Error


def test_process_folder_default_does_not_stop(tmp_path, monkeypatch):
    monkeypatch.setattr("kestrel_analyzer.pipeline.time.sleep", lambda *_: None)
    pipe = _failing_pipeline(tmp_path, 5)
    pipe.process_folder(str(tmp_path), callbacks={})
    assert pipe.error_stop is None
    assert pipe.error_count == 5   # every frame ran, as before this change


def test_process_folder_decode_errors_do_not_trigger_the_stop(tmp_path, monkeypatch):
    monkeypatch.setattr("kestrel_analyzer.pipeline.time.sleep", lambda *_: None)
    pipe = _failing_pipeline(tmp_path, 6, decode_error=True)
    pipe.process_folder(str(tmp_path), callbacks={}, max_consecutive_errors=2)
    assert pipe.error_stop is None
    assert pipe.error_count == 6


def test_resume_after_error_stop_processes_the_frames_not_reached(tmp_path, monkeypatch):
    monkeypatch.setattr("kestrel_analyzer.pipeline.time.sleep", lambda *_: None)
    pipe = _failing_pipeline(tmp_path, 8)
    pipe.process_folder(str(tmp_path), callbacks={}, max_consecutive_errors=3)
    seen = []
    resumed = _failing_pipeline(tmp_path, 0)
    real = resumed._iter_decoded

    def spy(files, folder, max_workers=3):
        seen.extend(files)
        return real(files, folder, max_workers)

    resumed._iter_decoded = spy
    resumed.process_folder(str(tmp_path), callbacks={})
    # Without --retry-errored the 3 Error rows are skipped; the 5 unreached frames run.
    assert seen == [f"IMG_{i:03d}.jpg" for i in range(3, 8)]

    seen.clear()
    again = _failing_pipeline(tmp_path, 0)
    real2 = again._iter_decoded

    def spy2(files, folder, max_workers=3):
        seen.extend(files)
        return real2(files, folder, max_workers)

    again._iter_decoded = spy2
    again.process_folder(str(tmp_path), callbacks={}, retry_errored=True)
    assert "IMG_000.jpg" in seen      # errored rows are retried with retry_errored


# ---- CLI: error-stop exit code, summary, --fail-on-errors --------------------------

def _fake_inst(**attrs):
    inst = MagicMock()
    inst.gpu_lost = None
    inst.error_stop = None
    inst.error_count = 0
    inst.stage_times = {}
    inst.stage_counts = {}
    for k, v in attrs.items():
        setattr(inst, k, v)
    return inst


def _run_main_with(inst, tmp_path, *extra):
    folder = tmp_path / "photos"
    folder.mkdir(exist_ok=True)
    with patch.object(cli_module, "AnalysisPipeline") as cls:
        cls.return_value = inst
        return cli_module.main([str(folder), *extra])


def test_cli_forwards_consecutive_error_limit(tmp_path):
    inst = _fake_inst()
    _run_main_with(inst, tmp_path)
    assert inst.process_folder.call_args.kwargs["max_consecutive_errors"] == 10
    inst2 = _fake_inst()
    _run_main_with(inst2, tmp_path, "--max-consecutive-errors", "0")
    assert inst2.process_folder.call_args.kwargs["max_consecutive_errors"] == 0


def test_cli_error_stop_exits_5_without_restarting(tmp_path, capsys):
    inst = _fake_inst(error_count=10, error_stop={"consecutive": 10, "total_errors": 10,
                                                  "last_file": "IMG_9.CR3", "last_error": "boom"})
    with patch.object(cli_module, "run_gpu_restart_loop") as loop, pytest.raises(SystemExit) as ei:
        _run_main_with(inst, tmp_path)
    assert ei.value.code == cli_module.EXIT_ERROR_STOP == 5
    loop.assert_not_called()
    out = capsys.readouterr().out
    assert "10 frames in a row failed" in out and "--retry-errored" in out


def test_cli_errors_print_summary_and_keep_exit_zero_by_default(tmp_path, capsys):
    inst = _fake_inst(error_count=2)
    assert _run_main_with(inst, tmp_path) is None      # returns normally: exit code 0 as before
    out = capsys.readouterr().out
    assert "2 frame(s) ended as Error rows" in out and "--retry-errored" in out


def test_cli_fail_on_errors_exits_4(tmp_path):
    inst = _fake_inst(error_count=1)
    with pytest.raises(SystemExit) as ei:
        _run_main_with(inst, tmp_path, "--fail-on-errors")
    assert ei.value.code == cli_module.EXIT_FRAMES_ERRORED == 4


def test_cli_clean_run_with_fail_on_errors_exits_normally(tmp_path, capsys):
    inst = _fake_inst()
    assert _run_main_with(inst, tmp_path, "--fail-on-errors") is None
    assert "ended as Error rows" not in capsys.readouterr().out


# ---- queue: error stop is an item error, not a GPU-lost banner ---------------------

class _StopPipeline:
    def __init__(self):
        self.detector_name = "mdv5a"
        self.use_gpu = True
        self.gpu_lost = None
        self.max_errors = None
        self.error_stop = None

    def process_folder(self, folder, **kwargs):
        self.max_errors = kwargs.get("max_consecutive_errors")
        self.error_stop = {"consecutive": 10, "total_errors": 10, "last_file": "IMG_3.CR3",
                           "last_error": "RuntimeError: boom"}


def test_queue_error_stop_marks_item_error_without_gpu_banner(tmp_path):
    a = tmp_path / "a"
    a.mkdir()
    qm = QueueManager()
    qm._pipeline = _StopPipeline()
    qm.enqueue([str(a)])
    if qm._thread is not None:
        qm._thread.join(timeout=5)
    status = qm.get_status()
    item = status["items"][0]
    assert item["status"] == "error"
    assert "10 frames in a row" in item["error"] and "IMG_3.CR3" in item["error"]
    assert status["gpu_lost"] is None
    assert qm._pipeline.max_errors == 10
