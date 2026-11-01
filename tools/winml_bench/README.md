# winml_bench (experiment, branch exp/winml-benchmark)

Benchmark of Windows ML (`onnxruntime-windowsml` + OpenVINO EP from the Windows ML catalog) against
`onnxruntime-directml` for the Kestrel analysis models. Not production code.

- `winml_setup.py` bootstraps the Windows App SDK, downloads/readies catalog EPs and registers them with ORT.
- `capture_feeds.py` records real model inputs (per session kind) from a CPU pipeline run.
- `model_bench.py` / `run_matrix.py` load each model on a device, compare outputs with CPU and time it.
- `compare_runs.py` compares two analyzed folders (species, quality, stars, crops).
- `run_e2e.sh`, `timing_block.sh`, `stab.sh` are the end-to-end and stability drivers used (paths are for the author's machine).
- `frozen_smoke.py` is the PyInstaller smoke test.
- `results/` holds the raw JSON and logs.

The pipeline hook is in `analyzer/kestrel_analyzer/ml/providers.py` (`_new_session`), enabled only when
`KESTREL_WINML_DEVICE=ov_gpu|ov_npu|ov_cpu` is set (`KESTREL_WINML_DML_KINDS`, default `sam_dec,quality`,
stay on DirectML).
