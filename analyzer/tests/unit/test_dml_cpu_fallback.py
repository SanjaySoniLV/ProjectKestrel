"""A lost DirectML device must not silently empty frames: per-component CPU fallback.

Observed failure: SAM's batched decoder hit a DirectML device-lost error (887A0005) while the detector
ran on a vendor EP. The decoder error was swallowed, so every later frame was saved as "Unknown" with
no detections and no Error row. These tests pin the replacement behaviour with fake sessions (no model
weights, no GPU):

* the failing session retries the same run on the CPU, so the frame keeps its detections,
* only DirectML sessions move; a session on a vendor EP stays where it is,
* a decode error is raised, never turned into a skipped box,
* the fallback is reported (status message, listener, log event); the database schema is untouched,
* SAM prompts are decoded one box per decoder run.
"""

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.ml import provider_coordinator as pc  # noqa: E402
from kestrel_analyzer.ml import providers as prov  # noqa: E402
from kestrel_analyzer.ml.provider_coordinator import ProviderCoordinator, ProviderState  # noqa: E402
from kestrel_analyzer.ml.resilient_session import ResilientOnnxSession  # noqa: E402
from kestrel_analyzer.ml import enhanced_gpu as eg  # noqa: E402
from kestrel_analyzer.ml.speciesnet_sam_hq import DetectorSAMHQWrapper, OnnxSamPredictor  # noqa: E402

pytestmark = pytest.mark.unit

LOST = RuntimeError("DmlCommandRecorder / ReadbackHeap.cpp(22) 887A0005 The GPU device instance has been suspended.")
H, W = 64, 96


class _Sess:
    """Stands in for ort.InferenceSession. ``dml`` sessions raise ``fail`` once they are 'lost'."""

    def __init__(self, providers, kind, device):
        self._providers = list(providers)
        self.kind = kind
        self.device = device
        self.calls = []

    def get_providers(self):
        return list(self._providers)

    def get_inputs(self):
        return [SimpleNamespace(name="x", shape=[1])]

    def get_outputs(self):
        return []

    def run(self, names, feed, run_options=None):
        self.calls.append(feed)
        if self.device.lost and self._providers[0] == "DmlExecutionProvider":
            raise LOST
        if self.kind == "sam_dec":
            mask = np.zeros((1, 1, H, W), dtype=np.float32)
            mask[0, 0, 10:30, 10:40] = 1.0
            return mask, np.array([[0.9]], dtype=np.float32), None
        return [self.kind]


class _Device:
    lost = False


@pytest.fixture
def world(monkeypatch):
    """A real coordinator and real ResilientOnnxSessions on top of fake ORT sessions."""
    dev = _Device()
    built = []
    monkeypatch.setattr(pc, "gpu_providers", lambda: ["DmlExecutionProvider", "CPUExecutionProvider"])
    # Towers follow the DirectML policy (on the GPU), even when the suite runs on macOS, where
    # the CoreML policy keeps them on the CPU.
    monkeypatch.setattr(pc, "tower_use_gpu", lambda ep: True)

    def fake_build(path, providers, kind="", config=None, vendor=None):
        provs = ["OpenVINOExecutionProvider"] if vendor else list(providers)
        s = _Sess(provs, kind, dev)
        built.append((kind, provs[0]))
        return s

    monkeypatch.setattr(prov, "build_session", fake_build)
    coord = ProviderCoordinator(user_gpu_enabled=True)
    spec = eg.VendorSpec("OpenVINOExecutionProvider", frozenset({"detector"}), (("detector", "mdv5a.onnx"),))
    monkeypatch.setattr(coord, "vendor_for", lambda kind, name="": spec if kind == "detector" else None)
    return SimpleNamespace(dev=dev, built=built, coord=coord)


def _session(world, kind, name="m.onnx"):
    return ResilientOnnxSession(kind, name, world.coord)


def test_decoder_device_lost_retries_the_same_run_on_the_cpu(world):
    dec = _session(world, "sam_dec")
    assert dec.get_providers()[0] == "DmlExecutionProvider"
    world.dev.lost = True
    feed = {"marker": 1}
    mask, iou, _ = dec.run(None, feed)  # raises nothing: same run, answered by the CPU session
    assert mask.shape == (1, 1, H, W) and float(iou[0, 0]) == pytest.approx(0.9)
    assert dec.get_providers() == ["CPUExecutionProvider"]
    assert dec._session.calls == [feed]
    assert [f["kind"] for f in world.coord.cpu_fallbacks] == ["sam_dec"]
    assert world.coord.state is ProviderState.GPU      # not a global demotion
    assert not world.coord.gpu_lost                    # so nothing stops the queue


