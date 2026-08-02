"""Unit tests for ONNX provider selection by platform."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer import ml


pytestmark = pytest.mark.unit


def test_linux_platform_uses_cpu_only_provider():
    assert ml._gpu_ep_for_platform("linux") is None


def test_windows_platform_uses_directml_provider():
    assert ml._gpu_ep_for_platform("win32") == "DmlExecutionProvider"


def test_macos_platform_uses_coreml_provider():
    assert ml._gpu_ep_for_platform("darwin") == "CoreMLExecutionProvider"
