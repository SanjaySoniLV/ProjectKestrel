"""ML wrappers; use lazy exports so `import kestrel_analyzer.ml.speciesnet_taxonomy` does not load TensorFlow."""

from __future__ import annotations

import sys
from typing import Any

__all__ = ["BirdSpeciesClassifier", "QualityClassifier", "SpeciesNetSAMHQWrapper"]

# Platform-aware GPU execution provider for ONNX Runtime.
# macOS: CoreMLExecutionProvider (Apple Neural Engine / GPU via Core ML)
# Windows: DmlExecutionProvider (DirectX 12 GPU via DirectML)
# Linux/other: no bundled GPU EP in this project build (CPU-only fallback)
def _gpu_ep_for_platform(platform: str) -> str | None:
    if platform == "darwin":
        return "CoreMLExecutionProvider"
    if platform.startswith("win"):
        return "DmlExecutionProvider"
    return None


GPU_EP = _gpu_ep_for_platform(sys.platform)


def gpu_providers() -> list[str]:
    """Return ONNX Runtime execution providers list for GPU acceleration on the current platform."""
    if GPU_EP:
        return [GPU_EP, "CPUExecutionProvider"]
    return ["CPUExecutionProvider"]


def is_gpu_active(active_providers: list[str]) -> bool:
    """Return True if the platform GPU execution provider is active."""
    return bool(GPU_EP) and GPU_EP in active_providers


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
