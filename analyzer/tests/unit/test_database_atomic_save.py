"""Regression tests for atomic kestrel_database.csv writes.

The analysis pipeline calls ``save_database`` after every processed image while
the UI auto-refresh timer reads the same path (``read_kestrel_csv`` /
``apply_normalization``). A non-atomic ``DataFrame.to_csv(path)`` truncates the
destination and streams rows into it, so a concurrent reader can observe a
partial file and raise ``EmptyDataError`` or ``ParserError``.
"""

import os
import sys
import threading
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from kestrel_analyzer.database import _to_csv_atomic, save_database

pytestmark = pytest.mark.unit


def _frame(rows: int = 2000) -> pd.DataFrame:
    """A frame whose quoted fields span commas, so truncation lands mid-string."""
    return pd.DataFrame(
        {
            "filename": [f"DSC_{i:04d}.NEF" for i in range(rows)],
            "quality": [0.5] * rows,
            "species": ["Northern Cardinal, adult male"] * rows,
            "crops_json": ['[{"x": 1, "y": 2, "label": "bird, perched"}]'] * rows,
        }
    )


class TestAtomicCsvWrite:
    def test_output_is_byte_identical_to_plain_to_csv(self, tmp_path):
        """The atomic path must not change the file pandas would have written."""
        df = _frame(50)
        plain = tmp_path / "plain.csv"
        atomic = tmp_path / "atomic.csv"

        df.to_csv(plain, index=False)
        _to_csv_atomic(df, str(atomic))

        assert atomic.read_bytes() == plain.read_bytes()

    def test_no_temp_files_left_behind(self, tmp_path):
        df = _frame(50)
        _to_csv_atomic(df, str(tmp_path / "kestrel_database.csv"))

        assert [p.name for p in tmp_path.iterdir()] == ["kestrel_database.csv"]

    def test_existing_file_survives_a_failed_write(self, tmp_path):
        """A write that raises must leave the previous database intact."""
        db_path = tmp_path / "kestrel_database.csv"
        _to_csv_atomic(_frame(10), str(db_path))
        good = db_path.read_bytes()

        class Exploding(pd.DataFrame):
            def to_csv(self, *a, **kw):
                raise OSError("disk full")

        with pytest.raises(OSError):
            _to_csv_atomic(Exploding(_frame(10)), str(db_path))

        assert db_path.read_bytes() == good
        assert [p.name for p in tmp_path.iterdir()] == ["kestrel_database.csv"]

    def test_concurrent_reads_never_see_a_partial_file(self, tmp_path):
        """Reproduces the production race: pipeline saves while the UI reads.

        Against the pre-fix ``database.to_csv(db_path, index=False)`` this fails
        within a few hundred milliseconds with the two signatures seen in crash
        reports: ``No columns to parse from file`` and ``EOF inside string``.
        """
        db_path = str(tmp_path / "kestrel_database.csv")
        df = _frame()
        save_database(df, db_path)

        stop = threading.Event()
        errors = []
        reads = []

        def writer():
            while not stop.is_set():
                save_database(df, db_path)

        def reader():
            while not stop.is_set():
                try:
                    reads.append(len(pd.read_csv(db_path)))
                except Exception as exc:  # noqa: BLE001 - recording for assert
                    errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        stop.wait(3.0)
        stop.set()
        for t in threads:
            t.join()

        assert not errors, f"{len(errors)} partial read(s), e.g. {errors[0]}"
        assert reads, "reader never completed a read"
        # Every successful read must see the whole database, never a prefix.
        assert set(reads) == {len(df)}

    def test_no_temp_files_survive_concurrent_saves(self, tmp_path):
        """Parallel saves each get a unique mkstemp path and clean up after."""
        db_path = str(tmp_path / "kestrel_database.csv")
        df = _frame(200)

        threads = [
            threading.Thread(target=lambda: [save_database(df, db_path) for _ in range(5)])
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(os.listdir(tmp_path)) == ["kestrel_database.csv"]
        assert len(pd.read_csv(db_path)) == len(df)
