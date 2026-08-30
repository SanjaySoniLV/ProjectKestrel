"""save_database must not drop UI culled flags when the preserve-read fails.

S0-04: a bare ``except Exception: pass`` around the on-disk read let the
pipeline write BASE_COLUMNS over a CSV that already had ``culled=manual``.
"""

from pathlib import Path
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.database import BASE_COLUMNS, save_database


pytestmark = pytest.mark.unit


def _pipeline_row(filename: str = "IMG_001.CR3") -> pd.DataFrame:
    row = {col: None for col in BASE_COLUMNS}
    row["filename"] = filename
    row["species"] = "aves"
    row["quality"] = 0.5
    return pd.DataFrame([row])


def _disk_csv_with_culled(path: Path) -> bytes:
    df = _pipeline_row()
    df["culled"] = 1
    df["culled_origin"] = "manual"
    df.to_csv(path, index=False)
    return path.read_bytes()


class TestSaveDatabasePreserveCulled:
    def test_read_failure_leaves_culled_csv_intact(self, tmp_path, monkeypatch):
        csv_path = tmp_path / "kestrel_database.csv"
        original = _disk_csv_with_culled(csv_path)

        def boom(*_a, **_k):
            raise OSError("simulated disk read failure")

        monkeypatch.setattr(
            "kestrel_analyzer.database.read_database_csv", boom
        )

        with pytest.raises(OSError, match="simulated disk read failure"):
            save_database(_pipeline_row(), str(csv_path))

        assert csv_path.read_bytes() == original

    def test_missing_file_still_writes_pipeline_frame(self, tmp_path):
        csv_path = tmp_path / "kestrel_database.csv"
        save_database(_pipeline_row(), str(csv_path))
        reloaded = pd.read_csv(csv_path)
        assert reloaded.loc[0, "filename"] == "IMG_001.CR3"
        assert "culled" not in reloaded.columns
