"""Cloud pack-merge must not truncate live .kestrel CSV/JSON on write failure.

Repro (FINDINGS S0-03): ``_merge_database_csv`` and ``_merge_scenedata_additive``
opened the destination with ``"w"``. Crash/ENOSPC after truncate left a
zero-length ``kestrel_database.csv`` / ``kestrel_scenedata.json``.
"""

import csv
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cloud_compute_client import (  # noqa: E402
    _merge_database_csv,
    _merge_scenedata_additive,
)


pytestmark = pytest.mark.unit


def _write_csv(path: Path, rows: list[dict]) -> bytes:
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path.read_bytes()


class TestMergeCsvAtomic:
    def test_failed_write_leaves_local_csv_byte_identical(self, tmp_path, monkeypatch):
        dst = tmp_path / "kestrel_database.csv"
        original = _write_csv(
            dst,
            [
                {
                    "filename": "IMG_001.CR3",
                    "species": "American Goldfinch",
                    "culled": "1",
                    "culled_origin": "manual",
                }
            ],
        )
        src = tmp_path / "pack.csv"
        _write_csv(
            src,
            [
                {
                    "filename": "IMG_001.CR3",
                    "species": "House Finch",
                    "culled": "0",
                    "culled_origin": "",
                },
                {
                    "filename": "IMG_002.CR3",
                    "species": "Northern Cardinal",
                    "culled": "0",
                    "culled_origin": "",
                },
            ],
        )

        def _boom(self, row):
            raise OSError("No space left on device")

        monkeypatch.setattr(csv.DictWriter, "writerow", _boom)

        with pytest.raises(OSError, match="No space left on device"):
            _merge_database_csv(src, dst)

        assert dst.read_bytes() == original
        leftover = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
        assert leftover == []

    def test_successful_merge_keeps_local_culled(self, tmp_path):
        dst = tmp_path / "kestrel_database.csv"
        _write_csv(
            dst,
            [
                {
                    "filename": "IMG_001.CR3",
                    "species": "American Goldfinch",
                    "culled": "1",
                    "culled_origin": "manual",
                }
            ],
        )
        src = tmp_path / "pack.csv"
        _write_csv(
            src,
            [
                {
                    "filename": "IMG_001.CR3",
                    "species": "House Finch",
                    "culled": "0",
                    "culled_origin": "",
                },
                {
                    "filename": "IMG_002.CR3",
                    "species": "Northern Cardinal",
                    "culled": "0",
                    "culled_origin": "",
                },
            ],
        )

        _merge_database_csv(src, dst)

        rows = {r["filename"]: r for r in csv.DictReader(dst.open(encoding="utf-8"))}
        assert rows["IMG_001.CR3"]["culled"] == "1"
        assert rows["IMG_001.CR3"]["culled_origin"] == "manual"
        assert rows["IMG_001.CR3"]["species"] == "American Goldfinch"
        assert rows["IMG_002.CR3"]["species"] == "Northern Cardinal"

    def test_replace_retries_permission_error(self, tmp_path, monkeypatch):
        dst = tmp_path / "kestrel_database.csv"
        _write_csv(
            dst,
            [
                {
                    "filename": "IMG_001.CR3",
                    "species": "American Goldfinch",
                    "culled": "1",
                    "culled_origin": "manual",
                }
            ],
        )
        src = tmp_path / "pack.csv"
        _write_csv(
            src,
            [
                {
                    "filename": "IMG_002.CR3",
                    "species": "Northern Cardinal",
                    "culled": "0",
                    "culled_origin": "",
                }
            ],
        )

        calls = {"n": 0}
        real_replace = os.replace

        def _flaky(src_path, dst_path):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PermissionError("sharing violation")
            return real_replace(src_path, dst_path)

        monkeypatch.setattr("cloud_compute_client.os.replace", _flaky)
        monkeypatch.setattr("kestrel_analyzer.database.time.sleep", lambda *_a, **_k: None)

        _merge_database_csv(src, dst)

        assert calls["n"] >= 2
        rows = {r["filename"]: r for r in csv.DictReader(dst.open(encoding="utf-8"))}
        assert rows["IMG_001.CR3"]["culled"] == "1"
        assert rows["IMG_002.CR3"]["species"] == "Northern Cardinal"

    def test_exhausted_permission_error_leaves_csv_intact(self, tmp_path, monkeypatch):
        dst = tmp_path / "kestrel_database.csv"
        original = _write_csv(
            dst,
            [
                {
                    "filename": "IMG_001.CR3",
                    "species": "American Goldfinch",
                    "culled": "1",
                    "culled_origin": "manual",
                }
            ],
        )
        src = tmp_path / "pack.csv"
        _write_csv(
            src,
            [
                {
                    "filename": "IMG_002.CR3",
                    "species": "Northern Cardinal",
                    "culled": "0",
                    "culled_origin": "",
                }
            ],
        )

        def _locked(_src, _dst):
            raise PermissionError("sharing violation")

        monkeypatch.setattr("cloud_compute_client.os.replace", _locked)
        monkeypatch.setattr("kestrel_analyzer.database.time.sleep", lambda *_a, **_k: None)

        with pytest.raises(PermissionError, match="sharing violation"):
            _merge_database_csv(src, dst)

        assert dst.read_bytes() == original


class TestMergeScenedataAtomic:
    def test_failed_write_leaves_local_json_byte_identical(self, tmp_path, monkeypatch):
        dst = tmp_path / "kestrel_scenedata.json"
        payload = {
            "version": "2.0",
            "image_ratings": {"IMG_001.CR3": 5},
            "scenes": {
                "1": {
                    "scene_id": "1",
                    "image_filenames": ["IMG_001.CR3"],
                    "name": "Keepers",
                    "status": "reviewed",
                    "user_tags": {"species": [], "families": [], "finalized": False},
                }
            },
        }
        dst.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        original = dst.read_bytes()
        src = tmp_path / "pack_scenedata.json"
        src.write_text(
            json.dumps(
                {
                    "version": "2.0",
                    "image_ratings": {"IMG_001.CR3": 0, "IMG_002.CR3": 3},
                    "scenes": {},
                }
            ),
            encoding="utf-8",
        )

        def _boom(*_a, **_k):
            raise OSError("No space left on device")

        monkeypatch.setattr("cloud_compute_client.json.dump", _boom)

        with pytest.raises(OSError, match="No space left on device"):
            _merge_scenedata_additive(src, dst)

        assert dst.read_bytes() == original
        leftover = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
        assert leftover == []

    def test_successful_merge_keeps_local_ratings(self, tmp_path):
        dst = tmp_path / "kestrel_scenedata.json"
        dst.write_text(
            json.dumps(
                {
                    "version": "2.0",
                    "image_ratings": {"IMG_001.CR3": 5},
                    "scenes": {},
                }
            ),
            encoding="utf-8",
        )
        src = tmp_path / "pack_scenedata.json"
        src.write_text(
            json.dumps(
                {
                    "version": "2.0",
                    "image_ratings": {"IMG_001.CR3": 0, "IMG_002.CR3": 3},
                    "scenes": {},
                }
            ),
            encoding="utf-8",
        )

        _merge_scenedata_additive(src, dst)

        merged = json.loads(dst.read_text(encoding="utf-8"))
        assert merged["image_ratings"]["IMG_001.CR3"] == 5
        assert merged["image_ratings"]["IMG_002.CR3"] == 3
