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


class TestPreserveReadFiresOnEverySave:
    """The merge must run on every save, not just the first one.

    The merge is guarded by "this column is absent from the caller's frame", and
    the merge itself adds that column. When ``save_database`` wrote into the
    caller's frame, the guard was satisfied once and failed forever after: the
    pipeline then re-wrote the snapshot captured on its *first* save over
    anything marked since.

    This needs no file lock, no error and no Windows -- only culling while
    analysis runs, which is the app's main workflow.
    """

    def test_marks_made_between_saves_survive(self, tmp_path):
        csv_path = tmp_path / "kestrel_database.csv"
        pipeline = _pipeline_row("IMG_001.CR3")

        # Image 1 analysed and saved.
        save_database(pipeline, str(csv_path))

        # User culls image 1 in the UI.
        disk = pd.read_csv(csv_path)
        disk["culled"] = 1
        disk["culled_origin"] = "manual"
        disk.to_csv(csv_path, index=False)

        # Image 2 analysed and saved -- picks up the mark.
        pipeline = pd.concat(
            [pipeline, _pipeline_row("IMG_002.CR3")], ignore_index=True
        )
        save_database(pipeline, str(csv_path))
        assert pd.read_csv(csv_path).loc[0, "culled"] == 1, (
            "the first save did not pick up the user's mark at all"
        )

        # User culls image 2 as well.
        disk = pd.read_csv(csv_path)
        disk.loc[disk["filename"] == "IMG_002.CR3", "culled"] = 1
        disk.to_csv(csv_path, index=False)
        assert pd.read_csv(csv_path)["culled"].tolist() == [1, 1]

        # Image 3 analysed and saved. Before the fix this wrote the frozen
        # first-save snapshot back and erased image 2's mark.
        pipeline = pd.concat(
            [pipeline, _pipeline_row("IMG_003.CR3")], ignore_index=True
        )
        save_database(pipeline, str(csv_path))

        culled = pd.read_csv(csv_path)["culled"].tolist()
        assert culled[:2] == [1, 1], (
            f"a cull made between saves was erased by the next save: {culled}"
        )

    def test_caller_frame_is_not_mutated(self, tmp_path):
        """The direct invariant, stated without going through a save sequence."""
        csv_path = tmp_path / "kestrel_database.csv"
        _disk_csv_with_culled(csv_path)

        pipeline = _pipeline_row("IMG_001.CR3")
        before = list(pipeline.columns)

        save_database(pipeline, str(csv_path))

        assert list(pipeline.columns) == before, (
            "save_database added a preserved column to the caller's frame; that "
            "silently disables the merge on every subsequent save"
        )
        assert "culled" not in pipeline.columns

    def test_an_explicit_culled_column_still_wins(self, tmp_path):
        """The guard's original intent is intact.

        A caller that genuinely supplies ``culled`` -- the UI saving its own
        edits -- must still beat what is on disk. The fix must not turn the
        disk copy into an unconditional override.
        """
        csv_path = tmp_path / "kestrel_database.csv"
        _disk_csv_with_culled(csv_path)  # disk says culled=1

        caller = _pipeline_row("IMG_001.CR3")
        caller["culled"] = 0
        caller["culled_origin"] = "manual"

        save_database(caller, str(csv_path))

        assert pd.read_csv(csv_path)["culled"].tolist() == [0], (
            "the caller's own culled value was overwritten from disk"
        )
