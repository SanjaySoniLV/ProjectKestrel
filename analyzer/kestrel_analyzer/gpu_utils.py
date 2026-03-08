"""GPU detection and device management for CUDA, ROCm, and DirectML."""

import logging
import os
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class GPUBackend(Enum):
    CPU = "cpu"
    CUDA = "cuda"
    ROCM = "rocm"
    DIRECTML = "directml"


@dataclass
class GPUInfo:
    backend: GPUBackend
    device_name: str
    memory_mb: int


def detect_gpu() -> Optional[GPUInfo]:
    """Detect available GPU, checking ROCm before CUDA since ROCm uses CUDA API."""
    try:
        import torch
    except ImportError:
        logger.info("PyTorch not installed, using CPU")
        return None

    # Check ROCm (AMD) first — ROCm PyTorch exposes devices via torch.cuda API,
    # so we must check for ROCm before assuming NVIDIA CUDA.
    try:
        is_rocm = hasattr(torch.version, "hip") and torch.version.hip is not None
        if is_rocm and torch.cuda.is_available():
            device_name = torch.cuda.get_device_name(0)
            memory_mb = torch.cuda.get_device_properties(0).total_mem // (1024 * 1024)
            logger.info("AMD GPU detected via ROCm: %s (%d MB)", device_name, memory_mb)
            return GPUInfo(backend=GPUBackend.ROCM, device_name=device_name, memory_mb=memory_mb)
    except Exception as e:
        logger.debug("ROCm detection failed: %s", e)

    # Try CUDA (NVIDIA)
    try:
        if torch.cuda.is_available():
            device_name = torch.cuda.get_device_name(0)
            memory_mb = torch.cuda.get_device_properties(0).total_mem // (1024 * 1024)
            logger.info("NVIDIA GPU detected: %s (%d MB)", device_name, memory_mb)
            return GPUInfo(backend=GPUBackend.CUDA, device_name=device_name, memory_mb=memory_mb)
    except Exception as e:
        logger.debug("CUDA detection failed: %s", e)

    # Try DirectML (Windows — supports AMD, NVIDIA, and Intel GPUs)
    try:
        import torch_directml
        if torch_directml.is_available():
            device_name = torch_directml.device_name(0)
            # DirectML doesn't expose memory info directly
            logger.info("GPU detected via DirectML: %s", device_name)
            return GPUInfo(backend=GPUBackend.DIRECTML, device_name=device_name, memory_mb=0)
    except ImportError:
        logger.debug("torch-directml not installed")
    except Exception as e:
        logger.debug("DirectML detection failed: %s", e)

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
    if gpu is None:
        return torch.device("cpu")

    if gpu.backend in (GPUBackend.CUDA, GPUBackend.ROCM):
        return torch.device("cuda")

    if gpu.backend == GPUBackend.DIRECTML:
        import torch_directml
        return torch_directml.device(0)

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
        if sys.platform == "win32":
            hint = "pip install torch-directml onnxruntime-directml"
        else:
            hint = "pip install torch torchvision --index-url https://download.pytorch.org/whl/rocm6.2.4"
        return f"No GPU detected, using CPU. To enable GPU: {hint}"

    mem_info = f" ({gpu.memory_mb} MB)" if gpu.memory_mb > 0 else ""
    return f"{gpu.backend.value.upper()}: {gpu.device_name}{mem_info}"
