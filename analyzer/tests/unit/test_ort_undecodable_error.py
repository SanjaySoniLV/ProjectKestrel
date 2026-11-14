"""ONNX Runtime errors whose message is not UTF-8 must still be classified and logged.

On a Windows install whose ANSI code page is not UTF-8 (cp1252 for Spanish, French, ...), a
DirectML failure message carries Windows' localized description of the HRESULT. Python
decodes ONNX Runtime's message as UTF-8, so an accented letter turns the real error into
``UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe1 ...``. ``str()`` of that names
neither the HRESULT nor the component, so a lost GPU was not recognised (no CPU fallback;
every later image errored) and the logs never showed the real cause.
"""

import locale
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.ml import provider_coordinator as pc  # noqa: E402
from kestrel_analyzer.ml import providers as prov  # noqa: E402
from kestrel_analyzer.ml.provider_coordinator import (  # noqa: E402
    FailureAction,
    ProviderCoordinator,
    error_text,
    is_device_lost,
    is_session_dead,
)
from kestrel_analyzer.ml.resilient_session import ResilientOnnxSession  # noqa: E402

pytestmark = pytest.mark.unit

# A DirectML message with a Spanish-language description of 887A0006 (device hung),
# encoded in cp1252 the way Windows' FormatMessageA returns it.
_HUNG_ES = (
    "[ONNXRuntimeError] : 6 : RUNTIME_EXCEPTION : Non-zero status code returned while running "
    "DmlFusedNode_0_0 node. Status Message: DmlGraphFusionHelper.cpp(1078)\\"
    "onnxruntime_pybind11_state.pyd!00007FF000000000: Exception(2) tid(1234) 887A0006 "
    "La GPU no responderá a más comandos."
).encode("cp1252")
_OTHER_ES = (
    "[ONNXRuntimeError] : 1 : FAIL : DmlExecutionProvider 80070057 El parámetro no es correcto."
).encode("cp1252")


def _undecodable(raw: bytes) -> UnicodeDecodeError:
    """Build the exception exactly as Python raises it for a non-UTF-8 C++ message."""
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return e
    raise AssertionError("expected the sample to be invalid UTF-8")


@pytest.fixture(autouse=True)
def _cp1252(monkeypatch):
    monkeypatch.setattr(locale, "getpreferredencoding", lambda do_setlocale=True: "cp1252")


def test_the_raw_symptom_hides_the_hresult():
    exc = _undecodable(_HUNG_ES)
    assert "887A0006" not in str(exc)  # why plain str(exc) classification missed it


def test_error_text_recovers_the_localized_message():
    text = error_text(_undecodable(_HUNG_ES))
    assert "887A0006" in text and "responderá" in text
    assert error_text(RuntimeError("plain")) == "plain"
    assert error_text("text") == "text"
    assert error_text(None) == ""


def test_error_text_survives_an_unknown_code_page(monkeypatch):
    monkeypatch.setattr(locale, "getpreferredencoding", lambda do_setlocale=True: "no-such-codec")
    assert "887A0006" in error_text(_undecodable(_HUNG_ES))


def test_undecodable_device_lost_is_classified():
    exc = _undecodable(_HUNG_ES)
    assert is_device_lost(exc)
    assert is_session_dead(exc)
    coord = ProviderCoordinator(user_gpu_enabled=True)
    assert coord.on_run_failure(exc) is FailureAction.RECREATE_AND_RETRY


def test_an_ordinary_unicode_error_is_not_device_lost():
    exc = _undecodable(_OTHER_ES)
    assert not is_device_lost(exc)
    assert not is_session_dead(exc)


# ---- through the session wrapper (fake ORT sessions, no model weights, no GPU) ----

class _Sess:
    def __init__(self, providers, raw):
        self._providers = list(providers)
        self._raw = raw
        self.calls = 0

    def get_providers(self):
        return list(self._providers)

    def run(self, names, feed, run_options=None):
        self.calls += 1
        if self._providers[0] == "DmlExecutionProvider":
            raise _undecodable(self._raw)
        return ["ok"]


def _wrapped(monkeypatch, raw):
    monkeypatch.setattr(pc, "gpu_providers", lambda: ["DmlExecutionProvider", "CPUExecutionProvider"])
    monkeypatch.setattr(prov, "build_session",
                        lambda path, providers, kind="", config=None, vendor=None: _Sess(providers, raw))
    coord = ProviderCoordinator(user_gpu_enabled=True)
    monkeypatch.setattr(coord, "vendor_for", lambda kind, name="": None)
    return ResilientOnnxSession("sam_dec", "m.onnx", coord), coord


def test_wrapper_falls_back_to_cpu_on_undecodable_device_lost(monkeypatch):
    sess, coord = _wrapped(monkeypatch, _HUNG_ES)
    assert sess.get_providers()[0] == "DmlExecutionProvider"
    assert sess.run(None, {}) == ["ok"]  # same run, answered on the CPU
    assert sess.get_providers() == ["CPUExecutionProvider"]
    assert [f["kind"] for f in coord.cpu_fallbacks] == ["sam_dec"]
    assert "message not UTF-8" in coord.cpu_fallbacks[0]["reason"]


def test_wrapper_reraises_other_errors_with_readable_text(monkeypatch):
    sess, _ = _wrapped(monkeypatch, _OTHER_ES)
    with pytest.raises(RuntimeError) as info:
        sess.run(None, {})
    assert "80070057" in str(info.value) and "parámetro" in str(info.value)
    assert isinstance(info.value.__cause__, UnicodeDecodeError)
    assert sess.get_providers()[0] == "DmlExecutionProvider"  # not a device loss: no fallback
