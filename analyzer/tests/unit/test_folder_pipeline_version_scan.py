"""Folder-tree scan reports one pipeline version per folder (``pipeline_major``).

The app version stored in ``kestrel_metadata.json`` (``version`` = the build that
created the folder, ``kestrel_version`` = the build that last touched it) must not
decide whether a folder is outdated: a folder upgraded in place to pipeline 3
keeps its old creating ``version``, and a pipeline-3 folder analysed by an older
3.x build is still current.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ANALYZER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ANALYZER))

try:
    from api_bridge import Api
except Exception as e:  # pragma: no cover - environment-dependent
    pytest.skip(f"api_bridge not importable in this env: {e}", allow_module_level=True)

pytestmark = pytest.mark.unit


def _make_folder(root: Path, name: str, meta) -> Path:
    folder = root / name
    kdir = folder / ".kestrel"
    kdir.mkdir(parents=True)
    (kdir / "kestrel_database.csv").write_text("filename\n", encoding="utf-8")
    if meta is not None:
        text = meta if isinstance(meta, str) else json.dumps(meta)
        (kdir / "kestrel_metadata.json").write_text(text, encoding="utf-8")
    return folder


UPGRADED = {"version": "2.1.0", "kestrel_version": "3.0.0", "pipeline_major": 3,
            "upgraded_to_v3_utc": "2026-01-01T00:00:00Z"}


@pytest.mark.parametrize("meta,expected", [
    (UPGRADED, 3),
    ({"version": "3.0.0", "kestrel_version": "3.0.0", "pipeline_major": 3}, 3),
    ({"version": "2.4.1", "kestrel_version": "2.4.1"}, 2),   # pre-3: no pipeline_major
    ({"version": "2.4.1", "pipeline_major": None}, 2),
    ({}, 2),
    ("not json", None),                                        # unreadable: unknown
    ("[1, 2]", None),
    (None, None),                                              # no metadata file
])
def test_scan_pipeline_major(tmp_path, meta, expected):
    folder = _make_folder(tmp_path, "f", meta)
    path = str(folder / ".kestrel" / "kestrel_metadata.json")
    assert Api._scan_pipeline_major(path) == expected


def test_list_subfolders_reports_pipeline_major_not_app_versions(tmp_path):
    _make_folder(tmp_path, "upgraded", UPGRADED)
    _make_folder(tmp_path, "old", {"version": "2.1.0"})
    _make_folder(tmp_path, "nometa", None)
    (tmp_path / "plain").mkdir()
    _make_folder(tmp_path, "root_probe", {"version": "2.0.0", "pipeline_major": 3})

    api = Api.__new__(Api)
    res = api.list_subfolders(str(tmp_path), 1)
    assert res["success"], res
    by_name = {n["name"]: n for n in res["tree"]}

    assert by_name["upgraded"]["pipeline_major"] == 3
    assert by_name["old"]["pipeline_major"] == 2
    assert by_name["nometa"]["pipeline_major"] is None
    assert by_name["plain"]["pipeline_major"] is None
    assert by_name["root_probe"]["pipeline_major"] == 3
    for node in res["tree"]:
        assert "kestrel_version" not in node
    assert "root_kestrel_version" not in res
    assert res["root_pipeline_major"] is None  # tmp_path itself is not analysed


def test_list_subfolders_root_pipeline_major(tmp_path):
    (tmp_path / ".kestrel").mkdir()
    (tmp_path / ".kestrel" / "kestrel_database.csv").write_text("filename\n", encoding="utf-8")
    (tmp_path / ".kestrel" / "kestrel_metadata.json").write_text(json.dumps(UPGRADED), encoding="utf-8")
    res = Api.__new__(Api).list_subfolders(str(tmp_path), 1)
    assert res["success"], res
    assert res["root_pipeline_major"] == 3


# ── Frontend logic (runs the real functions under node when it is installed) ──

FOLDER_TREE_JS = (ANALYZER / "js" / "folder-tree.js").read_text(encoding="utf-8")


def _extract(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    return src[start:src.index(end_marker, start)]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_outdated_logic():
    code = (
        _extract(FOLDER_TREE_JS, "const CURRENT_PIPELINE_MAJOR", "/** Show a custom context menu")
        + """
const cases = [
  [{has_kestrel: true, pipeline_major: 3}, false, '3.0'],
  [{has_kestrel: true, pipeline_major: 2}, true, '2.x'],
  [{has_kestrel: true, pipeline_major: null}, false, ''],
  [{has_kestrel: true}, false, ''],
  [{has_kestrel: false, pipeline_major: 2}, false, '2.x'],
];
console.log(JSON.stringify(cases.map(([n]) => [isVersionOutdated(n), folderPipelineLabel(n)])));
"""
    )
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [[False, "3.0"], [True, "2.x"], [False, ""], [False, ""], [False, "2.x"]]


def test_no_js_consumer_compares_app_versions():
    for js in (ANALYZER / "js").glob("*.js"):
        assert "kestrel_version" not in js.read_text(encoding="utf-8"), js.name
    assert not re.search(r"isVersionOutdated[^}]*_appVersion", _extract(FOLDER_TREE_JS, "function isVersionOutdated", "function folderPipelineLabel"))
