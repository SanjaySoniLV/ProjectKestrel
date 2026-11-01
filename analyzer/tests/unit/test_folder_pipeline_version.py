"""One pipeline version per folder (``pipeline_version``) and how the app reads it.

Background: ``version`` (the build that created the folder) and
``kestrel_version`` (the build of the last run or v3 upgrade) used to be read as
if they described the pipeline. They are legacy fallbacks now. A folder upgraded
in place to pipeline 3 must read as 3.x, and a minor/patch difference never
makes a folder outdated.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ANALYZER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ANALYZER))

from kestrel_analyzer import config
from kestrel_analyzer.pipeline_version import (
    folder_pipeline_major,
    folder_pipeline_version,
    scan_summary,
    stamp_pipeline,
)

pytestmark = pytest.mark.unit

# Legacy metadata as the Arctic Tern build left it after an in-place upgrade:
# the creating build in ``version``, the upgrading build in ``kestrel_version``.
LEGACY_UPGRADED = {"version": "2.0.4", "kestrel_version": "3.0.0", "pipeline_major": 3,
                   "upgraded_to_v3_utc": "2026-10-30T00:00:00Z"}
LEGACY_2X = {"version": "2.0.4", "kestrel_version": "2.0.4"}
# A 2.x folder topped up by a 3.0 build: kestrel_version moved, the rows did not.
LEGACY_2X_TOPPED_UP = {"version": "2.0.4", "kestrel_version": "3.0.0"}
NEW_3X = {"pipeline_version": "3.0.0", "pipeline_major": 3}


def test_pipeline_major_is_derived_from_pipeline_version():
    assert config.PIPELINE_MAJOR == int(config.PIPELINE_VERSION.split(".")[0])
    assert config.VERSION == config.PIPELINE_VERSION  # deprecated alias


@pytest.mark.parametrize("meta,major,version", [
    (NEW_3X, 3, "3.0.0"),
    ({"pipeline_version": "3.1.2"}, 3, "3.1.2"),               # major from the full string
    (LEGACY_UPGRADED, 3, "3.0.0"),                             # upgraded: the upgrading build
    (LEGACY_2X, 2, "2.0.4"),
    (LEGACY_2X_TOPPED_UP, 2, "2.0.4"),                         # still 2.x rows: not 3.0.0
    ({"version": "3.0.0"}, 3, "3.0.0"),                        # fresh 3.0 folder, run cancelled
    ({"version": "1.2.0"}, 1, "1.2.0"),
    ({}, 2, ""),                                               # readable, nothing recorded
])
def test_read_folder_pipeline(meta, major, version):
    assert folder_pipeline_major(meta) == major
    assert folder_pipeline_version(meta) == version


def test_stamp_pipeline_replaces_the_legacy_stamps():
    meta = dict(LEGACY_UPGRADED, analyzer="x")
    stamp_pipeline(meta)
    assert meta["pipeline_version"] == config.PIPELINE_VERSION
    assert meta["pipeline_major"] == config.PIPELINE_MAJOR
    assert "version" not in meta and "kestrel_version" not in meta
    assert meta["upgraded_to_v3_utc"] and meta["analyzer"] == "x"  # untouched


def test_scan_summary():
    assert scan_summary(LEGACY_UPGRADED) == {
        "pipeline_major": 3, "pipeline_version": "3.0.0", "upgraded": True}
    assert scan_summary(LEGACY_2X) == {
        "pipeline_major": 2, "pipeline_version": "2.0.4", "upgraded": False}
    unknown = {"pipeline_major": None, "pipeline_version": None, "upgraded": False}
    assert scan_summary(None) == unknown
    assert scan_summary([1]) == unknown


def test_new_folder_metadata_records_only_the_pipeline_version(tmp_path):
    from kestrel_analyzer.database import load_database
    load_database(str(tmp_path), "test-analyzer")
    meta = json.loads((tmp_path / config.METADATA_FILENAME).read_text(encoding="utf-8"))
    assert meta["pipeline_version"] == config.PIPELINE_VERSION
    assert meta["pipeline_major"] == config.PIPELINE_MAJOR
    assert "version" not in meta and "kestrel_version" not in meta


# -- Folder-tree scan through the bridge --

try:
    from api_bridge import Api
except Exception:  # pragma: no cover - environment-dependent
    Api = None

needs_api = pytest.mark.skipif(Api is None, reason="api_bridge not importable")


def _make_folder(root: Path, name: str, meta) -> Path:
    folder = root / name
    kdir = folder / ".kestrel"
    kdir.mkdir(parents=True)
    (kdir / "kestrel_database.csv").write_text("filename\n", encoding="utf-8")
    if meta is not None:
        text = meta if isinstance(meta, str) else json.dumps(meta)
        (kdir / "kestrel_metadata.json").write_text(text, encoding="utf-8")
    return folder


@needs_api
@pytest.mark.parametrize("raw,major,version,upgraded", [
    (LEGACY_UPGRADED, 3, "3.0.0", True),
    (NEW_3X, 3, "3.0.0", False),
    (LEGACY_2X, 2, "2.0.4", False),
    ("not json", None, None, False),   # unreadable: unknown, never flagged
    ("[1, 2]", None, None, False),
    (None, None, None, False),         # no metadata file
])
def test_scan_pipeline(tmp_path, raw, major, version, upgraded):
    folder = _make_folder(tmp_path, "f", raw)
    got = Api._scan_pipeline(str(folder / ".kestrel" / "kestrel_metadata.json"))
    assert got == {"pipeline_major": major, "pipeline_version": version, "upgraded": upgraded}


@needs_api
def test_list_subfolders_reports_the_pipeline_not_app_versions(tmp_path):
    _make_folder(tmp_path, "upgraded", LEGACY_UPGRADED)
    _make_folder(tmp_path, "old", LEGACY_2X)
    _make_folder(tmp_path, "nometa", None)
    (tmp_path / "plain").mkdir()

    res = Api.__new__(Api).list_subfolders(str(tmp_path), 1)
    assert res["success"], res
    by_name = {n["name"]: n for n in res["tree"]}

    up = by_name["upgraded"]
    assert (up["pipeline_major"], up["pipeline_version"], up["upgraded"]) == (3, "3.0.0", True)
    assert by_name["old"]["pipeline_major"] == 2 and by_name["old"]["pipeline_version"] == "2.0.4"
    assert by_name["nometa"]["pipeline_major"] is None
    assert by_name["plain"]["pipeline_major"] is None
    for node in res["tree"]:
        assert "kestrel_version" not in node
    assert "root_kestrel_version" not in res


@needs_api
def test_list_subfolders_root_pipeline(tmp_path):
    (tmp_path / ".kestrel").mkdir()
    (tmp_path / ".kestrel" / "kestrel_database.csv").write_text("filename\n", encoding="utf-8")
    (tmp_path / ".kestrel" / "kestrel_metadata.json").write_text(json.dumps(LEGACY_UPGRADED), encoding="utf-8")
    res = Api.__new__(Api).list_subfolders(str(tmp_path), 1)
    assert res["success"], res
    assert (res["root_pipeline_major"], res["root_pipeline_version"], res["root_upgraded"]) == (3, "3.0.0", True)


@needs_api
def test_app_version_and_pipeline_version_are_separate():
    api = Api.__new__(Api)
    assert api.get_pipeline_version()["version"] == config.PIPELINE_VERSION
    app = api.get_app_version()["version"]
    assert app and app != config.PIPELINE_VERSION


# -- Frontend logic (runs the real functions under node when it is installed) --

FOLDER_TREE_JS = (ANALYZER / "js" / "folder-tree.js").read_text(encoding="utf-8")

JS_CASES = """
const cases = [
  {has_kestrel: true, pipeline_major: 3, pipeline_version: '3.0.0'},
  {has_kestrel: true, pipeline_major: 3, pipeline_version: '3.0.0', upgraded: true},
  {has_kestrel: true, pipeline_major: 3, pipeline_version: '3.0.0', stale_field: '1.0.0'},
  {has_kestrel: true, pipeline_major: 2, pipeline_version: '2.0.4'},
  {has_kestrel: true, pipeline_major: 2},
  {has_kestrel: true, pipeline_major: null},
  {has_kestrel: true},
  {has_kestrel: false, pipeline_major: 2},
];
console.log(JSON.stringify(cases.map(n => [isVersionOutdated(n), folderPipelineLabel(n), folderPipelineNote(n)])));
"""


def _extract(src: str, start_marker: str, end_marker: str) -> str:
    start = src.index(start_marker)
    return src[start:src.index(end_marker, start)]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_js_outdated_logic_uses_only_the_pipeline_major():
    code = _extract(FOLDER_TREE_JS, "const CURRENT_PIPELINE_MAJOR", "/** Show a custom context menu") + JS_CASES
    out = subprocess.run(["node", "-e", code], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout) == [
        [False, "3.0.0", "Pipeline 3.0.0"],
        [False, "3.0.0", "Pipeline 3.0.0 (upgraded in place from 2.x)"],
        [False, "3.0.0", "Pipeline 3.0.0"],
        [True, "2.0.4", "Pipeline 2.0.4"],
        [True, "2.x", "Pipeline 2.x"],
        [False, "", ""],
        [False, "", ""],
        [False, "2.x", ""],
    ]


def test_no_js_reads_app_style_folder_versions():
    for js in (ANALYZER / "js").glob("*.js"):
        text = js.read_text(encoding="utf-8")
        assert "kestrel_version" not in text, js.name
        assert "_appVersion;" not in text and "_appVersion =" not in text.replace("_appVersionNumber =", ""), js.name
