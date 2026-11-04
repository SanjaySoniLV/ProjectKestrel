"""GPU loss (Windows GPU reset): detection, stop at an image boundary, CLI restart, queue banner.

A lost DirectML device cannot be recovered inside the process that lost it
(every session rebuild then fails with 887A0020); a new process gets the GPU
back. These tests cover the pieces that make "stop and continue in a fresh
process" work, using fakes so no model weights or GPU are needed.
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
    ProviderState,
    ResilienceConfig,
    is_device_lost,
    is_session_dead,
)
from queue_manager import QueueManager  # noqa: E402

pytestmark = pytest.mark.unit

LOST_5 = RuntimeError("ReadbackHeap.cpp(22) 887A0005 The GPU device instance has been suspended.")
LOST_20 = RuntimeError(
    "dml_provider_factory.cc(645) 887A0020 An internal issue prevented the driver "
    "from carrying out the specified operation. The driver state is probably suspect"
)


# ---- coordinator classification -------------------------------------------

def test_887A0020_is_session_dead_and_device_lost():
    assert is_session_dead(LOST_20)
    assert is_device_lost(LOST_20)
    assert is_device_lost("DXGI_ERROR_DRIVER_INTERNAL_ERROR")


def test_ordinary_errors_are_not_device_lost():
    assert not is_device_lost(RuntimeError("Invalid input shape"))
    assert not is_device_lost(None)


def test_device_removed_demotion_flags_gpu_lost():
    coord = ProviderCoordinator(user_gpu_enabled=True)
    assert not coord.gpu_lost
    assert coord.on_run_failure(LOST_5) is FailureAction.RECREATE_AND_RETRY
    assert coord.gpu_lost
    assert "887A0005" in coord.gpu_lost_reason
    coord.clear_gpu_lost()
    assert not coord.gpu_lost


def test_non_device_demotion_does_not_flag_gpu_lost():
    coord = ProviderCoordinator(user_gpu_enabled=True)
    coord.on_run_failure(RuntimeError("model_path must not be empty"))
    assert coord.state is ProviderState.CPU
    assert not coord.gpu_lost


def test_failed_promotion_with_887A0020_flags_gpu_lost():
    coord = ProviderCoordinator(
        user_gpu_enabled=True, cfg=ResilienceConfig(max_demotions_before_lockout=0)
    )

    def recreate(gpu):
        if gpu:
            raise LOST_20

    coord.register_recreate_callback(recreate)
    coord._state = ProviderState.CPU
    assert coord.attempt_promotion() is False
    assert coord.gpu_lost


def test_lockout_flags_gpu_lost_even_for_unclassified_reasons():
    coord = ProviderCoordinator(
        user_gpu_enabled=True, cfg=ResilienceConfig(max_demotions_before_lockout=1)
    )
    coord.on_run_failure(RuntimeError("model_path must not be empty"))
    assert coord.state is ProviderState.GPU_LOCKED_OUT
    assert coord.gpu_lost


def test_cpu_only_run_never_reports_gpu_lost():
    coord = ProviderCoordinator(user_gpu_enabled=False)
    coord._flag_gpu_lost("887A0005", locked_out=True)
    assert not coord.gpu_lost


# ---- pipeline records the file that triggered the loss ---------------------

def test_pipeline_note_gpu_lost_records_file_and_reason():
    from kestrel_analyzer.pipeline import AnalysisPipeline

    pipe = AnalysisPipeline.__new__(AnalysisPipeline)
    pipe._log_path = None
    coord = ProviderCoordinator(user_gpu_enabled=True)
    pipe.sn_sam = MagicMock(coord=coord)
    msgs = []
    assert pipe._note_gpu_lost("IMG_1.CR3", msgs.append) is False
    assert pipe.__dict__.get("gpu_lost") is None
    coord.on_run_failure(LOST_5)
    with patch("kestrel_analyzer.pipeline.log_event"):
        assert pipe._note_gpu_lost("IMG_7.CR3", msgs.append) is True
    assert pipe.gpu_lost["file"] == "IMG_7.CR3"
    assert "887A0005" in pipe.gpu_lost["reason"]
    assert msgs


# ---- CLI restart loop --------------------------------------------------------

def test_restart_loop_reruns_until_child_stops_reporting_loss():
    calls = []
    codes = iter([cli_module.EXIT_GPU_LOST, cli_module.EXIT_GPU_LOST, 0])

    def runner(cmd):
        calls.append(cmd)
        return next(codes)

    rc = cli_module.run_gpu_restart_loop(
        ["/photos", "--gpu-child"], first_attempt=1, max_restarts=3, runner=runner
    )
    assert rc == 0
    assert len(calls) == 3
    attempts = [c[c.index("--gpu-restart-attempt") + 1] for c in calls]
    assert attempts == ["1", "2", "3"]
    assert all(c.count("--gpu-child") == 1 for c in calls)
    assert all("/photos" in c for c in calls)


def test_restart_loop_is_capped():
    calls = []

    def runner(cmd):
        calls.append(cmd)
        return cli_module.EXIT_GPU_LOST

    rc = cli_module.run_gpu_restart_loop(["/photos"], first_attempt=1, max_restarts=2, runner=runner)
    assert len(calls) == 2
    assert rc == cli_module.EXIT_GPU_LOST


def test_main_restarts_in_fresh_process_when_pipeline_reports_gpu_loss(tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    with patch.object(cli_module, "AnalysisPipeline") as cls, \
            patch.object(cli_module, "run_gpu_restart_loop", return_value=0) as loop:
        inst = MagicMock()
        inst.gpu_lost = {"file": "IMG_9.CR3", "reason": "887A0005"}
        cls.return_value = inst
        with pytest.raises(SystemExit) as ei:
            cli_module.main([str(folder)])
    assert ei.value.code == 0
    assert inst.process_folder.call_args.kwargs["stop_on_gpu_loss"] is True
    loop.assert_called_once()
    assert loop.call_args.kwargs["first_attempt"] == 1
    assert loop.call_args.kwargs["max_restarts"] == 3


def test_main_child_exits_with_restart_code(tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    with patch.object(cli_module, "AnalysisPipeline") as cls:
        inst = MagicMock()
        inst.gpu_lost = {"file": "IMG_9.CR3", "reason": "887A0005"}
        cls.return_value = inst
        with pytest.raises(SystemExit) as ei:
            cli_module.main([str(folder), "--gpu-child", "--gpu-restart-attempt", "1"])
    assert ei.value.code == cli_module.EXIT_GPU_LOST


def test_last_attempt_does_not_stop_on_gpu_loss(tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    with patch.object(cli_module, "AnalysisPipeline") as cls:
        inst = MagicMock()
        inst.gpu_lost = None
        cls.return_value = inst
        cli_module.main([str(folder), "--gpu-child", "--gpu-restart-attempt", "3"])
    assert inst.process_folder.call_args.kwargs["stop_on_gpu_loss"] is False


def test_no_gpu_or_zero_restarts_never_stops(tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    for extra in (["--no-gpu"], ["--max-gpu-restarts", "0"]):
        with patch.object(cli_module, "AnalysisPipeline") as cls:
            inst = MagicMock()
            inst.gpu_lost = None
            cls.return_value = inst
            cli_module.main([str(folder), *extra])
        assert inst.process_folder.call_args.kwargs["stop_on_gpu_loss"] is False


def test_child_command_source_and_frozen(monkeypatch):
    cmd = cli_module._child_command(["x"])
    assert cmd[0] == sys.executable and cmd[-1] == "x" and cmd[1].endswith("cli.py")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert cli_module._child_command(["x"]) == [sys.executable, "--cli", "x"]


# ---- queue: banner state, no further work on a dead device ----------------------

class _LossPipeline:
    """Fake pipeline: the first folder loses the GPU, as a real one would."""

    def __init__(self):
        self.detector_name = "mdv5a"
        self.use_gpu = True
        self.gpu_lost = None
        self.folders = []
        self.stop_flags = []

    def process_folder(self, folder, **kwargs):
        self.folders.append(folder)
        self.stop_flags.append(kwargs.get("stop_on_gpu_loss"))
        self.gpu_lost = {"file": "IMG_5.CR3", "reason": "887A0005 suspended"}


def _wait_idle(qm):
    t = qm._thread
    if t is not None:
        t.join(timeout=5)


def test_queue_stops_and_reports_gpu_lost(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    qm = QueueManager()
    qm._pipeline = _LossPipeline()
    qm.enqueue([str(a), str(b)])
    _wait_idle(qm)

    status = qm.get_status()
    assert status["gpu_lost"]["file"] == "IMG_5.CR3"
    items = {it["name"]: it for it in status["items"]}
    assert items["a"]["status"] == "error" and "Restart Kestrel" in items["a"]["error"]
    assert items["b"]["status"] == "error"
    assert qm._pipeline.folders == [str(a)]
    assert qm._pipeline.stop_flags == [True]


def test_queue_without_gpu_loss_reports_none(tmp_path):
    class _Ok(_LossPipeline):
        def process_folder(self, folder, **kwargs):
            self.folders.append(folder)

    a = tmp_path / "a"
    a.mkdir()
    qm = QueueManager()
    qm._pipeline = _Ok()
    qm.enqueue([str(a)])
    _wait_idle(qm)
    assert qm.get_status()["gpu_lost"] is None
    assert qm.get_status()["items"][0]["status"] == "done"
