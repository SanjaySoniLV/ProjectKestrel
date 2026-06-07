"""ML wrappers; use lazy exports so `import kestrel_analyzer.ml.speciesnet_taxonomy` does not load TensorFlow."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

__all__ = ["BirdSpeciesClassifier", "QualityClassifier", "SpeciesNetSAMHQWrapper"]

# Platform-aware GPU execution provider for ONNX Runtime.
# macOS: CoreMLExecutionProvider (Apple Neural Engine / GPU via Core ML)
# Windows: DmlExecutionProvider (DirectX 12 GPU via DirectML)
# Linux: CUDAExecutionProvider (per-model TRT EP added by gpu_providers_for)
GPU_EP = (
    "CoreMLExecutionProvider" if sys.platform == "darwin"
    else "CUDAExecutionProvider" if sys.platform == "linux"
    else "DmlExecutionProvider"
)

# --- Per-model TensorRT EP routing (Linux/CUDA) --------------------------------
# On Linux+CUDA, route the 4 validated models through TensorrtExecutionProvider
# with a pre-built engine cache, one cache dir per model so different graph
# hashes don't collide.
#
# Both YOLO detectors use kind="detector" in the calling code, so routing must
# be by model basename, not kind alone.
_TRT_ENGINE_CACHE_BY_BASENAME = {
    "model.onnx": "bird_species_classifier",
    "mdv5a.onnx": "detector_mdv5a",
    "mdv1000-cedar.onnx": "detector_mdv1000_cedar",
    "speciesNet_v4.0.1a.onnx": "speciesnet_classifier",
}
_TRT_ENGINE_CACHE_ROOT = "/kestrel_source/analyzer/models/trt_engines"


def gpu_providers() -> list[str]:
    """Return the default GPU provider list (no per-model TRT routing).
    Use `gpu_providers_for(kind, model_path)` when you have a model path —
    that variant routes TRT-eligible models through TensorrtExecutionProvider
    on Linux."""
    return [GPU_EP, "CPUExecutionProvider"]


def gpu_providers_for(kind: str, model_path: Path | str | None = None) -> list:
    """Provider list for one session, routing TRT-eligible models through
    TensorrtExecutionProvider on Linux. Falls back to plain GPU providers
    when the model isn't in the TRT allowlist or when we're not on Linux.

    Returns a list whose entries are either a string (provider name) or a
    (provider_name, options_dict) tuple — both forms accepted by ORT."""
    base = [GPU_EP, "CPUExecutionProvider"]
    if sys.platform != "linux" or model_path is None:
        return base
    basename = os.path.basename(str(model_path))
    cache_subdir = _TRT_ENGINE_CACHE_BY_BASENAME.get(basename)
    if cache_subdir is None:
        return base
    return [
        ("TensorrtExecutionProvider", {
            "trt_engine_cache_enable": True,
            "trt_engine_cache_path": f"{_TRT_ENGINE_CACHE_ROOT}/{cache_subdir}",
            "trt_fp16_enable": True,
            "trt_max_workspace_size": 1 << 30,
        }),
        "CUDAExecutionProvider",
        "CPUExecutionProvider",
    ]


def is_gpu_active(active_providers: list[str]) -> bool:
    """Return True if the platform GPU execution provider is active.
    Also returns True if TensorrtExecutionProvider is active (it's a GPU EP)."""
    return GPU_EP in active_providers or "TensorrtExecutionProvider" in active_providers


def __getattr__(name: str) -> Any:
    if name == "BirdSpeciesClassifier":
        from .bird_species import BirdSpeciesClassifier

        return BirdSpeciesClassifier
    if name == "QualityClassifier":
        from .quality import QualityClassifier

        return QualityClassifier
    if name == "SpeciesNetSAMHQWrapper":
        from .speciesnet_sam_hq import SpeciesNetSAMHQWrapper

        return SpeciesNetSAMHQWrapper
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