def test_only_directml_sessions_move_the_vendor_session_stays(world):
    det = _session(world, "detector", "mdv5a.onnx")
    enc = _session(world, "sam_enc")
    dec = _session(world, "sam_dec")
    quality = _session(world, "siglip_image")  # CPU-pinned kind
    assert det.get_providers() == ["OpenVINOExecutionProvider"]
    det_session_before = det._session
    world.dev.lost = True
    dec.run(None, {})
    assert det.get_providers() == ["OpenVINOExecutionProvider"] and det._session is det_session_before
    assert enc.get_providers() == ["CPUExecutionProvider"]        # shared DirectML device: moved too
    assert quality.get_providers() == ["CPUExecutionProvider"]
    assert sorted(f["kind"] for f in world.coord.cpu_fallbacks) == ["sam_dec", "sam_enc"]


def test_sessions_built_after_the_loss_start_on_the_cpu(world):
    dec = _session(world, "sam_dec")
    world.dev.lost = True
    dec.run(None, {})
    late = _session(world, "bird_species")
    assert late.get_providers() == ["CPUExecutionProvider"]
    assert "bird_species" in [f["kind"] for f in world.coord.cpu_fallbacks]


def test_fallback_is_reported_once_with_plain_language(world):
    messages, events = [], []
    world.coord.update_status_cb(messages.append)
    world.coord.add_fallback_listener(lambda kind, reason: events.append((kind, reason)))
    dec = _session(world, "sam_dec")
    enc = _session(world, "sam_enc")
    world.dev.lost = True
    dec.run(None, {})
    dec.run(None, {})
    enc.run(None, {})
    assert len(messages) == 1
    assert "Restart Kestrel" in messages[0] and "CPU" in messages[0]
    assert sorted(k for k, _ in events) == ["sam_dec", "sam_enc"]
    assert all("887A0005" in r for _, r in events)


def test_an_error_that_is_not_a_lost_device_still_propagates(world):
    dec = _session(world, "sam_dec")
    dec._session.run = lambda *a, **k: (_ for _ in ()).throw(ValueError("bad shape"))
    with pytest.raises(ValueError):
        dec.run(None, {})
    assert world.coord.cpu_fallbacks == []


def test_vendor_session_that_loses_its_device_goes_to_the_cpu(world):
    det = _session(world, "detector", "mdv5a.onnx")
    det._session.run = lambda *a, **k: (_ for _ in ()).throw(LOST)
    assert det.run(None, {}) == ["detector"]
    assert det.get_providers() == ["CPUExecutionProvider"]
    assert [f["kind"] for f in world.coord.cpu_fallbacks] == ["detector"]


def test_a_vendor_ep_stays_available_after_a_cpu_demotion(monkeypatch):
    coord = ProviderCoordinator(user_gpu_enabled=True)
    spec = eg.VendorSpec("OpenVINOExecutionProvider", frozenset({"detector"}), (("detector", "mdv5a.onnx"),))
    monkeypatch.setattr(eg, "vendor_spec_for", lambda kind, name="": spec)
    coord._state = ProviderState.CPU
    assert coord.vendor_for("detector", "mdv5a.onnx") is spec
    coord._state = ProviderState.GPU_LOCKED_OUT
    assert coord.vendor_for("detector", "mdv5a.onnx") is None


def test_a_cpu_rebuild_does_not_demote_or_recreate_other_sessions(world, monkeypatch):
    det = _session(world, "detector", "mdv5a.onnx")
    dec = _session(world, "sam_dec")
    calls = []
    monkeypatch.setattr(world.coord, "recreate_all", lambda: calls.append("recreate_all"))
    monkeypatch.setattr(world.coord, "_demote", lambda **k: calls.append("demote"))
    world.dev.lost = True
    dec.run(None, {})
    assert calls == [] and world.coord.state is ProviderState.GPU
    assert det.get_providers() == ["OpenVINOExecutionProvider"]


