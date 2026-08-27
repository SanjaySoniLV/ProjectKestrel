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

import json

from kestrel_analyzer.database import (
    _to_csv_atomic,
    read_database_csv,
    save_database,
    save_scenedata,
    write_json_atomic,
    write_text_atomic,
)
import kestrel_analyzer.database as _dbmod

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

        The reader goes through ``read_database_csv`` because that is what every
        reader in the app uses. On Windows the atomic write guarantees
        all-or-nothing content but not a collision-free ``open()``: a reader can
        still catch the destination mid-rename and get ``PermissionError``, which
        both sides absorb by retrying. A bare ``pd.read_csv`` here would be
        testing a call path the app does not have.

        The reader pauses briefly between reads to model the UI's auto-refresh
        timer. A zero-gap reopen loop can starve the writer past its retry window
        on Windows — see ``retry_on_file_lock`` — which is an artifact of the
        test harness rather than a race the app can hit.
        """
        db_path = str(tmp_path / "kestrel_database.csv")
        df = _frame()
        save_database(df, db_path)

        stop = threading.Event()
        errors = []
        reads = []
        write_errors = []

        def writer():
            while not stop.is_set():
                try:
                    save_database(df, db_path)
                except Exception as exc:  # noqa: BLE001 - recording for assert
                    write_errors.append(f"{type(exc).__name__}: {exc}")

        def reader():
            while not stop.is_set():
                try:
                    reads.append(len(read_database_csv(db_path)))
                except Exception as exc:  # noqa: BLE001 - recording for assert
                    errors.append(f"{type(exc).__name__}: {exc}")
                stop.wait(0.01)  # poll like the UI timer, don't spin

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        stop.wait(3.0)
        stop.set()
        for t in threads:
            t.join()

        assert not errors, f"{len(errors)} partial read(s), e.g. {errors[0]}"
        # A save that raises is a silently lost write — the failure mode the
        # writer-side retry exists to close.
        assert not write_errors, (
            f"{len(write_errors)} dropped save(s), e.g. {write_errors[0]}"
        )
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


class TestAtomicTextWrite:
    """write_text_atomic backs the UI CSV write path."""

    def test_writes_content_and_leaves_no_temp(self, tmp_path):
        p = tmp_path / "kestrel_scenedata.json"
        write_text_atomic(str(p), '{"a": 1}')
        assert p.read_text() == '{"a": 1}'
        assert [x.name for x in tmp_path.iterdir()] == ["kestrel_scenedata.json"]

    def test_existing_file_survives_a_failed_write(self, tmp_path, monkeypatch):
        """A crash/replace failure mid-write must leave the previous file intact."""
        p = tmp_path / "kestrel_scenedata.json"
        write_text_atomic(str(p), json.dumps({"good": True}))
        good = p.read_bytes()

        def boom(*_a, **_k):
            raise OSError("simulated crash during replace")

        monkeypatch.setattr(_dbmod.os, "replace", boom)
        with pytest.raises(OSError):
            write_text_atomic(str(p), '{"partial": ')  # deliberately truncated

        assert p.read_bytes() == good
        assert [x.name for x in tmp_path.iterdir()] == ["kestrel_scenedata.json"]


class TestAtomicJsonWrite:
    """write_json_atomic streams json.dump into the temp file (no dumps buffer)."""

    def test_roundtrips_and_leaves_no_temp(self, tmp_path):
        p = tmp_path / "kestrel_scenedata.json"
        obj = {"version": "2.0", "image_ratings": {"IMG_1.CR3": 5}, "scenes": {}}
        write_json_atomic(str(p), obj, indent=2)
        assert json.loads(p.read_text()) == obj
        assert [x.name for x in tmp_path.iterdir()] == ["kestrel_scenedata.json"]

    def test_streams_via_dump_not_dumps(self, tmp_path, monkeypatch):
        """Peak-memory path: serialize directly into the temp file."""
        dumped = {"via": None}

        real_dump = json.dump
        real_dumps = json.dumps

        def tracking_dump(obj, fp, *a, **k):
            dumped["via"] = "dump"
            return real_dump(obj, fp, *a, **k)

        def tracking_dumps(*a, **k):
            dumped["via"] = "dumps"
            return real_dumps(*a, **k)

        monkeypatch.setattr(_dbmod.json, "dump", tracking_dump)
        monkeypatch.setattr(_dbmod.json, "dumps", tracking_dumps)

        write_json_atomic(str(tmp_path / "kestrel_scenedata.json"), {"a": 1}, indent=2)
        assert dumped["via"] == "dump"

    def test_existing_file_survives_a_failed_write(self, tmp_path, monkeypatch):
        p = tmp_path / "kestrel_scenedata.json"
        write_json_atomic(str(p), {"good": True}, indent=2)
        good = p.read_bytes()

        def boom(*_a, **_k):
            raise OSError("simulated crash during replace")

        monkeypatch.setattr(_dbmod.os, "replace", boom)
        with pytest.raises(OSError):
            write_json_atomic(str(p), {"partial": True}, indent=2)

        assert p.read_bytes() == good
        assert [x.name for x in tmp_path.iterdir()] == ["kestrel_scenedata.json"]

    def test_save_scenedata_is_atomic_and_roundtrips(self, tmp_path, monkeypatch):
        scenedata = {"version": "2.0", "image_ratings": {"IMG_1.CR3": 5}, "scenes": {}}
        save_scenedata(scenedata, str(tmp_path))
        out = tmp_path / "kestrel_scenedata.json"
        assert json.loads(out.read_text()) == scenedata

        # A failed re-save must not destroy the previously saved decisions.
        good = out.read_bytes()

        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(_dbmod.os, "replace", boom)
        with pytest.raises(OSError):
            save_scenedata({"version": "2.0", "image_ratings": {}, "scenes": {}}, str(tmp_path))
        assert out.read_bytes() == good
        # no stray temp files
        assert sorted(x.name for x in tmp_path.iterdir()) == ["kestrel_scenedata.json"]

    def test_save_scenedata_does_not_call_dumps(self, tmp_path, monkeypatch):
        def fail_dumps(*_a, **_k):
            raise AssertionError("json.dumps must not be used")

        monkeypatch.setattr(_dbmod.json, "dumps", fail_dumps)
        save_scenedata({"version": "2.0", "image_ratings": {}, "scenes": {}}, str(tmp_path))
        assert (tmp_path / "kestrel_scenedata.json").exists()
