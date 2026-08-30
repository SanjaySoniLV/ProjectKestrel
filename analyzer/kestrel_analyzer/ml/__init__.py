"""ML wrappers; use lazy exports so `import kestrel_analyzer.ml.speciesnet_taxonomy` does not load TensorFlow."""

from __future__ import annotations

import sys
from typing import Any

__all__ = ["BirdSpeciesClassifier", "QualityClassifier", "SpeciesNetSAMHQWrapper"]

# Platform-aware GPU execution provider for ONNX Runtime.
# macOS: CoreMLExecutionProvider (Apple Neural Engine / GPU via Core ML)
# Windows: DmlExecutionProvider (DirectX 12 GPU via DirectML)
GPU_EP = "CoreMLExecutionProvider" if sys.platform == "darwin" else "DmlExecutionProvider"


def gpu_providers() -> list[str]:
    """Return ONNX Runtime execution providers list for GPU acceleration on the current platform."""
    return [GPU_EP, "CPUExecutionProvider"]


def is_gpu_active(active_providers: list[str]) -> bool:
    """Return True if the platform GPU execution provider is active."""
    return GPU_EP in active_providers


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
