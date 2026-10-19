"""Regression test: every DirectML device-lost error must demote GPU to CPU.

DirectML reports a lost device with the DXGI HRESULT in hex and Windows'
description text (e.g. ``887A0007 The GPU will not respond to more
commands...``), never the ``DXGI_ERROR_*`` symbol. Only the device-removed code
(887A0005) was matched, so a device hang (887A0006) or reset (887A0007) was
classified as an ordinary error: the coordinator did not demote and rebuild
sessions on CPU, and the image was marked errored instead of being retried.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.ml.provider_coordinator import (  # noqa: E402
    FailureAction,
    ProviderCoordinator,
    ProviderState,
    is_session_dead,
)

pytestmark = pytest.mark.unit


def _dml_error(code: str) -> RuntimeError:
    # Shape of the message ONNX Runtime's DirectML provider raises.
    return RuntimeError(
        "[ONNXRuntimeError] : 6 : RUNTIME_EXCEPTION : Non-zero status code returned "
        "while running DmlFusedNode_0_0 node. Name:'DmlFusedNode_0_0' Status Message: "
        "DmlGraphFusionHelper.cpp(1078)\\onnxruntime_pybind11_state.pyd!00007FF000000000: "
        f"Exception(2) tid(1234) {code} The GPU will not respond to more commands, most "
        "likely because some other application submitted invalid commands."
    )


@pytest.mark.parametrize("code", ["887A0005", "887A0006", "887A0007"])
def test_dml_device_lost_codes_are_session_dead(code):
    assert is_session_dead(_dml_error(code))


@pytest.mark.parametrize("code", ["887A0006", "887A0007"])
def test_device_hung_or_reset_demotes_to_cpu(code):
    coord = ProviderCoordinator(user_gpu_enabled=True)
    assert coord.on_run_failure(_dml_error(code)) is FailureAction.RECREATE_AND_RETRY
    assert coord.state is ProviderState.CPU


def test_unrelated_onnx_error_still_propagates():
    coord = ProviderCoordinator(user_gpu_enabled=True)
    exc = RuntimeError("[ONNXRuntimeError] : 2 : INVALID_ARGUMENT : Got invalid dimensions")
    assert coord.on_run_failure(exc) is FailureAction.PROPAGATE
    assert coord.state is ProviderState.GPU
