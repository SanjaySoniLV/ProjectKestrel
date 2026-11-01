"""Scene viewer honours the per-folder "Ignore wildlife" setting.

Two layers, same split as ``test_scene_filmstrip_order.py``:

* source lints: the scene viewer reads labels through ``getRowDisplayLabels``
  instead of ``r.species`` / ``r.crop_path`` directly;
* behaviour: the real ``species.js`` + ``crop-data.js`` run in a Node ``vm``
  with a few stubbed globals (skipped when Node is not installed).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ANALYZER = Path(__file__).resolve().parents[2]
JS = ANALYZER / "js"
SCENE_DIALOG_JS = (JS / "scene-dialog.js").read_text(encoding="utf-8")
CROP_DATA_JS = (JS / "crop-data.js").read_text(encoding="utf-8")

pytestmark = pytest.mark.unit

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not installed")


# ── source lints ────────────────────────────────────────────────────────────

def _body(src, start, end):
    s = src.index(start)
    return src[s:src.index(end, s)]


def test_selected_image_labels_come_from_the_shared_helper():
    body = _body(SCENE_DIALOG_JS, "async function selectFilmstripImage(", "// Update filmstrip card active state")
    assert "getRowDisplayLabels(r, activeCrop)" in body
    for direct in ("r.species", "r.family", "r.crop_path", "r.species_confidence"):
        assert direct not in body


def test_tooltips_thumbnails_and_suggestions_do_not_read_row_labels_directly():
    assert "escapeHtml(r.species" not in SCENE_DIALOG_JS
    assert "r.export_path || r.crop_path" not in SCENE_DIALOG_JS
    assert SCENE_DIALOG_JS.count("          _sceneTooltipSpeciesLine(r),") == 2
    body = _body(SCENE_DIALOG_JS, "function _computeConfidenceWeightedSuggestion(", "function _computeSceneTagSuggestions(")
    assert "getRowDisplayLabels(row)" in body
    assert "row.species" not in body and "row.family" not in body


def test_family_chips_skip_hidden_wildlife_rows():
    body = _body(CROP_DATA_JS, "function _computeSceneTagsFromRows(", "function _currentSceneTagSettings(")
    assert "includeFamilies && !isHiddenWildlifeLabel(r, r.species)" in body


# ── behaviour ───────────────────────────────────────────────────────────────

_HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const [speciesJs, cropJs, payload] = process.argv.slice(1);
const ctx = vm.createContext({
  console, JSON, Number, String, Array, Object, Math, Set, Map,
  _folderSettings: {}, rootPath: '/photos',
  parseNumber: (v) => { const n = parseFloat(v); return Number.isFinite(n) ? n : -1; },
  getRowExposurePipelineMode: () => 'legacy_auto_bright_v1',
  getOrigin: () => '', el: () => null, getSetting: (_k, d) => d,
});
vm.runInContext(fs.readFileSync(speciesJs, 'utf8'), ctx);
vm.runInContext(fs.readFileSync(cropJs, 'utf8'), ctx);
const { ignore, rows, calls } = JSON.parse(payload);
ctx._folderSettings = { '/photos': { ignore_wildlife: ignore } };
ctx.__rows = rows;
const out = vm.runInContext(`__rows.map(r => ({
  labels: getRowDisplayLabels(r),
  onlyHidden: rowHasOnlyHiddenCrops(r),
  fallback: getRowFallbackCropPath(r),
  crops: getRowCropRecords(r).length,
}))`, ctx);
const tags = vm.runInContext(`_computeSceneTagsFromRows(__rows, 0, false, true)`, ctx);
console.log(JSON.stringify({ out, tags }));
"""


def _crop(i, species, family, subject="bird", path=None, conf=0.9):
    return {"crop_index": i, "crop_path": path or f"crop/c{i}.jpg", "species": species,
            "species_confidence": conf, "family": family, "family_confidence": conf, "subject": subject}


def _row(filename, crops, primary=0):
    p = crops[primary]
    return {"filename": filename, "crops_json": json.dumps(crops), "primary_crop_index": str(primary),
            "crop_path": p["crop_path"], "species": p["species"], "species_confidence": p["species_confidence"],
            "family": p["family"], "family_confidence": p["family_confidence"], "export_path": f"export/{filename}.jpg"}


def _run(rows, ignore):
    res = subprocess.run(
        [NODE, "-e", _HARNESS, str(JS / "species.js"), str(JS / "crop-data.js"),
         json.dumps({"ignore": ignore, "rows": rows})],
        capture_output=True, text=True, timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


BIRD = _crop(0, "Northern Cardinal", "Cardinals")
DEER = _crop(0, "White-tailed Deer", "N/A", subject="wildlife", path="crop/deer.jpg")
DEER1 = _crop(1, "White-tailed Deer", "N/A", subject="wildlife", path="crop/deer1.jpg")
BIRD1 = _crop(1, "Northern Cardinal", "Cardinals", path="crop/c1.jpg")


@needs_node
def test_setting_off_labels_every_crop_exactly_as_before():
    rows = [_row("bird.jpg", [BIRD]), _row("deer.jpg", [DEER]), _row("both.jpg", [DEER, BIRD1], primary=0)]
    res = _run(rows, ignore=False)["out"]
    assert [o["labels"]["species"] for o in res] == ["Northern Cardinal", "White-tailed Deer", "White-tailed Deer"]
    assert all(not o["labels"]["hidden"] and not o["onlyHidden"] for o in res)
    assert res[1]["labels"]["cropPath"] == "crop/deer.jpg" and res[1]["fallback"] == "crop/deer.jpg"
    assert res[2]["crops"] == 2


@needs_node
def test_wildlife_only_row_shows_no_label_and_no_crop_when_ignored():
    res = _run([_row("deer.jpg", [DEER])], ignore=True)["out"][0]
    assert res["onlyHidden"] and res["crops"] == 0
    assert res["labels"] == {"hidden": True, "cropPath": "", "species": "", "family": ""}
    assert res["fallback"] == ""


@needs_node
def test_bird_only_row_is_unchanged_when_ignored():
    res = _run([_row("bird.jpg", [BIRD])], ignore=True)["out"][0]
    assert not res["labels"]["hidden"]
    assert (res["labels"]["species"], res["labels"]["family"], res["labels"]["cropPath"]) == (
        "Northern Cardinal", "Cardinals", "crop/c0.jpg")


@needs_node
def test_mixed_row_with_wildlife_primary_labels_the_bird_crop():
    row = _row("both.jpg", [DEER, BIRD1], primary=0)
    res = _run([row], ignore=True)["out"][0]
    assert res["crops"] == 1 and not res["labels"]["hidden"]
    assert res["labels"]["species"] == "Northern Cardinal"
    assert res["labels"]["cropPath"] == "crop/c1.jpg"


@needs_node
def test_scene_chips_hide_wildlife_species_and_family_when_ignored():
    # A wildlife primary crop with a meaningful family must not leak that family either.
    deer_family = _crop(0, "White-tailed Deer", "Deer", subject="wildlife", path="crop/deer.jpg")
    rows = [_row("bird.jpg", [BIRD]), _row("deer.jpg", [deer_family])]
    on = _run(rows, ignore=True)["tags"]
    assert on == {"species": ["Northern Cardinal"], "families": ["Cardinals"]}
    off = _run(rows, ignore=False)["tags"]
    assert off == {"species": ["Northern Cardinal", "White-tailed Deer"], "families": ["Cardinals", "Deer"]}
