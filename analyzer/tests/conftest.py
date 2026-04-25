"""Shared test fixtures for the Kestrel test suite."""

import json
import tempfile
from pathlib import Path
import pytest
import pandas as pd
import sys

# Add analyzer to path so we can import kestrel_analyzer modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from kestrel_analyzer.database import BASE_COLUMNS, REQUIRED_COLUMNS


# ============================================================================
# Fixtures for real image test sets
# ============================================================================

@pytest.fixture
def fixtures_dir():
    """Return path to the fixtures directory."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def set_a_path(fixtures_dir):
    """Path to set_a_fresh/ (4 CR3 images, 2 scenes, no analysis)."""
    return fixtures_dir / "test_sets" / "set_a_fresh"


@pytest.fixture
def set_b_paths(fixtures_dir):
    """Dict of {ext: path} for diverse RAW formats in set_b_formats/."""
    test_sets_dir = fixtures_dir / "test_sets" / "set_b_formats"
    result = {}
    if test_sets_dir.exists():
        for ext in [".cr2", ".cr3", ".nef", ".arw", ".dng", ".orf", ".rw2", ".pef"]:
            for file in test_sets_dir.glob(f"*{ext}"):
                result[ext] = file
                break
    return result


@pytest.fixture
def set_c_path(fixtures_dir):
    """Path to set_c_preanalyzed/ (4 CR3s with .kestrel/ dir)."""
    return fixtures_dir / "test_sets" / "set_c_preanalyzed"


@pytest.fixture
def set_c_kestrel_dir(set_c_path):
    """Path to set_c_preanalyzed/.kestrel/"""
    return set_c_path / ".kestrel"


@pytest.fixture
def set_d_path(fixtures_dir):
    """Path to set_d_jpeg_only/ (4 JPEG-only images)."""
    return fixtures_dir / "test_sets" / "set_d_jpeg_only"


@pytest.fixture
def set_e_path(fixtures_dir):
    """Path to set_e_raw_jpg_mix/ (RAW+JPG pairs)."""
    return fixtures_dir / "test_sets" / "set_e_raw_jpg_mix"


# ============================================================================
# Fixtures for synthetic test data
# ============================================================================

@pytest.fixture
def temp_kestrel_dir(tmp_path):
    """Create a temporary .kestrel directory with minimal CSV and JSON."""
    kestrel_dir = tmp_path / ".kestrel"
    kestrel_dir.mkdir()

    # Create empty CSV with just headers
    csv_path = kestrel_dir / "kestrel_database.csv"
    headers = ",".join(BASE_COLUMNS)
    csv_path.write_text(headers + "\n")

    # Create empty scenedata JSON
    scenedata_path = kestrel_dir / "kestrel_scenedata.json"
    scenedata_path.write_text(json.dumps({}, indent=2))

    # Create minimal metadata JSON
    metadata_path = kestrel_dir / "kestrel_metadata.json"
    metadata_path.write_text(json.dumps({
        "kestrel_version": "2.0.1",
        "analyzer_name": "test_analyzer",
        "analyzed_utc": "2026-01-01T00:00:00Z"
    }, indent=2))

    return kestrel_dir


@pytest.fixture
def sample_database():
    """Create a minimal Pandas DataFrame matching BASE_COLUMNS."""
    data = {col: [] for col in BASE_COLUMNS}
    return pd.DataFrame(data)


@pytest.fixture
def temp_output_dir(tmp_path):
    """Create a temporary directory for test output."""
    return tmp_path / "output"
