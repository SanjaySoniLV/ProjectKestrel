"""Register Windows ML execution providers with the Python onnxruntime env.

Experiment helper (branch exp/winml-benchmark). Needs onnxruntime-windowsml and the wasdk-* packages.
"""
import sys

_BOOT = None
_DONE = None


def init(download=True, verbose=True):
    """Bootstrap Windows App SDK, ensure vendor EPs are ready, register them. Returns {name: path}."""
    global _BOOT, _DONE
    if _DONE is not None:
        return _DONE
    import onnxruntime as ort
    import winui3.microsoft.windows.applicationmodel.dynamicdependency.bootstrap as bootstrap
    import winui3.microsoft.windows.ai.machinelearning as winml

    if _BOOT is None:
        _BOOT = bootstrap.initialize(options=bootstrap.InitializeOptions.ON_NO_MATCH_SHOW_UI)
        _BOOT.__enter__()
    catalog = winml.ExecutionProviderCatalog.get_default()
    out = {}
    for p in catalog.find_all_providers():
        if verbose:
            print("catalog:", p.name, p.ready_state, flush=True)
        if p.ready_state == winml.ExecutionProviderReadyState.NOT_PRESENT and not download:
            continue
        if p.ready_state != winml.ExecutionProviderReadyState.READY:
            r = p.ensure_ready_async().get()
            if verbose:
                print("  ensure_ready:", r.status, getattr(r, "diagnostic_text", ""), flush=True)
            if r.status != winml.ExecutionProviderReadyResultState.SUCCESS:
                continue
        ort.register_execution_provider_library(p.name, p.library_path)
        out[p.name] = p.library_path
    _DONE = out
    return out


if __name__ == "__main__":
    import onnxruntime as ort
    print(ort.__version__)
    print(init())
    for d in ort.get_ep_devices():
        print(d.ep_name, d.device.type, d.device.vendor, d.device.metadata)