def test_directml_is_never_retried_after_a_loss(world):
    dec = _session(world, "sam_dec")
    world.dev.lost = True
    dec.run(None, {})
    world.coord._state = ProviderState.CPU          # e.g. an unrelated later demotion
    world.coord._cpu_success_streak = 10_000
    assert world.coord.should_try_promote() is False
    assert world.coord.attempt_promotion() is False or world.coord.providers_for("sam_dec") == ["CPUExecutionProvider"]
    assert world.coord.providers_for("bird_species") == ["CPUExecutionProvider"]


def test_tower_that_moves_to_the_cpu_graph_is_flagged(world, tmp_path):
    gpu_graph = tmp_path / "bioclip_gpu.onnx"
    gpu_graph.write_bytes(b"x")
    tower = ResilientOnnxSession("bird_species", "bioclip_cpu.onnx", world.coord)
    tower._gpu_path = gpu_graph  # this session was built from the separate GPU graph
    dec = _session(world, "sam_dec")
    world.dev.lost = True
    dec.run(None, {})
    reasons = {f["kind"]: f["reason"] for f in world.coord.cpu_fallbacks}
    assert "CPU graph" in reasons["bird_species"] and "CPU graph" not in reasons["sam_dec"]


# ---- SAM: one box per decoder run, errors raise ---------------------------------------------

def _predictor(dec_session):
    p = OnnxSamPredictor.__new__(OnnxSamPredictor)
    p._dec_session = dec_session
    p._decoder_input_shapes = {}
    p._decoder_requires_padded_im_size = False
    p._batch_decode_runtime_failed = False
    p._batch_unsupported_logged = False
    p._supports_prompt_batching = p._detect_prompt_batch_support()
    return p


class _Dec:
    def __init__(self, fail_on_call=None):
        self.feeds = []
        self.fail_on_call = fail_on_call

    def get_inputs(self):  # a decoder graph that accepts batched prompts
        return [SimpleNamespace(name="point_coords", shape=["num_prompt_groups", 2, 2])]

    def run(self, names, feed, run_options=None):
        self.feeds.append(feed)
        if self.fail_on_call is not None and len(self.feeds) == self.fail_on_call:
            raise LOST
        mask = np.ones((1, 1, H, W), dtype=np.float32)
        return mask, np.array([[0.8]], dtype=np.float32), None


def test_sam_decodes_one_box_per_run_never_batched():
    dec = _Dec()
    p = _predictor(dec)
    boxes = [(1, 1, 10, 10), (20, 20, 30, 30), (40, 40, 50, 50)]
    out = p.decode_boxes(np.zeros((1, 4, 4, 4), np.float32), np.zeros((1, 1), np.float32), boxes, (32, 48), (H, W))
    assert len(out) == 3 and len(dec.feeds) == 3
    assert all(f["point_coords"].shape[0] == 1 and f["mask_input"].shape[0] == 1 for f in dec.feeds)
    assert p._supports_prompt_batching is False  # switched off even for a decoder that supports it


def test_sam_decode_error_is_raised_not_swallowed():
    p = _predictor(_Dec(fail_on_call=2))
    boxes = [(1, 1, 10, 10), (20, 20, 30, 30), (40, 40, 50, 50)]
    with pytest.raises(RuntimeError, match="887A0005"):
        p.decode_boxes(np.zeros((1, 4, 4, 4), np.float32), np.zeros((1, 1), np.float32), boxes, (32, 48), (H, W))


def _wrapper_with(predictor):
    w = DetectorSAMHQWrapper.__new__(DetectorSAMHQWrapper)
    det = SimpleNamespace(
        preprocess=lambda img: img,
        predict=lambda fp, inp: {"detections": [
            {"label": "animal", "conf": 0.9, "bbox": [0.1, 0.1, 0.3, 0.3]},
            {"label": "animal", "conf": 0.8, "bbox": [0.6, 0.5, 0.3, 0.3]},
        ]},
    )
    w.detector = det
    w.predictor = predictor
    w.max_bird_crops = 5
    w._ensure_detector = lambda: None
    w._ensure_sam = lambda: None
    return w


class _Pred:
    def __init__(self, decode):
        self._decode = decode

    def encode(self, img):
        return np.zeros(1), np.zeros(1), (32, 48), (H, W)

    def decode_boxes(self, *a):
        return self._decode(*a)


def test_decoder_failure_does_not_produce_an_empty_frame():
    def boom(*_a):
        raise LOST
    w = _wrapper_with(_Pred(boom))
    img = np.zeros((H, W, 3), np.uint8)
    with pytest.raises(RuntimeError, match="887A0005"):
        w._get_prediction_inner(img, "IMG_1.CR3")  # used to return ([], [], [], []) -> a false "Unknown"


