"""Frozen-build smoke test: bootstrap Windows ML, register vendor EPs, run a model on the OpenVINO GPU."""
import sys, os, time
import numpy as np
import onnxruntime as ort
import winml_setup

t = time.time()
eps = winml_setup.init(download=False, verbose=True)
print("registered:", list(eps), "in %.1fs" % (time.time() - t), flush=True)
model = sys.argv[1]
devs = [d for d in ort.get_ep_devices() if d.ep_name == "OpenVINOExecutionProvider" and str(d.device.type).endswith("GPU")]
so = ort.SessionOptions()
so.add_provider_for_devices(devs, {})
s = ort.InferenceSession(model, sess_options=so)
inp = s.get_inputs()[0]
shape = [d if isinstance(d, int) else 1 for d in inp.shape]
out = s.run(None, {inp.name: np.random.rand(*shape).astype(np.float32)})
print("frozen=%s providers=%s out=%s" % (getattr(sys, "frozen", False), s.get_providers(), out[0].shape), flush=True)
