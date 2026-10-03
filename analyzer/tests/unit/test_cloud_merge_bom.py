"""A Cloud Compute pack merge must not drop local rows after a UI save.

``api_bridge.write_kestrel_csv`` -- the desktop UI's save path -- writes
kestrel_database.csv with a UTF-8 byte-order mark. ``_merge_database_csv`` read
it as plain utf-8, so the BOM became part of the first header and "filename"
read as "\\ufefffilename". No local row then had a "filename" key, every one was
skipped as keyless, and the merged file contained only the pack's rows: the
first pack merged after any save from the UI deleted every row already in the
folder, and left a junk BOM-named column in the header.
"""

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from cloud_compute_client import _merge_database_csv

pytestmark = pytest.mark.unit

HEADER = "filename,species,scene_count,culled"


def _write(path: Path, rows, encoding):
    path.write_text(
        "\r\n".join([HEADER] + [",".join(r) for r in rows]) + "\r\n", encoding=encoding
    )


def _read(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        return list(reader.fieldnames or []), list(reader)


@pytest.mark.parametrize("local_encoding", ["utf-8", "utf-8-sig"])
def test_local_rows_survive_merge_regardless_of_bom(tmp_path, local_encoding):
    dst = tmp_path / "kestrel_database.csv"
    _write(dst, [(f"LOCAL_{i}.CR3", "Teal", "0", "accept") for i in range(20)], local_encoding)
    src = tmp_path / "pack.csv"
    _write(src, [(f"CLOUD_{i}.CR3", "Teal", "1", "") for i in range(5)], "utf-8")

    _merge_database_csv(src, dst)

    fields, rows = _read(dst)
    names = {r["filename"] for r in rows}
    assert len(rows) == 25, f"local rows lost ({local_encoding})"
    assert all(f"LOCAL_{i}.CR3" in names for i in range(20))
    # The user's cull decisions on local rows are kept verbatim.
    assert all(r["culled"] == "accept" for r in rows if r["filename"].startswith("LOCAL_"))
    assert fields == HEADER.split(","), f"header corrupted: {fields!r}"


def test_bom_in_pack_csv_is_also_tolerated(tmp_path):
    dst = tmp_path / "kestrel_database.csv"
    _write(dst, [("LOCAL_0.CR3", "Teal", "0", "")], "utf-8")
    src = tmp_path / "pack.csv"
    _write(src, [("CLOUD_0.CR3", "Teal", "1", "")], "utf-8-sig")

    _merge_database_csv(src, dst)

    fields, rows = _read(dst)
    assert {r["filename"] for r in rows} == {"LOCAL_0.CR3", "CLOUD_0.CR3"}
    assert fields == HEADER.split(",")
