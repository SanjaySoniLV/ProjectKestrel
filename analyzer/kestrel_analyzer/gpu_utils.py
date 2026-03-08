"""GPU detection and device management for CUDA (NVIDIA) and ROCm (AMD)."""

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class GPUBackend(Enum):
    CPU = "cpu"
    CUDA = "cuda"
    ROCM = "rocm"


@dataclass
class GPUInfo:
    backend: GPUBackend
    device_name: str
    memory_mb: int


def detect_gpu() -> Optional[GPUInfo]:
    """Detect available GPU, preferring CUDA then ROCm, returning None if CPU-only."""
    # Try CUDA (NVIDIA) first
    try:
        import torch
        if torch.cuda.is_available():
            device_name = torch.cuda.get_device_name(0)
            memory_mb = torch.cuda.get_device_properties(0).total_mem // (1024 * 1024)
            logger.info("NVIDIA GPU detected: %s (%d MB)", device_name, memory_mb)
            return GPUInfo(backend=GPUBackend.CUDA, device_name=device_name, memory_mb=memory_mb)
    except Exception as e:
        logger.debug("CUDA detection failed: %s", e)

    # Try ROCm (AMD)
    try:
        import torch
        if hasattr(torch, "hip") or (hasattr(torch.version, "hip") and torch.version.hip is not None):
            if torch.cuda.is_available():  # ROCm uses the CUDA API in PyTorch
                device_name = torch.cuda.get_device_name(0)
                memory_mb = torch.cuda.get_device_properties(0).total_mem // (1024 * 1024)
                logger.info("AMD GPU detected via ROCm: %s (%d MB)", device_name, memory_mb)
                return GPUInfo(backend=GPUBackend.ROCM, device_name=device_name, memory_mb=memory_mb)
    except Exception as e:
        logger.debug("ROCm detection failed: %s", e)

    logger.info("No GPU detected, using CPU")
    return None


def get_torch_device(use_gpu: bool):
    """Return the best available torch device."""
    try:
        import torch
    except ImportError:
        logger.warning("PyTorch not installed, cannot determine device")
        return None

    if not use_gpu:
        return torch.device("cpu")

    gpu = detect_gpu()
    if gpu and gpu.backend in (GPUBackend.CUDA, GPUBackend.ROCM):
        return torch.device("cuda")
    return torch.device("cpu")


def get_onnx_providers(use_gpu: bool) -> list[str]:
    """Return ONNX Runtime execution providers in priority order."""
    if not use_gpu:
        return ["CPUExecutionProvider"]

    providers = []
    try:
        import onnxruntime as ort
        available = ort.get_available_providers()

        # NVIDIA CUDA
        if "CUDAExecutionProvider" in available:
            providers.append("CUDAExecutionProvider")

        # AMD ROCm
        if "ROCMExecutionProvider" in available:
            providers.append("ROCMExecutionProvider")

        # Windows DirectML (covers both AMD and NVIDIA on Windows)
        if "DmlExecutionProvider" in available:
            providers.append("DmlExecutionProvider")

    except ImportError:
        pass

    providers.append("CPUExecutionProvider")
    return providers


def configure_tensorflow_gpu(use_gpu: bool) -> None:
    """Configure TensorFlow GPU memory growth to avoid OOM errors."""
    try:
        import tensorflow as tf

        if not use_gpu:
            tf.config.set_visible_devices([], "GPU")
            logger.info("TensorFlow: GPU disabled, using CPU only")
            return

        gpus = tf.config.list_physical_devices("GPU")
        if gpus:
            for gpu in gpus:
                tf.config.experimental.set_memory_growth(gpu, True)
            logger.info("TensorFlow: %d GPU(s) configured with memory growth", len(gpus))
        else:
            logger.info("TensorFlow: No GPUs found, using CPU")
    except Exception as e:
        logger.warning("TensorFlow GPU configuration failed: %s", e)


def get_gpu_summary(use_gpu: bool) -> str:
    """Return a human-readable summary of GPU status."""
    if not use_gpu:
        return "GPU disabled (CPU mode)"

    gpu = detect_gpu()
    if gpu is None:
        return "No GPU detected, using CPU"
    return f"{gpu.backend.value.upper()}: {gpu.device_name} ({gpu.memory_mb} MB)"