def test_frame_keeps_its_detections_when_the_decoder_device_is_lost_mid_frame(world):
    """Detector on the vendor EP, decoder on DirectML that dies on its first run: the same frame is
    answered by the CPU decoder and both detections survive."""
    dec_session = _session(world, "sam_dec")
    predictor = _predictor(dec_session)
    world.dev.lost = True
    w = _wrapper_with(predictor)
    predictor.encode = lambda img: (np.zeros((1, 4, 4, 4), np.float32), np.zeros((1, 1), np.float32), (32, 48), (H, W))
    masks, boxes, classes, scores = w._get_prediction_inner(np.zeros((H, W, 3), np.uint8), "IMG_6267.CR3")
    assert masks is not None and len(classes) >= 1
    assert dec_session.get_providers() == ["CPUExecutionProvider"]
    assert [f["kind"] for f in world.coord.cpu_fallbacks] == ["sam_dec"]


# ---- pipeline: log event, CLI restart ---------------------------------

def _pipeline_stub(fallbacks):
    from kestrel_analyzer.pipeline import AnalysisPipeline
    p = AnalysisPipeline.__new__(AnalysisPipeline)
    p.cpu_fallback_events = []
    p._current_file = "IMG_6266.CR3"
    p._log_path = "unused.json"
    p.gpu_lost = None
    p.sn_sam = SimpleNamespace(coord=SimpleNamespace(
        cpu_fallbacks=fallbacks, gpu_lost=False, gpu_lost_reason=None))
    return p


def test_fallback_event_is_logged_with_the_frame(monkeypatch):
    import kestrel_analyzer.pipeline as pipeline_mod
    logged = []
    monkeypatch.setattr(pipeline_mod, "log_event", lambda path, rec: logged.append(rec))
    p = _pipeline_stub([])
    p._on_cpu_fallback("sam_dec", "887A0005 device removed")
    assert p.cpu_fallback_events[0]["component"] == "sam_dec"
    assert p.cpu_fallback_events[0]["file"] == "IMG_6266.CR3"
    assert logged[0]["event"] == "cpu_fallback" and logged[0]["reason"].startswith("887A0005")


def test_cli_stops_for_a_restart_after_a_fallback_but_the_app_does_not(monkeypatch):  # noqa: C901
    import kestrel_analyzer.pipeline as pipeline_mod
    monkeypatch.setattr(pipeline_mod, "log_event", lambda *a, **k: None)
    p = _pipeline_stub([{"kind": "sam_dec", "reason": "887A0005"}])
    assert p._note_gpu_lost("IMG_6266.CR3") is False        # the app (and any default caller) keeps going on the CPU
    assert p._note_gpu_lost("IMG_6266.CR3", include_cpu_fallback=True) is True   # the CLI restarts in a fresh process
    assert p.gpu_lost["file"] == "IMG_6266.CR3"
    assert _pipeline_stub([])._note_gpu_lost("IMG_1.CR3", include_cpu_fallback=True) is False


# ---- what the user is told --------------------------------------------------------------------

def test_status_strip_warns_that_steps_are_on_the_cpu_and_needs_a_restart(monkeypatch):
    coord = SimpleNamespace(cpu_fallbacks=[{"kind": "sam_dec"}], gpu_lost=False,
                            state=SimpleNamespace(value="gpu"))
    eg.reset_runtime()
    mode = eg.gpu_mode(True, coord)
    assert mode["mode"] == "component_cpu" and mode["needs_restart"] and mode["report"]
    assert "Restart Kestrel" in mode["detail"] and "CPU" in mode["label"]
    for word in ("DirectML", "sam_dec", "OpenVINO", "ONNX"):
        assert word not in mode["label"] + mode["detail"]


def test_status_text_names_the_vendor_without_model_names(monkeypatch):
    coord = SimpleNamespace(cpu_fallbacks=[], gpu_lost=False, state=SimpleNamespace(value="gpu"))
    eg.reset_runtime()
    eg.note_vendor_active("detector", "mdv5a.onnx")
    monkeypatch.setattr(eg, "current_spec",
                        lambda: eg.VendorSpec("OpenVINOExecutionProvider", frozenset(), ()))
    mode = eg.gpu_mode(True, coord)
    assert mode["label"] == "Enhanced GPU Performance Active (Intel OpenVINO)" and mode["detail"] == ""
    eg.reset_runtime()
